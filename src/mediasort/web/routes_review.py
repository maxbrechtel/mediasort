"""Prüfliste: Vorschläge prüfen, bestätigen, korrigieren, ignorieren, Konflikte entscheiden und ausführen."""

import re
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from mediasort.db import Folder, Ignored, Item
from mediasort.models import AudioStream, Confidence, ItemStatus, MediaInfo, MediaKind
from mediasort.quality import (
    audio_codec_label,
    languages_tag,
    pick_audio,
    resolution_label,
    video_codec_label,
)
from mediasort.tmdb import TmdbError
from mediasort.watcher import auto_eligible
from mediasort.web.forms import (
    CORRECTION_FIELDS,
    FORMATS_3D,
    FormError,
    parse_correction_form,
)
from mediasort.web.routes_admin import Form
from mediasort.web.routes_main import BUSY, job_fragment, latest_job
from mediasort.web.templating import is_htmx, render

router = APIRouter()

NO_CONFIRMED = "Keine bestätigten Vorschläge."
NOTICES = {"busy": BUSY, "keine": NO_CONFIRMED}
DECIDE_FIRST = "Bitte zuerst entscheiden"
NOT_OPEN = "Dieser Vorschlag ist nicht mehr offen."
CONFIDENCE_BADGES = {"hoch": "🟢 hoch", "mittel": "🟡 mittel", "niedrig": "🔴 niedrig"}
KIND_FILTERS = {MediaKind.MOVIE.value: "Filme", MediaKind.EPISODE.value: "Serien"}
ONLY_FILTERS = {"conflicts": "Nur Konflikte", "errors": "Nur Fehler"}
SUGGESTION_TEXTS = {
    "keep_new": "Vorschlag: die neue Version ist besser.",
    "keep_existing": "Vorschlag: die vorhandene Version ist besser.",
    None: "Kein Vorschlag möglich (technische Angaben fehlen oder beide gleichwertig) – bitte selbst entscheiden.",
}
DECISION_LABELS = {
    "keep_new": "Neue Version behalten",
    "keep_existing": "Vorhandene behalten",
    "keep_both": "Beide behalten",
    "skip": "Überspringen",
}
_BETTER_LABELS = {"keep_new": "Bessere behalten (neue Version)",
                  "keep_existing": "Bessere behalten (vorhandene Version)"}
_CHANNELS = {1: "1.0", 2: "2.0", 3: "2.1", 6: "5.1", 7: "6.1", 8: "7.1"}
_VISIBLE = (ItemStatus.NEW.value, ItemStatus.CONFIRMED.value, ItemStatus.ERROR.value)
_QUERY_YEAR = re.compile(r"^(?P<title>.*\S)\s+\(?(?P<year>(?:19|20)\d{2})\)?$")


# --- Darstellung ------------------------------------------------------------------------


@dataclass
class ConflictView:
    existing: list[str]
    hint: str
    options: list[tuple[str, str]]  # (Entscheidung, Beschriftung)
    selected: str | None
    decided: str | None  # Beschriftung der getroffenen Entscheidung


@dataclass
class Row:
    item: Item
    old_name: str
    new_path: str | None
    badge: str
    tech: list[str]
    subtitles: str | None
    blocked: str | None  # warum der Vorschlag nicht bestätigt werden kann
    conflict: ConflictView | None
    message: str | None = None


@dataclass
class SeasonGroup:
    label: str
    rows: list[Row] = field(default_factory=list)


@dataclass
class SeriesGroup:
    title: str
    seasons: list[SeasonGroup] = field(default_factory=list)


def _media_info(data: Mapping | None) -> MediaInfo | None:
    if not data:
        return None
    return MediaInfo(width=data["width"], height=data["height"], video_codec=data["video_codec"],
                     hdr=data.get("hdr"), audio=tuple(AudioStream(**a) for a in data.get("audio") or ()))


def tech_labels(info: MediaInfo | None, fallback_langs: Sequence[str]) -> list[str]:
    """Auflösung, Codec, HDR, gewählte Tonspur und Sprachen, wie sie in den Dateinamen eingehen."""
    if info is None:
        return []
    labels = [resolution_label(info.width, info.height), video_codec_label(info.video_codec)]
    if info.hdr:
        labels.append(info.hdr)
    audio = pick_audio(info.audio)
    if audio is not None:
        channels = _CHANNELS.get(audio.channels, f"{audio.channels} Kanäle")
        labels.append(f"{audio_codec_label(audio.codec, audio.profile)} {channels}")
    tag = languages_tag(info.audio, fallback_langs)
    if tag:
        labels.append(tag)
    return labels


def _subtitles_text(subs: Sequence[Mapping] | None) -> str | None:
    if not subs:
        return None
    names = [(s.get("language") or "?").upper() + (" erzwungen" if s.get("forced") else "") for s in subs]
    return "Untertitel: " + ", ".join(names)


def _display_target(item: Item, roots: Sequence[Path]) -> str | None:
    """Folgen: nur der Dateiname (Serie und Staffel stehen in den Überschriften); Filme: relativ zur Bibliothek."""
    if not item.target:
        return None
    target = Path(item.target)
    if item.kind == MediaKind.EPISODE.value:
        return target.name
    containing = [root for root in roots if target.is_relative_to(root)]
    if not containing:
        return str(target)
    return str(target.relative_to(max(containing, key=lambda r: len(r.parts))))


def blocked_reason(item: Item) -> str | None:
    if item.status == ItemStatus.ERROR.value:
        return "Fehlerhafte Vorschläge können nicht bestätigt werden"
    if item.status not in _VISIBLE:
        return NOT_OPEN
    if item.conflict_json and item.decision is None:
        return DECIDE_FIRST
    if not item.target:
        return "Kein Zielpfad – bitte korrigieren"
    return None


def decision_options(item: Item) -> list[tuple[str, str]]:
    """„Bessere behalten“ (bzw. beide Richtungen ohne Vorschlag), „Beide behalten“, „Überspringen“."""
    conflict = item.conflict_json or {}
    suggestion = conflict.get("suggestion")
    if suggestion in _BETTER_LABELS:
        options = [(suggestion, _BETTER_LABELS[suggestion])]
    else:
        options = [("keep_new", DECISION_LABELS["keep_new"]), ("keep_existing", DECISION_LABELS["keep_existing"])]
    if item.target and item.target not in (conflict.get("existing") or []):
        options.append(("keep_both", DECISION_LABELS["keep_both"]))
    options.append(("skip", DECISION_LABELS["skip"]))
    return options


def _conflict_view(item: Item) -> ConflictView | None:
    if not item.conflict_json:
        return None
    suggestion = item.conflict_json.get("suggestion")
    options = decision_options(item)
    labels = dict(options)
    return ConflictView(
        existing=list(item.conflict_json.get("existing") or []),
        hint=SUGGESTION_TEXTS.get(suggestion, SUGGESTION_TEXTS[None]),
        options=options,
        selected=item.decision or (suggestion if suggestion in labels else None),
        decided=labels.get(item.decision, item.decision) if item.decision else None,
    )


def make_row(item: Item, roots: Sequence[Path], message: str | None = None) -> Row:
    parsed = item.parsed_json or {}
    return Row(
        item=item,
        old_name=Path(item.source).name,
        new_path=_display_target(item, roots),
        badge=CONFIDENCE_BADGES.get(item.confidence or "", "–"),
        tech=tech_labels(_media_info(item.mediainfo_json), parsed.get("languages") or ()),
        subtitles=_subtitles_text(item.subtitles_json),
        blocked=blocked_reason(item),
        conflict=_conflict_view(item),
        message=message,
    )


def _library_roots(s: Session) -> list[Path]:
    return [Path(p) for p in s.scalars(select(Folder.path).where(Folder.role == "library"))]


# --- Gruppieren und Filtern ------------------------------------------------------------------


@dataclass(frozen=True)
class Filters:
    confidence: str = ""
    kind: str = ""
    only: str = ""

    @classmethod
    def of(cls, confidence: str, kind: str, only: str) -> "Filters":
        """Unbekannte Werte werden ignoriert (kein Filter)."""
        return cls(
            confidence=confidence if confidence in {c.value for c in Confidence} else "",
            kind=kind if kind in KIND_FILTERS else "",
            only=only if only in ONLY_FILTERS else "",
        )

    @property
    def active(self) -> bool:
        return bool(self.confidence or self.kind or self.only)

    def matches(self, item: Item) -> bool:
        if self.confidence and item.confidence != self.confidence:
            return False
        if self.kind and item.kind != self.kind:
            return False
        if self.only == "conflicts" and not item.conflict_json:
            return False
        if self.only == "errors" and not (item.status == ItemStatus.ERROR.value or item.error):
            return False
        return True


def _parsed_title(item: Item) -> str:
    return (item.parsed_json or {}).get("title") or ""


def _heading(item: Item) -> str:
    if item.title:
        return f"{item.title} ({item.year})" if item.year else item.title
    return _parsed_title(item) or "Unbekannt"


def _episode_order(row: Row) -> tuple:
    episodes = row.item.episodes_json or []
    return (episodes[0] if episodes else 10_000, row.item.source)


def group_rows(rows: Sequence[Row], season_label: str) -> tuple[list[SeriesGroup], list[Row]]:
    """Serien nach Serie → Staffel → Folgen, Filme einzeln (jeweils alphabetisch)."""
    shows: dict[object, list[Row]] = {}
    movies = []
    for row in rows:
        item = row.item
        if item.kind == MediaKind.EPISODE.value:
            key = ("tmdb", item.tmdb_id) if item.tmdb_id is not None else ("group", item.group_key)
            shows.setdefault(key, []).append(row)
        else:
            movies.append(row)
    series = []
    for members in shows.values():
        by_season: dict[int | None, list[Row]] = {}
        for row in members:
            by_season.setdefault(row.item.season, []).append(row)
        group = SeriesGroup(title=_heading(members[0].item))
        for season in sorted(by_season, key=lambda n: (n is None, n or 0)):
            season_rows = sorted(by_season[season], key=_episode_order)
            group.seasons.append(SeasonGroup(_season_heading(season, season_rows, season_label), season_rows))
        series.append(group)
    series.sort(key=lambda g: g.title.casefold())
    movies.sort(key=lambda r: (_heading(r.item).casefold(), r.item.source))
    return series, movies


def _season_heading(season: int | None, rows: Sequence[Row], season_label: str) -> str:
    """Name des Staffelordners im Ziel, sonst „<Staffelbezeichnung> <Nummer>“."""
    if season is None:
        return "Ohne Staffelangabe"
    for row in rows:
        if row.item.target:
            return Path(row.item.target).parent.name
    return f"{season_label} {season}"


# --- Antworten ---------------------------------------------------------------------------------


def _load(s: Session, item_id: int) -> Item:
    item = s.get(Item, item_id)
    if item is None:
        raise HTTPException(404, "Vorschlag nicht gefunden")
    return item


def _row_response(request: Request, item_id: int, status_code: int = 200, message: str | None = None):
    """htmx: die neu gezeichnete Zeile; sonst zurück zur Prüfliste (mit Meldung bei Fehlern)."""
    if not is_htmx(request):
        if status_code == 200:
            return RedirectResponse(f"/review#item-{item_id}", status_code=303)
        return _review_page(request, Filters(), status_code=status_code, notice=message)
    with request.app.state.sf() as s:
        row = make_row(_load(s, item_id), _library_roots(s), message)
    return render(request, "_item_row.html", status_code=status_code, row=row)


def _review_page(request: Request, filters: Filters, *, status_code: int = 200, notice: str | None = None):
    settings = request.app.state.services.settings()
    with request.app.state.sf() as s:
        roots = _library_roots(s)
        items = s.scalars(select(Item).where(Item.status.in_(_VISIBLE)).order_by(Item.id))
        rows = [make_row(item, roots) for item in items if filters.matches(item)]
        job = latest_job(s)
    series, movies = group_rows(rows, settings.season_label)
    return render(
        request, "review.html", status_code=status_code, active="/review",
        series=series, movies=movies, filters=filters, notice=notice, job=job,
        confidences=[c.value for c in Confidence], kind_filters=KIND_FILTERS, only_filters=ONLY_FILTERS,
    )


@router.get("/review")
def review(request: Request, confidence: str = "", kind: str = "", only: str = "", hinweis: str = "",
           bestaetigt: int | None = None):
    notice = NOTICES.get(hinweis)
    if bestaetigt is not None:
        notice = f"{bestaetigt} sichere Vorschläge bestätigt." if bestaetigt != 1 else "1 sicherer Vorschlag bestätigt."
    return _review_page(request, Filters.of(confidence, kind, only), notice=notice)


# --- Bestätigen und Entscheiden ---------------------------------------------------------------


@router.post("/items/confirm-safe")
def confirm_safe(request: Request):
    """Alle neuen Vorschläge mit hoher Sicherheit, Ziel, ohne Konflikt und ohne Fehler bestätigen."""
    with request.app.state.sf() as s:
        count = 0
        for item in s.scalars(select(Item).where(Item.status == ItemStatus.NEW.value)):
            if auto_eligible(item):
                item.status = ItemStatus.CONFIRMED.value
                count += 1
        s.commit()
    return RedirectResponse(f"/review?bestaetigt={count}", status_code=303)


@router.post("/items/{item_id}/toggle")
def toggle(request: Request, item_id: int):
    """Haken: ``new`` ↔ ``confirmed``; nicht bestätigbare Vorschläge bleiben unverändert (409)."""
    with request.app.state.sf() as s:
        item = _load(s, item_id)
        reason = None
        if item.status == ItemStatus.CONFIRMED.value:
            item.status = ItemStatus.NEW.value
        elif (reason := blocked_reason(item)) is None:
            item.status = ItemStatus.CONFIRMED.value
        s.commit()
    if reason is not None:
        return _row_response(request, item_id, 409, reason)
    return _row_response(request, item_id)


@router.post("/items/{item_id}/decision")
def decide(request: Request, item_id: int, form: Form):
    """Konflikt entscheiden und bestätigen; „Überspringen“ nimmt die Entscheidung zurück (bleibt neu)."""
    decision = str(form.get("decision", ""))
    with request.app.state.sf() as s:
        item = _load(s, item_id)
        if decision not in DECISION_LABELS:
            return _row_response(request, item_id, 400, f"Unbekannte Entscheidung: {decision}")
        if not item.conflict_json or item.status not in (ItemStatus.NEW.value, ItemStatus.CONFIRMED.value):
            return _row_response(request, item_id, 409, "Hier gibt es nichts zu entscheiden.")
        if decision not in dict(decision_options(item)):
            return _row_response(request, item_id, 409,
                                 "„Beide behalten“ geht nur, wenn die Zielnamen verschieden sind.")
        if decision == "skip":
            item.decision, item.status = None, ItemStatus.NEW.value
        else:
            item.decision, item.status = decision, ItemStatus.CONFIRMED.value
        s.commit()
    return _row_response(request, item_id)


@router.post("/items/{item_id}/ignore")
def ignore(request: Request, item_id: int):
    """Vorschlag ignorieren; die Datei taucht auch bei späteren Scans nicht mehr auf."""
    with request.app.state.sf() as s:
        item = _load(s, item_id)
        if item.status not in _VISIBLE:
            return _row_response(request, item_id, 409, NOT_OPEN)
        item.status = ItemStatus.IGNORED.value
        s.merge(Ignored(path=item.source, size=item.size, created=time.time()))
        s.commit()
    if is_htmx(request):
        return Response("", status_code=200)  # Zeile verschwindet
    return RedirectResponse("/review", status_code=303)


# --- Neu planen und Korrigieren ------------------------------------------------------------------


def _replan(request: Request, item_id: int, **overrides) -> tuple[int, str | None]:
    """(HTTP-Status, Meldung) nach ``Services.replan``; 200 ohne Meldung bei Erfolg."""
    if request.app.state.runner.busy():
        return 409, BUSY
    try:
        request.app.state.services.replan(item_id, **overrides)
    except KeyError:
        raise HTTPException(404, "Vorschlag nicht gefunden") from None
    except TmdbError as exc:
        return 502, str(exc)
    except ValueError as exc:  # Status lässt keine Korrektur zu
        return 409, str(exc)
    return 200, None


@router.post("/items/{item_id}/replan")
def replan(request: Request, item_id: int):
    """Neu planen ohne Änderungen, z. B. nachdem ein fehlendes Ziel eingetragen wurde."""
    with request.app.state.sf() as s:
        _load(s, item_id)
    status_code, message = _replan(request, item_id)
    return _row_response(request, item_id, status_code, message)


def _correct_values(item: Item, given: Mapping[str, str] | None = None) -> dict[str, str]:
    """Formularwerte: abgeschickte Angaben, sonst die des Vorschlags."""
    parsed = item.parsed_json or {}
    values = {
        "q": item.title or parsed.get("title") or "",
        "kind": "tv" if item.kind == MediaKind.EPISODE.value else "movie",
        "reference": "",
        "season": "" if item.season is None else str(item.season),
        "episodes": ", ".join(str(e) for e in item.episodes_json or []),
        "format_3d": parsed.get("format_3d") or "",
    }
    for name in ("q", "kind", *CORRECTION_FIELDS):
        if given is not None and name in given:
            values[name] = str(given[name])
    return values


def _correct_response(request: Request, item_id: int, given: Mapping[str, str] | None = None, *,
                      status_code: int = 200, error: str | None = None,
                      results: list | None = None, search_error: str | None = None):
    with request.app.state.sf() as s:
        item = _load(s, item_id)
        row = make_row(item, _library_roots(s))
    page = not is_htmx(request)
    return render(
        request, "correct.html" if page else "_correct.html", status_code=status_code,
        active="/review", row=row, values=_correct_values(item, given), formats_3d=FORMATS_3D,
        error=error, results=results, search_error=search_error, page=page,
    )


@router.get("/items/{item_id}/correct")
def correct_form(request: Request, item_id: int):
    return _correct_response(request, item_id)


def _search(request: Request, q: str, kind: str) -> tuple[int, list | None, str | None]:
    """(HTTP-Status, Treffer, Meldung); eine Jahreszahl am Ende der Suche schränkt die Treffer ein."""
    q = q.strip()
    if not q:
        return 400, None, "Bitte einen Suchbegriff eingeben."
    services = request.app.state.services
    if not services.settings().tmdb_key:
        return 400, None, "TMDB-Schlüssel fehlt (Einstellungen)."
    year = None
    if m := _QUERY_YEAR.match(q):
        q, year = m.group("title"), int(m.group("year"))
    media_kind = MediaKind.EPISODE if kind == "tv" else MediaKind.MOVIE
    try:
        return 200, services.search(media_kind, q, year), None
    except TmdbError as exc:
        return 502, None, str(exc)


@router.get("/items/{item_id}/search")
def search(request: Request, item_id: int, q: str = "", kind: str = "movie"):
    with request.app.state.sf() as s:
        _load(s, item_id)
    status_code, results, message = _search(request, q, kind)
    if is_htmx(request):
        return render(request, "_search.html", status_code=status_code, item_id=item_id,
                      results=results, search_error=message)
    return _correct_response(request, item_id, request.query_params, status_code=status_code,
                             results=results, search_error=message)


@router.post("/items/{item_id}/correct")
def correct(request: Request, item_id: int, form: Form):
    """Korrektur übernehmen (``replan_item``); htmx lädt danach die Prüfliste neu (Gruppen ändern sich)."""
    with request.app.state.sf() as s:
        _load(s, item_id)
    try:
        overrides = parse_correction_form(form)
    except FormError as exc:
        return _correct_response(request, item_id, form, status_code=400, error=str(exc))
    status_code, message = _replan(request, item_id, **overrides)
    if status_code != 200:
        return _correct_response(request, item_id, form, status_code=status_code, error=message)
    if not is_htmx(request):
        return RedirectResponse(f"/review#item-{item_id}", status_code=303)
    response = _row_response(request, item_id)
    response.headers["HX-Refresh"] = "true"
    return response


# --- Ausführen ------------------------------------------------------------------------------------


def execute_job(sf: Callable[[], Session], run_items: Callable[..., list[str]], ids: list[int]):
    """Auftragsfunktion: führt die Vorschläge aus und fasst das Ergebnis zusammen."""

    def job(job_id: int, progress: Callable[[int, int], None]) -> str:
        hints = run_items(ids, False, job_id, progress=progress)
        with sf() as s:
            statuses = Counter(s.scalars(select(Item.status).where(Item.id.in_(ids))))
        summary = f"{statuses[ItemStatus.DONE.value]} einsortiert, {statuses[ItemStatus.ERROR.value]} Fehler"
        if skipped := statuses[ItemStatus.NEW.value]:
            summary += f", {skipped} übersprungen"
        return "; ".join([summary, *hints])

    return job


@router.post("/execute")
def execute_confirmed(request: Request):
    """Startet einen Auftrag mit allen bestätigten Vorschlägen (Plex-Teilscan danach, falls eingerichtet)."""
    state = request.app.state
    notice = None
    if state.runner.busy():
        notice = BUSY
    else:
        with state.sf() as s:
            ids = list(s.scalars(select(Item.id).where(Item.status == ItemStatus.CONFIRMED.value)
                                 .order_by(Item.id)))
        if not ids:
            notice = NO_CONFIRMED
        elif state.runner.submit("execute", execute_job(state.sf, state.services.run_items, ids)) is None:
            notice = BUSY
    if is_htmx(request):
        return job_fragment(request, notice)
    if notice is None:
        return RedirectResponse("/review", status_code=303)
    return RedirectResponse(f"/review?hinweis={'busy' if notice == BUSY else 'keine'}", status_code=303)
