"""Plex-Anbindung: Bibliotheken lesen, Papierkorb-Einstellung prüfen, Teilscans anstoßen."""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import httpx

TIMEOUT_S = 10.0
UNEXPECTED_RESPONSE = "Plex-Fehler: unerwartete Antwort"
UNREACHABLE_PREFIX = "Plex nicht erreichbar: "


@dataclass(frozen=True)
class PlexSection:
    key: str
    title: str
    type: str
    locations: tuple[str, ...]


class PlexError(Exception):
    """Fehler bei der Kommunikation mit Plex (Meldung auf Deutsch).

    ``status_code`` ist der HTTP-Status einer Fehlerantwort, sonst ``None``.
    """

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class PlexClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        transport: httpx.BaseTransport | None = None,
        timeout: float = TIMEOUT_S,
    ):
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._transport = transport
        self._timeout = timeout

    def _get(self, path: str, params: dict[str, str] | None = None) -> httpx.Response:
        headers = {"Accept": "application/json", "X-Plex-Token": self._token}
        try:
            with httpx.Client(transport=self._transport, timeout=self._timeout) as http:
                response = http.get(self._base_url + path, params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise PlexError(UNREACHABLE_PREFIX + (str(exc) or type(exc).__name__)) from exc
        if response.is_error:
            raise PlexError(f"Plex-Fehler: HTTP {response.status_code}", response.status_code)
        return response

    def _container(self, path: str) -> dict[str, Any]:
        try:
            container = self._get(path).json()["MediaContainer"]
        except (ValueError, KeyError, TypeError) as exc:
            raise PlexError(UNEXPECTED_RESPONSE) from exc
        if not isinstance(container, dict):
            raise PlexError(UNEXPECTED_RESPONSE)
        return container

    def sections(self) -> list[PlexSection]:
        container = self._container("/library/sections")
        try:
            return [
                PlexSection(
                    key=str(directory["key"]),
                    title=directory.get("title", ""),
                    type=directory.get("type", ""),
                    locations=tuple(loc["path"] for loc in directory.get("Location", [])),
                )
                for directory in container.get("Directory", [])
            ]
        except (KeyError, AttributeError, TypeError) as exc:
            raise PlexError(UNEXPECTED_RESPONSE) from exc

    def auto_empty_trash(self) -> bool:
        """Ist ``autoEmptyTrash`` aktiv? Fehlt die Einstellung, ist die Antwort ``False``."""
        container = self._container("/:/prefs")
        try:
            for setting in container.get("Setting", []):
                if setting.get("id") == "autoEmptyTrash":
                    return setting.get("value") is True
        except (AttributeError, TypeError) as exc:
            raise PlexError(UNEXPECTED_RESPONSE) from exc
        return False

    def refresh(self, section_key: str, path: str) -> None:
        self._get(f"/library/sections/{section_key}/refresh", {"path": path})


def _pure(path: Path | str) -> PurePosixPath:
    return PurePosixPath(path.as_posix() if isinstance(path, Path) else path)


def _is_within(path: PurePosixPath, prefix: PurePosixPath) -> bool:
    return path.parts[: len(prefix.parts)] == prefix.parts


def map_path(path: Path, mapping: Sequence[tuple[str, str]]) -> str:
    """Wendet das längste passende Präfix-Paar (komponentenweise) an, sonst unverändert."""
    pure = _pure(path)
    best: tuple[PurePosixPath, PurePosixPath] | None = None
    for local, remote in mapping:
        local_pure = _pure(local)
        if _is_within(pure, local_pure) and (best is None or len(local_pure.parts) > len(best[0].parts)):
            best = (local_pure, _pure(remote))
    if best is None:
        return pure.as_posix()
    local_pure, remote_pure = best
    return PurePosixPath(remote_pure, *pure.parts[len(local_pure.parts) :]).as_posix()


def suggest_mapping(local_roots: Sequence[Path], sections: Sequence[PlexSection]) -> list[tuple[str, str]]:
    """Schlägt Paare (mediasort-Pfad, Plex-Pfad) über gleiche letzte Pfadteile vor."""
    locations = [loc for section in sections for loc in section.locations]
    pairs: list[tuple[str, str]] = []
    for root in local_roots:
        local = _pure(root).as_posix()
        same_name = [loc for loc in locations if _pure(loc).name == _pure(local).name]
        if not same_name:
            continue
        remote = local if local in same_name else same_name[0]
        pair = (local, _pure(remote).as_posix())
        if pair not in pairs:
            pairs.append(pair)
    return pairs


def _title_folder(path: PurePosixPath, roots: Sequence[PurePosixPath]) -> PurePosixPath | None:
    containing = [root for root in roots if _is_within(path, root)]
    if not containing:
        return None
    root = max(containing, key=lambda r: len(r.parts))
    return PurePosixPath(*path.parts[: len(root.parts) + 1])


def refresh_touched(
    client: PlexClient,
    touched: Iterable[Path],
    mapping: Sequence[tuple[str, str]],
    library_roots: Sequence[Path],
) -> list[str]:
    """Stößt Plex-Teilscans für die betroffenen Titelordner an.

    Gibt Hinweise (deutsch) zurück, wenn Plex nicht erreichbar ist oder Fehler meldet.
    Pfade außerhalb der Bibliotheken und ohne passenden Plex-Abschnitt werden still übersprungen.
    """
    roots = [_pure(root) for root in library_roots]
    folders = {
        folder for path in touched if (folder := _title_folder(_pure(path), roots)) is not None
    }
    if not folders:
        return []
    try:
        sections = client.sections()
    except PlexError as exc:
        return [str(exc)]

    targets: set[tuple[str, str]] = set()
    for folder in folders:
        plex_folder = _pure(map_path(Path(folder), mapping))
        for section in sections:
            if any(_is_within(plex_folder, _pure(location)) for location in section.locations):
                targets.add((plex_folder.as_posix(), section.key))

    hints: list[str] = []
    for plex_folder, key in sorted(targets):
        try:
            client.refresh(key, plex_folder)
        except PlexError as exc:
            if str(exc) not in hints:
                hints.append(str(exc))
            if str(exc).startswith(UNREACHABLE_PREFIX):
                break  # Plex nicht erreichbar: keine weiteren Versuche (sonst N Timeouts)
    return hints
