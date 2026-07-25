"""Tests for the parts that must not be silently wrong.

Three things here are load-bearing enough to deserve real coverage:

  * The metadata reader. Capture date is the backbone of the whole design — time-bucketed
    face clustering is meaningless if dates are wrong — and a reader that quietly returns
    None looks exactly like a library with no EXIF.
  * The contract. An invariant with no test is a wish.
  * The stage-0 pipeline itself, including the branches that had never once executed.

`build_exif_jpeg` is kept from the hand-rolled-parser era, and it is worth MORE now than it
was then. It assembles a TIFF/Exif block byte by byte from the spec, and Pillow — an
independent implementation that has read every camera file in the world — parses it back.
Two implementations agreeing on hand-built bytes is real cross-validation; previously the
fixture and the parser shared an author, so a shared misconception passed green.

Stdlib unittest, no pytest:  .\run test
"""
from __future__ import annotations

import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import contract      # noqa: E402
import db            # noqa: E402
import inventory     # noqa: E402
import meta          # noqa: E402


# --------------------------------------------------------------------------
# Synthetic JPEG with a known Exif block, assembled from the TIFF spec
# --------------------------------------------------------------------------
def build_exif_jpeg(make=b"Apple\x00", model=b"iPhone 12\x00",
                    dt=b"2019:07:04 11:23:07\x00", orientation=6,
                    width=4032, height=3024) -> bytes:
    """Hand-assemble a little-endian TIFF/Exif block inside a minimal JPEG.

    Offsets are computed rather than hard-coded so the fixture stays correct if the string
    lengths change. DateTimeOriginal deliberately lives in the Exif sub-IFD, not IFD0 —
    that is where real cameras put it, and reading only IFD0 is the single easiest way to
    get a null date out of a file that has one.
    """
    ifd0_at = 8
    n0 = 4
    data_at = ifd0_at + 2 + n0 * 12 + 4
    make_at = data_at
    model_at = make_at + len(make)
    exif_ifd_at = model_at + len(model)
    n1 = 3
    dt_at = exif_ifd_at + 2 + n1 * 12 + 4

    def entry(tag, typ, count, value_bytes):
        assert len(value_bytes) == 4
        return struct.pack("<HHI", tag, typ, count) + value_bytes

    ifd0 = struct.pack("<H", n0)
    ifd0 += entry(0x010F, 2, len(make), struct.pack("<I", make_at))
    ifd0 += entry(0x0110, 2, len(model), struct.pack("<I", model_at))
    ifd0 += entry(0x0112, 3, 1, struct.pack("<HH", orientation, 0))
    ifd0 += entry(0x8769, 4, 1, struct.pack("<I", exif_ifd_at))
    ifd0 += struct.pack("<I", 0)

    exif_ifd = struct.pack("<H", n1)
    exif_ifd += entry(0x9003, 2, len(dt), struct.pack("<I", dt_at))
    exif_ifd += entry(0xA002, 4, 1, struct.pack("<I", width))
    exif_ifd += entry(0xA003, 4, 1, struct.pack("<I", height))
    exif_ifd += struct.pack("<I", 0)

    tiff = b"II" + struct.pack("<HI", 42, ifd0_at) + ifd0 + make + model + exif_ifd + dt
    app1 = b"Exif\x00\x00" + tiff

    # A real (if tiny) baseline JPEG so Pillow will actually open it: SOF0 + a quantisation
    # table + a Huffman table would be needed for a decodable image, but Image.open only
    # parses headers lazily, which is all read_meta needs.
    sof = struct.pack(">BHHB", 8, height, width, 3) + b"\x01\x11\x00\x02\x11\x01\x03\x11\x01"
    return (b"\xff\xd8"
            + b"\xff\xe1" + struct.pack(">H", len(app1) + 2) + app1
            + b"\xff\xc0" + struct.pack(">H", len(sof) + 2) + sof
            + b"\xff\xda" + struct.pack(">H", 2)
            + b"\xff\xd9")


class TmpCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()


class TestMetaReader(TmpCase):

    def _meta(self, blob: bytes, name="IMG_0001.jpg") -> dict:
        p = self.dir / name
        p.write_bytes(blob)
        with open(p, "rb") as fh:
            return meta.read_meta(fh, p.suffix.lower())

    def test_reads_full_exif_from_hand_built_bytes(self):
        """Cross-validation: Pillow agrees with a block assembled from the spec."""
        m = self._meta(build_exif_jpeg())
        self.assertEqual(m["captured_at"], "2019-07-04T11:23:07")
        self.assertEqual(m["capture_source"], "exif")
        self.assertEqual(m["make"], "Apple")
        self.assertEqual(m["model"], "iPhone 12")
        self.assertEqual(m["orientation"], 6)
        self.assertEqual((m["width"], m["height"]), (4032, 3024))

    def test_rejects_zero_placeholder_date(self):
        """'0000:00:00 00:00:00' is a real thing cameras write. It is not a date."""
        m = self._meta(build_exif_jpeg(dt=b"0000:00:00 00:00:00\x00"))
        self.assertIsNone(m["captured_at"])
        self.assertEqual((m["width"], m["height"]), (4032, 3024))

    def test_jpeg_without_exif_yields_dims_only(self):
        blob = build_exif_jpeg()
        stripped = b"\xff\xd8" + blob[blob.index(b"\xff\xc0"):]
        m = self._meta(stripped)
        self.assertIsNone(m["captured_at"])
        self.assertEqual((m["width"], m["height"]), (4032, 3024))

    def test_truncated_file_does_not_raise(self):
        """One corrupt photo in 100k must not kill an overnight run."""
        self.assertIsNone(self._meta(build_exif_jpeg()[:40])["captured_at"])

    def test_garbage_does_not_raise(self):
        self.assertIsNone(self._meta(b"\xff\xd8" + bytes(range(256)) * 8)["captured_at"])

    def test_extension_is_not_load_bearing(self):
        """Pillow sniffs the container, so a mislabelled file still reads correctly."""
        m = self._meta(build_exif_jpeg(), name="actually_a_jpeg.png")
        self.assertEqual(m["captured_at"], "2019-07-04T11:23:07")

    @unittest.skipUnless(meta.HEIF_OK, "pillow-heif not installed")
    def test_heic_reads(self):
        """HEIC support is the reason for the pillow-heif dependency.

        Verified against real iPhone 8/13/14 HEICs during development; this generates one
        so the suite stays self-contained and does not reference personal files.
        """
        from PIL import Image
        p = self.dir / "IMG_9999.heic"
        Image.new("RGB", (64, 48), (10, 120, 200)).save(p, format="HEIF")
        with open(p, "rb") as fh:
            m = meta.read_meta(fh, ".heic")
        self.assertEqual((m["width"], m["height"]), (64, 48))

    @unittest.skipUnless(os.environ.get("PHOTO_CURATOR_FIXTURES"),
                         "set PHOTO_CURATOR_FIXTURES=<dir of real camera files>")
    def test_real_camera_files(self):
        """Opt-in regression against a directory of genuine camera files.

        Not committed with a hard-coded path: fixtures are personal. Point the env var at
        a folder of real photos and this asserts a majority carry a parseable capture date.
        """
        root = Path(os.environ["PHOTO_CURATOR_FIXTURES"])
        files = [p for p in root.rglob("*") if p.suffix.lower() in inventory.IMAGE_EXTS][:200]
        self.assertTrue(files, f"no images under {root}")
        with_exif = sum(1 for p in files if meta.read_meta_path(p)["capture_source"] == "exif")
        self.assertGreater(with_exif, len(files) * 0.5,
                           f"only {with_exif}/{len(files)} carried EXIF — reader may be broken")


class TestDateFallbacks(unittest.TestCase):

    def test_filename_patterns(self):
        for name, want in [
            ("IMG_20190704_112307.jpg", "2019-07-04"),
            ("PXL_20231225_010203456.jpg", "2023-12-25"),
            ("Screenshot 2024-03-09 at 10.11.12.png", "2024-03-09"),
            ("2015.08.21 birthday.jpg", "2015-08-21"),
            ("20230420_141147(1).jpg", "2023-04-20"),
        ]:
            with self.subTest(name=name):
                got = meta.date_from_filename(name)
                self.assertIsNotNone(got, name)
                self.assertTrue(got.startswith(want), f"{name} -> {got}")

    def test_rejects_non_dates(self):
        self.assertIsNone(meta.date_from_filename("DSC_1234.jpg"))
        self.assertIsNone(meta.date_from_filename("19991350.jpg"))   # month 13
        self.assertIsNone(meta.date_from_filename("18500101.jpg"))   # too old

    def test_precedence_exif_beats_filename_beats_mtime(self):
        p = Path("IMG_20200101_000000.jpg")
        self.assertEqual(meta.best_date({"captured_at": "2019-07-04T11:23:07"}, p, 0)[1], "exif")
        self.assertEqual(meta.best_date({}, p, 0)[1], "filename")
        self.assertEqual(meta.best_date({}, Path("DSC_1.jpg"), 0)[1], "mtime")


class TestContract(TmpCase):
    def setUp(self):
        super().setUp()
        self.root = self.dir / "library"
        (self.root / "sub").mkdir(parents=True)
        (self.root / "sub" / "a.jpg").write_bytes(b"x" * 1000)

    def test_guard_rejects_writes_inside_library(self):
        with self.assertRaises(contract.ContractViolation):
            contract.guard_output(self.root / "index.db", self.root)
        with self.assertRaises(contract.ContractViolation):
            contract.guard_output(self.root / "deep" / "nested" / "x.db", self.root)

    def test_guard_allows_writes_outside_library(self):
        out = contract.guard_output(self.dir / "derived" / "index.db", self.root)
        self.assertTrue(out.parent.is_dir())

    def test_verify_detects_each_drift_class(self):
        man = self.dir / "derived" / "m.jsonl"
        contract.snapshot(self.root, man, None)
        self.assertEqual(contract.verify(self.root, man), [])

        (self.root / "sub" / "b.jpg").write_bytes(b"y" * 10)
        self.assertTrue(any(d.startswith("ADDED") for d in contract.verify(self.root, man)))

        (self.root / "sub" / "a.jpg").write_bytes(b"z" * 2000)
        self.assertTrue(any(d.startswith("CHANGED") for d in contract.verify(self.root, man)))

    def test_verify_replays_snapshot_extension_filter(self):
        """Regression: a filtered snapshot must not flag out-of-filter files as ADDED.

        Found on the first real run — snapshot filtered to media extensions while verify
        walked everything, so OneDrive's desktop.ini files read as contract violations. A
        false-positive verifier is worse than none.
        """
        man = self.dir / "derived" / "filtered.jsonl"
        contract.snapshot(self.root, man, {".jpg"})
        (self.root / "desktop.ini").write_text("[.ShellClassInfo]")
        (self.root / "sub" / "notes.txt").write_text("hello")
        self.assertEqual(contract.verify(self.root, man), [])

        (self.root / "sub" / "c.jpg").write_bytes(b"q" * 10)
        self.assertTrue(any(d.startswith("ADDED") for d in contract.verify(self.root, man)))

    def test_self_audit_is_clean(self):
        """Must pass on our own source, and must not cry wolf.

        Also pins db.py as the sole owner of sqlite3 for free — see contract._FUNNELLED.
        """
        self.assertEqual(contract.self_audit(), [])


class TestFingerprint(TmpCase):

    def _fp(self, data: bytes, name="f.bin") -> str:
        p = self.dir / name
        p.write_bytes(data)
        return inventory.fingerprint(p, len(data))

    def test_small_file_no_tail_read(self):
        """Files under 2 chunks must not attempt a seek-from-end."""
        self.assertEqual(self._fp(b"abc", "s1.bin"), self._fp(b"abc", "s2.bin"))

    def test_large_file_stable_and_discriminating(self):
        big = b"a" * (600 * 1024)
        self.assertEqual(self._fp(big, "l1.bin"), self._fp(big, "l2.bin"))
        self.assertNotEqual(self._fp(big, "l3.bin"), self._fp(big + b"z", "l4.bin"))

    def test_same_size_different_tail_differs(self):
        """The tail read is what makes same-size files distinguishable."""
        n = 600 * 1024
        self.assertNotEqual(self._fp(b"a" * n, "t1.bin"),
                            self._fp(b"a" * (n - 1) + b"b", "t2.bin"))

    def test_full_sha256_matches_hashlib(self):
        import hashlib
        data = b"pineapple" * 100_000
        p = self.dir / "full.bin"
        p.write_bytes(data)
        self.assertEqual(inventory.full_sha256(p), hashlib.sha256(data).hexdigest())


class TestFingerprintCollision(TmpCase):
    """Characterization, not a bug report — the same idiom as test_contract_paths T7.

    The two-phase hash is sold as "cheap by default, exact where it counts". The exact
    half does not hold, and the reason it went unnoticed is this repo's characteristic
    failure mode showing up a third time: `test_duplicates_collapse_to_one_asset_and_resolve`
    only ever writes a byte-IDENTICAL copy, so the phase that exists to catch a false
    collision has never been handed one. (Previously: the contract guard was tested in
    isolation while two call paths bypassed it, and the hand-rolled EXIF parser shared an
    author with its own fixture.) A test that can only pass is not coverage.

    Reachability is low for camera JPEGs — a per-shot EXIF timestamp sits inside the head
    256 KB — but it is not zero for machine-generated files from one encoder, or for large
    video containers whose head and tail are header and index.

    When asset-splitting lands, this test SHOULD fail. That is the point of writing it down.
    """

    def _collide(self):
        """Two different files: same size, same head 256 KB, same tail 256 KB."""
        root = self.dir / "library"
        root.mkdir(parents=True)
        head, tail = b"H" * inventory._CHUNK, b"T" * inventory._CHUNK
        (root / "photo_a.jpg").write_bytes(head + b"A" * 4096 + tail)
        (root / "photo_b.jpg").write_bytes(head + b"B" * 4096 + tail)
        return root

    def test_distinct_files_share_a_fingerprint(self):
        """The premise. If this ever fails, the rest of the class is moot."""
        root = self._collide()
        a, b = root / "photo_a.jpg", root / "photo_b.jpg"
        self.assertNotEqual(a.read_bytes(), b.read_bytes())
        self.assertEqual(a.stat().st_size, b.stat().st_size)
        self.assertEqual(inventory.fingerprint(a, a.stat().st_size),
                         inventory.fingerprint(b, b.stat().st_size))

    def test_collision_collapses_two_photos_into_one_asset(self):
        """The cost: one asset row for two distinct images, and b's metadata is gone.

        `upsert_asset` is ON CONFLICT DO NOTHING, so photo_b's dimensions, capture date and
        camera are discarded at insert time and its path row resolves to photo_a's row.
        """
        root = self._collide()
        derived = self.dir / "derived"
        stats = inventory.run(root, derived)
        self.assertEqual(stats["seen"], 2)
        self.assertEqual(stats["new"], 1, "the second file was indexed as its own asset")

        con = db.open_existing(derived)
        n_assets = con.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
        n_paths = con.execute("SELECT COUNT(*) FROM paths").fetchone()[0]
        con.close()
        self.assertEqual((n_assets, n_paths), (1, 2))

    def test_resolve_duplicates_does_not_detect_the_collision(self):
        """`dupes` digests ONE member and cannot notice the other differs."""
        import hashlib
        root = self._collide()
        derived = self.dir / "derived"
        inventory.run(root, derived)

        self.assertEqual(inventory.resolve_duplicates(derived), 1)

        con = db.open_existing(derived)
        stored = con.execute("SELECT sha256 FROM assets").fetchone()[0]
        con.close()
        true = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                for p in (root / "photo_a.jpg", root / "photo_b.jpg")}
        self.assertNotEqual(true["photo_a.jpg"], true["photo_b.jpg"])
        self.assertIn(stored, true.values())
        # The whole finding in one line: one of the two real digests is simply unrecorded.
        self.assertEqual(sum(stored == d for d in true.values()), 1)


class TestStageZero(TmpCase):
    """The pipeline end to end, including branches that had never once executed."""

    def setUp(self):
        super().setUp()
        self.root = self.dir / "library"
        (self.root / "2019").mkdir(parents=True)
        (self.root / "2019" / "IMG_20190704_112307.jpg").write_bytes(build_exif_jpeg())
        (self.root / "2019" / "no_date.jpg").write_bytes(b"\xff\xd8" + b"n" * 3000)
        self.derived = self.dir / "derived"

    def _run(self, **kw):
        return inventory.run(self.root, self.derived, **kw)

    def test_indexes_and_reports(self):
        stats = self._run()
        self.assertEqual(stats["seen"], 2)
        self.assertEqual(stats["new"], 2)
        self.assertEqual(stats["failed"], 0)

        report = inventory.census(self.derived)
        self.assertIn("LIBRARY CENSUS", report)
        self.assertIn("unique assets : 2", report)
        self.assertIn("exif", report)       # the EXIF file's provenance row
        self.assertIn("2019", report)       # both files date to 2019 (exif / filename)

    def test_second_run_is_resumed_not_rehashed(self):
        self._run()
        again = self._run()
        self.assertEqual(again["skipped"], 2)
        self.assertEqual(again["new"], 0)

    def test_rescan_ignores_the_resume_cache(self):
        self._run()
        again = self._run(rescan=True)
        self.assertEqual(again["skipped"], 0)

    def test_duplicates_collapse_to_one_asset_and_resolve(self):
        """resolve_duplicates had zero coverage and had never been executed."""
        dupe = self.root / "2019" / "copy_of_IMG_20190704_112307.jpg"
        dupe.write_bytes((self.root / "2019" / "IMG_20190704_112307.jpg").read_bytes())
        self._run()

        con = db.open_existing(self.derived)
        n_assets = con.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
        n_paths = con.execute("SELECT COUNT(*) FROM paths").fetchone()[0]
        con.close()
        self.assertEqual((n_assets, n_paths), (2, 3))   # one asset, two paths

        self.assertEqual(inventory.resolve_duplicates(self.derived), 1)

        con = db.open_existing(self.derived)
        sha = con.execute("SELECT sha256 FROM assets WHERE sha256 IS NOT NULL").fetchone()
        con.close()
        self.assertIsNotNone(sha, "the duplicate group got no full digest")
        self.assertEqual(len(sha[0]), 64)

        # Idempotent: a second pass re-confirms without recomputing.
        self.assertEqual(inventory.resolve_duplicates(self.derived), 1)

    def test_cloud_placeholders_are_skipped_by_default(self):
        """The --hydrate branch had never been exercised either way."""
        with mock.patch.object(contract, "is_cloud_placeholder", return_value=True):
            stats = self._run()
        self.assertEqual(stats["placeholder"], 2)
        self.assertEqual(stats["new"], 0)

        # An all-placeholder library indexes nothing, so census hits its empty path. It must
        # NOT say "run inventory first" — inventory did run. This is the real iCloudPhotos
        # case (33,095 files, 100% stubs) and the misleading message would have shipped.
        report = inventory.census(self.derived)
        self.assertIn("CLOUD PLACEHOLDERS", report)
        self.assertIn("DID run", report)
        self.assertNotIn("run `inventory` first", report)

    def test_hydrate_reads_placeholders_anyway(self):
        with mock.patch.object(contract, "is_cloud_placeholder", return_value=True):
            stats = self._run(hydrate=True)
        self.assertEqual(stats["placeholder"], 0)
        self.assertEqual(stats["new"], 2)

    def test_is_cloud_placeholder_false_for_ordinary_file(self):
        st = (self.root / "2019" / "no_date.jpg").stat()
        self.assertFalse(contract.is_cloud_placeholder(st))

    def test_interrupted_run_still_closes_its_ledger(self):
        """A killed walk must leave a finished, labelled run row — not an open one.

        `finish_run` used to sit AFTER the try/finally, so any exception (Ctrl-C included)
        committed the rows and then skipped the ledger, leaving finished_at NULL forever and
        leaking the connection. A stage whose selling point is "resume from where it died"
        cannot have an audit trail that cannot say where it died.
        """
        boom = KeyboardInterrupt("simulated Ctrl-C")
        with mock.patch.object(inventory, "fingerprint", side_effect=boom):
            with self.assertRaises(KeyboardInterrupt):
                self._run()

        con = db.open_existing(self.derived)
        row = con.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        con.close()
        self.assertIsNotNone(row["finished_at"], "interrupted run left an open ledger row")
        self.assertIn("interrupted", row["notes"])

    def test_completed_run_is_labelled_ok(self):
        """Positive control — a labeller that always says 'interrupted' would pass above."""
        self._run()
        con = db.open_existing(self.derived)
        row = con.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        con.close()
        self.assertIn("exit=ok", row["notes"])

    def test_census_on_empty_index_does_not_pretend(self):
        self._run()
        con = db.open_index(self.derived, self.root)
        con.execute("DELETE FROM assets")
        con.commit()
        con.close()
        self.assertIn("empty", inventory.census(self.derived))


if __name__ == "__main__":
    unittest.main(verbosity=2)
