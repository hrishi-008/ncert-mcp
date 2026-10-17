"""Calls to Claude for the offline ingestion steps (never used at runtime).

Two backends, same interface (`complete`):

  claude-cli  Runs `claude -p` (Claude Code), so it uses the Claude account Claude Code is logged into,
              e.g. a Pro plan. No API key needed. Default when ANTHROPIC_API_KEY is not set.
  api         The Anthropic API with ANTHROPIC_API_KEY.

Usage limits (claude-cli): Claude Code reports how much of the 5-hour and 7-day windows is used and
when each resets. When a limit is hit, or usage passes INGEST_MAX_UTILIZATION, this module sleeps
until the reset time and then carries on by itself. No manual step is needed.

Settings (env):
  INGEST_BACKEND          claude-cli | api
  INGEST_MODEL            model id or alias (default: sonnet for claude-cli, claude-opus-5-5 for api)
  INGEST_MAX_UTILIZATION  pause when a window is this full, 0-1 (default 0.97)
  INGEST_ON_LIMIT         sleep (default): wait in-process until the reset, then continue.
                          exit: save the reset time to data/ingest_resume.json and exit with code 75,
                          for machines that can't stay awake that long (a cloud session that is
                          restarted when idle); whoever runs the job restarts it at that time and it
                          continues from the saved pages.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime

BACKEND = os.environ.get("INGEST_BACKEND") or ("api" if os.environ.get("ANTHROPIC_API_KEY") else "claude-cli")
MODEL = os.environ.get("INGEST_MODEL") or ("sonnet" if BACKEND == "claude-cli" else "claude-opus-5-5")
MAX_UTILIZATION = float(os.environ.get("INGEST_MAX_UTILIZATION", "0.97"))
CLI_TIMEOUT = 900  # seconds for one call
LIMIT_MARGIN = 120  # sleep this long past the reset time, to be safe
ON_LIMIT = os.environ.get("INGEST_ON_LIMIT", "sleep")
RESUME_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "ingest_resume.json")
EXIT_LIMIT = 75  # exit code meaning "stopped for a usage limit; restart after resume_at"

_gate = threading.Lock()  # one sleeper at a time; other workers wait on it
_resume_at = 0.0  # epoch seconds: no calls before this
last_usage: dict = {}  # latest window usage reported by Claude Code, for progress logs


def log(msg: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", file=sys.stderr, flush=True)


class LimitHit(Exception):
    def __init__(self, resets_at: float | None, why: str):
        super().__init__(why)
        self.resets_at = resets_at


def image_block(png: bytes) -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": base64.b64encode(png).decode()}}


def text_block(text: str) -> dict:
    return {"type": "text", "text": text}


def complete(system: str, content: list[dict], *, max_tokens: int = 16000, effort: str = "medium",
             schema: dict | None = None):
    """One request. Returns the text, or the parsed object when `schema` is given."""
    if BACKEND == "api":
        return _api(system, content, max_tokens, effort, schema)
    while True:
        _wait_for_window()
        try:
            return _cli_with_retries(system, content, schema)
        except LimitHit as e:
            _sleep_until(e.resets_at, str(e))


# --- claude-cli backend ----------------------------------------------------------------------------

def _wait_for_window() -> None:
    while True:
        wait = _resume_at - time.time()
        if wait <= 0:
            return
        if ON_LIMIT == "exit" and wait > 90:
            with open(RESUME_FILE, "w") as f:
                json.dump({"resume_at": _resume_at, "resume_at_utc": datetime.utcfromtimestamp(_resume_at).isoformat(),
                           "usage": last_usage}, f)
            log(f"stopping for the usage limit; restart after {datetime.fromtimestamp(_resume_at):%Y-%m-%d %H:%M}")
            sys.stderr.flush()
            os._exit(EXIT_LIMIT)  # finished pages are already saved; the restart picks up from there
        time.sleep(min(wait, 60))


def _sleep_until(resets_at: float | None, why: str) -> None:
    global _resume_at
    with _gate:
        if resets_at is None:
            resets_at = time.time() + 30 * 60  # reset time unknown: look again in 30 minutes
        target = max(resets_at + LIMIT_MARGIN, time.time() + 60)
        if target > _resume_at:
            _resume_at = target
            log(f"usage limit: {why}. Sleeping until {datetime.fromtimestamp(target):%Y-%m-%d %H:%M} "
                f"({(target - time.time()) / 3600:.1f} h), then continuing by itself.")
    _wait_for_window()


def _cli_with_retries(system: str, content: list[dict], schema: dict | None):
    last = None
    for attempt in range(4):
        try:
            return _cli_call(system, content, schema)
        except LimitHit:
            raise
        except Exception as e:  # network blip, crash, bad JSON: retry with backoff
            last = e
            log(f"claude call failed ({type(e).__name__}: {str(e)[:200]}); retry {attempt + 1}/3")
            time.sleep(20 * (attempt + 1))
    raise RuntimeError(f"claude call kept failing: {last}")


def _cli_call(system: str, content: list[dict], schema: dict | None):
    cmd = [
        "claude", "-p", "--model", MODEL,
        "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
        "--system-prompt", system,
        # Keep each call small: no tools, no MCP servers, no skills, no project files, no saved session.
        "--tools", "", "--strict-mcp-config", "--disable-slash-commands",
        "--no-session-persistence", "--setting-sources", "",
    ]
    if schema:
        cmd += ["--json-schema", json.dumps(schema)]
    msg = json.dumps({"type": "user", "message": {"role": "user", "content": content}}) + "\n"
    with tempfile.TemporaryDirectory() as empty_dir:  # an empty folder, so no CLAUDE.md gets loaded
        proc = subprocess.run(cmd, input=msg, capture_output=True, text=True, timeout=CLI_TIMEOUT, cwd=empty_dir)
    result, limit = None, None
    for line in proc.stdout.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "rate_limit_event":
            limit = ev.get("rate_limit_info") or {}
        elif ev.get("type") == "result":
            result = ev
    text_all = (proc.stdout[-2000:] + proc.stderr[-2000:]).lower()

    if limit:
        last_usage.update({k: w.get("utilization") for k, w in (limit.get("unifiedWindows") or {}).items()})
        _check_limit(limit)
    if result is None or result.get("is_error"):
        why = (result or {}).get("result") or proc.stderr.strip()[-500:] or f"exit code {proc.returncode}"
        if re.search(r"usage limit|rate limit|limit reached|out of (extra )?usage|too many requests|429", text_all):
            raise LimitHit(_reset_from(limit, text_all), f"limit reached ({why[:120]})")
        raise RuntimeError(why[:500])
    if result.get("stop_reason") == "refusal":
        raise RuntimeError("the model declined this request")
    if schema:
        out = result.get("structured_output")
        return out if out is not None else json.loads(result["result"])
    return result.get("result", "")


def _check_limit(info: dict) -> None:
    """Raise LimitHit when a window is blocked; pause early when one is nearly full (after this call)."""
    global _resume_at
    status = str(info.get("status", "")).lower()
    if status and status not in ("allowed", "allowed_warning"):
        raise LimitHit(_reset_from(info, ""), f"{info.get('rateLimitType', 'usage')} window is {status}")
    for name, w in (info.get("unifiedWindows") or {}).items():
        util = w.get("utilization")
        if util is not None and util >= MAX_UTILIZATION and w.get("resetsAt"):
            with _gate:
                target = float(w["resetsAt"]) + LIMIT_MARGIN
                if target > _resume_at:
                    _resume_at = target
                    log(f"{name} window is {util:.0%} used; pausing until it resets at "
                        f"{datetime.fromtimestamp(target):%Y-%m-%d %H:%M}.")


def _reset_from(info: dict | None, text: str) -> float | None:
    if info:
        if info.get("resetsAt"):
            return float(info["resetsAt"])
        windows = [w.get("resetsAt") for w in (info.get("unifiedWindows") or {}).values() if w.get("resetsAt")]
        if windows:
            return float(max(windows)) if str(info.get("status")) != "allowed" else float(min(windows))
    m = re.search(r"\|(\d{10})\b", text)  # older CLI message: "Claude AI usage limit reached|1792262400"
    if m:
        return float(m.group(1))
    return None


# --- api backend -----------------------------------------------------------------------------------

def _api(system: str, content: list[dict], max_tokens: int, effort: str, schema: dict | None):
    try:
        import anthropic
    except ImportError:
        raise SystemExit("The 'anthropic' package is missing. Run: pip install -e '.[ingest]'")
    c = anthropic.Anthropic(max_retries=4)
    params = dict(model=MODEL, max_tokens=max_tokens,
                  system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                  messages=[{"role": "user", "content": content}],
                  output_config={"effort": effort, **({"format": {"type": "json_schema", "schema": schema}} if schema else {})})
    try:
        resp = c.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **params)
    except anthropic.BadRequestError as e:
        if "fallback" not in str(e).lower():
            raise
        resp = c.messages.create(**params)
    if resp.stop_reason == "refusal":
        raise RuntimeError("The model declined this request.")
    if resp.stop_reason == "max_tokens":
        raise RuntimeError("The response was cut off (max_tokens). Raise max_tokens and run again.")
    text = "".join(b.text for b in resp.content if b.type == "text")
    return json.loads(text) if schema else text


def describe() -> str:
    return f"backend={BACKEND} model={MODEL}"
