from __future__ import annotations

import time

from filelock import FileLock

from bourso_mcp.sync.runner import BackgroundSync, StartResult
from bourso_mcp.sync.service import SyncService
from tests.factories import make_account
from tests.fakes import FakeBankSource


def wait_until(pred, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.005)
    return pred()


def build(source, repo, settings, now, **kwargs):
    service = SyncService(lambda: source, repo, settings, now=lambda: now, sleep=lambda s: None)
    return BackgroundSync(service, repo, **kwargs)


def one_account(**kwargs):
    return FakeBankSource([make_account("cc1")], **kwargs)


def test_start_returns_immediately_and_completes(repo, settings, now):
    bg = build(one_account(), repo, settings, now)
    res = bg.start("all")
    assert res == StartResult(True, "started", None)
    out = bg.wait(2)
    assert out is not None and out.status == "ok"
    assert repo.last_finished_run().trigger == "mcp"
    assert bg.status()["last"]["status"] == "ok"
    assert bg.status()["current"] is None


def test_sms_code_submitted_via_submit_code(repo, settings, now):
    bg = build(one_account(two_factor="sms"), repo, settings, now)
    assert bg.start("all").started
    assert wait_until(lambda: (bg.status()["current"] or {}).get("status") == "awaiting_2fa")
    assert bg.status()["current"]["twofa_method"] == "sms"
    assert wait_until(lambda: bg.submit_code("123 456"))
    out = bg.wait(2)
    assert out is not None and out.status == "ok"


def test_submit_code_without_waiting_run_returns_false(repo, settings, now):
    bg = build(one_account(), repo, settings, now)
    assert bg.submit_code("123456") is False
    assert bg.submit_code("   ") is False
    assert bg.start("all").started
    bg.wait(2)
    assert bg.submit_code("123456") is False


def test_code_timeout_gives_auth_required(repo, settings, now):
    bg = build(one_account(two_factor="sms"), repo, settings, now, code_timeout=0.1)
    assert bg.start("all").started
    out = bg.wait(2)
    assert out is not None and out.status == "auth_required"
    assert out.error_type == "TwoFactorExpired"
    assert bg.submit_code("123456") is False


def test_start_while_running_reports_already_running(repo, settings, now):
    bg = build(one_account(two_factor="sms"), repo, settings, now)
    assert bg.start("all").started
    assert wait_until(lambda: (bg.status()["current"] or {}).get("status") == "awaiting_2fa")
    second = bg.start("all", force=True)
    assert second.started is False and second.reason == "already_running"
    assert wait_until(lambda: bg.submit_code("123456"))
    assert bg.wait(2).status == "ok"


def test_start_too_soon_reports_too_soon(repo, settings, now):
    run_id = repo.start_run("cli", "all", now)
    repo.finish_run(run_id, "ok", now, 0, 0, None, None)
    bg = build(one_account(), repo, settings, now)
    res = bg.start("all")
    assert res.started is False and res.reason == "too_soon"
    assert res.last_success_at == now
    assert bg.start("all", force=True).started
    assert bg.wait(2).status == "ok"


def test_chained_two_factor_sms_then_email(repo, settings, now):
    src = one_account(two_factor="sms", then_methods=["email"])
    bg = build(src, repo, settings, now)
    assert bg.start("all").started
    assert wait_until(lambda: (bg.status()["current"] or {}).get("twofa_method") == "sms")
    assert wait_until(lambda: bg.submit_code("123456"))
    assert wait_until(lambda: (bg.status()["current"] or {}).get("twofa_method") == "email")
    assert wait_until(lambda: bg.submit_code("123456"))
    out = bg.wait(2)
    assert out is not None and out.status == "ok"
    assert [c for c in src.calls if c[0] == "submit_2fa"] == [("submit_2fa", "123456")] * 2


def test_start_when_lock_held_elsewhere_reports_already_running(repo, settings, now):
    with FileLock(settings.lock_path, timeout=0):
        res = build(one_account(), repo, settings, now).start("all")
    assert res == StartResult(False, "already_running", None)
