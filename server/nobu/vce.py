"""VCE wire format as used by the Nobunaga Online PS2 client on the connect server (TCP 9070).

Wire format as the PS2 client speaks it. Addresses are client VAs.

Framing (client receive 0x12f460, client send 0x12f560):
    u16 big-endian payload length, then the payload. payload[0] is the command byte.

Field codecs (VCE generated helpers, all big-endian):
    put u8 0x128970 / u16 0x1289e0 / u32 0x128a60
    get u8 0x128ea0 / u16 0x128f10 / u32 0x128f90
    get bytes 0x129010: variable-length length header (0x1287d0) then the bytes
        header b < 0xFD -> length b; 0xFD -> u32 length; 0xFE -> u32 flag (must be 0) + u32 length;
        0xFF -> error. The reader rejects a field longer than its buffer (-2).

Connect-server protocol class (vtable 0x451180, object 0x4b3af0):
    client -> server  0x14 (0x1f5790)  u32, u32
                      0x15 (0x1f5830)  u32 n, n x {u32 code, u32 zero, u32 version}  = version check
                      0x16 (0x1f5970)  u32, u32, u32 [, u32 if nonzero]
    server -> client  dispatcher 0x1f5aa0 accepts 0x1e..0x23 only (jump table 0x4233a0)
                      0x22 (0x1f60d0)  version-check reply, see VersionReply
"""
import struct

CMD_REQ_14 = 0x14
CMD_VERSION_CHECK = 0x15
CMD_REQ_16 = 0x16
CMD_SERVER_ENTRY = 0x1E
CMD_SERVER_COUNT = 0x1F
CMD_SERVER_END = 0x20
CMD_MESSAGE = 0x21
CMD_VERSION_REPLY = 0x22
CMD_REPLY_23 = 0x23

# Client constants (globals gp-0x7e30 / gp-0x7e34, gp = 0x4691f0), read from the install at boot
# by 0x291480. They are what the client puts in its 0x15 request.
CLIENT_CODE = 1000201
CLIENT_VERSION = 20003

# 0x22 status word (reply+0x08), as handled by the update-check state machine 0x28e570
STATUS_OK = 0            # no patch
STATUS_PATCH = 1         # a patch manifest follows (LATESTVER/HTTPGET/... text, 0x29ac30)
STATUS_ERR_M1 = -1       # client shows error -3
STATUS_ERR_M2 = -2       # client shows error -4
STATUS_ERR_M99 = -0x63   # client shows error -1
STATUS_MAINT = -0x64     # special case at 0x28e940 (not decoded)


class WireError(ValueError):
    pass


# ---------------------------------------------------------------------------- framing

def frame(payload):
    """One TCP frame: u16be length + payload (0x12f560 / 0x12f460)."""
    if len(payload) > 0xFFFF:
        raise WireError("payload too long for a u16 frame")
    return struct.pack(">H", len(payload)) + bytes(payload)


class FrameReader:
    """Incremental splitter for the u16be-length framed stream."""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data):
        self.buf += data
        out = []
        while len(self.buf) >= 2:
            n = struct.unpack_from(">H", self.buf, 0)[0]
            if len(self.buf) < 2 + n:
                break
            out.append(bytes(self.buf[2:2 + n]))
            del self.buf[:2 + n]
        return out


# ---------------------------------------------------------------------------- field codecs

def put_u8(v):
    return struct.pack(">B", v & 0xFF)


def put_i8(v):
    return struct.pack(">b", v)


def put_u16(v):
    return struct.pack(">H", v & 0xFFFF)


def put_u32(v):
    return struct.pack(">I", v & 0xFFFFFFFF)


def put_len(n):
    """Length header as decoded by 0x1287d0."""
    if n < 0xFD:
        return bytes([n])
    return b"\xfd" + struct.pack(">I", n)


def put_bytes(b):
    b = bytes(b)
    return put_len(len(b)) + b


class Reader:
    def __init__(self, data, off=0):
        self.data = bytes(data)
        self.off = off

    def _take(self, n):
        if self.off + n > len(self.data):
            raise WireError(f"short read: need {n} at {self.off}, have {len(self.data) - self.off}")
        b = self.data[self.off:self.off + n]
        self.off += n
        return b

    def u8(self):
        return self._take(1)[0]

    def i8(self):
        return struct.unpack(">b", self._take(1))[0]

    def u16(self):
        return struct.unpack(">H", self._take(2))[0]

    def u32(self):
        return struct.unpack(">I", self._take(4))[0]

    def i32(self):
        return struct.unpack(">i", self._take(4))[0]

    def length(self):
        b = self.u8()
        if b < 0xFD:
            return b
        if b == 0xFD:
            return self.u32()
        if b == 0xFE:
            flag = self.u32()
            n = self.u32()
            if flag:
                raise WireError("0xFE length header with nonzero flag")
            return n
        raise WireError("null (0xFF) length header")

    def bytes(self, maxlen=None):
        n = self.length()
        if maxlen is not None and n > maxlen:
            raise WireError(f"field of {n} bytes exceeds client buffer {maxlen}")
        return self._take(n)

    def rest(self):
        return self.data[self.off:]

    def done(self):
        return self.off == len(self.data)


# ---------------------------------------------------------------------------- messages

def decode_version_check(payload):
    """0x15, built by 0x1f5830 from 0x18de58: [(code, zero, version), ...]."""
    r = Reader(payload)
    if r.u8() != CMD_VERSION_CHECK:
        raise WireError("not a 0x15 version check")
    n = r.u32()
    if n > 64:
        raise WireError(f"implausible entry count {n}")
    entries = [(r.u32(), r.u32(), r.u32()) for _ in range(n)]
    if not r.done():
        raise WireError(f"{len(r.rest())} trailing bytes")
    return entries


def encode_version_check(entries):
    out = put_u8(CMD_VERSION_CHECK) + put_u32(len(entries))
    for code, zero, ver in entries:
        out += put_u32(code) + put_u32(zero) + put_u32(ver)
    return out


# Client buffer sizes for the 0x22 byte fields (0x1f6238: 0x2000, 0x1f62a0: 0x100)
REPLY_TEXT_MAX = 0x2000
REPLY_HOST_MAX = 0x100


def encode_version_reply(a, b, status, total, seq, text=b"", host=b""):
    """0x22, parsed by 0x1f60d0 into a struct handed to the update-check handler 0x28e570.

    a, b    u32 -> struct +0x0/+0x4, copied to 0x51dca0 and only read by an error formatter
    status  u32 -> +0x8, see STATUS_*
    total   u8  -> +0xc   chunk count / size in KB (0x28e450 allocates total << 10)
    seq     u8  -> +0xd   chunk number; status 0 with seq > 0 means "no notice text"
    text    bytes (max 0x2000) -> +0xe, manifest (status 1) or notice text (status 0, seq <= 0)
    host    bytes (max 0x100)  -> +0x2014, copied to the login object (+0x172) and used as the
            %s of "https://%s?a=%s" (0x28f3a0): the HTTPS login host/path.
    """
    text = text.encode("ascii") if isinstance(text, str) else bytes(text)
    host = host.encode("ascii") if isinstance(host, str) else bytes(host)
    if len(text) > REPLY_TEXT_MAX:
        raise WireError("text longer than the client buffer (0x2000)")
    if len(host) >= REPLY_HOST_MAX:
        raise WireError("host longer than the client buffer (0x100 incl. NUL)")
    return (put_u8(CMD_VERSION_REPLY) + put_u32(a) + put_u32(b) + put_u32(status)
            + put_i8(total) + put_i8(seq) + put_bytes(text) + put_bytes(host))


def decode_version_reply(payload):
    r = Reader(payload)
    if r.u8() != CMD_VERSION_REPLY:
        raise WireError("not a 0x22 reply")
    d = dict(a=r.u32(), b=r.u32(), status=r.i32(), total=r.i8(), seq=r.i8(),
             text=r.bytes(REPLY_TEXT_MAX), host=r.bytes(REPLY_HOST_MAX))
    if not r.done():
        raise WireError(f"{len(r.rest())} trailing bytes")
    return d


def up_to_date_reply(entries, login_host):
    """The reply that takes the client down the no-patch path.

    0x28e6a0 (state 0 = waiting): status 0 and seq > 0 -> strcpy(login host) and state 7.
    State 7 (0x29b040) posts net sub 0xd (no notice pages -> sub 8), disconnects, and sub 8
    (0x18d7d8) hands the host to the login object and moves the client to the login step.
    """
    code, _, ver = entries[0] if entries else (CLIENT_CODE, 0, CLIENT_VERSION)
    return encode_version_reply(code, ver, STATUS_OK, 1, 1, b"", login_host)


# ---------------------------------------------------------------------------- world list
# Client 0x14 (0x1f5790 via 0x195120, sent after the login at 0x1879c8 / 0x18b3a4):
#   u32 code (gp-0x7e30 = 1000201), u32 flags of the chosen account (0x290700, default 0x40000000)
# Server answer (main handler 0x188530, cmds handled at 0x18b258):
#   0x1f u16 count                         (0x1f5b20; forwarded to the UI)
#   0x1e world entry, once per world       (0x1f5cc8 parse, 0x18ba70 stores up to 16 at net+0x54c)
#   0x20 end of list                       (0x18b400: client drops the connection, list cached)
# Client 0x16 (0x1f5970 via 0x195160, net command 0xb at 0x18e058): u32 entry.key, u32 entry.id,
#   u32 entry.sub [, u32]. Server 0x23 (0x1f5b88): u32 key, u16 id, u16 sub, u8 status
#   -> entry+0x8c = status, UI refresh of that world (0x18b4c8).

WORLD_NAME_MAX = 0x7E      # wire bytes (UTF-8, converted to SJIS into 0x40 bytes by 0x1a22d0)
WORLD_HOST_MAX = 0x40      # client buffer incl. NUL
WORLD_BLOB_MAX = 0x400

# World status byte (entry+0x8c). HYPOTHESIS: indexes UIMSG 1.32..1.35
# (normal, busy, heavy, offline).
WORLD_NORMAL, WORLD_BUSY, WORLD_HEAVY, WORLD_OFFLINE = 0, 1, 2, 3


def decode_req_14(payload):
    r = Reader(payload)
    if r.u8() != CMD_REQ_14:
        raise WireError("not a 0x14")
    code, flags = r.u32(), r.u32()
    return code, flags


def encode_world_count(n):
    return put_u8(CMD_SERVER_COUNT) + put_u16(n)


def encode_world_entry(wid, name, host, port, status=WORLD_NORMAL, key=0, sub=0,
                       b48=0, w8e=0, blob=b"", b49=None):
    """0x1e, field order of the parse at 0x1f5cc8 (struct offsets in the client entry)."""
    name = name.encode("utf-8") if isinstance(name, str) else bytes(name)
    host = host.encode("ascii") if isinstance(host, str) else bytes(host)
    if len(name) > WORLD_NAME_MAX:
        raise WireError("world name too long (0x7e bytes)")
    if len(host) >= WORLD_HOST_MAX:
        raise WireError("host too long (0x3f bytes)")
    if len(blob) > WORLD_BLOB_MAX:
        raise WireError("blob too long (0x400 bytes)")
    out = (put_u8(CMD_SERVER_ENTRY) + put_u16(wid)       # +0x00 id (0x18bb70 looks worlds up by it)
           + put_u16(sub) + put_u32(key)                  # +0x02, +0x04 (echoed in 0x16)
           + put_bytes(name)                              # +0x08 name
           + put_u8(b48)                                  # +0x48
           + put_bytes(host)                              # +0x4a host (0x18dfa8 connects to it)
           + put_u16(port)                                # +0x8a port
           + put_u8(status)                               # +0x8c status
           + put_u16(w8e)                                 # +0x8e
           + put_bytes(blob))                             # +0x90, length at +0x490
    if b49 is not None:
        out += put_u8(b49)                                # +0x49, optional trailing byte
    return out


def decode_world_entry(payload):
    r = Reader(payload)
    if r.u8() != CMD_SERVER_ENTRY:
        raise WireError("not a 0x1e")
    d = dict(id=r.u16(), sub=r.u16(), key=r.u32(), name=r.bytes(WORLD_NAME_MAX), b48=r.u8(),
             host=r.bytes(WORLD_HOST_MAX), port=r.u16(), status=r.u8(), w8e=r.u16(),
             blob=r.bytes(WORLD_BLOB_MAX))
    d["b49"] = r.u8() if not r.done() else None
    if not r.done():
        raise WireError(f"{len(r.rest())} trailing bytes")
    return d


def encode_world_end():
    return put_u8(CMD_SERVER_END)


def decode_req_16(payload):
    r = Reader(payload)
    if r.u8() != CMD_REQ_16:
        raise WireError("not a 0x16")
    vals = [r.u32(), r.u32(), r.u32()]
    if len(r.rest()) >= 4:
        vals.append(r.u32())
    return vals


def encode_world_status(key, wid, sub, status):
    """0x23 (0x1f5b88): u32 key, u16 id, u16 sub, u8 status."""
    return put_u8(CMD_REPLY_23) + put_u32(key) + put_u16(wid) + put_u16(sub) + put_u8(status)


def world_list(worlds):
    """Payloads answering 0x14: 0x1f count, 0x1e per world, 0x20."""
    if len(worlds) > 16:
        raise WireError("the client keeps at most 16 worlds (0x18ba74)")
    return ([encode_world_count(len(worlds))] + [encode_world_entry(**w) for w in worlds]
            + [encode_world_end()])


def parse_worlds(spec, default_host="127.0.0.1", default_port=9100):
    """NOBU_WORLDS: "Name@host:port,Name2@host:port" -> list of world dicts (ids from 1)."""
    worlds = []
    for i, item in enumerate(x for x in spec.split(",") if x.strip()):
        name, _, addr = item.strip().partition("@")
        host, _, port = (addr or default_host).partition(":")
        worlds.append(dict(wid=i + 1, name=name, host=host or default_host,
                           port=int(port) if port else default_port))
    return worlds


def describe(payload):
    """One-line decode for the log; never raises."""
    if not payload:
        return "empty payload"
    cmd = payload[0]
    try:
        if cmd == CMD_VERSION_CHECK:
            ents = decode_version_check(payload)
            return "0x15 version check " + ", ".join(
                f"(code={c}, zero={z}, version={v})" for c, z, v in ents)
        if cmd == CMD_VERSION_REPLY:
            d = decode_version_reply(payload)
            return (f"0x22 version reply a={d['a']} b={d['b']} status={d['status']} "
                    f"total={d['total']} seq={d['seq']} text={d['text']!r} host={d['host']!r}")
        if cmd == CMD_REQ_14:
            code, flags = decode_req_14(payload)
            return f"0x14 world-list request code={code} flags={flags:#x}"
        if cmd == CMD_REQ_16:
            return "0x16 world request " + " ".join(f"{v:#x}" for v in decode_req_16(payload))
        if cmd == CMD_SERVER_COUNT:
            return f"0x1f world count {Reader(payload, 1).u16()}"
        if cmd == CMD_SERVER_ENTRY:
            d = decode_world_entry(payload)
            return (f"0x1e world id={d['id']} name={d['name'].decode('utf-8', 'replace')!r} "
                    f"host={d['host'].decode('ascii', 'replace')}:{d['port']} status={d['status']} "
                    f"key={d['key']:#x} sub={d['sub']} b48={d['b48']} w8e={d['w8e']} "
                    f"blob={len(d['blob'])}B")
        if cmd == CMD_SERVER_END:
            return "0x20 end of world list"
        if cmd == CMD_REPLY_23:
            r = Reader(payload, 1)
            return (f"0x23 world status key={r.u32():#x} id={r.u16()} sub={r.u16()} "
                    f"status={r.u8()}")
    except WireError as e:
        return f"cmd {cmd:#04x} (malformed: {e})"
    return f"cmd {cmd:#04x} ({len(payload)} bytes, not decoded)"
