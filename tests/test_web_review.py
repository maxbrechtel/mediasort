import json
import threading
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from conftest import A, MI
from mediasort.db import Folder, Ignored, Item, Job
from mediasort.planner import ItemNotFound
from mediasort.settings import load_settings, save_settings
from mediasort.web.app import create_app

FIXTURES = Path(__file__).parent / "fixtures"
INFO = MI(1920, 1080, "h264", None, [A("dts", "de", 6)])
INFO_720 = MI(1280, 720, "h264", None, [A("ac3", "de", 6)])

MATRIX = {"de-DE": {"id": 603, "title": "Matrix", "original_title": "The Matrix",
                    "release_date": "1999-03-30", "popularity": 85.4},
          "en-US": {"id": 603, "title": "The Matrix", "original_title": "The Matrix",
                    "release_date": "1999-03-30", "popularity": 85.4}}
REACHER = {"de-DE": {"id": 108978, "name": "Reacher", "original_name": "Reacher",
                     "first_air_date": "2022-02-03", "popularity": 300.0},
           "en-US": {"id": 108978, "name": "Reacher", "original_name": "Reacher",
                     "first_air_date": "2022-02-03", "popularity": 300.0}}
SEARCH = {("movie", "matrix"): MATRIX, ("movie", "the matrix"): MATRIX, ("tv", "reacher"): REACHER}
DETAILS = {"movie/603": MATRIX, "tv/108978": REACHER}
REACHER_FILES = [f"Downloads/Reacher.S04E0{e}.German.DL.1080P.WEBRip.h265-WOTT/x.mkv" for e in (1, 2, 3)]
MATRIX_FILE = "Downloads/The.Matrix.1999.1080p.BluRay.x264-GRP/The.Matrix.1999.1080p.BluRay.x264-GRP.mkv"
UNKNOWN_FILE = "Downloads/Voellig.Unbekannt.2020.1080p.mkv"
HX = {"HX-Request": "true"}


class TmdbServer:
    def __init__(self):
        self.status = 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.status != 200:
            return httpx.Response(self.status)
        path = request.url.path.removeprefix("/3/")
        lang = request.url.params.get("language", "de-DE")
        if path.startswith("search/"):
            data = SEARCH.get((path.removeprefix("search/"), request.url.params["query"].lower()))
            return httpx.Response(200, json={"page": 1, "results": [data[lang]] if data else []})
        if path == "find/tt0133093":
            return httpx.Response(200, json=json.loads((FIXTURES / "tmdb" / f"find_tt0133093_{lang}.json").read_text()))
        if path == "tv/108978/season/4":
            names = {"de-DE": "Kapitel", "en-US": "Chapter"}
            eps = [{"episode_number": n, "name": f"{names[lang]} {n}"} for n in range(1, 9)]
            return httpx.Response(200, json={"season_number": 4, "episodes": eps})
        if path in DETAILS:
            return httpx.Response(200, json=DETAILS[path][lang])
        return httpx.Response(404, json={"status_message": "not found"})


class PlexServer:
    def __init__(self):
        self.refreshed: list[tuple[str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/library/sections":
            return httpx.Response(200, json=json.loads((FIXTURES / "plex" / "sections.json").read_text()))
        if path.endswith("/refresh"):
            self.refreshed.append((path.split("/")[3], request.url.params["path"]))
            return httpx.Response(200)
        return httpx.Response(404)


class Env:
    def __init__(self, tmp_path: Path):
        self.media = tmp_path / "media"
        for name in ("Downloads", "Filme_2D", "Filme_3D", "Serien"):
            (self.media / name).mkdir(parents=True)
        self.tmdb, self.plex = TmdbServer(), PlexServer()
        self.infos: dict[str, object] = {}
        self.app = create_app(
            tmp_path / "config",
            tmdb_transport=httpx.MockTransport(self.tmdb),
            plex_transport=httpx.MockTransport(self.plex),
            probe=lambda path: self.infos.get(str(path), INFO),
            start_background=False,
        )
        with self.app.state.sf() as s:
            f2d = Folder(path=str(self.media / "Filme_2D"), role="library", media_type="movie")
            f3d = Folder(path=str(self.media / "Filme_3D"), role="library", media_type="3d")
            ser = Folder(path=str(self.media / "Serien"), role="library", media_type="series")
            s.add_all([f2d, f3d, ser])
            s.flush()
            downloads = Folder(path=str(self.media / "Downloads"), role="input", media_type="auto",
                               movie_target_id=f2d.id, series_target_id=ser.id, movie3d_target_id=f3d.id)
            s.add(downloads)
            s.commit()
            self.downloads_id, self.filme_id = downloads.id, f2d.id
        self.settings(tmdb_key="k", min_age_minutes=0, sample_bytes=0)

    def settings(self, **kw) -> None:
        with self.app.state.sf() as s:
            st = load_settings(s)
            for key, value in kw.items():
                setattr(st, key, value)
            save_settings(s, st)

    def file(self, rel: str, info=INFO) -> Path:
        p = self.media / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"video")
        self.infos[str(p)] = info
        return p

    def plan(self) -> int:
        with self.app.state.sf() as s:
            downloads = s.get(Folder, self.downloads_id)
        return self.app.state.services.plan(downloads, False)

    def item(self, rel: str) -> Item:
        with self.app.state.sf() as s:
            return s.scalars(select(Item).where(Item.source == str(self.media / rel))
                             .order_by(Item.id.desc())).first()

    def get(self, item_id: int) -> Item:
        with self.app.state.sf() as s:
            return s.get(Item, item_id)

    def update(self, item_id: int, **values) -> None:
        with self.app.state.sf() as s:
            item = s.get(Item, item_id)
            for key, value in values.items():
                setattr(item, key, value)
            s.commit()


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


@pytest.fixture
def client(env):
    with TestClient(env.app) as c:
        c.env = env
        yield c


@pytest.fixture
def client_with_plex(env):
    env.settings(plex_url="http://plex:32400", plex_token="tok",
                 plex_path_map=[(str(env.media / "Filme_2D"), "/media/Filme_2D")])
    with TestClient(env.app) as c:
        c.env = env
        yield c


@pytest.fixture
def planned(env):  # 3 Reacher-Folgen, Matrix und ein unbekannter Film
    for rel in REACHER_FILES:
        env.file(rel)
    env.file(MATRIX_FILE)
    env.file(UNKNOWN_FILE)
    env.plan()


@pytest.fixture
def planned_conflict(env):  # Matrix liegt schon (schlechter) in der Bibliothek
    env.existing = env.file("Filme_2D/Matrix (1999)/Matrix.1999.720p.mkv", info=INFO_720)
    env.file(MATRIX_FILE)
    env.plan()


def block_runner(client) -> threading.Event:
    release, started = threading.Event(), threading.Event()

    def job(job_id, progress):
        started.set()
        release.wait(10)
        return "fertig"

    assert client.app.state.runner.submit("scan", job) is not None
    assert started.wait(10)
    return release


# --- Prüfliste --------------------------------------------------------------------------


def test_review_groups_series(client, planned):
    r = client.get("/review")
    assert r.status_code == 200
    t = r.text
    assert t.count("Staffel 4") == 1 and "Matrix (1999)" in t
    assert t.count("Reacher (2022)") == 1  # Serienkopf, Folgen darunter
    for n in (1, 2, 3):
        assert f"Reacher - S04E0{n} - Kapitel {n} - 1080p [DE].mkv" in t
    assert t.index("S04E01") < t.index("S04E02") < t.index("S04E03")
    assert "Voellig.Unbekannt.2020.1080p.mkv" in t and "kein Ziel" in t
    assert "The.Matrix.1999.1080p.BluRay.x264-GRP.mkv" in t
    assert str(client.env.media / "Filme_2D") not in t  # Ziel relativ zur Bibliothek


def test_review_row_details(client, planned):
    t = client.get("/review").text
    assert "🟢 hoch" in t and "🔴 niedrig" in t
    assert "1080p" in t and "h264" in t and "DTS 5.1" in t and "[DE]" in t
    assert 'data-label="Sicherheit"' in t


def test_review_hides_done_and_ignored(client, planned):
    env = client.env
    env.update(env.item(MATRIX_FILE).id, status="done")
    env.update(env.item(UNKNOWN_FILE).id, status="ignored")
    t = client.get("/review").text
    assert "Matrix (1999)" not in t and "Voellig" not in t and "Reacher" in t


def test_review_filters(client, planned):
    env = client.env
    env.update(env.item(UNKNOWN_FILE).id, status="error", error="Kein Ziel für Filme konfiguriert")
    t = client.get("/review?kind=movie").text
    assert "Matrix (1999)" in t and "S04E01" not in t
    t = client.get("/review?kind=episode").text
    assert "Matrix (1999)" not in t and "S04E01" in t
    t = client.get("/review?confidence=niedrig").text
    assert "Voellig" in t and "Matrix (1999)" not in t and "S04E01" not in t
    t = client.get("/review?only=errors").text
    assert "Voellig" in t and "Kein Ziel für Filme konfiguriert" in t and "Matrix (1999)" not in t
    t = client.get("/review?only=conflicts").text
    assert "Voellig" not in t and "Matrix (1999)" not in t and "Keine Vorschläge" in t
    assert client.get("/review?confidence=quatsch&only=x").status_code == 200  # unbekannt: kein Filter


def test_review_empty(client):
    assert "Keine Vorschläge" in client.get("/review").text


# --- Bestätigen ---------------------------------------------------------------------------


def test_toggle_and_confirm_safe(client, planned):
    env = client.env
    matrix = env.item(MATRIX_FILE)
    r = client.post(f"/items/{matrix.id}/toggle", headers=HX)
    assert r.status_code == 200 and f'id="item-{matrix.id}"' in r.text and "<html" not in r.text
    assert env.get(matrix.id).status == "confirmed"
    r = client.post(f"/items/{matrix.id}/toggle", follow_redirects=False)  # ohne htmx: zurück zur Liste
    assert r.status_code == 303 and r.headers["location"].startswith("/review")
    assert env.get(matrix.id).status == "new"

    unknown = env.item(UNKNOWN_FILE)
    r = client.post(f"/items/{unknown.id}/toggle", headers=HX)
    assert r.status_code == 409 and env.get(unknown.id).status == "new"

    r = client.post("/items/confirm-safe", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/review")
    assert env.get(matrix.id).status == "confirmed"
    assert all(env.item(rel).status == "confirmed" for rel in REACHER_FILES)
    assert env.get(unknown.id).status == "new"  # niedrige Sicherheit, kein Ziel


def test_confirm_safe_skips_medium_conflicts_and_errors(client, planned):
    env = client.env
    ep1, ep2, ep3 = (env.item(rel).id for rel in REACHER_FILES)
    env.update(ep1, confidence="mittel")
    env.update(ep2, conflict_json={"existing": ["/x/alt.mkv"], "suggestion": None})
    env.update(ep3, error="Staffel 4 konnte bei TMDB nicht geprüft werden")
    client.post("/items/confirm-safe")
    assert [env.get(i).status for i in (ep1, ep2, ep3)] == ["new", "new", "new"]
    assert env.item(MATRIX_FILE).status == "confirmed"


def test_error_item_cannot_be_confirmed_and_offers_replan(client, planned):
    env = client.env
    matrix = env.item(MATRIX_FILE)
    env.update(matrix.id, status="error", error="Datei nicht lesbar")
    r = client.post(f"/items/{matrix.id}/toggle", headers=HX)
    assert r.status_code == 409 and env.get(matrix.id).status == "error"
    assert "Datei nicht lesbar" in r.text and "Neu planen" in r.text and "disabled" in r.text


def test_replan_after_missing_target_is_configured(client, env):
    with env.app.state.sf() as s:
        s.get(Folder, env.downloads_id).movie_target_id = None
        s.commit()
    env.file(MATRIX_FILE)
    env.plan()
    item = env.item(MATRIX_FILE)
    assert item.status == "error" and item.error == "Kein Ziel für Filme konfiguriert"
    env.plan()  # unveränderte Fehlerquelle: kein neuer Vorschlag
    assert env.item(MATRIX_FILE).id == item.id
    with env.app.state.sf() as s:
        s.get(Folder, env.downloads_id).movie_target_id = env.filme_id
        s.commit()
    r = client.post(f"/items/{item.id}/replan", headers=HX)
    assert r.status_code == 200 and "Matrix (1999)" in r.text
    after = env.get(item.id)
    assert after.status == "new" and after.error is None and after.target.startswith(str(env.media / "Filme_2D"))


def test_replan_refused_for_done_item(client, planned):
    env = client.env
    matrix = env.item(MATRIX_FILE)
    env.update(matrix.id, status="done")
    assert client.post(f"/items/{matrix.id}/replan", headers=HX).status_code == 409
    assert client.post("/items/999/replan").status_code == 404


# --- Konflikte ----------------------------------------------------------------------------


def test_conflict_shows_both_versions_and_suggestion(client, planned_conflict):
    env = client.env
    item = env.item(MATRIX_FILE)
    assert item.conflict_json == {"existing": [str(env.existing)], "suggestion": "keep_new"}
    t = client.get("/review").text
    assert str(env.existing) in t and str(env.media / MATRIX_FILE) in t
    assert "Vorschlag: die neue Version ist besser" in t
    assert 'value="keep_new" checked' in t
    assert "Bessere behalten (neue Version) – die vorhandene Version wird samt Untertiteln gelöscht" in t
    assert "Beide behalten – nichts wird gelöscht" in t and "Überspringen – nichts wird verändert" in t
    assert "gelöscht wird erst beim Ausführen" in t
    assert "Bitte zuerst entscheiden" in t

    r = client.post(f"/items/{item.id}/toggle", headers=HX)
    assert r.status_code == 409 and env.get(item.id).status == "new"

    r = client.post(f"/items/{item.id}/decision", data={"decision": "keep_new"}, headers=HX)
    assert r.status_code == 200 and f'id="item-{item.id}"' in r.text
    after = env.get(item.id)
    assert (after.status, after.decision) == ("confirmed", "keep_new")

    r = client.post(f"/items/{item.id}/decision", data={"decision": "skip"}, follow_redirects=False)
    assert r.status_code == 303
    after = env.get(item.id)
    assert (after.status, after.decision) == ("new", None)


def test_conflict_without_suggestion_offers_both_options(client, planned_conflict):
    env = client.env
    item = env.item(MATRIX_FILE)
    env.update(item.id, conflict_json={**item.conflict_json, "suggestion": None})
    t = client.get("/review").text
    assert "Neue Version behalten – die vorhandene Version wird samt Untertiteln gelöscht" in t
    assert "Vorhandene behalten – die neue Datei wird samt Untertiteln gelöscht" in t
    assert "Bessere behalten" not in t
    client.post(f"/items/{item.id}/decision", data={"decision": "keep_existing"})
    assert (env.get(item.id).status, env.get(item.id).decision) == ("confirmed", "keep_existing")


def test_keep_both_only_with_different_target(client, planned_conflict):
    env = client.env
    item = env.item(MATRIX_FILE)
    client.post(f"/items/{item.id}/decision", data={"decision": "keep_both"})
    assert env.get(item.id).decision == "keep_both"
    env.update(item.id, decision=None, status="new",
               conflict_json={"existing": [item.target], "suggestion": "keep_new"})
    assert "Beide behalten" not in client.get("/review").text
    r = client.post(f"/items/{item.id}/decision", data={"decision": "keep_both"}, headers=HX)
    assert r.status_code == 409 and env.get(item.id).decision is None


def test_decision_validation(client, planned):
    env = client.env
    matrix = env.item(MATRIX_FILE)
    r = client.post(f"/items/{matrix.id}/decision", data={"decision": "keep_new"}, headers=HX)
    assert r.status_code == 409  # kein Konflikt
    env.update(matrix.id, conflict_json={"existing": ["/x/alt.mkv"], "suggestion": None})
    r = client.post(f"/items/{matrix.id}/decision", data={"decision": "löschen"}, headers=HX)
    assert r.status_code == 400 and env.get(matrix.id).decision is None


# --- Korrigieren --------------------------------------------------------------------------


def test_correct_form(client, planned):
    item = client.env.item(UNKNOWN_FILE)
    r = client.get(f"/items/{item.id}/correct", headers=HX)
    assert r.status_code == 200 and "<html" not in r.text
    for name in ('name="q"', 'name="reference"', 'name="season"', 'name="episodes"', 'name="format_3d"'):
        assert name in r.text
    for fmt in ("SBS", "HSBS", "OU", "HOU", "TAB", "MVC"):
        assert f'<option value="{fmt}"' in r.text
    page = client.get(f"/items/{item.id}/correct")
    assert page.status_code == 200 and "<html" in page.text and "Voellig.Unbekannt" in page.text


def test_search_lists_candidates(client, planned):
    item = client.env.item(UNKNOWN_FILE)
    r = client.get(f"/items/{item.id}/search", params={"q": "Matrix", "kind": "movie"}, headers=HX)
    assert r.status_code == 200 and "<html" not in r.text
    assert "Matrix (1999)" in r.text and 'value="tmdb:movie:603"' in r.text
    r = client.get(f"/items/{item.id}/search", params={"q": "Reacher", "kind": "tv"}, headers=HX)
    assert "Reacher (2022)" in r.text and 'value="tmdb:tv:108978"' in r.text
    r = client.get(f"/items/{item.id}/search", params={"q": "Gibtsnicht", "kind": "movie"}, headers=HX)
    assert "Keine Treffer" in r.text
    page = client.get(f"/items/{item.id}/search", params={"q": "Matrix", "kind": "movie"})
    assert "<html" in page.text and 'value="tmdb:movie:603"' in page.text


def test_search_tmdb_error_is_german(client, planned):
    item = client.env.item(UNKNOWN_FILE)
    client.env.tmdb.status = 500
    r = client.get(f"/items/{item.id}/search", params={"q": "Matrix2", "kind": "movie"}, headers=HX)
    assert r.status_code == 502 and "TMDB-Fehler: HTTP 500" in r.text


def test_correct_by_imdb_link(client, planned):
    env = client.env
    item = env.item(UNKNOWN_FILE)
    r = client.post(f"/items/{item.id}/correct", headers=HX,
                    data={"reference": "https://www.imdb.com/title/tt0133093/", "season": "", "episodes": "",
                          "format_3d": ""})
    assert r.status_code == 200 and r.headers.get("HX-Refresh") == "true" and r.text == ""
    after = env.get(item.id)
    assert after.tmdb_id == 603 and after.confidence == "hoch"
    assert after.target.startswith(str(env.media / "Filme_2D" / "Matrix (1999)") + "/")


def test_correct_by_search_pick_and_3d(client, planned):
    env = client.env
    item = env.item(UNKNOWN_FILE)
    r = client.post(f"/items/{item.id}/correct", follow_redirects=False,
                    data={"pick": "tmdb:movie:603", "reference": "", "format_3d": "HSBS"})
    assert r.status_code == 303 and r.headers["location"].startswith("/review")
    after = env.get(item.id)
    assert after.target.startswith(str(env.media / "Filme_3D" / "Matrix (1999)") + "/")
    assert "3D HSBS" in after.target


def test_correct_series_season_and_episodes(client, planned):
    env = client.env
    item = env.item(REACHER_FILES[0])
    client.post(f"/items/{item.id}/correct", data={"reference": "", "season": "4", "episodes": "5, 6"})
    after = env.get(item.id)
    assert (after.season, after.episodes_json) == (4, [5, 6])
    assert after.target.endswith("Reacher - S04E05-E06 - Kapitel 5 & Kapitel 6 - 1080p [DE].mkv")


def test_correct_invalid_input(client, planned):
    env = client.env
    item = env.item(UNKNOWN_FILE)
    r = client.post(f"/items/{item.id}/correct", data={"reference": "irgendwas"}, headers=HX)
    assert r.status_code == 400 and "TMDB-/IMDb-ID oder Link" in r.text and 'name="reference"' in r.text
    r = client.post(f"/items/{item.id}/correct", data={"reference": "", "episodes": "eins"})
    assert r.status_code == 400 and "Folgen" in r.text and "<html" in r.text
    r = client.post(f"/items/{item.id}/correct", data={"reference": "", "episodes": "²"})
    assert r.status_code == 400 and "Folgen" in r.text
    r = client.post(f"/items/{item.id}/correct", data={"reference": "", "season": "-1"})
    assert r.status_code == 400 and "Staffel" in r.text
    r = client.post(f"/items/{item.id}/correct", data={"reference": "", "format_3d": "XYZ"})
    assert r.status_code == 400 and "3D-Format" in r.text
    assert env.get(item.id).tmdb_id is None


def test_correct_tmdb_error_is_german(client, planned):
    env = client.env
    item = env.item(UNKNOWN_FILE)
    r = client.post(f"/items/{item.id}/correct", data={"reference": "tmdb:movie:999"}, headers=HX)
    assert r.status_code == 502 and "TMDB: nicht gefunden (404)" in r.text
    assert env.get(item.id).tmdb_id is None


# --- Ignorieren ---------------------------------------------------------------------------


def test_ignore_persists_across_scans(client, planned):
    env = client.env
    item = env.item(UNKNOWN_FILE)
    r = client.post(f"/items/{item.id}/ignore", headers=HX)
    assert r.status_code == 200 and r.text == ""
    assert env.get(item.id).status == "ignored"
    with env.app.state.sf() as s:
        ignored = s.get(Ignored, item.source)
        assert ignored is not None and ignored.size == item.size and ignored.created > 0
    env.plan()
    assert env.item(UNKNOWN_FILE).id == item.id  # kein neuer Vorschlag
    assert "Voellig" not in client.get("/review").text
    r = client.post(f"/items/{env.item(MATRIX_FILE).id}/ignore", follow_redirects=False)
    assert r.status_code == 303


# --- Ausführen ----------------------------------------------------------------------------


def test_execute_runs_and_triggers_plex(client_with_plex, planned):
    env = client_with_plex.env
    matrix = env.item(MATRIX_FILE)
    client_with_plex.post(f"/items/{matrix.id}/toggle")
    r = client_with_plex.post("/execute", headers=HX)
    assert r.status_code == 200 and 'id="current-job"' in r.text
    assert env.app.state.runner.wait_idle(10)
    after = env.get(matrix.id)
    assert after.status == "done" and Path(after.target).is_file()
    assert not (env.media / MATRIX_FILE).exists()
    assert ("1", "/media/Filme_2D/Matrix (1999)") in env.plex.refreshed
    with env.app.state.sf() as s:
        job = s.scalars(select(Job)).one()
        assert (job.kind, job.status, job.auto) == ("execute", "done", False)
        assert job.message == "1 einsortiert, 0 Fehler"
        assert (job.progress, job.total) == (1, 1)
    assert env.item(REACHER_FILES[0]).status == "new"  # nicht bestätigt: unberührt


def test_execute_reports_errors(client, planned):
    env = client.env
    matrix = env.item(MATRIX_FILE)
    client.post(f"/items/{matrix.id}/toggle")
    (env.media / MATRIX_FILE).write_bytes(b"geaendert")
    r = client.post("/execute", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/review"
    assert env.app.state.runner.wait_idle(10)
    with env.app.state.sf() as s:
        assert s.scalars(select(Job)).one().message == "0 einsortiert, 1 Fehler"
    assert env.get(matrix.id).status == "error"
    assert "0 einsortiert, 1 Fehler" in client.get("/review").text


def test_execute_without_confirmed_items_is_noop(client):
    r = client.post("/execute", headers=HX)
    assert r.status_code == 200 and "Keine bestätigten Vorschläge" in r.text
    r = client.post("/execute", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/review?hinweis=keine"
    assert "Keine bestätigten Vorschläge" in client.get("/review?hinweis=keine").text
    with client.app.state.sf() as s:
        assert s.scalars(select(Job)).first() is None


def test_execute_while_busy(client, planned):
    env = client.env
    client.post(f"/items/{env.item(MATRIX_FILE).id}/toggle")
    release = block_runner(client)
    try:
        r = client.post("/execute", headers=HX)
        assert "Es läuft bereits ein Auftrag" in r.text
        r = client.post("/execute")
        assert "Es läuft bereits ein Auftrag" in r.text
    finally:
        release.set()
    assert client.app.state.runner.wait_idle(10)
    with client.app.state.sf() as s:
        assert [j.kind for j in s.scalars(select(Job))] == ["scan"]
    assert env.item(MATRIX_FILE).status == "confirmed"


def test_correct_refused_while_busy(client, planned):
    item = client.env.item(UNKNOWN_FILE)
    release = block_runner(client)
    try:
        r = client.post(f"/items/{item.id}/correct", data={"reference": "tmdb:movie:603"}, headers=HX)
        assert r.status_code == 409 and "Es läuft bereits ein Auftrag" in r.text
        assert client.post(f"/items/{item.id}/replan", headers=HX).status_code == 409
    finally:
        release.set()
    assert client.app.state.runner.wait_idle(10)
    assert client.env.get(item.id).tmdb_id is None


def test_unknown_item_routes(client):
    assert client.post("/items/999/toggle").status_code == 404
    assert client.get("/items/999/correct").status_code == 404
    assert client.get("/items/999/search?q=x").status_code == 404
    assert client.post("/items/999/correct", data={"reference": ""}).status_code == 404
    assert client.post("/items/999/ignore").status_code == 404
    assert client.post("/items/999/decision", data={"decision": "skip"}).status_code == 404


# --- Nachbesserungen (Review-Runde 1) ------------------------------------------------------


def test_correct_rejects_huge_numbers(client, planned):
    env = client.env
    item = env.item(REACHER_FILES[0])
    r = client.post(f"/items/{item.id}/correct", data={"season": "99999999999999999999999"}, headers=HX)
    assert r.status_code == 400 and "„Staffel“ muss eine ganze Zahl von 0 bis 999 sein" in r.text
    r = client.post(f"/items/{item.id}/correct", data={"episodes": "1, 10000"}, headers=HX)
    assert r.status_code == 400 and "1 bis 9999" in r.text
    r = client.post(f"/items/{item.id}/correct", data={"season": "999", "episodes": "9999"}, headers=HX)
    assert r.status_code == 200
    assert (env.get(item.id).season, env.get(item.id).episodes_json) == (999, [9999])


def test_old_name_relative_to_input_folder(client, planned):
    env = client.env
    t = client.get("/review").text
    assert "<code title=\"" + str(env.media / REACHER_FILES[0]) + "\">Reacher.S04E01.German.DL.1080P.WEBRip.h265-WOTT/x.mkv</code>" in t
    assert "The.Matrix.1999.1080p.BluRay.x264-GRP/The.Matrix.1999.1080p.BluRay.x264-GRP.mkv</code>" in t
    assert ">Voellig.Unbekannt.2020.1080p.mkv</code>" in t


def test_series_grouped_by_target_folders(client, planned):
    env = client.env
    ep1, ep2, ep3 = (env.item(rel).id for rel in REACHER_FILES)
    env.update(ep3, target=str(env.media / "Serien" / "Jack Reacher (2022)" / "Staffel 4" / "x.mkv"))
    env.update(ep2, target=None)
    t = client.get("/review").text
    assert t.count("<h3>Reacher (2022) <small class=\"muted\">in Serien</small></h3>") == 1
    assert "<h3>Jack Reacher (2022) <small class=\"muted\">in Serien</small></h3>" in t
    assert "<h3>Reacher (2022)</h3>" in t  # ohne Ziel: nach TMDB-Titel
    assert t.count("<h4>Staffel 4</h4>") == 3


def test_new_item_with_error_text_is_warning_and_confirmable(client, planned):
    env = client.env
    ep = env.item(REACHER_FILES[0]).id
    env.update(ep, error="Staffel 4 konnte bei TMDB nicht geprüft werden")
    t = client.get("/review").text
    assert '<p class="notice">Hinweis: Staffel 4 konnte bei TMDB nicht geprüft werden</p>' in t
    r = client.post(f"/items/{ep}/toggle", headers=HX)
    assert r.status_code == 200 and env.get(ep).status == "confirmed"
    client.post(f"/items/{ep}/toggle")
    client.post("/items/confirm-safe")
    assert env.get(ep).status == "new"


def test_replan_surfaces_unexpected_errors(client, planned, monkeypatch):
    services = client.app.state.services
    item = client.env.item(UNKNOWN_FILE)

    def broken(item_id, **kw):
        raise KeyError("kaputt")

    monkeypatch.setattr(services, "replan", broken)
    with pytest.raises(KeyError):
        client.post(f"/items/{item.id}/replan", headers=HX)

    def vanished(item_id, **kw):
        raise ItemNotFound(item_id)

    monkeypatch.setattr(services, "replan", vanished)
    assert client.post(f"/items/{item.id}/replan", headers=HX).status_code == 404


def test_bulk_buttons_show_counts(client, planned):
    env = client.env
    t = client.get("/review").text
    assert "Alle sicheren übernehmen (4)" in t and "Ausgewählte ausführen (0)" in t
    r = client.post(f"/items/{env.item(MATRIX_FILE).id}/toggle", headers=HX)
    assert r.headers.get("HX-Trigger") == "items-changed"
    r = client.get("/review/bulk", headers=HX)
    assert "<html" not in r.text and 'hx-trigger="items-changed from:body"' in r.text
    assert "Alle sicheren übernehmen (3)" in r.text and "Ausgewählte ausführen (1)" in r.text
    r = client.post(f"/items/{env.item(UNKNOWN_FILE).id}/ignore", headers=HX)
    assert r.headers.get("HX-Trigger") == "items-changed"
