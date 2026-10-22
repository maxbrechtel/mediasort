"""SQLite-Datenhaltung (SQLAlchemy 2): alle Tabellen aus Spec 10."""

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy import JSON, Engine, Float, ForeignKey, Integer, String, Text, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker


class Base(DeclarativeBase):
    pass


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    value_json: Mapped[Any] = mapped_column(JSON)


class Folder(Base):
    __tablename__ = "folders"
    id: Mapped[int] = mapped_column(primary_key=True)
    path: Mapped[str] = mapped_column(String, unique=True)
    role: Mapped[str] = mapped_column(String)  # "input" | "library"
    media_type: Mapped[str] = mapped_column(String, default="auto")  # FolderType
    movie_target_id: Mapped[int | None] = mapped_column(ForeignKey("folders.id"))
    series_target_id: Mapped[int | None] = mapped_column(ForeignKey("folders.id"))
    movie3d_target_id: Mapped[int | None] = mapped_column(ForeignKey("folders.id"))
    auto_level: Mapped[str] = mapped_column(String, default="off")  # AutoLevel


class Scan(Base):
    __tablename__ = "scans"
    id: Mapped[int] = mapped_column(primary_key=True)
    folder_id: Mapped[int | None] = mapped_column(ForeignKey("folders.id"))
    started: Mapped[float | None] = mapped_column(Float)
    finished: Mapped[float | None] = mapped_column(Float)
    subset: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="running")
    auto: Mapped[bool] = mapped_column(default=False)
    errors_json: Mapped[Any | None] = mapped_column(JSON)  # Meldungen zu nicht lesbaren Pfaden


class Item(Base):
    __tablename__ = "items"
    id: Mapped[int] = mapped_column(primary_key=True)
    scan_id: Mapped[int | None] = mapped_column(ForeignKey("scans.id"), index=True)
    folder_id: Mapped[int | None] = mapped_column(ForeignKey("folders.id"))
    source: Mapped[str] = mapped_column(String)
    size: Mapped[int] = mapped_column(Integer)
    mtime: Mapped[float] = mapped_column(Float)
    kind: Mapped[str | None] = mapped_column(String)  # MediaKind
    parsed_json: Mapped[Any | None] = mapped_column(JSON)
    group_key: Mapped[str | None] = mapped_column(String)
    tmdb_id: Mapped[int | None] = mapped_column(Integer)
    title: Mapped[str | None] = mapped_column(String)
    year: Mapped[int | None] = mapped_column(Integer)
    season: Mapped[int | None] = mapped_column(Integer)
    episodes_json: Mapped[Any | None] = mapped_column(JSON)
    episode_titles_json: Mapped[Any | None] = mapped_column(JSON)
    confidence: Mapped[str | None] = mapped_column(String)  # Confidence
    alternatives_json: Mapped[Any | None] = mapped_column(JSON)
    mediainfo_json: Mapped[Any | None] = mapped_column(JSON)
    target: Mapped[str | None] = mapped_column(String)
    subtitles_json: Mapped[Any | None] = mapped_column(JSON)
    conflict_json: Mapped[Any | None] = mapped_column(JSON)
    decision: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="new")  # ItemStatus
    error: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    auto: Mapped[bool] = mapped_column(default=False)


class Ignored(Base):
    __tablename__ = "ignored"
    path: Mapped[str] = mapped_column(String, primary_key=True)
    size: Mapped[int] = mapped_column(Integer)
    created: Mapped[float] = mapped_column(Float)


class LibraryEntry(Base):
    __tablename__ = "library_index"
    id: Mapped[int] = mapped_column(primary_key=True)
    library_id: Mapped[int] = mapped_column(ForeignKey("folders.id"), index=True)
    path: Mapped[str] = mapped_column(String)
    title: Mapped[str] = mapped_column(String)
    year: Mapped[int | None] = mapped_column(Integer)
    tmdb_id: Mapped[int | None] = mapped_column(Integer, index=True)
    kind: Mapped[str] = mapped_column(String)


class History(Base):
    __tablename__ = "history"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[float] = mapped_column(Float)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"))
    action: Mapped[str] = mapped_column(String)
    old_path: Mapped[str | None] = mapped_column(String)
    new_path: Mapped[str | None] = mapped_column(String)
    auto: Mapped[bool] = mapped_column(default=False)


class TmdbCache(Base):
    __tablename__ = "tmdb_cache"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    value_json: Mapped[Any] = mapped_column(JSON)
    fetched: Mapped[float] = mapped_column(Float)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="queued")
    progress: Mapped[int] = mapped_column(Integer, default=0)
    total: Mapped[int] = mapped_column(Integer, default=0)
    started: Mapped[float | None] = mapped_column(Float)
    finished: Mapped[float | None] = mapped_column(Float)
    auto: Mapped[bool] = mapped_column(default=False)
    message: Mapped[str | None] = mapped_column(Text)


class FileState(Base):
    __tablename__ = "file_state"
    path: Mapped[str] = mapped_column(String, primary_key=True)
    size: Mapped[int] = mapped_column(Integer)
    mtime: Mapped[float] = mapped_column(Float)
    last_seen: Mapped[float] = mapped_column(Float)


def make_engine(db_path: Path) -> Engine:
    """SQLite-Engine mit WAL; Verbindungen dürfen von einem Hintergrund-Thread genutzt werden."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()

    return engine


def init_db(engine: Engine) -> sessionmaker[Session]:
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


class DbCache:
    """TMDB-Antworten mit Ablaufzeit in der Tabelle ``tmdb_cache``."""

    def __init__(
        self,
        sm: sessionmaker[Session],
        ttl_days: int = 30,
        now: Callable[[], float] = time.time,
    ):
        self._sm = sm
        self._ttl_s = ttl_days * 86400
        self._now = now

    def get(self, key: str) -> Any | None:
        with self._sm() as s:
            row = s.get(TmdbCache, key)
            if row is None:
                return None
            if self._now() - row.fetched > self._ttl_s:
                s.delete(row)
                s.commit()
                return None
            return row.value_json

    def put(self, key: str, value: Any) -> None:
        with self._sm() as s:
            s.merge(TmdbCache(key=key, value_json=value, fetched=self._now()))
            s.commit()
