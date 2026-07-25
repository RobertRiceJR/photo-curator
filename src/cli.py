"""photo-curator CLI — the single entry point, mirroring the orchestrator pattern.

    run inventory --root <library>   # stage 0: walk, fingerprint, index  (READ-ONLY)
    run census                       # what your library actually is
    run dupes                        # full sha256 for assets reached by several paths
    run snapshot --root <library>    # record the tree state (contract layer 3)
    run verify   --root <library>    # diff against the snapshot; proves nothing moved
    run audit                        # grep this package for destructive calls (layer 1)

Every command is read-only with respect to the library. `--derived` selects where
state lands and defaults to ./derived next to this repo, never inside the library.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import contract          # noqa: E402
import db                # noqa: E402
import inventory         # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DERIVED = ROOT / "derived"


def cmd_inventory(args) -> int:
    root, derived = Path(args.root), Path(args.derived)
    print(f"photo-curator · stage 0 inventory\n  library : {root}\n  derived : {derived}")
    print(f"  mode    : READ-ONLY (contract layers 1+2 active)\n")
    stats = inventory.run(root, derived, rescan=args.rescan, hydrate=args.hydrate)
    print(f"\n  walked {stats['seen']:,} files in {stats['elapsed']:.1f}s")
    print(f"    {stats['new']:,} newly indexed · {stats['skipped']:,} already cached · "
          f"{stats['placeholder']:,} cloud placeholders skipped · "
          f"{stats['failed']:,} unreadable")
    print(f"  index   : {stats['db']}")
    print(inventory.census(derived))
    return 0


def _default_roots() -> list[Path]:
    """Home plus every non-system drive root.

    Personal photos live overwhelmingly under the user profile (Pictures, OneDrive,
    Desktop) or on a secondary/external drive. Scanning all of C:\\ is mostly a tour of
    Windows and Program Files, so it is opt-in via explicit --roots rather than default.
    """
    roots = [Path.home()]
    for letter in "DEFGHIJKLMNOPQRSTUVWXYZ":
        drive = Path(f"{letter}:\\")
        if drive.exists():
            roots.append(drive)
    return roots


def cmd_discover(args) -> int:
    import discover
    roots = [Path(r) for r in args.roots] if args.roots else _default_roots()
    print("photo-curator · library discovery (read-only, stat only)")
    for r in roots:
        print(f"  scanning: {r}")
    print("  this can take a few minutes on a large or cold drive...\n")
    print(discover.report(discover.scan(roots, min_files=args.min_files)))
    return 0


def cmd_census(args) -> int:
    print(inventory.census(Path(args.derived)))
    return 0


def cmd_dupes(args) -> int:
    n = inventory.resolve_duplicates(Path(args.derived))
    print(f"recorded a full sha256 for {n:,} multi-path asset(s)")
    if n:
        print("  note: this digests ONE member per group — it does not prove the members are\n"
              "        byte-identical. See inventory.resolve_duplicates for the known gap.")
    return 0


def cmd_snapshot(args) -> int:
    root, derived = Path(args.root), Path(args.derived)
    manifest = derived / "manifest.jsonl"
    n = contract.snapshot(root, manifest, inventory.MEDIA_EXTS)
    print(f"snapshot: {n:,} files recorded -> {manifest}")
    return 0


def cmd_verify(args) -> int:
    manifest = Path(args.derived) / "manifest.jsonl"
    if not manifest.exists():
        print(f"no snapshot at {manifest} — run `snapshot` before the run you want to verify")
        return 1
    drift = contract.verify(Path(args.root), manifest)
    if not drift:
        print("CONTRACT HELD — library byte-identical to snapshot")
        return 0
    print(f"CONTRACT VIOLATED — {len(drift):,} file(s) drifted:")
    for line in drift[:50]:
        print(f"  {line}")
    if len(drift) > 50:
        print(f"  ... and {len(drift) - 50:,} more")
    return 2


def cmd_audit(args) -> int:
    hits = contract.self_audit(Path(__file__).parent)
    if not hits:
        print("audit clean — no destructive calls, and every DB open is inside db.py")
        return 0
    print(f"AUDIT FAILED — {len(hits)} contract violation(s):")
    for h in hits:
        print(f"  {h}")
    return 2


def cmd_test(args) -> int:
    import unittest
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="run", description="photo-curator")
    p.add_argument("--derived", default=str(DEFAULT_DERIVED),
                   help="where derived state lands (never inside the library)")
    sub = p.add_subparsers(dest="cmd", required=True)

    inv = sub.add_parser("inventory", help="stage 0: walk + index the library (read-only)")
    inv.add_argument("--root", required=True, help="photo library root")
    inv.add_argument("--rescan", action="store_true", help="ignore the resume cache")
    inv.add_argument("--hydrate", action="store_true",
                     help="read cloud placeholders too (forces a download — check disk first)")

    dis = sub.add_parser("discover", help="find candidate photo libraries on this machine")
    dis.add_argument("--roots", nargs="*", default=None,
                     help="where to look (default: home + every non-C: drive root)")
    dis.add_argument("--min-files", type=int, default=25,
                     help="ignore folders with fewer media files than this")

    sub.add_parser("census", help="report what the library contains")

    # No --root: the library root is recovered from the index (db.recorded_root). The old
    # flag defaulted to the CWD and was then ignored — worse than not having it.
    sub.add_parser("dupes", help="record a full sha256 for assets that have several paths")

    for name, help_text in (("snapshot", "record tree state"), ("verify", "diff tree vs snapshot")):
        sp = sub.add_parser(name, help=help_text)
        sp.add_argument("--root", required=True)

    sub.add_parser("audit", help="grep src/ for destructive calls")
    sub.add_parser("test", help="run the test suite")

    args = p.parse_args(argv)
    dispatch = {"inventory": cmd_inventory, "discover": cmd_discover, "census": cmd_census,
                "dupes": cmd_dupes, "snapshot": cmd_snapshot, "verify": cmd_verify,
                "audit": cmd_audit, "test": cmd_test}
    # A contract violation is a refusal, not a crash: exit cleanly with a code the caller
    # (and the call-path tests) can assert on. 2 matches cmd_verify's drift code.
    try:
        return dispatch[args.cmd](args)
    except contract.ContractViolation as e:
        print(f"CONTRACT VIOLATION — refusing to run\n  {e}")
        return 2
    except db.IndexMissing as e:
        print(f"{e}\n  run `inventory --root <library>` first")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
