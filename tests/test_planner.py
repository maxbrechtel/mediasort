import os
import shutil
import time
from collections import Counter
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from conftest import A, MI
from mediasort.db import DbCache, Folder, Ignored, Item, LibraryEntry, Scan
from mediasort.mediainfo import ProbeError
from mediasort.models import MediaKind
from mediasort.planner import PlanContext, RootMissing, plan_folder, replan_item, target_root
from mediasort.settings import Settings
from mediasort.tmdb import TmdbClient
from mediasort.web.routes_review import decision_options

DEFAULT_INFO = MI(1920, 1080, "h264", None, [A("dts", "de", 6), A("ac3", "en", 6)])
UHD_DE = MI(3840, 2160, "hevc", "HDR", [A("eac3", "de", 6)])

# --- TMDB-Fixtures --------------------------------------------------------------

MATRIX = {"de-DE": {"id": 603, "title": "Matrix", "original_title": "The Matrix",
                    "release_date": "1999-03-30", "popularity": 85.4},
          "en-US": {"id": 603, "title": "The Matrix", "original_title": "The Matrix",
                    "release_date": "1999-03-30", "popularity": 85.4}}
AVATAR = {"de-DE": {"id": 19995, "title": "Avatar - Aufbruch nach Pandora", "original_title": "Avatar",
                    "release_date": "2009-12-15", "popularity": 120.0},
          "en-US": {"id": 19995, "title": "Avatar", "original_title": "Avatar",
                    "release_date": "2009-12-15", "popularity": 120.0}}
REACHER = {"de-DE": {"id": 108978, "name": "Reacher", "original_name": "Reacher",
                     "first_air_date": "2022-02-03", "popularity": 300.0},
           "en-US": {"id": 108978, "name": "Reacher", "original_name": "Reacher",
                     "first_air_date": "2022-02-03", "popularity": 300.0}}
CHERNOBYL = {"de-DE": {"id": 87108, "name": "Chernobyl", "original_name": "Chernobyl",
                       "first_air_date": "2019-05-06", "popularity": 50.0},
             "en-US": {"id": 87108, "name": "Chernobyl", "original_name": "Chernobyl",
                       "first_air_date": "2019-05-06", "popularity": 50.0}}
SEARCH = {
    ("movie", "matrix"): MATRIX, ("movie", "the matrix"): MATRIX,
    ("movie", "avatar"): AVATAR,
    ("tv", "reacher"): REACHER, ("tv", "chernobyl"): CHERNOBYL,
}
DETAILS = {"movie/603": MATRIX, "movie/19995": AVATAR, "tv/108978": REACHER, "tv/87108": CHERNOBYL}
EPISODE_NAMES = {"de-DE": "Kapitel", "en-US": "Chapter"}


def tmdb_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path.removeprefix("/3/")
    lang = request.url.params.get("language", "de-DE")
    if path.startswith("search/"):
        kind = path.removeprefix("search/")
        data = SEARCH.get((kind, request.url.params["query"].lower()))
        return httpx.Response(200, json={"page": 1, "results": [data[lang]] if data else []})
    if path == "tv/108978/season/4":
        eps = [{"episode_number": n, "name": f"{EPISODE_NAMES[lang]} {n}"} for n in range(1, 9)]
        return httpx.Response(200, json={"season_number": 4, "episodes": eps})
    if path in DETAILS:
        return httpx.Response(200, json=DETAILS[path][lang])
    return httpx.Response(404, json={"status_message": "not found"})


# --- Testumgebung ---------------------------------------------------------------


class PlanResult:
    def __init__(self, env, scan_id):
        self.env, self.scan_id = env, scan_id

    def items(self) -> list[Item]:
        with self.env.sm() as s:
            return list(s.scalars(select(Item).where(Item.scan_id == self.scan_id).order_by(Item.source)))


class Env:
    def __init__(self, root: Path, sm, handler):
        self.root, self.sm = root, sm
        self.infos: dict[str, object] = {}
        self.counter: Counter = Counter()

        def counting(request):
            self.counter[request.url.path.removeprefix("/3/")] += 1
            return handler(request)

        self.tmdb = TmdbClient("key", cache=DbCache(sm), transport=httpx.MockTransport(counting),
                               sleep=lambda _s: None)
        for name in ("Downloads", "Filme_2D", "Filme_3D", "Serien"):
            (root / name).mkdir(parents=True)
        with sm() as s:
            f2d = Folder(path=str(root / "Filme_2D"), role="library", media_type="movie")
            f3d = Folder(path=str(root / "Filme_3D"), role="library", media_type="3d")
            ser = Folder(path=str(root / "Serien"), role="library", media_type="series")
            s.add_all([f2d, f3d, ser])
            s.flush()
            s.add(Folder(path=str(root / "Downloads"), role="input", media_type="auto",
                         movie_target_id=f2d.id, series_target_id=ser.id, movie3d_target_id=f3d.id))
            s.commit()
        self.settings = Settings(min_age_minutes=0, sample_bytes=0)
        self.ctx = PlanContext(session_factory=sm, settings=self.settings, tmdb=self.tmdb,
                               probe=self.probe, now=time.time)

    def probe(self, path: Path):
        info = self.infos.get(str(path), DEFAULT_INFO)
        if isinstance(info, Exception):
            raise info
        return info

    def file(self, rel: str, info=DEFAULT_INFO, content: bytes = b"video") -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        self.infos[str(p)] = info
        return p

    def p(self, rel: str) -> str:
        return str(self.root / rel)

    def folder(self, name: str) -> Folder:
        with self.sm() as s:
            return s.scalars(select(Folder).where(Folder.path == str(self.root / name))).one()

    def update_folder(self, name: str, **values) -> None:
        with self.sm() as s:
            f = s.scalars(select(Folder).where(Folder.path == str(self.root / name))).one()
            for k, v in values.items():
                setattr(f, k, v)
            s.commit()

    def plan(self, name: str, **kwargs) -> PlanResult:
        return PlanResult(self, plan_folder(self.ctx, self.folder(name), **kwargs))

    def calls(self, path: str) -> int:
        return self.counter[path]

    def item(self, item_id: int) -> Item:
        with self.sm() as s:
            return s.get(Item, item_id)


@pytest.fixture
def env(tmp_path, sessionmaker_):
    e = Env(tmp_path / "media", sessionmaker_, tmdb_handler)
    yield e
    e.tmdb.close()


@pytest.fixture
def env_tmdb_down(tmp_path, sessionmaker_):
    def down(request):
        raise httpx.ConnectError("Verbindung abgelehnt", request=request)

    e = Env(tmp_path / "media", sessionmaker_, down)
    yield e
    e.tmdb.close()


# --- Tests aus dem Plan ---------------------------------------------------------


def test_movie_in_downloads_gets_target(env):
    src = env.file("Downloads/The.Matrix.1999.German.DL.1080p.BluRay.x264/matrix.mkv")
    it = env.plan("Downloads").items()[0]
    assert it.target == env.p("Filme_2D/Matrix (1999)/Matrix (1999) 1080p h264 DTS [DE+EN].mkv")
    assert it.confidence == "hoch" and it.status == "new"
    assert (it.source, it.kind, it.tmdb_id, it.title, it.year) == (str(src), "movie", 603, "Matrix", 1999)
    assert it.group_key == "movie:matrix:1999" and it.conflict_json is None
    assert it.parsed_json["title"] == "matrix" and it.parsed_json["kind"] == "movie"
    assert it.mediainfo_json["audio"][0] == {
        "codec": "dts", "profile": None, "channels": 6, "language": "de", "default": False}


def test_episodes_grouped_one_search(env):
    for e in (1, 2, 3):
        env.file(f"Downloads/Reacher.S04E0{e}.German.DL.1080P.WEBRip.h265-WOTT/x.mkv")
    items = env.plan("Downloads").items()
    assert len({i.group_key for i in items}) == 1 and env.calls("search/tv") == 2  # de + en
    # de (Folgennummern, danach aus dem Cache für die Namen) + en, einmal je Gruppe und Staffel
    assert env.calls("tv/108978/season/4") == 2
    assert items[0].target.startswith(env.p("Serien/Reacher (2022)/Staffel 4/Reacher - S04E01 - "))
    assert items[0].target == env.p("Serien/Reacher (2022)/Staffel 4/Reacher - S04E01 - Kapitel 1 - 1080p [DE+EN].mkv")
    assert [i.confidence for i in items] == ["hoch"] * 3
    assert (items[2].season, items[2].episodes_json, items[2].episode_titles_json) == (4, [3], ["Kapitel 3"])


def test_3d_goes_to_3d_library(env):
    env.file("Downloads/Avatar.2009.3D.HSBS.1080p/a.mkv")
    it = env.plan("Downloads").items()[0]
    assert it.target.startswith(env.p("Filme_3D/"))
    assert it.target == env.p(
        "Filme_3D/Avatar - Aufbruch nach Pandora (2009)/"
        "Avatar - Aufbruch nach Pandora (2009) 1080p h264 DTS 3D HSBS [DE+EN].mkv")


def test_existing_movie_is_conflict_with_suggestion(env):
    env.file("Filme_2D/Matrix (1999)/Matrix (1999) 720p h264 AC3 [DE].mkv",
             info=MI(1280, 720, "h264", None, [A("ac3", "de", 6)]))
    env.file("Downloads/The.Matrix.1999.2160p/m.mkv", info=MI(3840, 2160, "hevc", "DV", [A("truehd", "de", 8)]))
    it = env.plan("Downloads").items()[0]
    assert it.conflict_json["existing"] == [env.p("Filme_2D/Matrix (1999)/Matrix (1999) 720p h264 AC3 [DE].mkv")]
    assert it.conflict_json["suggestion"] == "keep_new"


def test_same_target_in_batch_becomes_conflict(env):
    env.file("Downloads/Reacher.S04E05.German.DL.2160p.DV.HDR.WEBRip.AV1-WOTT/a.mkv", info=UHD_DE)
    env.file("Downloads/Reacher.S04E05.German.DL.HDR.2160p.WEB.h265-W4K/b.mkv", info=UHD_DE)
    items = env.plan("Downloads").items()
    assert items[0].target == items[1].target
    assert items[0].conflict_json is None
    assert items[1].conflict_json["existing"] == [items[0].source]


def test_already_correct_file_in_library_mode_has_no_item(env):
    env.file("Filme_2D/Matrix (1999)/Matrix (1999) 1080p h264 DTS [DE+EN].mkv")
    assert env.plan("Filme_2D").items() == []


def test_episode_in_movie_folder_is_flagged(env):
    env.file("Filme_2D/Chernobyl.2019.E05.720p.BluRay.DD5.1.x264-DON.mkv")
    it = env.plan("Filme_2D").items()[0]
    assert it.confidence == "niedrig" and "Serienfolge" in it.note
    assert it.kind == "movie" and env.calls("search/tv") == 0


def test_unreadable_file_is_error(env):
    env.file("Downloads/Kaputt.2020/k.mkv", info=ProbeError("ffprobe konnte die Datei nicht lesen"))
    it = env.plan("Downloads").items()[0]
    assert it.status == "error" and it.target is None and "ffprobe" in it.error


def test_tmdb_down_marks_items(env_tmdb_down):
    env = env_tmdb_down
    env.file("Downloads/The.Matrix.1999/m.mkv")
    it = env.plan("Downloads").items()[0]
    assert it.confidence == "niedrig" and "TMDB" in it.error and it.status == "new"
    assert it.target is None


def test_missing_root_raises(env):
    shutil.rmtree(env.root / "Filme_2D")
    with pytest.raises(RootMissing, match="Filme_2D"):
        env.plan("Downloads")
    assert not (env.root / "Filme_2D").exists()


def test_replan_with_tmdb_id_updates_group(env):
    for e in (1, 2, 3):
        env.file(f"Downloads/Jack.Reacher.Serie.S04E0{e}.German.DL.1080p/x.mkv")
    env.file("Downloads/The.Matrix.1999/m.mkv")
    items = env.plan("Downloads").items()
    episodes = [i for i in items if i.kind == "episode"]
    assert len(episodes) == 3 and all(i.target is None and i.confidence == "niedrig" for i in episodes)

    replan_item(env.ctx, episodes[1].id, tmdb_ref=("tv", "108978"))

    for n, old in enumerate(episodes, start=1):
        it = env.item(old.id)
        assert (it.tmdb_id, it.title, it.year, it.confidence) == (108978, "Reacher", 2022, "hoch")
        assert it.target == env.p(f"Serien/Reacher (2022)/Staffel 4/Reacher - S04E0{n} - Kapitel {n} - 1080p [DE+EN].mkv")
    movie = env.item(next(i.id for i in items if i.kind == "movie"))
    assert movie.tmdb_id == 603


# --- Weitere Tests --------------------------------------------------------------


def test_ignored_and_already_planned_files_skipped(env):
    env.file("Downloads/The.Matrix.1999/m.mkv")
    ignored = env.file("Downloads/Avatar.2009.3D.HSBS.1080p/a.mkv")
    with env.sm() as s:
        s.add(Ignored(path=str(ignored), size=5, created=0.0))
        s.commit()
    first = env.plan("Downloads").items()
    assert [i.source for i in first] == [env.p("Downloads/The.Matrix.1999/m.mkv")]
    assert env.plan("Downloads").items() == []
    with env.sm() as s:
        s.get(Item, first[0].id).status = "done"
        s.commit()
    assert len(env.plan("Downloads").items()) == 1


def test_scan_row_is_finished_with_errors_and_auto_flag(env):
    env.file("Downloads/The.Matrix.1999/m.mkv")
    result = env.plan("Downloads", auto=True)
    with env.sm() as s:
        scan = s.get(Scan, result.scan_id)
    assert scan.status == "done" and scan.finished is not None and scan.auto is True
    assert scan.errors_json == []
    assert result.items()[0].auto is True


def test_subset_limits_top_level_entries(env):
    env.file("Downloads/The.Matrix.1999/m.mkv")
    env.file("Downloads/Avatar.2009.3D.HSBS.1080p/a.mkv")
    items = env.plan("Downloads", subset=lambda name: name.startswith("A")).items()
    assert [i.tmdb_id for i in items] == [19995]


def test_missing_series_target_is_error(env):
    env.update_folder("Downloads", series_target_id=None)
    env.file("Downloads/Reacher.S04E01.German.DL.1080P.WEBRip.h265-WOTT/x.mkv")
    it = env.plan("Downloads").items()[0]
    assert it.status == "error" and it.target is None
    assert it.error == "Kein Ziel für Serien konfiguriert"


def test_no_tmdb_candidate_is_low_without_target(env):
    env.file("Downloads/Unbekannter.Film.2020.1080p/u.mkv")
    it = env.plan("Downloads").items()[0]
    assert (it.confidence, it.target, it.status, it.tmdb_id) == ("niedrig", None, "new", None)


def test_library_duplicate_folder_is_conflict(env):
    correct = env.file("Filme_2D/Matrix (1999)/Matrix (1999) 1080p h264 DTS [DE+EN].mkv")
    env.file("Filme_2D/The Matrix (1999)/the.matrix.720p.mkv",
             info=MI(1280, 720, "h264", None, [A("ac3", "de", 6)]))
    items = env.plan("Filme_2D").items()
    assert len(items) == 1
    assert items[0].target == env.p("Filme_2D/Matrix (1999)/Matrix (1999) 720p h264 AC3 [DE].mkv")
    conflict = items[0].conflict_json
    assert (conflict["existing"], conflict["suggestion"], conflict["partial"]) == ([str(correct)], "keep_existing", False)


def test_library_folder_found_by_title_remembers_tmdb_id(env):
    env.file("Filme_2D/Matrix (1999)/Matrix (1999) 720p h264 AC3 [DE].mkv")
    env.file("Downloads/The.Matrix.1999/m.mkv")
    env.plan("Downloads")
    with env.sm() as s:
        entry = s.scalars(select(LibraryEntry).where(LibraryEntry.path == env.p("Filme_2D/Matrix (1999)"))).one()
    assert entry.tmdb_id == 603


def test_existing_episode_in_series_library_is_conflict(env):
    old = env.file("Serien/Reacher (2022)/Season 4/reacher.s04e02.720p.mkv",
                   info=MI(1280, 720, "h264", None, [A("aac", "de", 2)]))
    env.file("Serien/Reacher (2022)/Season 4/reacher.s04e03.720p.mkv")
    env.file("Downloads/Reacher.S04E02.German.DL.1080P.WEBRip.h265-WOTT/x.mkv")
    it = env.plan("Downloads").items()[0]
    conflict = it.conflict_json
    assert (conflict["existing"], conflict["suggestion"], conflict["partial"]) == ([str(old)], "keep_new", False)


def test_conflict_without_quality_info_has_no_suggestion(env):
    env.file("Filme_2D/Matrix (1999)/Matrix (1999) 720p h264 AC3 [DE].mkv", info=ProbeError("kaputt"))
    env.file("Downloads/The.Matrix.1999/m.mkv")
    it = env.plan("Downloads").items()[0]
    assert len(it.conflict_json["existing"]) == 1 and it.conflict_json["suggestion"] is None


def test_subtitles_get_targets(env):
    env.file("Downloads/The.Matrix.1999/m.mkv")
    env.file("Downloads/The.Matrix.1999/m.de.forced.srt")
    it = env.plan("Downloads").items()[0]
    assert it.subtitles_json == [{
        "path": env.p("Downloads/The.Matrix.1999/m.de.forced.srt"), "language": "de", "forced": True,
        "target": env.p("Filme_2D/Matrix (1999)/Matrix (1999) 1080p h264 DTS [DE+EN].de.forced.srt"),
    }]


def test_fixed_3d_input_folder_treats_movies_as_3d(env):
    env.update_folder("Downloads", media_type="3d")
    env.file("Downloads/Avatar.2009.1080p/a.mkv")
    it = env.plan("Downloads").items()[0]
    assert it.target == env.p(
        "Filme_3D/Avatar - Aufbruch nach Pandora (2009)/Avatar - Aufbruch nach Pandora (2009) 1080p h264 DTS 3D [DE+EN].mkv")


def test_replan_overrides_episode_and_3d_format(env):
    env.file("Downloads/Reacher.S04E01.German.DL.1080P.WEBRip.h265-WOTT/x.mkv")
    env.file("Downloads/Avatar.2009.3D.1080p/a.mkv")
    ep, mov = sorted(env.plan("Downloads").items(), key=lambda i: i.kind)
    replan_item(env.ctx, ep.id, season=4, episodes=[7, 8])
    replan_item(env.ctx, mov.id, format_3d="SBS")
    assert env.item(ep.id).target == env.p(
        "Serien/Reacher (2022)/Staffel 4/Reacher - S04E07-E08 - Kapitel 7 & Kapitel 8 - 1080p [DE+EN].mkv")
    assert env.item(mov.id).target.endswith("1080p h264 DTS 3D SBS [DE+EN].mkv")


def test_replan_with_imdb_reference(env):
    env.file("Downloads/Unbekannter.Film.2020.1080p/u.mkv")
    it = env.plan("Downloads").items()[0]

    def handler(request):
        if request.url.path == "/3/find/tt0133093":
            lang = request.url.params["language"]
            return httpx.Response(200, json={"movie_results": [MATRIX[lang]], "tv_results": []})
        return tmdb_handler(request)

    env.ctx.tmdb = TmdbClient("key", transport=httpx.MockTransport(handler))
    replan_item(env.ctx, it.id, tmdb_ref=("imdb", "tt0133093"))
    env.ctx.tmdb.close()
    after = env.item(it.id)
    assert (after.tmdb_id, after.confidence, after.error) == (603, "hoch", None)
    assert after.target == env.p("Filme_2D/Matrix (1999)/Matrix (1999) 1080p h264 DTS [DE+EN].mkv")


def test_target_root_rules(env):
    with env.sm() as s:
        folders = {f.id: f for f in s.scalars(select(Folder))}
    downloads, f2d = env.folder("Downloads"), env.folder("Filme_2D")
    assert target_root(downloads, MediaKind.MOVIE, False, folders) == env.root / "Filme_2D"
    assert target_root(downloads, MediaKind.MOVIE, True, folders) == env.root / "Filme_3D"
    assert target_root(downloads, MediaKind.EPISODE, False, folders) == env.root / "Serien"
    assert target_root(f2d, MediaKind.EPISODE, False, folders) == env.root / "Filme_2D"


def test_series_folder_without_episode_number_then_corrected(env):
    env.update_folder("Downloads", media_type="series")
    env.file("Downloads/Reacher.German.DL.1080p/x.mkv")
    it = env.plan("Downloads").items()[0]
    assert (it.kind, it.target, it.confidence, it.note) == ("episode", None, "niedrig", "keine Folgennummer erkannt")
    replan_item(env.ctx, it.id, season=4, episodes=[2])
    after = env.item(it.id)
    assert after.note is None and after.status == "new"
    assert after.target == env.p("Serien/Reacher (2022)/Staffel 4/Reacher - S04E02 - Kapitel 2 - 1080p [DE+EN].mkv")


# --- Nachbesserung Runde 1 --------------------------------------------------------


def handler_with(overrides):
    """TMDB-Fixtures, bei denen einzelne Pfade durch eigene Antworten ersetzt sind."""
    def handler(request):
        path = request.url.path.removeprefix("/3/")
        if path in overrides:
            return overrides[path](request)
        return tmdb_handler(request)
    return handler


@pytest.fixture
def make_env(tmp_path, sessionmaker_):
    envs = []

    def make(handler):
        envs.append(Env(tmp_path / "media", sessionmaker_, handler))
        return envs[-1]

    yield make
    for e in envs:
        e.tmdb.close()


def test_unknown_season_is_not_high_and_explains(env):
    env.file("Downloads/Reacher.S09E01.German.DL.1080P.WEBRip.h265-WOTT/x.mkv")
    it = env.plan("Downloads").items()[0]
    assert it.confidence == "niedrig"
    assert it.error == "Staffel 9 bei TMDB nicht gefunden" and it.status == "new"


def test_season_lookup_failure_caps_confidence_at_medium(make_env):
    def down(request):
        raise httpx.ConnectError("Verbindung abgelehnt", request=request)

    env = make_env(handler_with({"tv/108978/season/4": down}))
    env.file("Downloads/Reacher.S04E01.German.DL.1080P.WEBRip.h265-WOTT/x.mkv")
    it = env.plan("Downloads").items()[0]
    assert it.confidence == "mittel" and "Staffel 4" in it.error and "TMDB" in it.error
    assert it.target == env.p("Serien/Reacher (2022)/Staffel 4/Reacher - S04E01 - 1080p [DE+EN].mkv")


def test_unnamed_episode_still_counts_as_existing(make_env):
    def season(request):
        eps = [{"episode_number": n, "name": "" if n == 3 else f"Kapitel {n}"} for n in range(1, 9)]
        return httpx.Response(200, json={"season_number": 4, "episodes": eps})

    env = make_env(handler_with({"tv/108978/season/4": season}))
    env.file("Downloads/Reacher.S04E03.German.DL.1080P.WEBRip.h265-WOTT/x.mkv")
    it = env.plan("Downloads").items()[0]
    assert it.confidence == "hoch" and it.error is None
    assert it.target == env.p("Serien/Reacher (2022)/Staffel 4/Reacher - S04E03 - 1080p [DE+EN].mkv")


def test_same_movie_in_batch_with_different_names_is_conflict(env):
    env.file("Downloads/The.Matrix.1999.1080p/a.mkv")
    env.file("Downloads/The.Matrix.1999.2160p/b.mkv", info=MI(3840, 2160, "hevc", "DV", [A("truehd", "de", 8)]))
    first, second = env.plan("Downloads").items()
    assert first.target != second.target
    assert first.conflict_json is None
    conflict = second.conflict_json
    assert (conflict["existing"], conflict["suggestion"], conflict["partial"]) == ([first.source], "keep_new", False)


def test_same_episode_in_batch_with_different_names_is_conflict(env):
    env.file("Downloads/Reacher.S04E05.German.DL.2160p.DV.HDR.WEBRip.AV1-WOTT/a.mkv", info=UHD_DE)
    env.file("Downloads/Reacher.S04E05.German.DL.1080p.WEB.h265-W4K/b.mkv")
    items = env.plan("Downloads").items()
    assert items[0].target != items[1].target
    conflicts = [i for i in items if i.conflict_json]
    assert len(conflicts) == 1
    other = next(i for i in items if not i.conflict_json)
    assert conflicts[0].conflict_json["existing"] == [other.source]
    assert conflicts[0].conflict_json["suggestion"] in ("keep_new", "keep_existing")


def test_multi_episode_overlap_in_batch_is_conflict(env):
    env.file("Downloads/Reacher.S04E05E06.German.DL.1080p.WEB.h265-W4K/a.mkv")
    env.file("Downloads/Reacher.S04E06.German.DL.720p.WEB.h264-X/b.mkv", info=MI(1280, 720, "h264", None, [A("aac", "de", 2)]))
    first, second = env.plan("Downloads").items()
    # die vorhandene Doppelfolge enthält E05, die neue Datei nicht: kein Vorschlag (Abschlussprüfung C2)
    conflict = second.conflict_json
    assert (conflict["existing"], conflict["suggestion"], conflict["partial"]) == ([first.source], None, True)
    assert (conflict["partial_existing"], conflict["partial_new"]) == (True, False)


def test_unchanged_error_file_is_not_planned_again(env):
    src = env.file("Downloads/Kaputt.2020/k.mkv", info=ProbeError("ffprobe konnte die Datei nicht lesen"))
    assert len(env.plan("Downloads").items()) == 1
    assert env.plan("Downloads").items() == []
    st = src.stat()
    os.utime(src, (st.st_atime, st.st_mtime - 3600))
    assert len(env.plan("Downloads").items()) == 1


def test_replan_refuses_done_and_ignored_items(env):
    env.file("Downloads/The.Matrix.1999/m.mkv")
    it = env.plan("Downloads").items()[0]
    for status in ("done", "ignored"):
        with env.sm() as s:
            s.get(Item, it.id).status = status
            s.commit()
        with pytest.raises(ValueError, match="nicht korrigiert"):
            replan_item(env.ctx, it.id, tmdb_ref=("movie", "603"))
    assert env.item(it.id).status == "ignored"


# --- Abschlussprüfung C1 ------------------------------------------------------------


def test_small_video_is_reported_in_scan_errors(env):
    env.settings.sample_bytes = 100
    small = env.file("Downloads/Show.S01/Show.S01E05.mkv", content=b"x" * 50)
    env.file("Downloads/The.Matrix.1999/m.mkv", content=b"x" * 500)
    result = env.plan("Downloads")
    assert [i.source for i in result.items()] == [env.p("Downloads/The.Matrix.1999/m.mkv")]
    with env.sm() as s:
        scan = s.get(Scan, result.scan_id)
    assert scan.errors_json == [f"Kleine Videodatei übersprungen (unter Sample-Grenze): {small}"]


def test_small_video_hint_is_not_repeated_for_unchanged_file(env):
    # Abschlussprüfung R4: jede Abfrage des Watchers würde sonst denselben Hinweis wiederholen
    env.settings.sample_bytes = 100
    small = env.file("Downloads/Show.S01/Show.S01E05.mkv", content=b"x" * 50)
    hint = f"Kleine Videodatei übersprungen (unter Sample-Grenze): {small}"

    def errors():
        with env.sm() as s:
            return s.get(Scan, env.plan("Downloads").scan_id).errors_json

    assert errors() == [hint]
    assert errors() == []
    small.write_bytes(b"x" * 60)  # verändert: wieder melden
    assert errors() == [hint]
    assert errors() == []
    other = env.file("Downloads/Show.S01/Show.S01E06.mkv", content=b"x" * 40)
    assert errors() == [f"Kleine Videodatei übersprungen (unter Sample-Grenze): {other}"]


# --- Abschlussprüfung C2: „Neue behalten“ löscht nur echte Dubletten ---------------------

SMALL_INFO = MI(1280, 720, "h264", None, [A("ac3", "de", 2)])


def _execute(env, item_id, decision):
    from mediasort.db import Job
    from mediasort.executor import execute

    with env.sm() as s:
        item = s.get(Item, item_id)
        item.decision, item.status = decision, "confirmed"
        job = Job(kind="execute", status="running")
        s.add(job)
        s.commit()
        job_id = job.id
    return execute(env.sm, [item_id], settings=env.settings, job_id=job_id)


def test_keep_new_spares_plex_extras_and_subfolders(env):
    old = env.file("Filme_2D/Matrix (1999)/Matrix (1999).mkv", SMALL_INFO)
    keep = [
        env.file("Filme_2D/Matrix (1999)/Featurettes/Making of.mkv", SMALL_INFO),
        env.file("Filme_2D/Matrix (1999)/Matrix (1999)-trailer.mkv", SMALL_INFO),
        env.file("Filme_2D/Matrix (1999)/Matrix (1999)-Behindthescenes.mp4", SMALL_INFO),
        env.file("Filme_2D/Matrix (1999)/.deletedByTMM/Matrix (1999) alt.mkv", SMALL_INFO),
        env.file("Filme_2D/Matrix (1999)/Sample/Matrix.mkv", SMALL_INFO),
        env.file("Filme_2D/Matrix (1999)/.versteckt.mkv", SMALL_INFO),
    ]
    env.file("Downloads/The.Matrix.1999.2160p.UHD/The.Matrix.1999.2160p.mkv", UHD_DE)
    [it] = env.plan("Downloads").items()
    assert it.conflict_json["existing"] == [str(old)]
    assert it.conflict_json["suggestion"] == "keep_new"
    assert _execute(env, it.id, "keep_new").done == 1
    assert not old.exists() and all(p.exists() for p in keep)


def test_small_existing_movie_file_is_not_a_version(env):
    env.settings.sample_bytes = 100
    env.file("Filme_2D/Matrix (1999)/Matrix (1999) clip.mkv", SMALL_INFO, content=b"x" * 50)
    env.file("Downloads/The.Matrix.1999.2160p.UHD/m.mkv", UHD_DE, content=b"x" * 500)
    [it] = env.plan("Downloads").items()
    assert it.conflict_json is None


def test_2d_and_3d_of_same_movie_in_one_batch_do_not_conflict(env):
    env.file("Downloads/Avatar.2009.1080p/Avatar.2009.1080p.mkv")
    env.file("Downloads/Avatar.2009.3D.HSBS.1080p/Avatar.2009.3D.HSBS.1080p.mkv")
    items = env.plan("Downloads").items()
    assert len(items) == 2 and not any(it.conflict_json for it in items)
    assert {Path(it.target).parts[-3] for it in items} == {"Filme_2D", "Filme_3D"}


def test_single_episode_vs_existing_double_episode_is_partial(env):
    double = env.file("Serien/Reacher (2022)/Staffel 4/Reacher - S04E01-E02 - 720p.mkv", SMALL_INFO)
    src = env.file("Downloads/Reacher.S04E01.German.DL.2160p.WEB/Reacher.S04E01.German.DL.2160p.WEB.mkv", UHD_DE)
    [it] = env.plan("Downloads").items()
    assert it.conflict_json["existing"] == [str(double)]
    assert it.conflict_json["partial"] is True and it.conflict_json["suggestion"] is None
    assert (it.conflict_json["partial_existing"], it.conflict_json["partial_new"]) == (True, False)
    assert [o for o, _ in decision_options(it)] == ["keep_existing", "keep_both", "skip"]
    r = _execute(env, it.id, "keep_new")
    assert r.failed == 1 and env.item(it.id).error == "Vorhandene Datei enthält weitere Folgen"
    assert double.exists() and src.exists()


def test_better_double_episode_vs_existing_single_is_partial_new(env):
    # neue Doppelfolge ersetzt die vorhandene Einzelfolge: erlaubt, es geht keine Folge verloren
    single = env.file("Serien/Reacher (2022)/Staffel 4/Reacher - S04E01 - 720p.mkv", SMALL_INFO)
    env.file("Downloads/Reacher.S04E01E02.German.DL.2160p.WEB/x.mkv", UHD_DE)
    [it] = env.plan("Downloads").items()
    assert it.conflict_json["existing"] == [str(single)]
    assert (it.conflict_json["partial_existing"], it.conflict_json["partial_new"]) == (False, True)
    assert it.conflict_json["partial"] is True and it.conflict_json["suggestion"] is None
    assert [o for o, _ in decision_options(it)] == ["keep_new", "keep_both", "skip"]
    assert _execute(env, it.id, "keep_new").done == 1
    assert not single.exists() and Path(env.item(it.id).target).exists()


def test_worse_double_episode_vs_existing_single_keeps_only_copy(env):
    # Abschlussprüfung R1: die neue Doppelfolge ist die einzige Kopie von E02
    single = env.file("Serien/Reacher (2022)/Staffel 4/Reacher - S04E01 - 2160p.mkv", UHD_DE)
    new = env.file("Downloads/Reacher.S04E01E02.German.720p.WEB/Reacher.S04E01E02.German.720p.WEB.mkv",
                   SMALL_INFO)
    [it] = env.plan("Downloads").items()
    assert it.conflict_json["existing"] == [str(single)]
    assert (it.conflict_json["partial_existing"], it.conflict_json["partial_new"]) == (False, True)
    assert it.conflict_json["suggestion"] is None
    assert "keep_existing" not in dict(decision_options(it))
    r = _execute(env, it.id, "keep_existing")
    assert r.failed == 1 and env.item(it.id).error == "Neue Datei enthält weitere Folgen"
    assert new.exists() and single.exists()


def test_mixed_episode_overlap_allows_no_deletion(env):
    # Abschlussprüfung R1: neu E01–E02, vorhanden E02–E03 – jede Seite hat eine Folge, die der anderen fehlt
    existing = env.file("Serien/Reacher (2022)/Staffel 4/Reacher - S04E02-E03 - 2160p.mkv", UHD_DE)
    new = env.file("Downloads/Reacher.S04E01E02.German.720p.WEB/Reacher.S04E01E02.German.720p.WEB.mkv",
                   SMALL_INFO)
    [it] = env.plan("Downloads").items()
    assert it.conflict_json["existing"] == [str(existing)]
    assert (it.conflict_json["partial_existing"], it.conflict_json["partial_new"]) == (True, True)
    assert it.conflict_json["partial"] is True and it.conflict_json["suggestion"] is None
    assert [o for o, _ in decision_options(it)] == ["keep_both", "skip"]
    r = _execute(env, it.id, "keep_existing")
    assert r.failed == 1 and env.item(it.id).error == "Neue Datei enthält weitere Folgen"
    r = _execute(env, it.id, "keep_new")
    assert r.failed == 1 and env.item(it.id).error == "Vorhandene Datei enthält weitere Folgen"
    assert new.exists() and existing.exists()
    assert _execute(env, it.id, "keep_both").done == 1
    assert existing.exists() and Path(env.item(it.id).target).exists()


def test_double_episode_vs_two_existing_singles_is_partial_new(env):
    # jede vorhandene Datei für sich fehlt eine Folge der neuen: „Vorhandene behalten“ gesperrt
    e1 = env.file("Serien/Reacher (2022)/Staffel 4/Reacher - S04E01 - 2160p.mkv", UHD_DE)
    e2 = env.file("Serien/Reacher (2022)/Staffel 4/Reacher - S04E02 - 2160p.mkv", UHD_DE)
    env.file("Downloads/Reacher.S04E01E02.German.720p.WEB/x.mkv", SMALL_INFO)
    [it] = env.plan("Downloads").items()
    assert sorted(it.conflict_json["existing"]) == sorted([str(e1), str(e2)])
    assert (it.conflict_json["partial_existing"], it.conflict_json["partial_new"]) == (False, True)
    assert it.conflict_json["suggestion"] is None


def test_double_episode_after_single_in_batch_is_partial_new(env):
    env.file("Downloads/Reacher.S04E01.German.DL.2160p.WEB/a.mkv", UHD_DE)
    env.file("Downloads/Reacher.S04E01E02.German.720p.WEB/b.mkv", SMALL_INFO)
    first, second = env.plan("Downloads").items()
    assert first.conflict_json is None
    conflict = second.conflict_json
    assert conflict["existing"] == [first.source]
    assert (conflict["partial_existing"], conflict["partial_new"], conflict["suggestion"]) == (False, True, None)


def test_title_only_library_match_has_no_suggestion(env):
    old = env.file("Filme_2D/Matrix/Matrix 720p.mkv", SMALL_INFO)  # ohne Jahr und ohne NFO
    env.file("Downloads/The.Matrix.1999.2160p/m.mkv", UHD_DE)
    [it] = env.plan("Downloads").items()
    assert it.conflict_json["existing"] == [str(old)] and it.conflict_json["suggestion"] is None
    with env.sm() as s:
        entry = s.scalars(select(LibraryEntry).where(LibraryEntry.path == env.p("Filme_2D/Matrix"))).one()
    assert entry.tmdb_id is None  # geratene ID wird nicht gemerkt


def test_conflict_records_existing_meta(env):
    old = env.file("Filme_2D/Matrix (1999)/Matrix (1999) 720p.mkv",
                   MI(3840, 2160, "hevc", "DV", [A("truehd", "de", 8), A("ac3", "en", 6)]))
    iso = env.file("Filme_2D/Matrix (1999)/Matrix (1999).iso", None)
    env.file("Downloads/The.Matrix.1999.1080p/m.mkv")
    [it] = env.plan("Downloads").items()
    st, st_iso = old.stat(), iso.stat()
    assert it.conflict_json["existing_meta"] == [
        {"path": str(old), "size": st.st_size, "mtime": st.st_mtime, "labels": "2160p h265 DV TrueHD [DE+EN]",
         "origin": "library"},
        {"path": str(iso), "size": st_iso.st_size, "mtime": st_iso.st_mtime, "labels": None, "origin": "library"},
    ]
    assert it.conflict_json["suggestion"] is None  # ISO ohne technische Angaben


def test_batch_conflict_meta_marks_origin_scan(env):
    first = env.file("Downloads/The.Matrix.1999.1080p/a.mkv")
    env.file("Downloads/The.Matrix.1999.2160p/b.mkv", UHD_DE)
    _, second = env.plan("Downloads").items()
    st = first.stat()
    assert second.conflict_json["existing_meta"] == [
        {"path": str(first), "size": st.st_size, "mtime": st.st_mtime, "labels": "1080p h264 DTS [DE+EN]",
         "origin": "scan"}]


def test_changed_existing_file_is_not_deleted(env):
    old = env.file("Filme_2D/Matrix (1999)/Matrix (1999) 720p.mkv", SMALL_INFO)
    src = env.file("Downloads/The.Matrix.1999.2160p/m.mkv", UHD_DE)
    [it] = env.plan("Downloads").items()
    old.write_bytes(b"inzwischen ersetzt, andere Groesse")
    r = _execute(env, it.id, "keep_new")
    assert r.failed == 1 and env.item(it.id).error == "Vorhandene Datei hat sich seit dem Scan verändert"
    assert old.read_bytes() == b"inzwischen ersetzt, andere Groesse" and src.exists()
    assert not Path(it.target).exists()


# --- Abschlussprüfung I1: ein Fehler betrifft nur seinen Vorschlag --------------------------


def test_unexpected_probe_error_marks_only_that_item(env):
    bad = env.file("Downloads/Kaputt.2020/k.mkv", info=UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"))
    env.file("Downloads/The.Matrix.1999/m.mkv")
    result = env.plan("Downloads")
    items = {i.source: i for i in result.items()}
    assert items[str(bad)].status == "error"
    assert "UnicodeDecodeError" in items[str(bad)].error and "Fehler" in items[str(bad)].error
    assert items[env.p("Downloads/The.Matrix.1999/m.mkv")].target is not None
    with env.sm() as s:
        assert s.get(Scan, result.scan_id).status == "done"


def test_unexpected_search_error_marks_only_that_group(env, monkeypatch):
    real_search = env.tmdb.search

    def search(kind, title, year):
        if title.lower() == "avatar":
            raise RuntimeError("unerwartet")
        return real_search(kind, title, year)

    monkeypatch.setattr(env.tmdb, "search", search)
    env.file("Downloads/Avatar.2009.1080p/a.mkv")
    env.file("Downloads/The.Matrix.1999/m.mkv")
    avatar, matrix = env.plan("Downloads").items()
    assert avatar.confidence == "niedrig" and avatar.target is None and "RuntimeError" in avatar.error
    assert matrix.target is not None and matrix.error is None


# --- Abschlussprüfung I2: Dubletten über Scans hinweg --------------------------------------


def test_duplicate_across_scans_is_conflict(env):
    from mediasort.watcher import auto_eligible

    first_src = env.file("Downloads/The.Matrix.1999.1080p/a.mkv")
    [first] = env.plan("Downloads").items()
    assert first.conflict_json is None
    env.file("Downloads/The.Matrix.1999.2160p/b.mkv", UHD_DE)
    [second] = env.plan("Downloads").items()
    conflict = second.conflict_json
    assert conflict["existing"] == [str(first_src)] and conflict["existing_meta"][0]["origin"] == "open"
    assert conflict["suggestion"] == "keep_new" and not auto_eligible(second)


def test_same_episode_across_scans_is_conflict(env):
    env.file("Downloads/Reacher.S04E05.German.DL.1080p.WEB.h265-W4K/a.mkv")
    env.plan("Downloads")
    env.file("Downloads/Reacher.S04E05.German.DL.2160p.DV.HDR.WEBRip.AV1-WOTT/b.mkv", UHD_DE)
    [second] = env.plan("Downloads").items()
    assert second.conflict_json["existing"] == [env.p("Downloads/Reacher.S04E05.German.DL.1080p.WEB.h265-W4K/a.mkv")]


def test_done_items_of_other_scans_are_not_claimed(env):
    first_src = env.file("Downloads/The.Matrix.1999.1080p/a.mkv")
    [first] = env.plan("Downloads").items()
    with env.sm() as s:
        s.get(Item, first.id).status = "ignored"
        s.commit()
    first_src.unlink()
    env.file("Downloads/The.Matrix.1999.2160p/b.mkv", UHD_DE)
    [second] = env.plan("Downloads").items()
    assert second.conflict_json is None


def test_replan_keeps_conflict_with_open_item_of_other_scan(env):
    first_src = env.file("Downloads/The.Matrix.1999.1080p/a.mkv")
    env.plan("Downloads")
    env.file("Downloads/The.Matrix.1999.2160p/b.mkv", UHD_DE)
    [second] = env.plan("Downloads").items()
    replan_item(env.ctx, second.id)
    assert env.item(second.id).conflict_json["existing"] == [str(first_src)]


# --- Abschlussprüfung I3: nicht lesbare Bibliothek ------------------------------------------


def _unreadable(monkeypatch, folder: Path) -> None:
    real_iterdir = Path.iterdir

    def iterdir(self):
        if self == folder:
            raise PermissionError(13, "Keine Berechtigung", str(self))
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", iterdir)


def test_unreadable_library_fails_scan_and_keeps_index(env, monkeypatch):
    env.file("Filme_2D/Matrix (1999)/Matrix (1999) 720p.mkv")
    env.file("Downloads/The.Matrix.1999/m.mkv")
    env.plan("Downloads")
    with env.sm() as s:
        s.query(Item).delete()
        s.commit()
    _unreadable(monkeypatch, env.root / "Filme_2D")
    with pytest.raises(RootMissing, match="Filme_2D"):
        env.plan("Downloads")
    with env.sm() as s:
        scans = s.scalars(select(Scan).order_by(Scan.id)).all()
        entry = s.scalars(select(LibraryEntry).where(LibraryEntry.path == env.p("Filme_2D/Matrix (1999)"))).one()
    assert scans[-1].status == "error" and entry.tmdb_id == 603


# --- Abschlussprüfung I5: SMB ohne Groß-/Kleinschreibung -------------------------------------


def test_case_only_rename_in_library_is_no_conflict(env, monkeypatch):
    real_exists = Path.exists

    def ci_exists(self, *args, **kwargs):  # wie auf einer SMB-Freigabe: Schreibweise egal
        if real_exists(self, *args, **kwargs):
            return True
        try:
            return any(c.name.casefold() == self.name.casefold() for c in self.parent.iterdir())
        except OSError:
            return False

    monkeypatch.setattr(Path, "exists", ci_exists)
    src = env.file("Filme_2D/Matrix (1999)/matrix (1999) 1080p h264 dts [de+en].mkv")
    [it] = env.plan("Filme_2D").items()
    assert it.source == str(src) and it.conflict_json is None
    assert it.target == env.p("Filme_2D/Matrix (1999)/Matrix (1999) 1080p h264 DTS [DE+EN].mkv")


# --- Abschlussprüfung I6: Bibliotheksmodus für große Serien --------------------------------


def test_library_episodes_are_parsed_once_per_run(env, monkeypatch):
    from mediasort import library

    for e in range(1, 9):
        env.file(f"Serien/Reacher (2022)/Season 4/reacher.s04e0{e}.720p.mkv")
    calls = Counter()
    real_parse = library.parse_name

    def counting(name, parent=None):
        calls[name] += 1
        return real_parse(name, parent)

    monkeypatch.setattr(library, "parse_name", counting)
    items = env.plan("Serien").items()
    assert len(items) == 8
    assert len(calls) == 8 and set(calls.values()) == {1}


def test_items_are_committed_in_batches(env, monkeypatch):
    from mediasort import planner

    monkeypatch.setattr(planner, "COMMIT_BATCH", 2)
    for name in ("A", "B", "C", "D", "E"):
        env.file(f"Downloads/Unbekannt.{name}.2020/x.mkv")
    seen: list[int] = []
    real_probe = env.ctx.probe

    def probe(path):
        with env.sm() as s:
            seen.append(s.query(Item).count())
        return real_probe(path)

    env.ctx.probe = probe
    result = env.plan("Downloads")
    assert seen == [0, 0, 2, 2, 4] and len(result.items()) == 5
    with env.sm() as s:
        assert s.get(Scan, result.scan_id).status == "done"
