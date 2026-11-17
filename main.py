"""Git Forge — a GitHub-compatible git forge in pure Python 3.13 stdlib.

One process, no UI, no outside service, no git binary, no package manager.
Surfaces: git smart HTTP (upload-pack v2, receive-pack v0), REST v3,
GraphQL v4, MCP over Streamable HTTP — all on one shared in-memory store.

Lifecycle (trial mode, MAKEFUN_ENV_URL set): bind an ephemeral port, POST
{"port": N} to $MAKEFUN_ENV_URL/register, long-poll GET $MAKEFUN_ENV_URL/done
until {"done": true}, then exit 0. Standalone mode (no MAKEFUN_ENV_URL):
serve forever on $PORT (default 0) for the local runner.
"""

import json
import os
import socket
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import graphql
import mcp
import rest
import store as store_mod
import web

try:
    import githttp
except ImportError:  # git surface not landed yet; other surfaces still serve
    githttp = None

SURFACES = tuple(s for s in (rest, graphql, mcp, githttp) if s is not None)

REQUEST_TIMEOUT_S = 30
RETRY_INITIAL_S = 0.25
RETRY_MAX_S = 2.0


def retry_delay(attempt):
    return min(RETRY_MAX_S, RETRY_INITIAL_S * (2 ** min(attempt, 8)))


def http_json(method, url, body=None, timeout=REQUEST_TIMEOUT_S):
    """One JSON request with urllib; returns (status, parsed_json_or_None)."""
    data = None
    headers = {"Content-Type": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            try:
                return resp.status, json.loads(raw.decode("utf-8")) if raw else None
            except Exception:
                return resp.status, None
    except urllib.error.HTTPError as err:
        raw = err.read()
        try:
            return err.code, json.loads(raw.decode("utf-8")) if raw else None
        except Exception:
            return err.code, None
    except Exception:
        return 0, None


def register(env_url, port):
    for attempt in range(100000):
        status, _body = http_json("POST", env_url.rstrip("/") + "/register", {"port": port})
        if status in (200, 409):
            return
        if status in (400, 405):
            raise RuntimeError("register answered HTTP %d" % status)
        time.sleep(retry_delay(attempt))


def wait_for_done(env_url):
    for attempt in range(100000):
        status, body = http_json("GET", env_url.rstrip("/") + "/done")
        if status == 200 and isinstance(body, dict) and body.get("done") is True:
            return
        time.sleep(retry_delay(attempt))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "git-forge/1.0.0"

    def setup(self):
        super().setup()
        # Small JSON responses must not wait on Nagle/delayed-ACK (~20-40 ms
        # per request): the timed workloads are thousands of small replies.
        try:
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception:
            pass

    def log_message(self, *args):
        pass

    def _read_body(self):
        length = self.headers.get("Content-Length")
        if length is not None:
            try:
                n = int(length)
            except ValueError:
                return b""
            if n <= 0:
                return b""
            return self.rfile.read(n)
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            chunks = bytearray()
            while True:
                line = self.rfile.readline(65536).strip()
                try:
                    size = int(line.split(b";")[0], 16)
                except ValueError:
                    break
                if size == 0:
                    self.rfile.readline(65536)
                    break
                chunks += self.rfile.read(size)
                self.rfile.readline(65536)
            return bytes(chunks)
        return b""

    def _dispatch(self, method):
        try:
            split = urllib.parse.urlsplit(self.path)
            path = urllib.parse.unquote(split.path)
            query = urllib.parse.parse_qs(split.query, keep_blank_values=True)
            body = self._read_body() if method in ("POST", "PUT", "PATCH", "DELETE") else b""
            headers = {k.lower(): v for k, v in self.headers.items()}
            token = os.environ.get("MAKEFUN_SHOP_TOKEN") or None
            req = web.Req(method, path, query, headers, body, self.server.base_url, token)
            st = self.server.store
            with st.lock:
                resp = self._route(st, req)
            self._send(resp)
        except Exception as exc:  # never crash the process on a request
            sys.stderr.write("[entry] handler error: %r\n" % (exc,))
            try:
                self._send(web.Resp(500, {"Content-Type": "application/json"},
                                    b'{"message":"Internal Server Error"}\n'))
            except Exception:
                pass

    def _route(self, st, req):
        if req.method == "GET" and req.path == "/healthz":
            return web.json_resp(st, 200, {"ok": True})
        for surface in SURFACES:
            try:
                resp = surface.handle(st, req)
            except web.HttpError as err:
                return web.error_resp(st, err.status, err.message)
            except Exception as exc:
                sys.stderr.write("[entry] surface error: %r\n" % (exc,))
                return web.error_resp(st, 500, "Internal Server Error")
            if resp is not None:
                return resp
        return web.error_resp(st, 404, "Not Found")

    def _send(self, resp):
        body = resp.body or b""
        headers = dict(resp.headers)
        headers.setdefault("Content-Length", str(len(body)))
        self.send_response(resp.status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

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


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # 50 concurrent clones open 50 connections at once; the default
    # listen backlog of 5 drops SYNs and costs ~1 s retransmits each.
    request_queue_size = 128


def make_server(port):
    st = store_mod.Store()
    server = Server(("0.0.0.0", port), Handler)
    server.store = st
    host, bound_port = server.server_address[:2]
    server.base_url = "http://127.0.0.1:%d" % bound_port
    return server, bound_port


def lifecycle(env_url, server, port):
    try:
        register(env_url, port)
        sys.stdout.write("[entry] registered; waiting for the env to say done\n")
        sys.stdout.flush()
        wait_for_done(env_url)
    except Exception as exc:
        sys.stderr.write("[entry] lifecycle error: %r\n" % (exc,))
        os._exit(1)
    sys.stdout.write("[entry] done\n")
    sys.stdout.flush()
    server.shutdown()
    os._exit(0)


def main():
    env_url = (os.environ.get("MAKEFUN_ENV_URL") or "").rstrip("/")
    if env_url:
        server, port = make_server(0)
        sys.stdout.write("[entry] listening on port %d\n" % port)
        sys.stdout.flush()
        thread = threading.Thread(target=lifecycle, args=(env_url, server, port), daemon=True)
        thread.start()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        return 0
    # Standalone mode (local runner): no env to register with.
    port = int(os.environ.get("PORT", "0"))
    server, bound_port = make_server(port)
    sys.stdout.write("[entry] listening on port %d (standalone)\n" % bound_port)
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
