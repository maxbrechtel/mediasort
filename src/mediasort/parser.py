"""Datei- und Ordnernamen in ein ``ParsedName`` zerlegen (Spec 5.2, 5.3)."""

import re
import unicodedata
from typing import Any

from guessit import guessit

from mediasort.langs import to_alpha2
from mediasort.models import MediaKind, ParsedName

_SEASON_RE = re.compile(r"(?<![A-Za-z0-9])(?:Staffel|St\.?)\s*0*(\d{1,2})(?!\d)", re.IGNORECASE)
_EPISODE_RE = re.compile(
    r"[\s._-]*(?<![A-Za-z0-9])(?:Folge|Episode)\s*0*(\d{1,3})(?!\d)", re.IGNORECASE
)
_FORMAT_3D_RE = re.compile(r"\b(TAB|MVC)\b", re.IGNORECASE)
_SAMPLE_RE = re.compile(r"\bsample\b", re.IGNORECASE)
_GARBAGE_RE = re.compile(r"^[a-z0-9]{8,}$")
_GUESSIT_3D_FORMATS = {
    "Half Side By Side": "HSBS",
    "Side By Side": "SBS",
    "Half Over Under": "HOU",
    "Over Under": "OU",
}


def normalize_german(name: str) -> str:
    """Übersetzt ``Staffel 3``/``St3`` und ``Folge 1``/``Episode 1`` in ``SxxEyy``."""
    season = _SEASON_RE.search(name)
    episode = _EPISODE_RE.search(name)
    if season is None and episode is None:
        return name
    season_no = int(season.group(1)) if season else None
    episode_no = int(episode.group(1)) if episode else None
    code = ""
    if season_no is not None:
        code += f"S{season_no:02d}"
    if episode_no is not None:
        code += f"E{episode_no:02d}"
    # Von hinten nach vorn ersetzen, damit die Positionen des ersten Treffers stimmen.
    spans = sorted(
        [(m.start(), m.end()) for m in (season, episode) if m is not None], reverse=True
    )
    anchor = season if season is not None else episode
    for start, end in spans:
        if start == anchor.start():
            lead = " " if season is None else ""
            name = name[:start] + lead + code + name[end:]
        else:
            name = name[:start] + name[end:]
    return name.strip()


def normalize_title(title: str) -> str:
    """Für Gruppierung und Vergleiche: ohne Akzente, klein, nur ``[a-z0-9 ]``."""
    decomposed = unicodedata.normalize("NFKD", title)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    cleaned = re.sub(r"[^a-z0-9]+", " ", stripped.casefold())
    return cleaned.strip()


def is_garbage_title(title: str | None) -> bool:
    """Unbrauchbar: fehlt, kürzer als 3 Zeichen oder zufällige Kennung wie ``y2x24go2wrcp``."""
    if title is None or len(title) < 3:
        return True
    return bool(_GARBAGE_RE.match(title)) and sum(ch.isdigit() for ch in title) >= 2


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


def _first(value: Any) -> Any:
    items = _as_list(value)
    return items[0] if items else None


def _guess(name: str) -> dict[str, Any]:
    return dict(guessit(normalize_german(name)))


def _title(guess: dict[str, Any]) -> str | None:
    title = _first(guess.get("title"))
    if title is None:
        return None
    alternative = _first(guess.get("alternative_title"))
    return f"{title} {alternative}" if alternative else str(title)


def _languages(guess: dict[str, Any]) -> tuple[str, ...]:
    codes: list[str] = []
    multi = False
    for language in _as_list(guess.get("language")):
        alpha3 = getattr(language, "alpha3", str(language))
        if alpha3 == "mul":
            multi = True
            continue
        alpha2 = to_alpha2(alpha3)
        if alpha2 and alpha2 not in codes:
            codes.append(alpha2)
    if multi and "de" in codes and "en" not in codes:
        codes.append("en")
    return tuple(codes)


def _season(guess: dict[str, Any], year: int | None) -> int | None:
    """Staffel laut guessit; ein Jahr, das als Staffel erkannt wurde, ergibt Staffel 1."""
    season = _first(guess.get("season"))
    if season is not None and season == year and season >= 1900:
        return 1
    return season


def _episodes(guess: dict[str, Any]) -> tuple[int, ...]:
    return tuple(int(e) for e in _as_list(guess.get("episode")))


def _format_3d(others: list[Any], filename: str) -> str | None:
    for guessit_name, short in _GUESSIT_3D_FORMATS.items():
        if guessit_name in others:
            return short
    match = _FORMAT_3D_RE.search(filename)
    return match.group(1).upper() if match else None


def _contains_words(haystack: str, needle: str) -> bool:
    return bool(needle) and f" {needle} " in f" {haystack} "


def parse_name(filename: str, parent: str | None = None) -> ParsedName:
    """Zerlegt einen Dateinamen; ``parent`` ist der Name des übergeordneten Ordners.

    Der Ordner wird zusammengeführt: Felder aus dem Dateinamen gewinnen immer.
    """
    guess = _guess(filename)
    others = _as_list(guess.get("other"))
    title = _title(guess)
    year = _first(guess.get("year"))
    languages = _languages(guess)
    season = _season(guess, year)
    episodes = _episodes(guess)
    is_3d = "3D" in others
    format_3d = _format_3d(others, filename) if is_3d else None

    if parent:
        parent_guess = _guess(parent)
        parent_others = _as_list(parent_guess.get("other"))
        parent_title = _title(parent_guess)
        parent_year = _first(parent_guess.get("year"))
        garbage = is_garbage_title(title)
        related = garbage or (
            parent_title is not None
            and _contains_words(normalize_title(parent_title), normalize_title(title or ""))
        )
        if garbage and parent_title is not None:
            title = parent_title
        if related:
            year = year or parent_year
            languages = languages or _languages(parent_guess)
            season = season if season is not None else _season(parent_guess, parent_year)
        if garbage:
            episodes = episodes or _episodes(parent_guess)
            if not is_3d and "3D" in parent_others:
                is_3d = True
                format_3d = _format_3d(parent_others, parent)

    if season is None and episodes:
        season = 1

    return ParsedName(
        kind=MediaKind.EPISODE if season is not None or episodes else MediaKind.MOVIE,
        title=title,
        year=year,
        season=season,
        episodes=episodes,
        languages=languages,
        is_3d=is_3d,
        format_3d=format_3d,
        is_sample="Sample" in others or bool(_SAMPLE_RE.search(filename)),
    )
