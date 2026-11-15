"""Entrypoint: bind an ephemeral port, register with the env, wait for done.

Lifecycle (kit starter/main.ts, mirrored):
  1. MAKEFUN_ENV_URL names the env's origin. Without it the forge still
     serves, on an ephemeral port (or --port), for local smoke tests.
  2. Listen on port 0 (never 8080 — the env owns it).
  3. POST $MAKEFUN_ENV_URL/register {"port": N}. 200 is normal; 409 means
     another entry already registered (keep waiting like any other).
  4. GET $MAKEFUN_ENV_URL/done until {"done": true}; poll with exponential
     backoff, never exit before the env says done.
  5. Close the server and exit 0.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
import urllib.error
import urllib.request

from forge.server import serve
from forge.store import Store

REQUEST_TIMEOUT_S = 30
RETRY_INITIAL_S = 0.25
RETRY_MAX_S = 2.0


def _retry_delay(attempt: int) -> float:
    return min(RETRY_MAX_S, RETRY_INITIAL_S * (2 ** min(attempt, 8)))


def _request(method: str, url: str, payload=None, timeout: float = REQUEST_TIMEOUT_S):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, response.read()


def register(env_url: str, port: int) -> None:
    attempt = 0
    while True:
        try:
            status, _body = _request("POST", f"{env_url}/register", {"port": port})
            if status in (200, 409):
                return
            if status in (400, 405):
                raise RuntimeError(f"register answered HTTP {status}")
        except urllib.error.HTTPError as err:
            if err.code in (400, 405):
                raise RuntimeError(f"register answered HTTP {err.code}") from err
        except (urllib.error.URLError, TimeoutError, OSError):
            pass
        time.sleep(_retry_delay(attempt))
        attempt += 1


def wait_for_done(env_url: str, stop) -> bool:
    """Poll /done until it says true. Returns False when `stop` is set."""
    attempt = 0
    while not stop.is_set():
        try:
            status, body = _request("GET", f"{env_url}/done")
            if status == 200:
                parsed = json.loads(body.decode("utf-8"))
                if parsed.get("done") is True:
                    return True
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            pass
        stop.wait(_retry_delay(attempt))
        attempt += 1
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="github-core-clone-01 forge")
    parser.add_argument("--port", type=int, default=0,
                        help="port to bind (default: ephemeral)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--token", default=None,
                        help="bearer token (default: $MAKEFUN_SHOP_TOKEN)")
    args = parser.parse_args()

    token = args.token if args.token is not None else os.environ.get("MAKEFUN_SHOP_TOKEN", "")
    store = Store(token=token, trial_now=os.environ.get("MAKEFUN_TRIAL_NOW"))
    server = serve(store, host=args.host, port=args.port)
    port = server.server_address[1]
    print(f"[entry] listening on port {port}", flush=True)

    # The server must serve while the main thread does the lifecycle
    # (register, poll /done). Without this thread the socket listens but
    # nobody accepts, and every request hangs.
    import threading
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    env_url = os.environ.get("MAKEFUN_ENV_URL", "").rstrip("/")
    if not env_url:
        print("[entry] MAKEFUN_ENV_URL is not set: serving without registering",
              flush=True)
        try:
            server_thread.join()
        except KeyboardInterrupt:
            pass
        return 0

    import threading
    stop = threading.Event()

    def _on_signal(_signum, _frame):
        stop.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    register(env_url, port)
    print("[entry] registered; waiting for the env to say done", flush=True)
    done = wait_for_done(env_url, stop)
    server.shutdown()
    server.server_close()
    print("[entry] done" if done else "[entry] stopped", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
