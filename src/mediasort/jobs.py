"""Hintergrund-Worker: führt Scans und Aufträge nacheinander in einem Thread aus."""

import logging
import threading
import time
from collections.abc import Callable

from sqlalchemy import true, update
from sqlalchemy.orm import Session

from mediasort.db import Job

log = logging.getLogger(__name__)

Progress = Callable[[int, int], None]
JobFn = Callable[[int, Progress], str | None]

ABORTED = "abgebrochen (Neustart)"


class JobRunner:
    """Genau ein Auftrag zur Zeit, ohne Warteschlange.

    ``submit`` legt eine ``Job``-Zeile an (``queued``); der Arbeits-Thread setzt sie auf
    ``running`` und danach auf ``done`` (Meldung = Rückgabewert von ``fn``) oder ``error``.
    """

    def __init__(self, session_factory: Callable[[], Session], now: Callable[[], float] = time.time):
        self._sf = session_factory
        self._now = now
        self._cond = threading.Condition()
        self._pending: tuple[int, JobFn] | None = None
        self._active = False  # von submit bis zum Abschluss der Job-Zeile
        self._stopping = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        with self._cond:
            if self._thread is not None and self._thread.is_alive():
                return
            self._abort_stale()
            self._stopping = False
            self._thread = threading.Thread(target=self._loop, name="mediasort-jobs", daemon=True)
            self._thread.start()

    def _abort_stale(self) -> None:
        """Aufträge eines früheren Prozesses (wartend/laufend) gelten als abgebrochen."""
        # ein vor ``start`` angenommener eigener Auftrag bleibt bestehen
        not_own = Job.id != self._pending[0] if self._pending is not None else true()
        with self._sf() as s:
            s.execute(
                update(Job)
                .where(Job.status.in_(("queued", "running")), not_own)
                .values(status="error", message=ABORTED, finished=self._now())
            )
            s.commit()

    def stop(self) -> None:
        with self._cond:
            self._stopping = True
            self._cond.notify_all()
            thread = self._thread
        if thread is not None:
            thread.join()

    def submit(self, kind: str, fn: JobFn, *, auto: bool = False) -> int | None:
        """Legt einen Auftrag an; ``None``, wenn schon einer wartet oder läuft."""
        with self._cond:
            if self._active:
                return None
            with self._sf() as s:
                row = Job(kind=kind, status="queued", auto=auto)
                s.add(row)
                s.commit()
                job_id = row.id
            self._pending = (job_id, fn)
            self._active = True
            self._cond.notify_all()
            return job_id

    def busy(self) -> bool:
        with self._cond:
            return self._active

    def wait_idle(self, timeout: float) -> bool:
        with self._cond:
            return self._cond.wait_for(lambda: not self._active, timeout)

    def _loop(self) -> None:
        while True:
            with self._cond:
                self._cond.wait_for(lambda: self._stopping or self._pending is not None)
                if self._stopping:
                    return
                job_id, fn = self._pending
                self._pending = None
            try:
                self._run(job_id, fn)
            except Exception:  # Datenbankfehler beim Protokollieren dürfen den Thread nicht beenden
                log.exception("Auftrag %s konnte nicht protokolliert werden", job_id)
            finally:
                with self._cond:
                    self._active = False
                    self._cond.notify_all()

    def _run(self, job_id: int, fn: JobFn) -> None:
        self._update(job_id, status="running", started=self._now())

        def progress(done: int, total: int) -> None:
            self._update(job_id, progress=done, total=total)

        try:
            message = fn(job_id, progress)
        except Exception as exc:
            log.exception("Auftrag %s fehlgeschlagen", job_id)
            self._update(job_id, status="error", finished=self._now(),
                         message=f"Auftrag fehlgeschlagen: {exc}")
        else:
            self._update(job_id, status="done", finished=self._now(), message=message)

    def _update(self, job_id: int, **values) -> None:
        with self._sf() as s:
            row = s.get(Job, job_id)
            for key, value in values.items():
                setattr(row, key, value)
            s.commit()
