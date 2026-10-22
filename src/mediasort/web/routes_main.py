"""Übersicht, manueller Scan und Anzeige des aktuellen Auftrags."""

from dataclasses import dataclass, field
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mediasort.db import Folder, History, Item, Job, Scan
from mediasort.models import ItemStatus
from mediasort.plex import PlexClient, PlexError
from mediasort.settings import Settings
from mediasort.web.templating import is_htmx, render

router = APIRouter()

BUSY = "Es läuft bereits ein Auftrag. Bitte warten, bis er fertig ist."
NOTICES = {"busy": BUSY}
_OPEN = (ItemStatus.NEW.value, ItemStatus.CONFIRMED.value)
RECENT_ACTIONS = 5
OVERVIEW_PLEX_TIMEOUT_S = 3.0  # die Übersicht soll bei Plex-Ausfall nicht hängen
_FINISHED = ("done", "error")


@dataclass
class InputRow:
    folder: Folder
    open_count: int
    scan_errors: list[str] = field(default_factory=list)


def latest_job(s: Session, kind: str | None = None) -> Job | None:
    query = select(Job).order_by(Job.id.desc()).limit(1)
    if kind is not None:
        query = query.where(Job.kind == kind)
    return s.scalars(query).first()


def warnings(settings: Settings, libraries: list[Folder], plex: PlexClient | None) -> list[str]:
    found = []
    if not settings.tmdb_key:
        found.append("TMDB-Schlüssel fehlt: ohne ihn kann nichts erkannt werden (Einstellungen).")
    if plex is not None:
        try:
            if plex.auto_empty_trash():
                found.append(
                    "Plex leert den Papierkorb automatisch: Titel verschwinden aus Plex, sobald ihre "
                    "Dateien kurz fehlen. Bitte in Plex „Papierkorb nach jedem Scan automatisch leeren“ "
                    "ausschalten."
                )
        except PlexError as exc:  # „Plex nicht erreichbar: …“ oder „Plex-Fehler: HTTP …“
            found.append(str(exc))
    for library in libraries:
        if not Path(library.path).is_dir():
            found.append(f"Bibliotheksordner fehlt: {library.path} (Freigabe nicht eingebunden?)")
    return found


def _input_rows(s: Session) -> list[InputRow]:
    rows = []
    for folder in s.scalars(select(Folder).where(Folder.role == "input").order_by(Folder.path)):
        open_count = s.scalar(
            select(func.count(Item.id)).where(Item.folder_id == folder.id, Item.status.in_(_OPEN))
        ) or 0
        last_scan = s.scalars(
            select(Scan).where(Scan.folder_id == folder.id).order_by(Scan.id.desc()).limit(1)
        ).first()
        errors = list(last_scan.errors_json or []) if last_scan is not None else []
        rows.append(InputRow(folder=folder, open_count=open_count, scan_errors=errors))
    return rows


@router.get("/")
def overview(request: Request, hinweis: str = ""):
    services = request.app.state.services
    settings = services.settings()
    with request.app.state.sf() as s:
        inputs = _input_rows(s)
        libraries = list(s.scalars(select(Folder).where(Folder.role == "library").order_by(Folder.path)))
        last_auto = latest_job(s, "auto")
        job = latest_job(s)
        recent = list(s.scalars(select(History).order_by(History.id.desc()).limit(RECENT_ACTIONS)))
    return render(
        request, "overview.html", active="/",
        settings=settings, inputs=inputs, last_auto=last_auto, job=job, recent=recent,
        warnings=warnings(settings, libraries, services.plex_client(settings, OVERVIEW_PLEX_TIMEOUT_S)),
        notice=NOTICES.get(hinweis),
    )


@router.post("/scan/{folder_id}")
def start_scan(request: Request, folder_id: int):
    state = request.app.state
    with state.sf() as s:
        if s.get(Folder, folder_id) is None:
            raise HTTPException(404, "Ordner nicht gefunden")
    job_id = state.runner.submit("scan", lambda job_id, progress: state.services.scan_folder(folder_id))
    notice = BUSY if job_id is None else None
    if is_htmx(request):
        return job_fragment(request, notice)
    return RedirectResponse("/" if notice is None else "/?hinweis=busy", status_code=303)


def job_fragment(request: Request, notice: str | None = None):
    """Fragment des jüngsten Auftrags; ist er (per htmx abgefragt) fertig, lädt die Seite neu.

    Das Fragment fragt nur nach, solange der Auftrag aktiv ist; die Antwort auf die letzte Nachfrage
    meldet den Abschluss und lässt htmx die Übersicht (offene Vorschläge) neu laden.
    """
    with request.app.state.sf() as s:
        job = latest_job(s)
        response = render(request, "_job.html", job=job, notice=notice)
    if is_htmx(request) and notice is None and job is not None and job.status in _FINISHED:
        response.headers["HX-Refresh"] = "true"
    return response


@router.get("/jobs/current")
def current_job(request: Request):
    return job_fragment(request)
