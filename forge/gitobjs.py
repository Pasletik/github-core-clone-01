"""Git objects, packfiles and pkt-lines — pure functions over bytes.

Everything here is stdlib-only (hashlib, zlib) and has no I/O. The object
store itself lives in forge.store; this module is the encoding layer the
store and the protocol handlers share.

Object ids are SHA-1 over "<type> <len>\\0<data>", as git computes them.
Packs are pack v2, written undeltified; packs read here may carry
ofs-delta and ref-delta entries, resolved on unpack.
"""

from __future__ import annotations

import hashlib
import zlib

ZERO_OID = "0" * 40

TYPE_CODES = {"commit": 1, "tree": 2, "blob": 3, "tag": 4}
CODE_TYPES = {1: "commit", 2: "tree", 3: "blob", 4: "tag", 6: "ofs-delta", 7: "ref-delta"}

# pkt-line framing -----------------------------------------------------------
PKT_LINE_MAX = 65520


def hash_object(obj_type: str, data: bytes) -> str:
    h = hashlib.sha1()
    h.update(obj_type.encode("ascii"))
    h.update(b" ")
    h.update(str(len(data)).encode("ascii"))
    h.update(b"\0")
    h.update(data)
    return h.hexdigest()


def pkt_line(payload: bytes | str) -> bytes:
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    size = len(payload) + 4
    return ("%04x" % size).encode("ascii") + payload


def pkt_flush() -> bytes:
    return b"0000"


def pkt_delim() -> bytes:
    return b"0001"


def pkt_response_end() -> bytes:
    return b"0002"


def encode_pkt_lines(lines) -> bytes:
    """lines: iterable of str/bytes, or 'flush'/'delim'/'response-end' markers."""
    out = bytearray()
    for line in lines:
        if line == "flush":
            out += pkt_flush()
        elif line == "delim":
            out += pkt_delim()
        elif line == "response-end":
            out += pkt_response_end()
        else:
            out += pkt_line(line)
    return bytes(out)


def decode_pkt_lines(data: bytes):
    """Return a list of (kind, payload) tuples; kind in data/flush/delim/end.

    Raises ValueError on a malformed frame (the server answers 400).
    """
    packets = []
    i = 0
    n = len(data)
    while i < n:
        if i + 4 > n:
            raise ValueError("truncated pkt-line length")
        head = data[i:i + 4]
        try:
            size = int(head.decode("ascii"), 16)
        except ValueError:
            raise ValueError("pkt-line length is not hex: %r" % head[:4])
        if size == 0:
            packets.append(("flush", b""))
            i += 4
        elif size == 1:
            packets.append(("delim", b""))
            i += 4
        elif size == 2:
            packets.append(("end", b""))
            i += 4
        elif size < 4 or i + size > n:
            raise ValueError("pkt-line length out of range: %d" % size)
        else:
            packets.append(("data", data[i + 4:i + size]))
            i += size
    return packets


def pkt_text_lines(data: bytes):
    """Decode a pkt-line stream to a list of text lines (without trailing \\n)."""
    lines = []
    for kind, payload in decode_pkt_lines(data):
        if kind == "data":
            lines.append(payload.decode("utf-8", "replace").rstrip("\n"))
        elif kind == "flush":
            lines.append(None)
        elif kind == "delim":
            lines.append("")
    return lines


# trees and commits ----------------------------------------------------------

def parse_tree(data: bytes):
    """Return [(mode: str, name: str, oid: str)] in stored order."""
    entries = []
    i = 0
    n = len(data)
    while i < n:
        sp = data.index(b" ", i)
        mode = data[i:sp].decode("ascii")
        nul = data.index(b"\0", sp)
        name = data[sp + 1:nul].decode("utf-8", "surrogateescape")
        oid = data[nul + 1:nul + 21].hex()
        entries.append((mode, name, oid))
        i = nul + 21
    return entries


def build_tree(entries) -> bytes:
    """entries: iterable of (mode, name, oid); sorted git-style by name."""
    out = bytearray()
    for mode, name, oid in sorted(entries, key=lambda e: _tree_sort_key(e[1])):
        out += mode.encode("ascii") + b" "
        out += name.encode("utf-8", "surrogateescape") + b"\0"
        out += bytes.fromhex(oid)
    return bytes(out)


def _tree_sort_key(name: str) -> bytes:
    # git sorts tree entries by name, directories as if suffixed with '/'.
    return name.encode("utf-8", "surrogateescape")


def parse_commit(data: bytes):
    """Return {'tree', 'parents': [...], 'author', 'committer', 'message', 'headers'}."""
    text = data.decode("utf-8", "surrogateescape")
    header, _, message = text.partition("\n\n")
    parents = []
    tree = None
    author = ""
    committer = ""
    for line in header.split("\n"):
        if line.startswith("tree "):
            tree = line[5:].strip()
        elif line.startswith("parent "):
            parents.append(line[7:].strip())
        elif line.startswith("author "):
            author = line[7:]
        elif line.startswith("committer "):
            committer = line[10:]
    return {
        "tree": tree,
        "parents": parents,
        "author": author,
        "committer": committer,
        "message": message,
        "headers": header,
    }


def build_commit(tree: str, parents, author: str, committer: str, message: str) -> bytes:
    lines = [f"tree {tree}"]
    for parent in parents:
        lines.append(f"parent {parent}")
    lines.append(f"author {author}")
    lines.append(f"committer {committer}")
    return ("\n".join(lines) + "\n\n" + message).encode("utf-8", "surrogateescape")


def parse_tag(data: bytes):
    text = data.decode("utf-8", "surrogateescape")
    header, _, message = text.partition("\n\n")
    out = {"message": message}
    for line in header.split("\n"):
        if line.startswith("object "):
            out["object"] = line[7:].strip()
        elif line.startswith("type "):
            out["type"] = line[5:].strip()
        elif line.startswith("tag "):
            out["tag"] = line[4:].strip()
        elif line.startswith("tagger "):
            out["tagger"] = line[7:]
    return out


def build_tag(obj: str, obj_type: str, tag: str, tagger: str, message: str) -> bytes:
    lines = [f"object {obj}", f"type {obj_type}", f"tag {tag}", f"tagger {tagger}"]
    return ("\n".join(lines) + "\n\n" + message).encode("utf-8", "surrogateescape")


# deltas ---------------------------------------------------------------------

def _read_varint(buf: bytes, pos: int):
    result = 0
    shift = 0
    while True:
        if pos >= len(buf):
            raise ValueError("delta varint truncated")
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        shift += 7
        if not (byte & 0x80):
            return result, pos


def apply_delta(base: bytes, delta: bytes) -> bytes:
    """Apply a git delta to base; returns the resolved bytes."""
    pos = 0
    src_size, pos = _read_varint(delta, pos)
    if src_size != len(base):
        raise ValueError("delta base size %d, expected %d" % (src_size, len(base)))
    dst_size, pos = _read_varint(delta, pos)
    out = bytearray()
    while pos < len(delta):
        op = delta[pos]
        pos += 1
        if op & 0x80:
            offset = 0
            size = 0
            for bit in range(4):
                if op & (1 << bit):
                    offset |= delta[pos] << (8 * bit)
                    pos += 1
            for bit in range(3):
                if op & (0x10 << bit):
                    size |= delta[pos] << (8 * bit)
                    pos += 1
            if size == 0:
                size = 0x10000
            out += base[offset:offset + size]
        elif op:
            out += delta[pos:pos + op]
            pos += op
        else:
            raise ValueError("delta opcode 0")
    if len(out) != dst_size:
        raise ValueError("delta result size %d, expected %d" % (len(out), dst_size))
    return bytes(out)


def _read_ofs_delta_base(buf: bytes, pos: int):
    byte = buf[pos]
    pos += 1
    offset = byte & 0x7F
    while byte & 0x80:
        byte = buf[pos]
        pos += 1
        offset = ((offset + 1) << 7) | (byte & 0x7F)
    return offset, pos


# packs ----------------------------------------------------------------------

def write_pack(objects) -> bytes:
    """Build a pack v2 from (oid, type, data) triples. Undeltified,
    deterministic order (sorted by oid), SHA-1 trailer. The oids are
    passed in (already known) instead of re-hashed from the content."""
    items = sorted(objects, key=lambda item: item[0])
    parts = [b"PACK", (2).to_bytes(4, "big"), len(items).to_bytes(4, "big")]
    for _oid, obj_type, data in items:
        code = TYPE_CODES[obj_type]
        size = len(data)
        first = (code << 4) | (size & 0x0F)
        size >>= 4
        header = bytearray()
        while size:
            header.append(first | 0x80)
            first = size & 0x7F
            size >>= 7
        header.append(first)
        parts.append(bytes(header))
        parts.append(zlib.compress(data))
    body = b"".join(parts)
    return body + hashlib.sha1(body).digest()


def verify_pack_checksum(pack: bytes) -> bool:
    if len(pack) < 20:
        return False
    return hashlib.sha1(pack[:-20]).digest() == pack[-20:]


def read_pack(pack: bytes, base_lookup=None):
    """Parse a pack v2. Returns {'objects': {oid: (type, data)}, 'delta_count': n}.

    base_lookup(oid) resolves a ref-delta base that is not in the pack
    (a thin pack's base, or an object the store already holds).
    Raises ValueError on a malformed pack.
    """
    if len(pack) < 32 or pack[:4] != b"PACK":
        raise ValueError("not a packfile")
    version = int.from_bytes(pack[4:8], "big")
    if version not in (2, 3):
        raise ValueError("pack version %d" % version)
    count = int.from_bytes(pack[8:12], "big")
    if not verify_pack_checksum(pack):
        raise ValueError("pack trailer checksum mismatch")
    pos = 12
    entries = []  # (offset, type|delta-kind, payload, extra)
    objects = {}
    delta_count = 0
    for _ in range(count):
        entry_offset = pos
        byte = pack[pos]
        pos += 1
        obj_type = (byte >> 4) & 0x07
        size = byte & 0x0F
        shift = 4
        while byte & 0x80:
            byte = pack[pos]
            pos += 1
            size |= (byte & 0x7F) << shift
            shift += 7
        if obj_type == 6:  # ofs-delta
            base_off, pos = _read_ofs_delta_base(pack, pos)
            entries.append((entry_offset, "ofs-delta", base_off, size))
        elif obj_type == 7:  # ref-delta
            base_oid = pack[pos:pos + 20].hex()
            pos += 20
            entries.append((entry_offset, "ref-delta", base_oid, size))
        else:
            entries.append((entry_offset, CODE_TYPES[obj_type], None, size))
        # zlib stream: inflate from pos; the decompressor tells us the end.
        # memoryview avoids copying the pack tail on every object.
        decomp = zlib.decompressobj()
        view = memoryview(pack)[pos:]
        data = decomp.decompress(view)
        consumed = len(view) - len(decomp.unused_data)
        pos += consumed
        if len(data) != size:
            raise ValueError("object size mismatch in pack")
        entries[-1] = (entry_offset, entries[-1][1], entries[-1][2], data)
    by_offset = {off: idx for idx, (off, *_rest) in enumerate(entries)}

    def resolve(idx, seen):
        kind = entries[idx][1]
        if kind in ("ofs-delta", "ref-delta"):
            if idx in seen:
                raise ValueError("delta cycle in pack")
            seen = seen | {idx}
            if kind == "ofs-delta":
                base_idx = by_offset.get(entries[idx][0] - entries[idx][2])
                if base_idx is None:
                    raise ValueError("ofs-delta base offset not in pack")
                base_type, base_data = resolve(base_idx, seen)
            else:
                base_oid = entries[idx][2]
                found = objects.get(base_oid)
                if found is not None:
                    base_type, base_data = found
                elif base_lookup is not None:
                    found = base_lookup(base_oid)
                    if found is None:
                        raise ValueError("ref-delta base %s not found" % base_oid)
                    base_type, base_data = found
                else:
                    raise ValueError("ref-delta base %s not in pack" % base_oid)
            data = apply_delta(base_data, entries[idx][3])
            delta_count_inc = 1
            return base_type, data, delta_count_inc
        return kind, entries[idx][3], 0

    total_deltas = 0
    for idx in range(len(entries)):
        obj_type, data, nd = resolve(idx, set())
        total_deltas += nd
        oid = hash_object(obj_type, data)
        objects[oid] = (obj_type, data)
    return {"objects": objects, "delta_count": total_deltas, "version": version,
            "declared_count": count}


# reachability ---------------------------------------------------------------

def collect_reachable(lookup, tips):
    """All oids reachable from tips (commits, their trees/blobs, tag targets).

    lookup(oid) -> (type, data) or None. Raises KeyError on a missing object.
    """
    seen = set()
    stack = [tip for tip in tips if tip != ZERO_OID]
    while stack:
        oid = stack.pop()
        if oid in seen or oid == ZERO_OID:
            continue
        found = lookup(oid)
        if found is None:
            raise KeyError("missing object %s" % oid)
        obj_type, data = found
        seen.add(oid)
        if obj_type == "commit":
            parsed = parse_commit(data)
            stack.append(parsed["tree"])
            stack.extend(parsed["parents"])
        elif obj_type == "tree":
            for _mode, _name, child in parse_tree(data):
                stack.append(child)
        elif obj_type == "tag":
            stack.append(parse_tag(data)["object"])
    return seen


def shallow_reachable(lookup, tips, depth):
    """Objects reachable from tips within `depth` commit generations
    (depth 1 = the tips only, parents excluded).

    Returns (objects: set, boundary: set of commit oids at exactly depth).
    The boundary commits' parents are not walked.
    """
    objects = set()
    boundary = set()
    # BFS over commits by generation.
    frontier = [(tip, 1) for tip in tips if tip != ZERO_OID]
    seen_commits = set()
    while frontier:
        oid, gen = frontier.pop()
        if oid in seen_commits or oid == ZERO_OID:
            continue
        found = lookup(oid)
        if found is None:
            raise KeyError("missing object %s" % oid)
        obj_type, data = found
        if obj_type != "commit":
            # a tag tip: peel it, same generation
            if obj_type == "tag":
                target = parse_tag(data)["object"]
                objects.add(oid)
                frontier.append((target, gen))
            continue
        seen_commits.add(oid)
        objects.add(oid)
        parsed = parse_commit(data)
        objects.update(collect_reachable(lookup, [parsed["tree"]]))
        if gen >= depth:
            boundary.add(oid)
        else:
            for parent in parsed["parents"]:
                frontier.append((parent, gen + 1))
    return objects, boundary
