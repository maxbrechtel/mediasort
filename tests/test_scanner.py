import os
from pathlib import Path

import pytest

from mediasort.scanner import (
    SUB_EXT,
    VIDEO_EXT,
    is_sample_name,
    letter_range,
    owns_subtitle,
    scan,
    subtitle_from_path,
)

MB = 1024 * 1024
GB = 1024 * MB
NOW = 1_800_000_000.0

DEFAULTS = dict(
    exclude=["incomplete", "nzbs", ".deletedByTMM", ".thumbnails", "@eaDir"],
    sample_bytes=100 * MB,
    min_age_s=600,
    now=NOW,
)


@pytest.fixture
def mk(tmp_path):
    def _mk(rel, size, age_s=3600):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "wb"):
            pass
        os.truncate(p, size)
        os.utime(p, (NOW - age_s, NOW - age_s))
        return p

    return _mk


def test_extension_sets():
    assert VIDEO_EXT == frozenset({"mkv", "mp4", "m4v", "avi", "mov", "wmv", "ts", "m2ts", "iso"})
    assert SUB_EXT == frozenset({"srt", "ass", "ssa", "sub", "idx", "sup", "vtt"})


def test_finds_videos_and_subtitles(tmp_path, mk):
    mk("Rel/Movie.2020.mkv", 2 * GB)
    mk("Rel/Movie.2020.ger.srt", 1)
    mk("Rel/Subs/eng.forced.srt", 1)
    mk("Rel/movie.nfo", 1)
    r = scan(tmp_path, **DEFAULTS)
    assert [v.path.name for v in r.videos] == ["Movie.2020.mkv"]
    assert sorted((s.language, s.forced) for s in r.videos[0].subtitles) == [("de", False), ("en", True)]
    v = r.videos[0]
    assert v.root == tmp_path and v.size == 2 * GB and v.mtime == NOW - 3600


def test_subtitles_need_matching_prefix_when_folder_has_several_videos(tmp_path, mk):
    mk("S/A.S01E01.mkv", GB)
    mk("S/A.S01E02.mkv", GB)
    mk("S/A.S01E01.de.srt", 1)
    mk("S/other.srt", 1)
    v = {x.path.name: x for x in scan(tmp_path, **DEFAULTS).videos}
    assert [s.path.name for s in v["A.S01E01.mkv"].subtitles] == ["A.S01E01.de.srt"]
    assert v["A.S01E02.mkv"].subtitles == ()


def test_samples_excluded_and_too_new_skipped(tmp_path, mk):
    mk("R/x.sample.mkv", 300 * MB)
    mk("R/small.mkv", 50 * MB)
    mk("R/new.mkv", GB, age_s=60)
    r = scan(tmp_path, **DEFAULTS)
    assert r.videos == [] and len(r.samples) == 2 and [p.name for p in r.too_new] == ["new.mkv"]


def test_sample_word_must_stand_alone(tmp_path, mk):
    mk("R/Sampler.2020.mkv", GB)
    mk("R/Sample.mkv", GB)
    mk("R/Movie-SAMPLE-x.mkv", GB)
    r = scan(tmp_path, **DEFAULTS)
    assert [v.path.name for v in r.videos] == ["Sampler.2020.mkv"]
    assert sorted(p.name for p in r.samples) == ["Movie-SAMPLE-x.mkv", "Sample.mkv"]


def test_excluded_hidden_and_ignored(tmp_path, mk):
    mk("incomplete/a.mkv", GB)
    mk(".hidden/b.mkv", GB)
    mk("ok/c.mkv", GB)
    mk("ok/d.mkv", GB)
    r = scan(tmp_path, **DEFAULTS, ignored={str(tmp_path / "ok/d.mkv")})
    assert [v.path.name for v in r.videos] == ["c.mkv"]


def test_ignored_video_still_counts_for_subtitle_ownership(tmp_path, mk):
    mk("R/A.mkv", GB)
    mk("R/B.mkv", GB)
    mk("R/other.srt", 1)
    r = scan(tmp_path, **DEFAULTS, ignored={str(tmp_path / "R/B.mkv")})
    assert r.videos[0].subtitles == ()


def test_letter_range(tmp_path, mk):
    mk("Avatar (2009)/a.mkv", GB)
    mk("Dune (2021)/d.mkv", GB)
    mk("300 (2007)/t.mkv", GB)
    names = {v.path.name for v in scan(tmp_path, **DEFAULTS, top_filter=letter_range("0", "C")).videos}
    assert names == {"a.mkv", "t.mkv"}


def test_letter_range_folds_umlauts_and_case():
    f = letter_range("A", "B")
    assert f("Ärger (2000)") and f("bambi") and f("Avatar")
    assert not f("Casablanca") and not f("300") and not f("Über")
    assert letter_range("0", "0")("[REC] (2007)") and letter_range("0", "0")("12 Monkeys")
    assert letter_range("U", "U")("Über (2000)")


def test_results_sorted_by_path(tmp_path, mk):
    for n in ("z/z.mkv", "a/b.mkv", "a/a.mkv", "m/m.mkv"):
        mk(n, GB)
    r = scan(tmp_path, **DEFAULTS)
    assert [v.path for v in r.videos] == sorted(v.path for v in r.videos)


def test_subs_folder_variants_and_non_subs_subfolder(tmp_path, mk):
    mk("R/Movie.mkv", GB)
    mk("R/Subtitles/Movie.fr.srt", 1)
    mk("R/Extras/x.srt", 1)
    r = scan(tmp_path, **DEFAULTS)
    assert [(s.path.name, s.language) for s in r.videos[0].subtitles] == [("Movie.fr.srt", "fr")]


def test_subtitle_from_path():
    s = subtitle_from_path(Path("/x/Movie.2020.ger.srt"))
    assert (s.path, s.language, s.forced) == (Path("/x/Movie.2020.ger.srt"), "de", False)
    s = subtitle_from_path(Path("/x/Subs/eng.forced.srt"))
    assert (s.language, s.forced) == ("en", True)
    s = subtitle_from_path(Path("/x/Subs/2_English.srt"))
    assert (s.language, s.forced) == ("en", False)
    s = subtitle_from_path(Path("/x/Movie.forced.de.srt"))
    assert (s.language, s.forced) == ("de", True)
    s = subtitle_from_path(Path("/x/other.srt"))
    assert (s.language, s.forced) == (None, False)


@pytest.mark.parametrize("name", ["It.2017.srt", "Who.Am.I.2014.srt", "He.Is.Back.srt",
                                  "No.Country.for.Old.Men.srt"])
def test_title_words_are_not_languages(name):
    assert subtitle_from_path(Path("/x") / name).language is None


def test_subtitle_trailing_token_rules():
    assert subtitle_from_path(Path("/x/Movie.German.srt")).language == "de"
    s = subtitle_from_path(Path("/x/c.forced.eng.srt"))
    assert (s.language, s.forced) == ("en", True)
    s = subtitle_from_path(Path("/x/eng.forced.srt"))
    assert (s.language, s.forced) == ("en", True)
    assert subtitle_from_path(Path("/x/Movie.en.sdh.srt")).language == "en"
    assert subtitle_from_path(Path("/x/Movie.3.de.srt")).language == "de"
    assert subtitle_from_path(Path("/x/Movie.de.2.srt")).language == "de"
    assert subtitle_from_path(Path("/x/Movie-de.srt")).language == "de"


def test_subtitle_with_video_stem_parses_remainder_only():
    s = subtitle_from_path(Path("/x/It.2017.srt"), "It.2017")
    assert s.language is None
    s = subtitle_from_path(Path("/x/It.2017.fr.srt"), "It.2017")
    assert s.language == "fr"
    assert subtitle_from_path(Path("/x/A.S01E01.de.srt"), "A.S01E01").language == "de"


def test_owning_video_stem_is_used_in_scan(tmp_path, mk):
    mk("R/It.2017.mkv", GB)
    mk("R/It.2017.srt", 1)
    mk("R/It.2017.fr.srt", 1)
    subs = scan(tmp_path, **DEFAULTS).videos[0].subtitles
    assert sorted((s.path.name, s.language) for s in subs) == [("It.2017.fr.srt", "fr"), ("It.2017.srt", None)]


def test_prefix_ownership_requires_separator(tmp_path, mk):
    mk("S/A.S01E01.mkv", GB)
    mk("S/A.S01E02.mkv", GB)
    mk("S/A.S01E010.srt", 1)
    mk("S/A.S01E01.de.srt", 1)
    mk("S/A.S01E01_forced.srt", 1)
    v = {x.path.name: x for x in scan(tmp_path, **DEFAULTS).videos}
    assert [s.path.name for s in v["A.S01E01.mkv"].subtitles] == ["A.S01E01.de.srt", "A.S01E01_forced.srt"]
    assert v["A.S01E02.mkv"].subtitles == ()


def test_errors_for_missing_root_and_broken_symlink(tmp_path, mk):
    r = scan(tmp_path / "missing", **DEFAULTS)
    assert r.videos == [] and len(r.errors) == 1 and r.errors[0].startswith("Ordner nicht lesbar: ")
    (tmp_path / "R").mkdir()
    os.symlink(tmp_path / "nowhere", tmp_path / "R" / "broken.mkv")
    mk("R/ok.mkv", GB)
    r = scan(tmp_path, **DEFAULTS)
    assert [v.path.name for v in r.videos] == ["ok.mkv"]
    assert r.errors == [f"Datei nicht lesbar: {tmp_path / 'R' / 'broken.mkv'}"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_unreadable_directory_reported(tmp_path, mk):
    mk("locked/a.mkv", GB)
    mk("ok/b.mkv", GB)
    os.chmod(tmp_path / "locked", 0)
    try:
        r = scan(tmp_path, **DEFAULTS)
    finally:
        os.chmod(tmp_path / "locked", 0o755)
    assert [v.path.name for v in r.videos] == ["b.mkv"]
    assert r.errors == [f"Ordner nicht lesbar: {tmp_path / 'locked'}"]


def test_is_sample_name():
    assert is_sample_name("rel-sample.mkv") and is_sample_name("Sample.People.2000.mkv")
    assert not is_sample_name("Samples.mkv") and not is_sample_name("Resample.2020.mkv")


def test_owns_subtitle():
    assert owns_subtitle("Movie", "Movie.de.srt") and owns_subtitle("Movie", "movie srt")
    assert owns_subtitle("Movie", "Movie")
    assert not owns_subtitle("Movie", "Movies.de.srt") and not owns_subtitle("Movie", "Other.srt")
