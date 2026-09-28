"""One job at a time, and one definition of what a pipeline is.

Two separate things are pinned here.

The first is a refactor: `runner._run_inline` and `tasks.run_pipeline_task` were
near-identical copies of the same loop, and they had drifted. The Celery copy
could never publish `{"type": "pipeline", "status": "failed"}` -- `run_stage_task`
re-raises, so the exception left the function and the FAILED check written after
it was unreachable. A browser watching a Celery run was told the stage failed and
never that the pipeline had stopped.

The second is the queue itself: the executor ran `max_workers=2`, so two reels --
and therefore two LLM workloads -- ran at once against one model server.
"""
from __future__ import annotations

import inspect



def test_both_executors_walk_through_the_same_function():
    """The drift this refactor removes. If either grows its own loop again, the
    bug above comes back with it."""
    from app import runner, tasks
    from app.stages import pipeline

    assert callable(pipeline.walk)

    inline = inspect.getsource(runner._run_inline)
    celery = inspect.getsource(tasks.run_pipeline_task)
    for source, name in ((inline, "runner._run_inline"),
                         (celery, "tasks.run_pipeline_task")):
        assert "walk(" in source, f"{name} no longer delegates to pipeline.walk"
        assert "while True" not in source, f"{name} grew its own loop again"


def test_the_walker_reports_why_it_stopped():
    """`walk` returns a reason rather than None, because the queue has to record
    why a job left it -- "complete" and "failed at storyboard" are different
    things to a person looking at a list."""
    source = inspect.getsource(__import__("app.stages.pipeline",
                                          fromlist=["walk"]).walk)
    for reason in ('"complete"', '"gone"', '"cancelled"',
                   '"review:"', '"failed:"', '"until:"'):
        assert reason in source, f"walk cannot report {reason}"


def test_a_failed_stage_says_the_pipeline_stopped():
    """The unreachable branch. In the old Celery walker this check sat after a
    call that re-raises, so it could never run."""
    from app.stages import pipeline

    source = inspect.getsource(pipeline.walk)
    failed_at = source.index("if state.status is Status.FAILED")
    published = source.index('"status": "failed"', failed_at)
    assert published > failed_at, "the pipeline-failed event is not published"


def test_only_one_pipeline_runs_at_a_time():
    """`ThreadPoolExecutor(max_workers=2)` is what let two reels run at once.

    A reel spends about 8.6 of its 11.3 minutes inside the two LLM stages, so
    two concurrent jobs are, in practice, two concurrent LLM workloads against
    one GPU -- which is the entire reason the queue exists.
    """
    from app import runner

    assert runner._pool._max_workers == 1, (
        "the inline executor can run more than one pipeline at a time"
    )


def test_the_per_job_lock_moved_next_to_the_walk_that_takes_it():
    from app.stages.pipeline import job_lock

    first = job_lock("job-a")
    assert job_lock("job-a") is first, "a job must get the same lock every time"
    assert job_lock("job-b") is not first, "two jobs must not share a lock"


def test_walk_on_a_deleted_job_returns_gone_rather_than_raising():
    """A job can be deleted while it is queued. The walker must report that, not
    take the scheduler down with it."""
    from app.stages.pipeline import walk
    from app.store import JobStore

    assert walk("no-such-job-at-all", JobStore()) == "gone"


# ---------------------------------------------------------------- the queue --
import pytest

from app.models.job import JobSource, QueueState
from app.store import JobStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A temporary job store, with the scheduler thread deliberately not started.

    Every test below drives `scheduler.tick()` directly. That is the point of
    splitting the tick out of the loop: ordering, cancellation and pausing are
    provable with no thread and no sleeps, so none of these can be flaky.
    """
    monkeypatch.setenv("REELFORGE_PATHS__DATA_DIR", str(tmp_path))
    monkeypatch.setenv("REELFORGE_EXECUTOR", "inline")
    import app.config
    import app.runner

    app.config.get_config.cache_clear()
    app.runner.mode.cache_clear()
    return JobStore()


def make(store, slug):
    return store.create(slug, JobSource(kind="github", url=f"https://github.com/x/{slug}"))


def fake_stages(monkeypatch, record, fail=None):
    """Replace the stage runners so a whole pipeline takes microseconds."""
    from app.stages import pipeline

    def run_stage(job, stage, store, progress=None):
        record.append((job.id, stage.value))
        if fail and stage.value == fail:
            raise RuntimeError("boom")
        job.mark(stage, pipeline.Status.DONE)
        store.save(job)
        return job

    monkeypatch.setattr(pipeline, "run_stage", run_stage)


def test_the_queue_runs_jobs_in_the_order_they_were_added(store, monkeypatch):
    from app import queue_store, scheduler

    record = []
    fake_stages(monkeypatch, record)
    ids = [make(store, name).id for name in ("first", "second", "third")]
    for job_id in ids:
        queue_store.enqueue(store, job_id)

    ran = [scheduler.tick(store) for _ in range(3)]
    assert ran == ids, "the queue did not run first-in first-out"


def test_a_second_job_does_not_start_while_one_holds_the_slot(store, monkeypatch):
    """The invariant. `ThreadPoolExecutor(max_workers=2)` ran two reels at once,
    which against one local model server is two concurrent LLM workloads."""
    from app import queue_store
    from app.models.job import QueueState as QS

    first, second = make(store, "a"), make(store, "b")
    queue_store.enqueue(store, first.id)
    queue_store.enqueue(store, second.id)

    claimed = queue_store.claim_next(store)
    assert claimed.id == first.id
    assert queue_store.claim_next(store) is None, "a second job claimed the slot"

    queue_store.release(store, first.id, reason="complete")
    assert store.load(first.id).queue.state is QS.IDLE
    assert queue_store.claim_next(store).id == second.id


def test_reordering_changes_which_job_runs_next(store, monkeypatch):
    from app import queue_store, scheduler

    record = []
    fake_stages(monkeypatch, record)
    a, b, c = (make(store, n) for n in ("a", "b", "c"))
    for job in (a, b, c):
        queue_store.enqueue(store, job.id)

    queue_store.reorder(store, [c.id, a.id])
    assert scheduler.tick(store) == c.id


def test_a_partial_reorder_keeps_the_jobs_it_did_not_mention(store):
    """A client whose list went stale because something was enqueued between its
    GET and its PUT should get a sane result, not a rejection."""
    from app import queue_store

    a, b, c = (make(store, n) for n in ("a", "b", "c"))
    for job in (a, b, c):
        queue_store.enqueue(store, job.id)

    order = queue_store.reorder(store, [c.id])
    assert order[0] == c.id
    assert set(order) == {a.id, b.id, c.id}


def test_moving_a_job_up_and_down_the_queue(store):
    from app import queue_store

    a, b, c = (make(store, n) for n in ("a", "b", "c"))
    for job in (a, b, c):
        queue_store.enqueue(store, job.id)

    assert queue_store.move(store, c.id, delta=-1) == [a.id, c.id, b.id]
    assert queue_store.move(store, c.id, delta=-1) == [c.id, a.id, b.id]
    # already at the front: nudging further is a no-op, not an error
    assert queue_store.move(store, c.id, delta=-1) == [c.id, a.id, b.id]


def test_cancelling_a_waiting_job_means_it_never_runs(store, monkeypatch):
    from app import queue_store, scheduler

    record = []
    fake_stages(monkeypatch, record)
    a, b = make(store, "a"), make(store, "b")
    queue_store.enqueue(store, a.id)
    queue_store.enqueue(store, b.id)

    queue_store.cancel(store, a.id)
    assert scheduler.tick(store) == b.id
    assert not any(job_id == a.id for job_id, _stage in record)


def test_pause_stops_the_next_job_starting_and_resume_starts_it(store, monkeypatch):
    from app import queue_store, scheduler

    record = []
    fake_stages(monkeypatch, record)
    job = make(store, "a")
    queue_store.enqueue(store, job.id)

    queue_store.set_paused(True, "testing")
    assert scheduler.tick(store) is None, "a paused queue started a job"

    queue_store.set_paused(False)
    assert scheduler.tick(store) == job.id


def test_a_queued_job_survives_a_restart(store, monkeypatch):
    """The whole reason queue state lives in job.json: a fresh store finds it."""
    from app import queue_store, scheduler

    record = []
    fake_stages(monkeypatch, record)
    job = make(store, "a")
    queue_store.enqueue(store, job.id)

    fresh = JobStore()
    assert fresh.load(job.id).queue.state is QueueState.QUEUED
    assert scheduler.recover(fresh) == [], "a merely-queued job needed recovery"
    assert scheduler.tick(fresh) == job.id


def test_an_interrupted_job_leaves_the_queue_rather_than_going_back_into_it(store):
    """Re-queueing would send it straight back to the stage that just died, and
    across a crash loop it would do that for ever."""
    from app import queue_store, scheduler

    job = make(store, "a")
    queue_store.enqueue(store, job.id)
    queue_store.claim_next(store)

    assert scheduler.recover(store) == [job.id]
    assert store.load(job.id).queue.state is QueueState.IDLE
    assert scheduler.recover(store) == [], "recovery is not idempotent"


def test_a_failing_job_does_not_stop_the_queue(store, monkeypatch):
    from app import queue_store, scheduler

    record = []
    fake_stages(monkeypatch, record, fail="ingest")
    a, b = make(store, "a"), make(store, "b")
    queue_store.enqueue(store, a.id)
    queue_store.enqueue(store, b.id)

    assert scheduler.tick(store) == a.id
    assert store.load(a.id).queue.reason.startswith("failed:")
    assert scheduler.tick(store) == b.id, "one failure emptied the queue"


def test_two_failures_in_a_row_pause_the_queue_with_a_reason(store, monkeypatch):
    """Five reels failing in ninety seconds is a broken model server, not five
    individually broken reels."""
    from app import queue_store, scheduler

    record = []
    fake_stages(monkeypatch, record, fail="ingest")
    for name in ("a", "b"):
        queue_store.enqueue(store, make(store, name).id)

    scheduler.tick(store)
    scheduler.tick(store)
    settings = queue_store.read()
    assert settings.paused
    assert "in a row" in settings.paused_reason

    # and a success clears the count
    queue_store.set_paused(False)
    assert queue_store.read().consecutive_failures == 0


def test_an_approved_reel_resumes_ahead_of_the_line_it_never_left(store, monkeypatch):
    """Approving a gate must put the reel back at the FRONT of the queue.

    It already had its turn and stopped only because it was told to. Sending it
    to the back means a reel queued while you were reading the script runs
    first and the approved one waits all over again -- and with gates on both
    `content` and `storyboard` that happens twice per reel.

    Driven through the endpoint on purpose: `queue_store.enqueue(front=True)`
    has always worked, and asserting on it proves nothing about whether the
    approve route actually asks for it.
    """
    from fastapi.testclient import TestClient

    from app import queue_store
    from app.main import app as fastapi_app
    from app.models.job import Stage, Status

    client = TestClient(fastapi_app)

    approved = make(store, "approved")
    later = make(store, "later")

    # the approved reel ran, parked at its gate, and the queue moved on
    queue_store.enqueue(store, approved.id)
    queue_store.claim_next(store)
    queue_store.release(store, approved.id, reason="review:content")
    approved.mark(Stage.CONTENT, Status.REVIEW)
    store.save(approved)
    queue_store.enqueue(store, later.id)

    response = client.post(f"/api/jobs/{approved.id}/stages/content/approve")
    assert response.status_code == 200, response.text

    claimed = queue_store.claim_next(store)
    assert claimed is not None and claimed.id == approved.id, (
        "the approved reel went to the back of the queue behind a later arrival"
    )


def test_the_approve_endpoint_actually_asks_for_the_front(store):
    """The behaviour above is one keyword argument away from silently
    regressing, and the queue ordering test cannot see the endpoint."""
    import inspect

    from app.api import routes_jobs

    source = inspect.getsource(routes_jobs.approve)
    assert "front=True" in source, "approve no longer requeues at the front"


def test_a_job_can_only_hold_one_place_in_the_queue(store):
    from app import queue_store

    job = make(store, "a")
    first = queue_store.enqueue(store, job.id)
    second = queue_store.enqueue(store, job.id)
    assert first["position"] == second["position"] == 1
    _running, waiting = queue_store.ordered(store)
    assert len(waiting) == 1


def test_a_finished_job_cannot_be_queued(store):
    from app import queue_store
    from app.models.job import STAGE_ORDER, Status

    job = make(store, "a")
    for stage in STAGE_ORDER:
        job.mark(stage, Status.DONE)
    store.save(job)

    with pytest.raises(ValueError, match="already finished"):
        queue_store.enqueue(store, job.id)


def test_a_corrupt_queue_file_means_not_paused(store, tmp_path):
    """Same posture as the config overlay's unreadable-YAML handling: the queue
    running is the state that needs no explanation."""
    from app import queue_store

    queue_store.path().parent.mkdir(parents=True, exist_ok=True)
    queue_store.path().write_text("{not json at all")
    assert queue_store.read().paused is False


def test_the_snapshot_keeps_the_keys_the_existing_ui_reads(store):
    """`app/ui/dist/index.html` reads exactly counts.running and counts.waiting.
    Renaming either blanks its header pill with no error at all."""
    from app import queue_store

    snapshot = queue_store.snapshot(store)
    assert "running" in snapshot["counts"] and "waiting" in snapshot["counts"]
    assert snapshot["waiting"] == snapshot["queued"], "waiting is no longer an alias"
    for key in ("entries", "blocked", "idle", "gated", "paused"):
        assert key in snapshot


def test_the_estimate_parses_the_z_suffixed_timestamps(store):
    """Stage timestamps end in `Z`, which Python 3.10's fromisoformat cannot
    read -- it returns nothing and the estimate silently comes out empty."""
    from app.eta import parse_time

    assert parse_time("2026-08-20T18:40:02.264734Z") is not None
    assert parse_time("2026-08-20T18:40:02.264734+00:00") is not None
    assert parse_time("nonsense") is None
