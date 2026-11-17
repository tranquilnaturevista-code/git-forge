"""MCP surface for the git forge (make.fun github-core-clone-01).

JSON-RPC 2.0 over Streamable HTTP at POST /mcp (DELETE /mcp terminates a
session). One handler: ``handle(store, req) -> Resp | None``; only POST and
DELETE /mcp are ours, anything else returns None so the router can decide.

Stdlib only. No file I/O, no wall clock, no randomness, no subprocess, no
module-level mutable state: the tools list is rebuilt from literals on every
call (byte-identical), and every handler is self-contained apart from the
shared store passed in. All state lives in the store, so MCP, REST,
GraphQL and git all see the same repos, issues and pull requests.
"""

import difflib
import json

from store import (
    TS,
    ExistsError,
    bind_owner,
    build_tree,
    commit_parents,
    commit_tree_oid,
    diff_trees,
    flatten_tree,
    get_repo,
    global_id,
    make_commit,
    merge_base,
    new_repo,
)
from web import HttpError, Resp, json_resp, require_auth

SERVER_NAME = "git-forge"
SERVER_VERSION = "1.0.0"
MODERN_REVISION = "2026-07-28"
LEGACY_REVISIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602


# ---------------------------------------------------------------- tools list

def _tools_list():
    """The six tools, in fixed order, rebuilt from literals every call."""
    return [
        {
            "name": "create_repo",
            "description": "Create a repository owned by the authenticated user.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "private": {"type": "boolean"},
                },
                "required": ["name"],
            },
        },
        {
            "name": "get_repo",
            "description": "Get a repository by owner and name.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "repo": {"type": "string"},
                },
                "required": ["owner", "repo"],
            },
        },
        {
            "name": "create_issue",
            "description": "Create an issue in a repository.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "repo": {"type": "string"},
                    "title": {"type": "string"},
                    "body": {"type": "string"},
                },
                "required": ["owner", "repo", "title"],
            },
        },
        {
            "name": "list_issues",
            "description": "List issues in a repository.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "repo": {"type": "string"},
                    "state": {"type": "string"},
                },
                "required": ["owner", "repo"],
            },
        },
        {
            "name": "create_pull_request",
            "description": "Create a pull request in a repository.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "repo": {"type": "string"},
                    "title": {"type": "string"},
                    "body": {"type": "string"},
                    "head": {"type": "string"},
                    "base": {"type": "string"},
                },
                "required": ["owner", "repo", "title", "head", "base"],
            },
        },
        {
            "name": "merge_pull_request",
            "description": "Merge a pull request.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "repo": {"type": "string"},
                    "pull_number": {"type": "integer"},
                },
                "required": ["owner", "repo", "pull_number"],
            },
        },
    ]


# Required properties per tool, in schema order (for missing-arg reports).
_REQUIRED = {
    "create_repo": ["name"],
    "get_repo": ["owner", "repo"],
    "create_issue": ["owner", "repo", "title"],
    "list_issues": ["owner", "repo"],
    "create_pull_request": ["owner", "repo", "title", "head", "base"],
    "merge_pull_request": ["owner", "repo", "pull_number"],
}

# Normalized-name synonyms per canonical property, per tool. Normalization
# is lowercase with every non-alphanumeric stripped, matching the suite's
# mapping (so "owner_login", "ownerLogin" and "owner-login" all hit "owner").
_SYNONYMS = {
    "create_repo": {
        "name": ("name", "repo", "repository", "repositoryname", "reponame"),
        "description": ("description", "about", "summary", "body"),
        "private": ("private", "isprivate"),
        "owner": ("owner", "ownerlogin", "org", "organization", "organisation",
                  "user", "userlogin", "login", "account", "namespace", "ownername"),
    },
    "get_repo": {
        "owner": ("owner", "ownerlogin", "org", "organization", "organisation",
                  "user", "userlogin", "login", "account", "namespace", "ownername"),
        "repo": ("repo", "repository", "repositoryname", "reponame", "reposlug",
                 "project", "projectname"),
        "full_name": ("fullname", "repofullname", "repositoryfullname"),
    },
    "create_issue": {
        "owner": ("owner", "ownerlogin", "org", "organization", "organisation",
                  "user", "userlogin", "login", "account", "namespace", "ownername"),
        "repo": ("repo", "repository", "repositoryname", "reponame", "reposlug",
                 "project", "projectname"),
        "full_name": ("fullname", "repofullname", "repositoryfullname"),
        "title": ("title", "issuetitle", "prtitle", "pullrequesttitle", "subject"),
        "body": ("body", "issuebody", "description", "content", "text", "comment"),
    },
    "list_issues": {
        "owner": ("owner", "ownerlogin", "org", "organization", "organisation",
                  "user", "userlogin", "login", "account", "namespace", "ownername"),
        "repo": ("repo", "repository", "repositoryname", "reponame", "reposlug",
                 "project", "projectname"),
        "full_name": ("fullname", "repofullname", "repositoryfullname"),
        "state": ("state", "status"),
    },
    "create_pull_request": {
        "owner": ("owner", "ownerlogin", "org", "organization", "organisation",
                  "user", "userlogin", "login", "account", "namespace", "ownername"),
        "repo": ("repo", "repository", "repositoryname", "reponame", "reposlug",
                 "project", "projectname"),
        "full_name": ("fullname", "repofullname", "repositoryfullname"),
        "title": ("title", "issuetitle", "prtitle", "pullrequesttitle", "subject"),
        "body": ("body", "issuebody", "description", "content", "text", "comment"),
        "head": ("head", "headref", "headbranch", "pullrequesthead", "prhead",
                 "source", "sourceref", "sourcebranch", "from", "frombranch", "branch"),
        "base": ("base", "baseref", "basebranch", "pullrequestbase", "prbase",
                 "target", "targetref", "targetbranch", "into", "intobranch"),
    },
    "merge_pull_request": {
        "owner": ("owner", "ownerlogin", "org", "organization", "organisation",
                  "user", "userlogin", "login", "account", "namespace", "ownername"),
        "repo": ("repo", "repository", "repositoryname", "reponame", "reposlug",
                 "project", "projectname"),
        "full_name": ("fullname", "repofullname", "repositoryfullname"),
        "pull_number": ("pullnumber", "pullrequestnumber", "prnumber", "number",
                        "issuenumber", "index"),
    },
}


def _norm(name):
    return "".join(ch for ch in str(name).lower() if ch.isalnum())


def _map_args(tool, arguments):
    """Map raw arguments onto the tool's canonical property names.

    Returns (mapped, missing) where mapped holds canonical names and
    missing lists required properties (schema order) with no value.
    A "full_name" value "owner/repo" fills owner/repo when repo is absent.
    """
    synonyms = _SYNONYMS[tool]
    by_norm = {}
    for key in arguments:
        by_norm.setdefault(_norm(key), key)
    mapped = {}
    for canonical, names in synonyms.items():
        for name in names:
            raw_key = by_norm.get(name)
            if raw_key is not None:
                mapped[canonical] = arguments[raw_key]
                break
    full = mapped.get("full_name")
    if isinstance(full, str) and "/" in full:
        owner, _, repo = full.partition("/")
        if "owner" not in mapped and owner:
            mapped["owner"] = owner
        if "repo" not in mapped and repo:
            mapped["repo"] = repo
    missing = [name for name in _REQUIRED[tool] if name not in mapped
               or mapped[name] is None or mapped[name] == ""]
    return mapped, missing


def _as_str(value, default=""):
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def _as_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    if value is None:
        return default
    return bool(value)


def _as_int(value):
    """An integer, accepting a numeric string defensively; None otherwise."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


# ---------------------------------------------------------------- views

def _owner_login(store, mapped):
    """The token owner's login: an explicit owner argument (bound, as the
    suite sends the true login for create_repo), else the store's bound
    login, else the last-resort placeholder "user" (the REST GET /user
    binds the real login before MCP normally runs)."""
    owner = mapped.get("owner")
    if isinstance(owner, str) and owner:
        bind_owner(store, owner)
        return owner
    if store.owner_login:
        return store.owner_login
    return "user"


def _repo_view(store, req, login, repo):
    base = req.base_url.rstrip("/")
    return {
        "id": repo.id,
        "name": repo.name,
        "full_name": "%s/%s" % (login, repo.name),
        "owner": {"login": login, "type": "User"},
        "description": repo.description,
        "private": repo.private,
        "default_branch": repo.default_branch,
        "created_at": TS,
        "updated_at": TS,
        "html_url": "%s/%s/%s" % (base, login, repo.name),
        "url": "%s/api/v3/repos/%s/%s" % (base, login, repo.name),
        "fork": False,
        "has_issues": True,
        "has_pull_requests": True,
    }


def _issue_view(req, login, repo, issue):
    view = dict(issue)
    view["user"] = {"login": login, "type": "User"}
    return view


def _new_issue(store, req, login, repo, title, body):
    number = repo.counter
    repo.counter += 1
    repo.created_seq += 1
    base = req.base_url.rstrip("/")
    issue = {
        "id": store.alloc_id(),
        "node_id": global_id("Issue", repo.id, number),
        "number": number,
        "title": title,
        "body": body,
        "state": "open",
        "locked": False,
        "labels": [],
        "comments": 0,
        "user": {"login": login, "type": "User"},
        "created_at": TS,
        "updated_at": TS,
        "closed_at": None,
        "url": "%s/api/v3/repos/%s/%s/issues/%d" % (base, login, repo.name, number),
        "html_url": "%s/%s/%s/issues/%d" % (base, login, repo.name, number),
        "repository_url": "%s/api/v3/repos/%s/%s" % (base, login, repo.name),
        "created_seq": repo.created_seq,
    }
    repo.issues[number] = issue
    repo.comments[number] = []
    return issue


def _pull_view(req, login, repo, pull):
    """The MCP/REST view of a stored pull request: head/base as nested
    {ref, sha, label} objects, shas read from the live branch tips (as
    REST does), so a merged pull shows the base at the merge commit."""
    base_url = req.base_url.rstrip("/")
    head_sha = repo.refs.get("refs/heads/" + pull["head_ref"]) or pull["head_sha"]
    base_sha = repo.refs.get("refs/heads/" + pull["base_ref"]) or pull["base_sha"]
    return {
        "id": pull["id"],
        "node_id": global_id("PullRequest", repo.id, pull["number"]),
        "number": pull["number"],
        "title": pull["title"],
        "body": pull["body"],
        "state": pull["state"],
        "locked": False,
        "draft": False,
        "merged": pull["merged"],
        "mergeable": pull["mergeable"],
        "merged_at": pull["merged_at"],
        "closed_at": pull.get("closed_at"),
        "merge_commit_sha": pull["merge_commit_sha"],
        "head": {"ref": pull["head_ref"], "sha": head_sha,
                 "label": "%s:%s" % (login, pull["head_ref"])},
        "base": {"ref": pull["base_ref"], "sha": base_sha,
                 "label": "%s:%s" % (login, pull["base_ref"])},
        "user": {"login": login, "type": "User"},
        "created_at": pull.get("created_at", TS),
        "updated_at": pull.get("updated_at", TS),
        "changed_files": pull.get("changed_files", 0),
        "additions": pull.get("additions", 0),
        "deletions": pull.get("deletions", 0),
        "commits": 1,
        "url": "%s/api/v3/repos/%s/%s/pulls/%d" % (base_url, login, repo.name, pull["number"]),
        "html_url": "%s/%s/%s/pull/%d" % (base_url, login, repo.name, pull["number"]),
    }


def _new_pull(store, req, login, repo, title, body, head, base, head_sha, base_sha):
    number = repo.counter
    repo.counter += 1
    repo.created_seq += 1
    head_tree = commit_tree_oid(repo.objects[head_sha][1]) if head_sha else None
    base_tree = commit_tree_oid(repo.objects[base_sha][1]) if base_sha else None
    additions = deletions = 0
    changed = 0
    if head_tree is not None and base_tree is not None:
        for change in diff_trees(repo, base_tree, head_tree):
            changed += 1
            old = change["old"]
            new = change["new"]
            old_lines = _blob_lines(repo, old[1]) if old else []
            new_lines = _blob_lines(repo, new[1]) if new else []
            if change["status"] == "added":
                additions += len(new_lines)
            elif change["status"] == "removed":
                deletions += len(old_lines)
            else:
                matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
                for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
                    if tag in ("insert", "replace"):
                        additions += j2 - j1
                    if tag in ("delete", "replace"):
                        deletions += _i2 - _i1
    # Canonical stored shape, shared with REST and GraphQL: flat
    # head_ref/base_ref/head_sha/base_sha plus head/base as plain branch
    # names (GraphQL reads pr["head"] as a string). The nested head/base
    # objects are built by _pull_view at read time, never stored.
    pull = {
        "id": store.alloc_id(),
        "number": number,
        "title": title,
        "body": body,
        "state": "open",
        "head": head,
        "base": base,
        "head_ref": head,
        "base_ref": base,
        "head_sha": head_sha,
        "base_sha": base_sha,
        "merged": False,
        "mergeable": True,
        "merged_at": None,
        "closed_at": None,
        "merge_commit_sha": None,
        "changed_files": changed,
        "additions": additions,
        "deletions": deletions,
        "created_seq": repo.created_seq,
        "created_at": TS,
        "updated_at": TS,
    }
    repo.pulls[number] = pull
    return pull


def _blob_lines(repo, oid):
    entry = repo.objects.get(oid)
    if entry is None or entry[0] != "blob":
        return []
    return entry[1].decode("utf-8", "surrogateescape").splitlines()


# ---------------------------------------------------------------- squash merge

def _build_tree_from_flat(repo, flat):
    """Build (and store) a tree object for {path: (mode, oid)}; None when
    the flat map is empty (the caller then uses the empty tree)."""

    def build_level(prefix):
        entries = {}
        for path, (mode, oid) in flat.items():
            if not path.startswith(prefix):
                continue
            rest = path[len(prefix):]
            first, sep, _ = rest.partition("/")
            if sep:
                sub = build_level(prefix + first + "/")
                if sub is not None:
                    entries[first] = ("40000", sub)
            else:
                entries[first] = (mode, oid)
        if not entries:
            return None
        return build_tree(repo, [(m, n, o) for n, (m, o) in entries.items()])

    tree = build_level("")
    if tree is None:
        return build_tree(repo, [])
    return tree


def _squash_merge(repo, pull):
    """Three-way squash merge of pull["head"] into pull["base"].

    Returns (sha, message) on success, or raises _ToolError on conflict /
    an already-merged pull. One squash commit on the default branch whose
    parent is the base tip and whose tree is the base tree with the head's
    changes applied; the base ref then points at the new commit.
    """
    if pull.get("merged"):
        raise _ToolError("pull request #%d is already merged" % pull["number"])
    base_ref = "refs/heads/" + pull["base_ref"]
    head_ref = "refs/heads/" + pull["head_ref"]
    base_tip = repo.refs.get(base_ref)
    head_tip = repo.refs.get(head_ref)
    if head_tip is None:
        raise _ToolError("unknown head branch: %s" % pull["head_ref"])
    if base_tip is None:
        # Unborn base: the squash commit is the head's tree with no parent.
        head_tree = commit_tree_oid(repo.objects[head_tip][1])
        message = "Merge pull request #%d from %s\n\n%s" % (
            pull["number"], pull["head_ref"], pull["title"])
        sha = make_commit(repo, head_tree, [], message)
        repo.refs[base_ref] = sha
        return sha, message
    ancestor = merge_base(repo, base_tip, head_tip)
    base_tree = commit_tree_oid(repo.objects[ancestor][1]) if ancestor else None
    flat_base = flatten_tree(repo, base_tree) if base_tree else {}
    head_tree = commit_tree_oid(repo.objects[head_tip][1])
    flat_head = flatten_tree(repo, head_tree) if head_tree else {}
    tip_tree = commit_tree_oid(repo.objects[base_tip][1])
    flat_tip = flatten_tree(repo, tip_tree) if tip_tree else {}
    merged = dict(flat_tip)
    for path in sorted(set(flat_base) | set(flat_head)):
        b = flat_base.get(path)
        h = flat_head.get(path)
        if h == b:
            continue  # head did not change this path
        t = flat_tip.get(path)
        if t == b:
            if h is None:
                merged.pop(path, None)
            else:
                merged[path] = h
        elif h == t:
            continue  # both sides made the same change
        else:
            raise _ToolError("merge conflict in %s" % path)
    new_tree = _build_tree_from_flat(repo, merged)
    message = "Merge pull request #%d from %s\n\n%s" % (
        pull["number"], pull["head_ref"], pull["title"])
    sha = make_commit(repo, new_tree, [base_tip], message)
    repo.refs[base_ref] = sha
    return sha, message


class _ToolError(Exception):
    """A tool execution failure: answered as isError:true, never a crash."""


# ---------------------------------------------------------------- tool dispatch

def _lookup_repo(store, mapped):
    owner = _as_str(mapped.get("owner"))
    name = _as_str(mapped.get("repo"))
    repo = get_repo(store, owner, name)
    if repo is None:
        raise _ToolError("repository not found: %s/%s" % (owner, name))
    return owner, repo


def _call_tool(store, req, name, arguments):
    """Run one tool; returns the result object, or raises _ToolError."""
    mapped, missing = _map_args(name, arguments)
    if missing:
        raise _ToolError("missing required argument: %s" % ", ".join(missing))
    if name == "create_repo":
        repo_name = _as_str(mapped.get("name"))
        if not repo_name or "/" in repo_name:
            raise _ToolError("invalid repository name: %r" % repo_name)
        login = _owner_login(store, mapped)
        try:
            repo = new_repo(store, repo_name,
                            _as_str(mapped.get("description"), ""),
                            _as_bool(mapped.get("private"), False))
        except ExistsError:
            raise _ToolError("repository already exists: %s/%s" % (login, repo_name))
        return _repo_view(store, req, login, repo)
    if name == "get_repo":
        owner, repo = _lookup_repo(store, mapped)
        return _repo_view(store, req, owner, repo)
    if name == "create_issue":
        owner, repo = _lookup_repo(store, mapped)
        issue = _new_issue(store, req, owner, repo,
                           _as_str(mapped.get("title")),
                           _as_str(mapped.get("body"), ""))
        return _issue_view(req, owner, repo, issue)
    if name == "list_issues":
        owner, repo = _lookup_repo(store, mapped)
        state = _as_str(mapped.get("state"), "open").lower()
        issues = [dict(issue) for issue in sorted(repo.issues.values(),
                                                  key=lambda i: i["number"])]
        if state == "all":
            pass
        elif state == "closed":
            issues = [i for i in issues if i.get("state") == "closed"]
        else:  # "open" and anything unrecognised defaults to open
            issues = [i for i in issues if i.get("state") == "open"]
        return {"issues": [_issue_view(req, owner, repo, i) for i in issues]}
    if name == "create_pull_request":
        owner, repo = _lookup_repo(store, mapped)
        head = _as_str(mapped.get("head"))
        base = _as_str(mapped.get("base"))
        head_sha = repo.refs.get("refs/heads/" + head)
        if head_sha is None:
            raise _ToolError("unknown head branch: %s" % head)
        base_sha = repo.refs.get("refs/heads/" + base)
        if base_sha is None:
            raise _ToolError("unknown base branch: %s" % base)
        pull = _new_pull(store, req, owner, repo,
                         _as_str(mapped.get("title")),
                         _as_str(mapped.get("body"), ""),
                         head, base, head_sha, base_sha)
        return _pull_view(req, owner, repo, pull)
    if name == "merge_pull_request":
        owner, repo = _lookup_repo(store, mapped)
        number = _as_int(mapped.get("pull_number"))
        if number is None:
            raise _ToolError("invalid pull_number: %r" % (mapped.get("pull_number"),))
        pull = repo.pulls.get(number)
        if pull is None:
            raise _ToolError("pull request not found: #%d" % number)
        sha, message = _squash_merge(repo, pull)
        pull["merged"] = True
        pull["state"] = "closed"
        pull["merged_at"] = TS
        pull["closed_at"] = TS
        pull["updated_at"] = TS
        pull["merge_commit_sha"] = sha
        return {"merged": True, "sha": sha, "message": message}
    raise _ToolError("unknown tool: %s" % name)


# ---------------------------------------------------------------- JSON-RPC

def _rpc_resp(store, rid, result=None, error=None, status=200):
    body = {"jsonrpc": "2.0", "id": rid}
    if error is not None:
        body["error"] = error
    else:
        body["result"] = result
    return json_resp(store, status, body)


def _rpc_error(store, rid, code, message, status=200):
    return _rpc_resp(store, rid, error={"code": code, "message": message},
                     status=status)


def _tool_result(store, rid, result):
    text = json.dumps(result, separators=(",", ":"))
    return _rpc_resp(store, rid, result={
        "content": [{"type": "text", "text": text}],
        "structuredContent": result,
        "isError": False,
    })


def _tool_error(store, rid, message):
    return _rpc_resp(store, rid, result={
        "content": [{"type": "text", "text": message}],
        "isError": True,
    })


# ---------------------------------------------------------------- entry point

def handle(store, req):
    """Serve POST /mcp and DELETE /mcp; return None for any other route."""
    path = req.path.rstrip("/") or "/"
    if path != "/mcp":
        return None
    if req.method == "DELETE":
        # Tolerate any state; a session the server never issued needs no
        # goodbye. Auth is not required to delete.
        return json_resp(store, 204, None)
    if req.method != "POST":
        return None
    require_auth(store, req)
    try:
        payload = json.loads(req.body.decode("utf-8"))
    except Exception:
        return _rpc_resp(store, None, status=400, error={
            "code": PARSE_ERROR, "message": "Parse error"})
    if not isinstance(payload, dict):
        return _rpc_error(store, None, INVALID_REQUEST, "Invalid Request")
    if "id" not in payload:
        # A notification: no JSON-RPC response. notifications/initialized
        # (and any other notification) answers HTTP 202 with an empty body.
        return json_resp(store, 202, None)
    rid = payload.get("id")
    method = payload.get("method")
    if not isinstance(method, str) or method == "":
        return _rpc_error(store, rid, INVALID_REQUEST, "Invalid Request")
    params = payload.get("params")
    if method == "server/discover":
        server_info = {"name": SERVER_NAME, "version": SERVER_VERSION}
        return _rpc_resp(store, rid, result={
            "supportedVersions": [MODERN_REVISION],
            "capabilities": {"tools": {}},
            "serverInfo": dict(server_info),
            "_meta": {"io.modelcontextprotocol/serverInfo": dict(server_info)},
        })
    if method == "initialize":
        offered = params.get("protocolVersion") if isinstance(params, dict) else None
        if offered not in LEGACY_REVISIONS:
            return _rpc_error(store, rid, INVALID_PARAMS,
                              "Unsupported protocol version: %s" % (offered,))
        return _rpc_resp(store, rid, result={
            "protocolVersion": offered,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })
    if method == "ping":
        return _rpc_resp(store, rid, result={})
    if method == "tools/list":
        return _rpc_resp(store, rid, result={"tools": _tools_list()})
    if method == "tools/call":
        if not isinstance(params, dict):
            return _rpc_error(store, rid, INVALID_PARAMS,
                              "params must be an object")
        tool_name = params.get("name")
        if not isinstance(tool_name, str) or tool_name == "":
            return _rpc_error(store, rid, INVALID_PARAMS,
                              "missing required argument: name")
        arguments = params.get("arguments", {})
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return _rpc_error(store, rid, INVALID_PARAMS,
                              "arguments must be an object")
        if tool_name not in _REQUIRED:
            return _rpc_error(store, rid, INVALID_PARAMS,
                              "Unknown tool: %s" % tool_name)
        try:
            result = _call_tool(store, req, tool_name, arguments)
        except _ToolError as exc:
            return _tool_error(store, rid, str(exc))
        return _tool_result(store, rid, result)
    return _rpc_error(store, rid, METHOD_NOT_FOUND,
                      "Method not found: %s" % method)
