"""REST v3 surface for the git forge (make.fun github-core-clone-01).

One entry point: handle(store, req) -> Resp | None.
Returns None when the path is not under /api/v3 (another surface owns it).
Raises HttpError for every error; main.py converts those to JSON errors.

Pure stdlib, no I/O, no wall clock, no randomness, no module-level
mutable state. All git object work goes through store.py helpers.
"""

import base64
import difflib
import re
from urllib.parse import urlencode

from store import (
    TS,
    ExistsError,
    build_tree,
    commit_parents,
    commit_tree_oid,
    delete_path,
    delete_repo,
    diff_trees,
    flatten_tree,
    get_repo,
    make_commit,
    merge_base,
    new_repo,
    parse_commit,
    parse_tree,
    read_path,
    read_tree_at,
    sha1_obj,
    write_path,
)
from web import HttpError, json_resp, require_auth

_SHA40 = re.compile(r"^[0-9a-fA-F]{40}$")


# --------------------------------------------------------------------------
# small helpers

def _login(store):
    """The bound owner login, or "" while nothing has bound it yet."""
    return store.owner_login or ""


def _owner_obj(store):
    return {"login": _login(store), "id": 1, "type": "User"}


def _repo_obj(store, repo):
    login = _login(store)
    return {
        "id": repo.id,
        "name": repo.name,
        "full_name": "%s/%s" % (login, repo.name),
        "owner": _owner_obj(store),
        "description": repo.description,
        "private": repo.private,
        "default_branch": repo.default_branch,
        "fork": False,
        "has_issues": True,
        "has_pull_requests": True,
        "created_at": TS,
        "updated_at": TS,
    }


def _label_obj(label):
    return {"id": label["id"], "name": label["name"], "color": label.get("color", "")}


def _issue_obj(store, repo, issue):
    return {
        "id": issue["id"],
        "number": issue["number"],
        "title": issue["title"],
        "body": issue["body"],
        "state": issue["state"],
        "labels": [_label_obj(repo.labels[name]) for name in issue["labels"]],
        "user": _owner_obj(store),
        "comments": len(repo.comments.get(issue["number"], [])),
        "created_at": TS,
        "updated_at": TS,
    }


def _tip_of(repo, branch):
    return repo.refs.get("refs/heads/" + branch)


def _pull_obj(store, repo, pr):
    head_sha = _tip_of(repo, pr["head_ref"]) or pr["head_sha"]
    base_sha = _tip_of(repo, pr["base_ref"]) or pr["base_sha"]
    return {
        "id": pr["id"],
        "number": pr["number"],
        "state": pr["state"],
        "title": pr["title"],
        "body": pr["body"],
        "head": {"ref": pr["head_ref"], "sha": head_sha},
        "base": {"ref": pr["base_ref"], "sha": base_sha},
        "merged": pr["merged"],
        "mergeable": pr["mergeable"],
        "merged_at": pr["merged_at"],
        "merge_commit_sha": pr["merge_commit_sha"],
        "user": _owner_obj(store),
        "created_at": TS,
        "updated_at": TS,
    }


def _page_params(req):
    try:
        per_page = int(req.query_one("per_page", "30"))
    except (TypeError, ValueError):
        per_page = 30
    per_page = max(1, min(100, per_page))
    try:
        page = int(req.query_one("page", "1"))
    except (TypeError, ValueError):
        page = 1
    page = max(1, page)
    return per_page, page


def _paginate(store, req, items):
    """Slice `items` to the requested page; add Link rel="next" when more remain."""
    per_page, page = _page_params(req)
    start = (page - 1) * per_page
    chunk = items[start:start + per_page]
    headers = None
    if start + len(chunk) < len(items):
        query = {}
        for key, vals in req.query.items():
            if key != "page" and vals:
                query[key] = vals[0]
        query["per_page"] = str(per_page)
        query["page"] = str(page + 1)
        url = req.base_url.rstrip("/") + req.path + "?" + urlencode(query)
        headers = {"Link": '<%s>; rel="next"' % url}
    return json_resp(store, 200, chunk, headers)


def _resolve_ref(repo, ref):
    """A branch name or a full commit sha -> commit oid, or None."""
    if not ref:
        return None
    tip = repo.refs.get("refs/heads/" + ref)
    if tip is not None:
        return tip
    if _SHA40.match(ref):
        oid = ref.lower()
        entry = repo.objects.get(oid)
        if entry is not None and entry[0] == "commit":
            return oid
    return None


def _history(repo, tip):
    """Newest-first commit list from `tip`: [{"sha", "commit": {"message"}}]."""
    out = []
    seen = set()
    stack = [tip]
    while stack:
        oid = stack.pop()
        if oid in seen:
            continue
        seen.add(oid)
        entry = repo.objects.get(oid)
        if entry is None or entry[0] != "commit":
            continue
        _headers, message = parse_commit(entry[1])
        out.append({
            "sha": oid,
            "commit": {"message": message.decode("utf-8", "surrogateescape").rstrip("\n")},
        })
        parents = commit_parents(entry[1])
        stack.extend(reversed(parents))
    return out


def _blob_bytes(repo, oid):
    entry = repo.objects.get(oid)
    if entry is None:
        return None
    return entry[1]


def _line_counts(old_bytes, new_bytes):
    old = old_bytes.decode("utf-8", "replace").splitlines() if old_bytes is not None else []
    new = new_bytes.decode("utf-8", "replace").splitlines() if new_bytes is not None else []
    matcher = difflib.SequenceMatcher(a=old, b=new, autojunk=False)
    additions = 0
    deletions = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("insert", "replace"):
            additions += j2 - j1
        if tag in ("delete", "replace"):
            deletions += i2 - i1
    return additions, deletions


def _tree_from_flat(repo, flat):
    """Rebuild a tree oid from {path: (mode, oid)} (recursive)."""
    if not flat:
        return build_tree(repo, [])
    dirs = {}
    entries = []
    for path, (mode, oid) in flat.items():
        head, sep, tail = path.partition("/")
        if sep:
            dirs.setdefault(head, {})[tail] = (mode, oid)
        else:
            entries.append((mode, head, oid))
    for name, sub in dirs.items():
        entries.append(("40000", name, _tree_from_flat(repo, sub)))
    return build_tree(repo, entries)


def _dir_tree_oid(repo, tip, path):
    """Tree oid of the directory `path` at `tip`, or None when absent."""
    entry = repo.objects.get(tip)
    if entry is None or entry[0] != "commit":
        return None
    tree_oid = commit_tree_oid(entry[1])
    if not path:
        return tree_oid
    for part in path.split("/"):
        if not part:
            continue
        node = repo.objects.get(tree_oid)
        if node is None or node[0] != "tree":
            return None
        found = None
        for mode, name, oid in parse_tree(node[1]):
            if name == part and mode == "40000":
                found = oid
                break
        if found is None:
            return None
        tree_oid = found
    return tree_oid


def _clean_path(path):
    parts = [p for p in path.split("/") if p not in ("", ".")]
    if ".." in parts:
        raise HttpError(422, "invalid path")
    return "/".join(parts)


def _decode_b64(content):
    if not isinstance(content, str):
        raise HttpError(422, "content must be base64")
    try:
        return base64.b64decode(re.sub(r"\s+", "", content), validate=True)
    except Exception:
        raise HttpError(422, "content is not valid base64")


def _number(seg):
    try:
        return int(seg)
    except (TypeError, ValueError):
        return None


def _require_repo(store, owner, name):
    repo = get_repo(store, owner, name)
    if repo is None:
        raise HttpError(404, "Not Found")
    return repo


def _label_names(body):
    raw = body.get("labels", [])
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise HttpError(422, "labels must be an array")
    names = []
    for item in raw:
        if isinstance(item, dict):
            item = item.get("name")
        if not isinstance(item, str) or not item:
            raise HttpError(422, "labels must be label names")
        names.append(item)
    return names


# --------------------------------------------------------------------------
# handlers: root / user

def _root(store, req):
    return json_resp(store, 200, {
        "current_user_url": req.base_url.rstrip("/") + "/api/v3/user",
        "repository_url": req.base_url.rstrip("/") + "/api/v3/repos/{owner}/{repo}",
    }, {"X-OAuth-Scopes": "repo, read:org"})


def _meta(store, req):
    return json_resp(store, 200, {"installed_version": "1.0.0"})


def _get_user(store, req):
    if store.owner_login is None:
        raise HttpError(404, "Not Found")
    return json_resp(store, 200, {"login": store.owner_login, "id": 1, "type": "User"})


def _create_repo(store, req):
    body = req.json_body()
    name = body.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > 100:
        raise HttpError(422, "name is required")
    description = body.get("description", "")
    if description is None:
        description = ""
    if not isinstance(description, str):
        description = str(description)
    private = bool(body.get("private", False))
    try:
        repo = new_repo(store, name, description, private)
    except ExistsError:
        raise HttpError(422, "already exists")
    return json_resp(store, 201, _repo_obj(store, repo))


def _list_user_repos(store, req):
    repos = [_repo_obj(store, repo) for repo in store.repos.values()]
    return _paginate(store, req, repos)


# --------------------------------------------------------------------------
# handlers: repos

def _get_repo(store, req, owner, name):
    return json_resp(store, 200, _repo_obj(store, _require_repo(store, owner, name)))


def _patch_repo(store, req, owner, name):
    repo = _require_repo(store, owner, name)
    body = req.json_body()
    if "description" in body:
        desc = body.get("description")
        repo.description = "" if desc is None else (desc if isinstance(desc, str) else str(desc))
    return json_resp(store, 200, _repo_obj(store, repo))


def _delete_repo(store, req, owner, name):
    try:
        delete_repo(store, owner, name)
    except Exception:
        raise HttpError(404, "Not Found")
    return json_resp(store, 204, None)


# --------------------------------------------------------------------------
# handlers: refs / branches / commits

def _create_ref(store, req, owner, name):
    repo = _require_repo(store, owner, name)
    body = req.json_body()
    ref = body.get("ref")
    sha = body.get("sha")
    if not isinstance(ref, str) or not ref.startswith("refs/heads/") or ref == "refs/heads/":
        raise HttpError(422, "ref must be refs/heads/<branch>")
    if not isinstance(sha, str) or not _SHA40.match(sha):
        raise HttpError(422, "unknown sha")
    sha = sha.lower()
    entry = repo.objects.get(sha)
    if entry is None or entry[0] != "commit":
        raise HttpError(422, "unknown sha")
    if ref in repo.refs:
        raise HttpError(422, "already exists")
    repo.refs[ref] = sha
    return json_resp(store, 201, {"ref": ref, "object": {"sha": sha}})


def _list_refs(store, req, owner, name):
    repo = _require_repo(store, owner, name)
    out = [{"ref": ref, "object": {"sha": sha}} for ref, sha in repo.refs.items()]
    return json_resp(store, 200, out)


def _get_ref(store, req, owner, name, branch):
    repo = _require_repo(store, owner, name)
    ref = "refs/heads/" + branch
    sha = repo.refs.get(ref)
    if sha is None:
        raise HttpError(404, "Not Found")
    return json_resp(store, 200, {"ref": ref, "object": {"sha": sha}})


def _list_branches(store, req, owner, name):
    repo = _require_repo(store, owner, name)
    out = [{"name": ref[len("refs/heads/"):], "commit": {"sha": sha}}
           for ref, sha in repo.refs.items() if ref.startswith("refs/heads/")]
    return json_resp(store, 200, out)


def _get_branch(store, req, owner, name, branch):
    repo = _require_repo(store, owner, name)
    sha = _tip_of(repo, branch)
    if sha is None:
        raise HttpError(404, "Not Found")
    return json_resp(store, 200, {"name": branch, "commit": {"sha": sha}})


def _list_commits(store, req, owner, name):
    repo = _require_repo(store, owner, name)
    ref = req.query_one("sha")
    if ref is None:
        tip = _tip_of(repo, repo.default_branch)
    else:
        tip = _resolve_ref(repo, ref)
    if tip is None:
        raise HttpError(404, "Not Found")
    return json_resp(store, 200, _history(repo, tip))


# --------------------------------------------------------------------------
# handlers: contents

def _put_contents(store, req, owner, name, path):
    repo = _require_repo(store, owner, name)
    body = req.json_body()
    message = body.get("message")
    if not isinstance(message, str) or not message:
        raise HttpError(422, "message is required")
    if "content" not in body or body.get("content") is None:
        raise HttpError(422, "content is required")
    data = _decode_b64(body.get("content"))
    branch = body.get("branch") or repo.default_branch
    if not isinstance(branch, str) or not branch:
        raise HttpError(422, "branch is required")
    tip = _tip_of(repo, branch)
    if tip is None and repo.refs:
        raise HttpError(404, "Not Found")
    current = read_path(repo, tip, path) if tip is not None else None
    current_sha = sha1_obj("blob", current) if current is not None else None
    if "sha" in body and body.get("sha") is not None:
        if body.get("sha") != current_sha:
            raise HttpError(409, "sha does not match the current blob")
    blob_sha = sha1_obj("blob", data)
    new_commit = write_path(repo, tip, path, data, message)
    repo.refs["refs/heads/" + branch] = new_commit
    status = 200 if current is not None else 201
    return json_resp(store, status, {
        "content": {
            "name": path.rsplit("/", 1)[-1],
            "path": path,
            "sha": blob_sha,
            "size": len(data),
            "encoding": "base64",
            "type": "file",
        },
        "commit": {"sha": new_commit, "message": message},
    })


def _get_contents(store, req, owner, name, path):
    repo = _require_repo(store, owner, name)
    ref = req.query_one("ref")
    if ref is None:
        tip = _tip_of(repo, repo.default_branch)
    else:
        tip = _resolve_ref(repo, ref)
    if tip is None:
        raise HttpError(404, "Not Found")
    found = read_tree_at(repo, tip, path)
    if found is None:
        raise HttpError(404, "Not Found")
    is_dir, entries = found
    if not is_dir:
        data = read_path(repo, tip, path)
        if data is None:
            raise HttpError(404, "Not Found")
        return json_resp(store, 200, {
            "type": "file",
            "encoding": "base64",
            "content": base64.b64encode(data).decode("ascii"),
            "sha": sha1_obj("blob", data),
            "size": len(data),
            "name": path.rsplit("/", 1)[-1],
            "path": path,
        })
    out = []
    for entry_name, mode, oid in entries:
        child_path = (path.rstrip("/") + "/" + entry_name) if path else entry_name
        if mode == "40000":
            child_oid = _dir_tree_oid(repo, tip, child_path)
            out.append({"type": "dir", "name": entry_name, "path": child_path,
                        "sha": child_oid or ""})
        else:
            out.append({"type": "file", "name": entry_name, "path": child_path,
                        "sha": oid or ""})
    return json_resp(store, 200, out)


def _delete_contents(store, req, owner, name, path):
    repo = _require_repo(store, owner, name)
    body = req.json_body()
    message = body.get("message") or ""
    if not isinstance(message, str):
        message = str(message)
    branch = body.get("branch") or repo.default_branch
    tip = _tip_of(repo, branch)
    if tip is None:
        raise HttpError(404, "Not Found")
    current = read_path(repo, tip, path)
    if current is None:
        raise HttpError(404, "Not Found")
    if "sha" in body and body.get("sha") is not None:
        if body.get("sha") != sha1_obj("blob", current):
            raise HttpError(409, "sha does not match the current blob")
    entry = repo.objects.get(tip)
    tree_oid = commit_tree_oid(entry[1]) if entry is not None else None
    flat = flatten_tree(repo, tree_oid) if tree_oid else {}
    if len(flat) == 1 and path in flat:
        new_tree = build_tree(repo, [])
        new_commit = make_commit(repo, new_tree, [tip], message)
    else:
        new_commit = delete_path(repo, tip, path, message)
    if new_commit is None:
        raise HttpError(404, "Not Found")
    repo.refs["refs/heads/" + branch] = new_commit
    return json_resp(store, 200, {"commit": {"sha": new_commit}})


# --------------------------------------------------------------------------
# handlers: labels / issues / comments

def _create_label(store, req, owner, name):
    repo = _require_repo(store, owner, name)
    body = req.json_body()
    label_name = body.get("name")
    if not isinstance(label_name, str) or not label_name:
        raise HttpError(422, "name is required")
    existing = repo.labels.get(label_name)
    if existing is not None:
        return json_resp(store, 201, _label_obj(existing))
    color = body.get("color", "")
    if color is None:
        color = ""
    if not isinstance(color, str):
        color = str(color)
    label = {"id": repo.label_seq, "name": label_name, "color": color}
    repo.label_seq += 1
    repo.labels[label_name] = label
    return json_resp(store, 201, _label_obj(label))


def _list_labels(store, req, owner, name):
    repo = _require_repo(store, owner, name)
    return json_resp(store, 200, [_label_obj(label) for label in repo.labels.values()])


def _create_issue(store, req, owner, name):
    repo = _require_repo(store, owner, name)
    body = req.json_body()
    title = body.get("title")
    if not isinstance(title, str) or not title:
        raise HttpError(422, "title is required")
    content = body.get("body", "")
    if content is None:
        content = ""
    if not isinstance(content, str):
        content = str(content)
    label_names = _label_names(body)
    for label_name in label_names:
        if label_name not in repo.labels:
            raise HttpError(422, "unknown label: %s" % label_name)
    number = repo.counter
    repo.counter += 1
    issue = {
        "id": store.alloc_id(),
        "number": number,
        "title": title,
        "body": content,
        "state": "open",
        "labels": list(label_names),
    }
    repo.issues[number] = issue
    return json_resp(store, 201, _issue_obj(store, repo, issue))


def _list_issues(store, req, owner, name):
    repo = _require_repo(store, owner, name)
    state = req.query_one("state", "open")
    direction = req.query_one("direction", "desc")
    issues = [repo.issues[n] for n in sorted(repo.issues)]
    if state == "open":
        issues = [i for i in issues if i["state"] == "open"]
    elif state == "closed":
        issues = [i for i in issues if i["state"] == "closed"]
    elif state == "all":
        pass
    else:
        raise HttpError(422, "invalid state")
    if direction == "desc":
        issues = list(reversed(issues))
    elif direction != "asc":
        raise HttpError(422, "invalid direction")
    return _paginate(store, req, [_issue_obj(store, repo, i) for i in issues])


def _get_issue(store, req, owner, name, number):
    repo = _require_repo(store, owner, name)
    issue = repo.issues.get(number)
    if issue is None:
        raise HttpError(404, "Not Found")
    return json_resp(store, 200, _issue_obj(store, repo, issue))


def _patch_issue(store, req, owner, name, number):
    repo = _require_repo(store, owner, name)
    issue = repo.issues.get(number)
    if issue is None:
        raise HttpError(404, "Not Found")
    body = req.json_body()
    if "state" in body:
        if body["state"] not in ("open", "closed"):
            raise HttpError(422, "invalid state")
        issue["state"] = body["state"]
    if "title" in body:
        if not isinstance(body["title"], str) or not body["title"]:
            raise HttpError(422, "title is required")
        issue["title"] = body["title"]
    if "body" in body:
        content = body["body"]
        issue["body"] = "" if content is None else (content if isinstance(content, str) else str(content))
    if "labels" in body:
        label_names = _label_names(body)
        for label_name in label_names:
            if label_name not in repo.labels:
                raise HttpError(422, "unknown label: %s" % label_name)
        issue["labels"] = label_names
    return json_resp(store, 200, _issue_obj(store, repo, issue))


def _create_comment(store, req, owner, name, number):
    repo = _require_repo(store, owner, name)
    if number not in repo.issues:
        raise HttpError(404, "Not Found")
    body = req.json_body()
    text = body.get("body")
    if not isinstance(text, str) or not text:
        raise HttpError(422, "body is required")
    comment = {"id": repo.comment_seq, "body": text}
    repo.comment_seq += 1
    repo.comments.setdefault(number, []).append(comment)
    return json_resp(store, 201, {"id": comment["id"], "body": comment["body"],
                                  "user": _owner_obj(store), "created_at": TS,
                                  "updated_at": TS})


def _list_comments(store, req, owner, name, number):
    repo = _require_repo(store, owner, name)
    if number not in repo.issues:
        raise HttpError(404, "Not Found")
    out = [{"id": c["id"], "body": c["body"]} for c in repo.comments.get(number, [])]
    return json_resp(store, 200, out)


# --------------------------------------------------------------------------
# handlers: pull requests

def _create_pull(store, req, owner, name):
    repo = _require_repo(store, owner, name)
    body = req.json_body()
    head = body.get("head")
    base = body.get("base")
    if not isinstance(head, str) or not head:
        raise HttpError(422, "head is required")
    if not isinstance(base, str) or not base:
        raise HttpError(422, "base is required")
    head_tip = _tip_of(repo, head)
    base_tip = _tip_of(repo, base)
    if head_tip is None or base_tip is None:
        raise HttpError(422, "unknown head or base branch")
    title = body.get("title", "")
    if title is None:
        title = ""
    if not isinstance(title, str):
        title = str(title)
    content = body.get("body", "")
    if content is None:
        content = ""
    if not isinstance(content, str):
        content = str(content)
    number = repo.counter
    repo.counter += 1
    pr = {
        "id": store.alloc_id(),
        "number": number,
        "title": title,
        "body": content,
        "state": "open",
        "head": head,
        "base": base,
        "head_ref": head,
        "base_ref": base,
        "head_sha": head_tip,
        "base_sha": base_tip,
        "merged": False,
        "mergeable": True,
        "merged_at": None,
        "merge_commit_sha": None,
    }
    repo.pulls[number] = pr
    return json_resp(store, 201, _pull_obj(store, repo, pr))


def _list_pulls(store, req, owner, name):
    repo = _require_repo(store, owner, name)
    state = req.query_one("state", "open")
    pulls = [repo.pulls[n] for n in sorted(repo.pulls)]
    if state == "open":
        pulls = [p for p in pulls if p["state"] == "open"]
    elif state == "closed":
        pulls = [p for p in pulls if p["state"] == "closed"]
    elif state == "all":
        pass
    else:
        raise HttpError(422, "invalid state")
    return _paginate(store, req, [_pull_obj(store, repo, p) for p in pulls])


def _get_pull(store, req, owner, name, number):
    repo = _require_repo(store, owner, name)
    pr = repo.pulls.get(number)
    if pr is None:
        raise HttpError(404, "Not Found")
    return json_resp(store, 200, _pull_obj(store, repo, pr))


def _pull_files(store, req, owner, name, number):
    repo = _require_repo(store, owner, name)
    pr = repo.pulls.get(number)
    if pr is None:
        raise HttpError(404, "Not Found")
    head_tip = _tip_of(repo, pr["head_ref"]) or pr["head_sha"]
    base_tip = _tip_of(repo, pr["base_ref"]) or pr["base_sha"]
    base_obj = repo.objects.get(base_tip)
    head_obj = repo.objects.get(head_tip)
    if base_obj is None or head_obj is None:
        raise HttpError(404, "Not Found")
    mb = merge_base(repo, head_tip, base_tip)
    mb_tree = None
    if mb is not None:
        mb_obj = repo.objects.get(mb)
        if mb_obj is not None:
            mb_tree = commit_tree_oid(mb_obj[1])
    head_tree = commit_tree_oid(head_obj[1])
    out = []
    for change in diff_trees(repo, mb_tree, head_tree):
        old_bytes = _blob_bytes(repo, change["old"][1]) if change["old"] else None
        new_bytes = _blob_bytes(repo, change["new"][1]) if change["new"] else None
        additions, deletions = _line_counts(old_bytes, new_bytes)
        out.append({
            "filename": change["path"],
            "status": change["status"],
            "additions": additions,
            "deletions": deletions,
            "changes": additions + deletions,
        })
    return json_resp(store, 200, out)


def _merge_pull(store, req, owner, name, number):
    repo = _require_repo(store, owner, name)
    pr = repo.pulls.get(number)
    if pr is None:
        raise HttpError(404, "Not Found")
    req.json_body()
    if pr["merged"]:
        raise HttpError(405, "already merged")
    head_tip = _tip_of(repo, pr["head_ref"])
    base_tip = _tip_of(repo, pr["base_ref"])
    if head_tip is None or base_tip is None:
        raise HttpError(404, "Not Found")
    head_obj = repo.objects.get(head_tip)
    base_obj = repo.objects.get(base_tip)
    if head_obj is None or base_obj is None:
        raise HttpError(404, "Not Found")
    mb = merge_base(repo, head_tip, base_tip)
    mb_tree = None
    if mb is not None:
        mb_obj = repo.objects.get(mb)
        if mb_obj is not None:
            mb_tree = commit_tree_oid(mb_obj[1])
    head_tree = commit_tree_oid(head_obj[1])
    base_tree = commit_tree_oid(base_obj[1])
    head_diff = diff_trees(repo, mb_tree, head_tree)
    base_diff = diff_trees(repo, mb_tree, base_tree)
    base_new = {d["path"]: (d["new"][1] if d["new"] else None) for d in base_diff}
    for change in head_diff:
        head_new = change["new"][1] if change["new"] else None
        if change["path"] in base_new and base_new[change["path"]] != head_new:
            raise HttpError(405, "merge conflict")
    base_flat = flatten_tree(repo, base_tree) if base_tree else {}
    head_flat = flatten_tree(repo, head_tree) if head_tree else {}
    for change in head_diff:
        if change["status"] == "removed":
            base_flat.pop(change["path"], None)
        else:
            base_flat[change["path"]] = head_flat[change["path"]]
    new_tree = _tree_from_flat(repo, base_flat)
    message = pr["title"] or ("Merge pull request #%d" % number)
    merge_sha = make_commit(repo, new_tree, [base_tip], message)
    repo.refs["refs/heads/" + pr["base_ref"]] = merge_sha
    pr["state"] = "closed"
    pr["merged"] = True
    pr["merged_at"] = TS
    pr["merge_commit_sha"] = merge_sha
    return json_resp(store, 200, {"merged": True, "sha": merge_sha, "message": message})


# --------------------------------------------------------------------------
# dispatch

def handle(store, req):
    """Route one request. None when the path is not under /api/v3."""
    path = req.path or "/"
    if path != "/api/v3" and not path.startswith("/api/v3/"):
        return None
    with store.lock:
        require_auth(store, req)
        return _dispatch(store, req)


def _dispatch(store, req):
    path = req.path.rstrip("/") or "/"
    segs = [s for s in path.split("/") if s != ""]
    method = req.method

    if segs == ["api", "v3"]:
        if method == "GET":
            return _root(store, req)
        raise HttpError(404, "Not Found")
    if len(segs) < 3 or segs[0] != "api" or segs[1] != "v3":
        raise HttpError(404, "Not Found")

    if segs[2] == "meta":
        if method == "GET" and len(segs) == 3:
            return _meta(store, req)
        raise HttpError(404, "Not Found")

    if segs[2] == "user":
        if len(segs) == 3 and method == "GET":
            return _get_user(store, req)
        if len(segs) == 4 and segs[3] == "repos":
            if method == "POST":
                return _create_repo(store, req)
            if method == "GET":
                return _list_user_repos(store, req)
        raise HttpError(404, "Not Found")

    if segs[2] != "repos" or len(segs) < 5:
        raise HttpError(404, "Not Found")
    owner, name = segs[3], segs[4]

    if len(segs) == 5:
        if method == "GET":
            return _get_repo(store, req, owner, name)
        if method == "PATCH":
            return _patch_repo(store, req, owner, name)
        if method == "DELETE":
            return _delete_repo(store, req, owner, name)
        raise HttpError(404, "Not Found")

    section = segs[5]

    if section == "git" and len(segs) >= 7 and segs[6] == "refs":
        if len(segs) == 7:
            if method == "POST":
                return _create_ref(store, req, owner, name)
            if method == "GET":
                return _list_refs(store, req, owner, name)
            raise HttpError(404, "Not Found")
        if len(segs) >= 9 and segs[7] == "heads" and method == "GET":
            return _get_ref(store, req, owner, name, "/".join(segs[8:]))
        raise HttpError(404, "Not Found")

    if section == "branches":
        if len(segs) == 6 and method == "GET":
            return _list_branches(store, req, owner, name)
        if len(segs) >= 7 and method == "GET":
            return _get_branch(store, req, owner, name, "/".join(segs[6:]))
        raise HttpError(404, "Not Found")

    if section == "commits":
        if len(segs) == 6 and method == "GET":
            return _list_commits(store, req, owner, name)
        raise HttpError(404, "Not Found")

    if section == "contents":
        content_path = _clean_path("/".join(segs[6:])) if len(segs) > 6 else ""
        if method == "PUT":
            if not content_path:
                raise HttpError(422, "path is required")
            return _put_contents(store, req, owner, name, content_path)
        if method == "GET":
            return _get_contents(store, req, owner, name, content_path)
        if method == "DELETE":
            if not content_path:
                raise HttpError(422, "path is required")
            return _delete_contents(store, req, owner, name, content_path)
        raise HttpError(404, "Not Found")

    if section == "labels":
        if len(segs) == 6 and method == "POST":
            return _create_label(store, req, owner, name)
        if len(segs) == 6 and method == "GET":
            return _list_labels(store, req, owner, name)
        raise HttpError(404, "Not Found")

    if section == "issues":
        if len(segs) == 6:
            if method == "POST":
                return _create_issue(store, req, owner, name)
            if method == "GET":
                return _list_issues(store, req, owner, name)
            raise HttpError(404, "Not Found")
        number = _number(segs[6])
        if number is None:
            raise HttpError(404, "Not Found")
        if len(segs) == 7:
            if method == "GET":
                return _get_issue(store, req, owner, name, number)
            if method == "PATCH":
                return _patch_issue(store, req, owner, name, number)
            raise HttpError(404, "Not Found")
        if len(segs) == 8 and segs[7] == "comments":
            if method == "POST":
                return _create_comment(store, req, owner, name, number)
            if method == "GET":
                return _list_comments(store, req, owner, name, number)
        raise HttpError(404, "Not Found")

    if section == "pulls":
        if len(segs) == 6:
            if method == "POST":
                return _create_pull(store, req, owner, name)
            if method == "GET":
                return _list_pulls(store, req, owner, name)
            raise HttpError(404, "Not Found")
        number = _number(segs[6])
        if number is None:
            raise HttpError(404, "Not Found")
        if len(segs) == 7 and method == "GET":
            return _get_pull(store, req, owner, name, number)
        if len(segs) == 8 and segs[7] == "files" and method == "GET":
            return _pull_files(store, req, owner, name, number)
        if len(segs) == 8 and segs[7] == "merge" and method == "PUT":
            return _merge_pull(store, req, owner, name, number)
        raise HttpError(404, "Not Found")

    raise HttpError(404, "Not Found")
