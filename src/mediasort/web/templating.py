"""Jinja2-Templates mit deutschen Bezeichnungen für Codes aus der Datenbank."""

from datetime import datetime
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates
from starlette.responses import Response

AUTO_LEVELS = {"off": "Aus", "detect": "Nur erkennen", "auto": "Automatisch einsortieren"}
ROLES = {"input": "Eingang", "library": "Bibliothek"}
MEDIA_TYPES = {"auto": "Automatisch", "movie": "Filme", "series": "Serien", "3d": "3D-Filme"}
JOB_KINDS = {"scan": "Scan", "auto": "Automatische Abfrage", "execute": "Ausführen",
             "library": "Bibliothek prüfen"}
JOB_STATUSES = {"queued": "wartet", "running": "läuft", "done": "fertig", "error": "Fehler"}
ACTIONS = {"moved": "verschoben", "deleted": "gelöscht", "folder_deleted": "Ordner gelöscht"}


def format_time(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M") if ts else "–"


def format_size(size: int | None) -> str:
    """Dateigröße lesbar mit deutschem Dezimalkomma (1024er-Schritte)."""
    if size is None:
        return "–"
    value = float(size)
    for unit in ("Byte", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            break
        value /= 1024
    if unit == "Byte":
        return f"{int(value)} Byte"
    return f"{value:.1f}".replace(".", ",") + f" {unit}"


templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.filters["zeit"] = format_time
templates.env.filters["groesse"] = format_size
templates.env.globals.update(
    AUTO_LEVELS=AUTO_LEVELS, ROLES=ROLES, MEDIA_TYPES=MEDIA_TYPES,
    JOB_KINDS=JOB_KINDS, JOB_STATUSES=JOB_STATUSES, ACTIONS=ACTIONS,
)


def is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def render(request: Request, name: str, status_code: int = 200, **context) -> Response:
    return templates.TemplateResponse(request, name, context, status_code=status_code)
