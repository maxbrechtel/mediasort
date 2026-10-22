import asyncio
import json
import threading
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from conftest import A, MI
from mediasort.db import FileState, Folder, History, Item, Job, Scan
from mediasort.settings import Settings, load_settings, save_settings
from mediasort.web import app as web_app
from mediasort.web.app import create_app, poll_loop
from mediasort.web.forms import FormError, format_path_map, parse_path_map

PLEX_FIXTURES = Path(__file__).parent / "fixtures" / "plex"
INFO = MI(1920, 1080, "h264", None, [A("dts", "de", 6)])

MATRIX = {"de-DE": {"id": 603, "title": "Matrix", "original_title": "The Matrix",
                    "release_date": "1999-03-30", "popularity": 85.4},
          "en-US": {"id": 603, "title": "The Matrix", "original_title": "The Matrix",
                    "release_date": "1999-03-30", "popularity": 85.4}}


def tmdb_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path.removeprefix("/3/")
    lang = request.url.params.get("language", "de-DE")
    if path == "search/movie":
        query = request.url.params["query"].lower()
        results = [MATRIX[lang]] if query in ("matrix", "the matrix") else []
        return httpx.Response(200, json={"page": 1, "results": results})
    if path == "movie/603":
        return httpx.Response(200, json=MATRIX[lang])
    return httpx.Response(200, json={"page": 1, "results": []})


class PlexServer:
    """Plex-Attrappe: liefert die Fixtures und merkt sich die Refresh-Aufrufe."""

    def __init__(self, auto_empty_trash: bool = True, reachable: bool = True):
        self.auto_empty_trash = auto_empty_trash
        self.reachable = reachable
        self.refreshed: list[tuple[str, str]] = []
        self.status = 200  # Fehlerstatus für alle Anfragen
        self.refresh_status = 200
        self.timeouts: list[float] = []  # Lese-Timeout je Anfrage

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.timeouts.append(request.extensions["timeout"]["read"])
        if not self.reachable:
            raise httpx.ConnectError("Verbindung abgelehnt", request=request)
        if self.status != 200:
            return httpx.Response(self.status)
        path = request.url.path
        if path == "/library/sections":
            return httpx.Response(200, json=json.loads((PLEX_FIXTURES / "sections.json").read_text()))
        if path == "/:/prefs":
            prefs = json.loads((PLEX_FIXTURES / "prefs.json").read_text())
            for setting in prefs["MediaContainer"]["Setting"]:
                if setting["id"] == "autoEmptyTrash":
                    setting["value"] = self.auto_empty_trash
            return httpx.Response(200, json=prefs)
        if path.endswith("/refresh"):
            key = path.split("/")[3]
            self.refreshed.append((key, request.url.params["path"]))
            return httpx.Response(self.refresh_status)
        return httpx.Response(404)


class Env:
    def __init__(self, tmp_path: Path, plex: PlexServer):
        self.media = tmp_path / "media"
        for name in ("Downloads", "Filme_2D", "Filme_3D", "Serien"):
            (self.media / name).mkdir(parents=True)
        self.plex = plex
        self.app = create_app(
            tmp_path / "config",
            tmdb_transport=httpx.MockTransport(tmdb_handler),
            plex_transport=httpx.MockTransport(plex),
            probe=lambda path: INFO,
            start_background=False,
        )
        sf = self.app.state.sf
        with sf() as s:
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

    def settings(self, **kw) -> None:
        with self.app.state.sf() as s:
            st = load_settings(s)
            for key, value in kw.items():
                setattr(st, key, value)
            save_settings(s, st)

    def load(self) -> Settings:
        with self.app.state.sf() as s:
            return load_settings(s)

    def file(self, rel: str) -> Path:
        p = self.media / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"video")
        return p


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path, PlexServer())


@pytest.fixture
def client(env):
    with TestClient(env.app) as c:
        c.env = env
        yield c


@pytest.fixture
def client_with_plex(env):
    env.settings(plex_url="http://plex:32400", plex_token="tok")
    with TestClient(env.app) as c:
        c.env = env
        yield c


@pytest.fixture
def media_tree(env):
    env.settings(tmdb_key="k", min_age_minutes=0, sample_bytes=0)
    env.file("Downloads/The.Matrix.1999.1080p.BluRay.x264-GRP/The.Matrix.1999.1080p.BluRay.x264-GRP.mkv")
    env.file("Downloads/Voellig.Unbekannt.2020.1080p.mkv")


def block_runner(client) -> threading.Event:
    release, started = threading.Event(), threading.Event()

    def job(job_id, progress):
        progress(1, 4)
        started.set()
        release.wait(10)
        return "fertig"

    assert client.app.state.runner.submit("scan", job) is not None
    assert started.wait(10)
    return release


# --- Übersicht --------------------------------------------------------------------


def test_overview_shows_folders_and_warnings(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "TMDB-Schlüssel fehlt" in r.text and "Downloads" in r.text
    assert "Aus" in r.text and "Scannen" in r.text
    assert "Noch keine automatische Abfrage" in r.text
    for link in ('href="/"', 'href="/review"', 'href="/library"', 'href="/history"',
                 'href="/ignored"', 'href="/settings"'):
        assert link in r.text
    assert "Papierkorb" not in r.text  # Plex nicht eingerichtet


def test_overview_without_warnings_when_configured(client):
    client.env.settings(tmdb_key="k")
    assert "TMDB-Schlüssel fehlt" not in client.get("/").text


def test_overview_auto_level_open_count_and_last_auto_job(client):
    env = client.env
    with env.app.state.sf() as s:
        s.get(Folder, env.downloads_id).auto_level = "detect"
        for status in ("new", "confirmed", "done", "ignored"):
            s.add(Item(folder_id=env.downloads_id, source=f"/x/{status}.mkv", size=1, mtime=1.0, status=status))
        s.add(Job(kind="auto", status="done", auto=True, started=1_700_000_000.0, finished=1_700_000_060.0,
                  message="1 Ordner geprüft, 0 neue Vorschläge, 0 automatisch einsortiert"))
        s.add(History(ts=1_700_000_000.0, action="moved", old_path="/x/alt.mkv", new_path="/y/neu.mkv"))
        s.commit()
    text = client.get("/").text
    assert "Nur erkennen" in text
    assert '<td class="num" data-label="Offene Vorschläge">2</td>' in text  # neu + bestätigt
    assert "1 Ordner geprüft, 0 neue Vorschläge" in text
    assert "/y/neu.mkv" in text and "verschoben" in text


def test_overview_warns_about_missing_library(client):
    client.env.settings(tmdb_key="k")
    (client.env.media / "Serien").rmdir()
    text = client.get("/").text
    assert "Bibliotheksordner fehlt" in text and str(client.env.media / "Serien") in text


def test_overview_shows_scan_errors(client):
    with client.env.app.state.sf() as s:
        s.add(Scan(folder_id=client.env.downloads_id, status="done", errors_json=["nicht lesbar: /x/kaputt"]))
        s.commit()
    assert "nicht lesbar: /x/kaputt" in client.get("/").text


def test_plex_trash_warning(client_with_plex):  # prefs autoEmptyTrash=true
    assert "Plex leert den Papierkorb automatisch" in client_with_plex.get("/").text


def test_no_plex_trash_warning_when_disabled(client_with_plex):
    client_with_plex.env.plex.auto_empty_trash = False
    text = client_with_plex.get("/").text
    assert "Papierkorb" not in text and "Plex nicht erreichbar" not in text


def test_plex_unreachable_warning(client_with_plex):
    client_with_plex.env.plex.reachable = False
    r = client_with_plex.get("/")
    assert r.status_code == 200 and "Plex nicht erreichbar" in r.text


def test_health_returns_plain_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.text == "ok"
    assert r.headers["content-type"].startswith("text/plain")


def test_static_htmx_is_served(client):
    r = client.get("/static/htmx.min.js")
    assert r.status_code == 200 and 'version:"2.0.4"' in r.text
    assert client.get("/static/style.css").status_code == 200
    assert '<script src="/static/htmx.min.js"' in client.get("/").text


# --- Scannen und Aufträge -----------------------------------------------------------


def test_scan_starts_job_and_reports(client, media_tree):
    r = client.post(f"/scan/{client.env.downloads_id}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert client.app.state.runner.wait_idle(10)  # Runner läuft auch mit start_background=False
    text = client.get("/jobs/current").text
    assert "2 Vorschläge erstellt" in text and "fertig" in text
    assert "every 2s" not in text  # abgeschlossen: kein Nachladen mehr
    with client.app.state.sf() as s:
        job = s.scalars(select(Job)).one()
        assert (job.kind, job.status, job.auto) == ("scan", "done", False)
        assert s.scalars(select(Scan)).one().auto is False
    assert '<td class="num" data-label="Offene Vorschläge">2</td>' in client.get("/").text


def test_scan_htmx_returns_job_fragment(client, media_tree):
    r = client.post(f"/scan/{client.env.downloads_id}", headers={"HX-Request": "true"})
    assert r.status_code == 200 and 'id="current-job"' in r.text and "<html" not in r.text
    assert client.app.state.runner.wait_idle(10)


def test_scan_missing_root_reports_error(client):
    (client.env.media / "Downloads").rmdir()
    client.post(f"/scan/{client.env.downloads_id}")
    assert client.app.state.runner.wait_idle(10)
    text = client.get("/jobs/current").text
    assert "Fehler" in text and "Ordner nicht gefunden" in text


def test_scan_while_busy_shows_notice(client, media_tree):
    release = block_runner(client)
    try:
        r = client.post(f"/scan/{client.env.downloads_id}", headers={"HX-Request": "true"})
        assert "Es läuft bereits ein Auftrag" in r.text
        r = client.post(f"/scan/{client.env.downloads_id}")  # ohne htmx: Hinweis auf der Übersicht
        assert "Es läuft bereits ein Auftrag" in r.text
    finally:
        release.set()
    assert client.app.state.runner.wait_idle(10)
    with client.app.state.sf() as s:
        assert len(s.scalars(select(Job)).all()) == 1


def test_scan_unknown_folder(client):
    assert client.post("/scan/999").status_code == 404


def test_current_job_polls_only_while_active(client):
    assert "Noch kein Auftrag" in client.get("/jobs/current").text
    release = block_runner(client)
    try:
        text = client.get("/jobs/current").text
        assert 'hx-trigger="every 2s"' in text and "läuft" in text and "1 / 4" in text
    finally:
        release.set()
    assert client.app.state.runner.wait_idle(10)
    assert 'hx-trigger="every 2s"' not in client.get("/jobs/current").text


def test_run_items_executes_and_refreshes_plex(client_with_plex, media_tree):
    env = client_with_plex.env
    env.settings(plex_path_map=[(str(env.media / "Filme_2D"), "/media/Filme_2D")])
    services = env.app.state.services
    with env.app.state.sf() as s:
        downloads = s.get(Folder, env.downloads_id)
    scan_id = services.plan(downloads, False)
    with env.app.state.sf() as s:
        item = s.scalars(select(Item).where(Item.scan_id == scan_id, Item.tmdb_id == 603)).one()
        item.status = "confirmed"
        job = Job(kind="auto", status="running", auto=True)
        s.add(job)
        s.commit()
        item_id, target, job_id = item.id, item.target, job.id
    services.run_items([item_id], True, job_id)
    assert Path(target).is_file()
    with env.app.state.sf() as s:
        assert s.get(Item, item_id).status == "done"
        assert all(h.auto for h in s.scalars(select(History)))
    assert ("1", "/media/Filme_2D/Matrix (1999)") in env.plex.refreshed


def test_run_items_tolerates_unreachable_plex(client_with_plex, media_tree):
    env = client_with_plex.env
    env.plex.reachable = False
    services = env.app.state.services
    with env.app.state.sf() as s:
        downloads = s.get(Folder, env.downloads_id)
    scan_id = services.plan(downloads, False)
    with env.app.state.sf() as s:
        item = s.scalars(select(Item).where(Item.scan_id == scan_id, Item.tmdb_id == 603)).one()
        item.status = "confirmed"
        s.commit()
    services.run_items([item.id], False, 1)  # darf nicht werfen
    with env.app.state.sf() as s:
        assert s.get(Item, item.id).status == "done"


def test_poll_loop_rereads_interval_and_triggers():
    sleeps, triggered, minutes = [], [], iter([15, 5, 5])

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 3:
            raise asyncio.CancelledError

    def trigger():
        triggered.append(True)
        raise RuntimeError("Fehler im Auslöser darf die Schleife nicht beenden")

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(poll_loop(lambda: next(minutes), trigger, sleep=fake_sleep))
    assert sleeps == [900, 300, 300] and len(triggered) == 2


def test_background_task_starts_and_stops(tmp_path):
    app = create_app(tmp_path / "config", probe=lambda p: INFO, start_background=True)
    with TestClient(app) as c:
        assert c.get("/").status_code == 200
        assert app.state.poll_task is not None and not app.state.poll_task.done()
    assert app.state.poll_task.done()


# --- Einstellungen -------------------------------------------------------------------

SETTINGS_FORM = {
    "tmdb_key": "k", "language": "de-DE", "season_label": "Staffel", "sample_mb": "50",
    "min_age_minutes": "5", "exclude": "incomplete\n\n@eaDir \n", "cache_days": "7",
    "plex_url": "http://plex:32400", "plex_token": "tok",
    "plex_path_map": "/media/Filme_2D => /data/Filme\n", "auto_enabled": ["0", "1"],
    "poll_minutes": "20", "confirmed_trash_warning": "0",
}


def test_settings_page_shows_values(client):
    r = client.get("/settings")
    assert r.status_code == 200
    assert 'name="tmdb_key"' in r.text and "incomplete\nnzbs\n" in r.text
    assert "nzbs" in r.text and 'value="100"' in r.text  # Sample-Grenze in MB


def test_settings_save_and_folder_crud(client):
    r = client.post("/settings", data=SETTINGS_FORM, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/settings"
    st = client.env.load()
    assert (st.tmdb_key, st.language, st.season_label) == ("k", "de-DE", "Staffel")
    assert st.sample_bytes == 50 * 1_048_576 and st.min_age_minutes == 5 and st.cache_days == 7
    assert st.exclude == ["incomplete", "@eaDir"]
    assert st.plex_path_map == [("/media/Filme_2D", "/data/Filme")]
    assert st.auto_enabled is True and st.confirmed_trash_warning is False and st.poll_minutes == 20
    page = client.get("/settings").text
    assert "/media/Filme_2D =&gt; /data/Filme" in page

    r = client.post("/settings/folders", data={
        "path": "/x/Downloads/", "role": "input", "media_type": "auto", "auto_level": "auto",
        "movie_target_id": str(client.env.filme_id), "series_target_id": "", "movie3d_target_id": "",
    }, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/settings"
    assert "/x/Downloads" in client.get("/settings").text
    with client.app.state.sf() as s:
        added = s.scalars(select(Folder).where(Folder.path == "/x/Downloads")).one()
        assert (added.role, added.auto_level, added.movie_target_id, added.series_target_id) == (
            "input", "auto", client.env.filme_id, None)
        added_id = added.id

    r = client.post(f"/settings/folders/{added_id}/delete", follow_redirects=False)
    assert r.status_code == 303
    assert "/x/Downloads" not in client.get("/settings").text


def test_settings_checkbox_unchecked(client):
    client.env.settings(auto_enabled=True)
    client.post("/settings", data={**SETTINGS_FORM, "auto_enabled": "0"})
    assert client.env.load().auto_enabled is False


def test_settings_invalid_values_are_rejected(client):
    r = client.post("/settings", data={**SETTINGS_FORM, "poll_minutes": "0"})
    assert r.status_code == 400 and "Abfrageintervall" in r.text
    r = client.post("/settings", data={**SETTINGS_FORM, "min_age_minutes": "zehn"})
    assert r.status_code == 400 and "Mindestalter" in r.text and 'value="zehn"' in r.text
    r = client.post("/settings", data={**SETTINGS_FORM, "plex_path_map": "/a -> /b"})
    assert r.status_code == 400 and "=&gt;" in r.text
    assert client.env.load().tmdb_key == ""  # nichts gespeichert


def test_folder_validation(client):
    base = {"role": "library", "media_type": "movie", "auto_level": "off"}
    r = client.post("/settings/folders", data={**base, "path": "relativ"})
    assert r.status_code == 400 and "absoluten Pfad" in r.text
    r = client.post("/settings/folders", data={**base, "path": str(client.env.media / "Filme_2D")})
    assert r.status_code == 400 and "bereits eingetragen" in r.text
    r = client.post("/settings/folders", data={**base, "path": "/x/y", "role": "quatsch"})
    assert r.status_code == 400


def test_library_folder_ignores_targets(client):
    client.post("/settings/folders", data={
        "path": "/x/Lib", "role": "library", "media_type": "movie", "auto_level": "auto",
        "movie_target_id": str(client.env.filme_id)})
    with client.app.state.sf() as s:
        lib = s.scalars(select(Folder).where(Folder.path == "/x/Lib")).one()
        assert lib.movie_target_id is None and lib.auto_level == "off"


def test_deleting_library_clears_targets_and_open_items(client):
    env = client.env
    with env.app.state.sf() as s:
        s.add(Item(folder_id=env.downloads_id, source="/x/a.mkv", size=1, mtime=1.0, status="new"))
        s.add(Item(folder_id=env.downloads_id, source="/x/b.mkv", size=1, mtime=1.0, status="done"))
        s.commit()
    client.post(f"/settings/folders/{env.filme_id}/delete")
    client.post(f"/settings/folders/{env.downloads_id}/delete")
    with env.app.state.sf() as s:
        assert s.get(Folder, env.filme_id) is None
        assert [it.source for it in s.scalars(select(Item))] == ["/x/b.mkv"]
    assert client.post("/settings/folders/999/delete").status_code == 404


def test_deleting_library_unsets_target_of_input(client):
    env = client.env
    client.post(f"/settings/folders/{env.filme_id}/delete")
    with env.app.state.sf() as s:
        assert s.get(Folder, env.downloads_id).movie_target_id is None


def test_plex_test_button_suggests_mapping(client_with_plex):
    media = client_with_plex.env.media
    r = client_with_plex.post("/settings/plex/test", data={"plex_url": "http://plex:32400", "plex_token": "tok"})
    assert r.status_code == 200
    assert "Filme" in r.text and "Serien" in r.text
    assert f"{media}/Filme_2D → /media/Filme_2D" in r.text
    assert f"{media}/Serien → /media/Serien" in r.text
    assert "<html" in r.text  # ohne htmx: ganze Seite
    assert 'value="http://plex:32400"' in r.text  # eingegebene Werte bleiben stehen


def test_plex_test_keeps_unsaved_form_values(client_with_plex):
    r = client_with_plex.post("/settings/plex/test", data={**SETTINGS_FORM, "season_label": "Season"})
    assert 'value="Season"' in r.text
    assert client_with_plex.env.load().season_label == "Staffel"


def test_plex_test_htmx_fragment_and_errors(client):
    r = client.post("/settings/plex/test", data={"plex_url": "", "plex_token": ""},
                    headers={"HX-Request": "true"})
    assert "<html" not in r.text and "Plex-Adresse und Token" in r.text
    client.env.plex.reachable = False
    r = client.post("/settings/plex/test", data={"plex_url": "http://plex:32400", "plex_token": "tok"},
                    headers={"HX-Request": "true"})
    assert "Plex nicht erreichbar" in r.text


def test_path_map_helpers():
    assert parse_path_map(" /a => /b \n\n/c=>/d") == [("/a", "/b"), ("/c", "/d")]
    assert format_path_map([("/a", "/b")]) == "/a => /b"
    with pytest.raises(FormError):
        parse_path_map("/a => ")


# --- Nachbesserungen (Review-Runde 1) ------------------------------------------------------


def test_poll_loop_survives_interval_error():
    sleeps, triggered = [], []
    answers = iter([RuntimeError("Datenbank gesperrt"), 5, RuntimeError("wieder"), 7])

    def minutes():
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 4:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(poll_loop(minutes, lambda: triggered.append(True) or "gestartet", sleep=fake_sleep))
    assert sleeps == [900, 300, 300, 420]  # Standard 15, dann der zuletzt gelesene Wert
    assert len(triggered) == 3


def test_shutdown_survives_crashed_poll_task(tmp_path, monkeypatch):
    async def crashing(*args, **kwargs):
        raise RuntimeError("kaputt")

    monkeypatch.setattr(web_app, "poll_loop", crashing)
    app = create_app(tmp_path / "config", probe=lambda p: INFO, start_background=True)
    with TestClient(app) as c:
        assert c.get("/").status_code == 200
    thread = app.state.runner._thread
    assert thread is None or not thread.is_alive()


def auto_setup(env) -> None:
    env.settings(auto_enabled=True)
    with env.app.state.sf() as s:
        s.get(Folder, env.downloads_id).auto_level = "auto"
        s.commit()


def run_trigger(client) -> Job:
    assert client.app.state.watcher.trigger() == "gestartet"
    assert client.app.state.runner.wait_idle(10)
    with client.app.state.sf() as s:
        return s.scalars(select(Job).order_by(Job.id.desc())).first()


def test_auto_run_end_to_end(client_with_plex, media_tree):
    env = client_with_plex.env
    auto_setup(env)
    env.settings(plex_path_map=[(str(env.media / "Filme_2D"), "/media/Filme_2D")])
    first = run_trigger(client_with_plex)
    assert (first.kind, first.status, first.auto) == ("auto", "done", True)
    assert first.message == "1 Ordner geprüft, 2 neue Vorschläge, 0 automatisch einsortiert"
    second = run_trigger(client_with_plex)  # Dateien unverändert: jetzt stabil
    assert second.id != first.id and second.kind == "auto" and second.status == "done"
    assert second.message == "1 Ordner geprüft, 0 neue Vorschläge, 1 automatisch einsortiert"
    with env.app.state.sf() as s:
        assert len(s.scalars(select(Job)).all()) == 2
        assert all(scan.auto for scan in s.scalars(select(Scan)))
        matrix = s.scalars(select(Item).where(Item.tmdb_id == 603)).one()
        assert matrix.status == "done" and Path(matrix.target).is_file()
        history = s.scalars(select(History)).all()
        assert history and all(h.auto and h.job_id == second.id for h in history)
        assert s.scalars(select(FileState)).first() is not None
    assert ("1", "/media/Filme_2D/Matrix (1999)") in env.plex.refreshed
    assert "1 automatisch einsortiert" in client_with_plex.get("/").text


def test_auto_run_reports_plex_hint(client_with_plex, media_tree):
    env = client_with_plex.env
    auto_setup(env)
    env.settings(plex_path_map=[(str(env.media / "Filme_2D"), "/media/Filme_2D")])
    run_trigger(client_with_plex)
    env.plex.refresh_status = 500
    job = run_trigger(client_with_plex)
    assert job.status == "done"
    assert job.message.endswith("1 automatisch einsortiert; Plex-Fehler: HTTP 500")


def test_overview_plex_check_uses_short_timeout(client_with_plex):
    client_with_plex.get("/")
    assert client_with_plex.env.plex.timeouts and set(client_with_plex.env.plex.timeouts) == {3.0}


def test_overview_plex_http_error(client_with_plex):
    client_with_plex.env.plex.status = 401
    text = client_with_plex.get("/").text
    assert "Plex-Fehler: HTTP 401" in text and "nicht erreichbar" not in text


def test_job_fragment_requests_refresh_when_finished(client, media_tree):
    hx = {"HX-Request": "true"}
    release = block_runner(client)
    try:
        assert "HX-Refresh" not in client.get("/jobs/current", headers=hx).headers
    finally:
        release.set()
    assert client.app.state.runner.wait_idle(10)
    assert client.get("/jobs/current", headers=hx).headers.get("HX-Refresh") == "true"
    assert "HX-Refresh" not in client.get("/jobs/current").headers


def test_open_count_cell_has_label(client):
    assert 'data-label="Offene Vorschläge"' in client.get("/").text


EDIT = {"media_type": "movie", "auto_level": "auto", "movie_target_id": "", "series_target_id": "",
        "movie3d_target_id": ""}


def test_folder_edit_keeps_open_items(client):
    env = client.env
    with env.app.state.sf() as s:
        ser_id = s.scalars(select(Folder.id).where(Folder.path.endswith("Serien"))).one()
        s.add(Item(folder_id=env.downloads_id, source="/x/a.mkv", size=1, mtime=1.0, status="new"))
        s.commit()
    r = client.post(f"/settings/folders/{env.downloads_id}", follow_redirects=False, data={
        **EDIT, "series_target_id": str(ser_id), "path": "/anders", "role": "library"})
    assert r.status_code == 303 and r.headers["location"] == "/settings"
    with env.app.state.sf() as s:
        f = s.get(Folder, env.downloads_id)
        assert (f.path, f.role) == (str(env.media / "Downloads"), "input")  # nicht änderbar
        assert (f.media_type, f.auto_level) == ("movie", "auto")
        assert (f.movie_target_id, f.series_target_id, f.movie3d_target_id) == (None, ser_id, None)
        assert len(s.scalars(select(Item)).all()) == 1
    page = client.get("/settings").text
    assert f'action="/settings/folders/{env.downloads_id}"' in page


def test_folder_edit_library_and_errors(client):
    env = client.env
    client.post(f"/settings/folders/{env.filme_id}", data={**EDIT, "movie_target_id": str(env.filme_id)})
    with env.app.state.sf() as s:
        f = s.get(Folder, env.filme_id)
        assert (f.media_type, f.auto_level, f.movie_target_id) == ("movie", "off", None)
    assert client.post("/settings/folders/999", data=EDIT).status_code == 404
    r = client.post(f"/settings/folders/{env.downloads_id}", data={**EDIT, "auto_level": "immer"})
    assert r.status_code == 400 and "Automatik" in r.text


def test_folder_changes_refused_while_busy(client):
    env = client.env
    release = block_runner(client)
    try:
        r = client.post(f"/settings/folders/{env.filme_id}/delete")
        assert r.status_code == 409 and "läuft gerade ein Auftrag" in r.text
        r = client.post(f"/settings/folders/{env.downloads_id}", data=EDIT)  # Ziele würden entfallen
        assert r.status_code == 409 and "läuft gerade ein Auftrag" in r.text
        with env.app.state.sf() as s:
            current = s.get(Folder, env.downloads_id)
            same_targets = {k: str(getattr(current, k)) for k in
                            ("movie_target_id", "series_target_id", "movie3d_target_id")}
        r = client.post(f"/settings/folders/{env.downloads_id}", follow_redirects=False,
                        data={**EDIT, **same_targets, "auto_level": "detect"})
        assert r.status_code == 303  # Automatik-Stufe darf sich ändern
    finally:
        release.set()
    assert client.app.state.runner.wait_idle(10)
    with env.app.state.sf() as s:
        assert s.get(Folder, env.filme_id) is not None
        assert s.get(Folder, env.downloads_id).auto_level == "detect"


def test_delete_button_asks_for_confirmation(client):
    assert 'onsubmit="return confirm(' in client.get("/settings").text


# --- Abschlussprüfung I4: Ordner dürfen nicht ineinander liegen ------------------------------

FOLDER_FORM = {"role": "input", "media_type": "auto", "auto_level": "off"}


@pytest.mark.parametrize("rel", ["", "Downloads/Neu", "Filme_2D/Unterordner", "downloads/neu", "Serien/../Serien/X"])
def test_overlapping_folder_is_rejected(client, rel):
    path = str(client.env.media / rel) if rel else str(client.env.media)
    for role in ("input", "library"):
        r = client.post("/settings/folders", data={**FOLDER_FORM, "role": role, "path": path})
        assert r.status_code == 400 and "überschneidet sich" in r.text
    with client.app.state.sf() as s:
        assert s.scalar(select(func.count(Folder.id))) == 4


@pytest.mark.parametrize("prefix", ["/", "//"])
def test_overlapping_folder_with_extra_slashes_is_rejected(client, prefix):
    # Abschlussprüfung R2: „//media“ ist für normpath nicht dasselbe wie „/media“
    for path in (prefix + str(client.env.media), prefix + str(client.env.media / "Filme_2D") + "//X"):
        r = client.post("/settings/folders", data={**FOLDER_FORM, "path": path})
        assert r.status_code == 400 and "überschneidet sich" in r.text
    with client.app.state.sf() as s:
        assert s.scalar(select(func.count(Folder.id))) == 4


@pytest.mark.parametrize("path", ["//media", "///media", "/media//Downloads/X", "//media//Downloads//"])
def test_overlap_error_collapses_repeated_slashes(path):
    from mediasort.web.forms import overlap_error

    assert overlap_error(path, ["/media/Downloads"]) is not None
    assert overlap_error("/media/Downloads", [path]) is not None


def test_overlap_error_sibling_with_extra_slashes_is_no_overlap():
    from mediasort.web.forms import clean_path, overlap_error

    assert clean_path("//media/Filme_2D") == "/media/Filme_2D"
    assert overlap_error("//media/Filme_2D", ["/media/Downloads"]) is None
    assert overlap_error("//media/Filme_2D", ["/media/Filme_2D"]) is not None


def test_new_folder_path_is_stored_with_single_slashes(client):
    r = client.post("/settings/folders", data={**FOLDER_FORM, "path": "/" + str(client.env.media / "Neu") + "//"},
                    follow_redirects=False)
    assert r.status_code == 303
    with client.app.state.sf() as s:
        assert s.scalar(select(Folder.path).order_by(Folder.id.desc())) == str(client.env.media / "Neu")


def test_sibling_folder_is_accepted(client):
    r = client.post("/settings/folders", data={**FOLDER_FORM, "path": str(client.env.media / "Downloads2")},
                    follow_redirects=False)
    assert r.status_code == 303


def test_editing_overlapping_folder_is_rejected(client):
    with client.app.state.sf() as s:
        inner = Folder(path=str(client.env.media / "Downloads" / "Teil"), role="input", media_type="auto")
        s.add(inner)
        s.commit()
        inner_id = inner.id
    r = client.post(f"/settings/folders/{inner_id}", data={**FOLDER_FORM, "media_type": "movie"})
    assert r.status_code == 400 and "überschneidet sich" in r.text
    with client.app.state.sf() as s:
        assert s.get(Folder, inner_id).media_type == "auto"


def test_overlapping_folder_can_still_change_automation(client):
    # Abschlussprüfung R5: ein Altbestand mit ineinander liegenden Ordnern muss sich abschalten lassen
    with client.app.state.sf() as s:
        inner = Folder(path=str(client.env.media / "Downloads" / "Teil"), role="input", media_type="auto",
                       auto_level="auto")
        s.add(inner)
        s.commit()
        inner_id = inner.id
    r = client.post(f"/settings/folders/{inner_id}", data={**FOLDER_FORM, "auto_level": "off"},
                    follow_redirects=False)
    assert r.status_code == 303
    with client.app.state.sf() as s:
        assert s.get(Folder, inner_id).auto_level == "off"
    r = client.post(f"/settings/folders/{inner_id}", data={**FOLDER_FORM, "auto_level": "off"},
                    follow_redirects=False)
    assert r.status_code == 303  # unverändert lassen geht auch
    # Einschalten und jede andere Änderung: weiter die Prüfung auf Überschneidung
    for data in ({"auto_level": "detect"}, {"auto_level": "auto"}, {"auto_level": "off", "media_type": "movie"}):
        r = client.post(f"/settings/folders/{inner_id}", data={**FOLDER_FORM, **data})
        assert r.status_code == 400 and "überschneidet sich" in r.text
    with client.app.state.sf() as s:
        folder = s.get(Folder, inner_id)
        assert (folder.auto_level, folder.media_type) == ("off", "auto")


def test_scan_accepts_only_input_folders(client, media_tree):
    r = client.post(f"/scan/{client.env.filme_id}", follow_redirects=False)
    assert r.status_code == 400 and "Bibliothek prüfen" in r.text
    with client.app.state.sf() as s:
        assert s.scalar(select(func.count(Job.id))) == 0


# --- Abschlussprüfung, kleinere Punkte --------------------------------------------------------


def test_cross_site_post_is_rejected(client):
    for headers in ({"Origin": "http://evil.example"}, {"Origin": "null"},
                    {"Referer": "http://evil.example/seite"}, {"Origin": "http://testserver:8081"}):
        r = client.post("/settings", data=SETTINGS_FORM, headers=headers, follow_redirects=False)
        assert r.status_code == 403 and "fremden Seite" in r.text
    assert client.env.load().tmdb_key == ""
    assert client.get("/settings", headers={"Origin": "http://evil.example"}).status_code == 200


def test_same_origin_post_is_allowed(client):
    for headers in ({"Origin": "http://testserver"}, {"Origin": "http://testserver:80"},
                    {"Referer": "http://testserver/settings"}, {}):
        r = client.post("/settings", data=SETTINGS_FORM, headers=headers, follow_redirects=False)
        assert r.status_code == 303


def test_same_origin_check_handles_ports_and_proxies():
    from mediasort.web.app import same_origin

    assert same_origin("http://nas:8080", "nas:8080")
    assert same_origin("https://media.example", "media.example")  # TLS-Proxy: Host ohne Port
    assert same_origin("http://[::1]:8080", "[::1]:8080")
    assert not same_origin("http://nas:8080", "nas:8090")
    assert not same_origin("http://other:8080", "nas:8080")
    assert not same_origin("null", "nas:8080") and not same_origin("http://nas:x", "nas:8080")


def test_stale_running_scans_are_marked_on_startup(tmp_path):
    app = create_app(tmp_path / "config", start_background=False)
    with app.state.sf() as s:
        s.add_all([Scan(status="running", errors_json=["Ordner nicht lesbar: /x"]), Scan(status="done")])
        s.commit()
    with TestClient(app):
        with app.state.sf() as s:
            stale, done = s.scalars(select(Scan).order_by(Scan.id)).all()
    assert stale.status == "error" and stale.finished is not None
    assert stale.errors_json == ["Ordner nicht lesbar: /x", "abgebrochen (Neustart)"]
    assert done.status == "done"


@pytest.mark.parametrize("number", [2**31, 2**63, 10**30])
def test_huge_ids_are_404(client, number):
    assert client.post(f"/scan/{number}", follow_redirects=False).status_code == 404
    assert client.post(f"/settings/folders/{number}", data={}, follow_redirects=False).status_code == 404
    assert client.post(f"/settings/folders/{number}/delete", follow_redirects=False).status_code == 404
    for action in ("toggle", "ignore", "replan"):
        assert client.post(f"/items/{number}/{action}", follow_redirects=False).status_code == 404
    assert client.post(f"/items/{number}/decision", data={"decision": "skip"}).status_code == 404
    assert client.post(f"/items/{number}/correct", data={}).status_code == 404
    assert client.get(f"/items/{number}/correct").status_code == 404
    assert client.get(f"/items/{number}/search", params={"q": "x"}).status_code == 404


def test_folder_target_and_correction_numbers_reject_huge_values():
    from starlette.datastructures import FormData

    from mediasort.web.forms import parse_correction_form, parse_folder_options

    for raw in ("9" * 5000, "²", "1234567890"):
        with pytest.raises(FormError):
            parse_folder_options(FormData({"movie_target_id": raw}), "input", {1})
    for field in ("season", "episodes"):
        with pytest.raises(FormError):
            parse_correction_form(FormData({field: "9" * 5000}))
    with pytest.raises(FormError):
        parse_correction_form(FormData({"episodes": "1, " + "9" * 5000}))
