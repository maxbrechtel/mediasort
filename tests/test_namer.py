import unicodedata
from pathlib import Path

from conftest import MI, A

from mediasort.models import Subtitle
from mediasort.namer import (
    MAX_NAME_BYTES,
    MAX_VIDEO_NAME_BYTES,
    SUBTITLE_RESERVE,
    episode_relpath,
    movie_relpath,
    sanitize,
    subtitle_targets,
)


def test_sanitize():
    assert sanitize("Mission: Impossible") == "Mission - Impossible"
    assert sanitize('AC/DC: Live? "Best" <1>|*') == "AC-DC - Live Best 1"
    assert sanitize("Titel...  ") == "Titel"
    assert sanitize("A   B") == "A B"


def test_sanitize_backslash_and_nfc():
    assert sanitize("A\\B") == "A-B"
    assert unicodedata.is_normalized("NFC", sanitize(unicodedata.normalize("NFD", "Löwe")))


def test_movie_relpath():
    info = MI(1920, 800, "h264", None, [A("dts", "de", 6), A("ac3", "en", 6)])
    assert str(movie_relpath("28 Weeks Later", 2007, "mkv", info)) == \
        "28 Weeks Later (2007)/28 Weeks Later (2007) 1080p h264 DTS [DE+EN].mkv"


def test_movie_relpath_dv_and_3d():
    info = MI(1920, 1080, "h264", None, [A("dts", "de", 6), A("ac3", "en", 6)])
    assert movie_relpath("Avatar - Aufbruch nach Pandora", 2009, "mkv", info, is_3d=True, format_3d="SBS").name == \
        "Avatar - Aufbruch nach Pandora (2009) 1080p h264 DTS 3D SBS [DE+EN].mkv"
    dv = MI(3840, 1600, "hevc", "DV", [A("truehd", "de", 8), A("ac3", "en", 6)])
    assert movie_relpath("Dune", 2021, "mkv", dv).name == "Dune (2021) 2160p h265 DV TrueHD [DE+EN].mkv"


def test_movie_3d_without_format_and_without_audio_and_year():
    info = MI(1920, 1080, "h264", None, [A("dts", "de", 6)])
    assert movie_relpath("X", 2020, "mkv", info, is_3d=True).name == "X (2020) 1080p h264 DTS 3D [DE].mkv"
    assert movie_relpath("X", 2020, "mkv", MI(1920, 1080, "h264", None, [])).name == "X (2020) 1080p h264.mkv"
    assert str(movie_relpath("X", None, "mkv", info)) == "X/X 1080p h264 DTS [DE].mkv"


def test_movie_fallback_langs():
    info = MI(1920, 1080, "h264", None, [A("ac3", None, 6)])
    assert movie_relpath("X", 2020, "mkv", info, fallback_langs=["de", "en"]).name == \
        "X (2020) 1080p h264 AC3 [DE+EN].mkv"


def test_iso_without_info():
    assert str(movie_relpath("Star Wars - Das Erwachen der Macht", 2015, "iso", None, is_3d=True)) == \
        "Star Wars - Das Erwachen der Macht (2015)/Star Wars - Das Erwachen der Macht (2015) 3D.iso"
    assert movie_relpath("X", 2015, "iso", None, is_3d=True, format_3d="HSBS").name == "X (2015) 3D HSBS.iso"
    assert movie_relpath("X", 2015, "iso", None).name == "X (2015).iso"


def test_episode_relpath():
    info = MI(1920, 1080, "h264", None, [A("ac3", "de", 6), A("ac3", "en", 6)])
    assert str(episode_relpath("Breaking Bad", 2008, 3, [1], ["Kein Más"], "mkv", info)) == \
        "Breaking Bad (2008)/Staffel 3/Breaking Bad - S03E01 - Kein Más - 1080p [DE+EN].mkv"


def test_episode_multi_and_missing_title():
    info = MI(1280, 720, "h264", None, [A("ac3", "de", 2)])
    assert episode_relpath("Breaking Bad", 2008, 1, [1, 2], ["Pilot", "Die Katze ist im Sack"], "mkv", info).name == \
        "Breaking Bad - S01E01-E02 - Pilot & Die Katze ist im Sack - 720p [DE].mkv"
    assert episode_relpath("X", 2020, 0, [5], [""], "mkv", info).name == "X - S00E05 - 720p [DE].mkv"
    assert episode_relpath("X", 2020, 0, [5], [""], "mkv", info).parent.name == "Staffel 0"


def test_episode_three_episodes_and_empty_titles_dropped():
    info = MI(1280, 720, "h264", None, [A("ac3", "de", 2)])
    assert episode_relpath("X", 2020, 1, [1, 2, 3], ["A", "", "C"], "mkv", info).name == \
        "X - S01E01-E03 - A & C - 720p [DE].mkv"


def test_episode_without_info_year_and_label():
    p = episode_relpath("X", None, 2, [4], ["T"], "mkv", None, season_label="Season")
    assert str(p) == "X/Season 2/X - S02E04 - T.mkv"


def test_episode_no_languages():
    info = MI(1920, 1080, "h264", None, [])
    assert episode_relpath("X", 2020, 1, [1], [], "mkv", info).name == "X - S01E01 - 1080p.mkv"


def test_long_names_fit_255_bytes():
    p = episode_relpath("Serie", 2020, 1, [1], ["ä" * 300], "mkv", MI(1920, 1080, "h264", None, []))
    assert len(p.name.encode()) <= MAX_VIDEO_NAME_BYTES and p.name.endswith(" - 1080p.mkv")


def test_long_episode_title_cut_keeps_show_and_whole_chars():
    info = MI(1920, 1080, "h264", None, [A("ac3", "de", 2)])
    p = episode_relpath("Serie", 2020, 1, [1], ["日本語" * 100], "mkv", info)
    assert len(p.name.encode()) <= MAX_VIDEO_NAME_BYTES
    assert p.name.startswith("Serie - S01E01 - 日本語") and p.name.endswith(" - 1080p [DE].mkv")
    assert p.name.encode().decode("utf-8")  # gültiges UTF-8
    assert len(p.name.encode()) > MAX_VIDEO_NAME_BYTES - 3  # so wenig wie nötig gekürzt


def test_long_show_title_cut_after_episode_title():
    info = MI(1920, 1080, "h264", None, [])
    p = episode_relpath("S" * 300, 2020, 1, [1], ["Folge"], "mkv", info)
    assert len(p.name.encode()) <= MAX_VIDEO_NAME_BYTES
    assert len(p.parts[0].encode()) <= 255 and len(p.parts[1].encode()) <= 255
    assert p.name.endswith(" - S01E01 - 1080p.mkv")
    assert "Folge" not in p.name  # Folgenname zuerst geopfert
    show_part = p.name.split(" - S01E01")[0]
    assert set(show_part) == {"S"} and 0 < len(show_part) < 300  # Serientitel gekürzt


def test_long_movie_title_cut():
    info = MI(1920, 1080, "h264", None, [A("dts", "de", 6)])
    p = movie_relpath("ö" * 300, 2020, "mkv", info)
    assert len(p.name.encode()) <= MAX_VIDEO_NAME_BYTES and len(p.parent.name.encode()) <= 255
    assert p.name.endswith(" (2020) 1080p h264 DTS [DE].mkv")


def test_target_names_are_nfc():
    nfd = unicodedata.normalize("NFD", "König der Löwen")
    p = movie_relpath(nfd, 1994, "mkv", MI(1920, 1080, "h264", None, []))
    assert unicodedata.is_normalized("NFC", str(p))
    nfd_ep = unicodedata.normalize("NFD", "Kein Más")
    p = episode_relpath(nfd, 1994, 1, [1], [nfd_ep], "mkv", None)
    assert unicodedata.is_normalized("NFC", str(p))


def test_subtitle_targets(tmp_path):
    v = Path("/lib/X (2020)/X (2020) 1080p h264 AC3 [DE].mkv")
    subs = [Subtitle(Path("/in/a.ger.srt"), "de", False), Subtitle(Path("/in/b.ger.srt"), "de", False),
            Subtitle(Path("/in/c.forced.eng.srt"), "en", True), Subtitle(Path("/in/d.srt"), None, False),
            Subtitle(Path("/in/e.idx"), "de", False), Subtitle(Path("/in/e.sub"), "de", False)]
    names = [t.name for _, t in subtitle_targets(v, subs)]
    assert names == ["X (2020) 1080p h264 AC3 [DE].de.srt", "X (2020) 1080p h264 AC3 [DE].de.2.srt",
                     "X (2020) 1080p h264 AC3 [DE].en.forced.srt", "X (2020) 1080p h264 AC3 [DE].srt",
                     "X (2020) 1080p h264 AC3 [DE].de.idx", "X (2020) 1080p h264 AC3 [DE].de.sub"]


def test_subtitle_targets_second_idx_sub_pair_and_parent():
    v = Path("/lib/X (2020)/X (2020).iso")
    subs = [Subtitle(Path("/in/a.idx"), "de", False), Subtitle(Path("/in/a.sub"), "de", False),
            Subtitle(Path("/in/b.idx"), "de", False), Subtitle(Path("/in/b.sub"), "de", False)]
    result = subtitle_targets(v, subs)
    assert [s for s, _ in result] == subs
    assert [t.name for _, t in result] == ["X (2020).de.idx", "X (2020).de.sub",
                                           "X (2020).de.2.idx", "X (2020).de.2.sub"]
    assert all(t.parent == v.parent for _, t in result)


def test_subtitle_names_of_truncated_videos_fit_255_bytes():
    info = MI(1920, 1080, "h264", None, [A("ac3", "de", 2)])
    subs = [Subtitle(Path("/in/a.srt"), "de", True), Subtitle(Path("/in/b.srt"), "de", True),
            Subtitle(Path("/in/c.idx"), "en", True), Subtitle(Path("/in/c.sub"), "en", True)]
    videos = [
        episode_relpath("Serie", 2020, 1, [1], ["ä" * 300], "mkv", info),
        episode_relpath("S" * 300, 2020, 1, [1], ["x"], "mkv", info),
        movie_relpath("ö" * 300, 2020, "mkv", info),
    ]
    for rel in videos:
        video = Path("/lib") / rel
        stem = video.name[: -len(".mkv")]
        assert len(video.name.encode()) <= MAX_NAME_BYTES - SUBTITLE_RESERVE
        targets = subtitle_targets(video, subs)
        assert targets[1][1].name == stem + ".de.forced.2.srt"
        for _, target in targets:
            assert len(target.name.encode()) <= MAX_NAME_BYTES
            assert target.name.startswith(stem)


def test_season_label_is_sanitized():
    assert episode_relpath("X", 2020, 1, [1], [], "mkv", None, season_label="Sea/son:").parent.name == "Sea-son - 1"
