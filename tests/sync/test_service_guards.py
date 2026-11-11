from __future__ import annotations

from datetime import timedelta

import pytest
from filelock import FileLock

from bourso_mcp.sync.service import SyncAlreadyRunning, SyncService, SyncTooSoon
from tests.factories import make_account
from tests.fakes import FakeBankSource


def build(repo, settings, now, source=None, created=None):
    def factory():
        if created is not None:
            created.append(1)
        return source or FakeBankSource([make_account("cc1")])

    return SyncService(factory, repo, settings, now=lambda: now, sleep=lambda s: None)


def go(svc, **kw):
    return svc.run("all", "cli", lambda m: None, **kw)


def test_second_concurrent_sync_raises_already_running(repo, settings, now):
    created = []
    running = repo.start_run("cli", "all", now)
    with FileLock(settings.lock_path), pytest.raises(SyncAlreadyRunning) as info:
        go(build(repo, settings, now, created=created))
    assert info.value.run.id == running
    assert created == []
    assert repo.current_run().status == "running"  # rien n'a été marqué interrompu


def test_check_can_start_raises_when_lock_held_and_keeps_nothing(repo, settings, now):
    svc = build(repo, settings, now)
    with FileLock(settings.lock_path), pytest.raises(SyncAlreadyRunning):
        svc.check_can_start(False)
    svc.check_can_start(False)  # libre : le sondage ne garde pas le verrou
    svc.check_can_start(False)


def test_dry_run_respects_lock(repo, settings, now):
    created = []
    with FileLock(settings.lock_path), pytest.raises(SyncAlreadyRunning):
        build(repo, settings, now, created=created).dry_run(lambda m: None)
    assert created == []


def test_lock_released_after_run_and_after_error(repo, settings, now):
    svc = build(repo, settings, now)
    go(svc)
    with FileLock(settings.lock_path, timeout=0):
        pass

    class Boom(FakeBankSource):
        def list_accounts(self):
            raise RuntimeError("bogue")

    with pytest.raises(RuntimeError):
        go(build(repo, settings, now, source=Boom([])), force=True)
    with FileLock(settings.lock_path, timeout=0):
        pass
    assert repo.last_finished_run().status == "error"


def test_rate_limit_and_force(repo, settings, now):
    run_id = repo.start_run("cli", "all", now - timedelta(minutes=31))
    repo.finish_run(run_id, "ok", now - timedelta(minutes=30), 0, 0, None, None)
    created = []
    svc = build(repo, settings, now, created=created)
    with pytest.raises(SyncTooSoon) as info:
        go(svc)
    assert info.value.last_success_at == now - timedelta(minutes=30)
    assert created == []
    with pytest.raises(SyncTooSoon):
        svc.check_can_start(False)
    svc.check_can_start(True)
    assert go(svc, force=True).status == "ok"
    assert created == [1]


def test_partial_counts_as_success_and_old_success_passes(repo, settings, now):
    run_id = repo.start_run("cli", "all", now - timedelta(minutes=10))
    repo.finish_run(run_id, "partial", now - timedelta(minutes=5), 0, 0, None, None)
    with pytest.raises(SyncTooSoon):
        build(repo, settings, now).check_can_start(False)
    other = build(repo, settings, now + timedelta(hours=2))
    other.check_can_start(False)


def test_failed_run_does_not_trigger_rate_limit(repo, settings, now):
    run_id = repo.start_run("cli", "all", now - timedelta(minutes=10))
    repo.finish_run(run_id, "auth_failed", now - timedelta(minutes=5), 0, 0, "AuthFailed", "x")
    build(repo, settings, now).check_can_start(False)


def test_dry_run_skips_rate_limit(repo, settings, now):
    run_id = repo.start_run("cli", "all", now - timedelta(minutes=10))
    repo.finish_run(run_id, "ok", now - timedelta(minutes=5), 0, 0, None, None)
    assert len(build(repo, settings, now).dry_run(lambda m: None)) == 1


def test_interrupted_run_marked_error_on_next_start(repo, settings, now):
    stale = repo.start_run("cli", "all", now - timedelta(hours=3))
    repo.set_run_status(stale, "awaiting_2fa", "sms")
    res = go(build(repo, settings, now))
    assert res.status == "ok" and res.run_id != stale
    with repo._conn() as c:
        row = c.execute(
            "SELECT status, error_message FROM sync_runs WHERE id=?", (stale,)
        ).fetchone()
    assert row["status"] == "error" and row["error_message"] == "interrompu"
