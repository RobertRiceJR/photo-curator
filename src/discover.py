"""Find the photo library. Read-only reconnaissance, run before anything else.

You cannot census a library you cannot locate. This walks one or more roots, counts
media files per directory, and reports the directories that look like a photo library —
plus, critically, what share of each is a **cloud placeholder** rather than real bytes
on this disk. A 100k-file library that is 90% dehydrated stubs is a different project
than one that is local, and you want to know which you have before planning anything.

Written in Python rather than PowerShell on purpose: it reuses `contract.is_cloud_placeholder`
and `inventory.MEDIA_EXTS` directly. A parallel PowerShell implementation of the placeholder
bitmask would be a second copy of the same rule, free to drift from the first — which is the
exact class of defect this codebase has been paying down.

Two views, because a family archive is usually nested (`Photos/2019/Disney/`):
  * BY FOLDER  — where the files literally sit; finds the leaves.
  * BY SUBTREE — rolled up into ancestors; finds the ROOT you would point `inventory` at.
"""
from __future__ import annotations

import os
from collections import defaultdict
from pathlib import Path

import contract
import inventory

# Directories that are never a personal photo library but are full of images (icons,
# texture atlases, browser caches). Skipping them keeps the report readable and the
# walk fast; they are additive to contract.SKIP_DIRS, which handles OS/VCS noise.
NOISE_DIRS = {
    "appdata", "program files", "program files (x86)", "programdata", "windows",
    "node_modules", ".venv", "venv", "env", "site-packages", "temp", "tmp",
    "cache", "caches", "$windows.~bt", "msocache", "perflogs", "recovery",
    ".cache", ".npm", ".nuget", ".gradle", ".vscode", ".idea", "dist-info",
}

MIN_FILES = 25  # a directory below this is noise, not a library


def _is_noise(dirpath: str) -> bool:
    return any(part.lower() in NOISE_DIRS for part in Path(dirpath).parts)


def scan(roots: list[Path], min_files: int = MIN_FILES) -> dict:
    """Walk `roots` and tally media per directory. Never opens a file — stat only."""
    per_dir: dict[str, dict] = defaultdict(
        lambda: {"n": 0, "bytes": 0, "cloud": 0, "exts": defaultdict(int)})
    errors = 0

    for root in roots:
        root = Path(root)
        if not root.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None):
            dirnames[:] = [d for d in dirnames
                           if d not in contract.SKIP_DIRS
                           and not d.startswith(".")
                           and d.lower() not in NOISE_DIRS]
            if _is_noise(dirpath):
                continue
            for name in filenames:
                ext = Path(name).suffix.lower()
                if ext not in inventory.MEDIA_EXTS:
                    continue
                rec = per_dir[dirpath]
                try:
                    st = os.stat(os.path.join(dirpath, name))
                except OSError:
                    errors += 1
                    continue
                rec["n"] += 1
                rec["bytes"] += st.st_size
                rec["exts"][ext] += 1
                if contract.is_cloud_placeholder(st):
                    rec["cloud"] += 1

    # Roll every directory's tally up into all of its ancestors, so a library split
    # across year folders shows up as one candidate root rather than fifteen leaves.
    subtree: dict[str, dict] = defaultdict(lambda: {"n": 0, "bytes": 0, "cloud": 0})
    for dirpath, rec in per_dir.items():
        p = Path(dirpath)
        for anc in [p, *p.parents]:
            agg = subtree[str(anc)]
            agg["n"] += rec["n"]
            agg["bytes"] += rec["bytes"]
            agg["cloud"] += rec["cloud"]

    return {"per_dir": {k: v for k, v in per_dir.items() if v["n"] >= min_files},
            "subtree": {k: v for k, v in subtree.items() if v["n"] >= min_files},
            "errors": errors}


def _candidates(subtree: dict[str, dict], limit: int = 12) -> list[tuple[str, dict]]:
    """Pick the highest ancestor that still explains most of its subtree.

    Walking up, every ancestor of a library inherits its full count — so `C:\\` always
    "wins". A node is only interesting if it holds a large share of what its parent holds;
    once that share drops, the parent is just a container and the child is the real root.
    """
    rows = sorted(subtree.items(), key=lambda kv: -kv[1]["n"])
    keep: list[tuple[str, dict]] = []
    for path, rec in rows:
        parent = str(Path(path).parent)
        if parent != path and parent in subtree:
            if rec["n"] >= subtree[parent]["n"] * 0.85:
                continue  # parent already represents this subtree; prefer the parent
        keep.append((path, rec))
        if len(keep) >= limit:
            break
    return keep


def report(result: dict) -> str:
    per_dir, subtree = result["per_dir"], result["subtree"]
    if not per_dir:
        return ("no directory held at least "
                f"{MIN_FILES} media files — try explicit roots: "
                r"run discover --roots D:\ E:\ \\nas\photos")

    lines = ["", "=" * 74, "  LIBRARY DISCOVERY", "=" * 74]

    lines += ["", "  CANDIDATE LIBRARY ROOTS  (point `inventory --root` at one of these)", ""]
    lines.append(f"    {'files':>8}  {'size':>9}  {'cloud':>6}   path")
    for path, rec in _candidates(subtree):
        cloud = 100.0 * rec["cloud"] / max(rec["n"], 1)
        flag = "  <-- mostly cloud stubs" if cloud > 50 else ""
        lines.append(f"    {rec['n']:>8,}  {rec['bytes'] / 1e9:>7.1f} GB  "
                     f"{cloud:>5.0f}%   {path}{flag}")

    lines += ["", "  BIGGEST INDIVIDUAL FOLDERS", ""]
    top = sorted(per_dir.items(), key=lambda kv: -kv[1]["n"])[:10]
    for path, rec in top:
        exts = ", ".join(f"{e}:{n:,}" for e, n in
                         sorted(rec["exts"].items(), key=lambda kv: -kv[1])[:4])
        lines.append(f"    {rec['n']:>8,}  {path}")
        lines.append(f"              {exts}")

    total = sum(r["n"] for r in per_dir.values())
    cloud = sum(r["cloud"] for r in per_dir.values())
    lines += ["", f"  {total:,} media files in {len(per_dir):,} folders; "
                  f"{cloud:,} ({100.0 * cloud / max(total, 1):.0f}%) are cloud placeholders"]
    if result["errors"]:
        lines.append(f"  {result['errors']:,} files could not be stat'd (permissions)")
    lines.append("=" * 74)
    return "\n".join(lines)
