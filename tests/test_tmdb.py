import json
from collections import Counter
from pathlib import Path

import httpx
import pytest

from mediasort.db import DbCache
from mediasort.models import MediaKind
from mediasort.tmdb import TmdbClient, TmdbError

FIXTURES = Path(__file__).parent / "fixtures" / "tmdb"


def fixture_response(request: httpx.Request) -> httpx.Response:
    path = request.url.path.removeprefix("/3/")
    params = request.url.params
    name = path.replace("/", "_")
    if path.startswith("search/"):
        name += "_" + params["query"].lower().replace(" ", "_")
    for candidate in (f"{name}_{params.get('language')}", name):
        file = FIXTURES / f"{candidate}.json"
        if file.exists():
            return httpx.Response(200, json=json.loads(file.read_text(encoding="utf-8")))
    return httpx.Response(404, json={"status_message": "The resource you requested could not be found."})


@pytest.fixture
def calls():
    return Counter()


@pytest.fixture
def requests_seen():
    return []


def make_client(handler, **kwargs) -> TmdbClient:
    return TmdbClient("key", transport=httpx.MockTransport(handler), sleep=lambda _s: None, **kwargs)


@pytest.fixture
def client(calls, requests_seen, sessionmaker_):
    def handler(request: httpx.Request) -> httpx.Response:
        calls[request.url.path.removeprefix("/3/")] += 1
        requests_seen.append(request)
        return fixture_response(request)

    return make_client(handler, cache=DbCache(sessionmaker_))


def test_search_merges_english_title(client):
    c = client.search(MediaKind.MOVIE, "Matrix", 1999)[0]
    assert (c.tmdb_id, c.title, c.english_title, c.year) == (603, "Matrix", "The Matrix", 1999)
    assert (c.kind, c.original_title, c.popularity) == (MediaKind.MOVIE, "The Matrix", 85.4)


def test_search_keeps_order_of_configured_language(client):
    assert [c.tmdb_id for c in client.search(MediaKind.MOVIE, "Matrix", None)] == [603, 604]


def test_search_sends_year_and_languages(client, requests_seen):
    client.search(MediaKind.MOVIE, "Matrix", 1999)
    sent = {(r.url.path, r.url.params["language"], r.url.params["year"]) for r in requests_seen}
    assert sent == {("/3/search/movie", "de-DE", "1999"), ("/3/search/movie", "en-US", "1999")}


def test_search_series_uses_tv_endpoint_and_air_date_year(client, requests_seen):
    c = client.search(MediaKind.EPISODE, "Breaking Bad", 2008)[0]
    assert (c.tmdb_id, c.kind, c.title, c.year) == (1396, MediaKind.EPISODE, "Breaking Bad", 2008)
    assert {r.url.path for r in requests_seen} == {"/3/search/tv"}
    assert all(r.url.params["first_air_date_year"] == "2008" for r in requests_seen)


def test_search_without_year_retry(requests_seen):
    def handler(request):
        requests_seen.append(request)
        if "year" in request.url.params:
            return httpx.Response(200, json={"page": 1, "results": [], "total_results": 0})
        return fixture_response(request)

    result = make_client(handler).search(MediaKind.MOVIE, "Matrix", 1850)
    assert [c.tmdb_id for c in result] == [603, 604]
    assert [("year" in r.url.params, r.url.params["language"]) for r in requests_seen] == [
        (True, "de-DE"), (True, "en-US"), (False, "de-DE"), (False, "en-US")]


def test_search_without_year_does_not_retry_again(requests_seen):
    def handler(request):
        requests_seen.append(request)
        return httpx.Response(200, json={"page": 1, "results": []})

    assert make_client(handler).search(MediaKind.MOVIE, "Nichts", None) == []
    assert len(requests_seen) == 2


def test_search_falls_back_to_english_title_and_includes_english_only_hits():
    def handler(request):
        lang = request.url.params["language"]
        if lang == "de-DE":
            results = [{"id": 1, "title": "", "original_title": "Orig", "release_date": "", "popularity": 1.0}]
        else:
            results = [
                {"id": 1, "title": "English One", "original_title": "Orig", "release_date": "", "popularity": 1.0},
                {"id": 2, "title": "English Two", "original_title": "Zwei", "release_date": "2001-01-01", "popularity": 2.0},
            ]
        return httpx.Response(200, json={"results": results})

    first, second = make_client(handler).search(MediaKind.MOVIE, "x", None)
    assert (first.title, first.english_title, first.year) == ("English One", "English One", None)
    assert (second.tmdb_id, second.title, second.english_title, second.year) == (2, "English Two", "English Two", 2001)


def test_details_series(client):
    c = client.details(MediaKind.EPISODE, 1396)
    assert (c.tmdb_id, c.kind, c.title, c.original_title, c.english_title, c.year, c.popularity) == (
        1396, MediaKind.EPISODE, "Breaking Bad", "Breaking Bad", "Breaking Bad", 2008, 120.7)


def test_details_falls_back_to_english_title():
    def handler(request):
        title = "" if request.url.params["language"] == "de-DE" else "Some Movie"
        return httpx.Response(200, json={"id": 5, "title": title, "original_title": "Orig", "release_date": "", "popularity": 3})

    c = make_client(handler).details(MediaKind.MOVIE, 5)
    assert (c.title, c.english_title, c.year, c.popularity) == ("Some Movie", "Some Movie", None, 3.0)


def test_episode_titles_fall_back_to_english(client):
    t = client.episode_titles(1396, 3)
    assert t[1] == "Kein Más" and t[5] == "Mas"  # leer bzw. "Episode 5" → englischer Name
    assert t[2] == "Pferd ohne Namen" and t[3] == "I.F.T."


def test_episode_numbers_include_unnamed_episodes(client, calls):
    assert client.episode_numbers(1396, 3) == {1, 2, 3, 5}
    client.episode_titles(1396, 3)
    assert calls["tv/1396/season/3"] == 2  # gleiche Anfrage (de) kommt aus dem Cache, dazu en


def test_episode_numbers_errors_raise_tmdb_error_with_status():
    client = make_client(lambda r: httpx.Response(404, json={}))
    with pytest.raises(TmdbError, match="404") as exc_info:
        client.episode_numbers(1, 9)
    assert exc_info.value.status_code == 404


def test_episode_titles_missing_in_both_languages_are_omitted():
    def handler(request):
        return httpx.Response(200, json={"episodes": [
            {"episode_number": 1, "name": ""}, {"episode_number": 2, "name": "Folge 2"}]})

    assert make_client(handler).episode_titles(1, 1) == {}


def test_episode_titles_in_english_need_one_request(requests_seen):
    def handler(request):
        requests_seen.append(request)
        return httpx.Response(200, json={"episodes": [{"episode_number": 1, "name": "Pilot"}]})

    assert make_client(handler, language="en-US").episode_titles(1, 1) == {1: "Pilot"}
    assert len(requests_seen) == 1


def test_cache_avoids_second_request(client, calls):
    client.details(MediaKind.EPISODE, 1396)
    client.details(MediaKind.EPISODE, 1396)
    assert calls["tv/1396"] == 2  # de-DE und en-US, danach aus dem Cache


def test_cache_key_has_no_api_key(sessionmaker_):
    from mediasort.db import TmdbCache

    client = TmdbClient("geheim", cache=DbCache(sessionmaker_), sleep=lambda _s: None,
                        transport=httpx.MockTransport(fixture_response))
    client.details(MediaKind.EPISODE, 1396)
    with sessionmaker_() as s:
        keys = [row.key for row in s.query(TmdbCache)]
    assert len(keys) == 2
    assert not any("geheim" in k or "api_key" in k for k in keys)


def test_errors_are_not_cached(sessionmaker_):
    state = {"fail": True}

    def handler(request):
        if state["fail"]:
            return httpx.Response(500, json={})
        return fixture_response(request)

    client = make_client(handler, cache=DbCache(sessionmaker_))
    with pytest.raises(TmdbError):
        client.details(MediaKind.EPISODE, 1396)
    state["fail"] = False
    assert client.details(MediaKind.EPISODE, 1396).title == "Breaking Bad"


def test_find_imdb_movie(client, requests_seen):
    c = client.find_imdb("tt0133093")
    assert (c.tmdb_id, c.kind, c.title, c.english_title, c.year) == (603, MediaKind.MOVIE, "Matrix", "The Matrix", 1999)
    assert all(r.url.params["external_source"] == "imdb_id" for r in requests_seen)


def test_find_imdb_series(client):
    c = client.find_imdb("tt0903747")
    assert (c.tmdb_id, c.kind, c.title, c.year) == (1396, MediaKind.EPISODE, "Breaking Bad", 2008)


def test_find_imdb_unknown_returns_none(client):
    assert client.find_imdb("tt9999999") is None


def test_retries_once_on_429(requests_seen):
    sleeps = []
    responses = iter([
        httpx.Response(429, headers={"Retry-After": "2"}, json={}),
        httpx.Response(200, json={"id": 1, "name": "A", "original_name": "A", "first_air_date": "2020-01-01", "popularity": 1}),
        httpx.Response(200, json={"id": 1, "name": "A", "original_name": "A", "first_air_date": "2020-01-01", "popularity": 1}),
    ])
    client = TmdbClient("key", sleep=sleeps.append, transport=httpx.MockTransport(lambda r: next(responses)))
    assert client.details(MediaKind.EPISODE, 1).title == "A"
    assert sleeps == [2.0]


def test_second_429_raises():
    sleeps = []
    client = TmdbClient("key", sleep=sleeps.append,
                        transport=httpx.MockTransport(lambda r: httpx.Response(429, headers={"Retry-After": "0"}, json={})))
    with pytest.raises(TmdbError, match="Anfragelimit"):
        client.details(MediaKind.MOVIE, 1)
    assert len(sleeps) == 1


def test_http_error_message_is_german():
    client = make_client(lambda r: httpx.Response(401, json={"status_message": "Invalid API key"}))
    with pytest.raises(TmdbError, match="API-Schlüssel"):
        client.details(MediaKind.MOVIE, 1)
    client = make_client(lambda r: httpx.Response(404, json={}))
    with pytest.raises(TmdbError, match="404"):
        client.details(MediaKind.MOVIE, 1)


def test_network_error_raises_tmdb_error():
    def handler(request):
        raise httpx.ConnectError("boom", request=request)

    with pytest.raises(TmdbError, match="TMDB nicht erreichbar") as exc_info:
        make_client(handler).search(MediaKind.MOVIE, "Matrix", None)
    assert exc_info.value.status_code is None


def test_bearer_token_for_long_keys(requests_seen):
    def handler(request):
        requests_seen.append(request)
        return httpx.Response(200, json={"results": []})

    long_key = "e" * 41
    TmdbClient(long_key, transport=httpx.MockTransport(handler)).search(MediaKind.MOVIE, "x", None)
    TmdbClient("k" * 40, transport=httpx.MockTransport(handler)).search(MediaKind.MOVIE, "x", None)
    long_req, short_req = requests_seen[0], requests_seen[-1]
    assert long_req.headers["Authorization"] == f"Bearer {long_key}"
    assert "api_key" not in long_req.url.params
    assert short_req.url.params["api_key"] == "k" * 40
    assert "Authorization" not in short_req.headers


@pytest.mark.parametrize("text,ref", [
    ("https://www.themoviedb.org/tv/1396-breaking-bad", ("tv", "1396")),
    ("https://www.themoviedb.org/movie/603-the-matrix?language=de", ("movie", "603")),
    ("https://www.imdb.com/title/tt0133093/", ("imdb", "tt0133093")),
    ("tt0133093", ("imdb", "tt0133093")),
    (" tt0133093 ", ("imdb", "tt0133093")),
    ("tmdb:movie:603", ("movie", "603")),
    ("tmdb:tv:1396", ("tv", "1396")),
    ("Matrix", None),
    ("tmdb:foo:1", None),
    ("", None),
])
def test_parse_reference(text, ref):
    assert TmdbClient.parse_reference(text) == ref
