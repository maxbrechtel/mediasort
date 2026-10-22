"""Einstellungen: Werte, Ordnerliste und Plex-Test."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from mediasort.db import Folder, Item, LibraryEntry
from mediasort.models import ItemStatus
from mediasort.plex import PlexClient, PlexError, PlexSection, suggest_mapping
from mediasort.settings import save_settings
from mediasort.web.forms import (
    FormError,
    parse_folder_form,
    parse_folder_options,
    parse_settings_form,
    settings_form_values,
    submitted_values,
)
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
