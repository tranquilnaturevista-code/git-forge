"""Shared in-memory store for the git forge.

Pure Python 3.13 standard library. No I/O, no wall clock, no randomness:
every id comes from a monotonic counter, every timestamp is a fixed epoch,
so two runs fed the same requests produce the same responses.

The store is the single source of truth for all four surfaces (git smart
HTTP, REST v3, GraphQL v4, MCP). Repositories are keyed by name; the owner
login is bound lazily (see bind_owner) because the bearer token alone does
not name its login.
"""

import base64
import hashlib
import threading

# Fixed deterministic timestamps. SCENARIO_EPOCH_SEC in the kit is
# 1735689600 (2025-01-01T00:00:00Z); every created_at/updated_at/merged_at
# and every server-authored git commit uses it.
EPOCH = 1735689600
TS = "2025-01-01T00:00:00Z"
IDENT_NAME = "git-forge"
IDENT_EMAIL = "git-forge@make.fun"


def sha1_obj(obj_type, data):
    """Git object id: SHA-1("<type> <len>\\0" + bytes)."""
    h = hashlib.sha1()
    h.update(obj_type.encode("ascii"))
    h.update(b" ")
    h.update(str(len(data)).encode("ascii"))
    h.update(b"\0")
    h.update(data)
    return h.hexdigest()


def global_id(kind, *parts):
    """Opaque, stable, reversible global id (never the plain decimal)."""
    raw = (kind + ":" + ":".join(str(p) for p in parts)).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def parse_global_id(value):
    """Inverse of global_id; returns (kind, [parts]) or None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        padded = value + "=" * (-len(value) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except Exception:
        return None
    bits = raw.split(":")
    if len(bits) < 2:
        return None
    return bits[0], bits[1:]


class ExistsError(Exception):
    pass


class NotFoundError(Exception):
    pass


class ConflictError(Exception):
    pass


class Repo:
    __slots__ = (
        "name", "id", "description", "private", "default_branch",
        "objects", "refs", "issues", "pulls", "labels", "comments",
        "counter", "comment_seq", "label_seq", "created_seq",
    )

    def __init__(self, name, rid, description, private):
        self.name = name
        self.id = rid
        self.description = description
        self.private = private
        self.default_branch = "main"
        self.objects = {}      # oid (hex) -> (type str, bytes)
        self.refs = {}         # refname -> oid (hex)
        self.issues = {}       # number -> dict
        self.pulls = {}        # number -> dict
        self.labels = {}       # name -> dict
        self.comments = {}     # issue number -> [dict]
        self.counter = 1       # shared issue/PR number counter
        self.comment_seq = 1
        self.label_seq = 1
        self.created_seq = 0   # monotonic creation sequence inside this repo


class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self.owner_login = None   # lazily bound token owner login
        self.next_id = 1          # global monotonic integer id counter
        self.repos = {}           # name -> Repo
        self.request_seq = 0      # for X-GitHub-Request-Id

    def next_request_id(self):
        self.request_seq += 1
        return format(self.request_seq, "x")

    def alloc_id(self):
        rid = self.next_id
        self.next_id += 1
        return rid


def bind_owner(store, owner):
    """Bind the token owner's login on first sight; return the bound login."""
    if store.owner_login is None:
        store.owner_login = owner
    return store.owner_login


def owner_ok(store, owner):
    """True when `owner` is the bound owner (or nothing is bound yet)."""
    return store.owner_login is None or store.owner_login == owner


def new_repo(store, name, description="", private=False):
    if name in store.repos:
        raise ExistsError("already exists")
    repo = Repo(name, store.alloc_id(), description, private)
    store.repos[name] = repo
    return repo


def get_repo(store, owner, name):
    """Return the repo only when it exists AND the owner matches the bound
    owner (a foreign owner is a 404, never a 403). Binds the owner lazily
    on the first owner-named request for an existing repo."""
    repo = store.repos.get(name)
    if repo is None:
        return None
    if store.owner_login is None:
        bind_owner(store, owner)
    if store.owner_login != owner:
        return None
    return repo


def delete_repo(store, owner, name):
    repo = get_repo(store, owner, name)
    if repo is None:
        raise NotFoundError("repo not found")
    del store.repos[name]
    return repo


# ---------------------------------------------------------------- git objects

def store_object(repo, obj_type, data):
    oid = sha1_obj(obj_type, data)
    repo.objects[oid] = (obj_type, data)
    return oid


def get_object(repo, oid):
    return repo.objects.get(oid)


def parse_tree(data):
    """Parse a tree object into [(mode_str, name, oid_hex)]."""
    out = []
    i = 0
    n = len(data)
    while i < n:
        sp = data.index(b" ", i)
        mode = data[i:sp].decode("ascii")
        nul = data.index(b"\0", sp + 1)
        name = data[sp + 1:nul].decode("utf-8", "surrogateescape")
        oid = data[nul + 1:nul + 21].hex()
        out.append((mode, name, oid))
        i = nul + 21
    return out


def build_tree(repo, entries):
    """Build a tree object from [(mode, name, oid)] (git-sorted) and store it."""
    def sort_key(entry):
        mode, name, _oid = entry
        return name + "/" if mode == "40000" else name
    ordered = sorted(entries, key=sort_key)
    buf = bytearray()
    for mode, name, oid in ordered:
        buf += mode.encode("ascii") + b" " + name.encode("utf-8", "surrogateescape")
        buf += b"\0" + bytes.fromhex(oid)
    return store_object(repo, "tree", bytes(buf))


def parse_commit(data):
    """Parse a commit object into (headers dict list, message bytes)."""
    head, _, message = data.partition(b"\n\n")
    headers = []
    for line in head.split(b"\n"):
        if not line:
            continue
        key, _, val = line.partition(b" ")
        headers.append((key.decode("ascii"), val))
    return headers, message


def commit_tree_oid(data):
    headers, _ = parse_commit(data)
    for key, val in headers:
        if key == "tree":
            return val.decode("ascii")
    return None


def commit_parents(data):
    headers, _ = parse_commit(data)
    return [val.decode("ascii") for key, val in headers if key == "parent"]


def make_commit(repo, tree_oid, parents, message, ts=EPOCH):
    """Create a commit object (verbatim format) and store it."""
    if not message.endswith("\n"):
        message += "\n"
    ident = ("%s <%s> %d +0000" % (IDENT_NAME, IDENT_EMAIL, ts)).encode("ascii")
    buf = bytearray()
    buf += b"tree " + tree_oid.encode("ascii") + b"\n"
    for parent in parents:
        buf += b"parent " + parent.encode("ascii") + b"\n"
    buf += b"author " + ident + b"\n"
    buf += b"committer " + ident + b"\n"
    buf += b"\n" + message.encode("utf-8", "surrogateescape")
    return store_object(repo, "commit", bytes(buf))


def reachable(repo, want_oids):
    """All oids reachable from `want_oids` (commit->tree+parents,
    tag->target, tree->entries except gitlink mode 160000)."""
    seen = set()
    stack = list(want_oids)
    while stack:
        oid = stack.pop()
        if oid in seen:
            continue
        entry = repo.objects.get(oid)
        if entry is None:
            continue
        seen.add(oid)
        obj_type, data = entry
        if obj_type == "commit":
            tree = commit_tree_oid(data)
            if tree:
                stack.append(tree)
            stack.extend(commit_parents(data))
        elif obj_type == "tag":
            headers, _ = parse_commit(data)
            for key, val in headers:
                if key == "object":
                    stack.append(val.decode("ascii"))
        elif obj_type == "tree":
            for mode, _name, child in parse_tree(data):
                if mode != "160000":
                    stack.append(child)
    return seen


def is_ancestor(repo, anc, desc):
    """True when `anc` is `desc` or an ancestor of it (commit parents)."""
    if anc == desc:
        return True
    seen = set()
    stack = [desc]
    while stack:
        oid = stack.pop()
        if oid in seen:
            continue
        seen.add(oid)
        entry = repo.objects.get(oid)
        if entry is None or entry[0] != "commit":
            continue
        for parent in commit_parents(entry[1]):
            if parent == anc:
                return True
            stack.append(parent)
    return False


def merge_base(repo, a, b):
    """First common ancestor of commits a and b (BFS from both sides)."""
    if a == b:
        return a
    # ancestors of a with depth
    depth_a = {}
    queue = [(a, 0)]
    seen = {a}
    while queue:
        oid, depth = queue.pop(0)
        depth_a[oid] = depth
        entry = repo.objects.get(oid)
        if entry is None or entry[0] != "commit":
            continue
        for parent in commit_parents(entry[1]):
            if parent not in seen:
                seen.add(parent)
                queue.append((parent, depth + 1))
    # BFS from b, first hit in depth_a wins (lowest b-depth, then a-depth)
    queue = [(b, 0)]
    seen = {b}
    best = None
    best_key = None
    while queue:
        oid, depth = queue.pop(0)
        if oid in depth_a:
            key = (depth, depth_a[oid])
            if best_key is None or key < best_key:
                best = oid
                best_key = key
        entry = repo.objects.get(oid)
        if entry is None or entry[0] != "commit":
            continue
        for parent in commit_parents(entry[1]):
            if parent not in seen:
                seen.add(parent)
                queue.append((parent, depth + 1))
    return best


def flatten_tree(repo, tree_oid):
    """{path: (mode, oid)} for every blob under a tree (recursive)."""
    out = {}

    def walk(tree, prefix):
        entry = repo.objects.get(tree)
        if entry is None or entry[0] != "tree":
            return
        for mode, name, oid in parse_tree(entry[1]):
            path = prefix + name
            if mode == "40000":
                walk(oid, path + "/")
            else:
                out[path] = (mode, oid)

    walk(tree_oid, "")
    return out


def read_path(repo, commit_oid, path):
    """File bytes at `path` in the tree of `commit_oid`, or None."""
    entry = repo.objects.get(commit_oid)
    if entry is None or entry[0] != "commit":
        return None
    tree = commit_tree_oid(entry[1])
    flat = flatten_tree(repo, tree)
    hit = flat.get(path)
    if hit is None:
        return None
    blob = repo.objects.get(hit[1])
    if blob is None:
        return None
    return blob[1]


def read_tree_at(repo, commit_oid, path):
    """(is_dir, entries) for `path` in a commit's tree; entries is a list
    of (name, mode, oid) for a directory, or None when missing."""
    entry = repo.objects.get(commit_oid)
    if entry is None or entry[0] != "commit":
        return None
    tree = commit_tree_oid(entry[1])
    if path in ("", "/"):
        flat = flatten_tree(repo, tree)
        names = sorted({p.split("/")[0] for p in flat})
        out = []
        for name in names:
            # decide dir vs file by whether any path has this prefix
            is_dir = any(p.startswith(name + "/") for p in flat)
            if is_dir:
                out.append((name, "40000", None))
            else:
                out.append((name, flat[name][0], flat[name][1]))
        return (True, out)
    flat = flatten_tree(repo, tree)
    prefix = path.rstrip("/") + "/"
    children = {}
    for p, (mode, oid) in flat.items():
        if p == path:
            return (False, [(path.split("/")[-1], mode, oid)])
        if p.startswith(prefix):
            rest = p[len(prefix):]
            first = rest.split("/")[0]
            if "/" in rest:
                children[first] = ("40000", None)
            else:
                children[first] = (mode, oid)
    if not children:
        return None
    return (True, [(n, m, o) for n, (m, o) in sorted(children.items())])


def write_path(repo, base_commit_oid, path, data, message):
    """Create a commit on top of `base_commit_oid` (or None for an unborn
    repo) replacing/adding the file at `path` with `data`. Returns the new
    commit oid. Other files are unchanged."""
    blob_oid = store_object(repo, "blob", data)
    if base_commit_oid is None:
        parents = []
        base_tree = None
    else:
        parents = [base_commit_oid]
        entry = repo.objects.get(base_commit_oid)
        base_tree = commit_tree_oid(entry[1]) if entry else None
    parts = [p for p in path.split("/") if p != ""]
    new_tree = _put_in_tree(repo, base_tree, parts, blob_oid)
    return make_commit(repo, new_tree, parents, message)


def _put_in_tree(repo, tree_oid, parts, blob_oid):
    """Return a new tree oid with parts[0]/... set to blob_oid."""
    entries = {}
    if tree_oid is not None:
        entry = repo.objects.get(tree_oid)
        if entry is not None and entry[0] == "tree":
            for mode, name, oid in parse_tree(entry[1]):
                entries[name] = (mode, oid)
    name = parts[0]
    if len(parts) == 1:
        entries[name] = ("100644", blob_oid)
    else:
        sub = entries.get(name)
        sub_tree = sub[1] if sub is not None and sub[0] == "40000" else None
        entries[name] = ("40000", _put_in_tree(repo, sub_tree, parts[1:], blob_oid))
    return build_tree(repo, [(m, n, o) for n, (m, o) in entries.items()])


def delete_path(repo, base_commit_oid, path, message):
    """Create a commit removing `path` from the tree. Returns the new
    commit oid, or None when the path does not exist."""
    entry = repo.objects.get(base_commit_oid)
    if entry is None or entry[0] != "commit":
        return None
    base_tree = commit_tree_oid(entry[1])
    parts = [p for p in path.split("/") if p != ""]
    new_tree = _del_from_tree(repo, base_tree, parts)
    if new_tree is None:
        return None
    return make_commit(repo, new_tree, [base_commit_oid], message)


def _del_from_tree(repo, tree_oid, parts):
    """Return a new tree oid with the path removed, or None if absent."""
    if tree_oid is None:
        return None
    entry = repo.objects.get(tree_oid)
    if entry is None or entry[0] != "tree":
        return None
    entries = {}
    for mode, name, oid in parse_tree(entry[1]):
        entries[name] = (mode, oid)
    name = parts[0]
    if name not in entries:
        return None
    if len(parts) == 1:
        del entries[name]
    else:
        mode, oid = entries[name]
        if mode != "40000":
            return None
        sub = _del_from_tree(repo, oid, parts[1:])
        if sub is None:
            return None
        if sub == "empty":
            del entries[name]
        else:
            entries[name] = ("40000", sub)
    if not entries:
        return "empty"
    return build_tree(repo, [(m, n, o) for n, (m, o) in entries.items()])


def diff_trees(repo, base_tree_oid, head_tree_oid):
    """Diff two trees: [{path, status, old, new}] where old/new are
    (mode, oid) or None. status in added/modified/removed."""
    base = flatten_tree(repo, base_tree_oid) if base_tree_oid else {}
    head = flatten_tree(repo, head_tree_oid) if head_tree_oid else {}
    out = []
    for path in sorted(set(base) | set(head)):
        b = base.get(path)
        h = head.get(path)
        if b is None:
            out.append({"path": path, "status": "added", "old": None, "new": h})
        elif h is None:
            out.append({"path": path, "status": "removed", "old": b, "new": None})
        elif b[1] != h[1] or b[0] != h[0]:
            out.append({"path": path, "status": "modified", "old": b, "new": h})
    return out
