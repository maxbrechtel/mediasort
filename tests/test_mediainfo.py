import json
import shutil
import subprocess
from pathlib import Path

import pytest

from mediasort.mediainfo import ProbeError, parse_probe, probe, run_ffprobe

FIXTURES = Path(__file__).parent / "fixtures" / "ffprobe"


def load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def test_parse_dv():
    mi = parse_probe(load("dv_truehd"))
    assert (mi.width, mi.hdr, mi.video_codec) == (3840, "DV", "hevc")
    assert mi.height == 1608
    assert [(a.codec, a.language, a.default, a.channels) for a in mi.audio] == [
        ("truehd", "de", True, 8),
        ("ac3", "en", False, 6),
    ]


def test_parse_hdr10():
    assert parse_probe(load("hdr10")).hdr == "HDR"


def test_parse_hlg():
    d = load("hdr10")
    d["streams"][0]["color_transfer"] = "arib-std-b67"
    assert parse_probe(d).hdr == "HDR"


def test_parse_sdr_multi_audio():
    mi = parse_probe(load("sdr_multi"))
    assert mi.hdr is None and mi.width == 1920
    assert mi.video_codec == "h264"
    assert [(a.codec, a.language, a.default) for a in mi.audio] == [
        ("ac3", "en", True),
        ("dts", "de", False),
        ("aac", None, False),
    ]
    assert mi.audio[1].profile == "DTS-HD MA"
    assert mi.audio[0].profile is None


def test_missing_channels_is_zero():
    d = load("hdr10")
    del d["streams"][1]["channels"]
    assert parse_probe(d).audio[0].channels == 0


def test_no_video_stream_raises():
    d = load("sdr_multi")
    d["streams"] = [s for s in d["streams"] if s["codec_type"] != "video"]
    with pytest.raises(ProbeError):
        parse_probe(d)


def test_only_cover_art_raises():
    d = load("sdr_multi")
    d["streams"] = [s for s in d["streams"] if s["codec_type"] == "video" and s["disposition"]["attached_pic"]]
    with pytest.raises(ProbeError):
        parse_probe(d)


def test_iso_is_not_probed(tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("ffprobe darf nicht aufgerufen werden")

    monkeypatch.setattr(subprocess, "run", boom)
    p = tmp_path / "x.iso"
    p.write_bytes(b"0")
    assert probe(p) is None
    assert probe(tmp_path / "Y.ISO") is None


@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="ffprobe fehlt")
def test_broken_file_raises(tmp_path):
    p = tmp_path / "x.mkv"
    p.write_bytes(b"kaputt")
    with pytest.raises(ProbeError, match="ffprobe konnte die Datei nicht lesen"):
        probe(p)


def test_timeout_raises(monkeypatch, tmp_path):
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 0))

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(ProbeError, match="zu lange"):
        run_ffprobe(tmp_path / "x.mkv")


def test_missing_ffprobe_raises(monkeypatch, tmp_path):
    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("ffprobe")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(ProbeError, match="ffprobe"):
        run_ffprobe(tmp_path / "x.mkv")


def test_invalid_json_raises(monkeypatch, tmp_path):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="kein json", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(ProbeError):
        run_ffprobe(tmp_path / "x.mkv")


@pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="ffmpeg fehlt")
def test_real_file_roundtrip(tmp_path):
    out = tmp_path / "out.mkv"
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=size=1920x800:duration=1",
            "-f", "lavfi", "-i", "sine=duration=1",
            "-f", "lavfi", "-i", "sine=duration=1",
            "-map", "0", "-map", "1", "-map", "2",
            "-c:v", "mpeg4", "-c:a:0", "ac3", "-c:a:1", "aac",
            "-metadata:s:a:0", "language=ger", "-metadata:s:a:1", "language=eng",
            "-disposition:a:0", "default", "-disposition:a:1", "0",
            str(out),
        ],
        check=True,
        capture_output=True,
    )
    mi = probe(out)
    assert mi.video_codec == "mpeg4" and [a.language for a in mi.audio] == ["de", "en"] and mi.audio[0].default
    assert (mi.width, mi.height) == (1920, 800)
    assert not mi.audio[1].default


def test_invalid_utf8_in_ffprobe_output_is_tolerated(tmp_path, monkeypatch):
    # Abschlussprüfung I1: ein kaputtes Metadaten-Tag darf nicht den ganzen Scan abbrechen
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "ffprobe"
    fake.write_bytes(
        b'#!/bin/sh\nprintf \'{"streams":[{"codec_type":"video","width":1920,"height":1080,'
        b'"codec_name":"h264","tags":{"title":"\\377\\376kaputt"}}]}\'\n'
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    video = tmp_path / "x.mkv"
    video.write_bytes(b"0")
    mi = probe(video)
    assert (mi.width, mi.video_codec) == (1920, "h264")
