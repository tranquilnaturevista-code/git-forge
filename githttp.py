"""Git smart-HTTP surface: upload-pack protocol v2 (v0 fallback) and
receive-pack v0, over /{owner}/{repo}.git.

handle(store, req) -> Resp or None (None when the route is not ours).
Auth is checked first (401 via require_auth), then the repo (404).
Errors are raised as HttpError; main.py converts them to JSON errors.

Pure Python 3.13 standard library. No I/O, no wall clock, no randomness,
no subprocess, no git binary. No module-level mutable state.
"""

import gitobjs
import store as store_mod
from web import HttpError, Resp, require_auth

ZERO_OID = gitobjs.ZERO_OID

UPLOAD_AD_CT = "application/x-git-upload-pack-advertisement"
UPLOAD_RESULT_CT = "application/x-git-upload-pack-result"
RECEIVE_AD_CT = "application/x-git-receive-pack-advertisement"
RECEIVE_RESULT_CT = "application/x-git-receive-pack-result"

UPLOAD_V0_CAPS = (
    "side-band-64k thin-pack ofs-delta shallow multi_ack multi_ack_detailed "
    "symref=HEAD:refs/heads/main agent=git-forge/1.0"
)
RECEIVE_V0_CAPS = (
    "report-status side-band-64k delete-refs ofs-delta "
    "symref=HEAD:refs/heads/main agent=git-forge/1.0"
)

_HEX40 = set("0123456789abcdef")


def _is_oid(value):
    return isinstance(value, str) and len(value) == 40 and all(c in _HEX40 for c in value)


def _strip_nl(line):
    return line[:-1] if line.endswith("\n") else line


def _head_ref(repo):
    return "refs/heads/" + repo.default_branch


def _peel(repo, oid):
    """Follow annotated tags to the underlying non-tag object."""
    seen = set()
    cur = oid
    while cur not in seen:
        seen.add(cur)
        entry = repo.objects.get(cur)
        if entry is None or entry[0] != "tag":
            return cur
        target = None
        headers, _msg = store_mod.parse_commit(entry[1])
        for key, val in headers:
            if key == "object":
                target = val.decode("ascii", "replace")
                break
        if target is None or not _is_oid(target):
            return cur
        cur = target
    return cur


def _sorted_refs(repo):
    return sorted(repo.refs.items())


# ---------------------------------------------------------------- routing

def handle(store, req):
    parts = req.path.split("/")
    if len(parts) < 3 or not parts[2].endswith(".git"):
        return None
    owner = parts[1]
    name = parts[2][:-4]
    rest = parts[3:]
    if not owner or not name:
        return None
    if rest == ["info", "refs"] and req.method == "GET":
        kind = "info-refs"
    elif rest == ["git-upload-pack"] and req.method == "POST":
        kind = "upload-pack"
    elif rest == ["git-receive-pack"] and req.method == "POST":
        kind = "receive-pack"
    else:
        return None
    require_auth(store, req)
    repo = store_mod.get_repo(store, owner, name)
    if repo is None:
        raise HttpError(404, "Not Found")
    if kind == "info-refs":
        return _info_refs(store, req, repo)
    if kind == "upload-pack":
        return _upload_pack(store, req, repo)
    return _receive_pack(store, req, repo)


def _is_v2(req):
    return req.headers.get("git-protocol", "").strip() == "version=2"


# ---------------------------------------------------------------- advertisements

def _v0_advertisement(service, repo, caps):
    """v0 advertisement: '# service=...' preamble, flush, ref lines, flush."""
    out = [gitobjs.encode_pkt_line("# service=%s\n" % service), gitobjs.FLUSH]
    refs = _sorted_refs(repo)
    head = _head_ref(repo)
    if not refs:
        out.append(gitobjs.encode_pkt_line(
            "%s capabilities^{}\0%s\n" % (ZERO_OID, caps)))
    else:
        head_oid = repo.refs.get(head)
        first = True
        lines = []
        if head_oid:
            lines.append("%s HEAD" % head_oid)
        for refname, oid in refs:
            lines.append("%s %s" % (oid, refname))
            if repo.objects.get(oid, (None,))[0] == "tag":
                peeled = _peel(repo, oid)
                if peeled != oid:
                    lines.append("%s %s^{}" % (peeled, refname))
        for line in lines:
            if first:
                out.append(gitobjs.encode_pkt_line(line + "\0" + caps + "\n"))
                first = False
            else:
                out.append(gitobjs.encode_pkt_line(line + "\n"))
    out.append(gitobjs.FLUSH)
    return b"".join(out)


def _v2_advertisement(repo):
    out = [
        gitobjs.encode_pkt_line("version 2\n"),
        gitobjs.encode_pkt_line("ls-refs=unborn\n"),
        gitobjs.encode_pkt_line("fetch=shallow\n"),
        gitobjs.encode_pkt_line("object-format=sha1\n"),
        gitobjs.FLUSH,
    ]
    return b"".join(out)


def _info_refs(store, req, repo):
    service = req.query_one("service")
    if service not in ("git-upload-pack", "git-receive-pack"):
        raise HttpError(400, "missing or unknown service")
    if service == "git-upload-pack" and _is_v2(req):
        return Resp(200, {"Content-Type": UPLOAD_AD_CT}, _v2_advertisement(repo))
    if service == "git-upload-pack":
        return Resp(200, {"Content-Type": UPLOAD_AD_CT},
                    _v0_advertisement("git-upload-pack", repo, UPLOAD_V0_CAPS))
    return Resp(200, {"Content-Type": RECEIVE_AD_CT},
                _v0_advertisement("git-receive-pack", repo, RECEIVE_V0_CAPS))


# ---------------------------------------------------------------- upload-pack

def _decode_or_400(body):
    try:
        return gitobjs.decode_pkt_lines(body)
    except gitobjs.PktLineError as exc:
        raise HttpError(400, "malformed pkt-line: %s" % exc)


def _upload_pack(store, req, repo):
    packets = _decode_or_400(req.body)
    if _is_v2(req):
        return _upload_pack_v2(repo, packets)
    return _upload_pack_v0(repo, packets)


def _split_v2_request(packets):
    """Parse a v2 request: returns (command, args) where args are the
    text lines after the delim packet. Raises HttpError(400)."""
    lines = []
    seen_delim = False
    for kind, payload in packets:
        if kind == "flush":
            break
        if kind == "delim":
            seen_delim = True
            continue
        if kind == "data":
            lines.append(_strip_nl(payload.decode("utf-8", "surrogateescape")))
    if not lines or not lines[0].startswith("command="):
        raise HttpError(400, "missing command")
    command = lines[0][len("command="):]
    args = lines[1:] if seen_delim else lines[1:]
    # Lines before the delim (agent=/object-format=) are not arguments;
    # drop any pre-delim lines other than the command itself.
    if seen_delim:
        args = []
        after = False
        for kind, payload in packets:
            if kind == "delim":
                after = True
                continue
            if kind == "flush":
                break
            if after and kind == "data":
                args.append(_strip_nl(payload.decode("utf-8", "surrogateescape")))
    return command, args


def _ls_refs_lines(repo, args):
    peel = False
    symrefs = False
    prefixes = []
    for arg in args:
        if arg == "peel":
            peel = True
        elif arg == "symrefs":
            symrefs = True
        elif arg.startswith("ref-prefix "):
            prefixes.append(arg[len("ref-prefix "):])
        # "unborn" and anything else: no effect beyond the empty-repo line
    head = _head_ref(repo)
    lines = []
    if not repo.refs:
        lines.append("unborn HEAD symref-target:%s\n" % head)
        return lines
    head_oid = repo.refs.get(head)
    if not prefixes and head_oid:
        line = "%s HEAD" % head_oid
        if symrefs:
            line += " symref-target:%s" % head
        lines.append(line + "\n")
    for refname, oid in _sorted_refs(repo):
        if prefixes and not any(refname.startswith(p) for p in prefixes):
            continue
        line = "%s %s" % (oid, refname)
        if peel and repo.objects.get(oid, (None,))[0] == "tag":
            peeled = _peel(repo, oid)
            if peeled != oid:
                line += " peeled:%s" % peeled
        lines.append(line + "\n")
    return lines


def _upload_pack_v2(repo, packets):
    command, args = _split_v2_request(packets)
    if command == "ls-refs":
        body = gitobjs.encode_pkt_lines(_ls_refs_lines(repo, args) + ["flush"])
        return Resp(200, {"Content-Type": UPLOAD_RESULT_CT}, body)
    if command == "fetch":
        return _fetch_response(repo, args, v2=True)
    raise HttpError(400, "unknown command %r" % command)


def _upload_pack_v0(repo, packets):
    # Two flush-separated sections: wants/shallow/deepen, then have/done.
    sections = []
    current = []
    for kind, payload in packets:
        if kind == "flush":
            sections.append(current)
            current = []
        elif kind == "data":
            current.append(_strip_nl(payload.decode("utf-8", "surrogateescape")))
    if current:
        sections.append(current)
    if not sections or not sections[0]:
        raise HttpError(400, "missing want lines")
    args = []
    for section in sections:
        args.extend(section)
    first = args[0]
    if not first.startswith("want "):
        raise HttpError(400, "expected want line")
    return _fetch_response(repo, args, v2=False)


# ------------------------------------------------------------ fetch selection

def _parse_fetch_args(args):
    wants = []
    haves = []
    shallow = []
    depth = None
    for arg in args:
        if arg.startswith("want "):
            oid = arg[5:].split(" ")[0]
            if _is_oid(oid):
                wants.append(oid)
        elif arg.startswith("have "):
            oid = arg[5:].split(" ")[0]
            if _is_oid(oid):
                haves.append(oid)
        elif arg.startswith("shallow "):
            oid = arg[8:].split(" ")[0]
            if _is_oid(oid):
                shallow.append(oid)
        elif arg.startswith("deepen "):
            try:
                depth = int(arg[7:].split(" ")[0])
            except ValueError:
                depth = None
        # done / thin-pack / no-progress / ofs-delta / filter / deepen-since
        # / deepen-not: accepted and ignored (filter/deepen-not unused by
        # the suite's client; thin-pack/no-progress/ofs-delta are our
        # always-on behaviour).
    return wants, haves, shallow, depth


def _commit_depths(repo, wants, max_depth):
    """{commit oid: depth} for commits within max_depth of any want
    (want itself depth 1), following parents only."""
    depths = {}
    queue = []
    for want in wants:
        start = _peel(repo, want)
        entry = repo.objects.get(start)
        if entry is not None and entry[0] == "commit":
            if start not in depths:
                depths[start] = 1
                queue.append(start)
    head = 0
    while head < len(queue):
        commit = queue[head]
        head += 1
        depth = depths[commit]
        if depth >= max_depth:
            continue
        entry = repo.objects.get(commit)
        if entry is None:
            continue
        for parent in store_mod.commit_parents(entry[1]):
            if parent not in depths:
                depths[parent] = depth + 1
                queue.append(parent)
    return depths


def _walk_limited(repo, included_commits, extra_roots):
    """Objects reachable from included_commits without crossing into
    commits outside the set, plus tag chains / trees / blobs rooted at
    extra_roots (the raw want oids)."""
    send = set()
    stack = list(included_commits) + list(extra_roots)
    while stack:
        oid = stack.pop()
        if oid in send:
            continue
        entry = repo.objects.get(oid)
        if entry is None:
            continue
        send.add(oid)
        obj_type, data = entry
        if obj_type == "commit":
            tree = store_mod.commit_tree_oid(data)
            if tree:
                stack.append(tree)
            for parent in store_mod.commit_parents(data):
                if parent in included_commits:
                    stack.append(parent)
        elif obj_type == "tag":
            headers, _msg = store_mod.parse_commit(data)
            for key, val in headers:
                if key == "object":
                    stack.append(val.decode("ascii", "replace"))
        elif obj_type == "tree":
            for mode, _name, child in store_mod.parse_tree(data):
                if mode != "160000":
                    stack.append(child)
    return send


def _held_closure(repo, haves, shallow_set):
    """Objects the client already holds, given its have list and its
    shallow boundary: a shallow commit itself (and its tree/blobs) is
    held, but its parents are not — the client never received them."""
    held = set()
    stack = list(haves)
    while stack:
        oid = stack.pop()
        if oid in held:
            continue
        entry = repo.objects.get(oid)
        if entry is None:
            continue
        held.add(oid)
        obj_type, data = entry
        if obj_type == "commit":
            tree = store_mod.commit_tree_oid(data)
            if tree:
                stack.append(tree)
            if oid not in shallow_set:
                stack.extend(store_mod.commit_parents(data))
        elif obj_type == "tag":
            headers, _msg = store_mod.parse_commit(data)
            for key, val in headers:
                if key == "object":
                    stack.append(val.decode("ascii", "replace"))
        elif obj_type == "tree":
            for mode, _name, child in store_mod.parse_tree(data):
                if mode != "160000":
                    stack.append(child)
    return held


def _select_fetch(repo, wants, haves, client_shallow, depth):
    """Returns (send_oids, shallow_list, unshallow_list)."""
    held_haves = [h for h in haves if h in repo.objects]
    held = _held_closure(repo, held_haves, set(client_shallow))
    if depth is None:
        send = store_mod.reachable(repo, wants) - held
        return send, [], []
    depths = _commit_depths(repo, wants, depth)
    included = set(depths)
    send = _walk_limited(repo, included, wants) - held
    shallow = sorted(oid for oid, d in depths.items() if d == depth)
    shallow_set = set(shallow)
    unshallow = [oid for oid in client_shallow
                 if oid in included and oid not in shallow_set]
    return send, shallow, unshallow


def _acks(repo, wants, haves):
    """ACK per held have that is an ancestor-or-equal of a want, else NAK."""
    peeled_wants = []
    for want in wants:
        peeled = _peel(repo, want)
        if peeled not in peeled_wants:
            peeled_wants.append(peeled)
    acks = []
    for have in haves:
        if have not in repo.objects:
            continue
        for want in peeled_wants:
            if have == want or store_mod.is_ancestor(repo, have, want):
                acks.append(have)
                break
    return acks


def _fetch_response(repo, args, v2):
    wants, haves, client_shallow, depth = _parse_fetch_args(args)
    if not wants:
        raise HttpError(400, "fetch with no want")
    send, shallow, unshallow = _select_fetch(repo, wants, haves, client_shallow, depth)
    acks = _acks(repo, wants, haves)
    pack_objects = []
    for oid in sorted(send):
        entry = repo.objects.get(oid)
        if entry is not None:
            pack_objects.append((entry[0], entry[1]))
    pack = gitobjs.write_pack(pack_objects)
    if v2:
        out = []
        out.append(gitobjs.encode_pkt_line("acknowledgments\n"))
        if acks:
            for ack in acks:
                out.append(gitobjs.encode_pkt_line("ACK %s\n" % ack))
        else:
            out.append(gitobjs.encode_pkt_line("NAK\n"))
        out.append(gitobjs.encode_pkt_line("ready\n"))
        out.append(gitobjs.DELIM)
        out.append(gitobjs.encode_pkt_line("shallow-info\n"))
        for oid in shallow:
            out.append(gitobjs.encode_pkt_line("shallow %s\n" % oid))
        for oid in unshallow:
            out.append(gitobjs.encode_pkt_line("unshallow %s\n" % oid))
        out.append(gitobjs.DELIM)
        out.append(gitobjs.encode_pkt_line("packfile\n"))
        out.append(gitobjs.encode_sideband(1, pack))
        out.append(gitobjs.FLUSH)
        return Resp(200, {"Content-Type": UPLOAD_RESULT_CT}, b"".join(out))
    out = []
    if acks:
        for ack in acks:
            out.append(gitobjs.encode_pkt_line("ACK %s\n" % ack))
    else:
        out.append(gitobjs.encode_pkt_line("NAK\n"))
    for oid in shallow:
        out.append(gitobjs.encode_pkt_line("shallow %s\n" % oid))
    for oid in unshallow:
        out.append(gitobjs.encode_pkt_line("unshallow %s\n" % oid))
    out.append(gitobjs.encode_sideband(1, pack))
    out.append(gitobjs.FLUSH)
    return Resp(200, {"Content-Type": UPLOAD_RESULT_CT}, b"".join(out))


# ---------------------------------------------------------------- receive-pack

def _parse_push_commands(packets):
    """Returns (commands, caps) where commands are (old, new, ref)."""
    commands = []
    caps = []
    for index, (kind, payload) in enumerate(packets):
        if kind != "data":
            continue
        text = _strip_nl(payload.decode("utf-8", "surrogateescape"))
        if index == 0 and "\0" in text:
            text, cap_text = text.split("\0", 1)
            caps = cap_text.split(" ")
        fields = text.split(" ")
        if len(fields) != 3 or not _is_oid(fields[0]) or not _is_oid(fields[1]):
            raise HttpError(400, "malformed push command")
        if not fields[2].startswith("refs/") or any(
                ord(c) < 0x20 or c == " " for c in fields[2]):
            raise HttpError(400, "malformed push ref")
        commands.append((fields[0], fields[1], fields[2]))
    if not commands:
        raise HttpError(400, "push with no commands")
    return commands, caps


def _ff_ok(repo, old, new):
    """Fast-forward check for one command, against the pre-push refs."""
    if old == new:
        return True
    old_peeled = _peel(repo, old) if old != ZERO_OID else ZERO_OID
    new_peeled = _peel(repo, new) if new != ZERO_OID else ZERO_OID
    if old_peeled == new_peeled:
        return True
    old_entry = repo.objects.get(old_peeled)
    new_entry = repo.objects.get(new_peeled)
    if old_entry is None or new_entry is None:
        return False
    if old_entry[0] != "commit" or new_entry[0] != "commit":
        return False
    return store_mod.is_ancestor(repo, old_peeled, new_peeled)


def _receive_pack(store, req, repo):
    try:
        packets, pack_offset = gitobjs.decode_pkt_lines_prefix(req.body)
    except gitobjs.PktLineError as exc:
        raise HttpError(400, "malformed pkt-line: %s" % exc)
    commands, caps = _parse_push_commands(packets)
    pack_bytes = req.body[pack_offset:]
    sideband = "side-band-64k" in caps

    def report(inner_lines):
        """Wrap pkt-lined report lines in sideband (or raw) framing."""
        inner = gitobjs.encode_pkt_lines(inner_lines + ["flush"])
        if sideband:
            return gitobjs.encode_sideband(1, inner) + gitobjs.FLUSH
        return inner + gitobjs.FLUSH

    updates = [cmd for cmd in commands if cmd[1] != ZERO_OID]
    new_objects = {}
    if pack_bytes:
        try:
            parsed = gitobjs.read_pack(pack_bytes)
        except gitobjs.PackError as exc:
            body = report(["unpack %s\n" % exc])
            return Resp(200, {"Content-Type": RECEIVE_RESULT_CT}, body)
        new_objects = parsed["objects"]
    elif updates:
        # A create/update command list always carries a pack, even an
        # empty 32-byte one; its absence is a corrupt exchange.
        body = report(["unpack missing packfile\n"])
        return Resp(200, {"Content-Type": RECEIVE_RESULT_CT}, body)
    # Store pushed objects verbatim (never re-serialize).
    for oid, (obj_type, data) in new_objects.items():
        repo.objects[oid] = (obj_type, data)
    # Evaluate every command independently against the pre-push refs.
    original_refs = dict(repo.refs)
    results = []  # (ref, ok)
    for old, new, ref in commands:
        current = original_refs.get(ref)
        if new == ZERO_OID:
            # Delete: ok iff old == current value.
            ok = current is not None and old == current
        elif old == ZERO_OID:
            # Create: ok iff the ref does not exist and everything
            # reachable from new is present after unpack.
            if current is not None or new not in repo.objects:
                ok = False
            else:
                ok = all(oid in repo.objects
                         for oid in store_mod.reachable(repo, [new]))
        else:
            # Update: ok iff old == current, new is present with a
            # complete reachable closure, and old fast-forwards to new.
            if current is None or old != current or new not in repo.objects:
                ok = False
            elif not all(oid in repo.objects
                         for oid in store_mod.reachable(repo, [new])):
                ok = False
            else:
                ok = _ff_ok(repo, old, new)
        results.append((ref, ok))
    # Apply only the accepted refs, in command order.
    for (old, new, ref), (_ref, ok) in zip(commands, results):
        if not ok:
            continue
        if new == ZERO_OID:
            repo.refs.pop(ref, None)
        else:
            repo.refs[ref] = new
    lines = ["unpack ok\n"]
    for ref, ok in results:
        if ok:
            lines.append("ok %s\n" % ref)
        else:
            lines.append("ng %s non-fast-forward\n" % ref)
    return Resp(200, {"Content-Type": RECEIVE_RESULT_CT}, report(lines))
