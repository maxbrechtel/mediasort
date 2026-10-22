"""Einstellungen (Werte, Ordnerliste, Plex-Test), Bibliothek prüfen, Verlauf und Ignoriert."""

import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from mediasort.db import Folder, History, Ignored, Item, LibraryEntry, Scan
from mediasort.models import ItemStatus
from mediasort.plex import PlexClient, PlexError, PlexSection, suggest_mapping
from mediasort.scanner import letter_range
from mediasort.settings import load_settings, save_settings
from mediasort.web.forms import (
    FormError,
    parse_folder_form,
    parse_folder_options,
    parse_settings_form,
    settings_form_values,
    submitted_values,
)
from mediasort.web.routes_main import BUSY, NOTICES, OVERVIEW_PLEX_TIMEOUT_S, job_fragment, latest_job
from mediasort.web.templating import is_htmx, render

router = APIRouter()

BUSY_FOLDERS = ("Es läuft gerade ein Auftrag. Ordner können erst danach entfernt "
                "oder ihre Ziele geändert werden.")
_TARGET_COLUMNS = ("movie_target_id", "series_target_id", "movie3d_target_id")
_UNFINISHED = (ItemStatus.NEW.value, ItemStatus.CONFIRMED.value, ItemStatus.ERROR.value)


async def _form(request: Request) -> FormData:
    """Liest das Formular asynchron, damit die Handler selbst synchron (im Threadpool) laufen können."""
    return await request.form()


Form = Annotated[FormData, Depends(_form)]


@dataclass
class PlexTest:
    error: str | None = None
    sections: list[PlexSection] = field(default_factory=list)
    mapping: list[tuple[str, str]] = field(default_factory=list)
    auto_empty_trash: bool = False


def _settings_page(request: Request, *, status_code: int = 200, values=None, **context):
    services = request.app.state.services
    with request.app.state.sf() as s:
        folders = list(s.scalars(select(Folder).order_by(Folder.role, Folder.path)))
    return render(
        request, "settings.html", status_code=status_code, active="/settings",
        values=values if values is not None else settings_form_values(services.settings()),
        folders=folders, libraries=[f for f in folders if f.role == "library"],
        paths={f.id: f.path for f in folders}, **context,
    )


def _library_ids(s: Session) -> set[int]:
    return set(s.scalars(select(Folder.id).where(Folder.role == "library")))


@router.get("/settings")
def settings_page(request: Request):
    return _settings_page(request)


@router.post("/settings")
def save_settings_form(request: Request, form: Form):
    current = request.app.state.services.settings()
    try:
        new = parse_settings_form(form, current)
    except FormError as exc:
        return _settings_page(request, status_code=400, values=submitted_values(form, current), error=str(exc))
    with request.app.state.sf() as s:
        save_settings(s, new)
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/folders")
def add_folder(request: Request, form: Form):
    with request.app.state.sf() as s:
        try:
            fields = parse_folder_form(form, _library_ids(s))
            if s.scalars(select(Folder).where(Folder.path == fields["path"])).first() is not None:
                raise FormError(f"Der Ordner {fields['path']} ist bereits eingetragen")
        except FormError as exc:
            return _settings_page(request, status_code=400, folder_error=str(exc))
        s.add(Folder(**fields))
        s.commit()
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/folders/{folder_id}")
def edit_folder(request: Request, folder_id: int, form: Form):
    """Typ, Automatik-Stufe und Ziele ändern; Pfad und Rolle bleiben, offene Vorschläge auch."""
    with request.app.state.sf() as s:
        folder = s.get(Folder, folder_id)
        if folder is None:
            raise HTTPException(404, "Ordner nicht gefunden")
        try:
            options = parse_folder_options(form, folder.role, _library_ids(s))
        except FormError as exc:
            return _settings_page(request, status_code=400, folder_error=str(exc))
        targets_change = any(getattr(folder, c) != options[c] for c in _TARGET_COLUMNS)
        if targets_change and request.app.state.runner.busy():
            return _settings_page(request, status_code=409, folder_error=BUSY_FOLDERS)
        for name, value in options.items():
            setattr(folder, name, value)
        s.commit()
    return RedirectResponse("/settings", status_code=303)


def delete_folder(s: Session, folder_id: int) -> bool:
    """Entfernt einen Ordner samt offenen Vorschlägen; Verweise anderer Ordner auf ihn als Ziel entfallen.

    Erledigte und ignorierte Vorschläge sowie der Verlauf bleiben erhalten.
    """
    folder = s.get(Folder, folder_id)
    if folder is None:
        return False
    for column in _TARGET_COLUMNS:
        attr = getattr(Folder, column)
        s.execute(update(Folder).where(attr == folder_id).values({column: None}))
    s.execute(delete(Item).where(Item.folder_id == folder_id, Item.status.in_(_UNFINISHED)))
    s.execute(delete(LibraryEntry).where(LibraryEntry.library_id == folder_id))
    s.delete(folder)
    s.commit()
    return True


@router.post("/settings/folders/{folder_id}/delete")
def remove_folder(request: Request, folder_id: int):
    with request.app.state.sf() as s:
        if s.get(Folder, folder_id) is None:
            raise HTTPException(404, "Ordner nicht gefunden")
        if request.app.state.runner.busy():
            return _settings_page(request, status_code=409, folder_error=BUSY_FOLDERS)
        delete_folder(s, folder_id)
    return RedirectResponse("/settings", status_code=303)


def run_plex_test(client: PlexClient | None, library_roots: list[Path]) -> PlexTest:
    if client is None:
        return PlexTest(error="Bitte Plex-Adresse und Token eintragen.")
    try:
        sections = client.sections()
        trash = client.auto_empty_trash()
    except PlexError as exc:
        return PlexTest(error=str(exc))
    mapping = suggest_mapping(library_roots, sections)
    return PlexTest(sections=sections, mapping=mapping, auto_empty_trash=trash)


@router.post("/settings/plex/test")
def plex_test(request: Request, form: Form):
    services = request.app.state.services
    saved = services.settings()
    url = str(form.get("plex_url", saved.plex_url)).strip()
    token = str(form.get("plex_token", saved.plex_token)).strip()
    result = run_plex_test(services.plex_client_for(url, token), services.library_roots())
    if is_htmx(request):
        return render(request, "_plex_test.html", plex_test=result)
    return _settings_page(request, values=submitted_values(form, saved), plex_test=result)


# --- Bibliothek prüfen (Spec 7.5) ----------------------------------------------------------------

TRASH_WARNING = (
    "Plex leert den Papierkorb automatisch. Beim Prüfen einer Bibliothek werden Dateien umbenannt "
    "oder verschoben und fehlen dabei kurz; Plex würde die Titel dann samt Metadaten endgültig "
    "entfernen. Bitte in Plex „Papierkorb nach jedem Scan automatisch leeren“ ausschalten oder "
    "bestätigen, dass du das Risiko kennst."
)
TRASH_CONFIRM_MISSING = "Bitte zuerst bestätigen, dass du die Warnung gelesen hast."
PLEX_UNREACHABLE = ("Plex ist nicht erreichbar; ob der Papierkorb automatisch geleert wird, konnte nicht "
                    "geprüft werden. Der Scan wurde trotzdem gestartet.")
LIBRARY_NOTICES = {**NOTICES, "plex": PLEX_UNREACHABLE}
RECENT_LIBRARY_SCANS = 10
HISTORY_PAGE_SIZE = 100
_LETTER_ERROR = "Bitte je einen Buchstaben (A–Z) oder die Ziffer 0 angeben."


@dataclass
class TrashCheck:
    warn: bool = False  # Plex leert den Papierkorb automatisch und die Warnung ist noch nicht bestätigt
    hint: str | None = None  # Plex nicht erreichbar: Hinweis statt Sperre


@dataclass
class ScanRow:
    scan: Scan
    folder: str
    count: int


def check_trash(request: Request) -> TrashCheck:
    services = request.app.state.services
    settings = services.settings()
    if settings.confirmed_trash_warning:
        return TrashCheck()
    client = services.plex_client(settings, OVERVIEW_PLEX_TIMEOUT_S)
    if client is None:
        return TrashCheck()
    try:
        return TrashCheck(warn=client.auto_empty_trash())
    except PlexError as exc:
        return TrashCheck(hint=f"{exc}. Die Papierkorb-Einstellung von Plex konnte nicht geprüft werden.")


def _recent_scans(s: Session) -> list[ScanRow]:
    rows = s.execute(
        select(Scan, Folder.path).join(Folder, Folder.id == Scan.folder_id)
        .where(Folder.role == "library").order_by(Scan.id.desc()).limit(RECENT_LIBRARY_SCANS)
    ).all()
    counts = dict(s.execute(
        select(Item.scan_id, func.count(Item.id)).where(Item.scan_id.in_([r[0].id for r in rows]))
        .group_by(Item.scan_id)
    ).all()) if rows else {}
    return [ScanRow(scan, path, counts.get(scan.id, 0)) for scan, path in rows]


def _library_page(request: Request, *, status_code: int = 200, values=None, trash: TrashCheck | None = None,
                  error: str | None = None, notice: str | None = None):
    with request.app.state.sf() as s:
        libraries = list(s.scalars(select(Folder).where(Folder.role == "library").order_by(Folder.path)))
        scans = _recent_scans(s)
        job = latest_job(s)
    return render(
        request, "library.html", status_code=status_code, active="/library",
        libraries=libraries, scans=scans, job=job, error=error, notice=notice,
        trash=trash if trash is not None else check_trash(request), trash_warning=TRASH_WARNING,
        values=values or {"folder_id": "", "mode": "letters", "from_letter": "A", "to_letter": "Z",
                          "subfolder": ""},
    )


@router.get("/library")
def library_page(request: Request, hinweis: str = ""):
    return _library_page(request, notice=LIBRARY_NOTICES.get(hinweis))


def _letter(value: str, default: str) -> str:
    value = (value.strip() or default).upper()
    if len(value) != 1 or not value.isascii() or not value.isalnum():
        raise FormError(_LETTER_ERROR)
    return "0" if value.isdigit() else value


def _subset(form: FormData, root: Path) -> tuple[Callable[[str], bool], str]:
    """Filter für oberste Einträge und das Etikett für ``Scan.subset``; ``FormError`` bei ungültigen Angaben."""
    subfolder = str(form.get("subfolder", "")).strip()
    mode = str(form.get("mode", "")) or ("folder" if subfolder else "letters")
    if mode == "letters":
        first = _letter(str(form.get("from_letter", "")), "A")
        last = _letter(str(form.get("to_letter", "")), "Z")
        if first > last:
            raise FormError("Der erste Buchstabe muss vor dem letzten liegen.")
        return letter_range(first, last), f"{first}–{last}" if first != last else first
    if mode != "folder":
        raise FormError("Unbekannter Ausschnitt.")
    if not subfolder:
        raise FormError("Bitte den Namen des Ordners angeben.")
    wanted = unicodedata.normalize("NFC", subfolder)
    try:
        names = [p.name for p in root.iterdir() if p.is_dir()]
    except OSError:
        raise FormError(f"Ordner nicht lesbar: {root}") from None
    chosen = next((n for n in names if unicodedata.normalize("NFC", n) == wanted), None)
    if chosen is None:
        raise FormError(f"In {root} gibt es keinen Ordner „{subfolder}“.")
    return (lambda name: name == chosen), f"Ordner: {chosen}"


@router.post("/library/scan")
def library_scan(request: Request, form: Form):
    state = request.app.state
    values = {"folder_id": str(form.get("folder_id", "")), "mode": str(form.get("mode", "")) or "letters",
              "from_letter": str(form.get("from_letter", "")) or "A",
              "to_letter": str(form.get("to_letter", "")) or "Z",
              "subfolder": str(form.get("subfolder", ""))}
    with state.sf() as s:
        folder = s.get(Folder, int(values["folder_id"])) if values["folder_id"].isdigit() else None
    if folder is None:
        return _library_page(request, status_code=404 if values["folder_id"].isdigit() else 400,
                             values=values, error="Bitte eine Bibliothek auswählen.")
    if folder.role != "library":
        return _library_page(request, status_code=400, values=values, error="Das ist keine Bibliothek.")
    try:
        subset, label = _subset(form, Path(folder.path))
    except FormError as exc:
        return _library_page(request, status_code=400, values=values, error=str(exc))

    trash = check_trash(request)
    if trash.warn:
        if not form.get("confirm_trash_warning"):
            return _library_page(request, status_code=400, values=values, trash=trash, error=TRASH_CONFIRM_MISSING)
        with state.sf() as s:
            settings = load_settings(s)
            settings.confirmed_trash_warning = True
            save_settings(s, settings)

    folder_id = folder.id
    job_id = state.runner.submit(
        "library", lambda job_id, progress: state.services.plan_subset(folder_id, subset, label))
    notice = BUSY if job_id is None else None
    if is_htmx(request):
        return job_fragment(request, notice)
    hint = "busy" if job_id is None else "plex" if trash.hint else ""
    return RedirectResponse("/library" + (f"?hinweis={hint}" if hint else ""), status_code=303)


# --- Verlauf (Spec 8) ----------------------------------------------------------------------------


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@router.get("/history")
def history_page(request: Request, q: str = "", auto: str = "", page: str = "1"):
    q = q.strip()
    query = select(History)
    if q:
        pattern = f"%{_escape_like(q)}%"
        query = query.where(
            History.old_path.ilike(pattern, escape="\\") | History.new_path.ilike(pattern, escape="\\"))
    if auto in ("ja", "nein"):
        query = query.where(History.auto.is_(auto == "ja"))
    with request.app.state.sf() as s:
        total = s.scalar(select(func.count()).select_from(query.subquery())) or 0
        pages = max(1, -(-total // HISTORY_PAGE_SIZE))
        number = min(max(int(page), 1) if page.isdecimal() else 1, pages)
        rows = list(s.scalars(query.order_by(History.ts.desc(), History.id.desc())
                              .limit(HISTORY_PAGE_SIZE).offset((number - 1) * HISTORY_PAGE_SIZE)))
    return render(request, "history.html", active="/history", rows=rows, total=total,
                  q=q, auto=auto if auto in ("ja", "nein") else "", page=number, pages=pages)


# --- Ignoriert (Spec 8) --------------------------------------------------------------------------


@router.get("/ignored")
def ignored_page(request: Request):
    with request.app.state.sf() as s:
        rows = list(s.scalars(select(Ignored).order_by(Ignored.created.desc(), Ignored.id.desc())))
    return render(request, "ignored.html", active="/ignored", rows=rows)


@router.post("/ignored/{path_id}/remove")
def unignore(request: Request, path_id: int):
    """Ignorieren aufheben: der nächste Scan schlägt die Datei wieder vor (alte Vorschläge bleiben Historie)."""
    with request.app.state.sf() as s:
        row = s.get(Ignored, path_id)
        if row is None:
            raise HTTPException(404, "Eintrag nicht gefunden")
        s.delete(row)
        s.commit()
    return RedirectResponse("/ignored", status_code=303)
