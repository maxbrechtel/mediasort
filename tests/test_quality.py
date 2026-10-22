import pytest
from conftest import A, MI

from mediasort.quality import (
    audio_codec_label,
    audio_rank,
    better,
    languages_tag,
    pick_audio,
    quality_key,
    resolution_label,
    video_codec_label,
)


@pytest.mark.parametrize("w,h,label", [
    (3840, 2160, "2160p"), (3840, 1600, "2160p"), (1920, 800, "1080p"), (1920, 1080, "1080p"),
    (1280, 720, "720p"), (1280, 536, "720p"), (1024, 576, "576p"), (720, 576, "576p"), (720, 480, "480p"),
])
def test_resolution_label(w, h, label):
    assert resolution_label(w, h) == label


@pytest.mark.parametrize("codec,label", [("h264", "h264"), ("hevc", "h265"), ("av1", "av1"), ("vp9", "vp9"),
    ("mpeg2video", "mpeg2"), ("mpeg4", "xvid"), ("vc1", "vc1")])
def test_video_codec_label(codec, label):
    assert video_codec_label(codec) == label


@pytest.mark.parametrize("codec,profile,label", [("truehd", None, "TrueHD"), ("dts", "DTS-HD MA", "DTS"),
    ("dts", None, "DTS"), ("eac3", None, "EAC3"), ("ac3", None, "AC3"), ("flac", None, "FLAC"),
    ("aac", "LC", "AAC"), ("opus", None, "Opus"), ("mp3", None, "MP3"), ("pcm_s16le", None, "PCM_S16LE")])
def test_audio_codec_label(codec, profile, label):
    assert audio_codec_label(codec, profile) == label


def test_audio_rank_orders_codec_then_channels():
    assert audio_rank(A("truehd", "en", 2)) > audio_rank(A("dts", "en", 8))
    assert audio_rank(A("ac3", "en", 6)) > audio_rank(A("ac3", "en", 2))
    assert audio_rank(A("pcm_s16le", "en", 2))[0] == 0


def test_pick_audio_prefers_best_german():
    s = [A("truehd", "en", 8, default=True), A("ac3", "de", 6), A("dts", "de", 6)]
    assert pick_audio(s).codec == "dts"


def test_pick_audio_more_channels_wins_tie():
    s = [A("ac3", "de", 2), A("ac3", "de", 6)]
    assert pick_audio(s).channels == 6


def test_pick_audio_without_german_takes_default_then_first():
    assert pick_audio([A("aac", "en", 2), A("ac3", "fr", 6, default=True)]).language == "fr"
    assert pick_audio([A("aac", "en", 2), A("ac3", "fr", 6)]).language == "en"


def test_pick_audio_empty_is_none():
    assert pick_audio([]) is None


def test_languages_tag():
    assert languages_tag([A("ac3", "en", 6), A("dts", "de", 6), A("ac3", "de", 2), A("aac", None, 2)], ()) == "[DE+EN]"
    assert languages_tag([A("aac", None, 2)], ("de", "en")) == "[DE+EN]"
    assert languages_tag([A("aac", None, 2)], ()) == ""
    assert languages_tag([A("aac", "fr", 2), A("aac", "en", 2)], ()) == "[EN+FR]"


def test_languages_tag_stream_languages_win_over_fallback():
    assert languages_tag([A("aac", "fr", 2)], ("de", "en")) == "[FR]"


def test_quality_key_none_without_info():
    assert quality_key(None, 5) is None


def test_better_prefers_resolution_then_hdr_then_codec():
    uhd = MI(3840, 2160, "hevc", None, [A("ac3", "de", 6)])
    fhd_dv = MI(1920, 1080, "hevc", "DV", [A("truehd", "de", 8)])
    assert better((uhd, 10), (fhd_dv, 99)) == "a"
    dv = MI(3840, 2160, "hevc", "DV", [A("ac3", "de", 6)])
    assert better((uhd, 10), (dv, 10)) == "b"
    av1 = MI(3840, 2160, "av1", None, [A("ac3", "de", 6)])
    assert better((av1, 10), (uhd, 10)) is None      # av1 und h265 gleichwertig
    assert better((av1, 10), (uhd, 11)) == "b"       # dann entscheidet die Größe


def test_better_german_audio_beats_no_german():
    de = MI(1920, 1080, "h264", None, [A("ac3", "de", 6)])
    en = MI(1920, 1080, "h264", None, [A("truehd", "en", 8)])
    assert better((de, 1), (en, 2)) == "a"


def test_better_unknown_info_gives_no_suggestion():
    assert better((None, 5), (MI(1920, 1080, "h264", None, []), 1)) is None
    assert better((MI(1920, 1080, "h264", None, []), 1), (None, 5)) is None
