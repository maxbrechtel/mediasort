import os
from pathlib import Path

import pytest
from sqlalchemy import select

from mediasort.db import FileState, Folder, Item, Job, Scan
from mediasort.jobs import JobRunner
from mediasort.settings import load_settings, save_settings
from mediasort.watcher import Watcher, auto_eligible, is_stable

T0 = 1_000_000.0


def FS(path, size, mtime, last_seen=0.0) -> FileState:
    return FileState(path=path, size=size, mtime=mtime, last_seen=last_seen)


def test_is_stable():
    assert not is_stable("a", 10, 1000, now=1300, min_age_s=600, state=None)
    assert not is_stable("a", 10, 1000, now=2000, min_age_s=600, state=None)  # erstes Sehen
    assert is_stable("a", 10, 1000, now=2000, min_age_s=600, state=FS("a", 10, 1000))
    assert not is_stable("a", 11, 1000, now=2000, min_age_s=600, state=FS("a", 10, 1000))
    assert not is_stable("a", 10, 1001, now=2000, min_age_s=600, state=FS("a", 10, 1000))
    assert not is_stable("a", 10, 1000, now=1300, min_age_s=600, state=FS("a", 10, 1000))  # zu jung
    assert is_stable("a", 10, 1000, now=1600, min_age_s=600, state=FS("a", 10, 1000))  # genau Mindestalter


def _item(**kw) -> Item:
    base = {"source": "/in/a.mkv", "size": 1, "mtime": 1.0, "confidence": "hoch", "conflict_json": None,
            "status": "new", "error": None, "target": "/lib/A (2000)/A (2000).mkv"}
    return Item(**{**base, **kw})


def test_auto_eligible():
    assert auto_eligible(_item())
    assert not auto_eligible(_item(confidence="mittel"))
    assert not auto_eligible(_item(confidence="niedrig"))
    assert not auto_eligible(_item(confidence=None))
    assert not auto_eligible(_item(conflict_json={"existing": "/lib/x.mkv"}))
    assert not auto_eligible(_item(status="error", error="ffprobe fehlgeschlagen"))
    assert not auto_eligible(_item(error="ffprobe fehlgeschlagen"))
    assert not auto_eligible(_item(target=None))
    assert not auto_eligible(_item(status="confirmed"))
    assert not auto_eligible(_item(status="done"))


# --- Watcher --------------------------------------------------------------------


class Clock:
    def __init__(self, t: float):
        self.t = t

    def __call__(self) -> float:
        return self.t


class Env:
    """Ordner in der DB, Dateien auf der Platte, aufgezeichnete Aufrufe von plan/run_items."""

    def __init__(self, tmp_path: Path, sm):
        self.tmp = tmp_path
        self.sm = sm
        self.clock = Clock(T0)
        self.planned: list[tuple[int, bool]] = []
        self.ran: list[tuple[list[int], bool]] = []
        self.plan_effect = {}  # folder_id -> callable(folder) oder Exception
        self.busy = False
        self.submitted = []
        self.ran_jobs: list[int | None] = []
        self.ran_status: list[str] = []  # Status der Items beim Aufruf von run_items

    # Konfiguration
    def settings(self, **kw):
        with self.sm() as s:
            st = load_settings(s)
            for k, v in kw.items():
                setattr(st, k, v)
            save_settings(s, st)

    def folder(self, name: str, role="input", auto_level="off") -> int:
        path = self.tmp / name
        path.mkdir(exist_ok=True)
        with self.sm() as s:
            f = Folder(path=str(path), role=role, auto_level=auto_level)
            s.add(f)
            s.commit()
            return f.id

    def file(self, rel: str, size: int = 10, age_s: float = 3600) -> Path:
        p = self.tmp / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * size)
        mtime = self.clock.t - age_s
        os.utime(p, (mtime, mtime))
        return p

    def item(self, folder_id: int, path: Path, scan_id: int | None = None, **kw) -> int:
        st = path.stat()
        base = {"folder_id": folder_id, "scan_id": scan_id, "source": str(path), "size": st.st_size,
                "mtime": st.st_mtime, "confidence": "hoch", "status": "new",
                "target": str(self.tmp / "lib" / path.name)}
        with self.sm() as s:
            it = Item(**{**base, **kw})
            s.add(it)
            s.commit()
            return it.id

    def state(self, path: Path, size=None, mtime=None):
        st = path.stat()
        with self.sm() as s:
            s.merge(FS(str(path), st.st_size if size is None else size,
                       st.st_mtime if mtime is None else mtime, T0 - 900))
            s.commit()

    def get_state(self, path: Path) -> FileState | None:
        with self.sm() as s:
            return s.get(FileState, str(path))

    # Abhängigkeiten des Watchers
    def plan(self, folder: Folder, auto: bool) -> int:
        self.planned.append((folder.id, auto))
        effect = self.plan_effect.get(folder.id)
        if isinstance(effect, Exception):
            raise effect
        with self.sm() as s:
            scan = Scan(folder_id=folder.id, started=self.clock.t, finished=self.clock.t, status="done", auto=auto)
            s.add(scan)
            s.commit()
            scan_id = scan.id
        if effect is not None:
            effect(folder, scan_id)
        return scan_id

    def run_items(self, ids: list[int], auto: bool, job_id: int | None) -> None:
        self.ran.append((list(ids), auto))
        self.ran_jobs.append(job_id)
        with self.sm() as s:
            for i in ids:
                it = s.get(Item, i)
                self.ran_status.append(it.status)
                it.status = "done"
            s.commit()

    def status(self, item_id: int) -> str:
        with self.sm() as s:
            return s.get(Item, item_id).status

    def set_level(self, folder_id: int, level: str):
        with self.sm() as s:
            s.get(Folder, folder_id).auto_level = level
            s.commit()


@pytest.fixture
def env(tmp_path, sessionmaker_):
    return Env(tmp_path, sessionmaker_)


class FakeRunner:
    def __init__(self, env: Env):
        self.env = env

    def busy(self) -> bool:
        return self.env.busy

    def submit(self, kind, fn, *, auto=False):
        self.env.submitted.append((kind, fn, auto))
        return 1


@pytest.fixture
def w(env, sessionmaker_):
    return Watcher(sessionmaker_, FakeRunner(env), env.plan, env.run_items, now=env.clock)


def test_tick_master_switch_off_does_nothing(env, w):
    fid = env.folder("in", auto_level="auto")
    p = env.file("in/a.mkv")
    env.item(fid, p)
    env.state(p)
    assert w.tick() == "Automatik ist ausgeschaltet"
    assert env.planned == [] and env.ran == []


def test_tick_detect_plans_but_does_not_execute(env, w):
    env.settings(auto_enabled=True)
    fid = env.folder("in", auto_level="detect")
    p = env.file("in/a.mkv")
    env.state(p)  # stabil und hoch, aber nur erkennen
    env.plan_effect[fid] = lambda folder, scan_id: env.item(fid, p, scan_id=scan_id)
    result = w.tick()
    assert env.planned == [(fid, True)]
    assert env.ran == []
    assert result == "1 Ordner geprüft, 1 neuer Vorschlag, 0 automatisch einsortiert"
    # Dateizustand wird trotzdem fortgeschrieben
    assert env.get_state(p).last_seen == T0


def test_tick_off_folder_is_not_planned(env, w):
    env.settings(auto_enabled=True)
    fid = env.folder("in", auto_level="off")
    p = env.file("in/a.mkv")
    env.item(fid, p)
    assert w.tick() == "0 Ordner geprüft, 0 neue Vorschläge, 0 automatisch einsortiert"
    assert env.planned == [] and env.ran == []
    assert env.get_state(p) is None


def test_tick_auto_executes_only_eligible_and_stable(env, w):
    env.settings(auto_enabled=True, min_age_minutes=10)
    fid = env.folder("in", auto_level="auto")
    stable = env.file("in/stable.mkv")
    fresh = env.file("in/fresh.mkv")  # zum ersten Mal gesehen
    unsure = env.file("in/unsure.mkv")
    young = env.file("in/young.mkv", age_s=60)
    grown = env.file("in/grown.mkv", size=20)
    conflict = env.file("in/conflict.mkv")
    a = env.item(fid, stable)
    b = env.item(fid, fresh)
    env.item(fid, unsure, confidence="mittel")
    y = env.item(fid, young)
    g = env.item(fid, grown)
    env.item(fid, conflict, conflict_json={"existing": "/lib/x.mkv"})
    for p in (stable, unsure, young, conflict):
        env.state(p)
    env.state(grown, size=10)

    result = w.tick(job_id=42)
    assert env.ran == [([a], True)]
    assert env.ran_jobs == [42]
    assert env.ran_status == ["confirmed"]  # der Executor führt nur bestätigte Vorschläge aus
    assert result == "1 Ordner geprüft, 0 neue Vorschläge, 1 automatisch einsortiert"
    # FileState wurde für alle offenen Items aktualisiert
    assert env.get_state(fresh).size == 10
    assert env.get_state(grown).size == 20

    # nächste Abfrage: erstmals gesehene, inzwischen alte und nicht mehr wachsende Dateien
    # sind jetzt stabil; unsichere und Konflikte bleiben liegen
    env.clock.t += 900
    assert w.tick() == "1 Ordner geprüft, 0 neue Vorschläge, 3 automatisch einsortiert"
    assert env.ran[-1] == ([b, y, g], True)


def test_tick_auto_includes_items_from_this_ticks_plan_only_when_stable(env, w):
    env.settings(auto_enabled=True)
    fid = env.folder("in", auto_level="auto")
    p = env.file("in/a.mkv")
    env.plan_effect[fid] = lambda folder, scan_id: env.item(fid, p, scan_id=scan_id)
    assert w.tick() == "1 Ordner geprüft, 1 neuer Vorschlag, 0 automatisch einsortiert"
    assert env.ran == []
    env.plan_effect.pop(fid)
    env.clock.t += 900
    assert w.tick() == "1 Ordner geprüft, 0 neue Vorschläge, 1 automatisch einsortiert"
    assert len(env.ran) == 1


def test_tick_missing_file_is_skipped(env, w):
    env.settings(auto_enabled=True)
    fid = env.folder("in", auto_level="auto")
    p = env.file("in/a.mkv")
    env.item(fid, p)
    env.state(p)
    p.unlink()
    assert w.tick().endswith("0 automatisch einsortiert")
    assert env.ran == []


def test_tick_planner_error_is_reported_and_others_continue(env, w):
    env.settings(auto_enabled=True)
    bad = env.folder("bad", auto_level="detect")
    good = env.folder("good", auto_level="auto")
    p = env.file("good/a.mkv")
    a = env.item(good, p)
    env.state(p)
    env.plan_effect[bad] = RuntimeError("Ordner nicht erreichbar")
    result = w.tick()
    assert [fid for fid, _ in env.planned] == [bad, good]
    assert env.ran == [([a], True)]
    assert result.startswith("1 Ordner geprüft, 0 neue Vorschläge, 1 automatisch einsortiert")
    assert f"Fehler in {env.tmp / 'bad'}: Ordner nicht erreichbar" in result


def test_library_folders_never_automatic(env, w):
    env.settings(auto_enabled=True)
    lib = env.folder("lib", role="library", auto_level="auto")
    p = env.file("lib/a.mkv")
    env.item(lib, p)
    env.state(p)
    assert w.tick() == "0 Ordner geprüft, 0 neue Vorschläge, 0 automatisch einsortiert"
    assert env.planned == [] and env.ran == []


def test_tick_skipped_when_busy(env, w):
    env.settings(auto_enabled=True)
    env.busy = True
    assert w.trigger() == "übersprungen: Auftrag läuft"
    assert env.submitted == []


def test_trigger_switched_off_submits_nothing(env, w):
    assert w.trigger() == "Automatik ist ausgeschaltet"
    assert env.submitted == []


def test_trigger_submits_tick_as_auto_job(env, w):
    env.settings(auto_enabled=True)
    fid = env.folder("in", auto_level="auto")
    p = env.file("in/a.mkv")
    a = env.item(fid, p)
    env.state(p)
    assert w.trigger() == "gestartet"
    (kind, fn, auto), = env.submitted
    assert (kind, auto) == ("auto", True)
    reported = []
    assert fn(7, lambda done, total: reported.append((done, total))) == (
        "1 Ordner geprüft, 0 neue Vorschläge, 1 automatisch einsortiert")
    assert env.ran == [([a], True)] and env.ran_jobs == [7]
    assert reported[-1] == (1, 1)


def test_trigger_with_real_runner_records_result(env, sessionmaker_):
    env.settings(auto_enabled=True)
    runner = JobRunner(sessionmaker_, now=env.clock)
    runner.start()
    try:
        w = Watcher(sessionmaker_, runner, env.plan, env.run_items, now=env.clock)
        assert w.trigger() == "gestartet"
        assert runner.wait_idle(5)
        with sessionmaker_() as s:
            job = s.scalars(select(Job).where(Job.kind == "auto")).one()
        assert (job.status, job.auto, job.progress, job.total) == ("done", True, 0, 0)
        assert job.message == "0 Ordner geprüft, 0 neue Vorschläge, 0 automatisch einsortiert"
    finally:
        runner.stop()


def test_tick_rechecks_master_switch_before_executing(env, w):
    env.settings(auto_enabled=True)
    fid = env.folder("in", auto_level="auto")
    p = env.file("in/a.mkv")
    a = env.item(fid, p)
    env.state(p)
    env.plan_effect[fid] = lambda folder, scan_id: env.settings(auto_enabled=False)
    assert w.tick() == "1 Ordner geprüft, 0 neue Vorschläge, 0 automatisch einsortiert"
    assert env.ran == []
    assert env.status(a) == "new"


def test_tick_rechecks_folder_level_before_executing(env, w):
    env.settings(auto_enabled=True)
    fid = env.folder("in", auto_level="auto")
    p = env.file("in/a.mkv")
    a = env.item(fid, p)
    env.state(p)
    env.plan_effect[fid] = lambda folder, scan_id: env.set_level(fid, "detect")
    assert w.tick().endswith("0 automatisch einsortiert")
    assert env.ran == []
    assert env.status(a) == "new"


def test_tick_does_not_confirm_item_ignored_meanwhile(env, w, monkeypatch):
    env.settings(auto_enabled=True)
    fid = env.folder("in", auto_level="auto")
    p, q = env.file("in/a.mkv"), env.file("in/b.mkv")
    a, b = env.item(fid, p), env.item(fid, q)
    env.state(p)
    env.state(q)
    observe = w._observe

    def observe_then_user_ignores(folders, min_age_s):
        ids = observe(folders, min_age_s)
        assert ids == [a, b]
        with env.sm() as s:
            s.get(Item, b).status = "ignored"  # Nutzer ignoriert b, bevor bestätigt wird
            s.commit()
        return ids

    monkeypatch.setattr(w, "_observe", observe_then_user_ignores)
    assert w.tick().endswith("1 automatisch einsortiert")
    assert env.ran == [([a], True)]
    assert env.status(b) == "ignored"


def test_tick_skips_item_whose_file_changed_since_suggestion(env, w):
    env.settings(auto_enabled=True)
    fid = env.folder("in", auto_level="auto")
    p = env.file("in/a.mkv")
    a = env.item(fid, p, size=5)  # Vorschlag stammt von einer kleineren Fassung
    env.state(p)  # Datei ist seit der letzten Abfrage unverändert
    b_path = env.file("in/b.mkv")
    st = b_path.stat()
    b = env.item(fid, b_path, mtime=st.st_mtime - 100)
    env.state(b_path)
    assert w.tick().endswith("0 automatisch einsortiert")
    assert env.ran == []
    assert env.status(a) == "new" and env.status(b) == "new"
