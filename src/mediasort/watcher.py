"""Automatische Ordnerüberwachung (Spec 7.6): regelmäßige Abfrage statt inotify."""

import os
from collections.abc import Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mediasort.db import FileState, Folder, Item
from mediasort.jobs import JobRunner
from mediasort.models import AutoLevel, Confidence, ItemStatus
from mediasort.settings import load_settings

SWITCHED_OFF = "Automatik ist ausgeschaltet"
SKIPPED_BUSY = "übersprungen: Auftrag läuft"
STARTED = "gestartet"


def is_stable(
    path: str, size: int, mtime: float, now: float, min_age_s: float, state: FileState | None
) -> bool:
    """Mindestens ``min_age_s`` alt und seit der letzten Abfrage (``state``) unverändert."""
    if now - mtime < min_age_s or state is None:
        return False
    return state.size == size and state.mtime == mtime


def auto_eligible(item: Item) -> bool:
    """Sicherheit hoch, kein Konflikt/keine Dublette, Status neu, kein Fehler, Ziel gesetzt."""
    return (
        item.confidence == Confidence.HIGH.value
        and not item.conflict_json
        and item.status == ItemStatus.NEW.value
        and not item.error
        and bool(item.target)
    )


class Watcher:
    def __init__(
        self,
        session_factory: Callable[[], Session],
        runner: JobRunner,
        plan: Callable[[Folder, bool], int],
        run_items: Callable[[list[int], bool], None],
        now: Callable[[], float],
    ):
        self._sf = session_factory
        self._runner = runner
        self._plan = plan
        self._run_items = run_items
        self._now = now

    def trigger(self) -> str:
        """Startet eine Abfrage als Hintergrundauftrag, außer es läuft schon einer."""
        if self._runner.busy():
            return SKIPPED_BUSY
        job_id = self._runner.submit("auto", lambda job_id, progress: self.tick(), auto=True)
        return SKIPPED_BUSY if job_id is None else STARTED

    def tick(self) -> str:
        """Eine Abfrage, synchron; gibt den Ergebnistext für die Übersicht zurück."""
        with self._sf() as s:
            settings = load_settings(s)
            folders = list(s.scalars(
                select(Folder)
                .where(Folder.role == "input", Folder.auto_level != AutoLevel.OFF.value)
                .order_by(Folder.id)
            ))
        if not settings.auto_enabled:
            return SWITCHED_OFF

        checked, new, errors = 0, 0, []
        for folder in folders:
            try:
                scan_id = self._plan(folder, True)
            except Exception as exc:  # ein Ordner darf die übrigen nicht aufhalten
                errors.append(f"Fehler in {folder.path}: {exc}")
                continue
            checked += 1
            with self._sf() as s:
                new += s.scalar(select(func.count(Item.id)).where(Item.scan_id == scan_id)) or 0

        ids = self._observe(folders, min_age_s=settings.min_age_minutes * 60)
        if ids:
            self._run_items(ids, True)

        noun = "neuer Vorschlag" if new == 1 else "neue Vorschläge"
        parts = [f"{checked} Ordner geprüft, {new} {noun}, {len(ids)} automatisch einsortiert"]
        return "; ".join(parts + errors)

    def _observe(self, folders: list[Folder], min_age_s: float) -> list[int]:
        """Schreibt ``FileState`` fort und liefert die IDs, die automatisch einsortiert werden."""
        auto_ids = {f.id for f in folders if f.auto_level == AutoLevel.AUTO.value}
        now = self._now()
        eligible: list[int] = []
        with self._sf() as s:
            items = list(s.scalars(
                select(Item)
                .where(Item.status == ItemStatus.NEW.value, Item.folder_id.in_([f.id for f in folders]))
                .order_by(Item.id)
            ))
            sources = {it.source for it in items}
            previous = {
                st.path: (st.size, st.mtime)
                for st in s.scalars(select(FileState).where(FileState.path.in_(sources)))
            }
            current: dict[str, tuple[int, float]] = {}
            for source in sources:
                try:
                    st = os.stat(source)
                except OSError:
                    continue
                current[source] = (st.st_size, st.st_mtime)

            for it in items:
                if it.folder_id not in auto_ids or it.source not in current or not auto_eligible(it):
                    continue
                size, mtime = current[it.source]
                if (size, mtime) != (it.size, it.mtime):
                    continue  # Datei hat sich seit dem Vorschlag geändert
                prev = previous.get(it.source)
                state = None if prev is None else FileState(path=it.source, size=prev[0], mtime=prev[1])
                if is_stable(it.source, size, mtime, now, min_age_s, state):
                    eligible.append(it.id)

            for source, (size, mtime) in current.items():
                s.merge(FileState(path=source, size=size, mtime=mtime, last_seen=now))
            s.commit()
        return eligible
