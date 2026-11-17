"""Git object and wire primitives: pkt-lines, packfiles, objects, trees, commits.

Written from the author's own understanding of the git wire format. No code
is copied from any existing implementation.
"""

import hashlib
import zlib

ZERO_OID = "0" * 40

OBJ_COMMIT = 1
OBJ_TREE = 2
OBJ_BLOB = 3
OBJ_TAG = 4
OBJ_OFS_DELTA = 6
OBJ_REF_DELTA = 7

_TYPE_NAMES = {1: "commit", 2: "tree", 3: "blob", 4: "tag"}
_NAME_TYPES = {"commit": 1, "tree": 2, "blob": 3, "tag": 4}


def type_name(otype):
    return _TYPE_NAMES.get(otype, "unknown")


def type_code(name):
    return _NAME_TYPES[name]


def oid_of(otype, payload):
    """The SHA-1 object id of a git object: sha1("<type> <len>\\0" + payload)."""
    head = ("%s %d" % (type_name(otype), len(payload))).encode("ascii") + b"\x00"
    return hashlib.sha1(head + payload).hexdigest()


def oid_bytes(hex_oid):
    return bytes.fromhex(hex_oid)


# ---------------------------------------------------------------- pkt-lines

def pkt_line(data):
    """One pkt-line: 4 hex digits of length (header included) + payload."""
    return ("%04x" % (len(data) + 4)).encode("ascii") + data


def pkt_flush():
    return b"0000"


def pkt_delim():
    return b"0001"


def encode_pkt_lines(items):
    """items: bytes payloads (line content, usually ending with \\n), or the
    strings 'flush' / 'delim'. Returns the concatenated framing."""
    out = bytearray()
    for item in items:
        if item == "flush":
            out += b"0000"
        elif item == "delim":
            out += b"0001"
        else:
            out += pkt_line(item)
    return bytes(out)


class PktError(ValueError):
    pass


def decode_pkt_lines(data):
    """Decode a whole pkt-line stream. Returns a list of (kind, payload) with
    kind in 'data', 'flush', 'delim'. Raises PktError on malformed input."""
    out = []
    pos = 0
    total = len(data)
    while pos < total:
        if pos + 4 > total:
            raise PktError("truncated pkt-line header")
        raw = data[pos:pos + 4]
        try:
            length = int(raw.decode("ascii"), 16)
        except ValueError:
            raise PktError("bad pkt-line length %r" % raw)
        if length == 0:
            out.append(("flush", b""))
            pos += 4
        elif length == 1:
            out.append(("delim", b""))
            pos += 4
        elif length < 4:
            raise PktError("pkt-line length %d too small" % length)
        elif pos + length > total:
            raise PktError("truncated pkt-line payload")
        else:
            out.append(("data", data[pos + 4:pos + length]))
            pos += length
    return out


def decode_pkt_lines_until_flush(data):
    """Decode pkt-lines up to and including the first flush packet. Returns
    (packets, rest) where rest is the bytes after the flush."""
    packets = []
    pos = 0
    total = len(data)
    while pos < total:
        if pos + 4 > total:
            raise PktError("truncated pkt-line header")
        raw = data[pos:pos + 4]
        try:
            length = int(raw.decode("ascii"), 16)
        except ValueError:
            raise PktError("bad pkt-line length %r" % raw)
        if length == 0:
            packets.append(("flush", b""))
            return packets, data[pos + 4:]
        if length == 1:
            packets.append(("delim", b""))
            pos += 4
            continue
        if length < 4 or pos + length > total:
            raise PktError("truncated pkt-line payload")
        packets.append(("data", data[pos + 4:pos + length]))
        pos += length
    raise PktError("stream ended without a flush packet")


# ---------------------------------------------------------------- packfiles

def pack_object_header(otype, size):
    """The type/size varint that starts a pack entry: first byte holds the
    type in bits 4-6 and the low 4 bits of the size; following bytes hold 7
    size bits each, least significant groups first, MSB set while more follow."""
    out = bytearray()
    first = (otype << 4) | (size & 0x0F)
    size >>= 4
    if size:
        first |= 0x80
    out.append(first)
    while size:
        byte = size & 0x7F
        size >>= 7
        if size:
            byte |= 0x80
        out.append(byte)
    return bytes(out)


def write_pack(entries, level=1):
    """entries: iterable of (otype, payload). Returns a complete PACK v2 file
    (header, entries, SHA-1 trailer). No deltas are ever emitted."""
    out = bytearray()
    entries = list(entries)
    out += b"PACK"
    out += (2).to_bytes(4, "big")
    out += len(entries).to_bytes(4, "big")
    for otype, payload in entries:
        out += pack_object_header(otype, len(payload))
        out += zlib.compress(payload, level)
    out += hashlib.sha1(bytes(out)).digest()
    return bytes(out)


def _read_varint(data, pos):
    """Read a pack-entry type/size varint. Returns (otype, size, new_pos)."""
    byte = data[pos]
    pos += 1
    otype = (byte >> 4) & 0x07
    size = byte & 0x0F
    shift = 4
    while byte & 0x80:
        byte = data[pos]
        pos += 1
        size |= (byte & 0x7F) << shift
        shift += 7
    return otype, size, pos


def _read_ofs_offset(data, pos):
    """Read an OFS_DELTA base offset. Returns (offset, new_pos)."""
    byte = data[pos]
    pos += 1
    offset = byte & 0x7F
    while byte & 0x80:
        byte = data[pos]
        pos += 1
        offset = ((offset + 1) << 7) | (byte & 0x7F)
    return offset, pos


def _read_delta_size(data, pos):
    """Read a delta base/result size varint (7 bits per byte, MSB last is
    clear; bytes are most-significant first). Returns (value, new_pos)."""
    value = 0
    while True:
        byte = data[pos]
        pos += 1
        value = (value << 7) | (byte & 0x7F)
        if not (byte & 0x80):
            return value, pos


def apply_delta(base, delta):
    """Apply a git delta stream to a base object payload. Raises ValueError
    on malformed deltas."""
    pos = 0
    base_size, pos = _read_delta_size(delta, pos)
    if base_size != len(base):
        raise ValueError("delta base size mismatch")
    result_size, pos = _read_delta_size(delta, pos)
    out = bytearray()
    total = len(delta)
    while pos < total:
        opcode = delta[pos]
        pos += 1
        if opcode & 0x80:
            offset = 0
            size = 0
            for bit in range(4):
                if opcode & (1 << bit):
                    offset |= delta[pos] << (8 * bit)
                    pos += 1
            for bit in range(3):
                if opcode & (0x10 << bit):
                    size |= delta[pos] << (8 * bit)
                    pos += 1
            if size == 0:
                size = 0x10000
            if offset + size > len(base):
                raise ValueError("delta copy out of range")
            out += base[offset:offset + size]
        elif opcode:
            if pos + opcode > total:
                raise ValueError("delta insert out of range")
            out += delta[pos:pos + opcode]
            pos += opcode
        else:
            raise ValueError("delta opcode 0")
    if len(out) != result_size:
        raise ValueError("delta result size mismatch")
    return bytes(out)


def _inflate_at(data, pos, size):
    """Inflate one zlib stream starting at pos. Returns (payload, new_pos).
    When size is given, the inflated payload must have that length."""
    decomp = zlib.decompressobj()
    chunk = data[pos:]
    payload = decomp.decompress(chunk)
    consumed = len(chunk) - len(decomp.unused_data)
    if size is not None and len(payload) != size:
        raise ValueError("inflated size mismatch: got %d, want %d" % (len(payload), size))
    if not decomp.eof:
        raise ValueError("truncated zlib stream in pack")
    return payload, pos + consumed


def read_pack(data, resolve_base=None):
    """Parse a PACK v2/v3 file. Returns (objects, delta_count) where objects
    maps oid hex -> (otype, payload). resolve_base(oid) is consulted for
    REF_DELTA bases not present in the pack (thin packs); it may be None.
    Raises ValueError on malformed input or a bad trailer checksum."""
    if len(data) < 12 or data[:4] != b"PACK":
        raise ValueError("not a packfile")
    version = int.from_bytes(data[4:8], "big")
    if version not in (2, 3):
        raise ValueError("unsupported pack version %d" % version)
    count = int.from_bytes(data[8:12], "big")
    if len(data) < 20:
        raise ValueError("packfile too short for a trailer")
    if hashlib.sha1(data[:-20]).digest() != data[-20:]:
        raise ValueError("pack trailer checksum mismatch")
    objects = {}
    by_offset = {}
    pending_ref = []
    pending_ofs = []
    pos = 12
    delta_count = 0
    for _ in range(count):
        entry_start = pos
        otype, _size, pos = _read_varint(data, pos)
        if otype == OBJ_OFS_DELTA:
            delta_count += 1
            offset, pos = _read_ofs_offset(data, pos)
            delta_payload, pos = _inflate_at(data, pos, None)
            pending_ofs.append((entry_start, entry_start - offset, delta_payload))
            by_offset[entry_start] = None
        elif otype == OBJ_REF_DELTA:
            delta_count += 1
            base_oid = data[pos:pos + 20].hex()
            pos += 20
            delta_payload, pos = _inflate_at(data, pos, None)
            pending_ref.append((entry_start, base_oid, delta_payload))
            by_offset[entry_start] = None
        elif otype in (OBJ_COMMIT, OBJ_TREE, OBJ_BLOB, OBJ_TAG):
            payload, pos = _inflate_at(data, pos, _size)
            oid = oid_of(otype, payload)
            objects[oid] = (otype, payload)
            by_offset[entry_start] = (otype, payload)
        else:
            raise ValueError("unknown pack object type %d" % otype)

    def _resolve_delta(base_entry, delta_payload):
        if base_entry is None:
            return None
        base_type, base_payload = base_entry
        payload = apply_delta(base_payload, delta_payload)
        oid = oid_of(base_type, payload)
        return base_type, payload, oid

    # OFS_DELTA bases always precede the delta in the pack; resolve in order.
    for entry_start, base_start, delta_payload in pending_ofs:
        resolved = _resolve_delta(by_offset.get(base_start), delta_payload)
        if resolved is None:
            raise ValueError("ofs-delta base not found in pack")
        base_type, payload, oid = resolved
        objects[oid] = (base_type, payload)
        by_offset[entry_start] = (base_type, payload)
    # REF_DELTA bases may appear anywhere (or only in the store, thin pack).
    for entry_start, base_oid, delta_payload in pending_ref:
        base_entry = objects.get(base_oid)
        if base_entry is None and resolve_base is not None:
            base_entry = resolve_base(base_oid)
        resolved = _resolve_delta(base_entry, delta_payload)
        if resolved is None:
            raise ValueError("ref-delta base %s not found" % base_oid)
        base_type, payload, oid = resolved
        objects[oid] = (base_type, payload)
        by_offset[entry_start] = (base_type, payload)
    return objects, delta_count


# ---------------------------------------------------------------- trees

def parse_tree(payload):
    """Parse a tree object payload into [(mode, name, oid_hex), ...] in the
    order stored (already git-sorted)."""
    entries = []
    pos = 0
    total = len(payload)
    while pos < total:
        space = payload.index(b" ", pos)
        mode = payload[pos:space].decode("ascii")
        nul = payload.index(b"\x00", space)
        name = payload[space + 1:nul].decode("utf-8", "surrogateescape")
        oid = payload[nul + 1:nul + 21].hex()
        entries.append((mode, name, oid))
        pos = nul + 21
    return entries


def tree_sort_key(entry):
    """Git's tree order: compare names, with directories compared as if they
    had a trailing slash."""
    mode, name, _oid = entry
    return name + "/" if mode == "40000" else name


def build_tree(entries):
    """Serialize [(mode, name, oid_hex)] into a tree object payload."""
    out = bytearray()
    for mode, name, oid in sorted(entries, key=tree_sort_key):
        out += mode.encode("ascii")
        out += b" "
        out += name.encode("utf-8", "surrogateescape")
        out += b"\x00"
        out += bytes.fromhex(oid)
    return bytes(out)


# ---------------------------------------------------------------- commits

def parse_ident(text):
    """Parse 'Name <email> 1234567890 +0000' into (name, email, ts, tz)."""
    try:
        lt = text.index("<")
        gt = text.index(">", lt)
        name = text[:lt].strip()
        email = text[lt + 1:gt]
        rest = text[gt + 1:].split()
        ts = int(rest[0])
        tz = rest[1] if len(rest) > 1 else "+0000"
        return name, email, ts, tz
    except (ValueError, IndexError):
        return text, "", 0, "+0000"


def parse_commit(payload):
    """Parse a commit object into a dict. Keeps the raw author/committer
    lines as well as parsed fields."""
    head, _, message = payload.partition(b"\n\n")
    tree = None
    parents = []
    author = None
    committer = None
    for line in head.decode("utf-8", "surrogateescape").split("\n"):
        if line.startswith("tree "):
            tree = line[5:].strip()
        elif line.startswith("parent "):
            parents.append(line[7:].strip())
        elif line.startswith("author "):
            author = line[7:]
        elif line.startswith("committer "):
            committer = line[10:]
    author_name, author_email, author_ts, author_tz = parse_ident(author or "")
    cm_name, cm_email, cm_ts, cm_tz = parse_ident(committer or "")
    return {
        "tree": tree,
        "parents": parents,
        "author": author,
        "committer": committer,
        "author_name": author_name,
        "author_email": author_email,
        "author_ts": author_ts,
        "author_tz": author_tz,
        "committer_name": cm_name,
        "committer_email": cm_email,
        "committer_ts": cm_ts,
        "committer_tz": cm_tz,
        "message": message.decode("utf-8", "surrogateescape"),
    }


def build_commit(tree, parents, author, committer, message):
    """Serialize a commit object. author/committer are full ident lines
    ('Name <email> ts +0000'); message is a str."""
    out = bytearray()
    out += ("tree %s\n" % tree).encode("ascii")
    for parent in parents:
        out += ("parent %s\n" % parent).encode("ascii")
    out += ("author %s\n" % author).encode("utf-8", "surrogateescape")
    out += ("committer %s\n" % committer).encode("utf-8", "surrogateescape")
    out += b"\n"
    out += message.encode("utf-8", "surrogateescape")
    return bytes(out)


def parse_tag(payload):
    """Parse a tag object into a dict (object, type, tag, tagger, message)."""
    head, _, message = payload.partition(b"\n\n")
    result = {"message": message.decode("utf-8", "surrogateescape")}
    for line in head.decode("utf-8", "surrogateescape").split("\n"):
        if line.startswith("object "):
            result["object"] = line[7:].strip()
        elif line.startswith("type "):
            result["type"] = line[5:].strip()
        elif line.startswith("tag "):
            result["tag"] = line[4:].strip()
        elif line.startswith("tagger "):
            result["tagger"] = line[7:]
    return result
