"""FastAPI-Anwendung: verbindet Datenbank, Hintergrund-Worker, Überwachung, TMDB und Plex."""

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable, Iterable, Iterator, Sequence
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mediasort import mediainfo, plex
from mediasort.db import DbCache, Folder, Item, Scan, init_db, make_engine
from mediasort.executor import execute
from mediasort.jobs import JobRunner
from mediasort.models import Candidate, MediaInfo, MediaKind
from mediasort.planner import PlanContext, plan_folder, replan_item
from mediasort.plex import PlexClient, refresh_touched
from mediasort.settings import Settings, load_settings
from mediasort.tmdb import TmdbClient
from mediasort.watcher import Watcher
from mediasort.web import routes_admin, routes_main, routes_review

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


class Services:
    """Baut TMDB- und Plex-Clients aus den aktuellen Einstellungen und verbindet die Bausteine."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        *,
        probe: Callable[[Path], MediaInfo | None],
        tmdb_transport: httpx.BaseTransport | None = None,
        plex_transport: httpx.BaseTransport | None = None,
        now: Callable[[], float] = time.time,
    ):
        self.sf = session_factory
        self._probe = probe
        self._tmdb_transport = tmdb_transport
        self._plex_transport = plex_transport
        self._now = now

    def settings(self) -> Settings:
        with self.sf() as s:
            return load_settings(s)

    def tmdb_client(self, settings: Settings) -> TmdbClient:
        return TmdbClient(
            api_key=settings.tmdb_key,
            language=settings.language,
            cache=DbCache(self.sf, ttl_days=settings.cache_days),
            transport=self._tmdb_transport,
        )

    def plex_client(self, settings: Settings, timeout: float = plex.TIMEOUT_S) -> PlexClient | None:
        """``None``, solange Adresse oder Token fehlen (Plex ist optional)."""
        return self.plex_client_for(settings.plex_url, settings.plex_token, timeout)

    def plex_client_for(self, url: str, token: str, timeout: float = plex.TIMEOUT_S) -> PlexClient | None:
        if not url or not token:
            return None
        return PlexClient(url, token, transport=self._plex_transport, timeout=timeout)

    def library_roots(self) -> list[Path]:
        with self.sf() as s:
            return [Path(p) for p in s.scalars(select(Folder.path).where(Folder.role == "library"))]

    @contextmanager
    def _tmdb(self) -> Iterator[tuple[Settings, TmdbClient]]:
        settings = self.settings()
        tmdb = self.tmdb_client(settings)
        try:
            yield settings, tmdb
        finally:
            tmdb.close()

    @contextmanager
    def _plan_context(self) -> Iterator[PlanContext]:
        with self._tmdb() as (settings, tmdb):
            yield PlanContext(session_factory=self.sf, settings=settings, tmdb=tmdb,
                              probe=self._probe, now=self._now)

    def plan(self, folder: Folder, auto: bool) -> int:
        """Scannt ``folder`` mit den aktuellen Einstellungen; Rückgabe ist die Scan-ID."""
        with self._plan_context() as ctx:
            return plan_folder(ctx, folder, auto=auto)

    def replan(
        self,
        item_id: int,
        *,
        tmdb_ref: tuple[str, str] | None = None,
        season: int | None = None,
        episodes: Sequence[int] | None = None,
        format_3d: str | None = None,
    ) -> None:
        """Plant einen Vorschlag neu, wahlweise mit Korrekturen (siehe ``replan_item``)."""
        with self._plan_context() as ctx:
            replan_item(ctx, item_id, tmdb_ref=tmdb_ref, season=season, episodes=episodes,
                        format_3d=format_3d)

    def search(self, kind: MediaKind, query: str, year: int | None) -> list[Candidate]:
        """TMDB-Suche für die Korrektur."""
        with self._tmdb() as (_settings, tmdb):
            return tmdb.search(kind, query, year)

    def _plan_message(self, scan_id: int) -> str:
        with self.sf() as s:
            count = s.scalar(select(func.count(Item.id)).where(Item.scan_id == scan_id)) or 0
            errors = s.get(Scan, scan_id).errors_json or []
        message = "1 Vorschlag erstellt" if count == 1 else f"{count} Vorschläge erstellt"
        if errors:
            message += f"; {len(errors)} Pfade nicht lesbar"
        return message

    def _folder(self, folder_id: int) -> Folder:
        with self.sf() as s:
            folder = s.get(Folder, folder_id)
        if folder is None:
            raise LookupError("Ordner ist nicht mehr eingetragen")
        return folder

    def scan_folder(self, folder_id: int) -> str:
        """Manueller Scan als Auftrag; Ergebnistext für die Übersicht."""
        return self._plan_message(self.plan(self._folder(folder_id), False))

    def plan_subset(self, folder_id: int, subset: Callable[[str], bool], label: str) -> str:
        """Prüft einen Ausschnitt eines Ordners (Bibliothek prüfen); ``label`` steht im ``Scan``."""
        folder = self._folder(folder_id)
        with self._plan_context() as ctx:
            scan_id = plan_folder(ctx, folder, subset=subset, subset_label=label)
        return self._plan_message(scan_id)

    def run_items(
        self,
        ids: list[int],
        auto: bool,
        job_id: int | None,
        progress: Callable[[int, int], None] | None = None,
    ) -> list[str]:
        """Führt bestätigte Vorschläge im laufenden Auftrag aus, stößt danach Plex an; liefert Plex-Hinweise."""
        settings = self.settings()
        report = execute(self.sf, ids, settings=settings, job_id=job_id, auto=auto,
                         progress=progress or (lambda done, total: None))
        hints = self.refresh_plex(settings, report.touched)
        for hint in hints:
            log.warning("Plex-Teilscan nach Auftrag %s: %s", job_id, hint)
        return hints

    def refresh_plex(self, settings: Settings, touched: Iterable[Path]) -> list[str]:
        """Plex-Teilscans; liefert Hinweise statt Fehler (der Auftrag gilt trotzdem als erfolgreich)."""
        client = self.plex_client(settings)
        if client is None:
            return []
        try:
            return refresh_touched(client, touched, settings.plex_path_map, self.library_roots())
        except Exception as exc:  # Plex darf einen Auftrag nie scheitern lassen
            log.exception("Plex-Teilscan fehlgeschlagen")
            return [f"Plex-Teilscan fehlgeschlagen: {exc}"]


async def poll_loop(
    minutes: Callable[[], int],
    trigger: Callable[[], str],
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    default_minutes: int = Settings.poll_minutes,
) -> None:
    """Stößt alle ``minutes()`` Minuten (jedes Mal neu gelesen) die Überwachung an.

    Kann das Intervall nicht gelesen werden, gilt der zuletzt gelesene Wert (anfangs ``default_minutes``).
    """
    interval = default_minutes
    while True:
        try:
            interval = max(1, int(await asyncio.to_thread(minutes)))
        except Exception:
            log.exception("Abfrageintervall nicht lesbar, verwende %s Minuten", interval)
        await sleep(interval * 60)
        try:
            result = await asyncio.to_thread(trigger)
        except Exception:
            log.exception("Automatische Abfrage konnte nicht gestartet werden")
        else:
            log.info("Automatische Abfrage: %s", result)


async def _stop_task(task: asyncio.Task | None) -> None:
    """Beendet die Abfrageschleife; ihr eigener Fehler darf das Herunterfahren nicht verhindern."""
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:
        log.exception("Automatische Abfrage ist mit einem Fehler beendet worden")


def create_app(
    config_dir: Path,
    *,
    tmdb_transport: httpx.BaseTransport | None = None,
    plex_transport: httpx.BaseTransport | None = None,
    probe: Callable[[Path], MediaInfo | None] = mediainfo.probe,
    start_background: bool = True,
) -> FastAPI:
    engine = make_engine(Path(config_dir) / "mediasort.db")
    sf = init_db(engine)
    services = Services(sf, probe=probe, tmdb_transport=tmdb_transport, plex_transport=plex_transport)
    runner = JobRunner(sf)
    watcher = Watcher(sf, runner, plan=services.plan, run_items=services.run_items, now=time.time)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        runner.start()
        app.state.poll_task = None
        if start_background:
            app.state.poll_task = asyncio.create_task(
                poll_loop(lambda: services.settings().poll_minutes, watcher.trigger)
            )
        try:
            yield
        finally:
            try:
                await _stop_task(app.state.poll_task)
            finally:
                try:
                    await asyncio.to_thread(runner.stop)
                finally:
                    engine.dispose()

    app = FastAPI(title="mediasort", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.sf = sf
    app.state.runner = runner
    app.state.watcher = watcher
    app.state.services = services
    app.state.poll_task = None
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(routes_main.router)
    app.include_router(routes_admin.router)
    app.include_router(routes_review.router)
    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config_dir = Path(os.environ.get("MEDIASORT_CONFIG", "/config"))
    port = int(os.environ.get("MEDIASORT_PORT", "8080"))
    uvicorn.run(create_app(config_dir), host="0.0.0.0", port=port)
