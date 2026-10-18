import struct


class Incomplete(Exception):
    pass


def mp_encode(value):
    out = bytearray()
    _enc(value, out)
    return bytes(out)


def _enc(v, out):
    if v is None:
        out.append(0xC0)
    elif v is True:
        out.append(0xC3)
    elif v is False:
        out.append(0xC2)
    elif isinstance(v, int):
        _enc_int(v, out)
    elif isinstance(v, float):
        out.append(0xCB)
        out += struct.pack(">d", v)
    elif isinstance(v, str):
        _enc_str(v, out)
    elif isinstance(v, list):
        _enc_array(v, out)
    elif isinstance(v, dict):
        _enc_dict(v, out)
    else:
        raise TypeError("cannot encode %s" % type(v).__name__)


def _enc_int(v, out):
    if v >= 0:
        if v < 0x80:
            out.append(v)
        elif v <= 0xFF:
            out.append(0xCC)
            out.append(v)
        elif v <= 0xFFFF:
            out.append(0xCD)
            out += struct.pack(">H", v)
        elif v <= 0xFFFFFFFF:
            out.append(0xCE)
            out += struct.pack(">I", v)
        else:
            out.append(0xCF)
            out += struct.pack(">Q", v)
    elif v >= -0x20:
        out.append(v & 0xFF)
    elif v >= -0x80:
        out.append(0xD0)
        out.append(v & 0xFF)
    elif v >= -0x8000:
        out.append(0xD1)
        out += struct.pack(">h", v)
    elif v >= -0x80000000:
        out.append(0xD2)
        out += struct.pack(">i", v)
    else:
        out.append(0xD3)
        out += struct.pack(">q", v)


def _enc_str(v, out):
    b = v.encode("utf-8")
    n = len(b)
    if n < 32:
        out.append(0xA0 | n)
    elif n < 0x100:
        out.append(0xD9)
        out.append(n)
    elif n < 0x10000:
        out.append(0xDA)
        out += struct.pack(">H", n)
    else:
        out.append(0xDB)
        out += struct.pack(">I", n)
    out += b


def _enc_array(v, out):
    n = len(v)
    if n < 0x10:
        out.append(0x90 | n)
    elif n < 0x10000:
        out.append(0xDC)
        out += struct.pack(">H", n)
    else:
        out.append(0xDD)
        out += struct.pack(">I", n)
    for item in v:
        _enc(item, out)


def _enc_dict(v, out):
    keys = list(v.keys())
    n = len(keys)
    if n < 0x10:
        out.append(0x80 | n)
    elif n < 0x10000:
        out.append(0xDE)
        out += struct.pack(">H", n)
    else:
        out.append(0xDF)
        out += struct.pack(">I", n)
    for k in keys:
        _enc_str(k, out)
        _enc(v[k], out)


class _Cur:
    __slots__ = ("buf", "off")

    def __init__(self, buf, off=0):
        self.buf = buf
        self.off = off


def mp_decode_one(buf, off=0):
    cur = _Cur(buf, off)
    value = _dec(cur)
    return value, cur.off


def _need(cur, n):
    if cur.off + n > len(cur.buf):
        raise Incomplete()


def _be_int(cur, n):
    _need(cur, n)
    v = int.from_bytes(cur.buf[cur.off: cur.off + n], "big")
    cur.off += n
    return v


def _dec(cur):
    _need(cur, 1)
    b = cur.buf[cur.off]
    cur.off += 1
    if b <= 0x7F:
        return b
    if b >= 0xE0:
        return b - 0x100
    if 0xA0 <= b <= 0xBF:
        return _dec_str(cur, b & 0x1F)
    if 0x90 <= b <= 0x9F:
        return _dec_array(cur, b & 0x0F)
    if 0x80 <= b <= 0x8F:
        return _dec_dict(cur, b & 0x0F)
    if b == 0xC0:
        return None
    if b == 0xC2:
        return False
    if b == 0xC3:
        return True
    if b == 0xC4:
        return _dec_bin(cur, _be_int(cur, 1))
    if b == 0xC5:
        return _dec_bin(cur, _be_int(cur, 2))
    if b == 0xC6:
        return _dec_bin(cur, _be_int(cur, 4))
    if b == 0xC7:
        return _dec_ext(cur, _be_int(cur, 1))
    if b == 0xC8:
        return _dec_ext(cur, _be_int(cur, 2))
    if b == 0xC9:
        return _dec_ext(cur, _be_int(cur, 4))
    if b == 0xCA:
        _need(cur, 4)
        v = struct.unpack_from(">f", cur.buf, cur.off)[0]
        cur.off += 4
        return v
    if b == 0xCB:
        _need(cur, 8)
        v = struct.unpack_from(">d", cur.buf, cur.off)[0]
        cur.off += 8
        return v
    if b == 0xCC:
        return _be_int(cur, 1)
    if b == 0xCD:
        return _be_int(cur, 2)
    if b == 0xCE:
        return _be_int(cur, 4)
    if b == 0xCF:
        return _be_int(cur, 8)
    if b == 0xD0:
        v = _be_int(cur, 1)
        return v - 0x100 if v >= 0x80 else v
    if b == 0xD1:
        v = _be_int(cur, 2)
        return v - 0x10000 if v >= 0x8000 else v
    if b == 0xD2:
        v = _be_int(cur, 4)
        return v - 0x100000000 if v >= 0x80000000 else v
    if b == 0xD3:
        v = _be_int(cur, 8)
        return v - 0x10000000000000000 if v >= 0x8000000000000000 else v
    if b == 0xD4:
        return _dec_ext(cur, 1)
    if b == 0xD5:
        return _dec_ext(cur, 2)
    if b == 0xD6:
        return _dec_ext(cur, 4)
    if b == 0xD7:
        return _dec_ext(cur, 8)
    if b == 0xD8:
        return _dec_ext(cur, 16)
    if b == 0xD9:
        return _dec_str(cur, _be_int(cur, 1))
    if b == 0xDA:
        return _dec_str(cur, _be_int(cur, 2))
    if b == 0xDB:
        return _dec_str(cur, _be_int(cur, 4))
    if b == 0xDC:
        return _dec_array(cur, _be_int(cur, 2))
    if b == 0xDD:
        return _dec_array(cur, _be_int(cur, 4))
    if b == 0xDE:
        return _dec_dict(cur, _be_int(cur, 2))
    if b == 0xDF:
        return _dec_dict(cur, _be_int(cur, 4))
    raise ValueError("bad msgpack byte 0x%02x" % b)


def _dec_str(cur, n):
    _need(cur, n)
    v = bytes(cur.buf[cur.off: cur.off + n]).decode("utf-8", "replace")
    cur.off += n
    return v


def _dec_bin(cur, n):
    _need(cur, n)
    v = bytes(cur.buf[cur.off: cur.off + n])
    cur.off += n
    return v


def _dec_ext(cur, n):
    _need(cur, n + 1)
    t = cur.buf[cur.off]
    data = bytes(cur.buf[cur.off + 1: cur.off + 1 + n])
    cur.off += n + 1
    return {"__nvimcp_ext": t, "data": data.hex()}


def _dec_array(cur, n):
    return [_dec(cur) for _ in range(n)]


def _dec_dict(cur, n):
    d = {}
    for _ in range(n):
        k = _dec(cur)
        d[k if isinstance(k, (str, int)) else str(k)] = _dec(cur)
    return d
