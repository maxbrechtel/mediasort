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
_NAME_SPLIT_RE = re.compile(r"[._\s-]+")
_SEPARATORS = ".-_ "
_LANG_NOISE = frozenset({"sdh", "cc"})
_SUB_DIRS = frozenset({"subs", "subtitles"})


@dataclass
class ScanResult:
    videos: list[VideoFile] = field(default_factory=list)
    samples: list[Path] = field(default_factory=list)
    too_new: list[Path] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)  # deutsche Meldungen zu nicht lesbaren Pfaden


def _ext(name: str) -> str:
    return name.rpartition(".")[2].lower() if "." in name else ""


def subtitle_from_path(path: Path, video_stem: str | None = None) -> Subtitle:
    """Sprache und ``forced`` eines Untertitels aus dem Dateinamen.

    Beginnt der Name mit ``video_stem``, wird nur der Rest danach ausgewertet. Die Sprache
    steht am Ende des Namens: von hinten werden ``forced``, ``sdh``, ``cc`` und ein- bis
    zweistellige Zahlen übersprungen; das erste andere Wort entscheidet (kein Sprachcode: ``None``).
    """
    name = path.stem
    if video_stem and name.lower().startswith(video_stem.lower()):
        name = name[len(video_stem):]
    parts = [p for p in _NAME_SPLIT_RE.split(name) if p]
    forced = any(p.lower() == "forced" for p in parts)
    language = None
    for part in reversed(parts):
        low = part.lower()
        if low == "forced" or low in _LANG_NOISE or (part.isdigit() and len(part) <= 2):
            continue
        language = to_alpha2(part)
        break
    return Subtitle(path=path, language=language, forced=forced)


def is_sample_name(name: str) -> bool:
    """Dateiname enthält das Wort „sample“ (nicht als Teil eines längeren Wortes)."""
    return bool(_SAMPLE_RE.search(name))


def owns_subtitle(stem: str, subtitle_name: str) -> bool:
    """Untertitelname beginnt mit dem Videonamen, danach Trennzeichen oder Ende."""
    if not subtitle_name.lower().startswith(stem.lower()):
        return False
    rest = subtitle_name[len(stem):]
    return not rest or rest[0] in _SEPARATORS


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


def _subtitle_files(
    directory: str, names: Sequence[str], subdirs: Sequence[str], errors: list[str]
) -> list[Path]:
    found = [Path(directory, n) for n in names if _ext(n) in SUB_EXT]
    for sub in subdirs:
        if sub.lower() not in _SUB_DIRS:
            continue
        try:
            entries = sorted(os.listdir(Path(directory, sub)))
        except OSError:
            errors.append(f"Ordner nicht lesbar: {Path(directory, sub)}")
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

    def on_walk_error(exc: OSError) -> None:
        result.errors.append(f"Ordner nicht lesbar: {exc.filename}")

    for dirpath, dirnames, filenames in os.walk(root, onerror=on_walk_error):
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
                result.errors.append(f"Datei nicht lesbar: {path}")
                continue
            if is_sample_name(name) or st.st_size < sample_bytes:
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
        sub_paths = _subtitle_files(dirpath, names, dirnames, result.errors)
        for path, size, mtime in local:
            if mtime < 0:
                continue
            if len(local) == 1:
                mine = sub_paths
            else:
                mine = [p for p in sub_paths if owns_subtitle(path.stem, p.name)]
            result.videos.append(
                VideoFile(
                    path=path, size=size, mtime=mtime, root=root,
                    subtitles=tuple(sorted(
                        (subtitle_from_path(p, path.stem) for p in mine), key=lambda s: s.path
                    )),
                )
            )
    result.videos.sort(key=lambda v: v.path)
    result.samples.sort()
    result.too_new.sort()
    result.errors.sort()
    return result
