"""Bestätigte Vorschläge ausführen: verschieben, Konflikte umsetzen, aufräumen, protokollieren (Spec 7.3, 7.4).

Grundsätze: Es wird nie überschrieben, nur ausdrücklich entschiedene Dubletten werden gelöscht,
und Bibliothekswurzeln werden nie angelegt – nur Ordner darunter.
"""

import errno
import os
import shutil
import time
from collections.abc import Callable, Collection, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from mediasort.db import Folder, History, Item
from mediasort.models import ItemStatus
from mediasort.scanner import _SAMPLE_RE, SUB_EXT, VIDEO_EXT, _owns
from mediasort.settings import Settings

TMP_SUFFIX = ".mediasort-tmp"
ROOT_MISSING = "Bibliotheksordner fehlt – ist die Freigabe eingebunden?"
UNDECIDED = "Konflikt nicht entschieden"
TARGET_TAKEN = "Ziel belegt"
SOURCE_CHANGED = "Quelle wurde seit dem Scan verändert"
_DECISIONS = (None, "keep_new", "keep_existing", "keep_both")


def _ext(name: str) -> str:
    return name.rpartition(".")[2].lower() if "." in name else ""


def _exists(path: Path) -> bool:
    """Auch defekte symbolische Links zählen als vorhanden (sie würden überschrieben)."""
    return os.path.lexists(path)


# --- Verschieben -----------------------------------------------------------------------


def move_file(src: Path, dst: Path) -> None:
    """Verschiebt ``src`` nach ``dst``; ``FileExistsError``, wenn ``dst`` existiert.

    Über Dateisystemgrenzen (``EXDEV``) wird in ``<dst>.mediasort-tmp`` kopiert, die Größe
    verglichen, umbenannt und erst dann die Quelle gelöscht. Bei einem Fehler wird die
    Teilkopie entfernt; die Quelle bleibt.
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
    os.unlink(src)


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


def _only_leftovers(folder: Path, exclude: Collection[str], sample_bytes: int) -> bool:
    """Kein echtes Video und kein Ausschlussordner darunter; alles lesbar.

    Wie beim Scannen gilt ein Video als Sample, wenn „sample“ im Namen steht oder es nicht
    größer als ``sample_bytes`` ist. Versteckte Ordner werden mit durchsucht.
    """
    unreadable: list[OSError] = []
    for dirpath, dirnames, filenames in os.walk(folder, onerror=unreadable.append):
        if any(d in exclude for d in dirnames):
            return False
        for name in filenames:
            if _ext(name) not in VIDEO_EXT or _SAMPLE_RE.search(name):
                continue
            path = Path(dirpath, name)
            try:
                size = path.stat().st_size
            except OSError:
                if path.is_symlink():
                    continue  # defekter Link, kein Video
                return False
            if size > sample_bytes:
                return False
    return not unreadable


def cleanup_source(
    folder: Path, *, protected: Collection[Path], exclude: Sequence[str], sample_bytes: int
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
    if not _only_leftovers(folder, set(exclude), sample_bytes):
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


@dataclass
class _Context:
    settings: Settings
    libraries: list[Path]
    protected: frozenset[Path]
    report: ExecutionReport


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
        if _ext(path.name) not in SUB_EXT or not _owns(stem, path.name):
            continue
        if any(len(other) > len(stem) and _owns(other, path.name) for other in other_stems):
            continue  # gehört zu einem Video mit längerem Namen
        if path.is_file():
            found.append(path)
    return sorted(found)


def _same_file(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _resolve_existing(s: Session, conflict: dict | None, source: Path) -> list[Path]:
    """Vorhandene Versionen; schon verschobene Quellen dieses Laufs über ihr Item auflösen."""
    found: list[Path] = []
    for raw in (conflict or {}).get("existing", []):
        path = Path(raw)
        if not path.is_file():
            moved = s.scalars(
                select(Item.target)
                .where(Item.source == raw, Item.status == ItemStatus.DONE.value, Item.target.is_not(None))
                .order_by(Item.id.desc())
            ).first()
            if moved is None or not Path(moved).is_file():
                continue
            path = Path(moved)
        if path != source and path not in found and not _same_file(path, source):
            found.append(path)
    return found


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


class _ItemRun:
    """Führt einen Vorschlag aus und sammelt die durchgeführten Aktionen fürs Protokoll."""

    def __init__(self, ctx: _Context):
        self.ctx = ctx
        self.log: list[tuple[float, str, str, str | None]] = []

    def record(self, action: str, old: Path, new: Path | None = None) -> None:
        self.log.append((time.time(), action, str(old), str(new) if new is not None else None))

    def move(self, src: Path, dst: Path) -> None:
        move_file(src, dst)
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

    def process(self, s: Session, item: Item) -> bool:
        """``True``: erledigt; ``False``: übersprungen (zurück auf „neu“). Fehler als Ausnahme."""
        decision = item.decision
        if decision == "skip":
            item.status = ItemStatus.NEW.value
            return False
        if decision not in _DECISIONS:
            raise _Failed(f"Unbekannte Entscheidung: {decision}")
        if item.conflict_json and decision is None:
            raise _Failed(UNDECIDED)

        source = Path(item.source)
        try:
            st = source.stat()
        except FileNotFoundError:
            raise _Failed(f"Quelle nicht gefunden: {source}") from None
        if st.st_size != item.size or st.st_mtime != item.mtime:
            raise _Failed(SOURCE_CHANGED)
        source_subs = list(item.subtitles_json or [])
        existing = _resolve_existing(s, item.conflict_json, source)
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
        if _exists(target) and replaced is None:
            raise _Failed(TARGET_TAKEN if decision == "keep_both" else f"Ziel existiert bereits: {target}")
        sub_moves = [(Path(d["path"]), Path(d["target"])) for d in source_subs if d.get("target")]
        for _, sub_target in sub_moves:
            if _exists(sub_target) and sub_target not in replaced_subs:
                raise _Failed(f"Untertitelziel belegt: {sub_target}")

        if replaced is not None:
            self.delete_version(replaced)
        _make_dirs(root, target.parent)
        report.touched.update((target.parent, source.parent))
        self.move(source, target)
        for sub_source, sub_target in sub_moves:
            if sub_source.is_file():
                self.move(sub_source, sub_target)
        if decision == "keep_new":
            for path in existing:
                if path != replaced and path != target and path.is_file():
                    self.delete_version(path)
        self.cleanup(item, source.parent)
        return True

    def cleanup(self, item: Item, folder: Path) -> None:
        settings = self.ctx.settings
        try:
            deleted = cleanup_source(folder, protected=self.ctx.protected, exclude=settings.exclude,
                                     sample_bytes=settings.sample_bytes)
        except OSError as exc:
            item.note = f"Ursprungsordner konnte nicht vollständig gelöscht werden: {exc}"
            return
        if deleted:
            self.record("folder_deleted", folder)


def _message(exc: Exception) -> str:
    if isinstance(exc, _Failed):
        return str(exc)
    if isinstance(exc, FileExistsError):
        return f"{TARGET_TAKEN}: {exc.filename}"
    if isinstance(exc, OSError):
        return f"Dateifehler: {exc}"
    return f"Unerwarteter Fehler: {exc!r}"


def _execute_one(session_factory: Callable[[], Session], item_id: int, ctx: _Context,
                 job_id: int, auto: bool) -> None:
    with session_factory() as s:
        item = s.get(Item, item_id)
        if item is None or item.status != ItemStatus.CONFIRMED.value:
            return
        run = _ItemRun(ctx)
        try:
            done = run.process(s, item)
        except Exception as exc:
            s.rollback()
            item = s.get(Item, item_id)
            item.status, item.error = ItemStatus.ERROR.value, _message(exc)
            ctx.report.failed += 1
        else:
            if done:
                item.status, item.error = ItemStatus.DONE.value, None
                ctx.report.done += 1
        s.add_all(
            History(ts=ts, job_id=job_id, action=action, old_path=old, new_path=new, auto=auto)
            for ts, action, old, new in run.log
        )
        s.commit()


def execute(
    session_factory: Callable[[], Session],
    item_ids: Sequence[int],
    *,
    settings: Settings,
    job_id: int,
    auto: bool = False,
    progress: Callable[[int, int], None] = lambda d, t: None,
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
    )
    total = len(item_ids)
    for number, item_id in enumerate(item_ids, 1):
        _execute_one(session_factory, item_id, ctx, job_id, auto)
        progress(number, total)
    return ctx.report
