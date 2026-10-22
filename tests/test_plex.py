import json
from pathlib import Path

import httpx
import pytest

from mediasort.plex import (
    PlexClient,
    PlexError,
    PlexSection,
    map_path,
    refresh_touched,
    suggest_mapping,
)

FIXTURES = Path(__file__).parent / "fixtures" / "plex"

SECTIONS = [
    PlexSection("1", "Filme", "movie", ("/media/Filme_2D",)),
    PlexSection("2", "Serien", "show", ("/media/Serien",)),
]


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def seen():
    return []


@pytest.fixture
def client(seen):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/library/sections":
            return httpx.Response(200, json=fixture("sections.json"))
        if request.url.path == "/:/prefs":
            return httpx.Response(200, json=fixture("prefs.json"))
        if request.url.path.endswith("/refresh"):
            return httpx.Response(200)
        return httpx.Response(404)

    return PlexClient("http://plex:32400", "tok", transport=httpx.MockTransport(handler))


def refreshed(seen) -> list[tuple[str, str]]:
    return [
        (r.url.path.split("/")[3], r.url.params["path"]) for r in seen if r.url.path.endswith("/refresh")
    ]


def client_with(handler, base_url="http://plex:32400") -> PlexClient:
    return PlexClient(base_url, "tok", transport=httpx.MockTransport(handler))


def test_sections(client):
    s = client.sections()
    assert [(x.key, x.locations) for x in s] == [("1", ("/media/Filme_2D",)), ("2", ("/media/Serien",))]
    assert [(x.title, x.type) for x in s] == [("Filme", "movie"), ("Serien", "show")]


def test_auto_empty_trash(client):
    assert client.auto_empty_trash() is True


def test_auto_empty_trash_false_value():
    prefs = {"MediaContainer": {"Setting": [{"id": "autoEmptyTrash", "value": False}]}}
    assert client_with(lambda r: httpx.Response(200, json=prefs)).auto_empty_trash() is False


def test_auto_empty_trash_missing_setting_is_false():
    prefs = {"MediaContainer": {"Setting": [{"id": "FriendlyName", "value": "x"}]}}
    assert client_with(lambda r: httpx.Response(200, json=prefs)).auto_empty_trash() is False


def test_token_and_accept_header_sent(client, seen):
    client.sections()
    assert seen[0].headers["X-Plex-Token"] == "tok"
    assert seen[0].headers["Accept"] == "application/json"


def test_trailing_slash_in_base_url_is_stripped():
    urls = []

    def handler(request):
        urls.append(str(request.url))
        return httpx.Response(200, json=fixture("sections.json"))

    client_with(handler, "http://plex:32400/").sections()
    assert urls == ["http://plex:32400/library/sections"]


def test_refresh_request(client, seen):
    client.refresh("1", "/media/Filme_2D/Matrix (1999)")
    assert seen[0].method == "GET"
    assert seen[0].url.path == "/library/sections/1/refresh"
    assert seen[0].url.params["path"] == "/media/Filme_2D/Matrix (1999)"


def test_http_error_raises_plex_error():
    with pytest.raises(PlexError, match="Plex-Fehler: HTTP 401") as info:
        client_with(lambda r: httpx.Response(401)).sections()
    assert info.value.status_code == 401


def test_invalid_json_raises_plex_error():
    with pytest.raises(PlexError):
        client_with(lambda r: httpx.Response(200, text="<html>")).sections()


def test_map_path():
    assert map_path(Path("/data/Filme_2D/X"), [("/data", "/media")]) == "/media/Filme_2D/X"


def test_map_path_longest_prefix_wins():
    mapping = [("/data", "/media"), ("/data/Filme_2D", "/plex/movies")]
    assert map_path(Path("/data/Filme_2D/X"), mapping) == "/plex/movies/X"
    assert map_path(Path("/data/Serien/Y"), mapping) == "/media/Serien/Y"


def test_map_path_is_component_wise():
    assert map_path(Path("/data2/X"), [("/data", "/media")]) == "/data2/X"


def test_map_path_unmatched_and_exact():
    assert map_path(Path("/other/X"), [("/data", "/media")]) == "/other/X"
    assert map_path(Path("/data"), [("/data/", "/media/")]) == "/media"
    assert map_path(Path("/a/b"), []) == "/a/b"


def test_suggest_mapping_identical():
    assert suggest_mapping([Path("/media/Filme_2D")], SECTIONS) == [("/media/Filme_2D", "/media/Filme_2D")]


def test_suggest_mapping_by_last_component():
    roots = [Path("/mnt/nas/Filme_2D"), Path("/mnt/nas/Serien"), Path("/mnt/nas/Filme_3D")]
    assert suggest_mapping(roots, SECTIONS) == [
        ("/mnt/nas/Filme_2D", "/media/Filme_2D"),
        ("/mnt/nas/Serien", "/media/Serien"),
    ]


def test_suggest_mapping_prefers_identical_path():
    sections = [PlexSection("1", "A", "movie", ("/x/Filme", "/media/Filme"))]
    assert suggest_mapping([Path("/media/Filme")], sections) == [("/media/Filme", "/media/Filme")]


def test_suggest_mapping_nothing_matches():
    assert suggest_mapping([Path("/media/Downloads")], SECTIONS) == []


def test_refresh_touched(client, seen):
    msgs = refresh_touched(
        client,
        [
            Path("/media/Filme_2D/Matrix (1999)/x.mkv"),
            Path("/media/Filme_2D/Matrix (1999)"),
            Path("/media/Filme_3D/Avatar (2009)"),
            Path("/media/Downloads/Rel"),
        ],
        [("/media", "/media")],
        [Path("/media/Filme_2D"), Path("/media/Filme_3D")],
    )
    assert msgs == [] and refreshed(seen) == [("1", "/media/Filme_2D/Matrix (1999)")]


def test_refresh_touched_maps_paths_and_is_ordered(client, seen):
    msgs = refresh_touched(
        client,
        [Path("/data/Serien/Zeta/Staffel 1/e.mkv"), Path("/data/Filme_2D/Alpha (2000)"), Path("/data/Serien/Beta")],
        [("/data", "/media")],
        [Path("/data/Filme_2D"), Path("/data/Serien")],
    )
    assert msgs == []
    assert refreshed(seen) == [
        ("1", "/media/Filme_2D/Alpha (2000)"),
        ("2", "/media/Serien/Beta"),
        ("2", "/media/Serien/Zeta"),
    ]


def test_refresh_touched_library_root_refreshes_root(client, seen):
    refresh_touched(client, [Path("/media/Serien")], [], [Path("/media/Serien")])
    assert refreshed(seen) == [("2", "/media/Serien")]


def test_refresh_touched_section_location_prefix_is_component_wise(seen):
    sections = {
        "MediaContainer": {
            "Directory": [
                {"key": "9", "title": "X", "type": "movie", "Location": [{"id": 9, "path": "/media/Filme"}]}
            ]
        }
    }

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=sections)

    refresh_touched(
        client_with(handler), [Path("/media/Filme_2D/Matrix (1999)")], [], [Path("/media/Filme_2D")]
    )
    assert refreshed(seen) == []


def test_refresh_touched_nothing_to_do_makes_no_request(client, seen):
    assert refresh_touched(client, [Path("/media/Downloads/Rel")], [], [Path("/media/Filme_2D")]) == []
    assert seen == []


def test_unreachable_gives_hint():
    def handler(request):
        raise httpx.ConnectError("Connection refused")

    msgs = refresh_touched(
        client_with(handler), [Path("/media/Filme_2D/A (2000)")], [], [Path("/media/Filme_2D")]
    )
    assert len(msgs) == 1 and msgs[0].startswith("Plex nicht erreichbar: ")
    assert "Connection refused" in msgs[0]


def test_refresh_http_error_hint_and_continues(seen):
    def handler(request):
        seen.append(request)
        if request.url.path == "/library/sections":
            return httpx.Response(200, json=fixture("sections.json"))
        if request.url.params["path"].endswith("Bad"):
            return httpx.Response(500)
        return httpx.Response(200)

    msgs = refresh_touched(
        client_with(handler),
        [Path("/media/Filme_2D/Bad"), Path("/media/Filme_2D/Good")],
        [],
        [Path("/media/Filme_2D")],
    )
    assert msgs == ["Plex-Fehler: HTTP 500"]
    assert refreshed(seen) == [("1", "/media/Filme_2D/Bad"), ("1", "/media/Filme_2D/Good")]


def test_sections_failure_gives_single_hint(seen):
    def handler(request):
        seen.append(request)
        return httpx.Response(401)

    msgs = refresh_touched(
        client_with(handler),
        [Path("/media/Filme_2D/A"), Path("/media/Filme_2D/B")],
        [],
        [Path("/media/Filme_2D")],
    )
    assert msgs == ["Plex-Fehler: HTTP 401"]
    assert len(seen) == 1
