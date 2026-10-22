"""Mediendaten einer Videodatei über ffprobe auslesen."""

import json
import subprocess
from pathlib import Path

from mediasort.langs import to_alpha2
from mediasort.models import AudioStream, MediaInfo

_HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}


class ProbeError(Exception):
    """ffprobe konnte die Datei nicht auswerten."""


def run_ffprobe(path: Path, timeout: float = 60) -> dict:
    cmd = ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise ProbeError(f"ffprobe hat zu lange gebraucht (mehr als {timeout:g} s): {path}") from exc
    except OSError as exc:
        raise ProbeError(f"ffprobe konnte nicht gestartet werden: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or f"Exit-Code {result.returncode}"
        raise ProbeError(f"ffprobe konnte die Datei nicht lesen: {detail}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ProbeError(f"ffprobe lieferte keine gültige Ausgabe: {exc}") from exc


def _is_attached_pic(stream: dict) -> bool:
    return stream.get("disposition", {}).get("attached_pic") == 1


def _hdr(stream: dict) -> str | None:
    side_data = stream.get("side_data_list") or []
    if any("dovi" in str(entry.get("side_data_type", "")).lower() for entry in side_data):
        return "DV"
    if stream.get("color_transfer") in _HDR_TRANSFERS:
        return "HDR"
    return None


def _audio(stream: dict) -> AudioStream:
    language = stream.get("tags", {}).get("language")
    return AudioStream(
        codec=stream.get("codec_name", ""),
        profile=stream.get("profile"),
        channels=int(stream.get("channels") or 0),
        language=to_alpha2(language) if language else None,
        default=stream.get("disposition", {}).get("default") == 1,
    )


def parse_probe(data: dict) -> MediaInfo:
    streams = data.get("streams") or []
    video = next(
        (s for s in streams if s.get("codec_type") == "video" and not _is_attached_pic(s)),
        None,
    )
    if video is None:
        raise ProbeError("Die Datei enthält keinen Videostream.")
    audio = tuple(_audio(s) for s in streams if s.get("codec_type") == "audio")
    return MediaInfo(
        width=int(video.get("width") or 0),
        height=int(video.get("height") or 0),
        video_codec=video.get("codec_name", ""),
        hdr=_hdr(video),
        audio=audio,
    )


def probe(path: Path) -> MediaInfo | None:
    """Liest die Mediendaten; ISO-Abbilder werden nicht untersucht (``None``)."""
    if path.suffix.lower() == ".iso":
        return None
    return parse_probe(run_ffprobe(path))
