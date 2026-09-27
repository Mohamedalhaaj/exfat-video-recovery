#!/usr/bin/env python3
"""exfatrec: read-only recovery of deleted camera videos from exFAT memory cards.

    scan     list live and deleted files; explain what sits at a byte offset
    map      classify every cluster and find orphan video headers
    plan     turn an orphan header or a deleted entry into a list of cluster runs
    preview  decode frames of every clip found into one HTML page, before recovering any
    extract  copy a plan's clusters into a new file on another disk, then trim it
    trim     cut the cluster slack after the last valid MP4 box
    verify   check every indexed video/metadata sample against the file's index

The card (or image) is only ever opened with O_RDONLY. Nothing is written to it.
Standard library only; `verify` also needs ffprobe/ffmpeg.
"""
import argparse
import array
import bisect
import json
import os
import re
import shlex
import shutil
import stat
import struct
import subprocess
import sys
import time
from typing import NoReturn, Optional, Tuple

__version__ = "1.0.0"

ALIGN = 4096  # read alignment: a multiple of both 512-byte and 4K-native sectors
BLK = 64  # clusters per sequential read
RTMD_SIG = b"\x00\x1c\x01\x00"  # first bytes of a Sony real-time-metadata sample
NAL_CODECS = {b"avc1", b"avc3", b"hvc1", b"hev1"}
TOP_LEVEL = {b"ftyp", b"uuid", b"free", b"skip", b"wide", b"mdat", b"moov", b"meta", b"udta", b"pdin", b"moof", b"mfra"}
MOOV_CHILDREN = {b"mdia", b"minf", b"stbl"}


def die(msg) -> NoReturn:
    sys.exit(f"exfatrec: {msg}")


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def gb(n):
    return f"{n / 1e9:.2f} GB"


def printable(b):
    return "".join(chr(x) if 32 <= x < 127 else "." for x in b)


def chown_to_sudo_user(fd_or_path):
    """Files written under sudo should belong to the person who ran sudo. Never fatal."""
    uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if not (uid and gid and hasattr(os, "geteuid") and os.geteuid() == 0):
        return
    try:
        if isinstance(fd_or_path, int):
            os.fchown(fd_or_path, int(uid), int(gid))
        else:
            os.chown(fd_or_path, int(uid), int(gid), follow_symlinks=False)
    except OSError as e:
        log(f"warning: could not hand {fd_or_path} to the sudo user: {e}")


def write_json(path, obj):
    """Write JSON without following a symlink planted at `path`."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o644)
    except OSError as e:
        die(f"cannot write {path}: {e.strerror} (a symlink there is refused on purpose)")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
        f.flush()
        chown_to_sudo_user(f.fileno())


# ---------------------------------------------------------------- device ----

class Device:
    """Aligned, read-only access to a raw device or a disk image."""

    def __init__(self, path):
        self.path = path
        try:
            self.fd = os.open(path, os.O_RDONLY)
        except PermissionError:
            die(f"permission denied on {path}: raw devices need sudo")
        except FileNotFoundError:
            die(f"no such device or image: {path}")
        mode = os.fstat(self.fd).st_mode
        self.is_device = stat.S_ISBLK(mode) or stat.S_ISCHR(mode)

    def read(self, off, n):
        a = off - off % ALIGN
        e = off + n
        e += (-e) % ALIGN
        out, pos = [], a
        while pos < e:
            b = os.pread(self.fd, min(8 << 20, e - pos), pos)
            if not b:
                break
            out.append(b)
            pos += len(b)
        data = b"".join(out)
        return data[off - a: off - a + n]


def fs_kind(boot):
    if boot[3:11] == b"EXFAT   ":
        return "exFAT"
    if boot[3:11] == b"NTFS    ":
        return "NTFS"
    if boot[82:90] == b"FAT32   " or boot[54:59] in (b"FAT12", b"FAT16"):
        return "FAT"
    return None


def find_partition_offset(dev):
    """Byte offset of the first exFAT volume: bare volume, MBR or GPT; 512 or 4096-byte sectors."""
    s0 = dev.read(0, 512)
    kind = fs_kind(s0)
    if kind == "exFAT":
        return 0
    if kind:
        die(f"this is a {kind} volume, not exFAT (wrong disk? FAT32 cards are not supported)")
    if s0[510:512] != b"\x55\xaa":
        die("no exFAT volume and no partition table found: is this the right disk?")
    found = []
    types = [s0[0x1BE + 16 * i + 4] for i in range(4)]
    if 0xEE in types:
        for lba_size in (512, 4096):
            hdr = dev.read(lba_size, 512)
            if hdr[:8] != b"EFI PART":
                continue
            lba, count, esize = struct.unpack_from("<QII", hdr, 72)
            table = dev.read(lba * lba_size, count * esize)
            for i in range(count):
                e = table[i * esize:(i + 1) * esize]
                if len(e) == esize and e[:16] != bytes(16):
                    found.append((struct.unpack_from("<Q", e, 32)[0], lba_size))
            break
        else:
            die("protective MBR without a GPT header: pass --part-offset (start LBA x sector size)")
    else:
        for i in range(4):
            e = s0[0x1BE + 16 * i: 0x1BE + 16 * (i + 1)]
            if e[4]:
                start = struct.unpack_from("<I", e, 8)[0]
                found += [(start, 512), (start, 4096)]
    kinds = []
    for start, lba_size in found:
        boot = dev.read(start * lba_size, 512)
        k = fs_kind(boot)
        if k == "exFAT" and (lba_size == 512 or 1 << boot[0x6C] == lba_size):
            return start * lba_size
        if k:
            kinds.append(k)
    if kinds:
        die(f"the card holds {', '.join(sorted(set(kinds)))}, not exFAT (FAT32 cards are not supported)")
    die("partition table found, but no exFAT partition: is this the right disk? (or pass --part-offset)")


def resolve_part(dev, arg):
    return find_partition_offset(dev) if arg == "auto" else int(arg)


def base_disk(dev_path):
    """/dev/rdisk4 or /dev/disk4s1 -> disk4; /dev/sdb1 -> sdb; /dev/mmcblk0p1 -> mmcblk0."""
    name = os.path.basename(os.path.realpath(dev_path))
    if name.startswith("rdisk"):
        name = name[1:]
    m = re.match(r"(disk\d+|mmcblk\d+|nvme\d+n\d+|loop\d+|[a-z]+?)(s\d+|p\d+|\d+)?$", name)
    return m.group(1) if m else name


def disk_of_dir(path):
    try:
        line = subprocess.run(["df", "-P", path], capture_output=True, text=True, check=True).stdout.splitlines()[-1]
    except (OSError, subprocess.CalledProcessError, IndexError):
        return None
    return line.split()[0]


def refuse_if_on_card(dev, out_path):
    """Never let an output file land on the card being recovered."""
    if not dev.is_device:
        return
    outdir = os.path.dirname(os.path.abspath(out_path)) or "."
    target = disk_of_dir(outdir)
    if target is None:
        die(f"could not tell which disk {outdir} is on; refusing to risk writing to the card")
    if base_disk(target) == base_disk(dev.path):
        die(f"{outdir} is on the card itself; writing there would destroy the data being recovered")


# ----------------------------------------------------------------- exFAT ----

def decode_ts(v, ms10, utc):
    """exFAT timestamp -> 'YYYY-MM-DD hh:mm:ss [UTC±hh:mm]' (local time as stored)."""
    if not v:
        return None
    s = "%04d-%02d-%02d %02d:%02d:%02d" % (
        1980 + (v >> 25), (v >> 21) & 0xF, (v >> 16) & 0x1F,
        (v >> 11) & 0x1F, (v >> 5) & 0x3F, (v & 0x1F) * 2 + ms10 // 100)
    if utc & 0x80:
        o = utc & 0x7F
        if o & 0x40:
            o -= 0x80
        m = o * 15
        s += " UTC%s%02d:%02d" % ("+" if m >= 0 else "-", abs(m) // 60, abs(m) % 60)
    return s


class ExFAT:
    def __init__(self, dev, part_off):
        self.dev = dev
        bs = dev.read(part_off, 512)
        if bs[3:11] != b"EXFAT   ":
            die(f"no exFAT boot sector at byte {part_off}")
        vol_len, fat_off, _fat_len, heap_off, clus_cnt, self.root = struct.unpack_from("<QIIIII", bs, 0x48)
        self.serial = "%08X" % struct.unpack_from("<I", bs, 0x64)[0]
        self.bps = 1 << bs[0x6C]
        self.cs = self.bps << bs[0x6D]
        self.part_off = part_off
        self.heap_abs = part_off + heap_off * self.bps
        self.part_end = part_off + vol_len * self.bps
        self.maxc = clus_cnt + 2
        self.heap_end = self.cl_off(self.maxc)
        self.fat = array.array("I")
        self.fat.frombytes(dev.read(part_off + fat_off * self.bps, self.maxc * 4))
        if sys.byteorder != "little":
            self.fat.byteswap()
        self.records = []
        self._bitmap_entry: Optional[Tuple[int, int]] = None
        self._seen = set()
        self._deleted_dirs = []
        # Live tree first, so a stale deleted entry can never hide a live folder with the same cluster.
        self._walk(self.root, 0, False, "", 0, False)
        while self._deleted_dirs:
            self._walk(*self._deleted_dirs.pop(0))
        fc, length = self._bitmap()
        self.bitmap = self.read_runs(self.to_runs(self.chain(fc)), length)
        runs = []
        for r in self.records:
            if not r["deleted"] and r["size"]:
                for s, e in self.runs_for(r["first_cluster"], r["size"], r["nofatchain"]):
                    runs.append((s, e, r["path"]))
        runs.sort()
        self.live_runs = runs
        self._starts = [r[0] for r in runs]

    def _bitmap(self) -> Tuple[int, int]:
        if self._bitmap_entry is None:
            die("allocation bitmap entry not found in the root directory")
        return self._bitmap_entry

    def geometry(self):
        return {"part_offset": self.part_off, "serial": self.serial, "bytes_per_sector": self.bps,
                "cluster_size": self.cs, "cluster_count": self.maxc - 2, "heap_abs": self.heap_abs,
                "partition_end": self.part_end}

    def cl_off(self, c):
        return self.heap_abs + (c - 2) * self.cs

    def chain(self, fc, maxn=None):
        out, c, seen = [], fc, set()
        while 2 <= c < self.maxc and c not in seen:
            out.append(c)
            seen.add(c)
            if maxn and len(out) >= maxn:
                break
            c = self.fat[c]
        return out

    @staticmethod
    def to_runs(clusters):
        runs = []
        for c in clusters:
            if runs and runs[-1][1] == c:
                runs[-1][1] = c + 1
            else:
                runs.append([c, c + 1])
        return runs

    def runs_for(self, fc, size, nofat):
        n = -(-size // self.cs)
        if n == 0 or not (2 <= fc < self.maxc):
            return []
        if nofat:
            return [[fc, min(fc + n, self.maxc)]]
        return self.to_runs(self.chain(fc, n))

    def read_runs(self, runs, length=0):
        buf = b"".join(self.dev.read(self.cl_off(s), (e - s) * self.cs) for s, e in runs)
        return buf[:length] if length else buf

    def allocated(self, c):
        i = c - 2
        return (self.bitmap[i >> 3] >> (i & 7)) & 1 if 0 <= i < (self.maxc - 2) else 1

    def count_allocated(self, runs):
        return sum(self.allocated(c) for s, e in runs for c in range(s, e))

    def owner(self, c):
        i = bisect.bisect_right(self._starts, c) - 1
        if i >= 0 and self.live_runs[i][0] <= c < self.live_runs[i][1]:
            return self.live_runs[i][2]
        return None

    def owners(self, runs, limit=6):
        hits = []
        for s, e in runs:
            i = max(bisect.bisect_right(self._starts, s) - 1, 0)
            while i < len(self.live_runs) and self.live_runs[i][0] < e:
                _rs, re_, p = self.live_runs[i]
                if re_ > s and p not in hits:
                    hits.append(p)
                    if len(hits) >= limit:
                        return hits
                i += 1
        return hits

    def _walk(self, fc, length, nofat, path, depth, in_deleted):
        if fc in self._seen or depth > 32 or not (2 <= fc < self.maxc):
            return
        self._seen.add(fc)
        if in_deleted:
            runs = [[fc, min(fc + max(1, -(-length // self.cs)), self.maxc)]]
        elif length:
            runs = self.runs_for(fc, length, nofat)
        else:
            runs = self.to_runs(self.chain(fc))
        for r in self._parse_dir(self.read_runs(runs, length), path, in_deleted):
            if not (0 <= r["size"] <= self.part_end - self.part_off):
                continue
            self.records.append(r)
            if r["is_dir"] and 0 < r["size"] <= (64 << 20):
                args = (r["first_cluster"], r["size"], r["nofatchain"], r["path"], depth + 1, r["deleted"])
                if r["deleted"]:
                    self._deleted_dirs.append(args)
                else:
                    self._walk(*args)

    def _parse_dir(self, buf, path, in_deleted):
        recs, n, i = [], len(buf) // 32, 0
        while i < n:
            e = buf[i * 32:(i + 1) * 32]
            t = e[0]
            if t == 0:
                break
            if t == 0x81 and self._bitmap_entry is None and not in_deleted:
                self._bitmap_entry = struct.unpack_from("<IQ", e, 20)
            if t in (0x85, 0x05):
                sc = e[1]
                sec = [buf[(i + k) * 32:(i + k + 1) * 32] for k in range(1, sc + 1) if i + k < n]
                if len(sec) == sc and sc >= 2 and sec[0][0] in (0xC0, 0x40):
                    attrs = struct.unpack_from("<H", e, 4)[0]
                    ct, mt = struct.unpack_from("<II", e, 8)
                    st = sec[0]
                    raw = b"".join(s2[2:32] for s2 in sec[1:] if s2[0] in (0xC1, 0x41))
                    name = raw[:2 * st[3]].decode("utf-16-le", "replace")  # NameLength counts UTF-16 units
                    recs.append({
                        "path": f"{path}/{name}", "name": name,
                        "deleted": t == 0x05 or in_deleted,
                        "is_dir": bool(attrs & 0x10),
                        "size": struct.unpack_from("<Q", st, 24)[0],
                        "valid_size": struct.unpack_from("<Q", st, 8)[0],
                        "first_cluster": struct.unpack_from("<I", st, 20)[0],
                        "nofatchain": bool(st[1] & 2),
                        "ctime": decode_ts(ct, e[20], e[22]), "mtime": decode_ts(mt, e[21], e[23])})
                    # A deleted set advances one slot so reused slots parse as their own entries.
                    i += 1 + sc if t == 0x85 else 1
                    continue
            i += 1
        return recs


def open_fs(a):
    dev = Device(a.device)
    part = resolve_part(dev, a.part_offset)
    log(f"reading exFAT at byte {part:,} of {a.device} (read-only)...")
    return dev, ExFAT(dev, part)


# ------------------------------------------------------------- MP4 boxes ----

def walk_boxes(read, start, end, max_boxes=64):
    """Top-level MP4 boxes from `start`; stops at the first implausible header."""
    boxes, pos = [], start
    while len(boxes) < max_boxes and pos + 8 <= end:
        h = read(pos, 16)
        if not h or len(h) < 8:
            break
        size, typ = struct.unpack(">I4s", h[:8])
        hdr = 8
        if size == 1:
            if len(h) < 16:
                break
            size, hdr = struct.unpack(">Q", h[8:16])[0], 16
        if not all(32 <= x < 127 for x in typ):
            break
        box = {"at": pos, "type": typ.decode("ascii"), "size": size, "hdr": hdr}
        boxes.append(box)
        if size == 0:
            box["note"] = "size 0 = runs to end of file (recording never finalized?)"
            break
        if size < hdr:
            break
        pos += size
    return boxes


def header_info(cluster):
    """What an ftyp cluster declares: brand and the byte span ftyp..end of mdat."""
    boxes = walk_boxes(lambda o, n: cluster[o:o + n], 0, len(cluster), 16)
    types = [b["type"] for b in boxes]
    info = {"brand": printable(cluster[8:12]), "boxes": types}
    if "mdat" in types:
        mdat = boxes[types.index("mdat")]
        if mdat["size"]:
            info["declared_span"] = mdat["at"] + mdat["size"]
        else:
            info["unfinalized"] = True
        if "moov" in types[:types.index("mdat")]:
            moov = boxes[types.index("moov")]
            info["moov_first"] = [moov["at"], moov["size"]]
    # Sony header-last shape: the header fills exactly one cluster and ends in the mdat header.
    info["one_cluster_header"] = bool(boxes) and boxes[-1]["type"] == "mdat" and \
        boxes[-1]["at"] + boxes[-1]["hdr"] == len(cluster)
    return info


def iter_children(buf, start, end):
    pos = start
    while pos + 8 <= end:
        size, typ = struct.unpack(">I4s", buf[pos:pos + 8])
        hdr = 8
        if size == 1:
            if pos + 16 > end:
                return
            size, hdr = struct.unpack(">Q", buf[pos + 8:pos + 16])[0], 16
        elif size == 0:
            size = end - pos
        if size < hdr or pos + size > end:
            return
        yield typ, pos + hdr, pos + size
        pos += size


def moov_tracks(moov):
    """[(handler, codec, chunk_offsets)] for each trak in a moov box."""
    hdr = 16 if struct.unpack(">I", moov[:4])[0] == 1 else 8
    tracks = []
    for typ, s, e in iter_children(moov, hdr, len(moov)):
        if typ != b"trak":
            continue
        found = {"handler": None, "codec": None, "offsets": []}

        def walk(s0, e0, parent):
            for t, cs_, ce in iter_children(moov, s0, e0):
                if t in MOOV_CHILDREN:
                    walk(cs_, ce, t)
                elif t == b"hdlr" and parent == b"mdia":  # QuickTime minf holds a second (data) hdlr
                    found["handler"] = moov[cs_ + 8:cs_ + 12]
                elif t == b"stsd":
                    found["codec"] = moov[cs_ + 12:cs_ + 16]
                elif t in (b"stco", b"co64"):
                    w, fmt = (4, ">I") if t == b"stco" else (8, ">Q")
                    n = struct.unpack(">I", moov[cs_ + 4:cs_ + 8])[0]
                    n = min(n, (ce - cs_ - 8) // w)
                    found["offsets"] = [struct.unpack(fmt, moov[cs_ + 8 + w * i:cs_ + 8 + w * (i + 1)])[0]
                                        for i in range(n)]
        walk(s, e, b"trak")
        tracks.append((found["handler"], found["codec"], found["offsets"]))
    return tracks


def _nal_start_ok(b):
    """A length-prefixed H.264/HEVC sample starts with a sane length and a clear forbidden bit."""
    n = struct.unpack(">I", b[:4])[0]
    return 0 < n < (64 << 20) and not (b[4] & 0x80)


def _rtmd_start_ok(b):
    return b[:4] == RTMD_SIG


def check_samples(read, tracks):
    """Probe chunk starts of checkable tracks through `read`: returns (checked, bad)."""
    checked = bad = 0
    for handler, codec, offs in tracks:
        if not offs:
            continue
        if handler == b"vide" and codec in NAL_CODECS:
            ok = _nal_start_ok
        elif codec == b"rtmd":
            ok = _rtmd_start_ok
        else:
            continue
        step = max(1, len(offs) // 6)
        for off in sorted(set(offs[::step] + [offs[len(offs) // 2], offs[-1]])):
            b = read(off, 8)
            checked += 1
            bad += not (b and len(b) == 8 and ok(b))
    return checked, bad


def runs_reader(fs, runs):
    """read(file_offset, n) through a list of cluster runs; None past the end."""
    cs = fs.cs

    def read(off, n):
        out = b""
        while n > 0:
            k, within = divmod(off, cs)
            for s, e in runs:
                if k < e - s:
                    c = s + k
                    break
                k -= e - s
            else:
                return None
            part = min(n, cs - within)
            out += fs.dev.read(fs.cl_off(c) + within, part)
            off, n = off + part, n - part
        return out
    return read


def file_end(read, start, limit):
    """End of the last plausible top-level box from `start` (boxes after moov, e.g. Sony `meta`)."""
    end = start
    for b in walk_boxes(read, start, limit, 16):
        if not b["size"] or b["at"] + b["size"] > limit:
            break
        end = b["at"] + b["size"]
    return end


def evaluate_layout(fs, name, runs, info, exact_clusters=None):
    """Check one layout guess: moov where the header says, sample offsets land on real frames."""
    span = info.get("declared_span")
    capacity = sum(e - s for s, e in runs) * fs.cs
    read = runs_reader(fs, runs)
    if info.get("moov_first"):
        moov_at, msize = info["moov_first"]
        end = span
    else:
        if not span or span + 8 > capacity:
            return None
        h = read(span, 8)
        if not h or h[4:8] != b"moov":
            return None
        moov_at, msize = span, struct.unpack(">I", h[:4])[0]
        end = file_end(read, span, capacity)
    if msize < 8 or moov_at + msize > capacity or end > capacity:
        return None
    need = -(-end // fs.cs)
    have = sum(e - s for s, e in runs)
    if exact_clusters is not None and need != exact_clusters:
        return None
    if name == "header-last" and need != have:
        return None  # the file must end exactly where its header cluster begins
    # Keep exactly the clusters the file needs.
    kept, left = [], need
    for s, e in runs:
        take = min(e - s, left)
        if take:
            kept.append([s, s + take])
        left -= take
    moov = read(moov_at, msize)
    checked, bad = check_samples(read, moov_tracks(moov)) if moov else (0, 0)
    status = "failed" if bad else ("verified" if checked else "unverified")
    return {"runs": kept, "bytes": end, "moov_size": msize, "samples_checked": checked, "samples_bad": bad,
            "status": status, "overwritten_clusters": fs.count_allocated(kept), "overlaps_live": fs.owners(kept)}


def layouts_for_header(fs, c, info, file_clusters=None, moov_clusters=None):
    """All plausible layouts for a clip whose ftyp header is cluster `c`."""
    cs, span, out = fs.cs, info.get("declared_span"), {}
    if not span:
        return out
    guesses = []
    nxt = fs.fat[c]
    if 2 <= nxt < fs.maxc:  # a stale FAT chain still leading out of the header
        ch = fs.chain(c, file_clusters or (-(-span // cs) + 4096))
        if len(ch) > 1:
            guesses.append(("fat-chain", fs.to_runs(ch)))
    if span % cs == 0 and info.get("one_cluster_header"):
        if file_clusters:
            start = c - (file_clusters - 1)
            if start >= 2:
                guesses.append(("header-last", [[c, c + 1], [start, c]]))
        elif moov_clusters:
            i = bisect.bisect_left(moov_clusters, c) - 1
            if i >= 0:
                start = moov_clusters[i] - (span // cs - 1)
                if start >= 2:
                    guesses.append(("header-last", [[c, c + 1], [start, c]]))
    n = file_clusters or (-(-span // cs) + 1024)
    guesses.append(("header-first", [[c, min(c + n, fs.maxc)]]))
    for name, runs in guesses:
        lay = evaluate_layout(fs, name, runs, info, file_clusters)
        if lay and all(lay["runs"] != v["runs"] for v in out.values()):  # same clusters = same answer
            out[name] = lay
    return out


def trim_mp4(path, apply=True):
    """Find the end of the last valid top-level box; truncate the slack after it."""
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        def read(o, n):
            f.seek(o)
            return f.read(n)
        if read(4, 4) != b"ftyp":
            return {"boxes": [], "end": size, "file_size": size, "trimmed": False,
                    "reason": "not an MP4/MOV (no ftyp)"}
        boxes, pos = [], 0
        for b in walk_boxes(read, 0, size, 4096):
            if not b["size"] or b["at"] + b["size"] > size:
                break
            boxes.append(b)
            pos = b["at"] + b["size"]
    types = [b["type"] for b in boxes]
    result = {"boxes": boxes, "end": pos, "file_size": size, "trimmed": False, "would_trim": size - pos}
    if "mdat" not in types or "moov" not in types:
        result["reason"] = "no complete mdat+moov: left untouched (repair with untrunc)"
    elif pos < size and apply:
        os.truncate(path, pos)
        result["trimmed"] = True
    return result


# ------------------------------------------------------------------ scan ----

def plan_deleted_entry(fs, r):
    """Best cluster runs for a deleted entry, and how sure we are."""
    s, n = r["first_cluster"], -(-r["size"] // fs.cs)
    if r["nofatchain"]:
        return [[s, min(s + n, fs.maxc)]], "contiguous (NoFatChain)", True
    ch = fs.chain(s, n)
    if len(ch) == n:
        used = sum(fs.allocated(c) for c in ch)
        how = "FAT chain intact" if not used else f"FAT chain intact, {used} cluster(s) reused by other files"
        return fs.to_runs(ch), how, True
    head = fs.dev.read(fs.cl_off(s), fs.cs)
    if head[4:8] == b"ftyp":  # a video whose chain is gone: test the known layouts against its index
        lays = layouts_for_header(fs, s, header_info(head), file_clusters=n)
        good = [k for k, v in lays.items() if v["status"] == "verified"]
        if len(good) == 1:
            return lays[good[0]]["runs"], f"{good[0]} (FAT chain lost; layout verified against the moov)", True
    return [[s, min(s + n, fs.maxc)]], "GUESSED contiguous (FAT chain lost, layout not verified)", False


def cmd_scan(a):
    dev = Device(a.device)
    if a.out:
        refuse_if_on_card(dev, a.out)
    part = resolve_part(dev, a.part_offset)
    log(f"reading exFAT at byte {part:,} of {a.device} (read-only)...")
    fs = ExFAT(dev, part)
    deleted = []
    for r in fs.records:
        if not (r["deleted"] and not r["is_dir"] and r["size"] and 2 <= r["first_cluster"] < fs.maxc):
            continue
        runs, how, sure = plan_deleted_entry(fs, r)
        total = sum(e - b for b, e in runs)
        used = fs.count_allocated(runs)
        r.update(runs=runs, runs_method=how, runs_verified=sure, clusters=total, overwritten_clusters=used,
                 past_card_end=r["first_cluster"] + -(-r["size"] // fs.cs) > fs.maxc,
                 pct_now_in_use=round(100.0 * used / total, 2) if total else None,
                 overlaps_live=fs.owners(runs), head=printable(dev.read(fs.cl_off(r["first_cluster"]), 16)))
        deleted.append(r)

    out = {"tool": f"exfatrec {__version__}", "geometry": fs.geometry(), "records": fs.records, "offset": []}
    if a.out:
        write_json(a.out, out)  # written before the offset analysis, so nothing is lost if that fails

    if a.offset is not None:
        for label, off in (("partition-relative", fs.part_off + a.offset), ("disk-absolute", a.offset)):
            rep = {"interpretation": label, "byte": off}
            if fs.heap_abs <= off < fs.heap_end:
                c = (off - fs.heap_abs) // fs.cs + 2
                boxes = walk_boxes(dev.read, off, fs.heap_end)
                rep.update(cluster=c, cluster_aligned=(off - fs.heap_abs) % fs.cs == 0,
                           allocated_now=bool(fs.allocated(c)), live_owner=fs.owner(c),
                           deleted_entries=[r["path"] for r in deleted if any(b <= c < e for b, e in r["runs"])],
                           head=printable(dev.read(off, 64)), boxes=boxes[:12])
                if boxes and boxes[-1]["size"]:
                    rep["declared_span"] = boxes[-1]["at"] + boxes[-1]["size"] - off
            else:
                rep["note"] = "outside the cluster heap"
            out["offset"].append(rep)
        if a.out:
            write_json(a.out, out)
    if a.out:
        log(f"wrote {a.out}")

    live = [r for r in fs.records if not r["deleted"] and not r["is_dir"]]
    print(f"volume serial {fs.serial}, cluster {fs.cs // 1024} KiB, {fs.maxc - 2:,} clusters")
    print(f"live files: {len(live):,} ({gb(sum(r['size'] for r in live))})")
    print(f"deleted file entries: {len(deleted):,}")
    trash = [r for r in live if "/.Trashes/" in r["path"] or "/.Trash/" in r["path"]]
    if trash:
        print(f"NOTE: {len(trash):,} files ({gb(sum(r['size'] for r in trash))}) are still in the card's Trash"
              " folder and can simply be copied out.")
    if deleted:
        print("\nlargest deleted entries (in-use % = how much of their space is now taken by other files):")
        for r in sorted(deleted, key=lambda r: -r["size"])[:a.top]:
            print(f"  {gb(r['size']):>10}  in-use {r['pct_now_in_use']:>6}%  {r['mtime'] or '':<26} {r['path']}"
                  f"  first cluster {r['first_cluster']}  [{r['runs_method']}]")
    for rep in out["offset"]:
        print(f"\noffset {a.offset:,} read as {rep['interpretation']} (byte {rep['byte']:,}):")
        if "cluster" not in rep:
            print(f"  {rep['note']}")
            continue
        print(f"  cluster {rep['cluster']:,} aligned={rep['cluster_aligned']} allocated={rep['allocated_now']}"
              f" owner={rep['live_owner']} head={rep['head'][:32]!r}")
        for b in rep["boxes"]:
            print(f"    box {b['type']} size {b['size']:,}{'  ' + b['note'] if 'note' in b else ''}")
        if "declared_span" in rep:
            print(f"  declared span {gb(rep['declared_span'])}"
                  + ("  (runs past the end of the card: not contiguous from here)"
                     if rep["byte"] + rep["declared_span"] > fs.heap_end else ""))


# ------------------------------------------------------------------- map ----

def analyze_orphan(fs, c, info, moov_clusters):
    cand = {"header": c, **info, "layouts": layouts_for_header(fs, c, info, moov_clusters=moov_clusters)}
    if not cand["layouts"]:
        if info.get("unfinalized"):
            cand["note"] = ("mdat size is 0: the recording was never finalized; copy it with "
                            "`plan --layout header-first --force --max-bytes N` and repair with untrunc")
        elif not info.get("declared_span"):
            cand["note"] = "no mdat size in the header cluster; try `scan --offset` on it"
        elif info.get("one_cluster_header"):
            cand["note"] = "header-last shape, but no moov before or after it: index overwritten or never written"
        else:
            cand["note"] = "no moov where the header says the video ends (overwritten, fragmented or unfinalized)"
    return cand


def cmd_map(a):
    dev = Device(a.device)
    refuse_if_on_card(dev, a.out)
    part = resolve_part(dev, a.part_offset)
    log(f"reading exFAT at byte {part:,} of {a.device} (read-only)...")
    fs = ExFAT(dev, part)
    first = max(2, a.first_cluster)
    last = min(fs.maxc, a.last_cluster or fs.maxc)
    if first >= last:
        die("empty cluster range")
    cs, zero, ones = fs.cs, bytes(fs.cs), b"\xff" * fs.cs
    runs, heads = [], {}
    t0, c = time.time(), first
    total = (last - first) * cs
    while c < last:
        k = min(BLK, last - c)
        if a.free_only and all(fs.allocated(c + j) for j in range(k)):
            # Orphans live only in free space: skip reading blocks that belong to live files.
            for cc in range(c, c + k):
                key = ("A", 1, fs.owner(cc))
                if runs and runs[-1][0] == key and runs[-1][2] == cc:
                    runs[-1][2] = cc + 1
                else:
                    runs.append([key, cc, cc + 1])
            c += k
            continue
        buf = dev.read(fs.cl_off(c), k * cs)
        if len(buf) < k * cs:
            die(f"short read at cluster {c}")
        for j in range(k):
            cc = c + j
            cl = buf[j * cs:(j + 1) * cs]
            tag = cl[4:8]
            if cl == zero:
                cls = "Z"
            elif cl == ones:
                cls = "F"
            elif tag == b"ftyp":
                cls = "H"
            elif tag == b"moov":
                cls = "M"
            elif cl[:4] == RTMD_SIG:
                cls = "R"
            else:
                cls = "D"
            alloc = fs.allocated(cc)
            own = fs.owner(cc) if alloc else None
            if cls == "H":
                heads[cc] = {"type": "ftyp", "alloc": alloc, "owner": own, **header_info(cl)}
            elif cls == "M":
                heads[cc] = {"type": "moov", "alloc": alloc, "owner": own,
                             "moov_size": struct.unpack(">I", cl[:4])[0]}
            key = (cls, alloc, own)
            if runs and runs[-1][0] == key and runs[-1][2] == cc:
                runs[-1][2] = cc + 1
            else:
                runs.append([key, cc, cc + 1])
        c += k
        done = (c - first) * cs
        if (c - first) % 4096 < BLK or c >= last:
            el = time.time() - t0
            rate = done / el / 1e6 if el else 0
            eta = (total - done) / (rate * 1e6) / 60 if rate else 0
            print(f"\rmapped {gb(done)} / {gb(total)}  {rate:5.0f} MB/s  ETA {eta:4.1f} min",
                  end="", file=sys.stderr, flush=True)
    print(file=sys.stderr)

    moov_clusters = sorted(k for k, v in heads.items() if v["type"] == "moov" and not v["alloc"])
    orphans = {}
    for hc, info in sorted(heads.items()):
        if info["type"] == "ftyp" and not info["alloc"]:
            clean = {k: v for k, v in info.items() if k not in ("type", "alloc", "owner")}
            orphans[str(hc)] = analyze_orphan(fs, hc, clean, moov_clusters)

    rle = [{"cls": k[0], "alloc": k[1], "owner": k[2], "start": s, "end": e} for k, s, e in runs]
    out = {"tool": f"exfatrec {__version__}", "device": a.device, "geometry": fs.geometry(),
           "range": [first, last], "runs": rle, "heads": {str(k): v for k, v in heads.items()},
           "orphans": orphans}
    write_json(a.out, out)
    log(f"wrote {a.out}")

    me = f"python3 {shlex.quote(os.path.abspath(sys.argv[0]))}"
    print(f"{len(orphans)} orphan video header(s) (free ftyp clusters) in clusters {first:,}..{last - 1:,}:")
    for key, o in orphans.items():
        span = o.get("declared_span")
        print(f"\n  header at cluster {int(key):,}  brand {o['brand']}  declared {gb(span) if span else '?'}")
        for name, lay in o["layouts"].items():
            if lay["status"] == "verified" and not lay["overwritten_clusters"]:
                verdict = "RECOVERABLE"
            elif lay["status"] == "verified":
                verdict = f"{lay['overwritten_clusters']:,} clusters overwritten by " + \
                    ", ".join(lay["overlaps_live"][:3])
            else:
                verdict = f"index found, frames {lay['status']} ({lay['samples_bad']}/{lay['samples_checked']} bad)"
            print(f"    {name:<12} {gb(lay['bytes'])} in {sum(e - s for s, e in lay['runs']):,} clusters -> {verdict}")
        if any(v["status"] == "verified" for v in o["layouts"].values()):
            print(f"    next: {me} plan {shlex.quote(a.out)} --header {int(key)} --out plan.json")
        elif o["layouts"]:
            print("    frames did not verify: not recoverable as found (see README before using --force)")
        else:
            print(f"    no layout matched: {o.get('note', '')}")


# ------------------------------------------------------------------ plan ----

def cmd_plan(a):
    with open(a.source, encoding="utf-8") as f:
        src = json.load(f)
    geo = src["geometry"]
    cs = geo["cluster_size"]
    exact = False
    if a.entry:
        recs = [r for r in src.get("records", []) if r["deleted"] and r["path"] == a.entry and "runs" in r]
        if a.entry_cluster is not None:
            recs = [r for r in recs if r["first_cluster"] == a.entry_cluster]
        if not recs:
            die(f"no deleted entry with runs at {a.entry!r} (paths come from `scan` output)")
        if len(recs) > 1:
            listing = "\n".join(f"  --entry-cluster {r['first_cluster']}  {gb(r['size'])}  {r['mtime']}"
                                f"  [{r['runs_method']}]" for r in recs)
            die(f"{len(recs)} deleted entries share this path; pick one:\n{listing}")
        r = recs[0]
        if not r.get("runs_verified") and not a.force:
            die(f"{r['path']}: its layout is a guess ({r['runs_method']}); --force to copy it anyway")
        if r.get("overwritten_clusters", 0) and not a.force:
            die(f"{r['path']}: {r['overwritten_clusters']:,} of its clusters are now used by other files;"
                " --force to copy what is left")
        runs, size = r["runs"], r["size"]
        exact = bool(r.get("runs_verified"))
        note = f"deleted entry {r['path']} ({r['runs_method']}), {r['pct_now_in_use']}% now in use"
    else:
        if a.header is None:
            die("give --header CLUSTER (from `map`) or --entry PATH (from `scan`)")
        o = src.get("orphans", {}).get(str(a.header))
        if o is None:
            die(f"cluster {a.header} is not an orphan header in {a.source}")
        layouts = o["layouts"]
        if a.layout == "auto":
            good = [k for k, v in layouts.items() if v["status"] == "verified" and not v["overwritten_clusters"]]
            if len(good) > 1:
                die(f"several layouts fit ({', '.join(good)}); choose one with --layout")
            if not good:
                die(f"no verified, intact layout for this header ({o.get('note') or 'see map output'});"
                    " choose --layout and add --force to copy anyway")
            layout = good[0]
        else:
            layout = a.layout
        if layout in layouts:
            lay = layouts[layout]
            if (lay["status"] != "verified" or lay["overwritten_clusters"]) and not a.force:
                die(f"{layout}: frames {lay['status']}, {lay['overwritten_clusters']} clusters overwritten;"
                    " --force to copy anyway")
            runs, size = lay["runs"], lay["bytes"]
        elif a.force and layout == "header-first":
            if o.get("one_cluster_header") and not o.get("unfinalized"):
                die("this header has the Sony header-last shape: its video sits BEFORE it, so copying"
                    " forward would give other data; the moov was not found, so it cannot be rebuilt here")
            limit = a.max_bytes or o.get("declared_span")
            if not limit:
                die("no declared size in the header: give --max-bytes to say how much to copy")
            n = -(-limit // cs)
            mapped_end = src.get("range", [0, geo["cluster_count"] + 2])[1]
            stop = min(a.header + n, geo["cluster_count"] + 2, mapped_end)
            if a.header + n > mapped_end:
                log(f"map only covered clusters up to {mapped_end:,}; re-run map with a larger --last-cluster"
                    " to copy further")
            busy = sorted(r["start"] for r in src.get("runs", []) if r["alloc"] and r["end"] > a.header + 1)
            i = bisect.bisect_left(busy, a.header + 1)
            end = min(stop, busy[i]) if i < len(busy) else stop  # stop at the first cluster another file owns
            runs, size = [[a.header, end]], (end - a.header) * cs
        else:
            die(f"layout {layout} did not match this header")
        note = f"orphan header at cluster {a.header}, layout {layout}"
    plan = {"tool": f"exfatrec {__version__}", "part_offset": geo["part_offset"], "serial": geo["serial"],
            "cluster_size": cs, "heap_abs": geo["heap_abs"], "runs": runs,
            "expected_bytes": size, "exact_size": exact, "note": note}
    total = sum(e - s for s, e in runs) * cs
    if a.out:
        write_json(a.out, plan)
        log(f"wrote {a.out}")
    else:
        json.dump(plan, sys.stdout)
        print()
    log(f"{note}\n{len(runs)} run(s), {gb(total)} to copy")


# --------------------------------------------------------------- extract ----

def cmd_extract(a):
    with open(a.plan, encoding="utf-8") as f:
        plan = json.load(f)
    dev = Device(a.device)
    bs = dev.read(plan["part_offset"], 512)
    if bs[3:11] != b"EXFAT   " or "%08X" % struct.unpack_from("<I", bs, 0x64)[0] != plan["serial"]:
        die("this device is not the card the plan was made from (volume serial differs)")
    out = os.path.abspath(a.output)
    part_path = out + ".part"
    outdir = os.path.dirname(out)
    for p in (out, part_path):
        if os.path.lexists(p):
            die(f"refusing to overwrite {p}")
    if not os.path.isdir(outdir):
        die(f"output folder does not exist: {outdir}")
    refuse_if_on_card(dev, out)
    cs, heap, runs = plan["cluster_size"], plan["heap_abs"], plan["runs"]
    total = sum(e - s for s, e in runs) * cs
    need = total + (256 << 20)
    free = shutil.disk_usage(outdir).free
    if free < need:
        die(f"not enough space in {outdir}: need {gb(need)} (the clip plus a 256 MB margin), have {gb(free)}")

    t0, done = time.time(), 0
    fd = os.open(part_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
    try:
        with os.fdopen(fd, "wb") as f:
            for s, e in runs:
                c = s
                while c < e:
                    k = min(BLK, e - c)
                    buf = dev.read(heap + (c - 2) * cs, k * cs)
                    if len(buf) != k * cs:
                        raise IOError(f"short read at cluster {c}")
                    f.write(buf)
                    done += len(buf)
                    c += k
                    if (done // cs) % 4096 < BLK or done == total:
                        el = time.time() - t0
                        rate = done / el / 1e6 if el else 0
                        eta = (total - done) / (rate * 1e6) / 60 if rate else 0
                        print(f"\rcopied {gb(done)} / {gb(total)}  {rate:5.0f} MB/s  ETA {eta:4.1f} min",
                              end="", file=sys.stderr, flush=True)
            f.flush()
            os.fsync(f.fileno())
    except (OSError, IOError) as err:
        print(file=sys.stderr)
        die(f"copy stopped: {err}. The partial copy is kept as {part_path} ({done:,} of {total:,} bytes)")
    print(file=sys.stderr)
    if plan.get("exact_size") and plan["expected_bytes"] <= done:
        os.truncate(part_path, plan["expected_bytes"])  # the directory entry gave the exact size
        print(f"cut to the size recorded in the directory entry: {plan['expected_bytes']:,} bytes")
    elif not a.no_trim:
        report_trim(trim_mp4(part_path))
    os.replace(part_path, out)
    chown_to_sudo_user(out)
    print(f"wrote {out} in {(time.time() - t0) / 60:.1f} min")


def report_trim(t):
    for b in t["boxes"]:
        print(f"  box {b['type']:<4} at {b['at']:>16,}  size {b['size']:>16,}")
    if t["trimmed"]:
        print(f"trimmed {t['file_size'] - t['end']:,} bytes of cluster slack -> {t['end']:,} bytes")
    elif t.get("reason"):
        print(f"not trimmed: {t['reason']}")
    elif t.get("would_trim"):
        print(f"would trim {t['would_trim']:,} bytes -> {t['end']:,} bytes (dry run)")
    else:
        print("nothing to trim")


def cmd_trim(a):
    report_trim(trim_mp4(a.file, apply=not a.dry_run))


# ---------------------------------------------------------------- verify ----

def ffprobe_json(args):
    try:
        out = subprocess.run(["ffprobe", "-v", "error", *args, "-of", "json"], capture_output=True,
                             text=True, check=True).stdout
    except FileNotFoundError:
        die("ffprobe not found (install ffmpeg)")
    except subprocess.CalledProcessError as e:
        die(f"ffprobe failed: {e.stderr.strip()[:300]}")
    return json.loads(out)


def packets(path, index):
    """(pos, size, flags) of every packet of one stream, straight from the index."""
    proc = subprocess.run(["ffprobe", "-v", "error", "-select_streams", str(index), "-show_entries",
                           "packet=pos,size,flags", "-of", "compact=p=0:nk=0", path],
                          capture_output=True, text=True)
    if proc.returncode:
        die(f"ffprobe could not list packets of stream {index}: {proc.stderr.strip()[:300]}")
    for line in proc.stdout.splitlines():
        kv = dict(x.split("=", 1) for x in line.split("|") if "=" in x)
        if kv.get("pos", "N/A") != "N/A":
            yield int(kv["pos"]), int(kv["size"]), kv.get("flags", "")


def cmd_verify(a):
    streams = ffprobe_json(["-show_entries", "stream=index,codec_type,codec_name,codec_tag_string:format=duration",
                            a.file])
    dur = float(streams.get("format", {}).get("duration", 0) or 0)
    print(f"{a.file}: {dur / 60:.2f} min")
    ok, unchecked_video = True, 0
    if not any(st.get("codec_type") == "video" for st in streams.get("streams", [])):
        print("VERIFY_FAILED: the file has no video stream")
        sys.exit(1)
    with open(a.file, "rb") as f:
        for st in streams.get("streams", []):
            idx, kind, codec = st["index"], st.get("codec_type"), st.get("codec_name")
            if kind == "video" and codec in ("h264", "hevc"):
                bad, frames, keys, nals = [], 0, 0, 0
                for pos, size, flags in packets(a.file, idx):
                    frames += 1
                    keys += "K" in flags
                    p, end = pos, pos + size
                    while p < end:
                        f.seek(p)
                        h = f.read(5)
                        if len(h) < 5:
                            break
                        n = struct.unpack(">I", h[:4])[0]
                        if h[4] & 0x80 or n == 0:
                            break
                        nals += 1
                        p += 4 + n
                    if p != end:
                        bad.append(frames)
                ok &= frames > 0 and not bad
                print(f"  stream {idx} {codec}: {frames:,} frames, {keys:,} keyframes, {nals:,} NAL units,"
                      f" broken frames: {len(bad)}" + (f" (first: {bad[:5]})" if bad else ""))
            elif kind == "data" and st.get("codec_tag_string") == "rtmd":
                n = badm = 0
                for pos, _size, _flags in packets(a.file, idx):
                    n += 1
                    f.seek(pos)
                    badm += f.read(4) != RTMD_SIG
                ok &= not badm
                print(f"  stream {idx} rtmd: {n:,} samples, bad signature: {badm}")
            else:
                unchecked_video += kind == "video"
                print(f"  stream {idx} {kind}/{codec}: no structural check for this codec")
    print("  (the structural check covers frame and NAL boundaries, not picture content;"
          " --decode checks content)")
    if a.decode:
        log("full software decode (no -hwaccel: VideoToolbox rejects some camera 4K H.264 streams)...")
        t0 = time.time()
        proc = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-err_detect", "crccheck+bitstream+buffer",
                               "-i", a.file, "-map", "0:v", "-map", "0:a?", "-f", "null", "-"],
                              capture_output=True, text=True)
        errs = [l for l in proc.stderr.splitlines() if l.strip()]
        ok &= proc.returncode == 0 and not errs
        print(f"  decode: exit {proc.returncode}, {len(errs)} error line(s), {(time.time() - t0) / 60:.1f} min")
        for l in errs[:5]:
            print(f"    {l}")
    if not ok:
        print("VERIFY_FAILED")
        sys.exit(1)
    if unchecked_video and not a.decode:
        print("VERIFY_INCOMPLETE: a video stream was not checked; rerun with --decode")
        sys.exit(2)
    print("VERIFY_OK")


# --------------------------------------------------------------- preview ----

def _u32s(buf, start, n):
    return list(struct.unpack(f">{n}I", buf[start:start + 4 * n])) if n else []


def parse_movie(moov):
    """mvhd creation/duration plus, per track, everything needed to find and decode samples."""
    hdr = 16 if struct.unpack(">I", moov[:4])[0] == 1 else 8
    movie = {"created": None, "seconds": 0.0, "tracks": []}

    def walk(s0, e0, parent, t):
        for typ, s, e in iter_children(moov, s0, e0):
            if typ in MOOV_CHILDREN:
                walk(s, e, typ, t)
            elif typ == b"mdhd":
                t["timescale"] = struct.unpack(">I", moov[s + 20:s + 24] if moov[s] == 1 else moov[s + 12:s + 16])[0]
            elif typ == b"hdlr" and parent == b"mdia":
                t["handler"] = moov[s + 8:s + 12]
            elif typ == b"stsd" and struct.unpack(">I", moov[s + 4:s + 8])[0]:
                es = s + 8
                esize, fmt = struct.unpack(">I4s", moov[es:es + 8])
                t["codec"] = fmt
                if fmt in NAL_CODECS:
                    t["width"], t["height"] = struct.unpack(">HH", moov[es + 32:es + 36])
                    for ct, a, b in iter_children(moov, es + 86, min(es + esize, e)):
                        if ct in (b"avcC", b"hvcC"):
                            t["config"] = (ct, moov[a:b])
            elif typ == b"stts":
                n = struct.unpack(">I", moov[s + 4:s + 8])[0]
                v = _u32s(moov, s + 8, 2 * n)
                t["stts"] = list(zip(v[::2], v[1::2]))
            elif typ == b"stss":
                t["sync"] = _u32s(moov, s + 8, struct.unpack(">I", moov[s + 4:s + 8])[0])
            elif typ == b"stsc":
                n = struct.unpack(">I", moov[s + 4:s + 8])[0]
                v = _u32s(moov, s + 8, 3 * n)
                t["stsc"] = list(zip(v[::3], v[1::3]))
            elif typ == b"stsz":
                size, n = struct.unpack(">II", moov[s + 4:s + 12])
                t["sizes"] = [size] * n if size else _u32s(moov, s + 12, n)
            elif typ == b"stco":
                t["chunks"] = _u32s(moov, s + 8, struct.unpack(">I", moov[s + 4:s + 8])[0])
            elif typ == b"co64":
                n = struct.unpack(">I", moov[s + 4:s + 8])[0]
                t["chunks"] = list(struct.unpack(f">{n}Q", moov[s + 8:s + 8 + 8 * n]))

    for typ, s, e in iter_children(moov, hdr, len(moov)):
        if typ == b"mvhd":
            if moov[s] == 1:
                ct, _m, ts, dur = struct.unpack(">QQIQ", moov[s + 4:s + 32])
            else:
                ct, _m, ts, dur = struct.unpack(">IIII", moov[s + 4:s + 20])
            movie["created"] = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(ct - 2082844800)) if ct else None
            movie["seconds"] = dur / ts if ts else 0.0
        elif typ == b"trak":
            t = {}
            walk(s, e, b"trak", t)
            movie["tracks"].append(t)
    return movie


def sample_table(t):
    """[(file_offset, size, chunk_index, seconds)] for every sample of a track."""
    sizes, chunks, stsc = t.get("sizes", []), t.get("chunks", []), t.get("stsc", [])
    out, si = [], 0
    bounds = stsc + [(len(chunks) + 1, 0)]
    for j in range(len(stsc)):
        first, spc = bounds[j]
        for chunk in range(first, bounds[j + 1][0]):
            off = chunks[chunk - 1]
            for _ in range(spc):
                if si >= len(sizes):
                    break
                out.append([off, sizes[si], chunk - 1, 0.0])
                off += sizes[si]
                si += 1
    scale, now, i = t.get("timescale") or 1, 0, 0
    for count, delta in t.get("stts", []):
        for _ in range(count):
            if i < len(out):
                out[i][3] = now / scale
            now += delta
            i += 1
    return out


def annexb(config, sample):
    """A decodable H.264/HEVC elementary stream from a length-prefixed sample and its avcC/hvcC."""
    kind, cfg = config
    params = []
    if kind == b"avcC":
        nal_len = (cfg[4] & 3) + 1
        p = 6
        for _ in range(cfg[5] & 0x1F):
            n = struct.unpack(">H", cfg[p:p + 2])[0]
            params.append(cfg[p + 2:p + 2 + n])
            p += 2 + n
        count, p = cfg[p], p + 1
        for _ in range(count):
            n = struct.unpack(">H", cfg[p:p + 2])[0]
            params.append(cfg[p + 2:p + 2 + n])
            p += 2 + n
        fmt = "h264"
    else:
        nal_len, p = (cfg[21] & 3) + 1, 23
        for _ in range(cfg[22]):
            count = struct.unpack(">H", cfg[p + 1:p + 3])[0]
            p += 3
            for _ in range(count):
                n = struct.unpack(">H", cfg[p:p + 2])[0]
                params.append(cfg[p + 2:p + 2 + n])
                p += 2 + n
        fmt = "hevc"
    stream = b"".join(b"\0\0\0\1" + x for x in params)
    q = 0
    while q + nal_len <= len(sample):
        n = int.from_bytes(sample[q:q + nal_len], "big")
        q += nal_len
        if n <= 0 or q + n > len(sample):
            break
        stream += b"\0\0\0\1" + sample[q:q + n]
        q += n
    return fmt, stream


def decode_jpeg(config, sample, width):
    if not config or not shutil.which("ffmpeg"):
        return None
    fmt, stream = annexb(config, sample)
    p = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", fmt, "-i", "pipe:0", "-frames:v", "1",
                        "-vf", f"scale={width}:-2", "-c:v", "mjpeg", "-q:v", "5", "-f", "image2", "pipe:1"],
                       input=stream, capture_output=True)
    return p.stdout if p.returncode == 0 and p.stdout[:2] == b"\xff\xd8" else None


def sony_sidecars(fs, c):
    """Sony writes the clip's thumbnail JPEG and M01.XML right after the header cluster."""
    found = {}
    for k in (1, 2, 3):
        if not (2 <= c + k < fs.maxc) or fs.allocated(c + k):
            continue
        blob = fs.dev.read(fs.cl_off(c + k), fs.cs)
        if blob[:3] == b"\xff\xd8\xff" and "thumb" not in found:
            end = blob.find(b"\xff\xd9")
            found["thumb"] = blob[:end + 2] if end > 0 else None
        elif blob[:5] == b"<?xml" and b"NonRealTimeMeta" in blob[:4096]:
            text = blob.split(b"\0", 1)[0].decode("utf-8", "replace")
            for key, pat in (("created", r'CreationDate value="([^"]+)"'), ("model", r'modelName="([^"]+)"'),
                             ("codec", r'videoCodec="([^"]+)"'), ("fps", r'captureFps="([^"]+)"'),
                             ("frames", r'<Duration value="(\d+)"')):
                m = re.search(pat, text)
                if m:
                    found[key] = m.group(1)
    return found


def preview_clip(fs, runs, info, n_frames, width, own=False):
    """Metadata, survival timeline and decoded frames for one clip, read through `runs`.
    own=True for a live file: its clusters are allocated to itself, not overwritten."""
    span = info.get("declared_span")
    read = runs_reader(fs, runs)
    if info.get("moov_first"):
        at, size = info["moov_first"]
    else:
        h = read(span, 8) if span else None
        if not h or h[4:8] != b"moov":
            return {"error": "no index (moov) where the header says: frames cannot be located"}
        at, size = span, struct.unpack(">I", h[:4])[0]
    moov = read(at, size)
    movie = parse_movie(moov)
    video = next((t for t in movie["tracks"] if t.get("handler") == b"vide" and t.get("codec") in NAL_CODECS), None)
    card_of = [c for s, e in runs for c in range(s, e)]
    clip = {"created": movie["created"], "seconds": movie["seconds"], "frames": [], "segments": []}
    if not video:
        clip["error"] = "no H.264/HEVC video track to preview"
        return clip
    clip.update(codec=video["codec"].decode(), width=video.get("width"), height=video.get("height"))
    samples = sample_table(video)
    if len(samples) > 1:
        clip["fps"] = round((len(samples) - 1) / max(samples[-1][3], 1e-9), 3)
    good_chunk = {}

    def sample_ok(s):
        off, size, chunk, _t = s
        ks = range(off // fs.cs, (off + size - 1) // fs.cs + 1)
        if any(k >= len(card_of) or (not own and fs.allocated(card_of[k])) for k in ks):
            return False
        if chunk not in good_chunk:
            b = read(video["chunks"][chunk], 8)
            good_chunk[chunk] = bool(b and len(b) == 8 and _nal_start_ok(b))
        return good_chunk[chunk]

    ok = [sample_ok(s) for s in samples]
    for s, g in zip(samples, ok):
        if clip["segments"] and clip["segments"][-1][0] == g:
            clip["segments"][-1][2] = s[3]
        else:
            clip["segments"].append([g, s[3], s[3]])
    clip["pct_ok"] = round(100.0 * sum(ok) / len(ok), 1) if ok else 0.0
    sync = [i - 1 for i in video.get("sync", range(1, len(samples) + 1)) if 0 < i <= len(samples) and ok[i - 1]]
    if sync:
        times = [samples[i][3] for i in sync]
        picks = []
        for j in range(n_frames):
            target = clip["seconds"] * (j + 0.5) / n_frames
            i = bisect.bisect_left(times, target)
            cand = min((x for x in (i - 1, i) if 0 <= x < len(sync)), key=lambda x: abs(times[x] - target))
            if sync[cand] not in picks:
                picks.append(sync[cand])
        for i in picks:
            off, size, _c, t = samples[i]
            jpg = decode_jpeg(video.get("config"), read(off, size), width)
            clip["frames"].append({"t": t, "jpeg": jpg})
    return clip


def _fmt_t(sec):
    sec = int(sec)
    return f"{sec // 3600}:{sec // 60 % 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"


def render_preview_html(title, clips):
    import base64
    import html
    cards = []
    for n, c in enumerate(clips, 1):
        p = c["preview"]
        dur = p.get("seconds") or 0
        pct = p.get("pct_ok")
        if c.get("layout") == "file" and not p.get("error"):
            badge, cls = (f"On the card: {pct}% plays", "ok" if pct == 100.0 else "warn")
        elif p.get("error"):
            badge, cls = "Cannot preview", "bad"
        elif pct == 100.0 and not c.get("overwritten"):
            badge, cls = "Recoverable: 100%", "ok"
        elif pct:
            badge, cls = f"Partial: {pct}% survives", "warn"
        else:
            badge, cls = "Overwritten", "bad"
        bar = "".join(
            f'<span class="{"g" if g else "r"}" style="width:{max((b - a) / dur * 100, 0.3) if dur else 0:.3f}%" '
            f'title="{_fmt_t(a)}-{_fmt_t(b)} {"survives" if g else "lost"}"></span>'
            for g, a, b in p.get("segments", []))
        frames = "".join(
            (f'<figure><img src="data:image/jpeg;base64,{base64.b64encode(f["jpeg"]).decode()}" alt="frame at {_fmt_t(f["t"])}">'
             if f["jpeg"] else '<figure><div class="noimg">not decodable</div>')
            + f'<figcaption>{_fmt_t(f["t"])}</figcaption></figure>' for f in p.get("frames", []))
        sc = c.get("sidecar", {})
        thumb = (f'<img class="thumb" src="data:image/jpeg;base64,{base64.b64encode(c["sidecar"]["thumb"]).decode()}" '
                 f'alt="camera thumbnail">' if c.get("sidecar", {}).get("thumb") else "")
        facts = [("Recorded", sc.get("created") or p.get("created") or "?"), ("Length", _fmt_t(dur) if dur else "?"),
                 ("Size", gb(c["bytes"]) if c.get("bytes") else "?"),
                 ("Video", f'{p.get("width", "?")}x{p.get("height", "?")} {p.get("codec", "")} {p.get("fps", "")} fps'.strip()),
                 ("Camera", sc.get("model", "")), ("Where", c["where"]), ("Layout", c.get("layout", ""))]
        dl = "".join(f"<dt>{k}</dt><dd>{html.escape(str(v))}</dd>" for k, v in facts if v)
        err = f'<p class="err">{html.escape(p["error"])}</p>' if p.get("error") else ""
        cards.append(f'''<article>
<header>{thumb}<div><h2>#{n} · {html.escape(str(sc.get("created") or p.get("created") or "time unknown"))} · {_fmt_t(dur) if dur else "?"}</h2>
<p class="where">{html.escape(c["name"])}</p><span class="badge {cls}">{badge}</span></div></header>
<dl>{dl}</dl>{err}
<div class="bar" aria-label="survival timeline">{bar}</div>
<div class="axis"><span>0:00</span><span>{_fmt_t(dur) if dur else ""}</span></div>
<div class="frames">{frames}</div>
<pre>{html.escape(c["command"])}</pre>
</article>''')
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>
:root{{--bg:#f6f5f2;--fg:#1d1d1b;--card:#fff;--mute:#6b6b66;--line:#e2e0da;--ok:#1f8a4c;--warn:#b7791f;--bad:#c0392b}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151514;--fg:#ecebe6;--card:#1f1f1d;--mute:#9a9a93;--line:#33332f}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 -apple-system,system-ui,sans-serif}}
main{{max-width:1100px;margin:0 auto;padding:24px 16px}} h1{{font-size:22px;margin:0 0 4px}} .sub{{color:var(--mute);margin:0 0 20px}}
article{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin:0 0 18px}}
article header{{display:flex;gap:14px;align-items:center}} h2{{font-size:17px;margin:0 0 6px}}
.thumb{{width:120px;border-radius:6px}} .badge{{font-size:12px;font-weight:600;padding:3px 8px;border-radius:99px;color:#fff}}
.ok{{background:var(--ok)}} .warn{{background:var(--warn)}} .bad{{background:var(--bad)}}
dl{{display:grid;grid-template-columns:max-content 1fr;gap:2px 14px;margin:12px 0}} dt{{color:var(--mute)}} dd{{margin:0}}
.bar{{display:flex;height:12px;border-radius:6px;overflow:hidden;background:var(--line)}} .bar .g{{background:var(--ok)}} .bar .r{{background:var(--bad)}}
.axis{{display:flex;justify-content:space-between;color:var(--mute);font-size:12px;margin:2px 0 12px}}
.frames{{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:8px}}
figure{{margin:0}} figure img,.noimg{{width:100%;aspect-ratio:16/9;object-fit:cover;border-radius:6px;background:var(--line)}}
.noimg{{display:flex;align-items:center;justify-content:center;color:var(--mute);font-size:12px}}
figcaption{{color:var(--mute);font-size:12px;text-align:center;font-variant-numeric:tabular-nums}}
pre{{background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:8px 10px;overflow-x:auto;font-size:12px;margin:12px 0 0}}
.where{{color:var(--mute);margin:0 0 6px;font-size:13px}}
.err{{color:var(--bad)}}</style></head><body><main>
<h1>{html.escape(title)}</h1><p class="sub">Frames are decoded straight from the card; nothing has been recovered or written yet.
Green = survives, red = overwritten by newer files.</p>
{"".join(cards) or "<p>No video found.</p>"}
</main></body></html>'''


def cmd_preview(a):
    sources = []
    if not a.sources and not a.file:
        die("give map/scan JSON files and/or --file PATH")
    for path in a.sources:
        with open(path, encoding="utf-8") as f:
            sources.append((path, json.load(f)))
    dev = Device(a.device)
    refuse_if_on_card(dev, a.out)
    fs = ExFAT(dev, sources[0][1]["geometry"]["part_offset"] if sources else resolve_part(dev, "auto"))
    for path, src in sources:
        if fs.serial != src["geometry"]["serial"]:
            die(f"{path} was made from another card (volume serial differs)")
    me = f"python3 {shlex.quote(os.path.abspath(sys.argv[0]))}"
    clips = []
    named = {r["first_cluster"] for _p, src in sources for r in src.get("records", [])
             if r.get("deleted") and r.get("runs") and r.get("head", "")[4:8] == "ftyp"}
    for src_path, src in sources:
        preview_source(fs, src_path, src, a, me, clips, named)
    for path in a.file or []:
        rec = next((r for r in fs.records if not r["deleted"] and r["path"] == path), None)
        if rec is None:
            die(f"no live file {path!r} on the card")
        runs = fs.runs_for(rec["first_cluster"], rec["size"], rec["nofatchain"])
        info = header_info(fs.dev.read(fs.cl_off(runs[0][0]), fs.cs))
        log(f"previewing live file {path}...")
        clips.append({"name": path, "where": "live file on the card", "layout": "file", "bytes": rec["size"],
                      "command": "already on the card", "sidecar": {},
                      "preview": preview_clip(fs, runs, info, a.frames, a.width, own=True)})
    finish_preview(fs, a, clips)


def preview_source(fs, src_path, src, a, me, clips, named):
    for key, o in src.get("orphans", {}).items():
        if (a.header and int(key) not in a.header) or int(key) in named:
            continue  # a deleted entry still names this clip: it is shown under its file name instead
        if fs.allocated(int(key)):
            log(f"skipping header {int(key):,}: it belongs to a file again (already restored?)")
            continue
        lays = o.get("layouts", {})
        name = max(lays, key=lambda n: (lays[n]["status"] == "verified", -lays[n]["overwritten_clusters"],
                                        lays[n]["samples_checked"] - lays[n]["samples_bad"]), default=None)
        entry = {"name": f"Lost clip, header at cluster {int(key):,}", "where": f"orphan header, cluster {int(key):,}",
                 "layout": name or "none", "bytes": lays[name]["bytes"] if name else o.get("declared_span"),
                 "overwritten": lays[name]["overwritten_clusters"] if name else None,
                 "command": f"{me} plan {shlex.quote(src_path)} --header {int(key)} --out plan.json",
                 "sidecar": sony_sidecars(fs, int(key))}
        log(f"previewing header {int(key):,} ({name or 'no layout'})...")
        entry["preview"] = preview_clip(fs, lays[name]["runs"], o, a.frames, a.width) if name else \
            {"error": o.get("note") or "no layout matched"}
        clips.append(entry)
    for r in src.get("records", []):
        if not (r.get("deleted") and r.get("runs") and r.get("head", "")[4:8] == "ftyp"):
            continue
        head = fs.dev.read(fs.cl_off(r["runs"][0][0]), fs.cs)
        info = header_info(head)
        log(f"previewing deleted {r['path']}...")
        clips.append({"name": r["path"], "where": f"deleted entry, first cluster {r['first_cluster']:,}",
                      "layout": r.get("runs_method", ""), "bytes": r["size"], "overwritten": r.get("overwritten_clusters"),
                      "command": f"{me} plan {shlex.quote(src_path)} --entry {shlex.quote(r['path'])} --out plan.json",
                      "sidecar": {}, "preview": preview_clip(fs, r["runs"], info, a.frames, a.width)})


def finish_preview(fs, a, clips):
    clips.sort(key=lambda c: c["preview"].get("created") or "")
    page = render_preview_html(f"Videos found on card {fs.serial}", clips)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(a.out, flags, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(page)
        f.flush()
        chown_to_sudo_user(f.fileno())
    for c in clips:
        p = c["preview"]
        print(f"{c['name']}: {p.get('created') or '?'}  {_fmt_t(p['seconds']) if p.get('seconds') else '?'}  "
              f"{p.get('pct_ok', 0)}% survives  {len([f for f in p.get('frames', []) if f['jpeg']])} frame(s)"
              + (f"  [{p['error']}]" if p.get("error") else ""))
    print(f"wrote {a.out}: open it in a browser to see the clips before recovering any")


# ------------------------------------------------------------------ main ----

def main(argv=None):
    p = argparse.ArgumentParser(prog="exfatrec.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"exfatrec {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def device_args(sp):
        sp.add_argument("device", help="raw card device (macOS /dev/rdiskN, Linux /dev/sdX) or a disk image")
        sp.add_argument("--part-offset", default="auto",
                        help="byte offset of the exFAT volume (default: auto from MBR/GPT)")

    sp = sub.add_parser("scan", help="list live and deleted files; explain a byte offset")
    device_args(sp)
    sp.add_argument("--offset", type=int, help="byte offset to explain (e.g. the number in a carved MOV_<n> name)")
    sp.add_argument("--out", help="write full results to this JSON file (not on the card)")
    sp.add_argument("--top", type=int, default=25, help="how many deleted entries to list")
    sp.set_defaults(func=cmd_scan)

    sp = sub.add_parser("map", help="classify every cluster and find orphan video headers")
    device_args(sp)
    sp.add_argument("--out", required=True, help="JSON file for the cluster map (not on the card)")
    sp.add_argument("--first-cluster", type=int, default=2)
    sp.add_argument("--last-cluster", type=int, help="stop before this cluster (default: end of card)")
    sp.add_argument("--free-only", action="store_true",
                    help="skip reading clusters that live files own (much faster; orphans are only in free space)")
    sp.set_defaults(func=cmd_map)

    sp = sub.add_parser("plan", help="turn an orphan header (map) or a deleted entry (scan) into cluster runs")
    sp.add_argument("source", help="JSON from `map` (with --header) or from `scan` (with --entry)")
    sp.add_argument("--header", type=int, help="cluster of an orphan ftyp header, as listed by `map`")
    sp.add_argument("--entry", help="path of a deleted entry, as listed by `scan`")
    sp.add_argument("--entry-cluster", type=int, help="first cluster, when several deleted entries share a path")
    sp.add_argument("--layout", choices=["auto", "header-last", "header-first", "fat-chain"], default="auto")
    sp.add_argument("--force", action="store_true",
                    help="plan even if clusters are overwritten, frames are unverified, or no layout matched")
    sp.add_argument("--max-bytes", type=int, help="with --force header-first: how much to copy when the size is unknown")
    sp.add_argument("--out", help="write the plan here (default: stdout)")
    sp.set_defaults(func=cmd_plan)

    sp = sub.add_parser("extract", help="copy a plan's clusters to a new file on ANOTHER disk, then trim it")
    sp.add_argument("device")
    sp.add_argument("plan")
    sp.add_argument("output", help="new file; must not be on the card")
    sp.add_argument("--no-trim", action="store_true")
    sp.set_defaults(func=cmd_extract)

    sp = sub.add_parser("trim", help="cut cluster slack after the last valid MP4 box")
    sp.add_argument("file")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=cmd_trim)

    sp = sub.add_parser("preview", help="decode frames of every clip found, into one HTML page, before recovering")
    sp.add_argument("device")
    sp.add_argument("sources", nargs="*", help="JSON from `map` and/or `scan`")
    sp.add_argument("--file", action="append", help="also preview a live file on the card, e.g. /PRIVATE/M4ROOT/CLIP/C0001.MP4")
    sp.add_argument("--out", required=True, help="HTML file to write (not on the card)")
    sp.add_argument("--frames", type=int, default=6, help="frames per clip (default 6)")
    sp.add_argument("--width", type=int, default=480, help="frame width in pixels (default 480)")
    sp.add_argument("--header", type=int, action="append", help="only this orphan header (repeatable)")
    sp.set_defaults(func=cmd_preview)

    sp = sub.add_parser("verify", help="check every indexed sample of a recovered MP4 (needs ffprobe)")
    sp.add_argument("file")
    sp.add_argument("--decode", action="store_true", help="also decode every frame with ffmpeg (slow)")
    sp.set_defaults(func=cmd_verify)

    a = p.parse_args(argv)
    a.func(a)


if __name__ == "__main__":
    main()
