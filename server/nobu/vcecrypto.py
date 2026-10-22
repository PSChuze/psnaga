"""VCE encrypted-connection primitives (the world connection), stdlib only.

Wire format as the PS2 client speaks it. Addresses are client VAs.

Handshake (client role 0x12c180, server role 0x12bbb0 = VCE's own reference):
  C>S  8 raw bytes: u32be VCE major version (0, 0x1399e0), u32be key length (conn+0x40 = 8)
  S>C  u32be status (0 = ok; 3 = version, 4 = key length mismatch), then three
       u32be-length-prefixed ASCII hex strings g, p, Y = g^y mod p          (0x12bd88..0x12bef0)
  C>S  u32be length + X = g^x mod p (hex)                                   (0x12c4b0..0x12c530)
  K = Y^x mod p = X^y mod p. Numbers are OpenSSL BIGNUMs: BN_hex2bn in, BN_bn2hex out
  (0x12b040; in the overlay: upper-case, whole bytes, no leading zero byte). Key (0x129d80
  -> 0x13a260): the first conn+0x40 = 8 bytes of the hex text of K, as bytes. Both
  directions use the same key (send ctx conn+0x84, receive ctx conn+0x44).

Records after the handshake (encoder 0x12f760, decoder around 0x12ebf0):
  u32be padded_len, u32be plain_len, padded_len bytes of Blowfish-ECB ciphertext.
  padded_len = plain_len rounded down to 8, plus 8 (the client always adds 1..8 zero bytes);
  the decoder accepts any multiple of 8 >= plain_len, <= 0x7ff8. Blowfish is standard
  (0x129720 key schedule, 0x129520 encrypt, 0x129620 decrypt, pi tables at 0x416300/0x416350)
  but each 8-byte block is loaded as two LITTLE-endian 32-bit words (L = bytes 0..3).
  The plaintext is the ordinary VCE stream: u16be length + payload frames, which may span records.
"""
import secrets

# VCE's built-in DH group (server role 0x12bbb0 reads g from 0x4612c8 and p from 0x3ddf10).
DEFAULT_G = "2"
DEFAULT_P = ("f488fd584e49dbcd20b49de49107366b336c380d451d0f7c88b31c7c5b2d8ef6f3c923c043f0a55b"
             "188d8ebb558cb85d38d334fd7c175743a31d186cde33212cb52aff3ce1b1294018118d7c84a70a"
             "72d686c40319c807297aca950cd9969fabd00a509b0246d3083d66a45d419f9c7cbd894b221926"
             "baaba25ec355e92f78c7")
VCE_MAJOR = 0
KEY_LEN = 8                 # world connection parameter block +0x18 (0x1930e0) -> conn+0x40
MAX_RECORD = 0x7FF0         # encoder chunk limit (0x12f7b8)


# ---------------------------------------------------------------------------- bignum text

def bn2hex(n):
    """OpenSSL BN_bn2hex: upper-case, whole bytes, '0' for zero."""
    if n == 0:
        return "0"
    return n.to_bytes((n.bit_length() + 7) // 8, "big").hex().upper()


def hex2bn(s):
    s = s.strip()
    return int(s, 16) if s else 0


def key_from_shared(k, keylen=KEY_LEN):
    """0x13a260(dst, keylen, bn2hex(K)): hex pairs from the start of the text -> bytes."""
    text = bn2hex(k)
    raw = bytes.fromhex(text if len(text) % 2 == 0 else text + "0")
    return (raw + bytes(keylen))[:keylen]


def dh_secret():
    """A 64-hex-digit secret, like 0x12e560(buf, 0x40)."""
    return secrets.token_hex(32)


def modexp_text(base_hex, exp_hex, mod_hex):
    """0x12b040(base, mod, exp): BN_mod_exp on hex text, result as BN_bn2hex text."""
    return bn2hex(pow(hex2bn(base_hex), hex2bn(exp_hex), hex2bn(mod_hex)))


# ---------------------------------------------------------------------------- Blowfish

def _pi_hex_words(nwords):
    """Fractional hex digits of pi as 32-bit words (the Blowfish P-array and S-box init)."""
    bits = nwords * 32 + 64

    def atan_inv(x):
        one = 1 << bits
        total = term = one // x
        x2, n, sign = x * x, 1, -1
        while term:
            term //= x2
            n += 2
            total += sign * (term // n)
            sign = -sign
        return total

    pi = 16 * atan_inv(5) - 4 * atan_inv(239)          # pi * 2^bits
    frac = pi - (3 << bits)
    frac >>= 64                                          # drop guard bits
    out = []
    for i in range(nwords):
        shift = (nwords - 1 - i) * 32
        out.append((frac >> shift) & 0xFFFFFFFF)
    return out


_PI = _pi_hex_words(18 + 4 * 256)
P_INIT = _PI[:18]
S_INIT = [_PI[18 + 256 * i:18 + 256 * (i + 1)] for i in range(4)]
assert P_INIT[0] == 0x243F6A88 and P_INIT[17] == 0x8979FB1B
assert S_INIT[0][0] == 0xD1310BA6 and S_INIT[3][255] == 0x3AC372E6


class Blowfish:
    def __init__(self, key):
        if not 1 <= len(key) <= 56:
            raise ValueError("Blowfish key must be 1..56 bytes")
        self.P = list(P_INIT)
        self.S = [list(s) for s in S_INIT]
        j = 0
        for i in range(18):                              # 0x1297c4: key bytes cycled, big-endian
            d = 0
            for _ in range(4):
                d = ((d << 8) | key[j]) & 0xFFFFFFFF
                j = (j + 1) % len(key)
            self.P[i] ^= d
        L = R = 0
        for i in range(0, 18, 2):
            L, R = self.encrypt_words(L, R)
            self.P[i], self.P[i + 1] = L, R
        for s in self.S:
            for i in range(0, 256, 2):
                L, R = self.encrypt_words(L, R)
                s[i], s[i + 1] = L, R

    def _f(self, x):
        S = self.S
        h = (S[0][x >> 24] + S[1][(x >> 16) & 0xFF]) & 0xFFFFFFFF
        return ((h ^ S[2][(x >> 8) & 0xFF]) + S[3][x & 0xFF]) & 0xFFFFFFFF

    def encrypt_words(self, L, R):
        P = self.P
        for i in range(16):
            L ^= P[i]
            R ^= self._f(L)
            L, R = R, L
        L, R = R, L
        R ^= P[16]
        L ^= P[17]
        return L, R

    def decrypt_words(self, L, R):
        P = self.P
        for i in range(17, 1, -1):
            L ^= P[i]
            R ^= self._f(L)
            L, R = R, L
        L, R = R, L
        R ^= P[1]
        L ^= P[0]
        return L, R

    # standard big-endian block (for test vectors)
    def encrypt_block_be(self, block):
        L, R = self.encrypt_words(int.from_bytes(block[:4], "big"), int.from_bytes(block[4:], "big"))
        return L.to_bytes(4, "big") + R.to_bytes(4, "big")

    # VCE: each block is two little-endian words (0x12f91c / 0x12eda0)
    def encrypt_vce(self, data):
        out = bytearray()
        for o in range(0, len(data), 8):
            L, R = self.encrypt_words(int.from_bytes(data[o:o + 4], "little"),
                                      int.from_bytes(data[o + 4:o + 8], "little"))
            out += L.to_bytes(4, "little") + R.to_bytes(4, "little")
        return bytes(out)

    def decrypt_vce(self, data):
        out = bytearray()
        for o in range(0, len(data), 8):
            L, R = self.decrypt_words(int.from_bytes(data[o:o + 4], "little"),
                                      int.from_bytes(data[o + 4:o + 8], "little"))
            out += L.to_bytes(4, "little") + R.to_bytes(4, "little")
        return bytes(out)


# ---------------------------------------------------------------------------- records

class WireError(ValueError):
    pass


def seal(cipher, plain):
    """Plaintext stream bytes -> list of records (client layout: always 1..8 pad bytes)."""
    out = []
    for o in range(0, len(plain), MAX_RECORD):
        chunk = plain[o:o + MAX_RECORD]
        padded = (len(chunk) // 8) * 8 + 8
        body = chunk + bytes(padded - len(chunk))
        out.append(padded.to_bytes(4, "big") + len(chunk).to_bytes(4, "big") + cipher.encrypt_vce(body))
    return b"".join(out)


class RecordReader:
    """Incremental decoder for the encrypted record stream -> plaintext bytes."""

    def __init__(self, cipher):
        self.cipher = cipher
        self.buf = bytearray()

    def feed(self, data):
        self.buf += data
        out = bytearray()
        while len(self.buf) >= 8:
            padded = int.from_bytes(self.buf[0:4], "big")
            plain = int.from_bytes(self.buf[4:8], "big")
            if padded % 8 or padded > 0x7FF8 or plain > padded:
                raise WireError(f"bad record header padded={padded} plain={plain}")
            if len(self.buf) < 8 + padded:
                break
            body = bytes(self.buf[8:8 + padded])
            del self.buf[:8 + padded]
            out += self.cipher.decrypt_vce(body)[:plain]
        return bytes(out)


# ---------------------------------------------------------------------------- handshake

def client_hello(keylen=KEY_LEN, major=VCE_MAJOR):
    return major.to_bytes(4, "big") + keylen.to_bytes(4, "big")


def parse_client_hello(data):
    if len(data) < 8:
        raise WireError("short client hello")
    return int.from_bytes(data[:4], "big"), int.from_bytes(data[4:8], "big")


def _lp(text):
    b = text.encode("ascii")
    return len(b).to_bytes(4, "big") + b


def server_hello(g, p, y_hex, status=0):
    return status.to_bytes(4, "big") + _lp(g) + _lp(p) + _lp(y_hex)


def parse_server_hello(data):
    """Client side (0x12c180). Returns (status, g, p, Y, consumed) or None if incomplete."""
    if len(data) < 16:
        return None
    status = int.from_bytes(data[0:4], "big")
    off, parts = 4, []
    for limit in (0x3F, 0x3FF, 0x3FF):
        if len(data) < off + 4:
            return None
        n = int.from_bytes(data[off:off + 4], "big")
        if n >= limit:
            raise WireError("hello field too long")
        if len(data) < off + 4 + n:
            return None
        parts.append(data[off + 4:off + 4 + n].decode("ascii"))
        off += 4 + n
    return (status, *parts, off)


def parse_lp(data):
    """u32be length + text; returns (text, consumed) or None if incomplete."""
    if len(data) < 4:
        return None
    n = int.from_bytes(data[:4], "big")
    if n >= 0x3FF:
        raise WireError("X too long")
    if len(data) < 4 + n:
        return None
    return data[4:4 + n].decode("ascii"), 4 + n


class ServerHandshake:
    """Server role, same steps as VCE's 0x12bbb0."""

    def __init__(self, g=DEFAULT_G, p=DEFAULT_P, secret=None):
        self.g, self.p = g, p
        self.y = secret or dh_secret()
        self.Y = modexp_text(g, self.y, p)

    def hello(self, keylen):
        status = 0 if keylen == KEY_LEN else 4             # 0x12bcf4: conn+0x40 must match
        return server_hello(self.g, self.p, self.Y, status)

    def finish(self, x_hex):
        k = hex2bn(modexp_text(x_hex, self.y, self.p))
        return key_from_shared(k)


class ClientHandshake:
    """Python model of the client role 0x12c180 (used by the tests)."""

    def __init__(self, secret=None):
        self.x = secret or dh_secret()

    def answer(self, g, p, Y):
        X = modexp_text(g, self.x, p)
        k = hex2bn(modexp_text(Y, self.x, p))
        return _lp(X), key_from_shared(k)
