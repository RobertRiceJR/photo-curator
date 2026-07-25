"""Tests that assert the CALL PATH is guarded — not just that the guard function works.

`test_core.TestContract.test_guard_rejects_writes_inside_library` proves
`contract.guard_output()` raises when handed a bad path. It says nothing about whether any
code path actually calls it. That gap is not hypothetical: `inventory.census()` and
`inventory.resolve_duplicates()` opened the SQLite index with no guard at all, and a fully
green 16-test suite never noticed, because every test exercised the guard in isolation.

So these tests drive `cli.main()` from the front door with `--derived` pointed *inside* a
fake library, and assert on the filesystem afterward.

Two deliberate choices worth knowing:

  * The oracle is `Path.rglob`, NOT `contract.verify`. `contract.SKIP_DIRS` contains
    "derived" and `_walk` prunes it, so layer 3 is structurally blind to a database created
    at `<library>/derived/index.db` — the exact artifact these tests hunt. Using `verify`
    here would report a clean library while the bug sat inside it. T7 asserts that blindness
    on purpose so it is documented rather than rediscovered.
  * `db.connect()` is a WRITE site, not a read: it mkdir's the parent, creates the .db plus
    WAL sidecars, and runs DDL. "Just opening the index" is what puts files in your library.
"""
from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import cli          # noqa: E402
import contract     # noqa: E402
import db           # noqa: E402
import inventory    # noqa: E402


class CallPathCase(unittest.TestCase):
    """A fake library with real (tiny) JPEGs, and a derived dir safely outside it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root = self.base / "library"
        (self.root / "2019" / "trip").mkdir(parents=True)
        (self.root / "2019" / "trip" / "a.jpg").write_bytes(b"\xff\xd8" + b"a" * 4000)
        (self.root / "2019" / "b.jpg").write_bytes(b"\xff\xd8" + b"b" * 4000)
        self.outside = self.base / "derived"

    def tearDown(self):
        self.tmp.cleanup()

    def tree(self) -> set[str]:
        """Every path under the library, at the OS level.

        Deliberately not contract._walk — that prunes `derived/`, which would hide the
        very files this asserts the absence of.
        """
        return {str(p.relative_to(self.root)).replace("\\", "/")
                for p in self.root.rglob("*")}

    def run_cli(self, *argv: str) -> int:
        with contextlib.redirect_stdout(io.StringIO()):
            return cli.main(list(argv))


class TestNoStateInsideLibrary(CallPathCase):

    def test_no_command_can_put_state_inside_the_library(self):
        """T1 — the test that fails today.

        Three placements of --derived inside the library x three commands. Pre-fix:
        `inventory` raises ContractViolation (an ERROR, which is still red and still
        correct); `census` and `dupes` each create derived/, index.db and journal files
        under the library and return 0, failing both assertions.
        """
        before = self.tree()
        placements = {
            "root": self.root,                              # derived == the library itself
            "obvious": self.root / "derived",               # the natural mistake
            "buried": self.root / "2019" / "trip" / "d",    # deep inside
        }
        commands = {
            "inventory": ["inventory", "--root", str(self.root)],
            "census": ["census"],
            "dupes": ["dupes"],
        }
        for where, derived in placements.items():
            for name, argv in commands.items():
                with self.subTest(derived=where, cmd=name):
                    rc = self.run_cli("--derived", str(derived), *argv)
                    self.assertNotEqual(rc, 0, f"{name} reported success")
                    self.assertEqual(self.tree(), before,
                                     f"{name} created state inside the library")

    def test_the_happy_path_still_works(self):
        """T2 — positive control. A guard that refuses everything would pass T1."""
        self.assertEqual(
            self.run_cli("--derived", str(self.outside), "inventory", "--root", str(self.root)), 0)
        self.assertTrue((self.outside / db.INDEX_NAME).is_file())
        self.assertEqual(self.run_cli("--derived", str(self.outside), "census"), 0)
        self.assertEqual(self.run_cli("--derived", str(self.outside), "dupes"), 0)

    def test_later_stages_recover_the_root_without_being_told(self):
        """T3 — the ordering fact the whole design leans on.

        census and dupes run AFTER inventory, so the library root is already knowable
        from prior state. That is why they need no --root of their own.
        """
        self.assertIsNone(db.recorded_root(self.outside))
        self.run_cli("--derived", str(self.outside), "inventory", "--root", str(self.root))
        self.assertEqual(db.recorded_root(self.outside), self.root.resolve())

    def test_every_index_open_is_vetted_by_guard_output(self):
        """T4 — assert the guard was CONSULTED, not merely satisfied.

        T1 can pass by luck: a tempdir that happens to sit outside the library satisfies
        the contract without anyone checking. This cannot. It is also the test that keeps
        the property true for stages 1-9 — a new command that opens the index adds one
        line here, and forgetting the guard fails loudly.
        """
        self.run_cli("--derived", str(self.outside), "inventory", "--root", str(self.root))
        commands = {
            "inventory": ["inventory", "--root", str(self.root)],
            "census": ["census"],
            "dupes": ["dupes"],
        }
        for name, argv in commands.items():
            with self.subTest(cmd=name):
                seen: list[str] = []
                real = contract.guard_output

                def spy(out_path, library_root, _seen=seen, _real=real):
                    _seen.append(Path(out_path).name)
                    return _real(out_path, library_root)

                with mock.patch.object(contract, "guard_output", spy):
                    self.run_cli("--derived", str(self.outside), *argv)
                self.assertIn(db.INDEX_NAME, seen,
                              f"{name} opened the index without consulting guard_output")

    def test_read_stages_cannot_bring_an_index_into_being(self):
        """T5 — containment by incapacity.

        A stage that *cannot* create a database cannot create one inside your library.
        That is strictly stronger than being checked before creating one, and it is why
        census needs no library root at all.
        """
        missing = self.base / "never-inventoried"
        with self.assertRaises(db.IndexMissing):
            db.open_existing(missing)
        self.assertFalse(missing.exists(), "open_existing created the derived directory")

        self.assertEqual(self.run_cli("--derived", str(missing), "census"), 1)
        self.assertEqual(self.run_cli("--derived", str(missing), "dupes"), 1)
        self.assertFalse(missing.exists(), "a read stage created the derived directory")


class TestFunnelAudit(unittest.TestCase):

    def test_audit_flags_a_db_opened_outside_the_funnel(self):
        """T6 — protects the stages that do not exist yet.

        Stage 3 imports sqlite3, calls connect() directly, and reopens this exact hole.
        Every other test in this file would still pass. This is the one that wouldn't.
        """
        with tempfile.TemporaryDirectory() as td:
            pkg = Path(td)
            (pkg / "stage3_faces.py").write_text(
                "import sqlite3\n\n\ndef go(p):\n    return sqlite3.connect(p)\n",
                encoding="utf-8")
            hits = contract.self_audit(pkg)
        self.assertTrue(any("sqlite3" in h for h in hits), f"audit missed it: {hits}")
        self.assertTrue(any("stage3_faces.py" in h for h in hits), hits)


class TestLayer3BlindSpot(CallPathCase):

    def test_layer3_is_blind_to_a_pruned_directory_name(self):
        """T7 — characterization, not a bug report.

        `run verify` reports CONTRACT HELD for a database created at
        <library>/derived/index.db, because contract.SKIP_DIRS prunes the directory name.
        Asserting it here means the next person reads it instead of rediscovering it, and
        it explains why layer 2 has to be structural rather than one more check.
        """
        manifest = self.base / "m.jsonl"
        contract.snapshot(self.root, manifest, None)
        (self.root / "derived").mkdir()
        (self.root / "derived" / "index.db").write_bytes(b"SQLite format 3\x00")
        self.assertEqual(contract.verify(self.root, manifest), [],
                         "layer 3 now sees pruned dirs — update this test and CONTRACT.md")


if __name__ == "__main__":
    unittest.main(verbosity=2)
