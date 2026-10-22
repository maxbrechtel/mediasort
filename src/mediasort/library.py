"""Bibliotheksindex: vorhandene Filme/Serien in den Plex-Bibliotheken finden (Spec 7.2)."""

import os
import re
import unicodedata
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from mediasort.db import LibraryEntry
from mediasort.matcher import HIGH_SIMILARITY, YEAR_TOLERANCE, title_similarity
from mediasort.models import Candidate
from mediasort.parser import parse_name
from mediasort.scanner import VIDEO_LIKE_EXT, is_named_sample

_FOLDER_RE = re.compile(r"^(?P<title>.*?)\s*\((?P<year>\d{4})\)\s*$")
_NFO_UNIQUEID_RE = re.compile(
    r"""<uniqueid\b[^>]*\btype\s*=\s*["']tmdb["'][^>]*>\s*(\d+)\s*</uniqueid>""", re.IGNORECASE
)
_NFO_TMDBID_RE = re.compile(r"<tmdbid>\s*(\d+)\s*</tmdbid>", re.IGNORECASE)
# Plex-Extras: Dateinamen-Endungen und Ordnernamen (``Specials`` ist kein Extras-Ordner)
PLEX_EXTRA_SUFFIXES = ("-trailer", "-featurette", "-behindthescenes", "-deleted", "-deletedscene",
                       "-interview", "-scene", "-short", "-other", "-sample")
EXTRAS_DIRS = frozenset({"extras", "featurettes", "trailers", "behind the scenes", "deleted scenes",
                         "interviews", "scenes", "shorts", "other"})


class LibraryUnreadable(Exception):
    """Der Bibliotheksordner kann nicht gelesen werden; der Index bleibt unverändert (Meldung auf Deutsch)."""


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def read_nfo_tmdb_id(folder: Path) -> int | None:
    """TMDB-ID aus einer ``*.nfo`` direkt im Ordner (``<uniqueid type="tmdb">`` oder ``<tmdbid>``)."""
    try:
        nfos = sorted(p for p in folder.iterdir() if p.suffix.lower() == ".nfo" and p.is_file())
    except OSError:
        return None
    for nfo in nfos:
        try:
            text = nfo.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for pattern in (_NFO_UNIQUEID_RE, _NFO_TMDBID_RE):
            found = pattern.search(text)
            if found:
                return int(found.group(1))
    return None


def parse_folder_name(name: str) -> tuple[str, int | None]:
    """``"Titel (2009)"`` -> ``("Titel", 2009)``; ohne Jahr bleibt der Name unverändert."""
    found = _FOLDER_RE.match(name)
    if found and found.group("title"):
        return found.group("title"), int(found.group("year"))
    return name.strip(), None


def is_plex_extra(name: str) -> bool:
    """Plex-Extra nach Namen: ``Film-trailer.mkv``, ``Film-featurette.mp4`` usw."""
    stem = name.rpartition(".")[0] if "." in name else name
    return stem.casefold().endswith(PLEX_EXTRA_SUFFIXES)


def _is_version(path: Path, sample_bytes: int, dirs: Sequence[str] = ()) -> bool:
    """Echte Videodatei, die als vorhandene Version eines Titels zählt (Abschlussprüfung C2):
    videoartig, nicht versteckt, kein Sample nach Namen, kein Plex-Extra, ab der Sample-Grenze."""
    name = path.name
    ext = name.rpartition(".")[2].lower() if "." in name else ""
    if name.startswith(".") or ext not in VIDEO_LIKE_EXT:
        return False
    if is_named_sample(name, dirs) or is_plex_extra(name):
        return False
    try:
        st = path.stat()
    except OSError:
        return False
    return path.is_file() and st.st_size >= sample_bytes


def movie_versions(folder: Path, sample_bytes: int) -> list[Path]:
    """Vorhandene Versionen eines Films: nur Videos direkt im Filmordner (nie in Unterordnern
    wie ``Featurettes`` oder ``.deletedByTMM``), sortiert; fehlender Ordner ergibt eine leere Liste."""
    try:
        children = list(folder.iterdir())
    except OSError:
        return []
    return sorted(p for p in children if _is_version(p, sample_bytes))


@dataclass(frozen=True)
class ShowVideo:
    """Videodatei eines Serienordners mit geparster Staffel und Folgen."""

    path: Path
    season: int | None
    episodes: tuple[int, ...]


def show_videos(show_dir: Path, *, exclude: Collection[str] = (), sample_bytes: int = 0) -> list[ShowVideo]:
    """Alle Folgen-Dateien eines Serienordners (rekursiv), je einmal geparst.

    Versteckte Ordner, Ausschlussordner und Plex-Extras-Ordner werden übersprungen.
    """
    found: list[ShowVideo] = []
    for dirpath, dirnames, filenames in os.walk(show_dir):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in exclude
                             and d.casefold() not in EXTRAS_DIRS)
        rel_dirs = Path(dirpath).relative_to(show_dir).parts
        for name in sorted(filenames):
            path = Path(dirpath, name)
            if _is_version(path, sample_bytes, rel_dirs):
                parsed = parse_name(name, path.parent.name)
                found.append(ShowVideo(path, parsed.season, tuple(parsed.episodes)))
    return sorted(found, key=lambda v: v.path)


def episode_files(
    show_dir: Path,
    season: int,
    episodes: Sequence[int],
    *,
    exclude: Collection[str] = (),
    sample_bytes: int = 0,
    videos: Sequence[ShowVideo] | None = None,
) -> list[Path]:
    """Videos im Serienordner, deren geparste Staffel/Folgen sich mit den gesuchten überschneiden.

    ``videos`` ist das (zwischengespeicherte) Ergebnis von ``show_videos`` für ``show_dir``.
    """
    if videos is None:
        videos = show_videos(show_dir, exclude=exclude, sample_bytes=sample_bytes)
    wanted = set(episodes)
    return [v.path for v in videos if v.season == season and wanted.intersection(v.episodes)]


@dataclass(frozen=True)
class LibraryMatch:
    path: Path
    title_only: bool  # nur über den Titel gefunden: Ordner ohne TMDB-ID und ohne Jahr


class LibraryIndex:
    """Index der obersten Ordner einer Bibliothek, abgelegt in ``LibraryEntry``."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        library_id: int,
        root: Path,
        kind: str = "movie",
    ) -> None:
        self._session_factory = session_factory
        self._library_id = library_id
        self._root = Path(root).absolute()
        self._kind = kind

    def _entry_for(self, folder: Path, known_id: int | None) -> LibraryEntry:
        title, year = parse_folder_name(_nfc(folder.name))
        return LibraryEntry(
            library_id=self._library_id,
            path=str(folder),
            title=title,
            year=year,
            tmdb_id=read_nfo_tmdb_id(folder) or known_id,
            kind=self._kind,
        )

    def refresh(self) -> int:
        """Ersetzt die Einträge der Bibliothek; bekannte ``tmdb_id`` bleibt bei unverändertem Pfad.

        Ist der Ordner nicht lesbar, bleibt der Index wie er ist: ``LibraryUnreadable``.
        """
        try:
            folders = sorted(p for p in self._root.iterdir() if p.is_dir() and not p.name.startswith("."))
        except OSError as exc:
            raise LibraryUnreadable(f"Bibliothek nicht lesbar: {self._root} ({exc.strerror or exc})") from exc
        with self._session_factory() as session:
            old = session.scalars(select(LibraryEntry).where(LibraryEntry.library_id == self._library_id)).all()
            known = {e.path: e.tmdb_id for e in old}
            for entry in old:
                session.delete(entry)
            session.flush()
            session.add_all(self._entry_for(f, known.get(str(f))) for f in folders)
            session.commit()
        return len(folders)

    def find(self, cand: Candidate) -> list[Path]:
        """Ordner mit gleicher TMDB-ID, danach (ohne ID) titelähnliche Ordner mit passendem Jahr."""
        return [m.path for m in self.find_matches(cand)]

    def find_matches(self, cand: Candidate) -> list[LibraryMatch]:
        """Wie ``find``; vermerkt zusätzlich, ob ein Ordner nur über den Titel (ohne ID und Jahr) passt."""
        with self._session_factory() as session:
            entries = session.scalars(select(LibraryEntry).where(LibraryEntry.library_id == self._library_id)).all()
            rows = [(e.path, e.title, e.year, e.tmdb_id) for e in entries]
        by_id: list[str] = []
        by_title: list[tuple[str, bool]] = []
        for path, title, year, tmdb_id in rows:
            if tmdb_id is not None:
                if tmdb_id == cand.tmdb_id:
                    by_id.append(path)
                continue
            if year is not None and cand.year is not None and abs(year - cand.year) > YEAR_TOLERANCE:
                continue
            if title_similarity(title, cand) >= HIGH_SIMILARITY:
                by_title.append((path, year is None))
        return ([LibraryMatch(Path(p), False) for p in sorted(by_id)]
                + [LibraryMatch(Path(p), title_only) for p, title_only in sorted(by_title)])

    def remember(self, folder: Path, tmdb_id: int) -> None:
        """Merkt die TMDB-ID eines Ordners (legt den Eintrag an, falls er fehlt)."""
        folder = Path(folder).absolute()
        with self._session_factory() as session:
            entry = session.scalars(
                select(LibraryEntry).where(
                    LibraryEntry.library_id == self._library_id, LibraryEntry.path == str(folder)
                )
            ).first()
            if entry is None:
                entry = self._entry_for(folder, None)
                session.add(entry)
            entry.tmdb_id = tmdb_id
            session.commit()
