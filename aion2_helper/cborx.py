"""A small CBOR decoder that understands the cbor-x extensions used by aion2.th.gl.

The map files on ``cdn.th.gl`` are CBOR written by the JavaScript library
cbor-x with record structures and packed values switched on. Plain CBOR
decoders return those as opaque tags, so this module implements just what the
files use (no dependency needed):

- tag 51 ``[values, prefixes, suffixes, rump]``: a table of repeated values;
  the decoded document is ``rump``.
- simple value *n* (n < 16) and tag 6 *k*: references into that table
  (``values[n]`` and ``values[16 + 2k]`` / ``values[15 - 2k]``).
- tag 0xDFFF ``[id, keys, v1, v2, ...]``: defines record structure ``id`` and
  returns its first object; tag ``id`` ``[v1, v2, ...]`` returns later ones.
- tag 0xDFFE ``[id, keys1, keys2, ..., rest]``: defines several structures.
"""

from __future__ import annotations

import struct

RECORD_INLINE_ID = 0xDFFF
RECORD_DEFINITIONS_ID = 0xDFFE
PACKED_TABLE = 51
PACKED_REFERENCE = 6
MAX_DEPTH = 64

_BREAK = object()


class CBORError(ValueError):
    pass


class _Decoder:
    def __init__(self, data: bytes):
        self.data = memoryview(data)
        self.pos = 0
        self.packed: list | None = None
        self.structures: dict[int, list] = {}
        self.depth = 0

    # ───── low level ─────

    def _byte(self) -> int:
        if self.pos >= len(self.data):
            raise CBORError("unexpected end of data")
        b = self.data[self.pos]
        self.pos += 1
        return b

    def _take(self, n: int) -> memoryview:
        if n < 0 or self.pos + n > len(self.data):
            raise CBORError("unexpected end of data")
        out = self.data[self.pos : self.pos + n]
        self.pos += n
        return out

    def _arg(self, info: int) -> int | None:
        if info < 24:
            return info
        if info == 24:
            return self._byte()
        if info == 25:
            return struct.unpack(">H", self._take(2))[0]
        if info == 26:
            return struct.unpack(">I", self._take(4))[0]
        if info == 27:
            return struct.unpack(">Q", self._take(8))[0]
        if info == 31:
            return None  # indefinite length
        raise CBORError(f"invalid additional info {info}")

    def _array_length(self) -> int:
        head = self._byte()
        if head >> 5 != 4:
            raise CBORError("expected an array")
        n = self._arg(head & 0x1F)
        if n is None:
            raise CBORError("indefinite array where a record was expected")
        return n

    # ───── items ─────

    def read(self):
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise CBORError("nested too deeply")
        try:
            return self._read()
        finally:
            self.depth -= 1

    def _read(self):
        head = self._byte()
        major, info = head >> 5, head & 0x1F
        if major == 7:
            return self._simple(info)
        arg = self._arg(info)
        if major == 0:
            return arg
        if major == 1:
            return -1 - arg
        if major in (2, 3):
            if arg is None:
                parts = []
                while (item := self.read()) is not _BREAK:
                    parts.append(item)
                return b"".join(parts) if major == 2 else "".join(parts)
            raw = bytes(self._take(arg))
            return raw if major == 2 else raw.decode("utf-8")
        if major == 4:
            if arg is None:
                out = []
                while (item := self.read()) is not _BREAK:
                    out.append(item)
                return out
            return [self.read() for _ in range(arg)]
        if major == 5:
            out = {}
            if arg is None:
                while (key := self.read()) is not _BREAK:
                    out[_key(key)] = self.read()
                return out
            for _ in range(arg):
                key = self.read()
                out[_key(key)] = self.read()
            return out
        return self._tag(arg)

    def _simple(self, info: int):
        if info == 20:
            return False
        if info == 21:
            return True
        if info in (22, 23):
            return None
        if info == 25:
            return _half(struct.unpack(">H", self._take(2))[0])
        if info == 26:
            return struct.unpack(">f", self._take(4))[0]
        if info == 27:
            return struct.unpack(">d", self._take(8))[0]
        if info == 31:
            return _BREAK
        n = self._byte() if info == 24 else info
        if self.packed is not None and n < len(self.packed):
            return self.packed[n]
        raise CBORError(f"unknown simple value {n}")

    def _tag(self, tag: int):
        if tag in self.structures:
            keys = self.structures[tag]
            n = self._array_length()
            return {_key(keys[i]): self.read() for i in range(n)}
        if tag == RECORD_INLINE_ID:
            n = self._array_length()
            rid, keys = self.read(), self.read()
            self.structures[rid] = keys
            return {_key(keys[i - 2]): self.read() for i in range(2, n)}
        if tag == RECORD_DEFINITIONS_ID:
            n = self._array_length()
            rid = self.read()
            for _ in range(2, n):
                self.structures[rid] = self.read()
                rid += 1
            return self.read()
        if tag == PACKED_TABLE:
            if self._array_length() != 4:
                raise CBORError("packed table must be a 4 element array")
            values = self.read()
            if not isinstance(values, list) or not values:
                raise CBORError("empty packed table")
            prefixes, suffixes = self.read(), self.read()
            if prefixes or suffixes:
                raise CBORError("packed prefixes/suffixes are not supported")
            self.packed = values
            return self.read()
        if tag == PACKED_REFERENCE:
            k = self.read()
            if self.packed is None or not isinstance(k, int):
                raise CBORError("packed reference without a table")
            i = 16 + (2 * k if k >= 0 else -2 * k - 1)
            if i >= len(self.packed):
                raise CBORError("packed reference out of range")
            return self.packed[i]
        return self.read()  # any other tag: keep the value, drop the tag


def _key(k):
    return k if isinstance(k, (str, int, float, bool)) or k is None else repr(k)


def _half(h: int) -> float:
    exp = (h >> 10) & 0x1F
    mant = h & 0x3FF
    if exp == 0:
        val = mant * 2.0**-24
    elif exp == 31:
        val = float("inf") if mant == 0 else float("nan")
    else:
        val = (mant + 1024) * 2.0 ** (exp - 25)
    return -val if h & 0x8000 else val


def loads(data: bytes):
    """Decodes one cbor-x document."""
    return _Decoder(data).read()
