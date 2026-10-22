"""Vorschläge erzeugen: scannen, zerlegen, abgleichen, Zielpfade und Konflikte (Spec 5, 7.1, 7.2, 7.5)."""

import os
from collections.abc import Callable, Hashable, Sequence
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from mediasort.db import Folder, Ignored, Item, Scan, casefold
from mediasort.library import LibraryIndex, ShowVideo, movie_versions, show_videos
from mediasort.matcher import match
from mediasort.mediainfo import ProbeError
from mediasort.models import (
    AudioStream,
    Candidate,
    Confidence,
    FolderType,
    ItemStatus,
    MediaInfo,
    MediaKind,
    ParsedName,
    Subtitle,
    VideoFile,
)
from mediasort.namer import episode_relpath, movie_relpath, subtitle_targets
from mediasort.parser import normalize_title, parse_name
from mediasort.quality import better, quality_key, short_label
from mediasort.scanner import scan
from mediasort.settings import Settings
from mediasort.tmdb import TmdbClient, TmdbError

SERIES_NOTE = "sieht aus wie eine Serienfolge"
NO_EPISODE_NOTE = "keine Folgennummer erkannt"
ALREADY_CORRECT_NOTE = "Datei liegt bereits richtig"
SMALL_VIDEO_HINT = "Kleine Videodatei übersprungen (unter Sample-Grenze): {}"
_OPEN_STATUSES = (ItemStatus.NEW.value, ItemStatus.CONFIRMED.value)
_REPLANNABLE = (ItemStatus.NEW.value, ItemStatus.ERROR.value, ItemStatus.CONFIRMED.value)
_CONFIDENCE_ORDER = (Confidence.LOW, Confidence.MEDIUM, Confidence.HIGH)
_SUGGESTIONS = {"a": "keep_new", "b": "keep_existing"}
# Herkunft einer vorhandenen Version (``existing_meta[].origin``)
ORIGIN_LIBRARY = "library"  # liegt in der Bibliothek
ORIGIN_SCAN = "scan"  # Quelle eines anderen Vorschlags desselben Scans
ORIGIN_OPEN = "open"  # Quelle eines offenen Vorschlags aus einem anderen Scan


@dataclass
class PlanContext:
    session_factory: Callable[[], Session]
    settings: Settings
    tmdb: TmdbClient
    probe: Callable[[Path], MediaInfo | None]
    now: Callable[[], float]


class RootMissing(Exception):
    """Wurzel des Ordners oder einer Zielbibliothek fehlt (Meldung auf Deutsch)."""


class NoTarget(Exception):
    """Für diese Art von Datei ist keine Zielbibliothek eingetragen (Meldung auf Deutsch)."""


class ItemNotFound(KeyError):
    """Der zu korrigierende Vorschlag existiert nicht (mehr)."""


class NotReplannable(ValueError):
    """Der Vorschlag ist in einem Status, der keine Korrektur zulässt (Meldung auf Deutsch)."""


# --- Zielbibliothek -----------------------------------------------------------------


def _target_folder(folder: Folder, kind: MediaKind, is_3d: bool, folders: dict[int, Folder]) -> Folder:
    if folder.role == "library":
        return folder
    if kind == MediaKind.EPISODE:
        target_id, label = folder.series_target_id, "Serien"
    elif is_3d or folder.media_type == FolderType.MOVIE3D:
        target_id, label = folder.movie3d_target_id, "3D-Filme"
    else:
        target_id, label = folder.movie_target_id, "Filme"
    target = folders.get(target_id) if target_id is not None else None
    if target is None:
        raise NoTarget(f"Kein Ziel für {label} konfiguriert")
    return target


def target_root(folder: Folder, kind: MediaKind, is_3d: bool, folders: dict[int, Folder]) -> Path:
    """Bibliothek, in die eine Datei aus ``folder`` gehört; ``NoTarget``, wenn keine eingetragen ist."""
    return Path(_target_folder(folder, kind, is_3d, folders).path)


def _check_roots(folder: Folder, folders: dict[int, Folder]) -> None:
    paths = [folder.path]
    if folder.role != "library":
        for target_id in (folder.movie_target_id, folder.series_target_id, folder.movie3d_target_id):
            if target_id is not None and target_id in folders:
                paths.append(folders[target_id].path)
    for path in paths:
        if not Path(path).is_dir():
            raise RootMissing(f"Ordner nicht gefunden: {path}")


# --- JSON ---------------------------------------------------------------------------


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return value


def _as_json(obj: Any) -> Any:
    return None if obj is None else _jsonable(asdict(obj))


def _parsed_from_json(data: dict[str, Any]) -> ParsedName:
    return ParsedName(
        kind=MediaKind(data["kind"]) if data.get("kind") else None,
        title=data.get("title"),
        year=data.get("year"),
        season=data.get("season"),
        episodes=tuple(data.get("episodes") or ()),
        languages=tuple(data.get("languages") or ()),
        is_3d=bool(data.get("is_3d")),
        format_3d=data.get("format_3d"),
        is_sample=bool(data.get("is_sample")),
    )


def mediainfo_from_json(data: dict[str, Any] | None) -> MediaInfo | None:
    """``MediaInfo`` aus ``Item.mediainfo_json``."""
    if data is None:
        return None
    return MediaInfo(
        width=data["width"],
        height=data["height"],
        video_codec=data["video_codec"],
        hdr=data["hdr"],
        audio=tuple(AudioStream(**a) for a in data["audio"]),
    )


def _subtitles_json(subs: Sequence[Subtitle], target: Path | None) -> list[dict[str, Any]]:
    targets = dict(subtitle_targets(target, subs)) if target is not None else {}
    return [
        {"path": str(s.path), "language": s.language, "forced": s.forced,
         "target": str(targets[s]) if s in targets else None}
        for s in subs
    ]


def _subtitles_from_json(data: list[dict[str, Any]] | None) -> list[Subtitle]:
    return [Subtitle(Path(d["path"]), d["language"], d["forced"]) for d in data or []]


# --- Ein Lauf -----------------------------------------------------------------------


def _cap_confidence(item: Item, cap: Confidence) -> None:
    if _CONFIDENCE_ORDER.index(Confidence(item.confidence)) > _CONFIDENCE_ORDER.index(cap):
        item.confidence = cap.value


def _identities(
    kind: MediaKind, tmdb_id: int, season: int | None, episodes: Sequence[int], library: str
) -> list[tuple]:
    """Schlüssel „derselbe Film“ (je Bibliothek: 2D und 3D sind keine Dubletten) bzw. „dieselbe Folge“
    (je Folgennummer, damit Doppelfolgen überlappen)."""
    if kind == MediaKind.MOVIE:
        return [("movie", library, tmdb_id)]
    return [("episode", tmdb_id, season, e) for e in episodes]


def _exists(path: Path) -> bool:
    return path.exists()


def _same_entry(a: Path, b: Path) -> bool:
    """Derselbe Eintrag – auch auf SMB, wo sich Namen nur in der Groß-/Kleinschreibung unterscheiden."""
    if casefold(str(a)) == casefold(str(b)):
        return True
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _library_path(target: str, folders: dict[int, Folder]) -> str:
    """Bibliothek, in der ``target`` liegt (die tiefste passende), sonst der Zielordner selbst."""
    roots = [Path(f.path) for f in folders.values() if f.role == "library"]
    matches = [r for r in roots if Path(target).is_relative_to(r)]
    return str(max(matches, key=lambda r: len(r.parts))) if matches else str(Path(target).parent)


@dataclass(frozen=True)
class _Version:
    """Quelle eines anderen Vorschlags, die als vorhandene Version zählt."""

    source: str
    info: MediaInfo | None
    size: int
    mtime: float
    episodes: tuple[int, ...]
    origin: str


@dataclass(frozen=True)
class _SeasonCheck:
    numbers: frozenset[int] | None  # None: Staffel konnte nicht geprüft werden
    error: str | None = None
    cap: Confidence | None = None

    def episode_exists(self, episodes: Sequence[int]) -> bool | None:
        return None if self.numbers is None else all(e in self.numbers for e in episodes)

    def apply(self, item: Item) -> None:
        if self.error is not None:
            item.error = self.error
            _cap_confidence(item, self.cap)


class _Run:
    """Zustand eines Planungslaufs: Bibliotheksindizes, Folgennamen und schon vergebene Ziele."""

    def __init__(self, ctx: PlanContext, folder: Folder, folders: dict[int, Folder]):
        self.ctx = ctx
        self.folder = folder
        self.folders = folders
        self._indexes: dict[tuple[int, str], LibraryIndex] = {}
        self._seasons: dict[tuple[int, int], _SeasonCheck] = {}
        self._episode_names: dict[tuple[int, int], dict[int, str]] = {}
        self._shows: dict[Path, list[ShowVideo]] = {}  # Serienordner -> geparste Folgen-Dateien
        # Zielpfad oder Identität (siehe ``_identities``) -> Version des Items, das sie zuerst belegt hat
        self.claimed: dict[Hashable, _Version] = {}

    def index(self, library: Folder, kind: MediaKind) -> LibraryIndex:
        index_kind = "series" if kind == MediaKind.EPISODE else "movie"
        index = self._indexes.get((library.id, index_kind))
        if index is None:
            index = LibraryIndex(self.ctx.session_factory, library.id, Path(library.path), index_kind)
            index.refresh()
            self._indexes[(library.id, index_kind)] = index
        return index

    def season_check(self, tv_id: int, season: int) -> _SeasonCheck:
        key = (tv_id, season)
        if key not in self._seasons:
            try:
                check = _SeasonCheck(frozenset(self.ctx.tmdb.episode_numbers(tv_id, season)))
            except TmdbError as exc:
                if exc.status_code == 404:
                    check = _SeasonCheck(None, f"Staffel {season} bei TMDB nicht gefunden", Confidence.LOW)
                else:
                    check = _SeasonCheck(
                        None, f"Staffel {season} konnte bei TMDB nicht geprüft werden: {exc}", Confidence.MEDIUM
                    )
            self._seasons[key] = check
        return self._seasons[key]

    def episode_names(self, tv_id: int, season: int) -> dict[int, str]:
        key = (tv_id, season)
        if key not in self._episode_names:
            names: dict[int, str] = {}
            if self.season_check(tv_id, season).numbers is not None:
                try:
                    names = self.ctx.tmdb.episode_titles(tv_id, season)
                except TmdbError:
                    pass  # Folgennamen sind optional (Spec 5.6)
            self._episode_names[key] = names
        return self._episode_names[key]

    def show_videos(self, show_dir: Path) -> list[ShowVideo]:
        """Folgen-Dateien eines Serienordners, je Lauf nur einmal gelesen und geparst."""
        if show_dir not in self._shows:
            settings = self.ctx.settings
            self._shows[show_dir] = show_videos(show_dir, exclude=settings.exclude,
                                                sample_bytes=settings.sample_bytes)
        return self._shows[show_dir]

    def claim(
        self,
        target: str,
        kind: MediaKind,
        tmdb_id: int,
        season: int | None,
        episodes: Sequence[int],
        library: str,
        version: _Version,
    ) -> None:
        for key in (target, *_identities(kind, tmdb_id, season, episodes, library)):
            self.claimed.setdefault(key, version)

    def place(
        self,
        item: Item,
        library: Folder,
        cand: Candidate,
        *,
        kind: MediaKind,
        is_3d: bool,
        format_3d: str | None,
        season: int | None,
        episodes: Sequence[int],
        languages: Sequence[str],
        info: MediaInfo | None,
        subs: Sequence[Subtitle],
    ) -> bool:
        """Setzt Ziel, Folgennamen, Untertitel und Konflikt; ``False``, wenn die Datei schon richtig liegt."""
        source = Path(item.source)
        ext = source.suffix
        if kind == MediaKind.MOVIE:
            rel = movie_relpath(cand.title, cand.year, ext, info, languages,
                                format_3d if is_3d else None, is_3d)
            item.episode_titles_json = None
        else:
            if season is None or not episodes:
                item.note = NO_EPISODE_NOTE
                item.confidence = Confidence.LOW.value
                return True
            names = self.episode_names(cand.tmdb_id, season)
            titles = [names[e] for e in episodes if e in names]
            rel = episode_relpath(cand.title, cand.year, season, episodes, titles, ext, info,
                                  languages, self.ctx.settings.season_label)
            item.episode_titles_json = titles
        target = Path(library.path) / rel
        if target == source:
            return False
        item.target = str(target)
        item.subtitles_json = _subtitles_json(subs, target)
        item.conflict_json = self._conflict(library, cand, kind, season, episodes, source, target,
                                            info, item.size)
        self.claim(str(target), kind, cand.tmdb_id, season, episodes, library.path,
                   _Version(str(source), info, item.size, item.mtime, tuple(episodes), ORIGIN_SCAN))
        return True

    def _conflict(
        self,
        library: Folder,
        cand: Candidate,
        kind: MediaKind,
        season: int | None,
        episodes: Sequence[int],
        source: Path,
        target: Path,
        info: MediaInfo | None,
        size: int,
    ) -> dict[str, Any] | None:
        """Vorhandene Versionen desselben Titels (Spec 7.2, Abschlussprüfung C2).

        ``existing``: Pfade; ``existing_meta``: je Pfad Größe, Änderungszeit, Kurzbeschreibung und
        Herkunft (zum Prüfen vor dem Löschen); ``partial``: eine vorhandene Datei enthält Folgen, die
        die neue nicht hat (dann darf die neue sie nicht ersetzen); ``suggestion``: bessere Version.
        """
        index = self.index(library, kind)
        # Pfad -> bekannte Version (Quelle eines anderen Vorschlags) oder None (liegt in der Bibliothek)
        found: dict[str, _Version | None] = {}
        coverage: dict[str, tuple[int, ...]] = {}  # Folgennummern vorhandener Folgen-Dateien
        title_only = False
        for match in index.find_matches(cand):
            if not match.title_only:  # eine nur über den Titel geratene ID wird nicht gemerkt
                index.remember(match.path, cand.tmdb_id)
            if kind == MediaKind.MOVIE:
                files = movie_versions(match.path, self.ctx.settings.sample_bytes)
            else:
                files = []
                for video in self.show_videos(match.path):
                    if video.season == season and set(episodes).intersection(video.episodes):
                        files.append(video.path)
                        coverage[str(video.path)] = video.episodes
            for path in files:
                found.setdefault(str(path), None)
            title_only = title_only or (match.title_only and bool(files))
        if _exists(target):
            found.setdefault(str(target), None)
        for key in (str(target), *_identities(kind, cand.tmdb_id, season, episodes, library.path)):
            claim = self.claimed.get(key)
            if claim is not None:
                found[claim.source] = claim
        for path in [p for p in found if _same_entry(Path(p), source)]:
            del found[path]  # die Quelle selbst (auch nur anders geschrieben) ist keine Dublette
        if not found:
            return None

        meta: list[dict[str, Any]] = []
        versions: list[tuple[MediaInfo | None, int | None]] = []
        partial = False
        for path, known in found.items():
            if known is not None:
                v_info, v_size, v_mtime, origin = known.info, known.size, known.mtime, known.origin
                covered = known.episodes
            else:
                v_info, v_size, v_mtime, origin = self._inspect(Path(path))
                covered = coverage[path] if path in coverage else self._parsed_episodes(Path(path))
            if kind == MediaKind.EPISODE and set(covered) - set(episodes):
                partial = True
            meta.append({"path": path, "size": v_size, "mtime": v_mtime,
                         "labels": short_label(v_info) if v_info is not None else None, "origin": origin})
            versions.append((v_info, v_size))
        suggestion = None if partial or title_only else self._suggestion(versions, info, size)
        return {"existing": list(found), "suggestion": suggestion, "partial": partial, "existing_meta": meta}

    def _inspect(self, path: Path) -> tuple[MediaInfo | None, int | None, float | None, str]:
        """(MediaInfo, Größe, Änderungszeit, Herkunft) einer Datei in der Bibliothek; ``None``, wo unbekannt."""
        try:
            st = path.stat()
            v_size, v_mtime = st.st_size, st.st_mtime
        except OSError:
            v_size = v_mtime = None
        try:
            v_info = self.ctx.probe(path)
        except (ProbeError, OSError):
            v_info = None
        return v_info, v_size, v_mtime, ORIGIN_LIBRARY

    @staticmethod
    def _parsed_episodes(path: Path) -> tuple[int, ...]:
        return tuple(parse_name(path.name, path.parent.name).episodes)

    @staticmethod
    def _suggestion(
        versions: Sequence[tuple[MediaInfo | None, int | None]], info: MediaInfo | None, size: int
    ) -> str | None:
        if any(v_info is None or v_size is None for v_info, v_size in versions):
            return None
        best = max(versions, key=lambda v: quality_key(*v))
        return _SUGGESTIONS.get(better((info, size), best))


# --- Planen ---------------------------------------------------------------------------


@dataclass
class _Entry:
    video: VideoFile
    parsed: ParsedName
    kind: MediaKind
    is_3d: bool
    note: str | None
    group_key: str
    candidates: list[Candidate] = field(default_factory=list)
    tmdb_error: str | None = None


def _entry(video: VideoFile, folder: Folder) -> _Entry:
    parent = video.path.parent.name if video.path.parent != video.root else None
    parsed = parse_name(video.path.name, parent)
    folder_type = FolderType(folder.media_type)
    note = None
    if folder_type == FolderType.SERIES:
        kind = MediaKind.EPISODE
    elif folder_type in (FolderType.MOVIE, FolderType.MOVIE3D):
        kind = MediaKind.MOVIE
        if parsed.season is not None or parsed.episodes:
            note = SERIES_NOTE
    else:
        kind = parsed.kind or MediaKind.MOVIE
    is_3d = parsed.is_3d or folder_type == FolderType.MOVIE3D
    group_key = f"{kind}:{normalize_title(parsed.title or '')}:{parsed.year or ''}"
    return _Entry(video, parsed, kind, is_3d, note, group_key)


def _search_groups(ctx: PlanContext, entries: Sequence[_Entry]) -> None:
    groups: dict[str, list[_Entry]] = {}
    for entry in entries:
        groups.setdefault(entry.group_key, []).append(entry)
    for members in groups.values():
        first = members[0]
        candidates: list[Candidate] = []
        error = None
        if first.parsed.title:
            try:
                candidates = ctx.tmdb.search(first.kind, first.parsed.title, first.parsed.year)
            except TmdbError as exc:
                error = str(exc)
            except Exception as exc:  # unerwartet: betrifft nur diese Gruppe
                error = f"TMDB-Suche fehlgeschlagen ({type(exc).__name__}): {exc}"
        for entry in members:
            entry.candidates, entry.tmdb_error = candidates, error


def _plan_entry(run: _Run, entry: _Entry, scan_id: int, auto: bool) -> Item | None:
    video, parsed, kind = entry.video, entry.parsed, entry.kind
    item = Item(
        scan_id=scan_id, folder_id=run.folder.id, source=str(video.path), size=video.size,
        mtime=video.mtime, kind=kind.value, parsed_json=_as_json(parsed), group_key=entry.group_key,
        status=ItemStatus.NEW.value, note=entry.note, auto=auto,
        subtitles_json=_subtitles_json(video.subtitles, None),
    )
    if kind == MediaKind.EPISODE:
        item.season, item.episodes_json = parsed.season, list(parsed.episodes)

    result = match(parsed, entry.candidates)
    cand = result.candidate
    season_check = None
    if cand is not None and kind == MediaKind.EPISODE and parsed.season is not None and parsed.episodes:
        season_check = run.season_check(cand.tmdb_id, parsed.season)
        result = match(parsed, entry.candidates, season_check.episode_exists(parsed.episodes))
    confidence = result.confidence
    if entry.note or entry.tmdb_error:
        confidence = Confidence.LOW
    item.confidence = confidence.value
    item.alternatives_json = [_as_json(c) for c in result.alternatives]
    if cand is not None:
        item.tmdb_id, item.title, item.year = cand.tmdb_id, cand.title, cand.year
    item.error = entry.tmdb_error
    if season_check is not None:
        season_check.apply(item)

    try:
        info = run.ctx.probe(video.path)
    except ProbeError as exc:
        item.status, item.error = ItemStatus.ERROR.value, str(exc)
        return item
    item.mediainfo_json = _as_json(info)
    try:
        library = _target_folder(run.folder, kind, entry.is_3d, run.folders)
    except NoTarget as exc:
        item.status, item.error = ItemStatus.ERROR.value, str(exc)
        return item
    if cand is None:
        return item
    placed = run.place(
        item, library, cand, kind=kind, is_3d=entry.is_3d, format_3d=parsed.format_3d,
        season=parsed.season, episodes=parsed.episodes, languages=parsed.languages,
        info=info, subs=video.subtitles,
    )
    return item if placed else None


def _failed_item(video: VideoFile, folder: Folder, scan_id: int, auto: bool, exc: Exception,
                 entry: _Entry | None = None) -> Item:
    """Vorschlag für eine Datei, deren Planung unerwartet gescheitert ist (der Scan läuft weiter)."""
    return Item(
        scan_id=scan_id, folder_id=folder.id, source=str(video.path), size=video.size, mtime=video.mtime,
        kind=(entry.kind if entry is not None else MediaKind.MOVIE).value,
        parsed_json=_as_json(entry.parsed) if entry is not None else None,
        group_key=entry.group_key if entry is not None else None,
        status=ItemStatus.ERROR.value, confidence=Confidence.LOW.value, auto=auto,
        subtitles_json=_subtitles_json(video.subtitles, None),
        error=f"Unerwarteter Fehler bei der Planung ({type(exc).__name__}): {exc}",
    )


def _already_planned(
    video: VideoFile, planned: set[str], latest: dict[str, tuple[str, int, float]]
) -> bool:
    """Offener Vorschlag vorhanden, oder der letzte war ein Fehler und die Datei ist unverändert."""
    source = str(video.path)
    if source in planned:
        return True
    last = latest.get(source)
    return last == (ItemStatus.ERROR.value, video.size, video.mtime)


def plan_folder(
    ctx: PlanContext,
    folder: Folder,
    *,
    subset: Callable[[str], bool] | None = None,
    subset_label: str | None = None,
    auto: bool = False,
) -> int:
    """Scannt ``folder``, legt einen ``Scan`` mit Vorschlägen an und gibt dessen ID zurück.

    ``subset_label`` ist die lesbare Bezeichnung des Ausschnitts (``Scan.subset``).
    """
    with ctx.session_factory() as s:
        folders = {f.id: f for f in s.scalars(select(Folder))}
        ignored = set(s.scalars(select(Ignored.path)))
        planned = set(s.scalars(select(Item.source).where(Item.status.in_(_OPEN_STATUSES))))
        # jüngstes Item je Quelle: (Status, Größe, Änderungszeit)
        latest = {row.source: (row.status, row.size, row.mtime) for row in s.execute(
            select(Item.source, Item.status, Item.size, Item.mtime).order_by(Item.id))}
        # offene Vorschläge früherer Scans belegen ihre Ziele und Titel weiterhin (Abschlussprüfung I2)
        open_items = list(s.scalars(
            select(Item).where(Item.status.in_(_OPEN_STATUSES), Item.target.is_not(None),
                               Item.tmdb_id.is_not(None)).order_by(Item.id)))
    _check_roots(folder, folders)

    with ctx.session_factory() as s:
        scan_row = Scan(folder_id=folder.id, started=ctx.now(), status="running", auto=auto,
                        subset=subset_label)
        s.add(scan_row)
        s.commit()
        scan_id = scan_row.id

    try:
        settings = ctx.settings
        result = scan(
            Path(folder.path),
            exclude=settings.exclude,
            sample_bytes=settings.sample_bytes,
            min_age_s=settings.min_age_minutes * 60,
            now=ctx.now(),
            ignored=ignored,
            top_filter=subset,
        )
        entries: list[_Entry] = []
        items: list[Item] = []
        for video in result.videos:
            if _already_planned(video, planned, latest):
                continue
            try:
                entries.append(_entry(video, folder))
            except Exception as exc:  # betrifft nur diese Datei
                items.append(_failed_item(video, folder, scan_id, auto, exc))
        _search_groups(ctx, entries)
        run = _Run(ctx, folder, folders)
        for other in open_items:
            _claim_item(run, other, folders, ORIGIN_OPEN)
        for entry in entries:
            try:
                item = _plan_entry(run, entry, scan_id, auto)
            except RootMissing:
                raise  # Bibliothek nicht lesbar: der ganze Scan scheitert
            except Exception as exc:  # betrifft nur diesen Vorschlag
                item = _failed_item(entry.video, folder, scan_id, auto, exc, entry)
            if item is not None:
                items.append(item)
    except Exception:
        with ctx.session_factory() as s:
            row = s.get(Scan, scan_id)
            row.status, row.finished = "error", ctx.now()
            s.commit()
        raise

    with ctx.session_factory() as s:
        s.add_all(items)
        row = s.get(Scan, scan_id)
        row.status, row.finished = "done", ctx.now()
        row.errors_json = list(result.errors) + [SMALL_VIDEO_HINT.format(p) for p in result.small]
        s.commit()
    return scan_id


# --- Korrektur ------------------------------------------------------------------------


def _reference_candidate(tmdb: TmdbClient, ref: tuple[str, str]) -> Candidate:
    source, ident = ref
    if source == "imdb":
        cand = tmdb.find_imdb(ident)
        if cand is None:
            raise TmdbError(f"TMDB: kein Eintrag zur IMDb-ID {ident} gefunden")
        return cand
    kind = MediaKind.MOVIE if source == "movie" else MediaKind.EPISODE
    return tmdb.details(kind, int(ident))


def _replan(
    run: _Run,
    item: Item,
    cand: Candidate | None,
    *,
    season: int | None = None,
    episodes: Sequence[int] | None = None,
    format_3d: str | None = None,
    corrected: bool = False,
) -> None:
    parsed = _parsed_from_json(item.parsed_json or {})
    if format_3d is not None:
        item.parsed_json = {**(item.parsed_json or {}), "is_3d": True, "format_3d": format_3d}
        parsed = _parsed_from_json(item.parsed_json)
    kind = cand.kind if cand is not None else MediaKind(item.kind)
    is_3d = parsed.is_3d or run.folder.media_type == FolderType.MOVIE3D
    item.kind = kind.value
    if kind == MediaKind.EPISODE:
        if season is None:
            season = item.season if item.season is not None else parsed.season
        if episodes is None:
            episodes = item.episodes_json or list(parsed.episodes)
        item.season, item.episodes_json = season, list(episodes)
    else:
        item.season = item.episodes_json = item.episode_titles_json = None
        season, episodes = None, ()

    item.target = item.conflict_json = item.decision = item.error = None
    item.status = ItemStatus.NEW.value
    if item.note in (NO_EPISODE_NOTE, ALREADY_CORRECT_NOTE):
        item.note = None
    subs = _subtitles_from_json(item.subtitles_json)
    item.subtitles_json = _subtitles_json(subs, None)
    if cand is not None:
        item.tmdb_id, item.title, item.year = cand.tmdb_id, cand.title, cand.year
    if corrected:  # Nutzer hat entschieden
        item.note, item.confidence = None, Confidence.HIGH.value
    if cand is not None and kind == MediaKind.EPISODE and season is not None and episodes:
        run.season_check(cand.tmdb_id, season).apply(item)

    if item.mediainfo_json is not None:
        info = mediainfo_from_json(item.mediainfo_json)
    else:
        try:
            info = run.ctx.probe(Path(item.source))
        except ProbeError as exc:
            item.status, item.error = ItemStatus.ERROR.value, str(exc)
            return
        item.mediainfo_json = _as_json(info)
    try:
        library = _target_folder(run.folder, kind, is_3d, run.folders)
    except NoTarget as exc:
        item.status, item.error = ItemStatus.ERROR.value, str(exc)
        return
    if cand is None:
        return
    placed = run.place(
        item, library, cand, kind=kind, is_3d=is_3d, format_3d=parsed.format_3d,
        season=season, episodes=tuple(episodes), languages=parsed.languages, info=info, subs=subs,
    )
    if not placed:
        item.status, item.note = ItemStatus.DONE.value, ALREADY_CORRECT_NOTE


def _claim_item(run: _Run, other: Item, folders: dict[int, Folder], origin: str) -> None:
    """Ziel und Identität eines anderen offenen Vorschlags als belegt vormerken."""
    episodes = tuple(other.episodes_json or ())
    run.claim(
        other.target, MediaKind(other.kind), other.tmdb_id, other.season, episodes,
        _library_path(other.target, folders),
        _Version(other.source, mediainfo_from_json(other.mediainfo_json), other.size, other.mtime, episodes, origin),
    )


def replan_item(
    ctx: PlanContext,
    item_id: int,
    *,
    tmdb_ref: tuple[str, str] | None = None,
    season: int | None = None,
    episodes: Sequence[int] | None = None,
    format_3d: str | None = None,
) -> None:
    """Korrigiert einen Vorschlag; bei Serien gilt eine neue TMDB-Zuordnung für die ganze Gruppe."""
    with ctx.session_factory() as s:
        item = s.get(Item, item_id)
        if item is None:
            raise ItemNotFound(f"Vorschlag {item_id} nicht gefunden")
        if item.status not in _REPLANNABLE:
            raise NotReplannable(f"Vorschlag {item_id} kann im Status „{item.status}“ nicht korrigiert werden")
        folders = {f.id: f for f in s.scalars(select(Folder))}
        others = list(s.scalars(select(Item).where(Item.scan_id == item.scan_id, Item.id != item.id)))
        elsewhere = list(s.scalars(select(Item).where(
            Item.scan_id != item.scan_id, Item.status.in_(_OPEN_STATUSES), Item.target.is_not(None),
            Item.tmdb_id.is_not(None)).order_by(Item.id)))

    if tmdb_ref is not None:
        cand = _reference_candidate(ctx.tmdb, tmdb_ref)
    elif item.tmdb_id is not None:
        cand = ctx.tmdb.details(MediaKind(item.kind), item.tmdb_id)
    else:
        cand = None

    group: list[Item] = []
    if tmdb_ref is not None and cand.kind == MediaKind.EPISODE:
        group = [o for o in others if o.group_key == item.group_key and o.status == ItemStatus.NEW.value]
    run = _Run(ctx, folders[item.folder_id], folders)
    for other in others:
        if other not in group and other.status in _OPEN_STATUSES and other.target and other.tmdb_id:
            _claim_item(run, other, folders, ORIGIN_SCAN)
    for other in elsewhere:
        _claim_item(run, other, folders, ORIGIN_OPEN)

    corrected = tmdb_ref is not None
    _replan(run, item, cand, season=season, episodes=episodes, format_3d=format_3d, corrected=corrected)
    for member in group:
        _replan(run, member, cand, corrected=corrected)

    with ctx.session_factory() as s:
        for it in (item, *group):
            s.merge(it)
        s.commit()
