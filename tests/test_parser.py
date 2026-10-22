import unicodedata

import pytest

from mediasort.models import MediaKind
from mediasort.parser import is_garbage_title, normalize_german, normalize_title, parse_name

LITTLE_THINGS = (
    "The Little Things 2021 German AC3D DL 2160p UHD BluRay DV HDR HEVC Remux-QfG DDL - NIMA4K org"
)
CONTINENTAL = (
    "The Continental Aus der Welt von John Wick 2023 S01 German EAC3 DL 2160 Hybrid "
    "WEB DV HDR10Plus HEVC-QfG DDL - NIMA4K org"
)

E, M = MediaKind.EPISODE, MediaKind.MOVIE

# (filename, parent, kind, title, year, season, episodes, languages, is_3d, format_3d, is_sample)
CASES = [
    ("Reacher.S04E05.German.DL.2160p.DV.HDR.WEBRip.AV1-WOTT.mkv", None,
     E, "Reacher", None, 4, (5,), ("de", "en"), False, None, False),
    ("Babylon.Berlin.UHD.Staffel3.Folge1.mkv", None,
     E, "Babylon Berlin", None, 3, (1,), (), False, None, False),
    ("Serie X Staffel 2 Folge 10.mkv", None,
     E, "Serie X", None, 2, (10,), (), False, None, False),
    ("Chernobyl.2019.E05.720p.BluRay.DD5.1.x264-DON.mkv", None,
     E, "Chernobyl", 2019, 1, (5,), (), False, None, False),
    ("Breaking.Bad.S01E01E02.German.720p.mkv", None,
     E, "Breaking Bad", None, 1, (1, 2), ("de",), False, None, False),
    ("bm1080-s01e02.mkv", None,
     E, "bm1080", None, 1, (2,), (), False, None, False),
    ("Final Destination 5 3D OU.mkv", None,
     M, "Final Destination 5", None, None, (), (), True, "OU", False),
    ("Avatar.2009.3D.HSBS.1080p.mkv", None,
     M, "Avatar", 2009, None, (), (), True, "HSBS", False),
    ("Avatar.2009.3D.MVC.mkv", None,
     M, "Avatar", 2009, None, (), (), True, "MVC", False),
    ("Final Destination 4 3D.mkv", None,
     M, "Final Destination 4", None, None, (), (), True, None, False),
    ("Star Wars - Das Erwachen der Macht (2015) 3D.iso", None,
     M, "Star Wars Das Erwachen der Macht", 2015, None, (), (), True, None, False),
    ("die.purpurnen.fluesse.2000.german.dl.2160p.uhd.bluray.x265-endstation.sample.mkv", None,
     M, "die purpurnen fluesse", 2000, None, (), ("de", "en"), False, None, True),
    ("y2x24go2wrcp.mkv", LITTLE_THINGS,
     M, "The Little Things", 2021, None, (), ("de", "en"), False, None, False),
    ("The.Continental.S01E02.mkv", CONTINENTAL,
     E, "The Continental", 2023, 1, (2,), ("de", "en"), False, None, False),
    ("matrix.mkv", "The.Matrix.1999.German.DL.1080p.BluRay.x264",
     M, "matrix", 1999, None, (), ("de", "en"), False, None, False),
    ("Breaking.Bad.S01E01.mkv", "Unrelated.Show.2015.German",
     E, "Breaking Bad", None, 1, (1,), (), False, None, False),
]


@pytest.mark.parametrize(
    "filename,parent,kind,title,year,season,episodes,languages,is_3d,format_3d,is_sample",
    CASES,
    ids=[c[0][:40] for c in CASES],
)
def test_parse_name(filename, parent, kind, title, year, season, episodes, languages,
                    is_3d, format_3d, is_sample):
    parsed = parse_name(filename, parent)
    assert parsed.kind == kind
    assert parsed.title == title
    assert parsed.year == year
    assert parsed.season == season
    assert parsed.episodes == episodes
    assert parsed.languages == languages
    assert parsed.is_3d is is_3d
    assert parsed.format_3d == format_3d
    assert parsed.is_sample is is_sample


def test_normalize_german():
    assert normalize_german("Serie Staffel 3 Folge 1") == "Serie S03E01"
    assert normalize_german("Serie.Staffel3.Folge1") == "Serie.S03E01"
    assert normalize_german("Serie St3 Episode 12") == "Serie S03E12"
    assert normalize_german("Serie St. 03 Folge 007") == "Serie S03E07"
    assert normalize_german("Serie Staffel 2") == "Serie S02"
    assert normalize_german("Serie.Staffel3.Folge1.mkv") == "Serie.S03E01.mkv"
    assert normalize_german("Serie Folge 5") == "Serie E05"
    assert normalize_german("Stalker 1979") == "Stalker 1979"
    assert normalize_german("Der Film") == "Der Film"


def test_is_garbage_title():
    assert is_garbage_title("y2x24go2wrcp") and is_garbage_title(None) and is_garbage_title("ab")
    assert not is_garbage_title("Reacher") and not is_garbage_title("M3GAN")
    assert not is_garbage_title("300")


def test_normalize_title():
    assert normalize_title("Der König der Löwen") == normalize_title(
        "der.konig.der.lowen".replace(".", " ")
    )
    assert normalize_title(unicodedata.normalize("NFD", "Löwen")) == normalize_title("Löwen")
    assert normalize_title("  Spider-Man:  Far   From Home! ") == "spider man far from home"
