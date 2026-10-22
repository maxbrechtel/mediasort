"""TMDB-Client (API v3): Suche, Details, Folgennamen, IMDb-Auflösung, mit Cache."""

import re
import time
from collections.abc import Callable
from typing import Any

import httpx

from mediasort.db import DbCache
from mediasort.models import Candidate, MediaKind

BASE_URL = "https://api.themoviedb.org/3"
TIMEOUT_S = 15.0
ENGLISH = "en-US"
MAX_RETRY_AFTER_S = 60.0
BEARER_MIN_LENGTH = 41  # v4-Zugriffstoken sind lang, v3-API-Schlüssel haben 32 Zeichen

_PLACEHOLDER_EPISODE = re.compile(r"^(Episode|Folge) \d+$")
_TMDB_REF = re.compile(r"^tmdb:(movie|tv):(\d+)$", re.IGNORECASE)
_TMDB_URL = re.compile(r"themoviedb\.org/(movie|tv)/(\d+)", re.IGNORECASE)
_IMDB_ID = re.compile(r"\b(tt\d{7,})\b")


class TmdbError(Exception):
    """Fehler bei der Kommunikation mit TMDB (Meldung auf Deutsch).

    ``status_code`` ist der HTTP-Status einer Fehlerantwort, sonst ``None``.
    """

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def _endpoint(kind: MediaKind) -> str:
    return "movie" if kind == MediaKind.MOVIE else "tv"


def _year(date: str | None) -> int | None:
    head = (date or "")[:4]
    return int(head) if head.isdigit() else None


def _candidate(kind: MediaKind, data: dict[str, Any], english_title: str | None) -> Candidate:
    if kind == MediaKind.MOVIE:
        title, original, date = data.get("title"), data.get("original_title"), data.get("release_date")
    else:
        title, original, date = data.get("name"), data.get("original_name"), data.get("first_air_date")
    return Candidate(
        tmdb_id=int(data["id"]),
        kind=kind,
        title=title or english_title or original or "",
        original_title=original or "",
        english_title=english_title or None,
        year=_year(date),
        popularity=float(data.get("popularity") or 0.0),
    )


def _title_of(kind: MediaKind, data: dict[str, Any]) -> str | None:
    return (data.get("title") if kind == MediaKind.MOVIE else data.get("name")) or None


class TmdbClient:
    def __init__(
        self,
        api_key: str,
        language: str = "de-DE",
        cache: DbCache | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._api_key = api_key
        self._language = language
        self._cache = cache
        self._sleep = sleep
        headers = {"Authorization": f"Bearer {api_key}"} if len(api_key) >= BEARER_MIN_LENGTH else {}
        self._http = httpx.Client(base_url=BASE_URL, timeout=TIMEOUT_S, transport=transport, headers=headers)

    def close(self) -> None:
        self._http.close()

    # --- HTTP -----------------------------------------------------------------

    def _get(self, path: str, **params: Any) -> dict[str, Any]:
        params = {k: v for k, v in params.items() if v is not None}
        key = path + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))
        if self._cache is not None:
            cached = self._cache.get(key)
            if cached is not None:
                return cached
        data = self._request(path, params)
        if self._cache is not None:
            self._cache.put(key, data)
        return data

    def _request(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        query = dict(params)
        if "Authorization" not in self._http.headers:
            query["api_key"] = self._api_key
        response = self._send(path, query)
        if response.status_code == 429:
            self._sleep(self._retry_after(response))
            response = self._send(path, query)
            if response.status_code == 429:
                raise TmdbError("TMDB: Anfragelimit überschritten, bitte später erneut versuchen", 429)
        if response.status_code >= 400:
            raise TmdbError(self._http_error_message(response), response.status_code)
        try:
            data = response.json()
        except ValueError as exc:
            raise TmdbError("TMDB: ungültige Antwort erhalten") from exc
        if not isinstance(data, dict):
            raise TmdbError("TMDB: ungültige Antwort erhalten")
        return data

    def _send(self, path: str, query: dict[str, Any]) -> httpx.Response:
        try:
            return self._http.get("/" + path.lstrip("/"), params=query)
        except httpx.TransportError as exc:
            raise TmdbError(f"TMDB nicht erreichbar: {exc}") from exc
        except httpx.RequestError as exc:  # z. B. DecodingError: Antwort nicht entpackbar
            raise TmdbError(f"TMDB: Antwort nicht lesbar ({type(exc).__name__}: {exc})") from exc
        except httpx.InvalidURL as exc:
            raise TmdbError(f"TMDB: ungültige Anfrage ({exc})") from exc

    @staticmethod
    def _retry_after(response: httpx.Response) -> float:
        try:
            seconds = float(response.headers.get("Retry-After", "1"))
        except ValueError:
            seconds = 1.0
        return min(max(seconds, 0.0), MAX_RETRY_AFTER_S)

    @staticmethod
    def _http_error_message(response: httpx.Response) -> str:
        status = response.status_code
        if status == 401:
            return "TMDB: API-Schlüssel ungültig (401)"
        if status == 404:
            return "TMDB: nicht gefunden (404)"
        return f"TMDB-Fehler: HTTP {status}"

    def _languages(self) -> list[str]:
        return [self._language] if self._language == ENGLISH else [self._language, ENGLISH]

    # --- Abfragen -------------------------------------------------------------

    def search(self, kind: MediaKind, query: str, year: int | None) -> list[Candidate]:
        result = self._search_once(kind, query, year)
        if not result and year is not None:
            result = self._search_once(kind, query, None)
        return result

    def _search_once(self, kind: MediaKind, query: str, year: int | None) -> list[Candidate]:
        endpoint = _endpoint(kind)
        year_param = "year" if kind == MediaKind.MOVIE else "first_air_date_year"
        found: dict[str, list[dict[str, Any]]] = {}
        for lang in self._languages():
            data = self._get(f"search/{endpoint}", query=query, language=lang, **{year_param: year})
            found[lang] = data.get("results") or []
        english = {int(r["id"]): r for r in found[ENGLISH]}
        merged = list(found[self._language])
        if self._language != ENGLISH:
            known = {int(r["id"]) for r in merged}
            merged += [r for r in found[ENGLISH] if int(r["id"]) not in known]
        candidates = []
        for item in merged:
            en_item = english.get(int(item["id"]))
            candidates.append(_candidate(kind, item, _title_of(kind, en_item) if en_item else None))
        return candidates

    def details(self, kind: MediaKind, tmdb_id: int) -> Candidate:
        endpoint = _endpoint(kind)
        data = self._get(f"{endpoint}/{tmdb_id}", language=self._language)
        if self._language == ENGLISH:
            english = data
        else:
            english = self._get(f"{endpoint}/{tmdb_id}", language=ENGLISH)
        return _candidate(kind, data, _title_of(kind, english))

    def episode_numbers(self, tv_id: int, season: int) -> set[int]:
        """Alle Folgennummern der Staffel, auch Folgen ohne Namen."""
        data = self._get(f"tv/{tv_id}/season/{season}", language=self._language)
        return {int(ep["episode_number"]) for ep in data.get("episodes") or []}

    def episode_titles(self, tv_id: int, season: int) -> dict[int, str]:
        path = f"tv/{tv_id}/season/{season}"
        local = self._episode_names(self._get(path, language=self._language))
        if self._language == ENGLISH:
            return local
        english = self._episode_names(self._get(path, language=ENGLISH))
        return {**english, **local}

    @staticmethod
    def _episode_names(data: dict[str, Any]) -> dict[int, str]:
        names = {}
        for ep in data.get("episodes") or []:
            name = (ep.get("name") or "").strip()
            if name and not _PLACEHOLDER_EPISODE.match(name):
                names[int(ep["episode_number"])] = name
        return names

    def find_imdb(self, imdb_id: str) -> Candidate | None:
        data = self._get(f"find/{imdb_id}", external_source="imdb_id", language=self._language)
        for key, kind in (("movie_results", MediaKind.MOVIE), ("tv_results", MediaKind.EPISODE)):
            results = data.get(key) or []
            if not results:
                continue
            first = results[0]
            english = self._get(f"find/{imdb_id}", external_source="imdb_id", language=ENGLISH).get(key) or []
            en_item = next((r for r in english if r.get("id") == first.get("id")), None)
            return _candidate(kind, first, _title_of(kind, en_item) if en_item else None)
        return None

    # --- Eingabehilfe -----------------------------------------------------------

    @staticmethod
    def parse_reference(text: str) -> tuple[str, str] | None:
        text = text.strip()
        if m := _TMDB_REF.match(text):
            return m.group(1).lower(), m.group(2)
        if m := _TMDB_URL.search(text):
            return m.group(1).lower(), m.group(2)
        if m := _IMDB_ID.search(text):
            return "imdb", m.group(1)
        return None
