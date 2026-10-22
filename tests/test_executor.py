import errno
import os
import shutil
from pathlib import Path

import pytest
from sqlalchemy import select

from mediasort import executor
from mediasort.db import Folder, History, Item, Scan
from mediasort.executor import (
    ExecutionReport,
    SourceNotRemoved,
    cleanup_source,
    execute,
    move_file,
)
from mediasort.settings import Settings

MB = 1024 * 1024
EXCLUDE = ["incomplete", "nzbs", ".deletedByTMM", ".thumbnails", "@eaDir"]


def write(path: Path, data: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def sparse(path: Path, size: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        fh.truncate(size)
    return path


# --- move_file -----------------------------------------------------------------------


def test_move_same_fs(tmp_path):
    src = write(tmp_path / "a" / "m.mkv", b"video")
    dst = tmp_path / "b" / "m.mkv"
    dst.parent.mkdir()
    move_file(src, dst)
    assert dst.read_bytes() == b"video" and not src.exists()


def test_move_never_overwrites(tmp_path):
    src = write(tmp_path / "src.mkv", b"new")
    dst = write(tmp_path / "dst.mkv", b"old")
    with pytest.raises(FileExistsError):
        move_file(src, dst)
    assert src.read_bytes() == b"new" and dst.read_bytes() == b"old"


def _exdev_once(monkeypatch):
    real_rename = os.rename
    calls = []

    def fake_rename(a, b):
        calls.append((a, b))
        if len(calls) == 1:
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real_rename(a, b)

    monkeypatch.setattr(executor.os, "rename", fake_rename)
    return calls


def test_move_exdev_fallback(tmp_path, monkeypatch):
    calls = _exdev_once(monkeypatch)
    src = write(tmp_path / "src.mkv", b"video data")
    dst = tmp_path / "dst.mkv"
    move_file(src, dst)
    assert len(calls) == 2
    assert dst.read_bytes() == b"video data" and not src.exists()
    assert not (tmp_path / "dst.mkv.mediasort-tmp").exists()


def test_move_exdev_copy_failure_keeps_source(tmp_path, monkeypatch):
    _exdev_once(monkeypatch)

    def broken_copy(a, b):
        Path(b).write_bytes(b"part")
        raise OSError(errno.EIO, "Ein-/Ausgabefehler")

    monkeypatch.setattr(executor.shutil, "copyfile", broken_copy)
    src = write(tmp_path / "src.mkv", b"video data")
    dst = tmp_path / "dst.mkv"
    with pytest.raises(OSError):
        move_file(src, dst)
    assert src.read_bytes() == b"video data"
    assert not dst.exists() and not (tmp_path / "dst.mkv.mediasort-tmp").exists()


def test_move_exdev_size_mismatch_keeps_source(tmp_path, monkeypatch):
    _exdev_once(monkeypatch)
    monkeypatch.setattr(executor.shutil, "copyfile", lambda a, b: Path(b).write_bytes(b"short"))
    src = write(tmp_path / "src.mkv", b"video data")
    dst = tmp_path / "dst.mkv"
    with pytest.raises(OSError):
        move_file(src, dst)
    assert src.read_bytes() == b"video data"
    assert not dst.exists() and not (tmp_path / "dst.mkv.mediasort-tmp").exists()


def test_move_exdev_never_overwrites_foreign_tmp(tmp_path, monkeypatch):
    _exdev_once(monkeypatch)
    src = write(tmp_path / "src.mkv", b"video data")
    tmp = write(tmp_path / "dst.mkv.mediasort-tmp", b"foreign")
    with pytest.raises(FileExistsError):
        move_file(src, tmp_path / "dst.mkv")
    assert src.read_bytes() == b"video data" and tmp.read_bytes() == b"foreign"


def test_move_exdev_unlink_failure_reports_duplicate(tmp_path, monkeypatch):
    _exdev_once(monkeypatch)
    src = write(tmp_path / "src.mkv", b"video data")
    dst = tmp_path / "dst.mkv"
    real_unlink = os.unlink

    def unlink(path, *a, **kw):
        if Path(path) == src:
            raise PermissionError(errno.EACCES, "Keine Berechtigung", str(path))
        return real_unlink(path, *a, **kw)

    monkeypatch.setattr(executor.os, "unlink", unlink)
    with pytest.raises(SourceNotRemoved):
        move_file(src, dst)
    assert dst.read_bytes() == b"video data" and src.read_bytes() == b"video data"


# --- cleanup_source ------------------------------------------------------------------


def test_cleanup_deletes_folder_with_leftovers(tmp_path):
    rel = tmp_path / "Downloads" / "Rel"
    write(rel / "m.nfo")
    write(rel / "cover.jpg")
    sparse(rel / "Sample" / "sample.mkv", 300 * MB)
    sparse(rel / "small.mkv", 50 * MB)
    assert cleanup_source(rel, protected={tmp_path / "Downloads"}, exclude=EXCLUDE, sample_bytes=100 * MB)
    assert not rel.exists() and (tmp_path / "Downloads").is_dir()


def test_cleanup_keeps_folder_with_big_video(tmp_path):
    rel = tmp_path / "Downloads" / "Rel"
    write(rel / "m.nfo")
    sparse(rel / "Sub" / "other.mkv", 300 * MB)
    assert not cleanup_source(rel, protected=set(), exclude=EXCLUDE, sample_bytes=100 * MB)
    assert (rel / "m.nfo").exists() and (rel / "Sub" / "other.mkv").exists()


def test_cleanup_counts_videos_in_hidden_dirs(tmp_path):
    rel = tmp_path / "Rel"
    sparse(rel / ".versteckt" / "m.mkv", 300 * MB)
    assert not cleanup_source(rel, protected=set(), exclude=EXCLUDE, sample_bytes=100 * MB)
    assert (rel / ".versteckt" / "m.mkv").exists()


def test_cleanup_never_deletes_roots_or_excluded(tmp_path):
    downloads = tmp_path / "Downloads"
    write(downloads / "x.nfo")
    write(downloads / "incomplete" / "Teil" / "a.nfo")
    write(downloads / "Rel" / "@eaDir" / "thumb.jpg")
    protected = {downloads, tmp_path}
    kw = {"protected": protected, "exclude": EXCLUDE, "sample_bytes": 100 * MB}
    assert not cleanup_source(downloads, **kw)
    assert not cleanup_source(downloads / "incomplete", **kw)
    assert not cleanup_source(downloads / "Rel", **kw)  # Vorfahr eines Ausschlussordners
    assert not cleanup_source(tmp_path, **kw)
    assert (downloads / "x.nfo").exists()
    assert (downloads / "incomplete" / "Teil" / "a.nfo").exists()
    assert (downloads / "Rel" / "@eaDir" / "thumb.jpg").exists()


def test_cleanup_never_deletes_ancestor_of_protected(tmp_path):
    write(tmp_path / "share" / "Filme" / "a.nfo")
    assert not cleanup_source(tmp_path / "share", protected={tmp_path / "share" / "Filme"},
                              exclude=EXCLUDE, sample_bytes=100 * MB)
    assert (tmp_path / "share" / "Filme" / "a.nfo").exists()


def test_cleanup_refuses_symlinked_folder(tmp_path):
    real = tmp_path / "real"
    write(real / "a.nfo")
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    assert not cleanup_source(link, protected=set(), exclude=EXCLUDE, sample_bytes=100 * MB)
    assert link.is_symlink() and (real / "a.nfo").exists()


def test_cleanup_does_not_follow_symlinks_inside(tmp_path):
    keep = tmp_path / "keep"
    write(keep / "wichtig.nfo")
    rel = tmp_path / "Rel"
    write(rel / "a.nfo")
    (rel / "verweis").symlink_to(keep, target_is_directory=True)
    assert cleanup_source(rel, protected=set(), exclude=EXCLUDE, sample_bytes=100 * MB)
    assert not rel.exists() and (keep / "wichtig.nfo").exists()


def test_cleanup_missing_folder_is_noop(tmp_path):
    assert not cleanup_source(tmp_path / "fehlt", protected=set(), exclude=EXCLUDE, sample_bytes=1)


def test_cleanup_sample_dirs_and_sample_suffix_are_leftovers(tmp_path):
    rel = tmp_path / "Rel"
    sparse(rel / "Samples" / "rel.mkv", 300 * MB)
    sparse(rel / "rel-sample.mkv", 300 * MB)
    sparse(rel / "rel.sample.mkv", 300 * MB)
    sparse(rel / "rel_Sample.mkv", 300 * MB)
    sparse(rel / "rel sample.mkv", 300 * MB)
    assert cleanup_source(rel, protected=set(), exclude=EXCLUDE, sample_bytes=100 * MB)
    assert not rel.exists()


def test_cleanup_keeps_big_movie_with_sample_in_title(tmp_path):
    pack = tmp_path / "Pack"
    other = sparse(pack / "Sample.People.2000.1080p.mkv", 3000 * MB)
    assert not cleanup_source(pack, protected=set(), exclude=EXCLUDE, sample_bytes=100 * MB)
    assert other.exists()


def test_cleanup_video_at_sample_limit_blocks(tmp_path):
    rel = tmp_path / "Rel"
    sparse(rel / "m.mkv", 100 * MB)
    assert not cleanup_source(rel, protected=set(), exclude=EXCLUDE, sample_bytes=100 * MB)
    sparse(rel / "m.mkv", 100 * MB - 1)
    assert cleanup_source(rel, protected=set(), exclude=EXCLUDE, sample_bytes=100 * MB)


# --- execute ---------------------------------------------------------------------------

TARGET_2D = "Filme_2D/M (2020)/M (2020) 1080p h264 AC3 [DE].mkv"


class Env:
    def __init__(self, root: Path, sm):
        self.root, self.sf = root, sm
        self.settings = Settings()
        for name in ("Downloads", "Filme_2D", "Serien"):
            (root / name).mkdir(parents=True)
        with sm() as s:
            lib = Folder(path=str(root / "Filme_2D"), role="library", media_type="movie")
            series = Folder(path=str(root / "Serien"), role="library", media_type="series")
            s.add_all([lib, series])
            s.flush()
            s.add(Folder(path=str(root / "Downloads"), role="input", movie_target_id=lib.id,
                         series_target_id=series.id))
            s.add_all([Scan(id=1, status="done"), Scan(id=2, status="done")])
            s.commit()

    def p(self, rel: str) -> Path:
        return self.root / rel

    def item(self, source: str, target: str | None, *, subs=(), existing=None, suggestion=None,
             decision=None, status="confirmed", data: bytes = b"video", scan_id=1) -> Item:
        src = self.p(source)
        if not src.exists():
            write(src, data)
        sub_json = []
        for sub_src, sub_tgt in subs:
            if not self.p(sub_src).exists():
                write(self.p(sub_src), b"sub")
            sub_json.append({"path": str(self.p(sub_src)), "language": "de", "forced": False,
                             "target": str(self.p(sub_tgt)) if sub_tgt else None})
        st = src.stat()
        conflict = None
        if existing is not None:
            conflict = {"existing": [str(self.p(e)) for e in existing], "suggestion": suggestion}
        with self.sf() as s:
            it = Item(source=str(src), size=st.st_size, mtime=st.st_mtime, kind="movie",
                      target=str(self.p(target)) if target else None, subtitles_json=sub_json,
                      conflict_json=conflict, decision=decision, status=status, scan_id=scan_id)
            s.add(it)
            s.commit()
            return it

    def item_to_filme_2d(self) -> Item:
        return self.item("Downloads/Rel/m.mkv", TARGET_2D)

    def get(self, item_id: int) -> Item:
        with self.sf() as s:
            return s.get(Item, item_id)

    def history(self) -> list[History]:
        with self.sf() as s:
            return list(s.scalars(select(History).order_by(History.id)))


@pytest.fixture
def env(tmp_path, sessionmaker_):
    return Env(tmp_path / "media", sessionmaker_)


def run(env, *items, **kw) -> ExecutionReport:
    return execute(env.sf, [it.id for it in items], settings=env.settings, job_id=1, **kw)


def test_execute_moves_video_and_subs_and_logs(env):
    write(env.p("Downloads/Rel/m.nfo"))
    item = env.item("Downloads/Rel/m.mkv", TARGET_2D,
                    subs=[("Downloads/Rel/m.ger.srt", "Filme_2D/M (2020)/M (2020) 1080p h264 AC3 [DE].de.srt")])
    progress = []
    r = execute(env.sf, [item.id], settings=env.settings, job_id=1, auto=True,
                progress=lambda d, t: progress.append((d, t)))
    assert (r.done, r.failed) == (1, 0)
    assert env.p(TARGET_2D).read_bytes() == b"video"
    assert env.p("Filme_2D/M (2020)/M (2020) 1080p h264 AC3 [DE].de.srt").read_bytes() == b"sub"
    assert not env.p("Downloads/Rel").exists() and env.p("Downloads").is_dir()
    hist = env.history()
    assert [h.action for h in hist] == ["moved", "moved", "folder_deleted"]
    assert (hist[0].old_path, hist[0].new_path) == (str(env.p("Downloads/Rel/m.mkv")), str(env.p(TARGET_2D)))
    assert hist[2].old_path == str(env.p("Downloads/Rel"))
    assert all(h.auto for h in hist)
    done = env.get(item.id)
    assert (done.status, done.error) == ("done", None)
    assert progress == [(1, 1)]


def test_execute_skips_changed_source(env):
    item = env.item_to_filme_2d()
    env.p("Downloads/Rel/m.mkv").write_bytes(b"anders und laenger")
    r = run(env, item)
    assert (r.done, r.failed) == (0, 1)
    failed = env.get(item.id)
    assert failed.status == "error" and "seit dem Scan verändert" in failed.error
    assert env.p("Downloads/Rel/m.mkv").exists() and not env.p("Filme_2D/M (2020)").exists()
    assert env.history() == []


def test_execute_missing_source_is_error(env):
    item = env.item_to_filme_2d()
    env.p("Downloads/Rel/m.mkv").unlink()
    r = run(env, item)
    assert r.failed == 1 and "Quelle" in env.get(item.id).error


def test_execute_only_confirmed_items(env):
    item = env.item("Downloads/Rel/m.mkv", TARGET_2D, status="new")
    r = run(env, item)
    assert (r.done, r.failed) == (0, 0)
    assert env.get(item.id).status == "new" and env.p("Downloads/Rel/m.mkv").exists()


def test_execute_target_exists_without_decision(env):
    write(env.p(TARGET_2D), b"alt")
    item = env.item_to_filme_2d()
    r = run(env, item)
    assert r.failed == 1
    assert env.p(TARGET_2D).read_bytes() == b"alt" and env.p("Downloads/Rel/m.mkv").exists()


def test_execute_undecided_conflict_is_error(env):
    write(env.p("Filme_2D/M (2020)/M (2020) 720p.mkv"), b"alt")
    item = env.item("Downloads/Rel/m.mkv", TARGET_2D, existing=["Filme_2D/M (2020)/M (2020) 720p.mkv"])
    r = run(env, item)
    failed = env.get(item.id)
    assert r.failed == 1 and failed.error == "Konflikt nicht entschieden"
    assert env.p("Downloads/Rel/m.mkv").exists() and env.p("Filme_2D/M (2020)/M (2020) 720p.mkv").exists()


def test_execute_skip_resets_to_new(env):
    old = "Filme_2D/M (2020)/M (2020) 720p.mkv"
    write(env.p(old), b"alt")
    item = env.item("Downloads/Rel/m.mkv", TARGET_2D, existing=[old], decision="skip")
    r = run(env, item)
    assert (r.done, r.failed) == (0, 0)
    assert env.get(item.id).status == "new"
    assert env.p("Downloads/Rel/m.mkv").exists() and env.p(old).exists() and env.history() == []


def test_execute_keep_new_deletes_old_version_and_subs(env):
    old_dir = "Filme_2D/M (2020)"
    write(env.p(f"{old_dir}/M (2020) 720p.mkv"), b"alt")
    write(env.p(f"{old_dir}/M (2020) 720p.de.srt"), b"alt-sub")
    write(env.p(f"{old_dir}/M (2020) 720p Extended.mkv"), b"andere")
    write(env.p(f"{old_dir}/M (2020) 720p Extended.de.srt"), b"andere-sub")
    item = env.item("Downloads/Rel/m.mkv", TARGET_2D, existing=[f"{old_dir}/M (2020) 720p.mkv"],
                    suggestion="keep_new", decision="keep_new")
    r = run(env, item)
    assert r.done == 1
    assert env.p(TARGET_2D).read_bytes() == b"video"
    assert not env.p(f"{old_dir}/M (2020) 720p.mkv").exists()
    assert not env.p(f"{old_dir}/M (2020) 720p.de.srt").exists()
    # Untertitel einer anderen Version mit längerem Namen bleiben
    assert env.p(f"{old_dir}/M (2020) 720p Extended.de.srt").exists()
    deleted = [h.old_path for h in env.history() if h.action == "deleted"]
    assert sorted(deleted) == sorted([str(env.p(f"{old_dir}/M (2020) 720p.mkv")),
                                      str(env.p(f"{old_dir}/M (2020) 720p.de.srt"))])


def test_execute_keep_new_replaces_file_at_target(env):
    sub_target = "Filme_2D/M (2020)/M (2020) 1080p h264 AC3 [DE].de.srt"
    write(env.p(TARGET_2D), b"alt")
    write(env.p(sub_target), b"alt-sub")
    item = env.item("Downloads/Rel/m.mkv", TARGET_2D, subs=[("Downloads/Rel/m.de.srt", sub_target)],
                    existing=[TARGET_2D], decision="keep_new")
    r = run(env, item)
    assert r.done == 1
    assert env.p(TARGET_2D).read_bytes() == b"video" and env.p(sub_target).read_bytes() == b"sub"
    assert [h.action for h in env.history()] == ["moved", "deleted", "deleted", "moved", "folder_deleted"]
    assert not list(env.p("Filme_2D/M (2020)").glob("*.mediasort-old"))


def test_execute_keep_new_target_occupied_by_unrelated_file(env):
    write(env.p(TARGET_2D), b"fremd")
    write(env.p("Filme_2D/M (2020)/M (2020) 720p.mkv"), b"alt")
    item = env.item("Downloads/Rel/m.mkv", TARGET_2D, existing=["Filme_2D/M (2020)/M (2020) 720p.mkv"],
                    decision="keep_new")
    r = run(env, item)
    assert r.failed == 1
    assert env.p(TARGET_2D).read_bytes() == b"fremd"
    assert env.p("Filme_2D/M (2020)/M (2020) 720p.mkv").exists() and env.p("Downloads/Rel/m.mkv").exists()


def test_execute_keep_new_changed_source_deletes_nothing(env):
    write(env.p(TARGET_2D), b"alt")
    item = env.item("Downloads/Rel/m.mkv", TARGET_2D, existing=[TARGET_2D], decision="keep_new")
    env.p("Downloads/Rel/m.mkv").write_bytes("verändert!".encode())
    r = run(env, item)
    assert r.failed == 1 and env.p(TARGET_2D).read_bytes() == b"alt"


def test_execute_subtitle_target_occupied_touches_nothing(env):
    sub_target = "Filme_2D/M (2020)/M (2020) 1080p h264 AC3 [DE].de.srt"
    write(env.p(sub_target), b"fremd")
    item = env.item("Downloads/Rel/m.mkv", TARGET_2D, subs=[("Downloads/Rel/m.de.srt", sub_target)])
    r = run(env, item)
    assert r.failed == 1
    assert env.p("Downloads/Rel/m.mkv").exists() and not env.p(TARGET_2D).exists()
    assert env.p(sub_target).read_bytes() == b"fremd" and env.history() == []


def test_execute_keep_existing_deletes_new_source(env):
    old = "Filme_2D/M (2020)/M (2020) 2160p.mkv"
    write(env.p(old), b"besser")
    write(env.p("Downloads/Rel/m.nfo"))
    item = env.item("Downloads/Rel/m.mkv", TARGET_2D, subs=[("Downloads/Rel/m.de.srt", None)],
                    existing=[old], decision="keep_existing")
    r = run(env, item)
    assert r.done == 1 and env.get(item.id).status == "done"
    assert env.p(old).read_bytes() == b"besser" and not env.p(TARGET_2D).exists()
    assert not env.p("Downloads/Rel").exists()
    assert [h.action for h in env.history()] == ["deleted", "deleted", "folder_deleted"]


def test_execute_keep_existing_requires_existing_version(env):
    item = env.item("Downloads/Rel/m.mkv", TARGET_2D, existing=["Filme_2D/M (2020)/weg.mkv"],
                    decision="keep_existing")
    r = run(env, item)
    assert r.failed == 1 and env.p("Downloads/Rel/m.mkv").exists()


def test_execute_keep_both_needs_free_target(env):
    write(env.p(TARGET_2D), b"alt")
    item = env.item("Downloads/Rel/m.mkv", TARGET_2D, existing=[TARGET_2D], decision="keep_both")
    r = run(env, item)
    failed = env.get(item.id)
    assert r.failed == 1 and failed.error == "Ziel belegt"
    assert env.p(TARGET_2D).read_bytes() == b"alt" and env.p("Downloads/Rel/m.mkv").exists()


def test_execute_keep_both_moves_and_keeps_existing(env):
    old = "Filme_2D/M (2020)/M (2020) 720p.mkv"
    write(env.p(old), b"alt")
    item = env.item("Downloads/Rel/m.mkv", TARGET_2D, existing=[old], decision="keep_both")
    r = run(env, item)
    assert r.done == 1 and env.p(old).exists() and env.p(TARGET_2D).exists()


def test_execute_resolves_same_batch_duplicate(env):
    first = env.item("Downloads/A/m.mkv", TARGET_2D, data=b"erste")
    second = env.item("Downloads/B/m.mkv", TARGET_2D, existing=["Downloads/A/m.mkv"],
                      decision="keep_new", data=b"zweite")
    r = run(env, first, second)
    assert (r.done, r.failed) == (2, 0)
    assert env.p(TARGET_2D).read_bytes() == b"zweite"
    assert not env.p("Downloads/A").exists() and not env.p("Downloads/B").exists()


def test_execute_existing_gone_is_skipped(env):
    item = env.item("Downloads/Rel/m.mkv", TARGET_2D, existing=["Filme_2D/M (2020)/weg.mkv"],
                    decision="keep_new")
    r = run(env, item)
    assert r.done == 1 and env.p(TARGET_2D).exists()


def test_execute_continues_after_error(env):
    bad = env.item("Downloads/A/m.mkv", "Filme_2D/A (2020)/A (2020).mkv")
    env.p("Downloads/A/m.mkv").write_bytes(b"geaendert")
    good = env.item("Downloads/B/m.mkv", "Filme_2D/B (2020)/B (2020).mkv")
    progress = []
    r = execute(env.sf, [bad.id, good.id], settings=env.settings, job_id=1,
                progress=lambda d, t: progress.append((d, t)))
    assert (r.done, r.failed) == (1, 1)
    assert env.get(bad.id).status == "error" and env.get(good.id).status == "done"
    assert progress == [(1, 2), (2, 2)]


def test_execute_unexpected_exception_marks_error_and_keeps_history(env, monkeypatch):
    item = env.item("Downloads/Rel/m.mkv", TARGET_2D,
                    subs=[("Downloads/Rel/m.de.srt", "Filme_2D/M (2020)/M (2020) 1080p h264 AC3 [DE].de.srt")])
    real_move = executor.move_file

    def move(src, dst):
        if src.suffix == ".srt":
            raise PermissionError(errno.EACCES, "Keine Berechtigung", str(src))
        real_move(src, dst)

    monkeypatch.setattr(executor, "move_file", move)
    r = run(env, item)
    failed = env.get(item.id)
    assert r.failed == 1 and failed.status == "error"
    assert failed.error.startswith("Dateifehler:") and "Keine Berechtigung" in failed.error
    assert [h.action for h in env.history()] == ["moved"]
    assert env.p("Downloads/Rel/m.de.srt").exists()


def test_missing_library_root_aborts_without_creating(env):
    shutil.rmtree(env.root / "Filme_2D")
    r = run(env, env.item_to_filme_2d())
    assert r.failed == 1 and not (env.root / "Filme_2D").exists()


def test_missing_library_root_message(env):
    item = env.item_to_filme_2d()
    shutil.rmtree(env.root / "Filme_2D")
    run(env, item)
    assert env.get(item.id).error == "Bibliotheksordner fehlt – ist die Freigabe eingebunden?"


def test_target_outside_libraries_is_error(env):
    item = env.item("Downloads/Rel/m.mkv", "Woanders/M (2020)/M.mkv")
    r = run(env, item)
    assert r.failed == 1 and not env.p("Woanders").exists()


def test_touched_contains_target_and_source_dirs(env):
    old = "Filme_2D/M (2019)/M (2019).mkv"
    write(env.p(old), b"alt")
    item = env.item("Downloads/Rel/m.mkv", TARGET_2D, existing=[old], decision="keep_new")
    r = run(env, item)
    assert r.touched == {env.p("Filme_2D/M (2020)"), env.p("Downloads/Rel"), env.p("Filme_2D/M (2019)")}


def test_execute_logs_job_id(env):
    from mediasort.db import Job

    with env.sf() as s:
        job = Job(kind="execute")
        s.add(job)
        s.commit()
    item = env.item_to_filme_2d()
    execute(env.sf, [item.id], settings=env.settings, job_id=job.id)
    assert {h.job_id for h in env.history()} == {job.id}
    assert not any(h.auto for h in env.history())


def test_execute_video_directly_in_input_root_keeps_root(env):
    item = env.item("Downloads/m.mkv", TARGET_2D)
    r = run(env, item)
    assert r.done == 1 and env.p("Downloads").is_dir()
    assert [h.action for h in env.history()] == ["moved"]


# --- Nachbesserung Review 1 -----------------------------------------------------------


def test_library_rename_in_same_folder_keeps_folder(env):
    folder = "Filme_2D/Sample People (2000)"
    sparse(env.p(f"{folder}/spl-1080p.mkv"), 300 * MB)
    target = f"{folder}/Sample People (2000) 1080p h264 AC3 [DE].mkv"
    item = env.item(f"{folder}/spl-1080p.mkv", target)
    r = run(env, item)
    assert r.done == 1 and env.p(target).exists()
    assert [h.action for h in env.history()] == ["moved"]


def test_target_below_source_folder_is_never_cleaned(env):
    # Quelle liegt direkt im Film-Ordner, das Ziel in einem Unterordner davon: der
    # Ordner darf auch dann nicht weg, wenn das Ziel als Sample durchgehen würde.
    sparse(env.p("Filme_2D/M (2020)/m.mkv"), 300 * MB)
    target = "Filme_2D/M (2020)/Sample/M (2020) sample.mkv"
    item = env.item("Filme_2D/M (2020)/m.mkv", target)
    r = run(env, item)
    assert r.done == 1 and env.p(target).exists()


def test_collateral_movie_with_sample_in_title_is_kept(env):
    other = sparse(env.p("Downloads/Pack/Sample.People.2000.1080p.mkv"), 3000 * MB)
    sparse(env.p("Downloads/Pack/Other.Movie.2001.1080p.mkv"), 3000 * MB)
    item = env.item("Downloads/Pack/Other.Movie.2001.1080p.mkv", "Filme_2D/Other Movie (2001)/Other Movie (2001).mkv")
    r = run(env, item)
    assert r.done == 1 and other.exists()


def test_stale_done_item_of_other_scan_is_not_resolved(env):
    unrelated = write(env.p("Filme_2D/X (1999)/X (1999).mkv"), b"fremd")
    with env.sf() as s:
        s.add(Item(source=str(env.p("Downloads/A/movie.mkv")), size=1, mtime=1, target=str(unrelated),
                   status="done", scan_id=2))
        s.add(History(ts=1, action="moved", old_path=str(env.p("Downloads/A/movie.mkv")),
                      new_path=str(unrelated)))
        s.commit()
    item = env.item("Downloads/B/movie.mkv", "Filme_2D/Y (2020)/Y (2020).mkv",
                    existing=["Downloads/A/movie.mkv"], decision="keep_new")
    r = run(env, item)
    assert r.done == 1 and unrelated.read_bytes() == b"fremd"


def test_stale_done_item_without_history_is_not_resolved(env):
    unrelated = write(env.p("Filme_2D/X (1999)/X (1999).mkv"), b"fremd")
    with env.sf() as s:
        s.add(Item(source=str(env.p("Downloads/A/movie.mkv")), size=1, mtime=1, target=str(unrelated),
                   status="done", scan_id=1))
        s.commit()
    item = env.item("Downloads/B/movie.mkv", "Filme_2D/Y (2020)/Y (2020).mkv",
                    existing=["Downloads/A/movie.mkv"], decision="keep_new")
    run(env, item)
    assert unrelated.read_bytes() == b"fremd"


def test_same_scan_move_from_earlier_job_is_resolved(env):
    first = env.item("Downloads/A/m.mkv", TARGET_2D, data=b"erste")
    run(env, first)
    second = env.item("Downloads/B/m.mkv", TARGET_2D, existing=["Downloads/A/m.mkv"],
                      decision="keep_new", data=b"zweite")
    r = execute(env.sf, [second.id], settings=env.settings, job_id=2)
    assert r.done == 1 and env.p(TARGET_2D).read_bytes() == b"zweite"


def test_keep_existing_source_is_not_resolved_as_moved(env):
    old = write(env.p("Filme_2D/M (2020)/M (2020) 2160p.mkv"), b"besser")
    first = env.item("Downloads/A/m.mkv", "Filme_2D/M (2020)/M (2020) 2160p.mkv",
                     existing=["Filme_2D/M (2020)/M (2020) 2160p.mkv"], decision="keep_existing")
    second = env.item("Downloads/B/m.mkv", "Filme_2D/M (2020)/M (2020) 720p.mkv",
                      existing=["Downloads/A/m.mkv"], decision="keep_new")
    r = run(env, first, second)
    assert (r.done, r.failed) == (2, 0)
    assert old.read_bytes() == b"besser"


def test_keep_new_restores_occupying_file_when_move_fails(env, monkeypatch):
    sub_target = "Filme_2D/M (2020)/M (2020) 1080p h264 AC3 [DE].de.srt"
    write(env.p(TARGET_2D), b"alt")
    write(env.p(sub_target), b"alt-sub")
    item = env.item("Downloads/Rel/m.mkv", TARGET_2D, subs=[("Downloads/Rel/m.de.srt", sub_target)],
                    existing=[TARGET_2D], decision="keep_new")
    real_move = executor.move_file

    def move(src, dst):
        if src == env.p("Downloads/Rel/m.mkv"):
            raise OSError(errno.EIO, "Ein-/Ausgabefehler", str(src))
        real_move(src, dst)

    monkeypatch.setattr(executor, "move_file", move)
    r = run(env, item)
    assert r.failed == 1
    assert env.p(TARGET_2D).read_bytes() == b"alt" and env.p(sub_target).read_bytes() == b"alt-sub"
    assert env.p("Downloads/Rel/m.mkv").read_bytes() == b"video"
    assert not list(env.p("Filme_2D/M (2020)").glob("*.mediasort-old"))
    assert env.history() == []


def test_keep_new_refuses_when_parking_name_taken(env):
    write(env.p(TARGET_2D), b"alt")
    write(env.p(TARGET_2D + ".mediasort-old"), b"rest")
    item = env.item("Downloads/Rel/m.mkv", TARGET_2D, existing=[TARGET_2D], decision="keep_new")
    r = run(env, item)
    assert r.failed == 1 and env.p(TARGET_2D).read_bytes() == b"alt"
    assert env.p(TARGET_2D + ".mediasort-old").read_bytes() == b"rest"


def test_execute_source_not_removed_logs_move_and_reports(env, monkeypatch):
    item = env.item_to_filme_2d()
    src = env.p("Downloads/Rel/m.mkv")
    _exdev_once(monkeypatch)
    real_unlink = os.unlink

    def unlink(path, *a, **kw):
        if Path(path) == src:
            raise PermissionError(errno.EACCES, "Keine Berechtigung", str(path))
        return real_unlink(path, *a, **kw)

    monkeypatch.setattr(executor.os, "unlink", unlink)
    r = run(env, item)
    failed = env.get(item.id)
    assert r.failed == 1 and failed.status == "error" and "doppelt" in failed.error
    assert env.p(TARGET_2D).exists() and src.exists()
    assert [(h.action, h.new_path) for h in env.history()] == [("moved", str(env.p(TARGET_2D)))]


def test_db_error_marks_item_and_job_continues(env, monkeypatch):
    from sqlalchemy.orm import Session

    first = env.item("Downloads/A/m.mkv", "Filme_2D/A (2020)/A (2020).mkv")
    second = env.item("Downloads/B/m.mkv", "Filme_2D/B (2020)/B (2020).mkv")
    real_commit = Session.commit
    calls = []

    def commit(self):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("Datenbank gesperrt")
        return real_commit(self)

    monkeypatch.setattr(Session, "commit", commit)
    r = run(env, first, second)
    assert (r.done, r.failed) == (1, 1)
    failed = env.get(first.id)
    assert failed.status == "error" and failed.error == "Datenbankfehler: Datenbank gesperrt"
    assert env.get(second.id).status == "done"
    moved = [h.old_path for h in env.history() if h.action == "moved"]
    assert moved == [str(env.p("Downloads/A/m.mkv")), str(env.p("Downloads/B/m.mkv"))]
