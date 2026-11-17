"""GraphQL v4 surface for the git forge (POST /api/graphql).

Stdlib only, no I/O, no wall clock, no randomness, no module-level mutable
state. A small parser/executor for exactly the documents the conformance
suite sends (see the kit's checks-graphql.ts / graphql-client.ts).

Store-internal dict shapes this module writes (REST/MCP read the same
store; these are the contract to align on):
  issue: {"id": store.alloc_id(), "number", "title", "body", "state": "open"|"closed",
          "labels": [], "created_seq": int, "created_at": TS, "updated_at": TS}
  pull:  {"id": store.alloc_id(), "number", "title", "body", "state": "open"|"closed",
          "head": <head branch name str>, "base": <base branch name str>,
          "head_ref"/"base_ref": same branch names, "head_sha"/"base_sha": tip oids,
          "merged": bool, "mergeable": True, "merge_commit_sha": oid|None,
          "merged_at": TS|None, "created_seq": int, "created_at": TS, "updated_at": TS}
  comment (repo.comments[issue_number]): {"id", "body", "user", "created_at": TS}
Numbers come from the shared per-repo counter (repo.counter).
"""

import base64
import json

from store import (
    TS,
    build_tree,
    commit_tree_oid,
    diff_trees,
    flatten_tree,
    get_repo,
    global_id,
    make_commit,
    merge_base,
    parse_global_id,
)
from web import HttpError, json_resp, require_auth


# --------------------------------------------------------------------------
# Schema used for validation (unknown-field detection) and execution.
# value: return type; "[X]" is a list of X, "Node" is the union.
# --------------------------------------------------------------------------

_SCHEMA = {
    "Query": {
        "viewer": "User",
        "repository": "Repository",
        "node": "Node",
        "__type": "__Type",
        "__schema": "__Schema",
    },
    "User": {"login": "String"},
    "Repository": {
        "id": "String",
        "databaseId": "Int",
        "name": "String",
        "nameWithOwner": "String",
        "owner": "User",
        "defaultBranchRef": "Ref",
        "viewerPermission": "String",
        "hasIssuesEnabled": "Boolean",
        "issue": "Issue",
        "issues": "IssueConnection",
        "pullRequests": "PullRequestConnection",
        "pullRequest": "PullRequest",
    },
    "Ref": {"name": "String"},
    "Issue": {
        "id": "String",
        "number": "Int",
        "title": "String",
        "state": "String",
        "body": "String",
        "createdAt": "String",
        "updatedAt": "String",
    },
    "IssueConnection": {"nodes": "[Issue]", "pageInfo": "PageInfo", "totalCount": "Int"},
    "PageInfo": {"hasNextPage": "Boolean", "endCursor": "String"},
    "PullRequest": {
        "id": "String",
        "number": "Int",
        "title": "String",
        "body": "String",
        "state": "String",
        "merged": "Boolean",
        "headRefName": "String",
        "baseRefName": "String",
        "mergeable": "String",
        "files": "FileConnection",
    },
    "PullRequestConnection": {"nodes": "[PullRequest]", "pageInfo": "PageInfo", "totalCount": "Int"},
    "FileConnection": {"nodes": "[PullRequestChangedFile]", "pageInfo": "PageInfo"},
    "PullRequestChangedFile": {"path": "String", "filename": "String"},
    "Node": {"id": "String"},
    "__Type": {
        "name": "String",
        "kind": "String",
        "fields": "[__Field]",
        "enumValues": "[__EnumValue]",
    },
    "__Schema": {"types": "[__Type]"},
    "__Field": {"name": "String"},
    "__EnumValue": {"name": "String"},
    "CreateIssuePayload": {"issue": "Issue"},
    "CloseIssuePayload": {"issue": "Issue"},
    "CreatePullRequestPayload": {"pullRequest": "PullRequest"},
    "MergePullRequestPayload": {"pullRequest": "PullRequest"},
    "AddCommentPayload": {"commentEdge": "IssueCommentEdge"},
    "IssueCommentEdge": {"node": "IssueComment"},
    "IssueComment": {"id": "String", "body": "String"},
    "Mutation": {
        "createIssue": "CreateIssuePayload",
        "closeIssue": "CloseIssuePayload",
        "createPullRequest": "CreatePullRequestPayload",
        "mergePullRequest": "MergePullRequestPayload",
        "addComment": "AddCommentPayload",
    },
}

# Introspection data: type name -> (fields, enum values).
_INTROSPECTION = {
    "Issue": (["id", "number", "title", "state", "body"], []),
    "PullRequest": (
        ["id", "number", "headRefName", "baseRefName", "mergeable", "state", "merged", "files"],
        [],
    ),
    "Repository": (
        ["id", "databaseId", "name", "nameWithOwner", "owner", "defaultBranchRef",
         "viewerPermission", "hasIssuesEnabled", "issue", "issues", "pullRequests",
         "pullRequest"],
        [],
    ),
    "SearchType": ([], ["ISSUE", "REPOSITORY", "USER", "CODE", "DISCUSSION"]),
    "User": (["id", "login"], []),
    "IssueState": ([], ["OPEN", "CLOSED"]),
    "PullRequestState": ([], ["OPEN", "CLOSED", "MERGED"]),
    "MergeableState": ([], ["MERGEABLE", "CONFLICTING", "UNKNOWN"]),
    "IssueOrderField": ([], ["CREATED_AT", "UPDATED_AT", "COMMENTS"]),
    "OrderDirection": ([], ["ASC", "DESC"]),
}


class _GQLError(Exception):
    """An execution error: becomes one entry of the envelope's errors."""


# --------------------------------------------------------------------------
# Tokenizer / parser for the small GraphQL subset the suite sends.
# --------------------------------------------------------------------------

class _Token:
    __slots__ = ("kind", "value")

    def __init__(self, kind, value):
        self.kind = kind
        self.value = value


def _tokenize(text):
    tokens = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n,":
            i += 1
        elif c == "#":
            while i < n and text[i] != "\n":
                i += 1
        elif text.startswith("...", i):
            tokens.append(_Token("spread", "..."))
            i += 3
        elif c in "{}()[]:!=$@&|":
            tokens.append(_Token("punct", c))
            i += 1
        elif c == '"':
            j = i + 1
            out = []
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n:
                    esc = text[j + 1]
                    mapping = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "/": "/"}
                    if esc == "u" and j + 5 < n + 1 and j + 6 <= n:
                        try:
                            out.append(chr(int(text[j + 2:j + 6], 16)))
                        except ValueError:
                            out.append("?")
                        j += 6
                    elif esc in mapping:
                        out.append(mapping[esc])
                        j += 2
                    else:
                        out.append(esc)
                        j += 2
                else:
                    out.append(text[j])
                    j += 1
            tokens.append(_Token("string", "".join(out)))
            i = j + 1
        elif c.isdigit() or (c == "-" and i + 1 < n and text[i + 1].isdigit()):
            j = i + 1
            is_float = False
            while j < n and (text[j].isdigit() or text[j] in ".eE+-"):
                if text[j] in ".eE":
                    is_float = True
                j += 1
            raw = text[i:j]
            try:
                tokens.append(_Token("float" if is_float else "int",
                                      float(raw) if is_float else int(raw)))
            except ValueError:
                tokens.append(_Token("int", 0))
            i = j
        elif c.isalpha() or c == "_":
            j = i + 1
            while j < n and (text[j].isalnum() or text[j] == "_"):
                j += 1
            tokens.append(_Token("name", text[i:j]))
            i = j
        else:
            i += 1
    tokens.append(_Token("eof", None))
    return tokens


class _Field:
    __slots__ = ("alias", "name", "args", "selection", "fragment_type")

    def __init__(self, alias, name, args, selection, fragment_type=None):
        self.alias = alias
        self.name = name
        self.args = args
        self.selection = selection
        self.fragment_type = fragment_type


class _Parser:
    def __init__(self, text):
        self.tokens = _tokenize(text)
        self.pos = 0

    def peek(self):
        return self.tokens[self.pos]

    def next(self):
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def expect_punct(self, value):
        tok = self.next()
        if tok.kind != "punct" or tok.value != value:
            raise _GQLError("Expected '%s' in GraphQL document" % value)

    def expect_name(self, value=None):
        tok = self.next()
        if tok.kind != "name" or (value is not None and tok.value != value):
            raise _GQLError("Expected name in GraphQL document")
        return tok.value

    def parse_document(self):
        operations = []
        fragments = {}
        while self.peek().kind != "eof":
            tok = self.peek()
            if tok.kind == "name" and tok.value == "fragment":
                self.next()
                name = self.expect_name()
                self.expect_name("on")
                ftype = self.expect_name()
                fragments[name] = (ftype, self.parse_selection_set())
            else:
                operations.append(self.parse_operation(fragments))
        if not operations:
            raise _GQLError("Empty GraphQL document")
        return operations, fragments

    def parse_operation(self, fragments):
        op_type = "query"
        tok = self.peek()
        if tok.kind == "name" and tok.value in ("query", "mutation", "subscription"):
            op_type = self.next().value
            if self.peek().kind == "name":
                self.next()  # operation name (ignored; never sent with operationName)
        elif tok.kind == "punct" and tok.value == "{":
            pass
        else:
            raise _GQLError("Expected an operation in GraphQL document")
        if self.peek().kind == "punct" and self.peek().value == "(":
            self._skip_parens()
        return (op_type, self.parse_selection_set())

    def _skip_parens(self):
        depth = 0
        while True:
            tok = self.next()
            if tok.kind == "eof":
                return
            if tok.kind == "punct" and tok.value == "(":
                depth += 1
            elif tok.kind == "punct" and tok.value == ")":
                depth -= 1
                if depth == 0:
                    return

    def parse_selection_set(self):
        self.expect_punct("{")
        fields = []
        while True:
            tok = self.peek()
            if tok.kind == "punct" and tok.value == "}":
                self.next()
                return fields
            if tok.kind == "eof":
                raise _GQLError("Unterminated selection set")
            fields.append(self.parse_field())

    def parse_field(self):
        tok = self.peek()
        if tok.kind == "spread":
            self.next()
            nxt = self.peek()
            if nxt.kind == "name" and nxt.value == "on":
                self.next()
                ftype = self.expect_name()
                return _Field(None, None, {}, self.parse_selection_set(), fragment_type=ftype)
            # Named fragment spread: "...Name" (not sent by the suite).
            name = self.expect_name()
            return _Field(None, None, {}, [], fragment_type="__fragment__:" + name)
        name = self.expect_name()
        alias = None
        if self.peek().kind == "punct" and self.peek().value == ":":
            self.next()
            alias = name
            name = self.expect_name()
        args = {}
        if self.peek().kind == "punct" and self.peek().value == "(":
            args = self.parse_arguments()
        selection = []
        if self.peek().kind == "punct" and self.peek().value == "{":
            selection = self.parse_selection_set()
        return _Field(alias, name, args, selection)

    def parse_arguments(self):
        self.expect_punct("(")
        args = {}
        while True:
            tok = self.peek()
            if tok.kind == "punct" and tok.value == ")":
                self.next()
                return args
            name = self.expect_name()
            self.expect_punct(":")
            args[name] = self.parse_value()

    def parse_value(self):
        tok = self.next()
        if tok.kind == "punct" and tok.value == "$":
            return ("$var", self.expect_name())
        if tok.kind == "string":
            return tok.value
        if tok.kind in ("int", "float"):
            return tok.value
        if tok.kind == "punct" and tok.value == "[":
            items = []
            while not (self.peek().kind == "punct" and self.peek().value == "]"):
                items.append(self.parse_value())
            self.next()
            return items
        if tok.kind == "punct" and tok.value == "{":
            obj = {}
            while not (self.peek().kind == "punct" and self.peek().value == "}"):
                key = self.expect_name()
                self.expect_punct(":")
                obj[key] = self.parse_value()
            self.next()
            return obj
        if tok.kind == "name":
            if tok.value == "true":
                return True
            if tok.value == "false":
                return False
            if tok.value == "null":
                return None
            return tok.value  # bare enum name -> its string
        raise _GQLError("Unexpected value in GraphQL document")


def _resolve_arg_value(value, variables):
    """Substitute $variables (recursively) in a parsed argument value."""
    if isinstance(value, tuple) and len(value) == 2 and value[0] == "$var":
        return variables.get(value[1])
    if isinstance(value, list):
        return [_resolve_arg_value(v, variables) for v in value]
    if isinstance(value, dict):
        return {k: _resolve_arg_value(v, variables) for k, v in value.items()}
    return value


def _resolve_args(field, variables):
    return {k: _resolve_arg_value(v, variables) for k, v in field.args.items()}


# --------------------------------------------------------------------------
# Cursors, ids, and small store helpers.
# --------------------------------------------------------------------------

def _encode_cursor(number):
    raw = ("cursor:%s" % number).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(cursor):
    if not isinstance(cursor, str) or not cursor:
        return None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except Exception:
        return None
    if raw.startswith("cursor:"):
        try:
            return int(raw[len("cursor:"):])
        except ValueError:
            return None
    try:
        return int(raw)
    except ValueError:
        return None


def _repo_by_dbid(store, dbid):
    try:
        dbid = int(dbid)
    except (TypeError, ValueError):
        return None
    for repo in store.repos.values():
        if repo.id == dbid:
            return repo
    return None


def _ref_name(value):
    """Branch name from a stored head/base: plain str, or a {"ref": ...} dict."""
    if isinstance(value, dict):
        return value.get("ref")
    return value


def _pr_branch(pr, key):
    """Branch name for a PR's head/base, tolerating shapes that only
    carry head_ref/base_ref (e.g. PRs created over REST before the
    shared shape was aligned)."""
    name = _ref_name(pr.get(key))
    if name:
        return name
    return pr.get(key + "_ref")


def _ref_sha(repo, branch):
    if branch is None:
        return None
    return repo.refs.get("refs/heads/" + branch)


def _issue_tuple(repo, issue):
    return ("Issue", (repo, issue))


def _pr_tuple(repo, pr):
    return ("PullRequest", (repo, pr))


def _new_issue(store, repo, title, body):
    number = repo.counter
    repo.counter += 1
    repo.created_seq += 1
    issue = {
        "id": store.alloc_id(),
        "number": number,
        "title": title,
        "body": body if body is not None else "",
        "state": "open",
        "labels": [],
        "created_seq": repo.created_seq,
        "created_at": TS,
        "updated_at": TS,
    }
    repo.issues[number] = issue
    return issue


def _new_pull(store, repo, title, body, head, base):
    number = repo.counter
    repo.counter += 1
    repo.created_seq += 1
    pr = {
        "id": store.alloc_id(),
        "number": number,
        "title": title,
        "body": body if body is not None else "",
        "state": "open",
        "head": head,
        "base": base,
        "head_ref": head,
        "base_ref": base,
        "head_sha": _ref_sha(repo, head),
        "base_sha": _ref_sha(repo, base),
        "merged": False,
        "mergeable": True,
        "merge_commit_sha": None,
        "merged_at": None,
        "created_seq": repo.created_seq,
        "created_at": TS,
        "updated_at": TS,
    }
    repo.pulls[number] = pr
    return pr


def _tree_from_flat(repo, flat):
    """Rebuild a tree oid from {path: (mode, oid)} (recursive)."""
    grouped = {}
    for path, entry in flat.items():
        first, _, rest = path.partition("/")
        grouped.setdefault(first, []).append((rest, entry))
    entries = []
    for name, items in grouped.items():
        if len(items) == 1 and items[0][0] == "":
            entries.append((items[0][1][0], name, items[0][1][1]))
        else:
            sub = {rest: entry for rest, entry in items}
            entries.append(("40000", name, _tree_from_flat(repo, sub)))
    return build_tree(repo, entries)


def _squash_merge(store, repo, pr, merge_method):
    """Apply the head's changes since merge-base onto the base tip.

    Returns the new commit oid. Raises _GQLError on a conflict.
    """
    head = _pr_branch(pr, "head")
    base = _pr_branch(pr, "base")
    base_tip = _ref_sha(repo, base)
    head_tip = _ref_sha(repo, head)
    if base_tip is None or head_tip is None:
        raise _GQLError("Cannot merge: missing base or head branch")
    base_entry = repo.objects.get(base_tip)
    head_entry = repo.objects.get(head_tip)
    if base_entry is None or head_entry is None:
        raise _GQLError("Cannot merge: missing base or head commit")
    base_tree = commit_tree_oid(base_entry[1])
    head_tree = commit_tree_oid(head_entry[1])
    mb = merge_base(repo, base_tip, head_tip)
    mb_tree = None
    if mb is not None:
        mb_entry = repo.objects.get(mb)
        if mb_entry is not None:
            mb_tree = commit_tree_oid(mb_entry[1])
    base_changes = {d["path"]: d for d in diff_trees(repo, mb_tree, base_tree)}
    head_changes = diff_trees(repo, mb_tree, head_tree)
    for change in head_changes:
        path = change["path"]
        other = base_changes.get(path)
        if other is not None and other.get("new") != change.get("new"):
            raise _GQLError("Merge conflict: %s changed on both branches" % path)
    new_flat = dict(flatten_tree(repo, base_tree)) if base_tree else {}
    for change in head_changes:
        if change["new"] is None:
            new_flat.pop(change["path"], None)
        else:
            new_flat[change["path"]] = change["new"]
    new_tree = _tree_from_flat(repo, new_flat)
    title = pr.get("title") or ""
    message = "Merge pull request #%d from %s\n\n%s" % (pr["number"], head, title)
    if merge_method == "MERGE":
        parents = [base_tip, head_tip]
    else:  # SQUASH (and REBASE): a single-parent commit on the base tip
        parents = [base_tip]
    commit = make_commit(repo, new_tree, parents, message)
    repo.refs["refs/heads/" + base] = commit
    pr["merged"] = True
    pr["state"] = "closed"
    pr["merged_at"] = TS
    pr["merge_commit_sha"] = commit
    pr["updated_at"] = TS
    return commit


def _pr_files(repo, pr):
    """Changed paths of head vs merge-base, as PullRequestChangedFile tuples."""
    head = _pr_branch(pr, "head")
    base = _pr_branch(pr, "base")
    base_tip = _ref_sha(repo, base)
    head_tip = _ref_sha(repo, head)
    if base_tip is None or head_tip is None:
        return []
    head_entry = repo.objects.get(head_tip)
    if head_entry is None:
        return []
    head_tree = commit_tree_oid(head_entry[1])
    mb = merge_base(repo, base_tip, head_tip)
    mb_tree = None
    if mb is not None:
        mb_entry = repo.objects.get(mb)
        if mb_entry is not None:
            mb_tree = commit_tree_oid(mb_entry[1])
    paths = [d["path"] for d in diff_trees(repo, mb_tree, head_tree)]
    return [("PullRequestChangedFile", {"path": p}) for p in paths]


def _pr_mergeable(repo, pr):
    head = _pr_branch(pr, "head")
    base = _pr_branch(pr, "base")
    if _ref_sha(repo, head) is None or _ref_sha(repo, base) is None:
        return "UNKNOWN"
    try:
        base_tip = _ref_sha(repo, base)
        head_tip = _ref_sha(repo, head)
        base_entry = repo.objects.get(base_tip)
        head_entry = repo.objects.get(head_tip)
        if base_entry is None or head_entry is None:
            return "UNKNOWN"
        base_tree = commit_tree_oid(base_entry[1])
        head_tree = commit_tree_oid(head_entry[1])
        mb = merge_base(repo, base_tip, head_tip)
        mb_tree = None
        if mb is not None:
            mb_entry = repo.objects.get(mb)
            if mb_entry is not None:
                mb_tree = commit_tree_oid(mb_entry[1])
        base_changes = {d["path"]: d for d in diff_trees(repo, mb_tree, base_tree)}
        for change in diff_trees(repo, mb_tree, head_tree):
            other = base_changes.get(change["path"])
            if other is not None and other.get("new") != change.get("new"):
                return "CONFLICTING"
        return "MERGEABLE"
    except Exception:
        return "UNKNOWN"


# --------------------------------------------------------------------------
# Validation: unknown fields become errors naming the field.
# --------------------------------------------------------------------------

def _elem_type(return_type):
    if return_type.startswith("[") and return_type.endswith("]"):
        return return_type[1:-1]
    return return_type


def _validate_selection(selection, typename, errors):
    fields = _SCHEMA.get(typename)
    if fields is None:
        return
    for field in selection:
        if field.fragment_type is not None:
            if field.fragment_type.startswith("__fragment__:"):
                continue
            _validate_selection(field.selection, field.fragment_type, errors)
            continue
        name = field.name
        if name == "__typename":
            continue
        if name not in fields:
            errors.append('Cannot query field "%s" on type "%s".' % (name, typename))
            continue
        if field.selection:
            _validate_selection(field.selection, _elem_type(fields[name]), errors)


# --------------------------------------------------------------------------
# Execution.
# --------------------------------------------------------------------------

class _Ctx:
    __slots__ = ("store", "variables", "errors")

    def __init__(self, store, variables):
        self.store = store
        self.variables = variables
        self.errors = []


def _convert(value, return_type, selection, ctx):
    """Turn a resolver result into JSON, executing sub-selections."""
    if value is None:
        return None
    if isinstance(value, list):
        elem = _elem_type(return_type)
        return [_convert(v, elem, selection, ctx) for v in value]
    if isinstance(value, tuple):
        typename, data = value
        if not selection:
            return None
        return _execute_selection(typename, data, selection, ctx)
    return value


def _execute_selection(typename, data, selection, ctx):
    out = {}
    for field in selection:
        if field.fragment_type is not None:
            if field.fragment_type.startswith("__fragment__:"):
                continue
            # Inline fragment: include only when it matches this node type.
            # Node is a union: the concrete type is the tuple's typename.
            if field.fragment_type != typename:
                continue
            out.update(_execute_selection(typename, data, field.selection, ctx))
            continue
        key = field.alias if field.alias else field.name
        if field.name == "__typename":
            out[key] = typename
            continue
        args = _resolve_args(field, ctx.variables)
        try:
            value, return_type = _resolve_field(ctx, typename, data, field.name, args)
        except _GQLError as exc:
            ctx.errors.append({"message": str(exc)})
            out[key] = None
            continue
        out[key] = _convert(value, return_type, field.selection, ctx)
    return out


def _issues_connection(repo, args):
    """Filtered/sorted/paged IssueConnection data for a repository."""
    issues = list(repo.issues.values())
    states = args.get("states")
    if states:
        wanted = set()
        for state in states:
            if isinstance(state, str):
                wanted.add(state.upper())
        issues = [i for i in issues if i.get("state", "open").upper() in wanted]
    order_by = args.get("orderBy") or {}
    direction = str(order_by.get("direction", "ASC")).upper()
    field = str(order_by.get("field", "CREATED_AT")).upper()
    if field in ("CREATED_AT", "UPDATED_AT", "COMMENTS"):
        key = lambda i: (i.get("created_seq", 0), i["number"])
    else:
        key = lambda i: (i.get("created_seq", 0), i["number"])
    issues.sort(key=key, reverse=(direction == "DESC"))
    # Cursor pagination over the (already ordered) list, stable by number.
    after = _decode_cursor(args.get("after"))
    start = 0
    if after is not None:
        for idx, issue in enumerate(issues):
            if issue["number"] == after:
                start = idx + 1
                break
        else:
            # Cursor not in this list: fall back to numbers greater than it
            # when ascending, smaller when descending.
            if direction == "DESC":
                start = next((idx for idx, i in enumerate(issues) if i["number"] < after), len(issues))
            else:
                start = next((idx for idx, i in enumerate(issues) if i["number"] > after), len(issues))
    first = args.get("first")
    if isinstance(first, int) and first >= 0:
        page = issues[start:start + first]
        has_next = start + first < len(issues)
    else:
        page = issues[start:]
        has_next = False
    end_cursor = _encode_cursor(page[-1]["number"]) if page else None
    return {
        "nodes": [_issue_tuple(repo, i) for i in page],
        "pageInfo": {"hasNextPage": has_next, "endCursor": end_cursor},
        "totalCount": len(issues),
    }


def _resolve_field(ctx, typename, data, name, args):
    """Return (value, return_type). value is a scalar, None, an entity
    tuple (typename, data), or a list of those."""
    store = ctx.store
    ret = _SCHEMA[typename][name]

    if typename == "Query":
        if name == "viewer":
            if store.owner_login is None:
                raise _GQLError("viewer is not bound: no repository has been created yet")
            return ("User", {"login": store.owner_login}), ret
        if name == "repository":
            repo = get_repo(store, args.get("owner"), args.get("name"))
            if repo is None:
                return None, ret
            return ("Repository", repo), ret
        if name == "node":
            parsed = parse_global_id(args.get("id"))
            if parsed is None:
                return None, ret
            kind, parts = parsed
            if kind == "Repository" and parts:
                repo = _repo_by_dbid(store, parts[0])
                return (("Repository", repo), ret) if repo is not None else (None, ret)
            if kind == "Issue" and len(parts) >= 2:
                repo = _repo_by_dbid(store, parts[0])
                issue = repo.issues.get(int(parts[1])) if repo is not None else None
                return (_issue_tuple(repo, issue), ret) if issue is not None else (None, ret)
            if kind == "PullRequest" and len(parts) >= 2:
                repo = _repo_by_dbid(store, parts[0])
                pr = repo.pulls.get(int(parts[1])) if repo is not None else None
                return (_pr_tuple(repo, pr), ret) if pr is not None else (None, ret)
            if kind == "IssueComment" and len(parts) >= 3:
                repo = _repo_by_dbid(store, parts[0])
                comments = repo.comments.get(int(parts[1]), []) if repo is not None else []
                comment = next((c for c in comments if c.get("id") == int(parts[2])), None)
                if comment is not None:
                    return ("IssueComment", (repo, int(parts[1]), comment)), ret
            return None, ret
        if name == "__type":
            tname = args.get("name")
            if tname in _INTROSPECTION:
                return ("__Type", tname), ret
            return None, ret
        if name == "__schema":
            return ("__Schema", None), ret

    if typename == "User":
        return data.get("login"), ret

    if typename == "Repository":
        repo = data
        if name == "id":
            return global_id("Repository", repo.id), ret
        if name == "databaseId":
            return repo.id, ret
        if name == "name":
            return repo.name, ret
        if name == "nameWithOwner":
            owner = store.owner_login or ""
            return "%s/%s" % (owner, repo.name), ret
        if name == "owner":
            return ("User", {"login": store.owner_login}), ret
        if name == "defaultBranchRef":
            return ("Ref", {"name": repo.default_branch}), ret
        if name == "viewerPermission":
            return "ADMIN", ret
        if name == "hasIssuesEnabled":
            return True, ret
        if name == "issue":
            issue = repo.issues.get(args.get("number"))
            return (_issue_tuple(repo, issue), ret) if issue is not None else (None, ret)
        if name == "issues":
            return ("IssueConnection", _issues_connection(repo, args)), ret
        if name == "pullRequests":
            pulls = sorted(repo.pulls.values(), key=lambda p: p["number"])
            first = args.get("first")
            if isinstance(first, int) and first >= 0:
                pulls = pulls[:first]
            return ("PullRequestConnection", {
                "nodes": [_pr_tuple(repo, p) for p in pulls],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "totalCount": len(repo.pulls),
            }), ret
        if name == "pullRequest":
            pr = repo.pulls.get(args.get("number"))
            return (_pr_tuple(repo, pr), ret) if pr is not None else (None, ret)

    if typename == "Ref":
        return data.get("name"), ret

    if typename == "Issue":
        repo, issue = data
        if name == "id":
            return global_id("Issue", repo.id, issue["number"]), ret
        if name == "state":
            return issue.get("state", "open").upper(), ret
        return issue.get(name), ret

    if typename == "IssueConnection":
        if name == "nodes":
            return data["nodes"], ret
        if name == "pageInfo":
            return ("PageInfo", data["pageInfo"]), ret
        if name == "totalCount":
            return data["totalCount"], ret

    if typename == "PageInfo":
        return data.get(name), ret

    if typename == "PullRequest":
        repo, pr = data
        if name == "id":
            return global_id("PullRequest", repo.id, pr["number"]), ret
        if name == "state":
            state = pr.get("state", "open")
            if pr.get("merged"):
                return "MERGED", ret
            return state.upper(), ret
        if name == "merged":
            return bool(pr.get("merged")), ret
        if name == "headRefName":
            return _pr_branch(pr, "head"), ret
        if name == "baseRefName":
            return _pr_branch(pr, "base"), ret
        if name == "mergeable":
            return _pr_mergeable(repo, pr), ret
        if name == "files":
            files = _pr_files(repo, pr)
            first = args.get("first")
            if isinstance(first, int) and first >= 0:
                files = files[:first]
            return ("FileConnection", {
                "nodes": files,
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }), ret
        return pr.get(name), ret

    if typename == "PullRequestConnection":
        if name == "nodes":
            return data["nodes"], ret
        if name == "pageInfo":
            return ("PageInfo", data["pageInfo"]), ret
        if name == "totalCount":
            return data["totalCount"], ret

    if typename == "FileConnection":
        if name == "nodes":
            return data["nodes"], ret
        if name == "pageInfo":
            return ("PageInfo", data["pageInfo"]), ret

    if typename == "PullRequestChangedFile":
        if name == "filename":
            return data.get("path"), ret
        return data.get(name), ret

    if typename == "__Type":
        tname = data
        fields, enums = _INTROSPECTION.get(tname, ([], []))
        if name == "name":
            return tname, ret
        if name == "kind":
            return "ENUM" if enums else "OBJECT", ret
        if name == "fields":
            return [("__Field", {"name": f}) for f in fields], ret
        if name == "enumValues":
            return [("__EnumValue", {"name": e}) for e in enums], ret

    if typename == "__Schema":
        if name == "types":
            return [("__Type", t) for t in _INTROSPECTION], ret

    if typename in ("__Field", "__EnumValue"):
        return data.get("name"), ret

    if typename == "IssueComment":
        _repo, _number, comment = data
        if name == "id":
            return global_id("IssueComment", _repo.id, _number, comment.get("id")), ret
        return comment.get(name), ret

    if typename == "IssueCommentEdge":
        return data["node"], ret

    if typename in ("CreateIssuePayload", "CloseIssuePayload"):
        return data["issue"], ret
    if typename == "AddCommentPayload":
        return data["commentEdge"], ret
    if typename in ("CreatePullRequestPayload", "MergePullRequestPayload"):
        return data["pullRequest"], ret

    if typename == "Mutation":
        return _resolve_mutation(ctx, name, args)

    raise _GQLError('Cannot query field "%s" on type "%s".' % (name, typename))


def _resolve_mutation(ctx, name, args):
    store = ctx.store
    inp = args.get("input") or {}
    if not isinstance(inp, dict):
        raise _GQLError("Mutation input must be an object")

    if name == "createIssue":
        parsed = parse_global_id(inp.get("repositoryId"))
        if parsed is None or parsed[0] != "Repository":
            raise _GQLError("createIssue: invalid repositoryId")
        repo = _repo_by_dbid(store, parsed[1][0])
        if repo is None:
            raise _GQLError("createIssue: repository not found")
        issue = _new_issue(store, repo, inp.get("title") or "", inp.get("body"))
        return ("CreateIssuePayload", {"issue": _issue_tuple(repo, issue)}), "CreateIssuePayload"

    if name == "closeIssue":
        parsed = parse_global_id(inp.get("issueId"))
        if parsed is None or parsed[0] != "Issue":
            raise _GQLError("closeIssue: invalid issueId")
        repo = _repo_by_dbid(store, parsed[1][0])
        issue = repo.issues.get(int(parsed[1][1])) if repo is not None and len(parsed[1]) >= 2 else None
        if issue is None:
            raise _GQLError("closeIssue: issue not found")
        issue["state"] = "closed"
        issue["updated_at"] = TS
        return ("CloseIssuePayload", {"issue": _issue_tuple(repo, issue)}), "CloseIssuePayload"

    if name == "createPullRequest":
        parsed = parse_global_id(inp.get("repositoryId"))
        if parsed is None or parsed[0] != "Repository":
            raise _GQLError("createPullRequest: invalid repositoryId")
        repo = _repo_by_dbid(store, parsed[1][0])
        if repo is None:
            raise _GQLError("createPullRequest: repository not found")
        head = inp.get("headRefName")
        base = inp.get("baseRefName")
        if _ref_sha(repo, head) is None:
            raise _GQLError("createPullRequest: head branch not found")
        if _ref_sha(repo, base) is None:
            raise _GQLError("createPullRequest: base branch not found")
        pr = _new_pull(store, repo, inp.get("title") or "", inp.get("body"), head, base)
        return ("CreatePullRequestPayload", {"pullRequest": _pr_tuple(repo, pr)}), "CreatePullRequestPayload"

    if name == "mergePullRequest":
        parsed = parse_global_id(inp.get("pullRequestId"))
        if parsed is None or parsed[0] != "PullRequest":
            raise _GQLError("mergePullRequest: invalid pullRequestId")
        repo = _repo_by_dbid(store, parsed[1][0])
        pr = repo.pulls.get(int(parsed[1][1])) if repo is not None and len(parsed[1]) >= 2 else None
        if pr is None:
            raise _GQLError("mergePullRequest: pull request not found")
        if pr.get("merged"):
            return ("MergePullRequestPayload", {"pullRequest": _pr_tuple(repo, pr)}), "MergePullRequestPayload"
        _squash_merge(store, repo, pr, str(inp.get("mergeMethod") or "SQUASH").upper())
        return ("MergePullRequestPayload", {"pullRequest": _pr_tuple(repo, pr)}), "MergePullRequestPayload"

    if name == "addComment":
        parsed = parse_global_id(inp.get("subjectId"))
        if parsed is None or parsed[0] not in ("Issue", "PullRequest"):
            raise _GQLError("addComment: invalid subjectId")
        repo = _repo_by_dbid(store, parsed[1][0])
        number = int(parsed[1][1]) if repo is not None and len(parsed[1]) >= 2 else None
        target = None
        if repo is not None and number is not None:
            target = repo.issues.get(number) or repo.pulls.get(number)
        if target is None:
            raise _GQLError("addComment: subject not found")
        cid = repo.comment_seq
        repo.comment_seq += 1
        comment = {
            "id": cid,
            "body": inp.get("body") or "",
            "user": store.owner_login,
            "created_at": TS,
        }
        repo.comments.setdefault(number, []).append(comment)
        node = ("IssueComment", (repo, number, comment))
        return ("AddCommentPayload", {"commentEdge": ("IssueCommentEdge", {"node": node})}), "AddCommentPayload"

    raise _GQLError('Cannot query field "%s" on type "Mutation".' % name)


# --------------------------------------------------------------------------
# Entry point.
# --------------------------------------------------------------------------

def _execute_operation(store, operation):
    """Run one {query, variables} operation -> {"data": ..., "errors": [...]}."""
    query = operation.get("query")
    variables = operation.get("variables") or {}
    if not isinstance(query, str) or not query.strip():
        return {"data": None, "errors": [{"message": "GraphQL query must be a non-empty string"}]}
    if not isinstance(variables, dict):
        variables = {}
    try:
        operations, _fragments = _Parser(query).parse_document()
    except _GQLError as exc:
        return {"data": None, "errors": [{"message": str(exc)}]}
    # The suite sends one operation per document; run the first.
    op_type, selection = operations[0]
    ctx = _Ctx(store, variables)
    root_type = "Mutation" if op_type == "mutation" else "Query"
    errors = []
    _validate_selection(selection, root_type, errors)
    if errors:
        # Partial data: the queried top-level fields as null.
        data = {}
        for field in selection:
            if field.fragment_type is None:
                data[field.alias if field.alias else field.name] = None
        return {"data": data, "errors": [{"message": m} for m in errors]}
    try:
        data = _execute_selection(root_type, None, selection, ctx)
    except _GQLError as exc:
        ctx.errors.append({"message": str(exc)})
        data = {}
    envelope = {"data": data}
    if ctx.errors:
        envelope["errors"] = ctx.errors
    return envelope


def handle(store, req):
    """POST /api/graphql handler. Returns a Resp (200 with the envelope,
    or a JSON array of envelopes for a batched request), or None when the
    route is not this surface's (main.py tries each surface in turn)."""
    if req.path != "/api/graphql":
        return None
    if req.method != "POST":
        raise HttpError(405, "Method Not Allowed")
    require_auth(store, req)
    try:
        payload = json.loads(req.body.decode("utf-8")) if req.body else {}
    except Exception:
        raise HttpError(400, "Invalid JSON body")
    if isinstance(payload, list):
        results = []
        for entry in payload:
            if not isinstance(entry, dict):
                results.append({"data": None, "errors": [{"message": "Batched operation must be an object"}]})
            else:
                results.append(_execute_operation(store, entry))
        return json_resp(store, 200, results)
    if not isinstance(payload, dict):
        raise HttpError(400, "JSON body must be an object")
    return json_resp(store, 200, _execute_operation(store, payload))
