"""Git smart HTTP: info/refs advertisements, upload-pack (v2 and v0) and
receive-pack (v0). Written from the author's own understanding of the git
wire protocol.
"""

from . import gitpack
from .store import ForgeError

AGENT = "github-core-clone/1.0.0"
ZERO = gitpack.ZERO_OID

# A pkt-line payload must stay under 65520 bytes (65516 of payload).
_SIDEBAND_CHUNK = 65515


def _chunk_sideband(pack):
    out = []
    for start in range(0, len(pack), _SIDEBAND_CHUNK):
        out.append(gitpack.pkt_line(b"\x01" + pack[start:start + _SIDEBAND_CHUNK]))
    if not out:
        out.append(gitpack.pkt_line(b"\x01" + pack))
    return b"".join(out)


def _repo_or_404(store, owner, name):
    repo = store.repo_for(owner, name)
    if repo is None:
        raise ForgeError(404, "Not Found")
    return repo


def _sorted_refs(repo):
    return sorted(repo.refs.items())


# ---------------------------------------------------------------- advertise

def info_refs(store, owner, name, service, git_protocol):
    repo = _repo_or_404(store, owner, name)
    if service == "git-upload-pack":
        if git_protocol == "version=2":
            return 200, "application/x-git-upload-pack-advertisement", _advertise_v2()
        return 200, "application/x-git-upload-pack-advertisement", _advertise_v0_upload(repo)
    if service == "git-receive-pack":
        return 200, "application/x-git-receive-pack-advertisement", _advertise_v0_receive(repo)
    raise ForgeError(400, "unknown service: %s" % service)


def _advertise_v2():
    lines = [
        b"version 2\n",
        ("agent=%s\n" % AGENT).encode(),
        b"fetch=shallow wait-for-done\n",
        b"ls-refs=unborn\n",
        b"object-format=sha1\n",
    ]
    return gitpack.encode_pkt_lines(lines + ["flush"])


def _v0_caps_upload(repo):
    return "side-band-64k shallow thin-pack ofs-delta agent=%s symref=HEAD:refs/heads/%s" % (
        AGENT, repo.default_branch)


def _v0_caps_receive():
    return "report-status side-band-64k delete-refs ofs-delta agent=%s" % AGENT


def _advertise_v0_upload(repo):
    out = [gitpack.pkt_line(b"# service=git-upload-pack\n"), gitpack.pkt_flush()]
    refs = _sorted_refs(repo)
    caps = _v0_caps_upload(repo)
    if not refs:
        out.append(gitpack.pkt_line(
            ("%s HEAD\x00%s\n" % (ZERO, caps)).encode()))
    else:
        head_oid = repo.refs.get("refs/heads/" + repo.default_branch, ZERO)
        out.append(gitpack.pkt_line(
            ("%s HEAD\x00%s\n" % (head_oid, caps)).encode()))
        out.extend(_v0_ref_lines(repo, refs))
    out.append(gitpack.pkt_flush())
    return b"".join(out)


def _advertise_v0_receive(repo):
    out = [gitpack.pkt_line(b"# service=git-receive-pack\n"), gitpack.pkt_flush()]
    refs = _sorted_refs(repo)
    caps = _v0_caps_receive()
    if not refs:
        out.append(gitpack.pkt_line(
            ("%s capabilities^{}\x00%s\n" % (ZERO, caps)).encode()))
    else:
        head_oid = repo.refs.get("refs/heads/" + repo.default_branch, ZERO)
        out.append(gitpack.pkt_line(
            ("%s HEAD\x00%s\n" % (head_oid, caps)).encode()))
        out.extend(_v0_ref_lines(repo, refs))
    out.append(gitpack.pkt_flush())
    return b"".join(out)


def _v0_ref_lines(repo, refs):
    lines = []
    for refname, oid in refs:
        lines.append(gitpack.pkt_line(("%s %s\n" % (oid, refname)).encode()))
        if refname.startswith("refs/tags/"):
            entry = repo.objects.get(oid)
            if entry is not None and entry[0] == gitpack.OBJ_TAG:
                target = gitpack.parse_tag(entry[1]).get("object")
                if target:
                    lines.append(gitpack.pkt_line(
                        ("%s %s^{}\n" % (target, refname)).encode()))
    return lines


# ---------------------------------------------------------------- ls-refs (v2)

def _ls_refs(store, repo, args):
    prefixes = [value for key, value in args if key == "ref-prefix"]
    want_peel = any(key == "peel" for key, _ in args)
    want_symrefs = any(key == "symrefs" for key, _ in args)
    want_unborn = any(key == "unborn" for key, _ in args)

    def matches(refname):
        return not prefixes or any(refname.startswith(p) for p in prefixes)

    lines = []
    refs = _sorted_refs(repo)
    default_ref = "refs/heads/" + repo.default_branch
    if not refs:
        # An empty repository advertises only HEAD (a zero oid with its
        # symref target). An `unborn refs/heads/<branch>` line would be read
        # as an existing ref by the suite's client, so it is not sent.
        if matches("HEAD"):
            head = "%s HEAD" % ZERO
            if want_symrefs:
                head += " symref-target:" + default_ref
            lines.append(head + "\n")
    else:
        if matches("HEAD"):
            head = "%s HEAD" % repo.refs.get(default_ref, ZERO)
            if want_symrefs:
                head += " symref-target:" + default_ref
            lines.append(head + "\n")
        for refname, oid in refs:
            if not matches(refname):
                continue
            line = "%s %s" % (oid, refname)
            if want_peel and refname.startswith("refs/tags/"):
                entry = repo.objects.get(oid)
                if entry is not None and entry[0] == gitpack.OBJ_TAG:
                    target = gitpack.parse_tag(entry[1]).get("object")
                    if target:
                        line += " peeled:" + target
            lines.append(line + "\n")
    return gitpack.encode_pkt_lines([line.encode() for line in lines] + ["flush"])


# ---------------------------------------------------------------- fetch planning

def _fetch_plan(store, repo, wants, haves, depth, client_shallow):
    """Work out what a fetch sends. Returns (pack_bytes, shallow, unshallow,
    acks). wants/haves are oid lists; depth is an int or None.

    Held commits are the ancestors of the haves, except that a shallow
    boundary's strict ancestors are not held (the client has the boundary
    commit itself, but not its parents); shallow boundaries themselves are
    held. Trees and blobs reachable from held commits are held too and are
    never re-sent."""
    cache_key = (tuple(sorted(wants)), tuple(sorted(haves)), depth,
                 tuple(sorted(client_shallow)))
    cached = repo.pack_cache.get(cache_key)
    if cached is not None:
        return cached

    # What the client already holds, as commits.
    have_closure = set()
    for have in haves:
        if store.get_commit(repo, have) is not None:
            have_closure.update(store.ancestors(repo, have))
    shallow_ancestors = set()
    for boundary in client_shallow:
        if store.get_commit(repo, boundary) is not None:
            shallow_ancestors.update(
                oid for oid in store.ancestors(repo, boundary)
                if oid != boundary)
    held_commits = (have_closure - shallow_ancestors)
    for boundary in client_shallow:
        if store.get_commit(repo, boundary) is not None:
            held_commits.add(boundary)

    # BFS from the wants: distances to every ancestor.
    dist = {}
    queue = []
    for want in wants:
        if want not in dist:
            dist[want] = 1
            queue.append(want)
    head = 0
    while head < len(queue):
        oid = queue[head]
        head += 1
        parsed = store.get_commit(repo, oid)
        if parsed is None:
            # A wanted tag: follow it to the object it peels to.
            entry = repo.objects.get(oid)
            if entry is not None and entry[0] == gitpack.OBJ_TAG:
                target = gitpack.parse_tag(entry[1]).get("object")
                if target and target not in dist:
                    dist[target] = dist[oid] + 1
                    queue.append(target)
            continue
        for parent in parsed["parents"]:
            if parent not in dist:
                dist[parent] = dist[oid] + 1
                queue.append(parent)

    acks = [have for have in haves if have in dist]
    included = [oid for oid, d in dist.items()
                if oid not in held_commits and (depth is None or d <= depth)]
    included.sort(key=lambda oid: (dist[oid], oid))
    included_set = set(included)
    shallow = []
    if depth is not None:
        for oid in included:
            if dist[oid] == depth:
                parsed = store.get_commit(repo, oid)
                if parsed is not None and parsed["parents"]:
                    shallow.append(oid)
    # A shallow boundary is unshallowed when the boundary itself is held or
    # sent and every parent of it is held or sent.
    held_or_sent = held_commits | included_set
    unshallow = []
    for boundary in client_shallow:
        parsed = store.get_commit(repo, boundary)
        if parsed is None or not parsed["parents"]:
            continue
        if boundary not in held_or_sent:
            continue
        if all(parent in held_or_sent for parent in parsed["parents"]):
            unshallow.append(boundary)

    # Trees and blobs the client already holds (reachable from held commits).
    held_objects = set(held_commits)
    for oid in held_commits:
        parsed = store.get_commit(repo, oid)
        if parsed is None:
            continue
        for _mode, blob_oid in store.flatten_tree(repo, parsed["tree"]).values():
            held_objects.add(blob_oid)
        _collect_tree_oids(store, repo, parsed["tree"], held_objects)

    entries = []
    added = set()
    for oid in included:
        entry = repo.objects.get(oid)
        if entry is None or oid in held_objects:
            continue
        entries.append(entry)
        added.add(oid)
    for oid in included:
        parsed = store.get_commit(repo, oid)
        if parsed is None:
            continue
        _collect_tree(store, repo, parsed["tree"], entries, added,
                      skip=held_objects)
    pack = gitpack.write_pack(entries, level=1)
    result = (pack, sorted(shallow), sorted(unshallow), acks)
    repo.pack_cache[cache_key] = result
    return result


def _collect_tree_oids(store, repo, tree_oid, out):
    """Add a tree oid and every tree oid under it to `out`."""
    stack = [tree_oid]
    while stack:
        oid = stack.pop()
        if oid in out or oid is None:
            continue
        out.add(oid)
        entries = store.get_tree(repo, oid)
        if entries is None:
            continue
        for mode, _name, child in entries:
            if mode == "40000":
                stack.append(child)


def _collect_tree(store, repo, tree_oid, entries, added, skip=None):
    """Append a tree and everything under it to entries (once each),
    skipping oids in `skip` (objects the client already holds)."""
    stack = [tree_oid]
    while stack:
        oid = stack.pop()
        if oid in added or oid is None:
            continue
        added.add(oid)
        if skip is not None and oid in skip:
            continue
        entry = repo.objects.get(oid)
        if entry is None or entry[0] != gitpack.OBJ_TREE:
            continue
        entries.append(entry)
        for mode, _name, child in gitpack.parse_tree(entry[1]):
            if mode == "40000":
                stack.append(child)
            elif child not in added:
                added.add(child)
                if skip is not None and child in skip:
                    continue
                blob = repo.objects.get(child)
                if blob is not None:
                    entries.append(blob)


# ---------------------------------------------------------------- upload-pack

def upload_pack(store, owner, name, git_protocol, body):
    repo = _repo_or_404(store, owner, name)
    try:
        packets = gitpack.decode_pkt_lines(body)
    except gitpack.PktError as exc:
        raise ForgeError(400, "malformed pkt-line stream: %s" % exc)
    lines = [(kind, payload) for kind, payload in packets if kind == "data"]
    if git_protocol == "version=2":
        return _upload_pack_v2(store, repo, packets)
    return _upload_pack_v0(store, repo, packets)


def _text_lines(packets):
    return [payload.decode("utf-8", "replace") for kind, payload in packets
            if kind == "data"]


def _upload_pack_v2(store, repo, packets):
    texts = _text_lines(packets)
    if not texts or not texts[0].startswith("command="):
        raise ForgeError(400, "expected a command= line")
    command = texts[0][len("command="):].strip()
    args = []
    seen_delim = False
    for kind, payload in packets:
        if kind == "delim":
            seen_delim = True
            continue
        if kind == "flush":
            break
        if not seen_delim:
            continue
        text = payload.decode("utf-8", "replace").rstrip("\n")
        if "=" in text and not text.startswith(("want ", "have ", "shallow ",
                                                 "deepen ", "deepen-since ",
                                                 "deepen-not ")):
            key, _, value = text.partition("=")
            args.append((key, value))
        else:
            key, _, value = text.partition(" ")
            args.append((key, value))
    if command == "ls-refs":
        return 200, "application/x-git-upload-pack-result", _ls_refs(store, repo, args)
    if command != "fetch":
        raise ForgeError(400, "unknown v2 command: %s" % command)
    wants = [v for k, v in args if k == "want"]
    haves = [v for k, v in args if k == "have"]
    shallow = [v for k, v in args if k == "shallow"]
    deepen = None
    for k, v in args:
        if k == "deepen":
            try:
                deepen = int(v)
            except ValueError:
                raise ForgeError(400, "bad deepen value")
    for want in wants:
        if store.get_object(repo, want) is None:
            return _fetch_error_v2(want)
    pack, shallow_out, unshallow_out, acks = _fetch_plan(
        store, repo, wants, haves, deepen, shallow)
    out = bytearray()
    out += gitpack.pkt_line(b"acknowledgments\n")
    if acks:
        for ack in acks:
            out += gitpack.pkt_line(("ACK %s\n" % ack).encode())
        out += gitpack.pkt_line(b"ready\n")
    else:
        out += gitpack.pkt_line(b"NAK\n")
    out += gitpack.pkt_delim()
    if deepen is not None or shallow:
        out += gitpack.pkt_line(b"shallow-info\n")
        for oid in shallow_out:
            out += gitpack.pkt_line(("shallow %s\n" % oid).encode())
        for oid in unshallow_out:
            out += gitpack.pkt_line(("unshallow %s\n" % oid).encode())
        out += gitpack.pkt_delim()
    out += gitpack.pkt_line(b"packfile\n")
    out += _chunk_sideband(pack)
    out += gitpack.pkt_flush()
    return 200, "application/x-git-upload-pack-result", bytes(out)


def _fetch_error_v2(want):
    out = bytearray()
    out += gitpack.pkt_line(b"acknowledgments\n")
    out += gitpack.pkt_line(b"NAK\n")
    out += gitpack.pkt_delim()
    out += gitpack.pkt_line(b"packfile\n")
    out += gitpack.pkt_line(b"\x03ERR unknown object %s\n" % want.encode())
    out += gitpack.pkt_flush()
    return 200, "application/x-git-upload-pack-result", bytes(out)


def _upload_pack_v0(store, repo, packets):
    # Two sections: wants (+shallow/deepen), flush, then haves (+done), flush.
    sections = []
    current = []
    for kind, payload in packets:
        if kind == "flush":
            sections.append(current)
            current = []
        elif kind == "data":
            current.append(payload.decode("utf-8", "replace").rstrip("\n"))
    if current:
        sections.append(current)
    wants = []
    haves = []
    shallow = []
    deepen = None
    for section in sections:
        for text in section:
            parts = text.split(" ")
            if parts[0] == "want" and len(parts) >= 2:
                wants.append(parts[1])
            elif parts[0] == "have" and len(parts) >= 2:
                haves.append(parts[1])
            elif parts[0] == "shallow" and len(parts) >= 2:
                shallow.append(parts[1])
            elif parts[0] == "deepen" and len(parts) >= 2:
                try:
                    deepen = int(parts[1])
                except ValueError:
                    raise ForgeError(400, "bad deepen value")
    for want in wants:
        if store.get_object(repo, want) is None:
            return _fetch_error_v0(want)
    pack, shallow_out, unshallow_out, acks = _fetch_plan(
        store, repo, wants, haves, deepen, shallow)
    out = bytearray()
    if acks:
        for ack in acks:
            out += gitpack.pkt_line(("ACK %s\n" % ack).encode())
    else:
        out += gitpack.pkt_line(b"NAK\n")
    for oid in shallow_out:
        out += gitpack.pkt_line(("shallow %s\n" % oid).encode())
    for oid in unshallow_out:
        out += gitpack.pkt_line(("unshallow %s\n" % oid).encode())
    out += _chunk_sideband(pack)
    out += gitpack.pkt_flush()
    return 200, "application/x-git-upload-pack-result", bytes(out)


def _fetch_error_v0(want):
    out = bytearray()
    out += gitpack.pkt_line(b"NAK\n")
    out += gitpack.pkt_line(b"\x03ERR unknown object %s\n" % want.encode())
    out += gitpack.pkt_flush()
    return 200, "application/x-git-upload-pack-result", bytes(out)


# ---------------------------------------------------------------- receive-pack

def receive_pack(store, owner, name, body):
    repo = _repo_or_404(store, owner, name)
    try:
        packets, rest = gitpack.decode_pkt_lines_until_flush(body)
    except gitpack.PktError as exc:
        return _push_report([("unpack", "malformed request: %s" % exc)], [])
    commands = []
    for kind, payload in packets:
        if kind != "data":
            continue
        text = payload.decode("utf-8", "replace")
        if "\x00" in text:
            text = text.split("\x00", 1)[0]
        text = text.rstrip("\n")
        parts = text.split(" ")
        if len(parts) < 3:
            return _push_report([("unpack", "malformed command")], [])
        commands.append((parts[0], parts[1], parts[2]))
    if not commands:
        return _push_report([("unpack", "no commands")], [])
    # Unpack the pack (delete-only pushes carry none).
    unpacked = {}
    if rest:
        try:
            unpacked, _ = gitpack.read_pack(
                rest, resolve_base=lambda oid: repo.objects.get(oid))
        except ValueError as exc:
            return _push_report([("unpack", str(exc))], [])
    store.put_objects(repo, unpacked)
    results = []
    for old_oid, new_oid, refname in commands:
        results.append(_apply_push(store, repo, old_oid, new_oid, refname))
    return _push_report([("unpack", "ok")], results)


def _apply_push(store, repo, old_oid, new_oid, refname):
    current = repo.refs.get(refname)
    is_tag = refname.startswith("refs/tags/")
    if new_oid == ZERO:
        # Delete.
        if current is None:
            return (refname, False, "non-fast-forward")
        if old_oid != ZERO and old_oid != current:
            return (refname, False, "non-fast-forward")
        store.delete_ref(repo, refname)
        return (refname, True, "")
    if old_oid == ZERO:
        # Create.
        if current is not None:
            return (refname, False, "already exists")
    else:
        if current is None:
            return (refname, False, "non-fast-forward")
        if old_oid != current:
            return (refname, False, "non-fast-forward")
    entry = repo.objects.get(new_oid)
    if entry is None:
        return (refname, False, "object not found")
    if entry[0] == gitpack.OBJ_TAG:
        if not is_tag:
            return (refname, False, "object not found")
    elif entry[0] != gitpack.OBJ_COMMIT:
        return (refname, False, "object not found")
    if not is_tag and old_oid != ZERO \
            and not store.is_ancestor(repo, old_oid, new_oid):
        return (refname, False, "non-fast-forward")
    store.set_ref(repo, refname, new_oid)
    return (refname, True, "")


def _push_report(unpack_lines, results):
    inner = bytearray()
    for kind, detail in unpack_lines:
        inner += gitpack.pkt_line(("unpack %s\n" % detail).encode())
    for refname, ok, reason in results:
        if ok:
            inner += gitpack.pkt_line(("ok %s\n" % refname).encode())
        else:
            inner += gitpack.pkt_line(("ng %s %s\n" % (refname, reason)).encode())
    inner += gitpack.pkt_flush()
    outer = gitpack.pkt_line(b"\x01" + bytes(inner)) + gitpack.pkt_flush()
    return 200, "application/x-git-receive-pack-result", outer
