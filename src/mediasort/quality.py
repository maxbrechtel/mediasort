"""Qualität: Labels für Dateinamen, Tonspurwahl, Sprachkürzel und Qualitätsvergleich."""

from collections.abc import Sequence
from typing import Literal

from mediasort.langs import to_alpha2
from mediasort.models import AudioStream, MediaInfo

_VIDEO_LABELS = {
    "h264": "h264",
    "avc": "h264",
    "hevc": "h265",
    "av1": "av1",
    "vp9": "vp9",
    "mpeg2video": "mpeg2",
    "mpeg4": "xvid",
}

_AUDIO_LABELS = {
    "truehd": "TrueHD",
    "dts": "DTS",
    "eac3": "EAC3",
    "ac3": "AC3",
    "flac": "FLAC",
    "aac": "AAC",
    "opus": "Opus",
    "mp3": "MP3",
}

_AUDIO_RANKS = {"truehd": 8, "dts": 7, "eac3": 6, "ac3": 5, "flac": 4, "aac": 3, "opus": 2, "mp3": 1}

_RESOLUTION_LEVELS = {"2160p": 4, "1080p": 3, "720p": 2, "576p": 1, "480p": 0}
_HDR_LEVELS = {"DV": 2, "HDR": 1}
_CODEC_LEVELS = {"av1": 2, "h265": 2, "h264": 1}


def resolution_label(width: int, height: int) -> str:
    """Auflösung vorrangig nach Bildbreite (Kinofilme sind oft beschnitten)."""
    if width >= 3200:
        return "2160p"
    if width >= 1800:
        return "1080p"
    if width >= 1200:
        return "720p"
    if width >= 900 or height >= 560:
        return "576p"
    return "480p"


def video_codec_label(codec: str) -> str:
    codec = codec.lower()
    return _VIDEO_LABELS.get(codec, codec)


def audio_codec_label(codec: str, profile: str | None) -> str:
    """Schreibweise der Tonspur; ``profile`` (z. B. DTS-HD MA) ändert das Label nicht."""
    codec = codec.lower()
    return _AUDIO_LABELS.get(codec, codec.upper())


def audio_rank(stream: AudioStream) -> tuple[int, int]:
    """Rang einer Tonspur: erst Codec, dann Kanalzahl (höher = besser)."""
    return (_AUDIO_RANKS.get(stream.codec.lower(), 0), stream.channels)


def pick_audio(streams: Sequence[AudioStream]) -> AudioStream | None:
    """Deutsche Spuren, sonst Standardspur, sonst erste Spur; darunter die beste nach Rang."""
    if not streams:
        return None
    german = [s for s in streams if s.language == "de"]
    if german:
        return max(german, key=audio_rank)
    for stream in streams:
        if stream.default:
            return stream
    return streams[0]


def languages_tag(streams: Sequence[AudioStream], fallback: Sequence[str]) -> str:
    """``"[DE+EN]"``: Deutsch zuerst, Rest alphabetisch; ohne Sprachen ``""``."""
    codes = {s.language for s in streams if s.language}
    if not codes:
        codes = {c for c in (to_alpha2(f) for f in fallback) if c}
    if not codes:
        return ""
    ordered = sorted(codes, key=lambda c: (c != "de", c))
    return "[" + "+".join(c.upper() for c in ordered) + "]"


def short_label(info: MediaInfo) -> str:
    """Kurzbeschreibung wie im Dateinamen, z. B. ``"2160p h265 DV TrueHD [DE+EN]"``."""
    parts = [resolution_label(info.width, info.height), video_codec_label(info.video_codec)]
    if info.hdr:
        parts.append(info.hdr)
    audio = pick_audio(info.audio)
    if audio is not None:
        parts.append(audio_codec_label(audio.codec, audio.profile))
    tag = languages_tag(info.audio, ())
    if tag:
        parts.append(tag)
    return " ".join(parts)


def quality_key(info: MediaInfo | None, size: int) -> tuple | None:
    """Sortierschlüssel für den Qualitätsvergleich (größer = besser); ``None`` ohne MediaInfo."""
    if info is None:
        return None
    picked = pick_audio(info.audio)
    return (
        _RESOLUTION_LEVELS[resolution_label(info.width, info.height)],
        _HDR_LEVELS.get(info.hdr or "", 0),
        _CODEC_LEVELS.get(video_codec_label(info.video_codec), 0),
        any(s.language == "de" for s in info.audio),
        audio_rank(picked) if picked else (0, 0),
        size,
    )


def better(
    a: tuple[MediaInfo | None, int], b: tuple[MediaInfo | None, int]
) -> Literal["a", "b"] | None:
    """Welche Version ist besser? ``None`` bei Gleichstand oder fehlenden technischen Angaben."""
    key_a = quality_key(*a)
    key_b = quality_key(*b)
    if key_a is None or key_b is None or key_a == key_b:
        return None
    return "a" if key_a > key_b else "b"
