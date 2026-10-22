"""Formulare der Einstellungsseite und der Korrektur: Werte darstellen und Eingaben prüfen (Meldungen auf Deutsch)."""

import posixpath
from collections.abc import Collection, Iterable, Mapping
from dataclasses import replace
from pathlib import PurePosixPath

from starlette.datastructures import FormData

from mediasort.db import casefold
from mediasort.models import AutoLevel, FolderType
from mediasort.settings import Settings
from mediasort.tmdb import TmdbClient

MB = 1_048_576
MAP_SEPARATOR = "=>"
ROLES = ("input", "library")

# Zahlenfelder: Formularname -> (Bezeichnung, kleinster Wert)
_NUMBERS = {
    "sample_mb": ("Sample-Grenze", 0),
    "min_age_minutes": ("Mindestalter", 0),
    "cache_days": ("TMDB-Cache", 1),
    "poll_minutes": ("Abfrageintervall", 1),
}
_TEXTS = ("tmdb_key", "language", "season_label", "plex_url", "plex_token")
_CHECKBOXES = ("auto_enabled", "confirmed_trash_warning")
_REQUIRED = {"language": "Sprache", "season_label": "Staffelbezeichnung"}
MAX_SEASON = 999  # größere Zahlen passen nicht in SQLite-INTEGER bzw. sind sicher Tippfehler
MAX_EPISODE = 9999
FORMATS_3D = ("SBS", "HSBS", "OU", "HOU", "TAB", "MVC")
CORRECTION_FIELDS = ("reference", "season", "episodes", "format_3d")
BAD_REFERENCE = ("Bitte eine TMDB-/IMDb-ID oder einen Link angeben (z. B. tmdb:movie:603, "
                 "https://www.themoviedb.org/tv/1396 oder tt0133093)")


class FormError(ValueError):
    """Ungültige Eingabe; die Meldung wird auf der Seite angezeigt."""


def parse_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def parse_path_map(text: str) -> list[tuple[str, str]]:
    pairs = []
    for line in parse_lines(text):
        local, sep, remote = line.partition(MAP_SEPARATOR)
        if not sep or not local.strip() or not remote.strip():
            raise FormError(f"Pfadzuordnung: Zeile „{line}“ hat nicht die Form „lokal => plex“")
        pairs.append((local.strip(), remote.strip()))
    return pairs


def format_path_map(pairs: Collection[tuple[str, str]]) -> str:
    return "\n".join(f"{local} {MAP_SEPARATOR} {remote}" for local, remote in pairs)


def settings_form_values(s: Settings) -> dict[str, str | bool]:
    """Werte für die Formularfelder (Sample-Grenze in MB, Listen zeilenweise)."""
    values: dict[str, str | bool] = {name: getattr(s, name) for name in _TEXTS + _CHECKBOXES}
    values.update(
        sample_mb=str(s.sample_bytes // MB),
        min_age_minutes=str(s.min_age_minutes),
        cache_days=str(s.cache_days),
        poll_minutes=str(s.poll_minutes),
        exclude="\n".join(s.exclude),
        plex_path_map=format_path_map(s.plex_path_map),
    )
    return values


def submitted_values(form: FormData, current: Settings) -> dict[str, str | bool]:
    """Die abgeschickten Werte, ergänzt um die aktuellen für fehlende Felder (zum erneuten Anzeigen)."""
    values = settings_form_values(current)
    for name in values:
        if name in _CHECKBOXES:
            if name in form:
                values[name] = _checked(form, name)
        elif name in form:
            values[name] = str(form[name])
    return values


def _checked(form: FormData, name: str) -> bool:
    # verstecktes Feld „0“ vor der Checkbox „1“: der letzte Wert zählt
    return form.getlist(name)[-1] == "1"


def _number(values: Mapping[str, str | bool], name: str) -> int:
    label, minimum = _NUMBERS[name]
    try:
        number = int(str(values[name]).strip())
    except ValueError:
        number = None
    if number is None or number < minimum:
        raise FormError(f"„{label}“ muss eine ganze Zahl ab {minimum} sein")
    return number


def parse_settings_form(form: FormData, current: Settings) -> Settings:
    """Neue Einstellungen aus dem Formular; nicht abgeschickte Felder behalten ihren Wert."""
    values = submitted_values(form, current)
    for name, label in _REQUIRED.items():
        if not str(values[name]).strip():
            raise FormError(f"„{label}“ darf nicht leer sein")
    return replace(
        current,
        **{name: str(values[name]).strip() for name in _TEXTS},
        **{name: bool(values[name]) for name in _CHECKBOXES},
        sample_bytes=_number(values, "sample_mb") * MB,
        min_age_minutes=_number(values, "min_age_minutes"),
        cache_days=_number(values, "cache_days"),
        poll_minutes=_number(values, "poll_minutes"),
        exclude=parse_lines(str(values["exclude"])),
        plex_path_map=parse_path_map(str(values["plex_path_map"])),
    )


def _choice(form: FormData, name: str, allowed: Collection[str], label: str, default: str) -> str:
    value = str(form.get(name, default))
    if value not in allowed:
        raise FormError(f"Ungültige Angabe für „{label}“: {value}")
    return value


def _target(form: FormData, name: str, library_ids: Collection[int]) -> int | None:
    raw = str(form.get(name, "")).strip()
    if not raw:
        return None
    if not raw.isdigit() or int(raw) not in library_ids:
        raise FormError("Als Ziel kann nur ein Bibliotheksordner gewählt werden")
    return int(raw)


def parse_folder_options(form: FormData, role: str, library_ids: Collection[int]) -> dict:
    """Änderbare Felder eines Ordners; Bibliotheken haben weder Ziele noch Automatik."""
    options = {
        "media_type": _choice(form, "media_type", [t.value for t in FolderType], "Typ", FolderType.AUTO),
        "auto_level": _choice(form, "auto_level", [a.value for a in AutoLevel], "Automatik", AutoLevel.OFF),
        "movie_target_id": _target(form, "movie_target_id", library_ids),
        "series_target_id": _target(form, "series_target_id", library_ids),
        "movie3d_target_id": _target(form, "movie3d_target_id", library_ids),
    }
    if role == "library":
        options.update(movie_target_id=None, series_target_id=None, movie3d_target_id=None,
                       auto_level=AutoLevel.OFF.value)
    return options


def parse_folder_form(form: FormData, library_ids: Collection[int]) -> dict:
    """Felder für eine neue ``Folder``-Zeile."""
    raw_path = str(form.get("path", "")).strip()
    if not raw_path.startswith("/"):
        raise FormError("Bitte einen absoluten Pfad angeben (beginnt mit /)")
    role = _choice(form, "role", ROLES, "Rolle", "input")
    return {"path": posixpath.normpath(raw_path), "role": role,
            **parse_folder_options(form, role, library_ids)}


def _folder_key(path: str) -> PurePosixPath:
    """Vergleichsform eines Ordnerpfads: normalisiert und ohne Groß-/Kleinschreibung (SMB)."""
    return PurePosixPath(casefold(posixpath.normpath(path)))


def overlap_error(path: str, others: Iterable[str]) -> str | None:
    """Meldung, wenn ``path`` in einem der ``others`` liegt oder einen davon enthält (Abschlussprüfung I4)."""
    mine = _folder_key(path)
    for other in others:
        theirs = _folder_key(other)
        if mine == theirs or mine.is_relative_to(theirs) or theirs.is_relative_to(mine):
            return (f"Der Ordner {path} überschneidet sich mit dem eingetragenen Ordner {other} – "
                    "Eingangs- und Bibliotheksordner dürfen nicht ineinander liegen.")
    return None


def _int_between(raw: str, label: str, minimum: int, maximum: int) -> int:
    number = int(raw) if raw.isascii() and raw.isdigit() else None
    if number is None or not minimum <= number <= maximum:
        raise FormError(f"„{label}“ muss eine ganze Zahl von {minimum} bis {maximum} sein")
    return number


def parse_correction_form(form: FormData) -> dict:
    """Korrekturangaben für ``replan_item``; leere Felder bleiben unverändert (``None``).

    Ein Suchtreffer kommt als ``pick`` (``tmdb:movie:603``) und hat Vorrang vor dem Freitextfeld.
    """
    raw_ref = str(form.get("pick", "")).strip() or str(form.get("reference", "")).strip()
    tmdb_ref = None
    if raw_ref:
        tmdb_ref = TmdbClient.parse_reference(raw_ref)
        if tmdb_ref is None:
            raise FormError(BAD_REFERENCE)
    raw_season = str(form.get("season", "")).strip()
    season = _int_between(raw_season, "Staffel", 0, MAX_SEASON) if raw_season else None
    episodes = None
    raw_episodes = str(form.get("episodes", "")).strip()
    if raw_episodes:
        parts = [p.strip() for p in raw_episodes.split(",") if p.strip()]
        if not parts or not all(p.isascii() and p.isdigit() and 1 <= int(p) <= MAX_EPISODE for p in parts):
            raise FormError(f"„Folgen“ bitte als Nummern von 1 bis {MAX_EPISODE} mit Komma angeben (z. B. 1, 2)")
        episodes = sorted({int(p) for p in parts})
    format_3d = str(form.get("format_3d", "")).strip() or None
    if format_3d is not None and format_3d not in FORMATS_3D:
        raise FormError(f"Ungültiges 3D-Format: {format_3d}")
    return {"tmdb_ref": tmdb_ref, "season": season, "episodes": episodes, "format_3d": format_3d}
