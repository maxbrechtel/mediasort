"""Bestätigte Vorschläge ausführen: verschieben, Konflikte umsetzen, aufräumen, protokollieren (Spec 7.3, 7.4).

Grundsätze: Es wird nie überschrieben, nur ausdrücklich entschiedene Dubletten werden gelöscht,
und Bibliothekswurzeln werden nie angelegt – nur Ordner darunter.
"""

import errno
import logging
import os
import shutil
import time
from collections.abc import Callable, Collection, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from mediasort.db import Folder, History, Item, casefold
from mediasort.models import ItemStatus
from mediasort.scanner import (
    PARTIAL_EXT,
    SUB_EXT,
    VIDEO_EXT,
    VIDEO_LIKE_EXT,
    is_named_sample,
    owns_subtitle,
)
from mediasort.settings import Settings

log = logging.getLogger(__name__)

TMP_SUFFIX = ".mediasort-tmp"
OLD_SUFFIX = ".mediasort-old"
ROOT_MISSING = "Bibliotheksordner fehlt – ist die Freigabe eingebunden?"
UNDECIDED = "Konflikt nicht entschieden"
TARGET_TAKEN = "Ziel belegt"
SOURCE_CHANGED = "Quelle wurde seit dem Scan verändert"
EXISTING_CHANGED = "Vorhandene Datei hat sich seit dem Scan verändert"
PARTIAL_REFUSED = "Vorhandene Datei enthält weitere Folgen"
_DECISIONS = (None, "keep_new", "keep_existing", "keep_both")
# Beiwerk, das beim Aufräumen unabhängig von der Größe mitgelöscht werden darf
LEFTOVER_EXT = frozenset({
    "nfo", "txt", "url", "jpg", "jpeg", "png", "gif", "bmp", "webp", "sfv", "md5", "srr", "nzb", "log", "xml",
})


def _ext(name: str) -> str:
    return name.rpartition(".")[2].lower() if "." in name else ""


def _exists(path: Path) -> bool:
    """Auch defekte symbolische Links zählen als vorhanden (sie würden überschrieben)."""
    return os.path.lexists(path)


# --- Verschieben -----------------------------------------------------------------------


class SourceNotRemoved(OSError):
    """Die Kopie liegt vollständig am Ziel, aber die Quelle konnte nicht gelöscht werden."""


def move_file(src: Path, dst: Path) -> None:
    """Verschiebt ``src`` nach ``dst``; ``FileExistsError``, wenn ``dst`` existiert.

    Über Dateisystemgrenzen (``EXDEV``) wird in ``<dst>.mediasort-tmp`` kopiert, die Größe
    verglichen, umbenannt und erst dann die Quelle gelöscht. Bei einem Fehler wird die
    Teilkopie entfernt; die Quelle bleibt. Scheitert erst das Löschen der Quelle, liegt die
    Datei an beiden Orten: ``SourceNotRemoved``.
    """
    src, dst = Path(src), Path(dst)
    if _exists(dst):
        raise FileExistsError(errno.EEXIST, "Ziel existiert bereits", str(dst))
    try:
        os.rename(src, dst)
        return
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
    tmp = dst.with_suffix(dst.suffix + TMP_SUFFIX)
    if _exists(tmp):
        raise FileExistsError(errno.EEXIST, "Zwischendatei existiert bereits", str(tmp))
    try:
        shutil.copyfile(src, tmp)
        if tmp.stat().st_size != src.stat().st_size:
            raise OSError(errno.EIO, "Kopie unvollständig (Größe weicht ab)", str(dst))
        if _exists(dst):
            raise FileExistsError(errno.EEXIST, "Ziel existiert bereits", str(dst))
        os.rename(tmp, dst)
    except BaseException:
        with suppress(OSError):
            tmp.unlink()  # Teilkopie entfernen; der ursprüngliche Fehler zählt
        raise
    try:
        os.unlink(src)
    except OSError as exc:
        raise SourceNotRemoved(
            exc.errno, f"Quelle nach dem Kopieren nicht gelöscht ({exc.strerror})", str(src)
        ) from exc


# --- Aufräumen -------------------------------------------------------------------------


def _norm(path: Path) -> Path:
    return Path(os.path.normpath(os.path.abspath(path)))


def _is_guarded(folder: Path, protected: Collection[Path]) -> bool:
    """``folder`` ist geschützt oder Vorfahr eines geschützten Ordners (auch nach Auflösen von Links)."""
    for candidate, guarded in (
        (_norm(folder), {_norm(p) for p in protected}),
        (folder.resolve(), {Path(p).resolve() for p in protected}),
    ):
        if any(p == candidate or p.is_relative_to(candidate) for p in guarded):
            return True
    return False


def _blocks_cleanup(path: Path, dirs: Sequence[str], sample_bytes: int, min_age_s: float, now: float) -> bool:
    """Diese Datei verhindert das Löschen ihres Ordners (Spec 7.4, Abschlussprüfung C1).

    Blockiert wird von (a) jedem Video, das nicht nach seinem Namen ein Sample ist – egal wie
    groß –, (b) unfertigen Dateien, (c) Dateien jünger als das Mindestalter und (d) unbekannten
    Dateien ab der Sample-Grenze. Die Größe allein macht ein Video nie löschbar.
    """
    ext = _ext(path.name)
    if ext in PARTIAL_EXT:
        return True
    video = ext in VIDEO_LIKE_EXT
    if video and not is_named_sample(path.name, dirs):
        return True
    try:
        st = path.stat()
    except OSError:
        return not path.is_symlink()  # defekter Link: kein Inhalt
    if now - st.st_mtime < min_age_s:
        return True
    known = video or ext in SUB_EXT or ext in LEFTOVER_EXT
    return not known and st.st_size >= sample_bytes


def _only_leftovers(folder: Path, exclude: Collection[str], sample_bytes: int, min_age_s: float,
                    now: float) -> bool:
    """Nichts Schützenswertes und kein Ausschlussordner darunter; alles lesbar.

    Versteckte Ordner werden mit durchsucht (siehe ``_blocks_cleanup``).
    """
    unreadable: list[OSError] = []
    for dirpath, dirnames, filenames in os.walk(folder, onerror=unreadable.append):
        if any(d in exclude for d in dirnames):
            return False
        rel_dirs = Path(dirpath).relative_to(folder).parts
        for name in filenames:
            if _blocks_cleanup(Path(dirpath, name), rel_dirs, sample_bytes, min_age_s, now):
                return False
    return not unreadable


def cleanup_source(
    folder: Path,
    *,
    protected: Collection[Path],
    exclude: Sequence[str],
    sample_bytes: int,
    min_age_s: float = 0.0,
    now: float | None = None,
) -> bool:
    """Löscht ``folder`` samt Inhalt, wenn darin nur noch Samples und Beiwerk liegen.

    Nie gelöscht werden: geschützte Ordner und deren Vorfahren, Ausschlussordner und deren
    Vorfahren sowie symbolische Links. ``True``, wenn der Ordner gelöscht wurde.
    """
    folder = Path(folder)
    if folder.is_symlink() or not folder.is_dir():
        return False
    if folder.name in exclude or _is_guarded(folder, protected):
        return False
    now = time.time() if now is None else now
    if not _only_leftovers(folder, set(exclude), sample_bytes, min_age_s, now):
        return False
    shutil.rmtree(folder)
    return True


# --- Ausführen -------------------------------------------------------------------------


@dataclass
class ExecutionReport:
    done: int = 0
    failed: int = 0
    touched: set[Path] = field(default_factory=set)


class _Failed(Exception):
    """Vorschlag kann nicht ausgeführt werden (deutsche Meldung)."""


class _Duplicated(_Failed):
    """Video liegt am Ziel, die Quelle aber ebenfalls noch."""


@dataclass
class _Context:
    settings: Settings
    libraries: list[Path]
    protected: frozenset[Path]
    report: ExecutionReport
    now: Callable[[], float] = time.time
    placed: set[Path] = field(default_factory=set)  # in diesem Auftrag belegte Zielordner
    moved: dict[str, str] = field(default_factory=dict)  # in diesem Auftrag verschoben: alt -> neu


def _subtitles_of(video: Path) -> list[Path]:
    """Untertitel neben ``video`` mit dessen Namensstamm, die keinem anderen Video gehören."""
    try:
        siblings = list(video.parent.iterdir())
    except OSError:
        return []
    other_stems = [p.stem for p in siblings if _ext(p.name) in VIDEO_EXT and p != video]
    stem = video.stem
    found = []
    for path in siblings:
        if _ext(path.name) not in SUB_EXT or not owns_subtitle(stem, path.name):
            continue
        if any(len(other) > len(stem) and owns_subtitle(other, path.name) for other in other_stems):
            continue  # gehört zu einem Video mit längerem Namen
        if path.is_file():
            found.append(path)
    return sorted(found)


def _same_file(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _moved_to(s: Session, raw: str, scan_id: int | None, moved_now: dict[str, str]) -> Path | None:
    """Wohin wurde ``raw`` verschoben – in diesem Auftrag oder von einem Item desselben Scans?"""
    if raw in moved_now:
        return Path(moved_now[raw])
    if scan_id is None:
        return None
    new_path = s.scalars(
        select(History.new_path)
        .join(Item, (Item.source == History.old_path) & (Item.target == History.new_path))
        .where(History.action == "moved", History.old_path == raw, Item.scan_id == scan_id)
        .order_by(History.id.desc())
    ).first()
    return Path(new_path) if new_path else None


@dataclass(frozen=True)
class _Existing:
    path: Path  # wo die Version jetzt liegt
    raw: str  # wie sie im Konflikt steht
    moved: bool  # in diesem Auftrag bzw. Scan von mediasort verschoben


def _same_name(a: Path, b: Path) -> bool:
    """Gleicher Pfad bis auf Groß-/Kleinschreibung (auf SMB derselbe Eintrag)."""
    return casefold(str(a)) == casefold(str(b))


def _resolve_existing(s: Session, item: Item, source: Path, moved_now: dict[str, str]) -> list[_Existing]:
    """Vorhandene Versionen; nicht mehr vorhandene Pfade nur über protokollierte Verschiebungen
    desselben Auftrags bzw. Scans auflösen, sonst überspringen. Die Quelle selbst – auch nur
    anders geschrieben – ist nie eine vorhandene Version."""
    found: list[_Existing] = []
    for raw in (item.conflict_json or {}).get("existing", []):
        path, moved = Path(raw), False
        if not path.is_file():
            path, moved = _moved_to(s, raw, item.scan_id, moved_now), True
            if path is None or not path.is_file():
                continue
        if path == source or _same_name(path, source) or _same_file(path, source):
            continue
        if path not in [e.path for e in found]:
            found.append(_Existing(path, raw, moved))
    return found


def _case_variant(src: Path, dst: Path) -> bool:
    """``dst`` ist (auf SMB) dieselbe Datei wie ``src``, nur anders geschrieben."""
    return src != dst and _exists(dst) and (_same_name(src, dst) or _same_file(src, dst))


def _unchanged(entry: _Existing, conflict: dict) -> bool:
    """Liegt die vorhandene Version noch so vor wie beim Scan (Größe, Änderungszeit)?

    Alte Konflikte ohne ``existing_meta`` werden nicht geprüft. Von mediasort selbst verschobene
    Versionen werden nur an der Größe gemessen (eine Kopie über Dateisystemgrenzen ändert die Zeit).
    """
    if "existing_meta" not in conflict:
        return True
    meta = next((m for m in conflict["existing_meta"] or [] if m.get("path") == entry.raw), None)
    if meta is None or meta.get("size") is None:
        return False
    try:
        st = entry.path.stat()
    except OSError:
        return False
    if st.st_size != meta["size"]:
        return False
    return entry.moved or st.st_mtime == meta.get("mtime")


def _library_root(target: Path, libraries: Sequence[Path]) -> Path:
    if ".." in target.parts or not target.is_absolute():
        raise _Failed(f"Ungültiger Zielpfad: {target}")
    matches = [lib for lib in libraries if target != lib and target.is_relative_to(lib)]
    if not matches:
        raise _Failed(f"Ziel liegt in keiner eingetragenen Bibliothek: {target}")
    root = max(matches, key=lambda p: len(p.parts))
    if not root.is_dir():
        raise _Failed(ROOT_MISSING)
    return root


def _make_dirs(root: Path, directory: Path) -> None:
    """Legt nur Ordner unterhalb von ``root`` an (``root`` selbst nie)."""
    current = root
    for part in directory.relative_to(root).parts:
        current = current / part
        try:
            current.mkdir()
        except FileExistsError:
            if not current.is_dir():
                raise


def _parked(path: Path) -> Path:
    return path.with_name(path.name + OLD_SUFFIX)


class _ItemRun:
    """Führt einen Vorschlag aus und sammelt die durchgeführten Aktionen fürs Protokoll."""

    def __init__(self, ctx: _Context):
        self.ctx = ctx
        self.log: list[tuple[float, str, str, str | None]] = []

    def record(self, action: str, old: Path, new: Path | None = None) -> None:
        self.log.append((time.time(), action, str(old), str(new) if new is not None else None))

    def history(self, job_id: int, auto: bool) -> list[History]:
        return [History(ts=ts, job_id=job_id, action=action, old_path=old, new_path=new, auto=auto)
                for ts, action, old, new in self.log]

    def moves(self) -> dict[str, str]:
        return {old: new for _, action, old, new in self.log if action == "moved"}

    def move(self, src: Path, dst: Path) -> None:
        if _case_variant(src, dst):
            self.rename_case(src, dst)
            return
        try:
            move_file(src, dst)
        except SourceNotRemoved as exc:
            self.record("moved", src, dst)
            raise _Duplicated(
                f"Nach {dst} kopiert, aber die Quelle konnte nicht gelöscht werden – "
                f"die Datei liegt jetzt doppelt vor: {src} ({exc.strerror})"
            ) from exc
        self.record("moved", src, dst)

    def rename_case(self, src: Path, dst: Path) -> None:
        """Nur die Schreibweise ändert sich (SMB): über einen Zwischennamen im selben Ordner umbenennen.

        Liegt danach trotzdem eine Datei unter ``dst`` (Dateisystem unterscheidet die Schreibweise,
        es ist also eine andere Datei), wird zurückbenannt: es wird nie überschrieben.
        """
        tmp = src.with_name(src.name + TMP_SUFFIX)
        if _exists(tmp):
            raise _Failed(f"Zwischenname belegt: {tmp}")
        os.rename(src, tmp)
        try:
            if _exists(dst):
                raise FileExistsError(errno.EEXIST, "Ziel existiert bereits", str(dst))
            os.rename(tmp, dst)
        except BaseException as exc:
            try:
                os.rename(tmp, src)
            except OSError:
                raise _Failed(f"{_message(exc)}; die Datei liegt noch unter: {tmp}") from exc
            raise
        self.record("moved", src, dst)

    def delete(self, path: Path) -> None:
        path.unlink()
        self.record("deleted", path)

    def delete_version(self, video: Path) -> None:
        """Eine vorhandene Version samt ihrer Untertitel löschen."""
        subs = _subtitles_of(video)
        self.ctx.report.touched.add(video.parent)
        self.delete(video)
        for sub in subs:
            self.delete(sub)

    def park(self, files: Sequence[Path]) -> list[tuple[Path, Path]]:
        """Belegende Dateien beiseite legen (``<Name>.mediasort-old``); bei Fehler zurücklegen."""
        parked: list[tuple[Path, Path]] = []
        try:
            for original in files:
                move_file(original, _parked(original))
                parked.append((original, _parked(original)))
        except BaseException as exc:
            self.unpark(parked, exc)
        return parked

    def unpark(self, parked: Sequence[tuple[Path, Path]], exc: BaseException) -> None:
        """Beiseite gelegte Dateien zurücklegen und ``exc`` weiterreichen."""
        stuck = []
        for original, aside in reversed(parked):
            try:
                move_file(aside, original)
            except OSError:
                stuck.append(str(aside))
        if stuck and isinstance(exc, Exception):
            raise _Failed(f"{_message(exc)}; alte Version liegt noch unter: {', '.join(stuck)}") from exc
        raise exc

    def drop_parked(self, parked: Sequence[tuple[Path, Path]]) -> None:
        for original, aside in parked:
            aside.unlink()
            self.record("deleted", original)
        if parked:
            self.ctx.report.touched.add(parked[0][0].parent)

    def process(self, s: Session, item: Item) -> bool:
        """``True``: erledigt; ``False``: übersprungen (zurück auf „neu“). Fehler als Ausnahme."""
        decision = item.decision
        if decision == "skip":
            item.status = ItemStatus.NEW.value
            return False
        if decision not in _DECISIONS:
            raise _Failed(f"Unbekannte Entscheidung: {decision}")
        conflict = item.conflict_json or {}
        if conflict and decision is None:
            raise _Failed(UNDECIDED)
        if decision == "keep_new" and conflict.get("partial"):
            raise _Failed(PARTIAL_REFUSED)

        source = Path(item.source)
        try:
            st = source.stat()
        except FileNotFoundError:
            raise _Failed(f"Quelle nicht gefunden: {source}") from None
        if st.st_size != item.size or st.st_mtime != item.mtime:
            raise _Failed(SOURCE_CHANGED)
        source_subs = list(item.subtitles_json or [])
        resolved = _resolve_existing(s, item, source, self.ctx.moved)
        existing = [e.path for e in resolved]
        report = self.ctx.report

        if decision == "keep_existing":
            if not existing:
                raise _Failed("Keine vorhandene Version mehr gefunden – neue Datei bleibt erhalten")
            report.touched.add(source.parent)
            self.delete(source)
            for sub in source_subs:
                if Path(sub["path"]).is_file():
                    self.delete(Path(sub["path"]))
            self.cleanup(item, source.parent)
            return True

        if not item.target:
            raise _Failed("Kein Zielpfad vorhanden")
        target = Path(item.target)
        root = _library_root(target, self.ctx.libraries)
        replaced = target if decision == "keep_new" and target in existing else None
        replaced_subs = _subtitles_of(target) if replaced else []
        if _exists(target) and replaced is None and not _case_variant(source, target):
            raise _Failed(TARGET_TAKEN if decision == "keep_both" else f"Ziel existiert bereits: {target}")
        sub_moves = [(Path(d["path"]), Path(d["target"])) for d in source_subs if d.get("target")]
        for sub_source, sub_target in sub_moves:
            if _exists(sub_target) and sub_target not in replaced_subs and not _case_variant(sub_source, sub_target):
                raise _Failed(f"Untertitelziel belegt: {sub_target}")
        for aside in map(_parked, [replaced, *replaced_subs] if replaced else []):
            if _exists(aside):
                raise _Failed(f"Zwischenname belegt: {aside}")
        doomed = resolved if decision == "keep_new" else []  # werden ersetzt bzw. gelöscht
        if not all(_unchanged(e, conflict) for e in doomed):
            raise _Failed(EXISTING_CHANGED)

        _make_dirs(root, target.parent)
        self.ctx.placed.add(target.parent)
        report.touched.update((target.parent, source.parent))
        parked = self.park([replaced, *replaced_subs]) if replaced else []
        try:
            self.move(source, target)
        except _Duplicated:
            self.drop_parked(parked)  # neue Version liegt am Ziel
            raise
        except BaseException as exc:
            self.unpark(parked, exc)
        self.drop_parked(parked)
        for sub_source, sub_target in sub_moves:
            if sub_source.is_file():
                self.move(sub_source, sub_target)
        for entry in doomed:
            if entry.path != replaced and entry.path != target and entry.path.is_file():
                if not _unchanged(entry, conflict):  # während des Verschiebens verändert
                    raise _Failed(EXISTING_CHANGED)
                self.delete_version(entry.path)
        self.cleanup(item, source.parent)
        return True

    def cleanup(self, item: Item, folder: Path) -> None:
        settings = self.ctx.settings
        try:
            deleted = cleanup_source(
                folder,
                protected=self.ctx.protected | self.ctx.placed,
                exclude=settings.exclude,
                sample_bytes=settings.sample_bytes,
                min_age_s=settings.min_age_minutes * 60,
                now=self.ctx.now(),
            )
        except OSError as exc:
            item.note = f"Ursprungsordner konnte nicht vollständig gelöscht werden: {exc}"
            return
        if deleted:
            self.record("folder_deleted", folder)


def _message(exc: BaseException) -> str:
    if isinstance(exc, _Failed):
        return str(exc)
    if isinstance(exc, FileExistsError):
        return f"{TARGET_TAKEN}: {exc.filename}"
    if isinstance(exc, OSError):
        return f"Dateifehler: {exc}"
    return f"Unerwarteter Fehler: {exc!r}"


def _save_db_failure(session_factory: Callable[[], Session], item_id: int, run: _ItemRun,
                     job_id: int, auto: bool, exc: Exception) -> None:
    """Nach einem Datenbankfehler: Item als Fehler und Protokoll in frischer Sitzung speichern."""
    try:
        with session_factory() as s:
            item = s.get(Item, item_id)
            if item is not None:
                item.status, item.error = ItemStatus.ERROR.value, f"Datenbankfehler: {exc}"
            s.add_all(run.history(job_id, auto))
            s.commit()
    except Exception:  # der Auftrag soll trotzdem weiterlaufen
        log.exception("Vorschlag %s: Fehler konnte nicht gespeichert werden", item_id)


def _execute_one(session_factory: Callable[[], Session], item_id: int, ctx: _Context,
                 job_id: int, auto: bool) -> None:
    run = _ItemRun(ctx)
    outcome = None  # "done" | "failed" | "skipped"; None: nicht ausführbar
    try:
        with session_factory() as s:
            item = s.get(Item, item_id)
            if item is None or item.status != ItemStatus.CONFIRMED.value:
                return
            try:
                outcome = "done" if run.process(s, item) else "skipped"
            except Exception as exc:  # Fehler betreffen nur diesen Vorschlag
                s.rollback()
                item = s.get(Item, item_id)
                item.status, item.error = ItemStatus.ERROR.value, _message(exc)
                outcome = "failed"
            else:
                if outcome == "done":
                    item.status, item.error = ItemStatus.DONE.value, None
            s.add_all(run.history(job_id, auto))
            s.commit()
    except Exception as exc:  # Datenbankfehler: Auftrag läuft weiter
        _save_db_failure(session_factory, item_id, run, job_id, auto, exc)
        outcome = "failed"
    finally:
        ctx.moved.update(run.moves())
    if outcome == "done":
        ctx.report.done += 1
    elif outcome == "failed":
        ctx.report.failed += 1


def execute(
    session_factory: Callable[[], Session],
    item_ids: Sequence[int],
    *,
    settings: Settings,
    job_id: int,
    auto: bool = False,
    progress: Callable[[int, int], None] = lambda d, t: None,
    now: Callable[[], float] = time.time,
) -> ExecutionReport:
    """Führt die bestätigten Vorschläge nacheinander aus, jeden in eigener Transaktion."""
    with session_factory() as s:
        roots = [(Path(f.path), f.role) for f in s.scalars(select(Folder))]
    protected = {p for path, _ in roots for p in (path, *path.parents)}
    ctx = _Context(
        settings=settings,
        libraries=[path for path, role in roots if role == "library"],
        protected=frozenset(protected),
        report=ExecutionReport(),
        now=now,
    )
    total = len(item_ids)
    for number, item_id in enumerate(item_ids, 1):
        _execute_one(session_factory, item_id, ctx, job_id, auto)
        progress(number, total)
    return ctx.report
