"""Bibliotheksindex: vorhandene Filme/Serien in den Plex-Bibliotheken finden (Spec 7.2)."""

import re
import unicodedata
from collections.abc import Callable, Sequence
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from mediasort.db import LibraryEntry
from mediasort.matcher import HIGH_SIMILARITY, YEAR_TOLERANCE, title_similarity
from mediasort.models import Candidate
from mediasort.parser import parse_name
from mediasort.scanner import VIDEO_EXT

_FOLDER_RE = re.compile(r"^(?P<title>.*?)\s*\((?P<year>\d{4})\)\s*$")
_NFO_UNIQUEID_RE = re.compile(
    r"""<uniqueid\b[^>]*\btype\s*=\s*["']tmdb["'][^>]*>\s*(\d+)\s*</uniqueid>""", re.IGNORECASE
)
_NFO_TMDBID_RE = re.compile(r"<tmdbid>\s*(\d+)\s*</tmdbid>", re.IGNORECASE)


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def read_nfo_tmdb_id(folder: Path) -> int | None:
    """TMDB-ID aus einer ``*.nfo`` direkt im Ordner (``<uniqueid type="tmdb">`` oder ``<tmdbid>``)."""
    try:
        nfos = sorted(p for p in folder.iterdir() if p.suffix.lower() == ".nfo" and p.is_file())
    except OSError:
        return None
    for nfo in nfos:
        try:
            text = nfo.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for pattern in (_NFO_UNIQUEID_RE, _NFO_TMDBID_RE):
            found = pattern.search(text)
            if found:
                return int(found.group(1))
    return None


def parse_folder_name(name: str) -> tuple[str, int | None]:
    """``"Titel (2009)"`` -> ``("Titel", 2009)``; ohne Jahr bleibt der Name unverändert."""
    found = _FOLDER_RE.match(name)
    if found and found.group("title"):
        return found.group("title"), int(found.group("year"))
    return name.strip(), None


def videos_in(folder: Path) -> list[Path]:
    """Alle Videodateien (rekursiv), sortiert; fehlender Ordner ergibt eine leere Liste."""
    if not folder.is_dir():
        return []
    return sorted(
        p for p in folder.rglob("*") if p.suffix.lstrip(".").lower() in VIDEO_EXT and p.is_file()
    )


def episode_files(show_dir: Path, season: int, episodes: Sequence[int]) -> list[Path]:
    """Videos im Serienordner, deren geparste Staffel/Folgen sich mit den gesuchten überschneiden."""
    wanted = set(episodes)
    result = []
    for video in videos_in(show_dir):
        parsed = parse_name(video.name, video.parent.name)
        if parsed.season == season and wanted.intersection(parsed.episodes):
            result.append(video)
    return result


class LibraryIndex:
    """Index der obersten Ordner einer Bibliothek, abgelegt in ``LibraryEntry``."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        library_id: int,
        root: Path,
        kind: str = "movie",
    ) -> None:
        self._session_factory = session_factory
        self._library_id = library_id
        self._root = Path(root).absolute()
        self._kind = kind

    def _entry_for(self, folder: Path, known_id: int | None) -> LibraryEntry:
        title, year = parse_folder_name(_nfc(folder.name))
        return LibraryEntry(
            library_id=self._library_id,
            path=str(folder),
            title=title,
            year=year,
            tmdb_id=read_nfo_tmdb_id(folder) or known_id,
            kind=self._kind,
        )

    def refresh(self) -> int:
        """Ersetzt die Einträge der Bibliothek; bekannte ``tmdb_id`` bleibt bei unverändertem Pfad."""
        try:
            folders = sorted(p for p in self._root.iterdir() if p.is_dir() and not p.name.startswith("."))
        except OSError:
            folders = []
        with self._session_factory() as session:
            old = session.scalars(select(LibraryEntry).where(LibraryEntry.library_id == self._library_id)).all()
            known = {e.path: e.tmdb_id for e in old}
            for entry in old:
                session.delete(entry)
            session.flush()
            session.add_all(self._entry_for(f, known.get(str(f))) for f in folders)
            session.commit()
        return len(folders)

    def find(self, cand: Candidate) -> list[Path]:
        """Ordner mit gleicher TMDB-ID, danach (ohne ID) titelähnliche Ordner mit passendem Jahr."""
        with self._session_factory() as session:
            entries = session.scalars(select(LibraryEntry).where(LibraryEntry.library_id == self._library_id)).all()
            rows = [(e.path, e.title, e.year, e.tmdb_id) for e in entries]
        by_id: list[str] = []
        by_title: list[str] = []
        for path, title, year, tmdb_id in rows:
            if tmdb_id is not None:
                if tmdb_id == cand.tmdb_id:
                    by_id.append(path)
                continue
            if year is not None and cand.year is not None and abs(year - cand.year) > YEAR_TOLERANCE:
                continue
            if title_similarity(title, cand) >= HIGH_SIMILARITY:
                by_title.append(path)
        return [Path(p) for p in sorted(by_id) + sorted(by_title)]

    def remember(self, folder: Path, tmdb_id: int) -> None:
        """Merkt die TMDB-ID eines Ordners (legt den Eintrag an, falls er fehlt)."""
        folder = Path(folder).absolute()
        with self._session_factory() as session:
            entry = session.scalars(
                select(LibraryEntry).where(
                    LibraryEntry.library_id == self._library_id, LibraryEntry.path == str(folder)
                )
            ).first()
            if entry is None:
                entry = self._entry_for(folder, None)
                session.add(entry)
            entry.tmdb_id = tmdb_id
            session.commit()
