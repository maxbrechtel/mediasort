"""Gemeinsame Test-Helfer (aus Tests importierbar: ``from conftest import A, MI``)."""

from mediasort.models import AudioStream, MediaInfo


def A(codec, language, channels, default=False, profile=None) -> AudioStream:
    return AudioStream(codec=codec, profile=profile, channels=channels, language=language, default=default)


def MI(width, height, video_codec, hdr, audio_list) -> MediaInfo:
    return MediaInfo(width=width, height=height, video_codec=video_codec, hdr=hdr, audio=tuple(audio_list))
