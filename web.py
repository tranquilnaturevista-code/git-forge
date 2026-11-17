"""HTTP plumbing shared by the four surfaces.

A handler receives a Req and returns a Resp (or raises HttpError).
main.py owns the socket server and routing; the surface modules only
implement handlers against these types.
"""

import json


class Req:
    __slots__ = ("method", "path", "query", "headers", "body", "base_url", "token")

    def __init__(self, method, path, query, headers, body, base_url, token):
        self.method = method          # e.g. "GET"
        self.path = path              # decoded path, e.g. "/api/v3/user/repos"
        self.query = query            # dict[str, list[str]]
        self.headers = headers        # dict with lowercase keys
        self.body = body              # raw request bytes
        self.base_url = base_url      # origin, e.g. "http://127.0.0.1:54321"
        self.token = token            # MAKEFUN_SHOP_TOKEN or None

    def query_one(self, name, default=None):
        vals = self.query.get(name)
        return vals[0] if vals else default

    def json_body(self):
        """Parse the body as JSON; raises HttpError(400) on failure."""
        if not self.body:
            return {}
        try:
            data = json.loads(self.body.decode("utf-8"))
        except Exception:
            raise HttpError(400, "Invalid JSON body")
        if not isinstance(data, dict):
            raise HttpError(400, "JSON body must be an object")
        return data

    def bearer(self):
        auth = self.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:]
        return None


class Resp:
    __slots__ = ("status", "headers", "body")

    def __init__(self, status, headers=None, body=b""):
        self.status = status
        self.headers = headers or {}
        self.body = body


class HttpError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def json_resp(store, status, obj, extra_headers=None):
    """JSON response with the mandatory headers. `obj` may be None (empty
    body, used only for 204)."""
    body = b"" if obj is None else (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8")
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "X-GitHub-Request-Id": store.next_request_id(),
    }
    if extra_headers:
        headers.update(extra_headers)
    return Resp(status, headers, body)


def error_resp(store, status, message):
    return json_resp(store, status, {"message": message})


def require_auth(store, req):
    """401 unless the request carries the exact configured bearer token.
    When no token is configured (local practice), any non-empty bearer is
    accepted; a missing one is still 401."""
    token = req.bearer()
    if token is None or token == "":
        raise HttpError(401, "Requires authentication")
    if req.token is not None and req.token != "" and token != req.token:
        raise HttpError(401, "Bad credentials")
    return token


def err_message(exc):
    if isinstance(exc, HttpError):
        return exc.status, exc.message
    return 500, "Internal Server Error"
