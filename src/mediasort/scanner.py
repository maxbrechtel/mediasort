"""Eingangsordner durchsuchen: Videos, Untertitel, Samples, zu neue Dateien."""

import os
import re
import unicodedata
from collections.abc import Callable, Container, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from mediasort.langs import to_alpha2
from mediasort.models import Subtitle, VideoFile

VIDEO_EXT = frozenset({"mkv", "mp4", "m4v", "avi", "mov", "wmv", "ts", "m2ts", "iso"})
SUB_EXT = frozenset({"srt", "ass", "ssa", "sub", "idx", "sup", "vtt"})

_SAMPLE_RE = re.compile(r"(?<![A-Za-z0-9])sample(?![A-Za-z0-9])", re.IGNORECASE)
_NAME_SPLIT_RE = re.compile(r"[._\s]+")
_SUB_DIRS = frozenset({"subs", "subtitles"})


@dataclass
class ScanResult:
    videos: list[VideoFile] = field(default_factory=list)
    samples: list[Path] = field(default_factory=list)
    too_new: list[Path] = field(default_factory=list)


def _ext(name: str) -> str:
    return name.rpartition(".")[2].lower() if "." in name else ""


def subtitle_from_path(path: Path) -> Subtitle:
    """Sprache aus den Namensteilen (letzter zuerst), ``forced`` wenn ein Teil so heißt."""
    parts = [p for p in _NAME_SPLIT_RE.split(path.stem) if p]
    forced = any(p.lower() == "forced" for p in parts)
    language = None
    for part in reversed(parts):
        if part.lower() == "forced":
            continue
        language = to_alpha2(part)
        if language:
            break
    return Subtitle(path=path, language=language, forced=forced)


def _letter_key(name: str) -> str:
    folded = unicodedata.normalize("NFKD", name[:1].casefold())
    letter = folded[:1]
    if "a" <= letter <= "z":
        return letter.upper()
    return "0"


def letter_range(first: str, last: str) -> Callable[[str], bool]:
    """Filter für oberste Einträge: erster Buchstabe zwischen ``first`` und ``last``.

    Ziffern und alles, was kein Buchstabe ist, zählen als ``0``; Umlaute werden gefaltet.
    """
    lo, hi = _letter_key(first), _letter_key(last)
    return lambda name: lo <= _letter_key(name) <= hi


def _subtitle_files(directory: str, names: Sequence[str], subdirs: Sequence[str]) -> list[Path]:
    found = [Path(directory, n) for n in names if _ext(n) in SUB_EXT]
    for sub in subdirs:
        if sub.lower() not in _SUB_DIRS:
            continue
        try:
            entries = sorted(os.listdir(Path(directory, sub)))
        except OSError:
            continue
        found += [Path(directory, sub, n) for n in entries
                  if _ext(n) in SUB_EXT and not n.startswith(".")]
    return found


def scan(
    root: Path,
    *,
    exclude: Sequence[str],
    sample_bytes: int,
    min_age_s: float,
    now: float,
    ignored: Container[str] = (),
    top_filter: Callable[[str], bool] | None = None,
) -> ScanResult:
    result = ScanResult()
    root = Path(root)
    excluded = set(exclude)
    for dirpath, dirnames, filenames in os.walk(root):
        at_top = Path(dirpath) == root
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in excluded and not d.startswith(".") and (not at_top or top_filter is None or top_filter(d))
        )
        names = sorted(
            n for n in filenames
            if not n.startswith(".") and (not at_top or top_filter is None or top_filter(n))
        )
        local: list[tuple[Path, int, float]] = []  # echte Videos dieses Ordners (ohne Samples)
        for name in names:
            if _ext(name) not in VIDEO_EXT:
                continue
            path = Path(dirpath, name)
            try:
                st = path.stat()
            except OSError:
                continue
            if _SAMPLE_RE.search(name) or st.st_size < sample_bytes:
                if str(path) not in ignored:
                    result.samples.append(path)
                continue
            if str(path) in ignored:
                local.append((path, st.st_size, -1.0))  # zählt nur für die Untertitelzuordnung
            elif now - st.st_mtime < min_age_s:
                result.too_new.append(path)
                local.append((path, st.st_size, -1.0))
            else:
                local.append((path, st.st_size, st.st_mtime))
        if not local:
            continue
        subs = [subtitle_from_path(p) for p in _subtitle_files(dirpath, names, dirnames)]
        for path, size, mtime in local:
            if mtime < 0:
                continue
            stem = path.stem.lower()
            if len(local) == 1:
                mine = subs
            else:
                mine = [s for s in subs if s.path.name.lower().startswith(stem)]
            result.videos.append(
                VideoFile(
                    path=path, size=size, mtime=mtime, root=root,
                    subtitles=tuple(sorted(mine, key=lambda s: s.path)),
                )
            )
    result.videos.sort(key=lambda v: v.path)
    result.samples.sort()
    result.too_new.sort()
    return result
