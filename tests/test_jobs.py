import threading

import pytest

from mediasort.db import Job
from mediasort.jobs import JobRunner


class Clock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def sf(sessionmaker_):
    return sessionmaker_


@pytest.fixture
def runner(sf):
    r = JobRunner(sf, now=Clock())
    r.start()
    yield r
    r.stop()


def job(sf, job_id: int) -> Job:
    with sf() as s:
        return s.get(Job, job_id)


def fast(job_id, progress):
    return "fertig"


def boom(job_id, progress):
    raise RuntimeError("kaputt")


def test_runner_runs_one_at_a_time(runner, sf):
    release = threading.Event()

    def slow(job_id, progress):
        release.wait(5)
        return "langsam fertig"

    a = runner.submit("x", slow)
    assert a is not None
    assert runner.busy()
    assert runner.submit("y", fast) is None
    release.set()
    assert runner.wait_idle(5)
    assert not runner.busy()
    row = job(sf, a)
    assert (row.kind, row.status, row.message) == ("x", "done", "langsam fertig")
    assert row.started == 1000.0 and row.finished == 1000.0
    with sf() as s:
        assert s.query(Job).count() == 1
    # danach ist wieder Platz
    b = runner.submit("y", fast)
    assert b is not None and runner.wait_idle(5)
    assert job(sf, b).status == "done"


def test_runner_records_error(runner, sf):
    jid = runner.submit("x", boom)
    assert runner.wait_idle(5)
    row = job(sf, jid)
    assert row.status == "error"
    assert "kaputt" in row.message
    assert row.finished is not None
    # der Arbeits-Thread lebt weiter
    jid2 = runner.submit("x", fast)
    assert runner.wait_idle(5)
    assert job(sf, jid2).status == "done"


def test_runner_job_row_lifecycle_and_progress(runner, sf):
    seen = {}
    release = threading.Event()
    reported = threading.Event()

    def work(job_id, progress):
        seen["id"] = job_id
        progress(3, 7)
        reported.set()
        release.wait(5)

    jid = runner.submit("scan", work, auto=True)
    assert reported.wait(5)
    row = job(sf, jid)
    assert (row.status, row.progress, row.total, row.auto) == ("running", 3, 7, True)
    release.set()
    assert runner.wait_idle(5)
    assert seen["id"] == jid
    row = job(sf, jid)
    assert row.status == "done" and row.message is None


def test_submit_before_start_stays_queued(sf):
    r = JobRunner(sf)
    jid = r.submit("x", fast)
    assert job(sf, jid).status == "queued"
    assert r.busy()
    assert not r.wait_idle(0.05)
    r.start()
    r.start()  # idempotent
    try:
        assert r.wait_idle(5)
        assert job(sf, jid).status == "done"
    finally:
        r.stop()


def test_wait_idle_when_idle(runner):
    assert runner.wait_idle(0)
