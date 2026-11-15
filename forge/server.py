"""HTTP plumbing: a threaded stdlib server, routing, auth, JSON replies.

One process, one Store. The handler is deliberately thin — the read
workload is 2,000 sequential GETs on one keep-alive connection, so every
response is built as bytes first (Content-Length set, HTTP/1.1
keep-alive) and dispatch is a prefix match, not a regex walk.

Latency note: the workloads are dominated by round trips, not CPU (a GET
issue is ~0.01 ms of Python). Two things keep a round trip at ~1 ms
instead of ~20 ms:
  * TCP_NODELAY on every accepted socket — Python's http.server leaves
    Nagle on, and a small response split across two writes (headers, then
    body) stalls behind the client's delayed ACK for ~20 ms. Node's
    servers disable Nagle by default; this one must do it explicitly.
  * wbufsize = -1, so the status line, headers and body buffer into one
    write (one TCP packet) and are flushed once.
"""

from __future__ import annotations

import json
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit, parse_qs

from .store import Store


class Request:
    __slots__ = ("method", "path", "segments", "query", "headers", "body")

    def __init__(self, method, path, segments, query, headers, body):
        self.method = method
        self.path = path
        self.segments = segments
        self.query = query
        self.headers = headers
        self.body = body

    def header(self, name: str, default=None):
        return self.headers.get(name.lower(), default)

    def json(self):
        """Parse the body as JSON. Raises ValueError on malformed input."""
        if not self.body:
            return None
        return json.loads(self.body.decode("utf-8"))

    def bearer(self):
        return self.header("authorization")


class Response:
    __slots__ = ("status", "body", "headers", "content_type")

    def __init__(self, status: int, body: bytes = b"", headers=None,
                 content_type: str = "application/json"):
        self.status = status
        self.body = body
        self.headers = headers or {}
        self.content_type = content_type


def json_response(status: int, payload, headers=None) -> Response:
    body = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
    return Response(status, body, headers)


def json_error(status: int, message: str, headers=None) -> Response:
    return json_response(status, {"message": message}, headers)


def _read_body(handler: BaseHTTPRequestHandler) -> bytes:
    length = handler.headers.get("Content-Length")
    if length is not None:
        try:
            n = int(length)
        except ValueError:
            return b""
        if n <= 0:
            return b""
        return handler.rfile.read(n)
    if handler.headers.get("Transfer-Encoding", "").lower() == "chunked":
        chunks = bytearray()
        while True:
            line = handler.rfile.readline(65536).strip()
            try:
                size = int(line.split(b";")[0], 16)
            except ValueError:
                break
            if size == 0:
                handler.rfile.readline(65536)
                break
            chunks += handler.rfile.read(size)
            handler.rfile.readline(65536)
        return bytes(chunks)
    return b""


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    # The clone workload opens 50 connections at once; the default listen
    # backlog of 5 drops SYNs and the client retries with second-long
    # backoff, which alone costs the whole workload.
    request_queue_size = 256

    def get_request(self):
        sock, addr = super().get_request()
        # Disable Nagle on the accepted socket (see the module docstring).
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        return sock, addr


def make_handler(store: Store):
    from . import gitproto, graphql, mcp, rest

    class ForgeHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "forge/1.0"
        # Buffer the whole response (status line + headers + body) and
        # flush once, so it leaves as a single packet.
        wbufsize = -1

        def log_message(self, *args):  # keep the trial log clean
            pass

        def _dispatch(self, method: str):
            try:
                raw = self.path or "/"
                split = urlsplit(raw)
                path = unquote(split.path)
                segments = [s for s in path.split("/") if s != ""]
                query = {k: v[0] for k, v in parse_qs(split.query).items()}
                headers = {k.lower(): v for k, v in self.headers.items()}
                body = _read_body(self)
                req = Request(method, path, segments, query, headers, body)
                resp = route(store, req)
            except Exception as err:  # never let one request kill the server
                resp = json_error(500, f"internal error: {err}")
            self._send(resp)

        def _send(self, resp: Response):
            body = resp.body or b""
            self.send_response(resp.status)
            self.send_header("Content-Type", resp.content_type)
            self.send_header("Content-Length", str(len(body)))
            for key, value in resp.headers.items():
                self.send_header(key, value)
            self.end_headers()
            if body and self.command != "HEAD":
                self.wfile.write(body)
            self.wfile.flush()

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_PUT(self):
            self._dispatch("PUT")

        def do_PATCH(self):
            self._dispatch("PATCH")

        def do_DELETE(self):
            self._dispatch("DELETE")

        def do_HEAD(self):
            self._dispatch("HEAD")

    return ForgeHandler


def route(store: Store, req: Request) -> Response:
    from . import gitproto, graphql, mcp, rest

    path = req.path
    method = req.method

    # Boot check: 200 {"ok": true}, no auth.
    if method == "GET" and path == "/healthz":
        return json_response(200, {"ok": True})

    # Git smart HTTP: /{owner}/{repo}.git/... — no bearer token here; the
    # suite's git client sends none.
    if len(req.segments) >= 2 and req.segments[1].endswith(".git"):
        return gitproto.dispatch(store, req)

    # MCP endpoint (auth inside).
    if path == "/mcp":
        return mcp.dispatch(store, req)

    # GraphQL endpoint (auth inside).
    if path == "/api/graphql":
        return graphql.dispatch(store, req)

    # REST v3 (auth inside).
    if path == "/api/v3" or path.startswith("/api/v3/"):
        return rest.dispatch(store, req)

    return json_error(404, "Not Found", {"X-GitHub-Request-Id": store.request_id()})


def serve(store: Store, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    server = _Server((host, port), make_handler(store))
    try:
        server.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass
    return server
