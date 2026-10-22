"""FastAPI-Anwendung: verbindet Datenbank, Hintergrund-Worker, Überwachung, TMDB und Plex."""

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable, Iterable
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mediasort import mediainfo
from mediasort.db import DbCache, Folder, Item, Scan, init_db, make_engine
from mediasort.executor import execute
from mediasort.jobs import JobRunner
from mediasort.models import MediaInfo
from mediasort.planner import PlanContext, plan_folder
from mediasort.plex import PlexClient, refresh_touched
from mediasort.settings import Settings, load_settings
from mediasort.tmdb import TmdbClient
from mediasort.watcher import Watcher
from mediasort.web import routes_admin, routes_main

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

    def plex_client(self, settings: Settings) -> PlexClient | None:
        """``None``, solange Adresse oder Token fehlen (Plex ist optional)."""
        return self.plex_client_for(settings.plex_url, settings.plex_token)

    def plex_client_for(self, url: str, token: str) -> PlexClient | None:
        if not url or not token:
            return None
        return PlexClient(url, token, transport=self._plex_transport)

    def library_roots(self) -> list[Path]:
        with self.sf() as s:
            return [Path(p) for p in s.scalars(select(Folder.path).where(Folder.role == "library"))]

    def plan(self, folder: Folder, auto: bool) -> int:
        """Scannt ``folder`` mit den aktuellen Einstellungen; Rückgabe ist die Scan-ID."""
        settings = self.settings()
        tmdb = self.tmdb_client(settings)
        try:
            ctx = PlanContext(session_factory=self.sf, settings=settings, tmdb=tmdb,
                              probe=self._probe, now=self._now)
            return plan_folder(ctx, folder, auto=auto)
        finally:
            tmdb.close()

    def scan_folder(self, folder_id: int) -> str:
        """Manueller Scan als Auftrag; Ergebnistext für die Übersicht."""
        with self.sf() as s:
            folder = s.get(Folder, folder_id)
        if folder is None:
            raise LookupError("Ordner ist nicht mehr eingetragen")
        scan_id = self.plan(folder, False)
        with self.sf() as s:
            count = s.scalar(select(func.count(Item.id)).where(Item.scan_id == scan_id)) or 0
            errors = s.get(Scan, scan_id).errors_json or []
        message = "1 Vorschlag erstellt" if count == 1 else f"{count} Vorschläge erstellt"
        if errors:
            message += f"; {len(errors)} Pfade nicht lesbar"
        return message

    def run_items(self, ids: list[int], auto: bool, job_id: int | None) -> None:
        """Führt bestätigte Vorschläge im laufenden Auftrag aus und stößt danach Plex an."""
        settings = self.settings()
        report = execute(self.sf, ids, settings=settings, job_id=job_id, auto=auto)
        for hint in self.refresh_plex(settings, report.touched):
            log.warning("Plex-Teilscan nach Auftrag %s: %s", job_id, hint)

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
) -> None:
    """Stößt alle ``minutes()`` Minuten (jedes Mal neu gelesen) die Überwachung an."""
    while True:
        await sleep(max(1, await asyncio.to_thread(minutes)) * 60)
        try:
            result = await asyncio.to_thread(trigger)
        except Exception:
            log.exception("Automatische Abfrage konnte nicht gestartet werden")
        else:
            log.info("Automatische Abfrage: %s", result)


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
            if app.state.poll_task is not None:
                app.state.poll_task.cancel()
                with suppress(asyncio.CancelledError):
                    await app.state.poll_task
            await asyncio.to_thread(runner.stop)
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
    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config_dir = Path(os.environ.get("MEDIASORT_CONFIG", "/config"))
    port = int(os.environ.get("MEDIASORT_PORT", "8080"))
    uvicorn.run(create_app(config_dir), host="0.0.0.0", port=port)
