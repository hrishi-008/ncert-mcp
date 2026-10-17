"""The usage-limit logic of the claude-cli backend, with fake rate-limit events (no real calls)."""

import time

import pytest

from ingest import llm


@pytest.fixture(autouse=True)
def reset_resume():
    llm._resume_at = 0.0
    yield
    llm._resume_at = 0.0


def test_limit_hit_raises_with_reset_time():
    info = {"status": "rejected", "resetsAt": 1792262400, "rateLimitType": "five_hour"}
    with pytest.raises(llm.LimitHit) as e:
        llm._check_limit(info)
    assert e.value.resets_at == 1792262400


def test_nearly_full_window_pauses_after_this_call():
    reset = time.time() + 3600
    info = {"status": "allowed", "unifiedWindows": {"five_hour": {"utilization": 0.99, "resetsAt": reset},
                                                     "seven_day": {"utilization": 0.2, "resetsAt": reset + 9e5}}}
    llm._check_limit(info)  # does not raise: this call's answer is still good
    assert llm._resume_at == pytest.approx(reset + llm.LIMIT_MARGIN)


def test_normal_usage_does_not_pause():
    info = {"status": "allowed", "unifiedWindows": {"five_hour": {"utilization": 0.3, "resetsAt": time.time() + 100}}}
    llm._check_limit(info)
    assert llm._resume_at == 0.0


def test_reset_time_from_old_style_message():
    assert llm._reset_from(None, "claude ai usage limit reached|1792262400") == 1792262400
    assert llm._reset_from(None, "something else") is None


def test_unknown_reset_sleeps_30_minutes(monkeypatch):
    monkeypatch.setattr(llm, "_wait_for_window", lambda: None)
    before = time.time()
    llm._sleep_until(None, "test")
    assert 29 * 60 < llm._resume_at - before < 33 * 60 + llm.LIMIT_MARGIN


def test_complete_sleeps_then_retries(monkeypatch):
    calls = {"n": 0}

    def fake_cli(system, content, schema):
        calls["n"] += 1
        if calls["n"] == 1:
            raise llm.LimitHit(time.time() - 1000, "five_hour window is rejected")
        return "page text"

    monkeypatch.setattr(llm, "BACKEND", "claude-cli")
    monkeypatch.setattr(llm, "_cli_with_retries", fake_cli)
    monkeypatch.setattr(llm, "_wait_for_window", lambda: None)
    assert llm.complete("sys", [llm.text_block("hi")]) == "page text"
    assert calls["n"] == 2


def test_exit_mode_saves_resume_time_and_exits(monkeypatch, tmp_path):
    import json as _json
    exits = []
    monkeypatch.setattr(llm, "ON_LIMIT", "exit")
    monkeypatch.setattr(llm, "RESUME_FILE", str(tmp_path / "resume.json"))
    monkeypatch.setattr(llm.os, "_exit", lambda code: exits.append(code) or (_ for _ in ()).throw(SystemExit(code)))
    llm._resume_at = time.time() + 3600
    with pytest.raises(SystemExit):
        llm._wait_for_window()
    assert exits == [llm.EXIT_LIMIT]
    assert _json.loads((tmp_path / "resume.json").read_text())["resume_at"] == pytest.approx(llm._resume_at)
