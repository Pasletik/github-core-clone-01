"""HTTP plumbing: a single-threaded non-blocking event loop, routing, auth,
JSON replies.

One process, one Store, one loop thread. The handler is deliberately thin —
the read workload is 2,000 sequential GETs on one keep-alive connection and
the clone workload is 50 concurrent clones, so every response is built as
bytes first (Content-Length set, HTTP/1.1 keep-alive) and dispatch is a
prefix match, not a regex walk.

Transport note: the workloads are dominated by round trips, not CPU (a GET
issue is ~0.01 ms of Python). The previous transport was a threaded stdlib
server (one thread per connection), and under the clone workload's 50
concurrent connections its throughput collapsed ~9x: 50 threads waking at
once fight over the GIL and the scheduler, so a request that costs 0.13 ms
sequentially cost ~1.17 ms concurrently. A single-threaded selector loop
has no threads to wake and no GIL to fight over — measured locally at
0.083 ms/req sequential and 0.133 ms/req at 50-way concurrency for the same
fixed response — so the loop serves every connection from one thread and
dispatch (route/store) runs on that thread too, which also removes any
cross-thread store races.

Latency notes (kept from the threaded server):
  * TCP_NODELAY on every accepted socket — Python's http.server leaves
    Nagle on, and a small response split across two writes (headers, then
    body) stalls behind the client's delayed ACK for ~20 ms. Node's
    servers disable Nagle by default; this one must do it explicitly.
  * The whole response (status line + headers + body) is assembled into
    one bytes object and handed to send() once, so it leaves as a single
    packet when it fits the socket buffer; larger bodies drain through
    the selector's write-ready events.
  * No Server or Date header is sent (the suite only requires Content-Type
    and X-GitHub-Request-Id; date_time_string alone costs ~6 us per
    response).

Request parsing mirrors BaseHTTPRequestHandler's validation (same status
codes for a bad request line or version), and header parsing is the same
fast manual loop the threaded server used, with a lenient fallback for
lines the fast path rejects — so behaviour is identical for unusual
requests. Bodies arrive as Content-Length (the clients' framing) or
chunked (parsed incrementally); Expect: 100-continue is answered as soon
as the headers are complete.
"""

from __future__ import annotations

import json
import selectors
import socket
import threading
from http import HTTPStatus
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


def _parse_headers_lenient(raw_lines):
    """The stdlib email parser's net behaviour for lines the fast path
    rejects: malformed lines are skipped, duplicates keep the first value,
    continuations append. Never returns None."""
    headers = _CIHeaders()
    prev_key = None
    for line in raw_lines:
        if line in (b"\r\n", b"\n", b""):
            break
        if line[:1] in (b" ", b"\t"):
            if prev_key is not None:
                headers[prev_key] += " " + line.strip().decode("iso-8859-1")
            continue
        if b":" not in line:
            continue
        name, value = line.split(b":", 1)
        key = name.decode("iso-8859-1").strip().lower()
        if not key or " " in key or "\t" in key:
            continue
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


# -- request-line validation (BaseHTTPRequestHandler's checks, verbatim) -----

def _validate_request_line(requestline: str):
    """-> (method, target, version, close) or (None, status, message).

    close is True when the request must close the connection afterwards
    (HTTP/1.0 or an explicit close); version is the request version string.
    """
    words = requestline.split()
    if len(words) == 0:
        return None, 400, "Bad request syntax (%r)" % requestline
    if len(words) >= 3:
        version = words[-1]
        try:
            if not version.startswith("HTTP/"):
                raise ValueError
            base_version_number = version.split("/", 1)[1]
            version_number = base_version_number.split(".")
            if len(version_number) != 2:
                raise ValueError
            if any(not c.isdigit() for c in version_number):
                raise ValueError("non digit in http version")
            if any(len(c) > 10 for c in version_number):
                raise ValueError("unreasonable length http version")
            version_number = int(version_number[0]), int(version_number[1])
        except (ValueError, IndexError):
            return None, 400, "Bad request version (%r)" % version
        if version_number >= (2, 0):
            return None, 505, "Invalid HTTP version (%s)" % base_version_number
        close = version_number < (1, 1)
    else:
        version = "HTTP/0.9"
        close = True
        if len(words) != 2:
            return None, 400, "Bad request syntax (%r)" % requestline
    method, target = words[0], words[1]
    if len(words) == 2 and method != "GET":
        return None, 400, "Bad HTTP/0.9 request type (%r)" % method
    if target.startswith("//"):
        target = "/" + target.lstrip("/")
    return (method, target, version, close), 0, ""


# -- the event loop ------------------------------------------------------------

_MAX_HEADER_LINE = 65536
_MAX_HEADER_LINES = 1000
_MAX_HEADER_BLOCK = 1 << 20
_RECV_CHUNK = 65536
_RECV_PER_EVENT = 16  # cap recv() calls per readable event (starvation guard)


def _reason(status: int) -> str:
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return ""


def _error_bytes(status: int, message: str) -> bytes:
    body = (
        "<!DOCTYPE HTML PUBLIC \"-//W3C//DTD HTML 4.01//EN\" "
        "\"http://www.w3.org/TR/html4/strict.dtd\">\n"
        "<html>\n    <head>\n        <meta http-equiv=\"Content-Type\" "
        "content=\"text/html;charset=utf-8\">\n"
        "        <title>Error response</title>\n    </head>\n"
        "    <body>\n        <h1>Error response</h1>\n"
        f"        <p>Error code: {status}</p>\n"
        f"        <p>Message: {message}.</p>\n"
        "        <p>Error code explanation: "
        f"{_reason(status)}.</p>\n"
        "    </body>\n</html>\n"
    ).encode("utf-8", "replace")
    head = (f"HTTP/1.1 {status} {_reason(status)}\r\n"
            "Content-Type: text/html;charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n\r\n").encode("ascii")
    return head + body


def _response_bytes(resp: Response, method: str) -> bytes:
    body = resp.body or b""
    head = (f"HTTP/1.1 {resp.status} {_reason(resp.status)}\r\n"
            f"Content-Type: {resp.content_type}\r\n"
            f"Content-Length: {len(body)}\r\n")
    for key, value in resp.headers.items():
        head += f"{key}: {value}\r\n"
    out = head.encode("iso-8859-1") + b"\r\n"
    if body and method != "HEAD":
        out += body
    return out


class _Conn:
    """One client connection: buffered input, a response queue, and the
    state of the request currently being read (headers, then body)."""

    __slots__ = (
        "sock", "inbuf", "outbuf", "read_eof", "close_after_write",
        "method", "target", "version", "close_conn", "headers",
        "body", "body_needed", "chunked", "chunk_left", "sent_100",
    )

    def __init__(self, sock):
        self.sock = sock
        self.inbuf = bytearray()
        self.outbuf = bytearray()
        self.read_eof = False
        self.close_after_write = False
        self.method = None
        self.target = None
        self.version = None
        self.close_conn = False
        self.headers = None
        self.body = bytearray()
        self.body_needed = 0
        self.chunked = False
        self.chunk_left = -1  # -1: expecting a chunk-size line
        self.sent_100 = False


class _LoopServer:
    """A selector-driven HTTP/1.1 server with the ThreadingHTTPServer
    interface main.py uses: server_address, serve_forever, shutdown,
    server_close."""

    daemon_threads = True
    request_queue_size = 256

    def __init__(self, host: str, port: int, store: Store):
        self.store = store
        self._sel = selectors.DefaultSelector()
        self._conns = {}  # sock -> _Conn
        self._stop = threading.Event()
        self._wakeup_r, self._wakeup_w = socket.socketpair()
        self._wakeup_r.setblocking(False)
        self._wakeup_w.setblocking(False)
        self._sel.register(self._wakeup_r, selectors.EVENT_READ, None)
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind((host, port))
        self._listener.listen(self.request_queue_size)
        self._listener.setblocking(False)
        self.server_address = self._listener.getsockname()
        self._sel.register(self._listener, selectors.EVENT_READ, None)
        self._loop_thread = None

    # -- lifecycle ------------------------------------------------------------
    def serve_forever(self, poll_interval=0.5):
        self._loop_thread = threading.current_thread()
        while not self._stop.is_set():
            try:
                events = self._sel.select(timeout=0.5)
            except OSError:
                break
            for key, mask in events:
                if key.fileobj is self._wakeup_r:
                    try:
                        self._wakeup_r.recv(4096)
                    except OSError:
                        pass
                    continue
                if key.fileobj is self._listener:
                    self._accept()
                    continue
                conn = key.data
                try:
                    if mask & selectors.EVENT_READ:
                        self._readable(conn)
                    if conn.sock.fileno() != -1 and (mask & selectors.EVENT_WRITE):
                        self._writable(conn)
                except (ConnectionResetError, BrokenPipeError, OSError):
                    self._close(conn)
        # loop stopped: close everything
        for conn in list(self._conns.values()):
            self._close(conn)
        try:
            self._sel.unregister(self._listener)
        except Exception:
            pass
        try:
            self._listener.close()
        except OSError:
            pass

    def shutdown(self):
        self._stop.set()
        try:
            self._wakeup_w.send(b"x")
        except OSError:
            pass
        thread = self._loop_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)

    def server_close(self):
        try:
            self._sel.unregister(self._wakeup_r)
        except Exception:
            pass
        try:
            self._wakeup_r.close()
        except OSError:
            pass
        try:
            self._wakeup_w.close()
        except OSError:
            pass
        try:
            self._sel.close()
        except Exception:
            pass

    # -- connection handling --------------------------------------------------
    def _accept(self):
        while True:
            try:
                sock, _addr = self._listener.accept()
            except BlockingIOError:
                return
            except OSError:
                return
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
            sock.setblocking(False)
            conn = _Conn(sock)
            self._conns[sock] = conn
            self._sel.register(sock, selectors.EVENT_READ, conn)

    def _close(self, conn: _Conn):
        try:
            self._sel.unregister(conn.sock)
        except Exception:
            pass
        self._conns.pop(conn.sock, None)
        try:
            conn.sock.close()
        except OSError:
            pass

    def _refresh(self, conn: _Conn):
        if conn.sock.fileno() == -1:
            return
        want = 0
        if not conn.read_eof:
            want |= selectors.EVENT_READ
        if conn.outbuf:
            want |= selectors.EVENT_WRITE
        if want == 0:
            self._close(conn)
            return
        try:
            self._sel.modify(conn.sock, want, conn)
        except (KeyError, ValueError, OSError):
            self._close(conn)

    def _readable(self, conn: _Conn):
        for _ in range(_RECV_PER_EVENT):
            try:
                data = conn.sock.recv(_RECV_CHUNK)
            except BlockingIOError:
                break
            except OSError:
                self._close(conn)
                return
            if not data:
                conn.read_eof = True
                break
            conn.inbuf += data
            if len(conn.inbuf) > _MAX_HEADER_BLOCK + (1 << 20):
                # unbounded input with no complete request: drop it
                self._close(conn)
                return
        self._pump(conn)
        if conn.sock.fileno() != -1:
            self._flush(conn)
            if conn.sock.fileno() != -1:
                self._refresh(conn)

    def _writable(self, conn: _Conn):
        self._flush(conn)
        if conn.sock.fileno() != -1:
            self._refresh(conn)

    def _flush(self, conn: _Conn):
        while conn.outbuf:
            try:
                sent = conn.sock.send(conn.outbuf)
            except BlockingIOError:
                return
            except OSError:
                self._close(conn)
                return
            if sent == 0:
                return
            del conn.outbuf[:sent]
        if conn.close_after_write:
            self._close(conn)

    # -- request parsing --------------------------------------------------------
    def _fail(self, conn: _Conn, status: int, message: str):
        conn.outbuf += _error_bytes(status, message)
        conn.close_after_write = True
        conn.inbuf.clear()
        self._flush(conn)

    def _pump(self, conn: _Conn):
        """Parse and dispatch every complete request buffered in conn."""
        while True:
            if conn.headers is None:
                if not self._parse_head(conn):
                    return
            if not self._parse_body(conn):
                return
            # a complete request is buffered: dispatch it
            self._dispatch_conn(conn)
            if conn.sock.fileno() == -1:
                return
            # reset for the next pipelined request on this connection
            conn.method = conn.target = conn.version = None
            conn.headers = None
            conn.body = bytearray()
            conn.body_needed = 0
            conn.chunked = False
            conn.chunk_left = -1
            conn.sent_100 = False
            if conn.close_after_write:
                return

    def _parse_head(self, conn: _Conn) -> bool:
        """True when a full header block is parsed (or an error was sent)."""
        buf = conn.inbuf
        end = buf.find(b"\r\n\r\n")
        term_len = 4
        if end < 0:
            end = buf.find(b"\n\n")
            term_len = 2
        if end < 0:
            if len(buf) > _MAX_HEADER_BLOCK:
                self._fail(conn, 431, "Request header fields too large")
            return False
        block = bytes(buf[:end])
        del buf[:end + term_len]
        lines = block.split(b"\n")
        if len(lines) > _MAX_HEADER_LINES:
            self._fail(conn, 431, "Request header fields too large")
            return False
        for line in lines:
            if len(line) > _MAX_HEADER_LINE:
                self._fail(conn, 431, "Request header line too long")
                return False
        requestline = lines[0].rstrip(b"\r").decode("iso-8859-1")
        parsed, status, message = _validate_request_line(requestline)
        if parsed is None:
            self._fail(conn, status, message)
            return False
        method, target, version, close = parsed
        raw_lines = [line + b"\r\n" for line in lines[1:]]
        headers = _parse_headers_fast(raw_lines)
        if headers is None:
            headers = _parse_headers_lenient(raw_lines)
        conn.method = method
        conn.target = target
        conn.version = version
        conn.close_conn = close
        conn.headers = headers
        # Connection header overrides the version default.
        conntype = headers.get("connection", "")
        if conntype.lower() == "close":
            conn.close_conn = True
        elif conntype.lower() == "keep-alive":
            conn.close_conn = False
        # Body framing: Content-Length wins over chunked (as _read_body did).
        length = headers.get("content-length")
        if length is not None:
            try:
                conn.body_needed = int(length)
            except ValueError:
                conn.body_needed = 0
            if conn.body_needed < 0:
                conn.body_needed = 0
            conn.chunked = False
        elif headers.get("transfer-encoding", "").lower() == "chunked":
            conn.chunked = True
            conn.body_needed = 0
        else:
            conn.body_needed = 0
        # Expect: 100-continue is answered once the headers are in.
        expect = headers.get("expect", "")
        if (expect.lower() == "100-continue" and version >= "HTTP/1.1"
                and not conn.sent_100):
            conn.outbuf += b"HTTP/1.1 100 Continue\r\n\r\n"
            conn.sent_100 = True
        return True

    def _parse_body(self, conn: _Conn) -> bool:
        """True when the request body is complete (or an error was sent)."""
        if not conn.chunked:
            need = conn.body_needed - len(conn.body)
            if need <= 0:
                return True
            take = min(need, len(conn.inbuf))
            conn.body += conn.inbuf[:take]
            del conn.inbuf[:take]
            return len(conn.body) >= conn.body_needed
        # chunked: chunk-size line, data, CRLF, repeat; 0-size ends it.
        while True:
            if conn.chunk_left < 0:
                nl = conn.inbuf.find(b"\n")
                if nl < 0:
                    return False
                line = bytes(conn.inbuf[:nl]).strip()
                del conn.inbuf[:nl + 1]
                try:
                    conn.chunk_left = int(line.split(b";", 1)[0], 16)
                except ValueError:
                    self._fail(conn, 400, "Bad chunk size")
                    return False
                if conn.chunk_left == 0:
                    # trailers until a blank line
                    while True:
                        nl = conn.inbuf.find(b"\n")
                        if nl < 0:
                            return False
                        trailer = bytes(conn.inbuf[:nl + 1])
                        del conn.inbuf[:nl + 1]
                        if trailer in (b"\r\n", b"\n", b""):
                            return True
                continue
            take = min(conn.chunk_left, len(conn.inbuf))
            conn.body += conn.inbuf[:take]
            del conn.inbuf[:take]
            conn.chunk_left -= take
            if conn.chunk_left > 0:
                return False
            # consume the CRLF after the chunk data
            if len(conn.inbuf) < 2:
                return False
            if conn.inbuf[:2] == b"\r\n":
                del conn.inbuf[:2]
            elif conn.inbuf[:1] == b"\n":
                del conn.inbuf[:1]
            else:
                self._fail(conn, 400, "Bad chunk terminator")
                return False
            conn.chunk_left = -1

    def _dispatch_conn(self, conn: _Conn):
        raw = conn.target or "/"
        if "://" in raw:
            split = urlsplit(raw)
            path = unquote(split.path or "/")
            query = {k: v[0] for k, v in parse_qs(split.query).items()}
        elif "?" in raw:
            target, _, qs = raw.partition("?")
            path = unquote(target) if "%" in target else target
            query = {k: v[0] for k, v in parse_qs(qs).items()}
        else:
            path = unquote(raw) if "%" in raw else raw
            query = {}
        segments = [s for s in path.split("/") if s != ""]
        req = Request(conn.method, path, segments, query, conn.headers,
                      bytes(conn.body))
        try:
            resp = route(self.store, req)
        except Exception as err:  # never let one request kill the server
            resp = json_error(500, f"internal error: {err}")
        conn.outbuf += _response_bytes(resp, conn.method)
        if conn.close_conn or conn.method == "CONNECT":
            conn.close_after_write = True


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


def serve(store: Store, host: str = "127.0.0.1", port: int = 0) -> _LoopServer:
    return _LoopServer(host, port, store)
