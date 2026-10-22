"""Gemeinsame Datentypen von mediasort."""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any


class MediaKind(StrEnum):
    MOVIE = "movie"
    EPISODE = "episode"


class FolderType(StrEnum):
    AUTO = "auto"
    MOVIE = "movie"
    SERIES = "series"
    MOVIE3D = "3d"


class Confidence(StrEnum):
    HIGH = "hoch"
    MEDIUM = "mittel"
    LOW = "niedrig"


class ItemStatus(StrEnum):
    NEW = "new"
    CONFIRMED = "confirmed"
    IGNORED = "ignored"
    DONE = "done"
    ERROR = "error"


def forbidden_decisions(conflict: Mapping[str, Any] | None) -> frozenset[str]:
    """Entscheidungen, bei denen eine Folge verloren ginge (Spec 7.2, Abschlussprüfung R1).

    ``partial_existing``: eine vorhandene Datei enthält Folgen, die der neuen fehlen – kein ``keep_new``.
    ``partial_new``: die neue Datei enthält Folgen, die einer vorhandenen fehlen – kein ``keep_existing``.
    Ältere Einträge kennen nur ``partial`` ohne Richtung: dann ist beides gesperrt.
    """
    conflict = conflict or {}
    if "partial_existing" in conflict or "partial_new" in conflict:
        existing_more, new_more = bool(conflict.get("partial_existing")), bool(conflict.get("partial_new"))
    else:
        existing_more = new_more = bool(conflict.get("partial"))
    return frozenset(d for d, refused in (("keep_new", existing_more), ("keep_existing", new_more)) if refused)


class AutoLevel(StrEnum):
    OFF = "off"
    DETECT = "detect"
    AUTO = "auto"


@dataclass(frozen=True)
class ParsedName:
    kind: MediaKind | None
    title: str | None
    year: int | None
    season: int | None
    episodes: tuple[int, ...]
    languages: tuple[str, ...]
    is_3d: bool
    format_3d: str | None
    is_sample: bool


@dataclass(frozen=True)
class AudioStream:
    codec: str
    profile: str | None
    channels: int
    language: str | None  # zweistellig klein oder None
    default: bool


@dataclass(frozen=True)
class MediaInfo:
    width: int
    height: int
    video_codec: str  # roher ffprobe-Name
    hdr: str | None  # "DV", "HDR" oder None
    audio: tuple[AudioStream, ...]


@dataclass(frozen=True)
class Subtitle:
    path: Path
    language: str | None
    forced: bool


@dataclass(frozen=True)
class VideoFile:
    path: Path
    size: int
    mtime: float
    root: Path
    subtitles: tuple[Subtitle, ...]


@dataclass(frozen=True)
class Candidate:
    tmdb_id: int
    kind: MediaKind
    title: str
    original_title: str
    english_title: str | None
    year: int | None
    popularity: float


@dataclass(frozen=True)
class Match:
    candidate: Candidate | None
    confidence: Confidence
    alternatives: tuple[Candidate, ...]
    score: float
