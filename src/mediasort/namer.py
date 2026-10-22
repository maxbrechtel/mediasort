"""Zielnamen nach Schema (Spec 6): reine Funktionen ohne Dateisystemzugriff."""

import re
import unicodedata
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

from mediasort.models import MediaInfo, Subtitle
from mediasort.quality import (
    audio_codec_label,
    languages_tag,
    pick_audio,
    resolution_label,
    video_codec_label,
)

MAX_NAME_BYTES = 255

_REMOVED = re.compile(r'[?*"<>|]')
_SPACES = re.compile(r"\s+")
_CUT_TRAILING = " .-&"


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def sanitize(name: str) -> str:
    """Dateisystemtaugliche Schreibweise eines Namensteils (NFC)."""
    name = _nfc(name).replace(":", " -").replace("/", "-").replace("\\", "-")
    name = _REMOVED.sub("", name)
    name = _SPACES.sub(" ", name).strip()
    return name.rstrip(". ")


def _size(text: str) -> int:
    return len(text.encode("utf-8"))


def _cut(text: str, max_bytes: int) -> str:
    """Kürzt ``text`` am Zeichen auf höchstens ``max_bytes`` UTF-8-Bytes (ohne Auslassungszeichen)."""
    if max_bytes <= 0:
        return ""
    used = 0
    end = 0
    for char in text:
        used += _size(char)
        if used > max_bytes:
            break
        end += 1
    return text[:end].rstrip(_CUT_TRAILING)


def _year_part(year: int | None) -> str:
    return f" ({year})" if year else ""


def _stem_with_title(title: str, year: int | None, tail: str, ext: str) -> str:
    """Dateiname ``<Titel>[ (Jahr)]<tail>.<ext>``; der Titel wird gekürzt, bis alles passt."""
    suffix = f"{_year_part(year)}{tail}.{ext}"
    title = _cut(title, MAX_NAME_BYTES - _size(suffix)) if _size(title) + _size(suffix) > MAX_NAME_BYTES else title
    return f"{title}{suffix}"


def _folder_name(title: str, year: int | None) -> str:
    suffix = _year_part(year)
    if _size(title) + _size(suffix) > MAX_NAME_BYTES:
        title = _cut(title, MAX_NAME_BYTES - _size(suffix))
    return f"{title}{suffix}"


def _clean_ext(ext: str) -> str:
    return ext.lstrip(".")


def movie_relpath(
    title: str,
    year: int | None,
    ext: str,
    info: MediaInfo | None,
    fallback_langs: Sequence[str] = (),
    format_3d: str | None = None,
    is_3d: bool = False,
) -> PurePosixPath:
    title = sanitize(title)
    ext = _clean_ext(ext)
    parts: list[str] = []
    if info is not None:
        parts.append(resolution_label(info.width, info.height))
        parts.append(video_codec_label(info.video_codec))
        if info.hdr:
            parts.append(info.hdr)
        audio = pick_audio(info.audio)
        if audio is not None:
            parts.append(audio_codec_label(audio.codec, audio.profile))
    if is_3d or format_3d:
        parts.append("3D")
        if format_3d:
            parts.append(sanitize(format_3d))
    if info is not None:
        tag = languages_tag(info.audio, fallback_langs)
        if tag:
            parts.append(tag)
    tail = "".join(f" {p}" for p in parts)
    return PurePosixPath(_folder_name(title, year)) / _stem_with_title(title, year, tail, ext)


def episode_relpath(
    show: str,
    year: int | None,
    season: int,
    episodes: Sequence[int],
    episode_titles: Sequence[str],
    ext: str,
    info: MediaInfo | None,
    fallback_langs: Sequence[str] = (),
    season_label: str = "Staffel",
) -> PurePosixPath:
    if not episodes:
        raise ValueError("Mindestens eine Folgennummer erforderlich")
    show = sanitize(show)
    ext = _clean_ext(ext)
    numbers = f"S{season:02d}E{episodes[0]:02d}"
    if len(episodes) > 1:
        numbers += f"-E{episodes[-1]:02d}"
    ep_title = " & ".join(t for t in (sanitize(t) for t in episode_titles) if t)

    tail_parts: list[str] = []
    if info is not None:
        tail_parts.append(f" - {resolution_label(info.width, info.height)}")
        tag = languages_tag(info.audio, fallback_langs)
        if tag:
            tail_parts.append(f" {tag}")
    tail = "".join(tail_parts)

    def build(show_part: str, title_part: str) -> str:
        middle = f" - {title_part}" if title_part else ""
        return f"{show_part} - {numbers}{middle}{tail}.{ext}"

    name = build(show, ep_title)
    if _size(name) > MAX_NAME_BYTES:
        # 1. Folgenname kürzen (soweit er neben Serie, Nummer und Rest Platz hat)
        room = MAX_NAME_BYTES - _size(build(show, "")) - _size(" - ")
        ep_title = _cut(ep_title, room)
        name = build(show, ep_title)
    if _size(name) > MAX_NAME_BYTES:
        # 2. Serientitel im Dateinamen kürzen
        room = MAX_NAME_BYTES - _size(build("", ep_title))
        name = build(_cut(show, room), ep_title)

    season_dir = f"{season_label} {season}"
    return PurePosixPath(_folder_name(show, year)) / season_dir / name


_PAIR_EXTS = {".idx", ".sub"}


def subtitle_targets(video_target: Path, subs: Sequence[Subtitle]) -> list[tuple[Subtitle, Path]]:
    """Zielpfade der Untertitel neben dem Video, in Eingabereihenfolge.

    Pro ``(Sprache, forced, Endung)`` wird ab der zweiten Datei durchnummeriert;
    ``.idx``/``.sub`` mit gleichem Stamm bilden eine Einheit und teilen die Nummer.
    """
    stem = video_target.name[: -len(video_target.suffix)] if video_target.suffix else video_target.name
    units: dict[tuple, list[Path]] = {}
    result: list[tuple[Subtitle, Path]] = []
    for sub in subs:
        ext = sub.path.suffix.lower()
        if ext in _PAIR_EXTS:
            group, unit = (sub.language, sub.forced, "idx/sub"), sub.path.with_suffix("")
        else:
            group, unit = (sub.language, sub.forced, ext), sub.path
        seen = units.setdefault(group, [])
        if unit not in seen:
            seen.append(unit)
        number = seen.index(unit) + 1
        name = stem
        if sub.language:
            name += f".{sub.language}"
        if sub.forced:
            name += ".forced"
        if number > 1:
            name += f".{number}"
        result.append((sub, video_target.with_name(name + ext)))
    return result
