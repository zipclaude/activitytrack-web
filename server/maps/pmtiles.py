# SPDX-License-Identifier: GPL-3.0-or-later
"""
PMTiles v3 (https://github.com/protomaps/PMTiles) for the offline maps: read an
archive, local or far away (HTTP range requests, so the 100 GB of the planet are
never downloaded), cut the tiles of an area, drop the heavy layers, write a small
archive. Standard library only: the server runs on Debian packages (ARCHITECTURE.md §2).

The layers of a vector tile are independent blocks of protobuf, so dropping one is
copying the others: no decoding of the geometry.
"""

import bisect
import gzip
import hashlib
import json
import os
import struct
import tempfile
import time
import urllib.error
import urllib.request

from . import tiles as geo

HEADER_LEN = 127
ATTRIBUTION = "© OpenStreetMap contributors · Protomaps"
# the layers the offline maps leave out: the green areas alone are ~85 % of the bytes
DEFAULT_DROP = ("landuse", "pois", "buildings", "natural", "landcover")
# a directory plus the header must fit the first 16 KiB, which readers fetch at once
ROOT_MAX = 16384 - HEADER_LEN
# ranges closer than this are read as one request
GAP = 128 * 1024
# a read larger than this is a sign of a wrong file, not of a basemap
MAX_READ = 64 * 1024 * 1024

NONE, GZIP = 1, 2
MVT = 1


class PMTilesError(Exception):
    pass


# ---- varints and protobuf blocks ------------------------------------------
def read_varint(b, i):
    r = s = 0
    while True:
        c = b[i]
        i += 1
        r |= (c & 0x7F) << s
        if not c & 0x80:
            return r, i
        s += 7


def write_varint(n):
    out = bytearray()
    while True:
        c = n & 0x7F
        n >>= 7
        if n:
            out.append(c | 0x80)
        else:
            out.append(c)
            return bytes(out)


def fields(b):
    """The top-level fields of a protobuf message: (number, wire type, value)."""
    i = 0
    while i < len(b):
        k, i = read_varint(b, i)
        f, w = k >> 3, k & 7
        if w == 0:
            v, i = read_varint(b, i)
        elif w == 2:
            ln, i = read_varint(b, i)
            v = b[i:i + ln]
            i += ln
        elif w == 1:
            v = b[i:i + 8]
            i += 8
        elif w == 5:
            v = b[i:i + 4]
            i += 4
        else:
            raise PMTilesError(f"wire type {w}")
        yield f, w, v


def layer_name(layer):
    for f, _, v in fields(layer):
        if f == 1:
            return v.decode("utf-8", "replace")
    return ""


def layer_names(tile):
    return [layer_name(v) for f, _, v in fields(tile) if f == 3]


def strip_layers(tile, drop):
    """The tile without the layers named in drop (an uncompressed MVT)."""
    out = bytearray()
    for f, w, v in fields(tile):
        if f == 3 and layer_name(v) in drop:
            continue
        if w == 2:
            out += write_varint(f << 3 | w) + write_varint(len(v)) + v
        elif w == 0:
            out += write_varint(f << 3 | w) + write_varint(v)
        else:
            raise PMTilesError(f"unexpected wire type {w} in a tile")
    return bytes(out)


def decompress(data, kind):
    if kind == GZIP:
        try:
            return gzip.decompress(data)
        except (OSError, EOFError) as e:
            raise PMTilesError(f"bad gzip data: {e}") from e
    if kind in (0, NONE):
        return data
    raise PMTilesError(f"unsupported compression {kind} (only none and gzip)")


def gz(data):
    # mtime 0: the same tile always gives the same bytes, so the files can be compared
    return gzip.compress(data, 9, mtime=0)


# ---- byte sources ----------------------------------------------------------
class FileSource:
    def __init__(self, path):
        self.path = path

    def read(self, offset, length):
        if length > MAX_READ:
            raise PMTilesError("read too large")
        with open(self.path, "rb") as f:
            f.seek(offset)
            data = f.read(length)
        if len(data) != length:
            raise PMTilesError("short read: the archive is truncated")
        return data


class HttpSource:
    """A remote archive read by byte ranges; the server must answer 206."""

    def __init__(self, url, user_agent="ActivityTrack-Web", timeout=30, retries=3):
        self.url, self.user_agent, self.timeout, self.retries = url, user_agent, timeout, retries
        self.requests = 0

    def read(self, offset, length):
        if length > MAX_READ:
            raise PMTilesError("read too large")
        if length == 0:
            return b""
        last = None
        for attempt in range(self.retries):
            req = urllib.request.Request(self.url, headers={
                "Range": f"bytes={offset}-{offset + length - 1}", "User-Agent": self.user_agent,
            })
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    if r.status != 206:
                        raise PMTilesError(f"{self.url}: the server does not answer byte ranges (HTTP {r.status})")
                    data = r.read(length + 1)
                self.requests += 1
            except PMTilesError:
                raise
            except (urllib.error.URLError, OSError) as e:
                last = e
                time.sleep(1 + attempt)
                continue
            if len(data) != length:
                raise PMTilesError("short read from the server")
            return data
        raise PMTilesError(f"cannot read {self.url}: {last}")


def source_for(where, user_agent="ActivityTrack-Web"):
    """A file path or an http(s) URL."""
    if where.startswith(("http://", "https://")):
        return HttpSource(where, user_agent)
    return FileSource(where)


# ---- archive ---------------------------------------------------------------
class Archive:
    def __init__(self, source):
        self.source = source
        h = source.read(0, HEADER_LEN)
        if h[:7] != b"PMTiles" or h[7] != 3:
            raise PMTilesError("not a PMTiles v3 archive")
        (self.root_off, self.root_len, self.meta_off, self.meta_len, self.leaf_off, self.leaf_len,
         self.data_off, self.data_len) = struct.unpack_from("<8Q", h, 8)
        self.int_comp, self.tile_comp, self.tile_type = h[97], h[98], h[99]
        self.min_zoom, self.max_zoom = h[100], h[101]
        self.bounds = [v / 1e7 for v in struct.unpack_from("<4i", h, 102)]
        if self.tile_type != MVT:
            raise PMTilesError("not a vector tile archive")
        if self.int_comp not in (NONE, GZIP) or self.tile_comp not in (NONE, GZIP):
            raise PMTilesError("only none and gzip compression are supported")
        self._dirs = {}
        self.root = self.directory(self.root_off, self.root_len)

    def metadata(self):
        raw = decompress(self.source.read(self.meta_off, self.meta_len), self.int_comp)
        try:
            return json.loads(raw or b"{}")
        except ValueError as e:
            raise PMTilesError("bad metadata") from e

    def directory(self, off, ln):
        key = (off, ln)
        if key in self._dirs:
            return self._dirs[key]
        b = decompress(self.source.read(off, ln), self.int_comp)
        n, i = read_varint(b, 0)
        ids, runs, lens, offs = [], [], [], []
        last = 0
        for _ in range(n):
            v, i = read_varint(b, i)
            last += v
            ids.append(last)
        for _ in range(n):
            v, i = read_varint(b, i)
            runs.append(v)
        for _ in range(n):
            v, i = read_varint(b, i)
            lens.append(v)
        end = 0
        for k in range(n):
            v, i = read_varint(b, i)
            o = end if v == 0 and k > 0 else v - 1
            offs.append(o)
            end = o + lens[k]
        self._dirs[key] = (ids, runs, lens, offs)
        return self._dirs[key]

    def locate(self, tid):
        """(offset, length) of the tile data, or None when the archive has no such tile."""
        d = self.root
        for _ in range(4):
            ids, runs, lens, offs = d
            i = bisect.bisect_right(ids, tid) - 1
            if i < 0:
                return None
            if runs[i] > 0:
                return (offs[i], lens[i]) if tid < ids[i] + runs[i] else None
            d = self.directory(self.leaf_off + offs[i], lens[i])
        raise PMTilesError("directories nested too deep")

    def fetch(self, locations):
        """{tile id: raw tile bytes} for {tile id: (offset, length)}: neighbouring tiles in one request."""
        out = {}
        order = sorted(locations.items(), key=lambda kv: kv[1])
        group, start, end = [], 0, 0

        def flush():
            if not group:
                return
            blob = self.source.read(self.data_off + start, end - start)
            for tid, (off, ln) in group:
                out[tid] = blob[off - start:off - start + ln]

        for tid, (off, ln) in order:
            if group and off - end <= GAP and off + ln - start <= 8 * 1024 * 1024:
                group.append((tid, (off, ln)))
                end = max(end, off + ln)
                continue
            flush()
            group, start, end = [(tid, (off, ln))], off, off + ln
        flush()
        return out

    def tile(self, tid):
        loc = self.locate(tid)
        if loc is None:
            return None
        return decompress(self.fetch({tid: loc})[tid], self.tile_comp)


# ---- writing ---------------------------------------------------------------
def serialize_directory(entries):
    """entries: (tile id, run length, length, offset) sorted by id; gzip, as the spec says."""
    b = bytearray(write_varint(len(entries)))
    last = 0
    for tid, _, _, _ in entries:
        b += write_varint(tid - last)
        last = tid
    for _, run, _, _ in entries:
        b += write_varint(run)
    for _, _, ln, _ in entries:
        b += write_varint(ln)
    prev_end = None
    for _, _, ln, off in entries:
        b += write_varint(0 if prev_end is not None and off == prev_end else off + 1)
        prev_end = off + ln
    return gz(bytes(b))


def write(path, blobs, box, min_zoom, max_zoom, metadata):
    """
    A PMTiles archive at path from {tile id: uncompressed MVT}; the tiles are gzipped
    and identical ones stored once. Written to a temporary file and moved into place.
    """
    data, seen, entries = bytearray(), {}, []
    for tid in sorted(blobs):
        z = gz(blobs[tid])
        key = hashlib.sha1(z).digest()
        if key not in seen:
            seen[key] = (len(data), len(z))
            data += z
        off, ln = seen[key]
        entries.append((tid, 1, ln, off))
    root = serialize_directory(entries)
    if len(root) > ROOT_MAX:
        raise PMTilesError(f"the directory is too large ({len(root)} bytes): choose a lower zoom or a smaller area")
    meta = gz(json.dumps(metadata, separators=(",", ":")).encode())
    head = bytearray(HEADER_LEN)
    head[0:7] = b"PMTiles"
    head[7] = 3
    root_off = HEADER_LEN
    meta_off = root_off + len(root)
    data_off = meta_off + len(meta)
    struct.pack_into("<8Q", head, 8, root_off, len(root), meta_off, len(meta), data_off, 0, data_off, len(data))
    struct.pack_into("<3Q", head, 72, len(entries), len(entries), len(seen))
    head[96], head[97], head[98], head[99] = 1, GZIP, GZIP, MVT
    head[100], head[101] = min_zoom, max_zoom
    struct.pack_into("<4i", head, 102, *(int(round(v * 1e7)) for v in box))
    head[118] = min(max_zoom, 8)
    struct.pack_into("<2i", head, 119, int(round((box[0] + box[2]) / 2 * 1e7)), int(round((box[1] + box[3]) / 2 * 1e7)))
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".part")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(head + root + meta + data)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return len(entries)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def extract(archive, box, max_zoom, path, drop=DEFAULT_DROP, progress=None):
    """
    The tiles of archive within box (min lon, min lat, max lon, max lat), zoom 0 to
    max_zoom, without the layers in drop, as a new archive at path. Returns the number of
    tiles written. progress(done, total) is called now and then.
    """
    wanted = {geo.tile_id(z, x, y): (z, x, y) for z, x, y in geo.tiles_of(box, max_zoom)}
    locations = {}
    for n, tid in enumerate(wanted):
        loc = archive.locate(tid)
        if loc is not None:
            locations[tid] = loc
        if progress and n % 200 == 0:
            progress(n, len(wanted) * 2)
    blobs = {}
    ordered = sorted(locations, key=lambda t: locations[t])
    # in batches, so that a map of thousands of tiles is not held whole as it is read
    for i in range(0, len(ordered), 400):
        batch = ordered[i:i + 400]
        raw = archive.fetch({t: locations[t] for t in batch})
        for tid in batch:
            blobs[tid] = strip_layers(decompress(raw[tid], archive.tile_comp), set(drop))
        if progress:
            progress(len(wanted) + i + len(batch), len(wanted) * 2)
    if not blobs:
        raise PMTilesError("the archive has no tile in this area")
    meta = dict(archive.metadata())
    meta["vector_layers"] = [v for v in meta.get("vector_layers", []) if v.get("id") not in drop]
    meta["attribution"] = ATTRIBUTION
    meta.pop("tilestats", None)
    return write(path, blobs, box, 0, max_zoom, meta)


def verify(path):
    """Reads every tile of an archive back; returns {layer: bytes} over all tiles."""
    a = Archive(FileSource(path))
    layers = {}
    ids, runs, lens, offs = a.root
    seen = {}
    for tid in ids:
        loc = a.locate(tid)
        tile = decompress(a.source.read(a.data_off + loc[0], loc[1]), a.tile_comp)
        for f, _, v in fields(tile):
            if f == 3:
                name = layer_name(v)
                layers[name] = layers.get(name, 0) + len(v)
        seen[tid] = True
    if len(seen) != len(ids):
        raise PMTilesError("duplicate tile ids")
    return layers
