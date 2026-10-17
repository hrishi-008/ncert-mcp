"""Small shared wrapper around the Claude API for the offline ingestion steps (never used at runtime)."""

from __future__ import annotations

import os
import sys

MODEL = os.environ.get("INGEST_MODEL", "claude-opus-5-5")


def client():
    """Anthropic client, or a clear stop if no credentials are set up."""
    try:
        import anthropic
    except ImportError:
        raise SystemExit("The 'anthropic' package is missing. Run: pip install -e '.[ingest]'")
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        print(
            "No Claude API key found. Put ANTHROPIC_API_KEY=... in .env (or export it), then run this again.\n"
            "(This step is offline ingestion only; the server never calls the API.)",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return anthropic.Anthropic(max_retries=4)


def create(c, **params):
    """messages.create with server-side refusal fallback; falls back to a plain call if the beta is refused."""
    import anthropic

    try:
        resp = c.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **params)
    except anthropic.BadRequestError as e:
        if "fallback" not in str(e).lower():
            raise
        resp = c.messages.create(**params)
    if resp.stop_reason == "refusal":
        raise RuntimeError(f"The model declined this request ({getattr(resp, 'stop_details', None)}).")
    if resp.stop_reason == "max_tokens":
        raise RuntimeError("The response was cut off (max_tokens). Raise max_tokens and run again.")
    return resp


def text_of(resp) -> str:
    return "".join(b.text for b in resp.content if b.type == "text")
