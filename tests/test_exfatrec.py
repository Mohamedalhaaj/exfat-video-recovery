"""Tests for exfatrec.

Unit tests run anywhere. The end-to-end test builds a real exFAT disk image
(macOS hdiutil) with an MBR, a deleted clip, and two orphan clips written
straight into free clusters, one in the Sony header-last layout and one in the
usual header-first layout. It then checks that each comes back byte-for-byte.
It needs macOS, hdiutil and ffmpeg with libx264.

    python3 -m unittest discover -s tests -v
"""
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import exfatrec  # noqa: E402

CLI = [sys.executable, os.path.join(ROOT, "exfatrec.py")]
CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts", b"dinf"}


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def run(*args, check=True):
    p = subprocess.run([*CLI, *map(str, args)], capture_output=True, text=True)
    if check and p.returncode:
        raise AssertionError(f"exfatrec {' '.join(map(str, args))} failed:\n{p.stdout}\n{p.stderr}")
    return p


def make_clip(path, seconds, pattern):
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"{pattern}=size=640x360:rate=24",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", str(seconds),
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "12", "-b:v", "4M",
                    "-c:a", "aac", "-y", path], check=True)
    with open(path, "rb") as f:
        return f.read()


def patch_offsets(buf, start, end, delta):
    pos = start
    while pos + 8 <= end:
        size, typ = struct.unpack(">I4s", buf[pos:pos + 8])
        hdr = 8
        if size == 1:
            size, hdr = struct.unpack(">Q", buf[pos + 8:pos + 16])[0], 16
        if size == 0:
            size = end - pos
        if typ in CONTAINERS:
            patch_offsets(buf, pos + hdr, pos + size, delta)
        elif typ in (b"stco", b"co64"):
            w = 4 if typ == b"stco" else 8
            fmt = ">I" if w == 4 else ">Q"
            n = struct.unpack(">I", buf[pos + 12:pos + 16])[0]
            for i in range(n):
                o = pos + 16 + w * i
                struct.pack_into(fmt, buf, o, struct.unpack(fmt, buf[o:o + w])[0] + delta)
        pos += size


def to_sony_layout(mp4, cs):
    """Rebuild an ffmpeg MP4 the way a Sony A7S III lays a clip out: a one-cluster
    header (ftyp + free padding + 64-bit mdat header), a cluster-padded mdat
    payload, then the moov. Chunk offsets are patched to match."""
    boxes = {b["type"]: b for b in exfatrec.walk_boxes(lambda o, n: mp4[o:o + n], 0, len(mp4))}
    ftyp = mp4[:boxes["ftyp"]["size"]]
    mdat, moov = boxes["mdat"], boxes["moov"]
    payload_at = mdat["at"] + 8
    payload = mp4[payload_at:mdat["at"] + mdat["size"]]
    payload += bytes(-len(payload) % cs)
    free_len = cs - len(ftyp) - 16
    header = ftyp + struct.pack(">I4s", free_len, b"free") + bytes(free_len - 8) + \
        struct.pack(">I4sQ", 1, b"mdat", 16 + len(payload))
    assert len(header) == cs
    moov_buf = bytearray(mp4[moov["at"]:moov["at"] + moov["size"]])
    patch_offsets(moov_buf, 8, len(moov_buf), cs - payload_at)
    return header + payload + bytes(moov_buf)


def deleted_entry_set(name, first_cluster, size, nofatchain=False):
    """A deleted exFAT file entry set (0x05 file, 0x40 stream, 0x41 names), as a camera leaves it."""
    units = name.encode("utf-16-le")
    nlen = len(units) // 2
    chunks = [units[i:i + 30] for i in range(0, len(units), 30)]
    file_e = bytearray(32)
    file_e[0], file_e[1] = 0x05, 1 + len(chunks)
    struct.pack_into("<H", file_e, 4, 0x20)
    stream = bytearray(32)
    stream[0], stream[1], stream[3] = 0x40, 0x01 | (0x02 if nofatchain else 0), nlen
    struct.pack_into("<Q", stream, 8, size)
    struct.pack_into("<I", stream, 20, first_cluster)
    struct.pack_into("<Q", stream, 24, size)
    names = [bytes([0x41, 0]) + c.ljust(30, b"\0") for c in chunks]
    return bytes(file_e) + bytes(stream) + b"".join(names)


def box(typ, payload=b""):
    return struct.pack(">I4s", 8 + len(payload), typ) + payload


def quicktime_moov():
    """A moov whose minf carries a second (data-reference) hdlr, as QuickTime .MOV files do."""
    mdia_hdlr = box(b"hdlr", bytes(4) + b"mhlr" + b"vide" + bytes(12))
    minf_hdlr = box(b"hdlr", bytes(4) + b"dhlr" + b"url " + bytes(12))
    stsd = box(b"stsd", bytes(4) + struct.pack(">I", 1) + box(b"avc1", bytes(78)))
    stco = box(b"stco", bytes(4) + struct.pack(">III", 2, 100, 200))
    stbl = box(b"stbl", stsd + stco)
    return box(b"moov", box(b"trak", box(b"mdia", mdia_hdlr + box(b"minf", minf_hdlr + stbl))))


def run_plan(src, *args):
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "src.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(src, f)
        return run("plan", path, *args, check=False)


GEO = {"part_offset": 0, "serial": "ABCD1234", "cluster_size": 4096, "heap_abs": 65536,
       "cluster_count": 1000, "bytes_per_sector": 512, "partition_end": 4 << 20}


class Helpers(unittest.TestCase):
    def test_base_disk(self):
        for dev, want in [("/dev/rdisk4", "disk4"), ("/dev/disk4s1", "disk4"), ("/dev/disk10s2", "disk10"),
                          ("/dev/sdb", "sdb"), ("/dev/sdb1", "sdb"), ("/dev/mmcblk0p1", "mmcblk0"),
                          ("/dev/nvme0n1p2", "nvme0n1"), ("/dev/loop3", "loop3")]:
            self.assertEqual(exfatrec.base_disk(dev), want, dev)

    def test_decode_ts(self):
        v = (46 << 25) | (9 << 21) | (23 << 16) | (19 << 11) | (55 << 5) | 1
        self.assertEqual(exfatrec.decode_ts(v, 0, 0x80 | 4), "2026-09-23 19:55:02 UTC+01:00")
        self.assertIsNone(exfatrec.decode_ts(0, 0, 0))

    def test_header_info_sony_cluster(self):
        cs = 131072
        cluster = struct.pack(">I4s4sI", 28, b"ftyp", b"XAVC", 0x01001FFF) + b"XAVCmp42iso2"
        cluster += struct.pack(">I4s", cs - 28 - 16, b"free") + bytes(cs - 28 - 16 - 8)
        cluster += struct.pack(">I4sQ", 1, b"mdat", 47882174480)
        info = exfatrec.header_info(cluster)
        self.assertEqual(info["brand"], "XAVC")
        self.assertEqual(info["declared_span"], cs - 16 + 47882174480)
        self.assertNotIn("moov_first", info)
        self.assertTrue(info["one_cluster_header"])

    def test_name_with_surrogate_pair(self):
        fs = exfatrec.ExFAT.__new__(exfatrec.ExFAT)
        fs._bitmap_entry = None
        recs = fs._parse_dir(deleted_entry_set("\U0001F3AC clip.MP4", 5, 100), "", False)
        self.assertEqual(recs[0]["name"], "\U0001F3AC clip.MP4")

    def test_trim_keeps_unknown_trailing_box(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.mp4")
            body = struct.pack(">I4s4s", 16, b"ftyp", b"isom") + bytes(4) + \
                struct.pack(">I4s", 12, b"mdat") + b"abcd" + struct.pack(">I4s", 8, b"moov") + \
                struct.pack(">I4s", 12, b"sefd") + b"\1\2\3\4"
            with open(p, "wb") as f:
                f.write(body + bytes(3000))
            self.assertEqual(exfatrec.trim_mp4(p, apply=False)["would_trim"], 3000)
            exfatrec.trim_mp4(p)
            with open(p, "rb") as f:
                self.assertEqual(f.read(), body)

    def test_refuses_output_on_the_card(self):
        dev = mock.Mock(is_device=True, path="/dev/rdisk9")
        with mock.patch.object(exfatrec, "disk_of_dir", return_value="/dev/disk9s1"):
            with self.assertRaises(SystemExit) as cm:
                exfatrec.refuse_if_on_card(dev, "/Volumes/CARD/out.MP4")
            self.assertIn("on the card itself", str(cm.exception))
        with mock.patch.object(exfatrec, "disk_of_dir", return_value=None):
            with self.assertRaises(SystemExit) as cm:
                exfatrec.refuse_if_on_card(dev, "/tmp/out.MP4")
            self.assertIn("could not tell", str(cm.exception))
        with mock.patch.object(exfatrec, "disk_of_dir", return_value="/dev/disk3s5"):
            exfatrec.refuse_if_on_card(dev, "/Users/me/out.MP4")  # another disk: allowed

    def test_quicktime_handler_is_read_from_mdia(self):
        self.assertEqual(exfatrec.moov_tracks(quicktime_moov()), [(b"vide", b"avc1", [100, 200])])

    def test_plan_refuses_overwritten_entry_without_force(self):
        rec = {"path": "/CLIP/C0001.MP4", "name": "C0001.MP4", "deleted": True, "is_dir": False, "size": 8192,
               "first_cluster": 10, "runs": [[10, 12]], "runs_method": "contiguous (NoFatChain)",
               "runs_verified": True, "overwritten_clusters": 1, "pct_now_in_use": 0.0, "mtime": None}
        p = run_plan({"geometry": GEO, "records": [rec]}, "--entry", rec["path"])
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("now used by other files", p.stderr)
        self.assertEqual(run_plan({"geometry": GEO, "records": [rec]}, "--entry", rec["path"], "--force").returncode, 0)

    def test_plan_never_copies_forward_from_a_sony_header(self):
        orphan = {"header": 50, "brand": "XAVC", "declared_span": 40960, "one_cluster_header": True, "layouts": {}}
        src = {"geometry": GEO, "range": [2, 1002], "runs": [], "orphans": {"50": orphan}}
        p = run_plan(src, "--header", "50", "--layout", "header-first", "--force")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("header-last shape", p.stderr)

    def test_forced_copy_stops_at_the_mapped_range(self):
        orphan = {"header": 10, "brand": "avc1", "unfinalized": True, "layouts": {}}
        src = {"geometry": GEO, "range": [2, 20], "runs": [], "orphans": {"10": orphan}}
        p = run_plan(src, "--header", "10", "--layout", "header-first", "--force", "--max-bytes", "400000")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)["runs"], [[10, 20]])
        self.assertIn("larger --last-cluster", p.stderr)

    def test_trim(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.mp4")
            body = struct.pack(">I4s4s", 16, b"ftyp", b"isom") + bytes(4) + \
                struct.pack(">I4s", 12, b"mdat") + b"abcd" + struct.pack(">I4s", 8, b"moov")
            with open(p, "wb") as f:
                f.write(body + bytes(5000))
            t = exfatrec.trim_mp4(p)
            self.assertTrue(t["trimmed"])
            with open(p, "rb") as f:
                self.assertEqual(f.read(), body)
            self.assertFalse(exfatrec.trim_mp4(p)["trimmed"])


def have_tools():
    if sys.platform != "darwin" or not shutil.which("hdiutil") or not shutil.which("ffmpeg"):
        return False
    enc = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    return " libx264 " in enc


@unittest.skipUnless(have_tools(), "needs macOS hdiutil and ffmpeg with libx264")
class EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="exfatrec-test-")
        t = cls.tmp
        cls.img = os.path.join(t, "card.dmg")
        subprocess.run(["hdiutil", "create", "-quiet", "-size", "256m", "-fs", "ExFAT", "-layout", "MBRSPUD",
                        "-type", "UDIF", "-volname", "CARD", cls.img], check=True)
        out = subprocess.run(["hdiutil", "attach", "-nobrowse", cls.img], capture_output=True, text=True,
                             check=True).stdout
        disk = out.split()[0]
        mnt = [l.split("\t")[-1].strip() for l in out.splitlines() if "/Volumes/" in l][0]
        try:
            clip_dir = os.path.join(mnt, "PRIVATE", "M4ROOT", "CLIP")
            os.makedirs(clip_dir)
            cls.live = make_clip(os.path.join(clip_dir, "C0001.MP4"), 3, "testsrc2")
            cls.deleted = make_clip(os.path.join(clip_dir, "C0002.MP4"), 3, "smptebars")
            subprocess.run(["sync"], check=True)
            os.remove(os.path.join(clip_dir, "C0002.MP4"))
            subprocess.run(["sync"], check=True)
        finally:
            subprocess.run(["hdiutil", "detach", "-quiet", disk], check=True)

        # Write two orphan clips (no directory entry) into free clusters, well away from C0002.
        dev = exfatrec.Device(cls.img)
        part = exfatrec.find_partition_offset(dev)
        fs = exfatrec.ExFAT(dev, part)
        cls.part, cls.cs, cls.heap_abs = part, fs.cs, fs.heap_abs
        busy = set()
        for r in fs.records:
            for s, e in fs.runs_for(r["first_cluster"], r["size"], r["nofatchain"]):
                busy.update(range(s, e))
        free_top = [c for c in range(fs.maxc - 1, 1, -1) if not fs.allocated(c) and c not in busy]

        cls.sony = to_sony_layout(make_clip(os.path.join(t, "sony.mp4"), 4, "testsrc"), fs.cs)
        m = -(-(len(cls.sony) - fs.cs) // fs.cs)   # clusters after the header
        header = free_top[0] - 2                   # leave the last clusters alone
        start = header - m
        cls.sony_header = header
        cls.plain = make_clip(os.path.join(t, "plain.mp4"), 4, "mandelbrot")
        n_plain = -(-len(cls.plain) // fs.cs)
        cls.plain_header = start - 64 - n_plain
        # A Sony clip deleted in camera: its entry survives, its FAT chain is gone.
        cls.sony3 = to_sony_layout(make_clip(os.path.join(t, "sony3.mp4"), 3, "rgbtestsrc"), fs.cs)
        m3 = -(-(len(cls.sony3) - fs.cs) // fs.cs)
        cls.sony3_header = cls.plain_header - 64
        start3 = cls.sony3_header - m3
        clip_dir = [r for r in fs.records if r["path"] == "/PRIVATE/M4ROOT/CLIP"][0]
        dir_buf = fs.read_runs(fs.runs_for(clip_dir["first_cluster"], clip_dir["size"], clip_dir["nofatchain"]))
        slot = next(i for i in range(len(dir_buf) // 32) if dir_buf[i * 32] == 0)
        dir_off = fs.cl_off(clip_dir["first_cluster"]) + slot * 32  # CLIP fits in its first cluster
        with open(cls.img, "r+b") as f:
            body = cls.sony[fs.cs:]
            for i in range(m):
                f.seek(fs.cl_off(start + i))
                f.write(body[i * fs.cs:(i + 1) * fs.cs])
            f.seek(fs.cl_off(header))
            f.write(cls.sony[:fs.cs])
            f.seek(fs.cl_off(cls.plain_header))
            f.write(cls.plain)
            body3 = cls.sony3[fs.cs:]
            for i in range(m3):
                f.seek(fs.cl_off(start3 + i))
                f.write(body3[i * fs.cs:(i + 1) * fs.cs])
            f.seek(fs.cl_off(cls.sony3_header))
            f.write(cls.sony3[:fs.cs])
            f.seek(dir_off)
            f.write(deleted_entry_set("C0003.MP4", cls.sony3_header, len(cls.sony3)))
        for c in range(start3, header + 1):
            assert not fs.allocated(c) and c not in busy, "fixture overlaps used clusters"

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def path(self, name):
        return os.path.join(self.tmp, name)

    def test_auto_partition_offset(self):
        self.assertGreater(self.part, 0)  # MBR layout: the volume does not start at byte 0

    def test_scan_and_recover_deleted_entry(self):
        run("scan", self.img, "--out", self.path("scan.json"))
        scan = load(self.path("scan.json"))
        rec = [r for r in scan["records"] if r["deleted"] and r["name"] == "C0002.MP4"]
        self.assertEqual(len(rec), 1)
        self.assertEqual(rec[0]["pct_now_in_use"], 0.0)
        run("plan", self.path("scan.json"), "--entry", rec[0]["path"], "--out", self.path("p_del.json"))
        out = self.path("deleted.MP4")
        run("extract", self.img, self.path("p_del.json"), out)
        with open(out, "rb") as f:
            self.assertEqual(f.read(), self.deleted)

    def test_map_finds_and_recovers_orphans(self):
        p = run("map", self.img, "--out", self.path("map.json"))
        self.assertIn("RECOVERABLE", p.stdout)
        orphans = load(self.path("map.json"))["orphans"]

        sony = orphans[str(self.sony_header)]
        self.assertIn("header-last", sony["layouts"])
        self.assertNotIn("header-first", sony["layouts"])
        self.assertEqual(sony["layouts"]["header-last"]["status"], "verified")
        run("plan", self.path("map.json"), "--header", self.sony_header, "--out", self.path("p_sony.json"))
        out = self.path("sony_recovered.MP4")
        run("extract", self.img, self.path("p_sony.json"), out)
        with open(out, "rb") as f:
            self.assertEqual(f.read(), self.sony)

        plain = orphans[str(self.plain_header)]
        self.assertEqual(list(plain["layouts"]), ["header-first"])
        run("plan", self.path("map.json"), "--header", self.plain_header, "--out", self.path("p_plain.json"))
        out = self.path("plain_recovered.MP4")
        run("extract", self.img, self.path("p_plain.json"), out)
        with open(out, "rb") as f:
            self.assertEqual(f.read(), self.plain)

        v = run("verify", out, "--decode", check=False)
        self.assertEqual(v.returncode, 0, v.stdout + v.stderr)
        self.assertIn("VERIFY_OK", v.stdout)

    def test_deleted_sony_entry_without_fat_chain(self):
        # The entry points at the header; the data sits before it. Reading forward would copy junk.
        run("scan", self.img, "--out", self.path("scan3.json"))
        rec = [r for r in load(self.path("scan3.json"))["records"] if r["deleted"] and r["name"] == "C0003.MP4"][0]
        self.assertTrue(rec["runs_method"].startswith("header-last"), rec["runs_method"])
        self.assertTrue(rec["runs_verified"])
        run("plan", self.path("scan3.json"), "--entry", rec["path"], "--out", self.path("p3.json"))
        out = self.path("sony3_recovered.MP4")
        run("extract", self.img, self.path("p3.json"), out)
        with open(out, "rb") as f:
            self.assertEqual(f.read(), self.sony3)

    def test_not_enough_space_refusal(self):
        run("map", self.img, "--out", self.path("map4.json"))
        run("plan", self.path("map4.json"), "--header", self.sony_header, "--out", self.path("p4.json"))
        with mock.patch.object(exfatrec.shutil, "disk_usage", return_value=mock.Mock(total=0, used=0, free=1000)):
            with self.assertRaises(SystemExit) as cm:
                exfatrec.main(["extract", self.img, self.path("p4.json"), self.path("nospace.MP4")])
        self.assertIn("not enough space", str(cm.exception))
        self.assertFalse(os.path.exists(self.path("nospace.MP4")))

    def test_verify_fails_without_video(self):
        audio = self.path("audio_only.mp4")
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=duration=1", "-c:a", "aac", "-y", audio],
                       check=True)
        v = run("verify", audio, check=False)
        self.assertEqual(v.returncode, 1)
        self.assertIn("no video stream", v.stdout)

    def test_offset_explains_carved_name(self):
        # Carvers name files after the partition-relative byte offset of the header.
        off = self.heap_abs - self.part + (self.sony_header - 2) * self.cs
        p = run("scan", self.img, "--offset", off)
        self.assertIn("box mdat", p.stdout)
        self.assertIn(f"cluster {self.sony_header:,}", p.stdout)

    def test_refusals(self):
        run("map", self.img, "--out", self.path("map2.json"))
        run("plan", self.path("map2.json"), "--header", self.sony_header, "--out", self.path("p2.json"))
        existing = self.path("exists.MP4")
        open(existing, "wb").close()
        p = run("extract", self.img, self.path("p2.json"), existing, check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("refusing to overwrite", p.stderr)

        plan = load(self.path("p2.json"))
        plan["serial"] = "DEADBEEF"
        with open(self.path("p_bad.json"), "w") as f:
            json.dump(plan, f)
        p = run("extract", self.img, self.path("p_bad.json"), self.path("never.MP4"), check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("not the card the plan was made from", p.stderr)
        self.assertFalse(os.path.exists(self.path("never.MP4")))


if __name__ == "__main__":
    unittest.main()
