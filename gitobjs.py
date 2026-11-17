"""Pkt-line codec, packfile read/write and sideband framing.

Pure Python 3.13 standard library. No I/O, no wall clock, no randomness,
no subprocess, no git binary. No module-level mutable state (constants
only). Used by githttp.py; the pack writer emits plain objects only
(no deltas), the pack reader also resolves OFS/REF deltas so a delta
pack from any peer is accepted.
"""

import hashlib
import struct
import zlib

PKT_LINE_MAX = 65520
PKT_LINE_MAX_PAYLOAD = PKT_LINE_MAX - 4

FLUSH = b"0000"
DELIM = b"0001"
RESPONSE_END = b"0002"

ZERO_OID = "0" * 40

TYPE_CODES = {"commit": 1, "tree": 2, "blob": 3, "tag": 4}
CODE_TYPES = {1: "commit", 2: "tree", 3: "blob", 4: "tag"}

# Largest data chunk that fits in one sideband pkt-line: the pkt-line
# payload is at most 65516 bytes, one of which is the band byte.
SIDEBAND_CHUNK = PKT_LINE_MAX_PAYLOAD - 1


class PktLineError(ValueError):
    """Malformed pkt-line framing."""


class PackError(ValueError):
    """Malformed or corrupt packfile."""


def sha1_obj(obj_type, data):
    """Git object id: SHA-1("<type> <len>\\0" + bytes)."""
    h = hashlib.sha1()
    h.update(obj_type.encode("ascii"))
    h.update(b" ")
    h.update(str(len(data)).encode("ascii"))
    h.update(b"\0")
    h.update(data)
    return h.hexdigest()


# ---------------------------------------------------------------- pkt-line

def encode_pkt_line(payload):
    """Encode one data packet: 4 lowercase-hex length bytes + payload."""
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    if len(payload) > PKT_LINE_MAX_PAYLOAD:
        raise PktLineError("pkt-line payload %d bytes over maximum" % len(payload))
    return ("%04x" % (len(payload) + 4)).encode("ascii") + payload


def encode_pkt_lines(items):
    """Encode a sequence; 'flush'/'delim'/'response-end' are control packets."""
    parts = []
    for item in items:
        if item == "flush":
            parts.append(FLUSH)
        elif item == "delim":
            parts.append(DELIM)
        elif item == "response-end":
            parts.append(RESPONSE_END)
        else:
            parts.append(encode_pkt_line(item))
    return b"".join(parts)


def decode_pkt_lines(data):
    """Decode a complete pkt-line stream.

    Returns a list of (kind, payload) where kind is 'data', 'flush',
    'delim' or 'response-end' and payload is bytes for 'data', else None.
    Raises PktLineError on any malformed header, over-long packet or
    truncated stream. Trailing bytes after the last complete packet that
    do not form a packet are an error (use decode_pkt_lines_prefix when
    raw bytes legitimately follow, e.g. a receive-pack packfile).
    """
    packets = []
    pos = 0
    n = len(data)
    while pos < n:
        if pos + 4 > n:
            raise PktLineError("truncated pkt-line: %d trailing byte(s)" % (n - pos))
        header = data[pos:pos + 4]
        try:
            text = header.decode("ascii")
        except UnicodeDecodeError:
            raise PktLineError("invalid pkt-line length %r" % (header,))
        if not all(c in "0123456789abcdefABCDEF" for c in text):
            raise PktLineError("invalid pkt-line length %r" % (text,))
        length = int(text, 16)
        if length == 0:
            packets.append(("flush", None))
            pos += 4
            continue
        if length == 1:
            packets.append(("delim", None))
            pos += 4
            continue
        if length == 2:
            packets.append(("response-end", None))
            pos += 4
            continue
        if length == 3:
            raise PktLineError("invalid pkt-line length 0003")
        if length > PKT_LINE_MAX:
            raise PktLineError("pkt-line length %d exceeds %d" % (length, PKT_LINE_MAX))
        if pos + length > n:
            raise PktLineError("truncated pkt-line: %d announced, %d available"
                               % (length, n - pos))
        packets.append(("data", data[pos + 4:pos + length]))
        pos += length
    return packets


def decode_pkt_lines_prefix(data):
    """Decode pkt-lines until (and including) the first flush packet.

    Returns (packets, rest_offset) where rest_offset is the index just
    past the flush packet; data[rest_offset:] is the raw trailing bytes
    (the packfile in a receive-pack request). Raises PktLineError when
    the stream ends without a flush or is malformed.
    """
    packets = []
    pos = 0
    n = len(data)
    while pos < n:
        if pos + 4 > n:
            raise PktLineError("truncated pkt-line: %d trailing byte(s)" % (n - pos))
        header = data[pos:pos + 4]
        try:
            text = header.decode("ascii")
        except UnicodeDecodeError:
            raise PktLineError("invalid pkt-line length %r" % (header,))
        if not all(c in "0123456789abcdefABCDEF" for c in text):
            raise PktLineError("invalid pkt-line length %r" % (text,))
        length = int(text, 16)
        if length == 0:
            packets.append(("flush", None))
            return packets, pos + 4
        if length == 1:
            packets.append(("delim", None))
            pos += 4
            continue
        if length == 2:
            packets.append(("response-end", None))
            pos += 4
            continue
        if length == 3:
            raise PktLineError("invalid pkt-line length 0003")
        if length > PKT_LINE_MAX:
            raise PktLineError("pkt-line length %d exceeds %d" % (length, PKT_LINE_MAX))
        if pos + length > n:
            raise PktLineError("truncated pkt-line: %d announced, %d available"
                               % (length, n - pos))
        packets.append(("data", data[pos + 4:pos + length]))
        pos += length
    raise PktLineError("pkt-line stream ended without a flush packet")


def pkt_text_lines(packets):
    """Decoded text of the data packets, control packets dropped."""
    return [payload.decode("utf-8", "surrogateescape")
            for kind, payload in packets if kind == "data"]


# ---------------------------------------------------------------- sideband

def encode_sideband(band, data):
    """Frame raw bytes as sideband pkt-lines (band 1=data, 2=progress, 3=error)."""
    if band not in (1, 2, 3):
        raise ValueError("invalid sideband band %r" % (band,))
    if not data:
        return b""
    parts = []
    for i in range(0, len(data), SIDEBAND_CHUNK):
        chunk = data[i:i + SIDEBAND_CHUNK]
        parts.append(encode_pkt_line(bytes([band]) + chunk))
    return b"".join(parts)


def sideband_packets(band, data):
    """encode_sideband as a list of individual pkt-line bytes."""
    if not data:
        return []
    return [encode_pkt_line(bytes([band]) + data[i:i + SIDEBAND_CHUNK])
            for i in range(0, len(data), SIDEBAND_CHUNK)]


# ---------------------------------------------------------------- pack write

def pack_object_header(obj_type, size):
    """The type/size varint header of one pack entry."""
    code = TYPE_CODES[obj_type]
    out = bytearray()
    first = (code << 4) | (size & 0x0F)
    size >>= 4
    while size:
        out.append(first | 0x80)
        first = size & 0x7F
        size >>= 7
    out.append(first)
    return bytes(out)


def write_pack(objects):
    """Build a packfile (version 2, plain objects, no deltas).

    `objects` is an iterable of (obj_type, data) pairs. The trailer is
    SHA-1 over everything before it. An empty input yields the 32-byte
    empty pack (header + count 0 + trailer).
    """
    items = list(objects)
    parts = [b"PACK" + struct.pack(">II", 2, len(items))]
    for obj_type, data in items:
        parts.append(pack_object_header(obj_type, len(data)))
        parts.append(zlib.compress(data))
    body = b"".join(parts)
    return body + hashlib.sha1(body).digest()


def empty_pack():
    return write_pack(())


def pack_checksum(pack):
    """SHA-1 of every pack byte before the 20-byte trailer."""
    if len(pack) < 20:
        raise PackError("pack is shorter than its trailer")
    return hashlib.sha1(pack[:-20]).hexdigest()


def verify_pack_checksum(pack):
    if len(pack) < 20:
        return False
    return pack[-20:].hex() == pack_checksum(pack)


# ---------------------------------------------------------------- pack read

def _read_varint_size(buf, start):
    if start >= len(buf):
        raise PackError("pack entry has no size header")
    byte = buf[start]
    size = byte & 0x0F
    shift = 4
    pos = start + 1
    first = byte
    while byte & 0x80:
        if pos >= len(buf):
            raise PackError("pack entry size varint is truncated")
        byte = buf[pos]
        pos += 1
        size += (byte & 0x7F) << shift
        shift += 7
        if shift > 63:
            raise PackError("pack entry size varint is too long")
    return size, pos, first


def _read_ofs_delta_base(buf, start):
    if start >= len(buf):
        raise PackError("ofs-delta has no base offset")
    byte = buf[start]
    pos = start + 1
    value = byte & 0x7F
    while byte & 0x80:
        if pos >= len(buf):
            raise PackError("ofs-delta base offset is truncated")
        byte = buf[pos]
        pos += 1
        value = ((value + 1) << 7) + (byte & 0x7F)
    return value, pos


def _inflate_at(buf, offset, limit):
    """Inflate one zlib stream starting at offset; return (data, next)."""
    decomp = zlib.decompressobj()
    try:
        data = decomp.decompress(buf[offset:limit])
        data += decomp.flush()
    except zlib.error as exc:
        raise PackError("zlib stream at offset %d did not inflate: %s" % (offset, exc))
    if not decomp.eof:
        raise PackError("zlib stream at offset %d is truncated" % offset)
    consumed = len(buf[offset:limit]) - len(decomp.unused_data)
    if consumed <= 0:
        raise PackError("zlib stream at offset %d consumed no bytes" % offset)
    return data, offset + consumed


def _apply_delta(base, delta):
    """Apply a git delta instruction stream to a base object's bytes."""
    pos = 0

    def read_size():
        nonlocal pos
        value = 0
        shift = 0
        while True:
            if pos >= len(delta):
                raise PackError("delta size varint is truncated")
            byte = delta[pos]
            pos += 1
            value += (byte & 0x7F) << shift
            shift += 7
            if not (byte & 0x80):
                return value
            if shift > 63:
                raise PackError("delta size varint is too long")

    source_size = read_size()
    target_size = read_size()
    if source_size != len(base):
        raise PackError("delta expects a %d-byte base, got %d" % (source_size, len(base)))
    out = bytearray()
    while pos < len(delta):
        op = delta[pos]
        pos += 1
        if op & 0x80:
            copy_offset = 0
            copy_size = 0
            shift = 0
            for bit in range(4):
                if op & (1 << bit):
                    if pos >= len(delta):
                        raise PackError("delta copy offset is truncated")
                    copy_offset += delta[pos] << shift
                    pos += 1
                shift += 8
            shift = 0
            for bit in range(4, 7):
                if op & (1 << bit):
                    if pos >= len(delta):
                        raise PackError("delta copy size is truncated")
                    copy_size += delta[pos] << shift
                    pos += 1
                shift += 8
            if copy_size == 0:
                copy_size = 0x10000
            if copy_offset + copy_size > len(base):
                raise PackError("delta copy is out of the base")
            out += base[copy_offset:copy_offset + copy_size]
        elif op != 0:
            literal = op & 0x7F
            if pos + literal > len(delta):
                raise PackError("delta insert is truncated")
            out += delta[pos:pos + literal]
            pos += literal
        else:
            raise PackError("delta opcode 0 is invalid")
    if len(out) != target_size:
        raise PackError("delta produced %d of %d bytes" % (len(out), target_size))
    return bytes(out)


def read_pack(pack):
    """Read a packfile (version 2 or 3) and resolve every object.

    Returns {"version": int, "count": int, "objects": {oid: (type, data)},
    "delta_count": int}. Raises PackError on any structural problem,
    size mismatch, hash mismatch or trailer mismatch. Deltas (ofs/ref)
    are resolved recursively; a ref-delta base that is neither in the
    pack nor resolvable here is an error.
    """
    buf = bytes(pack)
    if len(buf) < 32:
        raise PackError("pack is %d bytes, too short" % len(buf))
    if buf[0:4] != b"PACK":
        raise PackError("pack does not start with PACK")
    version = struct.unpack(">I", buf[4:8])[0]
    if version not in (2, 3):
        raise PackError("unsupported pack version %d" % version)
    count = struct.unpack(">I", buf[8:12])[0]
    trailer_start = len(buf) - 20
    if buf[trailer_start:].hex() != pack_checksum(buf):
        raise PackError("pack trailer does not match its bytes")
    raw = []  # (offset, type_code, size, data, base_offset, base_oid)
    by_offset = {}
    offset = 12
    for _index in range(count):
        entry_offset = offset
        size, offset, first = _read_varint_size(buf, offset)
        type_code = (first >> 4) & 0x07
        base_offset = None
        base_oid = None
        if type_code == 6:
            rel, offset = _read_ofs_delta_base(buf, offset)
            if rel <= 0 or rel > entry_offset:
                raise PackError("ofs-delta at %d names an invalid base offset %d"
                                % (entry_offset, rel))
            base_offset = entry_offset - rel
        elif type_code == 7:
            if offset + 20 > trailer_start:
                raise PackError("ref-delta base id is truncated")
            base_oid = buf[offset:offset + 20].hex()
            offset += 20
        elif type_code not in CODE_TYPES:
            raise PackError("pack entry has unknown type code %d" % type_code)
        data, offset = _inflate_at(buf, offset, trailer_start)
        if len(data) != size:
            raise PackError("pack entry at %d declares %d bytes but inflated to %d"
                            % (entry_offset, size, len(data)))
        entry = (entry_offset, type_code, size, data, base_offset, base_oid)
        raw.append(entry)
        by_offset[entry_offset] = entry
    if offset != trailer_start:
        raise PackError("pack has %d stray bytes before its trailer"
                        % (trailer_start - offset))
    objects = {}
    resolved = {}  # offset -> (type, data, oid)
    by_oid = {}
    resolving = set()

    def resolve(entry):
        entry_offset = entry[0]
        if entry_offset in resolved:
            return resolved[entry_offset]
        if entry_offset in resolving:
            raise PackError("delta cycle at pack offset %d" % entry_offset)
        resolving.add(entry_offset)
        _off, type_code, _size, data, base_offset, base_oid = entry
        if type_code == 6:
            base_entry = by_offset.get(base_offset)
            if base_entry is None:
                raise PackError("ofs-delta base at offset %d is not in the pack"
                                % base_offset)
            base_type, base_data, _boid = resolve(base_entry)
            obj_type = base_type
            obj_data = _apply_delta(base_data, data)
        elif type_code == 7:
            base_entry = by_oid.get(base_oid)
            if base_entry is None:
                raise PackError("ref-delta base %s is not in the pack" % base_oid)
            base_type, base_data, _boid = resolve(base_entry)
            obj_type = base_type
            obj_data = _apply_delta(base_data, data)
        else:
            obj_type = CODE_TYPES[type_code]
            obj_data = data
        oid = sha1_obj(obj_type, obj_data)
        result = (obj_type, obj_data, oid)
        resolved[entry_offset] = result
        by_oid[oid] = entry
        resolving.discard(entry_offset)
        return result

    delta_count = 0
    for entry in raw:
        if entry[1] in (6, 7):
            delta_count += 1
        obj_type, obj_data, oid = resolve(entry)
        if oid not in objects:
            objects[oid] = (obj_type, obj_data)
    return {"version": version, "count": count, "objects": objects,
            "delta_count": delta_count}
