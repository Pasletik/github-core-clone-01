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

Two more round-trip costs are shaved by the handler below:
  * parse_request is overridden with a fast manual header loop. The
    stdlib parses headers with the email package (~30 us per request);
    the fast path reads the raw header block once, splits on ':', and
    stores lowercase keys in a case-insensitive dict. Anything it cannot
    parse (malformed line, continuation without a previous header, a
    line over 64 KiB, over 200 header lines) falls back to the stdlib
    parser with the raw block replayed, so behaviour is identical for
    unusual requests. The fallback restores the real rfile afterwards,
    so a POST body is still readable.
  * send_response skips the Server and Date headers the stdlib adds
    (the suite only requires Content-Type and X-GitHub-Request-Id;
    date_time_string alone costs ~6 us per response).
"""

from __future__ import annotations

import io
import json
import socket
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit, parse_qs

from .store import Store


class _CIHeaders(dict):
    """Header map with case-insensitive lookup (keys stored lowercase)."""

    def get(self, key, default=None):
        return super().get(key.lower(), default)

    def __contains__(self, key):
        return super().__contains__(key.lower())


def _parse_headers_fast(raw_lines):
    """Parse buffered header lines into a _CIHeaders, or None to fall back.

    Continuation lines (leading SP/HT) append to the previous header's
    value. A duplicate name keeps its first value, matching the email
    parser's .get(). Returns None on any malformed line.
    """
    headers = _CIHeaders()
    prev_key = None
    for line in raw_lines:
        if line in (b"\r\n", b"\n", b""):
            break
        if line[:1] in (b" ", b"\t"):
            if prev_key is None:
                return None
            headers[prev_key] += " " + line.strip().decode("iso-8859-1")
            continue
        if b":" not in line:
            return None
        name, value = line.split(b":", 1)
        key = name.decode("iso-8859-1").strip().lower()
        if not key or " " in key or "\t" in key:
            return None
        if key not in headers:
            headers[key] = value.decode("iso-8859-1").strip()
            prev_key = key
    return headers


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

        def parse_request(self):
            # Fast path (see the module docstring). Request-line validation
            # is the stdlib's, verbatim; only header parsing is replaced,
            # and any header block it cannot parse falls back to the
            # stdlib parser with the raw block replayed.
            self.command = None
            self.request_version = version = self.default_request_version
            self.close_connection = True
            requestline = str(self.raw_requestline, 'iso-8859-1')
            requestline = requestline.rstrip('\r\n')
            self.requestline = requestline
            words = requestline.split()
            if len(words) == 0:
                return False
            if len(words) >= 3:
                version = words[-1]
                try:
                    if not version.startswith('HTTP/'):
                        raise ValueError
                    base_version_number = version.split('/', 1)[1]
                    version_number = base_version_number.split(".")
                    if len(version_number) != 2:
                        raise ValueError
                    if any(not c.isdigit() for c in version_number):
                        raise ValueError("non digit in http version")
                    if any(len(c) > 10 for c in version_number):
                        raise ValueError("unreasonable length http version")
                    version_number = int(version_number[0]), int(version_number[1])
                except (ValueError, IndexError):
                    self.send_error(HTTPStatus.BAD_REQUEST,
                                    "Bad request version (%r)" % version)
                    return False
                if version_number >= (1, 1) and self.protocol_version >= "HTTP/1.1":
                    self.close_connection = False
                if version_number >= (2, 0):
                    self.send_error(HTTPStatus.HTTP_VERSION_NOT_SUPPORTED,
                                    "Invalid HTTP version (%s)" % base_version_number)
                    return False
                self.request_version = version
            if not 2 <= len(words) <= 3:
                self.send_error(HTTPStatus.BAD_REQUEST,
                                "Bad request syntax (%r)" % requestline)
                return False
            command, path = words[:2]
            if len(words) == 2:
                self.close_connection = True
                if command != 'GET':
                    self.send_error(HTTPStatus.BAD_REQUEST,
                                    "Bad HTTP/0.9 request type (%r)" % command)
                    return False
            self.command, self.path = command, path
            if self.path.startswith('//'):
                self.path = '/' + self.path.lstrip('/')
            # Read the whole header block first, so a fallback can replay it.
            real_rfile = self.rfile
            raw_lines = []
            fallback = False
            while True:
                line = real_rfile.readline(65536)
                if len(line) == 65536 and not line.endswith(b"\n"):
                    fallback = True  # line too long; the stdlib raises 431
                    break
                raw_lines.append(line)
                if line in (b"\r\n", b"\n", b""):
                    break
                if len(raw_lines) > 200:
                    fallback = True  # too many headers; the stdlib raises
                    break
            if not fallback:
                headers = _parse_headers_fast(raw_lines)
                if headers is None:
                    fallback = True
            if fallback:
                self.rfile = io.BufferedReader(io.BytesIO(b"".join(raw_lines)))
                try:
                    ok = super().parse_request()
                finally:
                    self.rfile = real_rfile
                return ok
            self.headers = headers
            conntype = headers.get('connection', "")
            if conntype.lower() == 'close':
                self.close_connection = True
            elif (conntype.lower() == 'keep-alive' and
                  self.protocol_version >= "HTTP/1.1"):
                self.close_connection = False
            expect = headers.get('expect', "")
            if (expect.lower() == "100-continue" and
                    self.protocol_version >= "HTTP/1.1" and
                    self.request_version >= "HTTP/1.1"):
                if not self.handle_expect_100():
                    return False
            return True

        def send_response(self, code, message=None):
            # Skip the Server and Date headers the stdlib adds (see the
            # module docstring); log_request is a no-op below.
            self.send_response_only(code, message)

        def log_request(self, code='-', size='-'):
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
