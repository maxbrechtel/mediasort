import pytest

from mediasort.db import (
    DbCache,
    FileState,
    Folder,
    History,
    Ignored,
    Item,
    Job,
    LibraryEntry,
    Scan,
    make_engine,
)
from mediasort.settings import Settings, load_settings, save_settings


def test_settings_roundtrip(session):
    s = load_settings(session)
    assert s.language == "de-DE" and s.sample_bytes == 104_857_600 and s.season_label == "Staffel"
    s.tmdb_key = "k"
    s.plex_path_map = [("/media", "/media")]
    save_settings(session, s)
    session.expire_all()
    loaded = load_settings(session)
    assert loaded.plex_path_map == [("/media", "/media")]
    assert loaded.tmdb_key == "k"
    assert loaded == s


def test_settings_defaults_and_unknown_keys(session):
    from mediasort.db import Setting

    session.add(Setting(key="poll_minutes", value_json=5))
    session.add(Setting(key="no_such_field", value_json="x"))
    session.commit()
    s = load_settings(session)
    assert s.poll_minutes == 5 and s.min_age_minutes == 10 and not s.auto_enabled
    assert s.exclude == ["incomplete", "nzbs", ".deletedByTMM", ".thumbnails", "@eaDir"]
    assert Settings().exclude is not Settings().exclude


def test_cache_ttl(sessionmaker_):
    clock = [1_000_000.0]
    c = DbCache(sessionmaker_, ttl_days=30, now=lambda: clock[0])
    assert c.get("k") is None
    c.put("k", {"a": 1})
    assert c.get("k") == {"a": 1}
    clock[0] += 29 * 86400
    assert c.get("k") == {"a": 1}
    clock[0] += 2 * 86400
    assert c.get("k") is None


def test_cache_put_overwrites_and_refreshes(sessionmaker_):
    clock = [0.0]
    c = DbCache(sessionmaker_, ttl_days=1, now=lambda: clock[0])
    c.put("k", [1])
    clock[0] = 80_000
    c.put("k", [2])
    clock[0] = 160_000
    assert c.get("k") == [2]


def test_wal_mode_and_threads(tmp_path):
    engine = make_engine(tmp_path / "sub" / "m.db")
    try:
        with engine.connect() as conn:
            assert conn.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
    finally:
        engine.dispose()


def test_all_tables_usable(session):
    f = Folder(path="/media/in", role="input", media_type="auto", auto_level="off")
    session.add(f)
    session.flush()
    sc = Scan(folder_id=f.id, started=1.0, status="running", auto=False)
    session.add(sc)
    session.flush()
    session.add(
        Item(scan_id=sc.id, folder_id=f.id, source="/media/in/a.mkv", size=1, mtime=2.0,
             parsed_json={"title": "A"}, episodes_json=[1, 2], status="new", auto=False)
    )
    session.add_all([
        Ignored(path="/x", size=1, created=1.0),
        LibraryEntry(library_id=f.id, path="/lib/A (2000)", title="A", year=2000, tmdb_id=1, kind="movie"),
        History(ts=1.0, job_id=None, action="moved", old_path="/a", new_path="/b", auto=False),
        Job(kind="scan", status="queued", progress=0, total=0, auto=False),
        FileState(path="/x", size=1, mtime=1.0, last_seen=2.0),
    ])
    session.commit()
    item = session.query(Item).one()
    assert item.parsed_json == {"title": "A"} and item.episodes_json == [1, 2] and item.status == "new"
