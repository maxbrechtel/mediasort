import unicodedata
from pathlib import Path

import pytest

from mediasort.db import Folder, LibraryEntry
from mediasort.library import (
    LibraryIndex,
    episode_files,
    parse_folder_name,
    read_nfo_tmdb_id,
    videos_in,
)
from mediasort.models import Candidate, MediaKind

M = lambda id, t, y, en=None, orig=None: Candidate(id, MediaKind.MOVIE, t, orig or t, en, y, 1.0)


def mk(base: Path, rel: str, text: str = "x") -> Path:
    p = base / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def test_nfo_id(tmp_path):
    (tmp_path / "movie.nfo").write_text('<movie><uniqueid type="tmdb">603</uniqueid></movie>')
    assert read_nfo_tmdb_id(tmp_path) == 603


def test_nfo_id_with_other_attributes_and_ids(tmp_path):
    (tmp_path / "movie.nfo").write_text(
        '<movie><uniqueid type="imdb">tt0133093</uniqueid>'
        '<uniqueid default="true" type=\'tmdb\'>603</uniqueid></movie>'
    )
    assert read_nfo_tmdb_id(tmp_path) == 603


def test_nfo_tmdbid_tag(tmp_path):
    (tmp_path / "x.nfo").write_text("<movie><tmdbid>841</tmdbid></movie>")
    assert read_nfo_tmdb_id(tmp_path) == 841


def test_nfo_missing_or_unusable(tmp_path):
    assert read_nfo_tmdb_id(tmp_path) is None
    assert read_nfo_tmdb_id(tmp_path / "gibt-es-nicht") is None
    (tmp_path / "a.nfo").write_text("<movie><uniqueid type='imdb'>tt1</uniqueid></movie>")
    (tmp_path / "b.nfo").write_bytes(b"\xff\xfe kaputt <tmdbid>abc</tmdbid>")
    assert read_nfo_tmdb_id(tmp_path) is None


def test_nfo_only_directly_in_folder(tmp_path):
    mk(tmp_path, "sub/movie.nfo", "<tmdbid>5</tmdbid>")
    assert read_nfo_tmdb_id(tmp_path) is None


def test_parse_folder_name():
    assert parse_folder_name("Jede Sekunde zählt - The Guardian (2006)") == ("Jede Sekunde zählt - The Guardian", 2006)
    assert parse_folder_name("Greenland") == ("Greenland", None)
    assert parse_folder_name("Blade Runner 2049 (2017)") == ("Blade Runner 2049", 2017)
    assert parse_folder_name("Tron (Legacy) (2010)") == ("Tron (Legacy)", 2010)


@pytest.fixture
def lib(session, sessionmaker_, tmp_path):
    root = tmp_path / "Filme"
    root.mkdir()
    folder = Folder(path=str(root), role="library")
    session.add(folder)
    session.commit()
    return sessionmaker_, folder.id, root


def make_index(lib):
    sm, lib_id, root = lib
    return LibraryIndex(sm, lib_id, root)


def test_find_by_id_and_by_title(lib):
    _, _, root = lib
    mk(root, "Matrix (1999)/Matrix.mkv")
    mk(root, "Matrix (1999)/movie.nfo", '<movie><uniqueid type="tmdb">603</uniqueid></movie>')
    mk(root, "Jede Sekunde zaehlt The Guardian (2006)/a.mkv")
    index = make_index(lib)
    assert index.refresh() == 2
    hit = index.find(M(603, "Matrix", 1999))
    assert hit[0] == root / "Matrix (1999)" and hit[0].is_absolute()
    hits = index.find(M(4643, "Jede Sekunde zählt - The Guardian", 2006))
    assert [h.name for h in hits] == ["Jede Sekunde zaehlt The Guardian (2006)"]


def test_finds_nfd_folder(lib):
    _, _, root = lib
    name = unicodedata.normalize("NFD", "Jede Sekunde zählt - The Guardian (2006)")
    assert name != unicodedata.normalize("NFC", name)
    mk(root, f"{name}/a.mkv")
    index = make_index(lib)
    index.refresh()
    hits = index.find(M(4643, unicodedata.normalize("NFC", "Jede Sekunde zählt - The Guardian"), 2006))
    assert [h.name for h in hits] == [name]


def test_stored_title_is_nfc(lib):
    sm, _lib_id, root = lib
    mk(root, f"{unicodedata.normalize('NFD', 'Müller (2000)')}/a.mkv")
    make_index(lib).refresh()
    with sm() as s:
        (entry,) = s.query(LibraryEntry).all()
        assert entry.title == unicodedata.normalize("NFC", "Müller") and entry.year == 2000


def test_different_known_id_never_matches(lib):
    _, _, root = lib
    mk(root, "Dune (1984)/a.mkv", "")
    mk(root, "Dune (1984)/movie.nfo", "<tmdbid>841</tmdbid>")
    index = make_index(lib)
    index.refresh()
    assert index.find(M(438631, "Dune", 1984)) == []
    assert index.find(M(841, "Ganz anderer Titel", 2000)) == [root / "Dune (1984)"]


def test_year_distance(lib):
    _, _, root = lib
    mk(root, "Dune (2021)/a.mkv")
    mk(root, "Dune (1984)/a.mkv")
    mk(root, "Dune/a.mkv")
    index = make_index(lib)
    index.refresh()
    hits = index.find(M(1, "Dune", 2022))
    assert [h.name for h in hits] == ["Dune", "Dune (2021)"]  # ohne Jahr entscheidet die Ähnlichkeit allein


def test_candidate_without_year_matches_by_title(lib):
    _, _, root = lib
    mk(root, "Dune (1984)/a.mkv")
    index = make_index(lib)
    index.refresh()
    assert [h.name for h in index.find(M(1, "Dune", None))] == ["Dune (1984)"]


def test_id_matches_first_then_title_matches(lib):
    _, _, root = lib
    mk(root, "A Dune (2021)/x.mkv")
    mk(root, "Zeta/movie.nfo", "<tmdbid>7</tmdbid>")
    mk(root, "Dune (2021)/x.mkv")
    index = make_index(lib)
    index.refresh()
    assert [h.name for h in index.find(M(7, "Dune", 2021))] == ["Zeta", "Dune (2021)"]


def test_refresh_only_top_level_dirs_and_skips_hidden(lib):
    _, _, root = lib
    mk(root, "Matrix (1999)/Extras/Trailer (2000)/a.mkv")
    mk(root, ".hidden (2000)/a.mkv")
    mk(root, "loose (2001).mkv")
    assert make_index(lib).refresh() == 1


def test_refresh_replaces_entries_and_keeps_known_ids(lib):
    sm, _lib_id, root = lib
    mk(root, "Greenland (2020)/a.mkv")
    mk(root, "Weg (2001)/a.mkv")
    index = make_index(lib)
    index.refresh()
    index.remember(root / "Greenland (2020)", 524047)
    (root / "Weg (2001)").rename(root / "Neu (2002)")
    assert index.refresh() == 2
    with sm() as s:
        rows = {r.path: r for r in s.query(LibraryEntry).all()}
    assert set(rows) == {str(root / "Greenland (2020)"), str(root / "Neu (2002)")}
    assert rows[str(root / "Greenland (2020)")].tmdb_id == 524047
    assert rows[str(root / "Neu (2002)")].tmdb_id is None


def test_refresh_does_not_touch_other_libraries(lib, session):
    _sm, _lib_id, root = lib
    other = Folder(path="/x", role="library")
    session.add(other)
    session.commit()
    session.add(LibraryEntry(library_id=other.id, path="/x/Foo (2000)", title="Foo", year=2000, tmdb_id=1, kind="movie"))
    session.commit()
    mk(root, "Matrix (1999)/a.mkv")
    make_index(lib).refresh()
    assert session.query(LibraryEntry).filter_by(library_id=other.id).count() == 1


def test_refresh_missing_root_is_empty(lib):
    sm, lib_id, root = lib
    assert LibraryIndex(sm, lib_id, root / "weg").refresh() == 0


def test_remember_makes_folder_findable_by_id(lib):
    _, _, root = lib
    mk(root, "Irgendwas (2010)/a.mkv")
    index = make_index(lib)
    index.refresh()
    index.remember(root / "Irgendwas (2010)", 99)
    assert index.find(M(99, "Voellig anders", 1900)) == [root / "Irgendwas (2010)"]


def test_remember_unknown_folder_adds_entry(lib):
    _, _, root = lib
    index = make_index(lib)
    index.remember(root / "Neu (2024)", 5)
    assert index.find(M(5, "Neu", 2024)) == [root / "Neu (2024)"]


def test_videos_in(tmp_path):
    mk(tmp_path, "b/z.MKV")
    mk(tmp_path, "a.mp4")
    mk(tmp_path, "a.srt")
    mk(tmp_path, "info.nfo")
    assert videos_in(tmp_path) == [tmp_path / "a.mp4", tmp_path / "b" / "z.MKV"]
    assert videos_in(tmp_path / "weg") == []


def test_episode_files(tmp_path):
    for n in ["Season 4/Reacher.S04E05.mkv", "Staffel 4/Reacher - S04E05-E06 - X.mkv", "Staffel 4/Reacher - S04E07.mkv"]:
        mk(tmp_path, n)
    mk(tmp_path, "Staffel 3/Reacher - S03E05.mkv")
    assert len(episode_files(tmp_path, 4, [6])) == 1
    assert len(episode_files(tmp_path, 4, [5])) == 2
    assert len(episode_files(tmp_path, 4, [5, 7])) == 3
    assert episode_files(tmp_path, 4, [9]) == []
