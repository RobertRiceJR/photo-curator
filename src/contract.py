"""The non-destructive contract — the central invariant of this project.

Originals are READ-ONLY, forever. No stage moves, renames, deletes, or rewrites a
source file. Every artifact this pipeline produces lands under a single derived root
*outside* the library tree, so deleting that one directory returns you to the exact
pre-run state having lost nothing but compute time.

Enforced in THREE independent layers, mirroring the three-layer keyless-source
contract in the research repo this project grew out of:

  1. CONSTRUCTION  - `open_source()` is the only sanctioned way to touch an original,
     and it hard-codes mode 'rb'. Nothing in this package calls a mutator on a
     library path.
  2. CONTAINMENT   - `guard_output()` refuses any write path that resolves inside the
     library root. Derived state has exactly one home.
  3. VERIFICATION  - `snapshot()` records (relpath, size, mtime_ns) for the whole tree
     before a run; `verify()` diffs it afterward and reports any drift. This is the
     layer that makes an unattended job over 100k files trustworthy, because it
     catches what layers 1 and 2 cannot: a third-party library writing behind our back.

`self_audit()` greps this package's own source for the forbidden mutators, so layer 1
cannot rot silently as the code grows. Run it in CI, or via `run audit`.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import BinaryIO, Iterator


class ContractViolation(RuntimeError):
    """Raised when an operation would break the non-destructive contract."""


# Layer 1 ---------------------------------------------------------------------
def open_source(path: Path) -> BinaryIO:
    """The ONLY sanctioned way to read an original. Binary, read-only, no exceptions."""
    return open(path, "rb")


# Layer 2 ---------------------------------------------------------------------
def guard_output(out_path: Path, library_root: Path) -> Path:
    """Refuse to write anywhere inside the library tree.

    Returns the resolved path so callers can use it directly:
        p = guard_output(derived / "index.db", root)
    """
    out = Path(out_path).resolve()
    root = Path(library_root).resolve()
    if out == root or root in out.parents:
        raise ContractViolation(
            f"refusing to write inside the photo library: {out}\n"
            f"library root is {root}; all derived state must live outside it"
        )
    out.parent.mkdir(parents=True, exist_ok=True)
    return out


# Layer 3 ---------------------------------------------------------------------
def snapshot(root: Path, manifest: Path, exts: set[str] | None = None) -> int:
    """Record (relpath, size, mtime_ns) for every file under `root`.

    Written as JSONL so a 100k-file manifest streams instead of loading whole.
    Deliberately cheap — no hashing — because this runs before *and* after every
    stage and must never itself be the reason a run is slow.

    The extension filter is persisted in a header record and replayed by `verify`.
    That is not a convenience: when snapshot filtered to media files and verify
    walked everything, every stray `desktop.ini` the OS touched showed up as a
    contract violation. A verifier that cries wolf stops being read, which makes it
    worse than no verifier — so the two sides cannot be allowed to disagree.
    """
    manifest = guard_output(manifest, root)
    n = 0
    with open(manifest, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"_exts": sorted(exts) if exts else None}) + "\n")
        for path in _walk(root, exts):
            st = path.stat()
            rel = str(path.relative_to(root)).replace("\\", "/")
            fh.write(json.dumps({"p": rel, "s": st.st_size, "m": st.st_mtime_ns}) + "\n")
            n += 1
    return n


def verify(root: Path, manifest: Path) -> list[str]:
    """Diff the tree against a manifest. Empty list == contract held.

    Reports three drift classes, all of which mean something wrote to the library:
    missing (deleted/moved), changed (size or mtime differs), and added. Replays the
    snapshot's own extension filter so the comparison is like-for-like.
    """
    root = Path(root).resolve()
    before: dict[str, tuple[int, int]] = {}
    exts: set[str] | None = None
    with open(manifest, encoding="utf-8") as fh:
        header = json.loads(fh.readline() or "{}")
        if "_exts" in header:
            exts = set(header["_exts"]) if header["_exts"] else None
        else:  # headerless manifest from an older run — treat line 1 as data
            fh.seek(0)
        for line in fh:
            rec = json.loads(line)
            before[rec["p"]] = (rec["s"], rec["m"])

    drift: list[str] = []
    seen: set[str] = set()
    for path in _walk(root, exts):
        rel = str(path.relative_to(root)).replace("\\", "/")
        seen.add(rel)
        st = path.stat()
        if rel not in before:
            drift.append(f"ADDED    {rel}")
        elif before[rel] != (st.st_size, st.st_mtime_ns):
            drift.append(f"CHANGED  {rel}")
    for rel in before:
        if rel not in seen:
            drift.append(f"MISSING  {rel}")
    return drift


# Self-audit ------------------------------------------------------------------
_FORBIDDEN = (
    "shutil.move", "shutil.rmtree", "shutil.copyfile(",
    "os.remove", "os.unlink", "os.rename", "os.replace", "os.truncate",
    ".unlink(", ".rename(",
    # NOTE: bare `.replace(` is deliberately NOT listed. It is ambiguous — `str.replace`
    # is ubiquitous and benign, `Path.replace` is destructive — and a grep cannot tell
    # them apart without type inference. Listing it produced only false positives, which
    # is worse than not listing it: an audit that cries wolf stops being read. The
    # qualified `os.replace` above catches the common destructive form; the pathlib form
    # is covered by layers 2 and 3.
)


# Layer 2, mechanized. `db.py` is the ONLY module allowed to speak to sqlite3, because
# db.open_index() is where guard_output() lives. A module that reaches sqlite3 directly has
# routed around layer 2 — which is exactly how census() and resolve_duplicates() came to be
# able to create a database inside the photo library while all 16 tests stayed green.
# Layer 1 has a grep so it cannot rot; layer 2 needs the same.
_FUNNELLED = {
    "import sqlite3": "db.py",
    "from sqlite3": "db.py",
    "sqlite3.connect(": "db.py",
    "._open(": "db.py",          # the private opener, reached through an alias or not
}


def self_audit(package_dir: Path | None = None) -> list[str]:
    """Grep this package for contract violations. Layers 1 and 2, mechanized.

    Two rules:
      * `_FORBIDDEN` — destructive calls. The pipeline genuinely never needs any of them,
        so there is no allowlist: a hit is a finding, full stop.
      * `_FUNNELLED` — calls that belong to exactly one module. Database opens live in
        `db.py` because that is where the layer-2 guard is; anywhere else is a bypass.

    If a future stage truly needs an exception, that is a conversation and a CONTRACT.md
    amendment, not a quiet edit here.

    Known limits, stated so the results are read correctly: this is a grep, so a docstring
    outside `db.py` that mentions `import sqlite3` will false-positive, and
    `importlib.import_module("sqlite3")` slips through. Both are acceptable — the first
    costs a reworded docstring, and an audit that is honest about its reach stays trusted.
    """
    package_dir = package_dir or Path(__file__).parent
    hits: list[str] = []
    for py in sorted(package_dir.rglob("*.py")):
        if py.name == "contract.py":  # this file names them in order to ban them
            continue
        for i, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            for bad in _FORBIDDEN:
                if bad in line:
                    hits.append(f"{py.name}:{i}  {bad}  |  {line.strip()}")
            for pat, owner in _FUNNELLED.items():
                if pat in line and py.name != owner:
                    hits.append(f"{py.name}:{i}  {pat!r} outside {owner}  |  {line.strip()}")
    return hits


# Cloud placeholders ----------------------------------------------------------
# OneDrive / Dropbox / iCloud "files on demand" leave a dehydrated stub on disk: it
# stats with the real size but reading it raises OSError(EINVAL) — or, worse, silently
# triggers a multi-gigabyte download. Either outcome is unacceptable in a bulk job, so
# we detect and skip them by default rather than discovering it at 3am.
FILE_ATTRIBUTE_OFFLINE = 0x00001000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x00040000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x00400000
_CLOUD_MASK = (FILE_ATTRIBUTE_OFFLINE | FILE_ATTRIBUTE_RECALL_ON_OPEN
               | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS)


def is_cloud_placeholder(st: os.stat_result) -> bool:
    """True if these bytes are not actually on this disk. Windows-only signal."""
    return bool(getattr(st, "st_file_attributes", 0) & _CLOUD_MASK)


# Shared walk -----------------------------------------------------------------
SKIP_DIRS = {".git", "__pycache__", "$RECYCLE.BIN", "System Volume Information",
             ".thumbnails", ".derived", "derived"}


def _walk(root: Path, exts: set[str] | None) -> Iterator[Path]:
    """Walk `root`, skipping hidden and machine dirs. Read-only by construction."""
    root = Path(root).resolve()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            if exts is not None and Path(name).suffix.lower() not in exts:
                continue
            yield Path(dirpath) / name
