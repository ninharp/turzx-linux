"""Minimal MS-NRBF (.NET BinaryFormatter) reader.

TURZX ``.turtheme`` files are BinaryFormatter-serialized ``UsbMonitorL.Theme``
objects.  This module turns such a stream into plain Python objects:

* classes  -> ``NObj`` (dict subclass with ``.cls`` = .NET class name)
* arrays   -> ``list`` (byte[] -> ``bytes``)
* strings / primitives -> Python equivalents

Only reading is supported; nothing is ever executed.
"""
from __future__ import annotations

import io
import struct
from typing import Any


class NObj(dict):
    """A deserialized .NET object: field-name -> value, plus the class name."""

    def __init__(self, cls: str, lib: str | None = None):
        super().__init__()
        self.cls = cls
        self.lib = lib

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{self.cls} {dict.__repr__(self)}>"

    def get_member(self, name: str, default: Any = None) -> Any:
        """Look up a field, also accepting auto-property backing fields."""
        if name in self:
            return self[name]
        key = f"<{name}>k__BackingField"
        if key in self:
            return self[key]
        return default


class _Ref:
    __slots__ = ("id",)

    def __init__(self, id_: int):
        self.id = id_


# PrimitiveTypeEnumeration
_PRIM = {
    1: ("?", 1),   # Boolean
    2: ("B", 1),   # Byte
    3: (None, 0),  # Char (utf-8, variable)
    5: (None, 0),  # Decimal (length-prefixed string)
    6: ("<d", 8),  # Double
    7: ("<h", 2),  # Int16
    8: ("<i", 4),  # Int32
    9: ("<q", 8),  # Int64
    10: ("b", 1),  # SByte
    11: ("<f", 4),  # Single
    12: ("<q", 8),  # TimeSpan (ticks)
    13: ("<Q", 8),  # DateTime (ticks + kind)
    14: ("<H", 2),  # UInt16
    15: ("<I", 4),  # UInt32
    16: ("<Q", 8),  # UInt64
    17: (None, 0),  # Null
    18: (None, 0),  # String
}


class NrbfReader:
    def __init__(self, data: bytes):
        self.f = io.BytesIO(data)
        self.objects: dict[int, Any] = {}
        self.classes: dict[int, tuple] = {}  # object id -> (name, members, binfo, lib)
        self.libraries: dict[int, str] = {}
        self.root_id: int | None = None

    # -- low level -------------------------------------------------------
    def _read(self, n: int) -> bytes:
        b = self.f.read(n)
        if len(b) != n:
            raise EOFError("truncated NRBF stream")
        return b

    def _u8(self) -> int:
        return self._read(1)[0]

    def _i32(self) -> int:
        return struct.unpack("<i", self._read(4))[0]

    def _string(self) -> str:
        length = 0
        shift = 0
        while True:
            b = self._u8()
            length |= (b & 0x7F) << shift
            if not b & 0x80:
                break
            shift += 7
        return self._read(length).decode("utf-8", "replace")

    def _prim(self, ptype: int) -> Any:
        if ptype == 18:
            return self._string()
        if ptype == 5:
            return self._string()
        if ptype == 3:
            # UTF-8 char: read first byte to know length
            b0 = self._u8()
            n = 1 if b0 < 0x80 else 2 if b0 < 0xE0 else 3 if b0 < 0xF0 else 4
            return (bytes([b0]) + self._read(n - 1)).decode("utf-8", "replace")
        fmt, size = _PRIM[ptype]
        v = struct.unpack(fmt, self._read(size))[0]
        if ptype == 13:
            v &= 0x3FFFFFFFFFFFFFFF
        return v

    # -- records ---------------------------------------------------------
    def parse(self) -> Any:
        while True:
            rt = self._u8()
            if rt == 11:  # MessageEnd
                break
            self._record(rt, top=True)
        root = self._resolve(self.objects.get(self.root_id))
        return root

    def _record(self, rt: int, top: bool = False) -> Any:
        if rt == 0:  # SerializedStreamHeader
            self.root_id = self._i32()
            self._read(12)
            return None
        if rt == 12:  # BinaryLibrary
            lid = self._i32()
            self.libraries[lid] = self._string()
            return self._record(self._u8(), top)
        if rt in (1, 2, 3, 4, 5):
            return self._class_record(rt)
        if rt == 6:  # BinaryObjectString
            oid = self._i32()
            s = self._string()
            self.objects[oid] = s
            return s
        if rt == 7:
            return self._binary_array()
        if rt == 8:  # MemberPrimitiveTyped
            return self._prim(self._u8())
        if rt == 9:
            return _Ref(self._i32())
        if rt == 10:
            return None
        if rt == 13:
            return _Nulls(self._u8())
        if rt == 14:
            return _Nulls(self._i32())
        if rt == 15:  # ArraySinglePrimitive
            oid = self._i32()
            n = self._i32()
            pt = self._u8()
            if pt == 2:
                val: Any = self._read(n)
            else:
                val = [self._prim(pt) for _ in range(n)]
            self.objects[oid] = val
            return val
        if rt in (16, 17):  # ArraySingleObject / ArraySingleString
            oid = self._i32()
            n = self._i32()
            val = self._read_values(n)
            self.objects[oid] = val
            return val
        raise ValueError(f"unsupported NRBF record type {rt} at {self.f.tell() - 1}")

    def _read_values(self, n: int) -> list:
        out: list = []
        while len(out) < n:
            v = self._record(self._u8())
            if isinstance(v, _Nulls):
                out.extend([None] * v.n)
            else:
                out.append(v)
        return out

    def _class_record(self, rt: int) -> NObj:
        oid = self._i32()
        if rt == 1:  # ClassWithId
            meta_id = self._i32()
            name, members, binfo, lib = self.classes[meta_id]
        else:
            name = self._string()
            count = self._i32()
            members = [self._string() for _ in range(count)]
            binfo = None
            if rt in (4, 5):
                btypes = [self._u8() for _ in range(count)]
                extra = []
                for bt in btypes:
                    if bt == 0:
                        extra.append(self._u8())
                    elif bt == 3:
                        extra.append(self._string())
                    elif bt == 4:
                        extra.append((self._string(), self._i32()))
                    elif bt == 7:
                        extra.append(self._u8())
                    else:
                        extra.append(None)
                binfo = list(zip(btypes, extra))
            lib = None
            if rt in (3, 5):
                lib = self.libraries.get(self._i32())
        self.classes[oid] = (name, members, binfo, lib)
        obj = NObj(name, lib)
        self.objects[oid] = obj
        for i, m in enumerate(members):
            if binfo is not None and binfo[i][0] == 0:
                obj[m] = self._prim(binfo[i][1])
            else:
                v = self._record(self._u8())
                if isinstance(v, _Nulls):  # only valid for n == 1 inside a class
                    v = None
                obj[m] = v
        return obj

    def _binary_array(self) -> Any:
        oid = self._i32()
        atype = self._u8()
        rank = self._i32()
        lengths = [self._i32() for _ in range(rank)]
        if atype in (3, 4, 5):
            [self._i32() for _ in range(rank)]  # lower bounds
        bt = self._u8()
        extra = None
        if bt == 0 or bt == 7:
            extra = self._u8()
        elif bt == 3:
            extra = self._string()
        elif bt == 4:
            extra = (self._string(), self._i32())
        total = 1
        for ln in lengths:
            total *= ln
        if bt == 0:
            if extra == 2:
                val: Any = self._read(total)
            else:
                val = [self._prim(extra) for _ in range(total)]
        else:
            val = self._read_values(total)
        self.objects[oid] = val
        return val

    # -- reference resolution -------------------------------------------
    def _resolve(self, root: Any) -> Any:
        seen: set[int] = set()

        def fix(v: Any) -> Any:
            if isinstance(v, _Ref):
                return self.objects.get(v.id)
            return v

        stack = [root]
        while stack:
            o = stack.pop()
            if id(o) in seen:
                continue
            seen.add(id(o))
            if isinstance(o, dict):
                for k, v in o.items():
                    v = fix(v)
                    o[k] = v
                    if isinstance(v, (dict, list)):
                        stack.append(v)
            elif isinstance(o, list):
                for i, v in enumerate(o):
                    v = fix(v)
                    o[i] = v
                    if isinstance(v, (dict, list)):
                        stack.append(v)
        return root


class _Nulls:
    __slots__ = ("n",)

    def __init__(self, n: int):
        self.n = n


def loads(data: bytes) -> Any:
    return NrbfReader(data).parse()


def load(path: str) -> Any:
    with open(path, "rb") as fh:
        return loads(fh.read())


def unwrap_list(v: Any) -> list:
    """Turn a serialized ``List<T>`` (``_items``/``_size``) into a Python list."""
    if isinstance(v, NObj) and "_items" in v:
        items = v["_items"] or []
        return list(items[: v.get("_size", len(items))])
    if isinstance(v, list):
        return v
    return []
