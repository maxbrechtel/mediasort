import threading
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from mediasort.db import Folder, History, Ignored, Item, LibraryEntry, Scan
from test_web_main import Env, PlexServer

MATRIX_WRONG = "Filme_2D/Alpha/The.Matrix.1999.1080p.BluRay.x264-GRP.mkv"
UNKNOWN_A = "Filme_2D/Alpha/Voellig.Unbekannt.2020.1080p.mkv"
UNKNOWN_M = "Filme_2D/Mango/Nochmal.Unbekannt.2021.1080p.mkv"
UNKNOWN_Z = "Filme_2D/Zulu/Ganz.Anders.2019.1080p.mkv"
UNKNOWN_DIGIT = "Filme_2D/2001 Odyssee/Weltraum.Unbekannt.1968.1080p.mkv"


def wait(client) -> None:
    assert client.env.app.state.runner.wait_idle(10)


def sources(env: Env) -> set[str]:
    with env.app.state.sf() as s:
        return {i.source.removeprefix(str(env.media) + "/") for i in s.scalars(select(Item))}


def scan(client, **data):
    data.setdefault("folder_id", client.env.filme_id)
    r = client.post("/library/scan", data=data, follow_redirects=False)
    wait(client)
    return r


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path, PlexServer())


@pytest.fixture
def client(env):
    env.settings(tmdb_key="k", min_age_minutes=0, sample_bytes=0)
    with TestClient(env.app) as c:
        c.env = env
        yield c


@pytest.fixture
def client_with_plex(env):
    env.settings(tmdb_key="k", min_age_minutes=0, sample_bytes=0,
                 plex_url="http://plex:32400", plex_token="tok")
    with TestClient(env.app) as c:
        c.env = env
        yield c


@pytest.fixture
def library_tree(env):
    for rel in (MATRIX_WRONG, UNKNOWN_A, UNKNOWN_M, UNKNOWN_Z, UNKNOWN_DIGIT):
        env.file(rel)
    env.file("Filme_2D/Alpha/readme.mkv")  # zählt nur über seinen obersten Ordner


@pytest.fixture
def history_rows(env):
    rows = [
        History(ts=1_700_000_000.0, action="moved", old_path="/in/Matrix.mkv", new_path="/lib/Matrix (1999)/m.mkv", auto=False),
        History(ts=1_700_000_100.0, action="deleted", old_path="/lib/Alt/Film.mkv", new_path=None, auto=True),
        History(ts=1_700_000_200.0, action="folder_deleted", old_path="/in/Matrix.leer", new_path=None, auto=True),
        History(ts=1_700_000_300.0, action="moved", old_path="/in/Anderer.mkv", new_path="/lib/Anderer (2000)/a.mkv", auto=True),
    ]
    with env.app.state.sf() as s:
        s.add_all(rows)
        s.commit()


@pytest.fixture
def ignored_row(env):
    path = env.file("Downloads/Voellig.Unbekannt.2020.1080p.mkv")
    with env.app.state.sf() as s:
        row = Ignored(path=str(path), size=5 * 1024 * 1024, created=1_700_000_000.0)
        s.add(row)
        s.commit()
        return row.id, str(path)


# --- Bibliothek prüfen --------------------------------------------------------------------


def test_library_page_lists_libraries_and_defaults(client):
    r = client.get("/library")
    assert r.status_code == 200
    assert "Filme_2D" in r.text and "Filme_3D" in r.text and "Downloads" not in r.text
    assert 'name="from_letter"' in r.text and 'value="A"' in r.text and 'value="Z"' in r.text
    assert "Ich habe die Warnung gelesen" not in r.text  # Plex nicht eingerichtet


def test_library_scan_requires_trash_confirmation(client_with_plex, library_tree):
    client = client_with_plex
    r = scan(client)
    assert r.status_code == 400 and "Papierkorb" in r.text and "Ich habe die Warnung gelesen" in r.text
    assert sources(client.env) == set()
    assert not client.env.load().confirmed_trash_warning


def test_library_scan_with_confirmation_is_remembered(client_with_plex, library_tree):
    client = client_with_plex
    r = scan(client, confirm_trash_warning="on", from_letter="A", to_letter="C")
    assert r.status_code == 303 and r.headers["location"] == "/library"
    assert client.env.load().confirmed_trash_warning
    assert UNKNOWN_A in sources(client.env)
    assert "Ich habe die Warnung gelesen" not in client.get("/library").text
    assert scan(client, from_letter="M", to_letter="M").status_code == 303  # ohne erneute Frage


def test_library_scan_without_auto_empty_trash_needs_no_confirmation(env, library_tree):
    env.plex.auto_empty_trash = False
    env.settings(tmdb_key="k", min_age_minutes=0, sample_bytes=0, plex_url="http://plex:32400", plex_token="t")
    with TestClient(env.app) as client:
        client.env = env
        assert "Ich habe die Warnung gelesen" not in client.get("/library").text
        assert scan(client).status_code == 303
        assert not env.load().confirmed_trash_warning


def test_library_scan_plex_unreachable_does_not_block(env, library_tree):
    env.plex.reachable = False
    env.settings(tmdb_key="k", min_age_minutes=0, sample_bytes=0, plex_url="http://plex:32400", plex_token="t")
    with TestClient(env.app) as client:
        client.env = env
        r = scan(client)
        assert r.status_code == 303 and r.headers["location"] == "/library?hinweis=plex"
        page = client.get(r.headers["location"])
        assert "nicht erreichbar" in page.text
        assert UNKNOWN_A in sources(env)


def test_library_scan_letter_range(client, library_tree):
    r = scan(client, from_letter="A", to_letter="C")
    assert r.status_code == 303
    assert sources(client.env) == {MATRIX_WRONG, UNKNOWN_A, "Filme_2D/Alpha/readme.mkv"}
    with client.env.app.state.sf() as s:
        row = s.scalars(select(Scan).order_by(Scan.id.desc())).first()
        assert row.subset == "A–C" and row.status == "done"


def test_library_scan_defaults_to_all_letters_and_digits_are_zero(client, library_tree):
    scan(client)
    assert {UNKNOWN_A, UNKNOWN_M, UNKNOWN_Z}.issubset(sources(client.env))
    assert UNKNOWN_DIGIT not in sources(client.env)  # „0“ liegt vor A
    scan(client, from_letter="0", to_letter="0")
    assert UNKNOWN_DIGIT in sources(client.env)
    with client.env.app.state.sf() as s:
        labels = [r.subset for r in s.scalars(select(Scan).order_by(Scan.id))]
    assert labels == ["A–Z", "0"]


def test_library_scan_single_folder(client, library_tree):
    r = scan(client, mode="folder", subfolder="Mango")
    assert r.status_code == 303
    assert sources(client.env) == {UNKNOWN_M}
    with client.env.app.state.sf() as s:
        assert s.scalars(select(Scan.subset)).all() == ["Ordner: Mango"]
    # ohne ``mode`` genügt das Feld ``subfolder``
    assert scan(client, subfolder="Zulu").status_code == 303
    assert UNKNOWN_Z in sources(client.env)


@pytest.mark.parametrize("data", [
    {"mode": "folder", "subfolder": "Gibtsnicht"},
    {"mode": "folder", "subfolder": ""},
    {"mode": "folder", "subfolder": "../Filme_3D"},
    {"mode": "folder", "subfolder": "Alpha/.."},
    {"mode": "letters", "from_letter": "AB", "to_letter": "C"},
    {"mode": "letters", "from_letter": "!", "to_letter": "C"},
    {"mode": "letters", "from_letter": "D", "to_letter": "B"},
    {"folder_id": "9999"},
    {"folder_id": "abc"},
])
def test_library_scan_validation(client, library_tree, data):
    r = scan(client, **data)
    assert r.status_code in (400, 404)
    assert sources(client.env) == set()
    with client.env.app.state.sf() as s:
        assert s.scalar(select(func.count(Scan.id))) == 0


def test_library_scan_rejects_input_folder(client, library_tree):
    r = scan(client, folder_id=client.env.downloads_id)
    assert r.status_code == 400 and "Bibliothek" in r.text


def test_library_scan_refreshes_index(client, library_tree):
    with client.env.app.state.sf() as s:
        assert s.scalar(select(func.count(LibraryEntry.id))) == 0
    scan(client, from_letter="A", to_letter="C")
    with client.env.app.state.sf() as s:
        assert s.scalar(select(func.count(LibraryEntry.id))) > 0


def test_library_scan_busy_shows_notice(client, library_tree):
    runner = client.env.app.state.runner
    gate = threading.Event()
    runner.submit("scan", lambda job_id, progress: gate.wait(10))
    try:
        r = client.post("/library/scan", data={"folder_id": client.env.filme_id}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/library?hinweis=busy"
        assert "Es läuft bereits ein Auftrag" in client.get(r.headers["location"]).text
        r = client.post("/library/scan", data={"folder_id": client.env.filme_id},
                        headers={"HX-Request": "true"})
        assert r.status_code == 200 and "Es läuft bereits ein Auftrag" in r.text
    finally:
        gate.set()
        wait(client)
    assert sources(client.env) == set()


def test_library_page_lists_recent_scans(client, library_tree):
    scan(client, from_letter="A", to_letter="C")
    scan(client, mode="folder", subfolder="Mango")
    page = client.get("/library").text
    assert "A–C" in page and "Ordner: Mango" in page and 'href="/review"' in page
    assert "fertig" in page and "Filme_2D" in page


# --- Verlauf ---------------------------------------------------------------------------------


def test_history_lists_newest_first(client, history_rows):
    r = client.get("/history")
    assert r.status_code == 200
    text = r.text
    assert text.index("/in/Anderer.mkv") < text.index("/in/Matrix.leer") < text.index("/lib/Alt/Film.mkv") \
        < text.index("/in/Matrix.mkv")
    assert "verschoben" in text and "gelöscht" in text and "Ordner gelöscht" in text
    assert "manuell" in text and "automatisch" in text


def test_history_search_and_auto_filter(client, history_rows):
    r = client.get("/history", params={"q": "matrix"})  # unabhängig von Groß-/Kleinschreibung, auch im neuen Pfad
    assert "/in/Matrix.mkv" in r.text and "/in/Matrix.leer" in r.text and "Anderer" not in r.text
    r = client.get("/history", params={"q": "(1999)"})
    assert "/in/Matrix.mkv" in r.text and "/in/Matrix.leer" not in r.text
    r = client.get("/history", params={"auto": "ja"})
    assert "/in/Anderer.mkv" in r.text and "/lib/Alt/Film.mkv" in r.text and "/in/Matrix.mkv" not in r.text
    r = client.get("/history", params={"auto": "nein"})
    assert "/in/Matrix.mkv" in r.text and "/in/Anderer.mkv" not in r.text
    r = client.get("/history", params={"q": "matrix", "auto": "ja"})
    assert "/in/Matrix.leer" in r.text and "/in/Matrix.mkv" not in r.text
    assert "Keine Einträge" in client.get("/history", params={"q": "nichtda"}).text


def test_history_search_treats_wildcards_literally(client, history_rows):
    assert "Keine Einträge" in client.get("/history", params={"q": "%"}).text
    assert "Keine Einträge" in client.get("/history", params={"q": "_"}).text


def test_history_paginates_100_per_page(client):
    with client.env.app.state.sf() as s:
        s.add_all(History(ts=float(n), action="moved", old_path=f"/in/f{n:03}.mkv", new_path=f"/lib/f{n:03}.mkv")
                  for n in range(1, 251))
        s.commit()
    first = client.get("/history").text
    assert first.count("<tr") - 1 == 100 and "/in/f250.mkv" in first and "/in/f151.mkv" in first
    assert "/in/f150.mkv" not in first
    second = client.get("/history", params={"page": "2"}).text
    assert "/in/f150.mkv" in second and "/in/f051.mkv" in second and "/in/f050.mkv" not in second
    third = client.get("/history", params={"page": "3"}).text
    assert third.count("<tr") - 1 == 50
    assert client.get("/history", params={"page": "99"}).text.count("<tr") - 1 == 50  # begrenzt
    assert client.get("/history", params={"page": "x"}).status_code == 200
    assert "q=" in client.get("/history", params={"q": "f", "page": "2"}).text  # Suche bleibt beim Blättern


# --- Ignoriert -------------------------------------------------------------------------------


def test_ignored_list(client, ignored_row):
    _, path = ignored_row
    r = client.get("/ignored")
    day = datetime.fromtimestamp(1_700_000_000.0).strftime("%d.%m.%Y")
    assert r.status_code == 200 and path in r.text and "5,0 MB" in r.text and day in r.text


def test_ignored_empty(client):
    assert "Keine ignorierten Dateien" in client.get("/ignored").text


def test_unignore(client, ignored_row):
    row_id, path = ignored_row
    env = client.env
    env.settings(tmdb_key="k", min_age_minutes=0, sample_bytes=0)
    with env.app.state.sf() as s:
        downloads = s.get(Folder, env.downloads_id)
    env.app.state.services.plan(downloads, False)
    assert sources(env) == set()  # ignorierte Datei wird übersehen

    r = client.post(f"/ignored/{row_id}/remove", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/ignored"
    with env.app.state.sf() as s:
        assert s.scalars(select(Ignored)).all() == []
    assert path not in client.get("/ignored").text

    env.app.state.services.plan(downloads, False)
    assert sources(env) == {"Downloads/Voellig.Unbekannt.2020.1080p.mkv"}


def test_unignore_after_ignore_in_review_finds_file_again(client):
    env = client.env
    env.file("Downloads/Voellig.Unbekannt.2020.1080p.mkv")
    with env.app.state.sf() as s:
        downloads = s.get(Folder, env.downloads_id)
    env.app.state.services.plan(downloads, False)
    with env.app.state.sf() as s:
        item = s.scalars(select(Item)).one()
    assert client.post(f"/items/{item.id}/ignore", follow_redirects=False).status_code == 303
    with env.app.state.sf() as s:
        row = s.scalars(select(Ignored)).one()
    assert client.post(f"/ignored/{row.id}/remove", follow_redirects=False).status_code == 303
    env.app.state.services.plan(downloads, False)
    with env.app.state.sf() as s:
        statuses = [i.status for i in s.scalars(select(Item).order_by(Item.id))]
    assert statuses == ["ignored", "new"]  # alter Vorschlag bleibt als Historie, neuer entsteht


def test_unignore_unknown_id(client):
    assert client.post("/ignored/999/remove", follow_redirects=False).status_code == 404
