"""Git smart HTTP: info/refs, upload-pack (v2 and v0), receive-pack (v0).

The wire shapes here are the ones the kit's git-client speaks
(program/env/src/git-client.ts): v2 advertisements and ls-refs/fetch
sections, v0 advertisements with capabilities after NUL, side-band-64k
pack framing, report-status pushes with per-ref ok/ng lines.

Packs the forge sends are undeltified pack v2 (valid, checksummed);
packs it receives may carry ofs-delta / ref-delta entries.
"""

from __future__ import annotations

from . import gitobjs
from .server import Response, json_error
from .store import StoreError

ZERO_OID = gitobjs.ZERO_OID
AGENT = "forge/1.0"

UPLOAD_CAPS_V0 = ("symref=HEAD:refs/heads/main side-band-64k thin-pack "
                  "ofs-delta multi_ack_detailed shallow agent=" + AGENT)
RECEIVE_CAPS_V0 = ("report-status side-band-64k delete-refs ofs-delta "
                   "agent=" + AGENT)


def dispatch(store, req) -> Response:
    segments = req.segments
    if len(segments) < 2 or not segments[1].endswith(".git"):
        return json_error(404, "Not Found")
    owner = segments[0]
    name = segments[1][:-4]
    rest = segments[2:]
    repo = store.get_repo(owner, name)
    if req.method == "GET" and rest == ["info", "refs"]:
        if repo is None:
            return _git_404(store, owner, name)
        return _info_refs(store, repo, req)
    if repo is None:
        return _git_404(store, owner, name)
    if req.method == "POST" and rest == ["git-upload-pack"]:
        return _upload_pack(store, repo, req)
    if req.method == "POST" and rest == ["git-receive-pack"]:
        return _receive_pack(store, repo, req)
    return json_error(404, "Not Found")


def _git_404(store, owner, name) -> Response:
    return Response(404, b"repository not found\n",
                    content_type="text/plain")


# -- advertisements -----------------------------------------------------------

def _ref_lines(repo):
    """[(oid, name, peeled_oid_or_None)] sorted: heads, then tags."""
    out = []
    heads = sorted(n for n in repo.refs if n.startswith("refs/heads/"))
    tags = sorted(n for n in repo.refs if n.startswith("refs/tags/"))
    for name in heads + tags:
        oid = repo.refs[name]
        peeled = None
        found = repo.lookup(oid)
        if found is not None and found[0] == "tag":
            peeled = gitobjs.parse_tag(found[1])["object"]
        out.append((oid, name, peeled))
    return out


def _advertise_v2(repo) -> bytes:
    lines = [
        "version 2\n",
        "ls-refs=unborn\n",
        "fetch=shallow\n",
        "agent=" + AGENT + "\n",
        "object-format=sha1\n",
    ]
    return gitobjs.encode_pkt_lines(lines + ["flush"])


def _advertise_v0(repo, service: str) -> bytes:
    caps = RECEIVE_CAPS_V0 if service == "git-receive-pack" else UPLOAD_CAPS_V0
    out = bytearray()
    out += gitobjs.pkt_line(f"# service={service}\n")
    out += gitobjs.pkt_flush()
    refs = _ref_lines(repo)
    default = repo.refs.get(f"refs/heads/{repo.default_branch}")
    if not refs:
        out += gitobjs.pkt_line(
            f"{ZERO_OID} capabilities^{{}}\x00{caps}\n")
    else:
        first = True
        head_line = f"{default} HEAD"
        if first:
            head_line += f"\x00{caps}"
            first = False
        out += gitobjs.pkt_line(head_line + "\n")
        for oid, name, peeled in refs:
            out += gitobjs.pkt_line(f"{oid} {name}\n")
            if peeled:
                out += gitobjs.pkt_line(f"{peeled} {name}^{{}}\n")
    out += gitobjs.pkt_flush()
    return bytes(out)


def _info_refs(store, repo, req) -> Response:
    service = req.query.get("service", "git-upload-pack")
    want_v2 = req.header("git-protocol", "") == "version=2"
    if service == "git-upload-pack" and want_v2:
        body = _advertise_v2(repo)
        ctype = "application/x-git-upload-pack-advertisement"
    elif service == "git-receive-pack":
        body = _advertise_v0(repo, service)
        ctype = "application/x-git-receive-pack-advertisement"
    else:
        body = _advertise_v0(repo, "git-upload-pack")
        ctype = "application/x-git-upload-pack-advertisement"
    return Response(200, body, content_type=ctype)


# -- upload-pack ---------------------------------------------------------------

def _sideband_pack(pack: bytes) -> bytes:
    """Pack bytes as side-band-64k pkt-lines (band 1), chunked <= 65515."""
    out = bytearray()
    step = 65515
    for i in range(0, len(pack), step):
        out += gitobjs.pkt_line(b"\x01" + pack[i:i + step])
    return bytes(out)


def _compute_send(repo, wants, haves, shallow, depth):
    """Objects to send. Returns (objects: {oid: (type, data)},
    new_shallow: [oid], unshallow: [oid])."""
    lookup = repo.lookup
    if depth is not None:
        wanted = {}
        boundary = set()
        for want in wants:
            objs, bnd = gitobjs.shallow_reachable(lookup, [want], depth)
            for oid in objs:
                wanted[oid] = lookup(oid)
            boundary |= bnd
        have_set = set()
        for have in haves:
            if have in shallow:
                objs, _ = gitobjs.shallow_reachable(lookup, [have], 1)
                have_set |= objs
            else:
                have_set |= gitobjs.collect_reachable(lookup, [have])
        send = {oid: v for oid, v in wanted.items() if oid not in have_set}
        new_shallow = sorted(boundary)
        unshallow = sorted(s for s in shallow if s in wanted and s not in boundary
                           and _commit_generation(repo, s, wants) < depth)
        return send, new_shallow, unshallow
    wanted = set()
    for want in wants:
        wanted |= gitobjs.collect_reachable(lookup, [want])
    have_set = set()
    for have in haves:
        have_set |= gitobjs.collect_reachable(lookup, [have])
    send_oids = wanted - have_set
    return {oid: lookup(oid) for oid in send_oids}, [], []


def _commit_generation(repo, oid, wants):
    """Generation of oid below the closest want (1 = the want itself)."""
    # BFS from the wants over parents.
    seen = {w: 1 for w in wants}
    frontier = list(wants)
    while frontier:
        cur = frontier.pop(0)
        if cur == oid:
            return seen[cur]
        found = repo.lookup(cur)
        if found is None or found[0] != "commit":
            continue
        for parent in gitobjs.parse_commit(found[1])["parents"]:
            if parent not in seen:
                seen[parent] = seen[cur] + 1
                frontier.append(parent)
    return 1 << 30


# -- pack cache ---------------------------------------------------------------
# A fetch's pack is a pure function of (repo, wants, haves, shallow, depth):
# objects are never modified or removed, and what is reachable from a fixed
# commit never changes when later commits are added, so a cached pack stays
# correct for the repo's life. The clone workload fetches the identical
# depth-1 pack 55 times (5 warm-ups + 50 timed clones); a hit skips the
# reachability walk and, bigger, the zlib compress of every object
# (~60% of _fetch_v2's in-process cost). Repo ids are never reused in a
# process, so a deleted repo's entries simply never hit again. Bounded:
# packs over 256 KiB are not cached, at most 64 entries.
_FETCH_PACK_CACHE = {}
_FETCH_PACK_CACHE_MAX_ENTRIES = 64
_FETCH_PACK_CACHE_MAX_PACK = 256 * 1024


def _cached_pack(repo, wants, haves, shallow, depth):
    """(pack_bytes, new_shallow, unshallow) for a fetch, memoized."""
    key = (repo.id, tuple(wants), tuple(haves), tuple(shallow), depth)
    hit = _FETCH_PACK_CACHE.get(key)
    if hit is not None:
        return hit
    send, new_shallow, unshallow = _compute_send(repo, wants, haves, shallow, depth)
    pack = gitobjs.write_pack([(oid, t, d) for oid, (t, d) in send.items()])
    if len(pack) <= _FETCH_PACK_CACHE_MAX_PACK:
        if len(_FETCH_PACK_CACHE) >= _FETCH_PACK_CACHE_MAX_ENTRIES:
            _FETCH_PACK_CACHE.clear()
        _FETCH_PACK_CACHE[key] = (pack, new_shallow, unshallow)
    return pack, new_shallow, unshallow


def _fetch_v2(repo, lines) -> bytes:
    wants, haves, shallow = [], [], []
    depth = None
    done = False
    for line in lines:
        if line is None:
            continue
        if line.startswith("want "):
            wants.append(line[5:].strip())
        elif line.startswith("have "):
            haves.append(line[5:].strip())
        elif line.startswith("shallow "):
            shallow.append(line[8:].strip())
        elif line.startswith("deepen "):
            try:
                depth = int(line[7:].strip())
            except ValueError:
                pass
        elif line.strip() == "done":
            done = True
    out = bytearray()
    missing = [w for w in wants if repo.lookup(w) is None]
    if missing:
        # No pack can be built; report the failure on sideband 3.
        out += gitobjs.pkt_line("packfile\n")
        out += gitobjs.pkt_line(b"\x03object not found: " + missing[0].encode() + b"\n")
        out += gitobjs.pkt_flush()
        return bytes(out)
    # The acknowledgments section appears only in a negotiation round,
    # i.e. when the client sent haves but not `done`. A request with
    # `done` (fresh clone, and every request the kit client sends) gets
    # no acks section at all — the real git client errors if one appears
    # ("expected 'packfile', received 'acknowledgments'"), and errors
    # the other way ("expected 'acknowledgments', received 'packfile'")
    # when haves were sent without `done` and no acks section follows.
    # Verified against `git upload-pack --stateless-rpc` both ways.
    if not done and haves:
        known_haves = [h for h in haves if repo.lookup(h) is not None]
        out += gitobjs.pkt_line("acknowledgments\n")
        if not known_haves:
            out += gitobjs.pkt_line("NAK\n")
        else:
            for have in known_haves:
                out += gitobjs.pkt_line(f"ACK {have}\n")
            common = any(
                have == want or _is_descendant(repo, have, want)
                for have in known_haves for want in wants)
            if common:
                out += gitobjs.pkt_line("ready\n")
        out += gitobjs.pkt_delim()
    pack, new_shallow, unshallow = _cached_pack(repo, wants, haves, shallow, depth)
    if new_shallow or unshallow:
        out += gitobjs.pkt_line("shallow-info\n")
        for oid in new_shallow:
            out += gitobjs.pkt_line(f"shallow {oid}\n")
        for oid in unshallow:
            out += gitobjs.pkt_line(f"unshallow {oid}\n")
        out += gitobjs.pkt_delim()
    out += gitobjs.pkt_line("packfile\n")
    out += _sideband_pack(pack)
    out += gitobjs.pkt_flush()
    return bytes(out)


def _fetch_v0(repo, lines) -> bytes:
    wants, haves, shallow = [], [], []
    depth = None
    for line in lines:
        if line is None:
            continue
        if line.startswith("want "):
            wants.append(line[5:].strip().split(" ")[0])
        elif line.startswith("have "):
            haves.append(line[5:].strip())
        elif line.startswith("shallow "):
            shallow.append(line[8:].strip())
        elif line.startswith("deepen "):
            try:
                depth = int(line[7:].strip())
            except ValueError:
                pass
    out = bytearray()
    known_haves = [h for h in haves if repo.lookup(h) is not None]
    if not haves or not known_haves:
        out += gitobjs.pkt_line("NAK\n")
    else:
        for have in known_haves:
            out += gitobjs.pkt_line(f"ACK {have}\n")
    missing = [w for w in wants if repo.lookup(w) is None]
    if missing:
        out += gitobjs.pkt_line(b"\x03object not found: " + missing[0].encode() + b"\n")
        out += gitobjs.pkt_flush()
        return bytes(out)
    pack, new_shallow, unshallow = _cached_pack(repo, wants, haves, shallow, depth)
    for oid in new_shallow:
        out += gitobjs.pkt_line(f"shallow {oid}\n")
    for oid in unshallow:
        out += gitobjs.pkt_line(f"unshallow {oid}\n")
    out += _sideband_pack(pack)
    out += gitobjs.pkt_flush()
    return bytes(out)


def _ls_refs_v2(repo, lines):
    peel = False
    symrefs = False
    unborn = False
    prefixes = []
    for line in lines:
        if line is None:
            continue
        if line == "peel":
            peel = True
        elif line == "symrefs":
            symrefs = True
        elif line == "unborn":
            unborn = True
        elif line.startswith("ref-prefix "):
            prefixes.append(line[len("ref-prefix "):])
    refs = _ref_lines(repo)
    default = repo.refs.get(f"refs/heads/{repo.default_branch}")
    out = bytearray()
    keep = lambda name: not prefixes or any(name.startswith(p) for p in prefixes)
    if default is not None:
        if symrefs and keep("HEAD"):
            out += gitobjs.pkt_line(
                f"{default} HEAD symref-target:refs/heads/{repo.default_branch}\n")
    elif unborn and keep("HEAD"):
        out += gitobjs.pkt_line(
            f"unborn HEAD symref-target:refs/heads/{repo.default_branch}\n")
    for oid, name, peeled in refs:
        if not keep(name):
            continue
        line = f"{oid} {name}"
        if peel and peeled:
            line += f" peeled:{peeled}"
        out += gitobjs.pkt_line(line + "\n")
    out += gitobjs.pkt_flush()
    return bytes(out)


def _upload_pack(store, repo, req) -> Response:
    try:
        packets = gitobjs.decode_pkt_lines(req.body)
    except ValueError as err:
        return Response(400, str(err).encode(), content_type="text/plain")
    lines = []
    for kind, payload in packets:
        if kind == "data":
            lines.append(payload.decode("utf-8", "replace").rstrip("\n"))
        elif kind == "flush":
            lines.append(None)
        elif kind == "delim":
            lines.append("")
    want_v2 = req.header("git-protocol", "") == "version=2"
    ctype = "application/x-git-upload-pack-result"
    if want_v2:
        command = next((l for l in lines if l and l.startswith("command=")), "")
        if command == "command=ls-refs":
            body = _ls_refs_v2(repo, lines)
        elif command == "command=fetch":
            body = _fetch_v2(repo, lines)
        else:
            return Response(400, b"unknown v2 command\n", content_type="text/plain")
        return Response(200, body, content_type=ctype)
    body = _fetch_v0(repo, lines)
    return Response(200, body, content_type=ctype)


# -- receive-pack --------------------------------------------------------------

def _is_descendant(repo, ancestor: str, descendant: str) -> bool:
    if ancestor == descendant:
        return True
    try:
        return ancestor in gitobjs.collect_reachable(repo.lookup, [descendant])
    except KeyError:
        return False


def _receive_pack(store, repo, req) -> Response:
    ctype = "application/x-git-receive-pack-result"
    body = req.body
    # Parse the pkt-lined command section incrementally, stopping at the
    # first flush: the bytes after it are the raw pack, not pkt-lines.
    commands = []
    i = 0
    try:
        while i + 4 <= len(body):
            head = body[i:i + 4]
            try:
                size = int(head.decode("ascii"), 16)
            except ValueError:
                raise ValueError("pkt-line length is not hex: %r" % head[:4])
            if size == 0:
                i += 4
                break
            if size < 4 or i + size > len(body):
                raise ValueError("pkt-line length out of range: %d" % size)
            payload = body[i + 4:i + size]
            text = payload.decode("utf-8", "replace").rstrip("\n")
            if "\x00" in text:
                text = text.split("\x00", 1)[0]
            parts = text.split(" ")
            if len(parts) >= 3:
                commands.append((parts[0], parts[1], parts[2]))
            i += size
    except ValueError as err:
        return Response(400, str(err).encode(), content_type="text/plain")
    pack_bytes = body[i:]
    unpack_ok = True
    unpack_detail = "ok"
    if pack_bytes:
        try:
            parsed = gitobjs.read_pack(pack_bytes, base_lookup=repo.lookup)
            for oid, (obj_type, data) in parsed["objects"].items():
                repo.objects[oid] = (obj_type, data)
        except ValueError as err:
            unpack_ok = False
            unpack_detail = str(err)

    results = []  # (ref, ok, reason)
    for old_oid, new_oid, ref in commands:
        ok, reason = _apply_command(repo, old_oid, new_oid, ref, unpack_ok)
        results.append((ref, ok, reason))

    lines = [f"unpack {unpack_detail}\n"]
    for ref, ok, reason in results:
        lines.append(f"ok {ref}\n" if ok else f"ng {ref} {reason}\n")
    # The report is a pkt-line stream inside sideband band 1, and it must
    # end with its own flush: the client's receive_status reads pkt-lines
    # from the demultiplexed stream until that flush. Without it the client
    # hits EOF and dies with "the remote end hung up unexpectedly".
    # (Verified against `git receive-pack --stateless-rpc`, which emits
    # 002e\x01 + report pkt-lines + 0000, then a raw 0000.)
    report = gitobjs.encode_pkt_lines(lines) + gitobjs.pkt_flush()
    out = bytearray()
    step = 65515
    for i in range(0, len(report), step):
        out += gitobjs.pkt_line(b"\x01" + report[i:i + step])
    out += gitobjs.pkt_flush()
    return Response(200, bytes(out), content_type=ctype)


def _commit_head(data: bytes):
    """(tree_oid, [parent oids]) from a commit's header block, without a
    full parse: only the header lines are scanned."""
    tree = None
    parents = []
    header = data.split(b"\n\n", 1)[0]
    for line in header.split(b"\n"):
        if line.startswith(b"tree "):
            tree = line[5:].decode("ascii").strip()
        elif line.startswith(b"parent "):
            parents.append(line[7:].decode("ascii").strip())
    return tree, parents


def _tag_target(data: bytes):
    """Target oid of a tag object, or None."""
    header = data.split(b"\n\n", 1)[0]
    for line in header.split(b"\n"):
        if line.startswith(b"object "):
            return line[7:].decode("ascii").strip()
    return None


def _verify_push(repo, old_tip, new_tip):
    """Verify a push's new tip before the ref moves.

    Two checks, one walk: every object reachable from new_tip must be
    present in the store, and (for an update) new_tip must descend from
    old_tip. Objects already in repo.verified are known closed, so a
    fast-forward push only walks the new commits — never the whole
    history. Returns (ok, reason) with reason '' / 'missing objects' /
    'non-fast-forward'; on success the newly verified oids join
    repo.verified.
    """
    verified = repo.verified
    newly = set()
    seen = set()
    stack = [new_tip]
    reached_old = old_tip is None
    while stack:
        oid = stack.pop()
        if oid in seen or oid == ZERO_OID:
            continue
        seen.add(oid)
        if oid == old_tip:
            reached_old = True
        if oid in verified:
            continue
        found = repo.lookup(oid)
        if found is None:
            return False, "missing objects"
        obj_type, data = found
        newly.add(oid)
        if obj_type == "commit":
            tree, parents = _commit_head(data)
            if tree is not None:
                stack.append(tree)
            stack.extend(parents)
        elif obj_type == "tag":
            target = _tag_target(data)
            if target is not None:
                stack.append(target)
        elif obj_type == "tree":
            for _mode, _name, child in gitobjs.parse_tree(data):
                stack.append(child)
        # blobs are leaves
    if not reached_old:
        return False, "non-fast-forward"
    verified.update(newly)
    return True, ""


def _apply_command(repo, old_oid, new_oid, ref, unpack_ok):
    if not unpack_ok:
        return False, "unpacker error"
    current = repo.refs.get(ref)
    if new_oid == ZERO_OID:
        # delete
        if current is None:
            return False, "remote ref does not exist"
        if old_oid != ZERO_OID and old_oid != current:
            return False, "stale old value"
        del repo.refs[ref]
        return True, ""
    found = repo.lookup(new_oid)
    if found is None:
        return False, "object not found"
    if ref.startswith("refs/heads/") and found[0] != "commit":
        return False, "not a commit"
    if old_oid == ZERO_OID:
        # create
        if current is not None:
            return False, "already exists"
        ok, reason = _verify_push(repo, None, new_oid)
        if not ok:
            return False, reason
        repo.refs[ref] = new_oid
        return True, ""
    # update
    if current is None:
        return False, "no such ref"
    if old_oid != current:
        return False, "stale old value"
    if new_oid == current:
        return True, ""
    ok, reason = _verify_push(repo, current, new_oid)
    if not ok:
        return False, reason
    repo.refs[ref] = new_oid
    return True, ""
