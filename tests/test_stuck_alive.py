"""False 'Coworker task is not confirmed' alarms: a coworker that still works is not stuck."""
import os
import time

from notiontool import main_wake as mw


def _task(**extra):
    task = dict(id="t1", run_id="r1", state="uncertain", reconcile_checks=mw.STUCK_CHECKS)
    task.update(extra)
    return task


RUNS = {"r1": {"id": "r1", "operator": True}}


def test_without_alive_check_old_behaviour_stays():
    assert [t["id"] for _, t in mw.stuck_tasks([_task()], RUNS, set())] == ["t1"]


def test_alive_task_is_not_reported():
    assert mw.stuck_tasks([_task()], RUNS, set(), alive=lambda t: True) == []


def test_dead_task_is_still_reported():
    assert len(mw.stuck_tasks([_task()], RUNS, set(), alive=lambda t: False)) == 1


def test_remote_alive_needs_recent_step():
    now = 10_000.0
    assert mw.remote_alive(_task(remote_working=True), now)
    assert mw.remote_alive(_task(remote_running=True, remote_progress_at=now - 60), now)
    assert not mw.remote_alive(_task(remote_running=True, remote_progress_at=now - 3600), now)
    assert not mw.remote_alive(_task(remote_running=True), now)
    assert not mw.remote_alive(_task(), now)


def test_worktree_active_sees_fresh_file(tmp_path):
    sub = tmp_path / "data"
    sub.mkdir()
    f = sub / "notiontool.db-wal"
    f.write_text("x")
    now = time.time()
    assert mw.worktree_active(str(tmp_path), now)
    old = now - 3600
    os.utime(f, (old, old))
    assert not mw.worktree_active(str(tmp_path), now)


def test_worktree_active_ignores_git_and_missing(tmp_path):
    git = tmp_path / ".git"
    git.mkdir()
    (git / "index").write_text("x")
    assert not mw.worktree_active(str(tmp_path), time.time())
    assert not mw.worktree_active(str(tmp_path / "nope"), time.time())
    assert not mw.worktree_active(None, time.time())
