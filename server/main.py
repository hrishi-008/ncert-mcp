"""The MCP server: Streamable HTTP at /<SECRET_TOKEN>/mcp, health at /<SECRET_TOKEN>/health.

Every other path returns a plain 404, so the server doesn't reveal it exists. Request logs never
contain the path (it holds the secret) or any content: only the tool name, duration and status.

Run locally:
    SECRET_TOKEN=dev-token-1234567890 python -m server.main
    -> http://localhost:8000/dev-token-1234567890/mcp
"""

from __future__ import annotations

import logging
import os
import secrets
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import uvicorn
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from server import tools
from server.books import Books
from server.state import State

ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger("ncert")

INSTRUCTIONS = """\
This server holds the NEW NCERT textbooks for CBSE Class 6 and 7 (Science: Curiosity; Maths: Ganita
Prakash; SST: Exploring Society: India and Beyond), the teacher's saved notes, and a log of her papers.

How to work:
1. Always resolve chapters with find_chapter first. Chapter names and numbers changed in the new books:
   never guess from memory. If the match came from an old chapter name, tell her the new name.
2. Call get_chapter_pack once per chapter. It has the content outline, the book's exercises and her notes.
   Every question must come from what that chapter actually teaches. Use get_section / search_books for
   exact facts and wording. If something isn't in the book, say so; don't invent.
3. When she corrects you or states a preference, save it with update_teacher_notes.
4. After she is happy with the final paper, save it with log_paper.
5. Only call get_past_questions when she asks for no repeats.
"""


@dataclass
class Settings:
    secret_token: str
    books_db: Path
    state_db_url: str
    rate_limit_per_min: int = 60
    host: str = "0.0.0.0"
    port: int = 8000
    public_host: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        token = os.environ.get("SECRET_TOKEN", "").strip()
        if len(token) < 16:
            raise SystemExit(
                "SECRET_TOKEN must be set to a long random string (16+ chars). "
                f"For example: {secrets.token_urlsafe(24)}"
            )
        return cls(
            secret_token=token,
            books_db=Path(os.environ.get("BOOKS_DB_PATH", ROOT / "data" / "books.db")),
            state_db_url=os.environ.get("STATE_DB_URL", f"sqlite:///{ROOT / 'data' / 'state.db'}"),
            rate_limit_per_min=int(os.environ.get("RATE_LIMIT_PER_MIN", "60")),
            host=os.environ.get("HOST", "0.0.0.0"),
            port=int(os.environ.get("PORT", "8000")),
            public_host=os.environ.get("PUBLIC_HOST", "").strip(),
        )


class Guard:
    """Outer ASGI layer: 404 for any path outside the secret prefix, and a simple rate limit."""

    def __init__(self, app: ASGIApp, prefix: str, per_minute: int):
        self.app = app
        self.prefix = prefix
        self.per_minute = per_minute
        self.hits: deque[float] = deque()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)  # lifespan
            return
        path = scope.get("path", "")
        if not (path == self.prefix or path.startswith(self.prefix + "/")):
            await PlainTextResponse("Not Found", status_code=404)(scope, receive, send)
            return
        if scope.get("method") == "GET" and path == self.prefix + "/mcp":
            # Stateless server: no server-initiated stream. Per the MCP spec, answer 405 instead of
            # holding an empty SSE stream open until the client times out.
            await PlainTextResponse("Method Not Allowed", status_code=405, headers={"Allow": "POST"})(
                scope, receive, send
            )
            return
        now = time.monotonic()
        while self.hits and now - self.hits[0] > 60:
            self.hits.popleft()
        if len(self.hits) >= self.per_minute:
            log.warning("rate limit hit")
            await PlainTextResponse("Too many requests, slow down.", status_code=429, headers={"Retry-After": "10"})(
                scope, receive, send
            )
            return
        self.hits.append(now)
        start = time.perf_counter()
        status = {"code": 0}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        await self.app(scope, receive, send_wrapper)
        kind = "health" if path.endswith("/health") else "mcp"
        log.info("http %s %s status=%s ms=%.0f", scope.get("method"), kind, status["code"], (time.perf_counter() - start) * 1000)


def create_app(settings: Settings) -> ASGIApp:
    books = Books(settings.books_db)
    state = State(settings.state_db_url)
    mcp = MCPServer(name="ncert-books", title="NCERT Class 6-7 Books", instructions=INSTRUCTIONS, version="1.0.0")
    tools.register(mcp, books, state)
    prefix = f"/{settings.secret_token}"

    @mcp.custom_route(f"{prefix}/health", methods=["GET"])
    async def health(request: Request) -> JSONResponse:
        body: dict = {"status": "ok", "state_db": "ok" if state.ok() else "error"}
        if books.available():
            try:
                body["books"] = books.stats()
            except Exception as e:  # corrupt or half-copied books.db
                body["status"], body["books"] = "degraded", f"error: {type(e).__name__}"
        else:
            body["status"], body["books"] = "degraded", "books.db missing"
        return JSONResponse(body)

    # Behind a reverse proxy on 127.0.0.1, the SDK's localhost-only Host check would reject the
    # public domain, so allow it explicitly (DNS rebinding protection stays on).
    security = None
    if settings.public_host:
        security = TransportSecuritySettings(
            allowed_hosts=[settings.public_host, "127.0.0.1:*", "localhost:*"],
            allowed_origins=[f"https://{settings.public_host}", "https://claude.ai"],
        )
    app = mcp.streamable_http_app(
        streamable_http_path=f"{prefix}/mcp",
        stateless_http=True,  # no session state: restarts and redeploys never break her chat
        json_response=True,
        transport_security=security,
        host=settings.host,  # not localhost, so the SDK's localhost-only Host check stays off
    )
    return Guard(app, prefix, settings.rate_limit_per_min)


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = Settings.from_env()
    app = create_app(settings)
    log.info("starting on %s:%s (books: %s)", settings.host, settings.port, settings.books_db)
    uvicorn.run(app, host=settings.host, port=settings.port, access_log=False, proxy_headers=True, forwarded_allow_ips="*")


if __name__ == "__main__":
    main()
