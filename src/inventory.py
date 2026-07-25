"""Stage 0 — inventory and census. Read-only, resumable, dependency-free.

Before committing to any ML stack you need to know what the library actually *is*:
how many files, which formats, what date range, how much of it has real capture
metadata, and how much is already duplicated. That is this stage's entire job, and
it answers questions that change downstream decisions — e.g. the share of HEIC
directly determines whether Immich's current HEIC thumbnail regression matters to you.

WHY A TWO-PHASE HASH. Fully hashing 100k photos means reading every byte: at ~4 MB
average that is ~400 GB of I/O and hours of wall clock. So identity uses a
`fingerprint` — size plus the head and tail 256 KB — which is ~100x cheaper and
collides only for files that share a size AND both ends. Full sha256 is then computed
ONLY for the handful of fingerprint collisions, in `resolve_duplicates()`, where it
actually decides something. Cheap by default, exact where it counts.

CLOUD PLACEHOLDERS ARE THE DEFAULT, NOT THE EXCEPTION. The first real library this ran
against was 100% dehydrated iCloud stubs: they stat with the true size but reading one
raises OSError(EINVAL), or silently pulls gigabytes down. So they are detected and skipped
by default, and counted separately — a census that quietly reported 0 assets for a 33,000
file library would be worse than one that refused.
"""
from __future__ import annotations

import hashlib
import mimetypes
import time
from pathlib import Path

import contract
import db as index_db
import meta as meta_reader

# What counts as a library asset. Deliberately broad — a family archive is not just JPEG.
IMAGE_EXTS = {".jpg", ".jpeg", ".jpe", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff",
              ".heic", ".heif", ".avif", ".dng", ".nef", ".arw", ".cr2", ".cr3", ".orf",
              ".rw2", ".raf", ".srw"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".3gp", ".mts", ".wmv"}
MEDIA_EXTS = IMAGE_EXTS | VIDEO_EXTS

_CHUNK = 256 * 1024


def fingerprint(path: Path, size: int) -> str:
    """Cheap content identity: sha256 over size + first 256KB + last 256KB."""
    h = hashlib.sha256()
    h.update(str(size).encode())
    with contract.open_source(path) as fh:
        h.update(fh.read(_CHUNK))
        if size > _CHUNK * 2:
            fh.seek(-_CHUNK, 2)
            h.update(fh.read(_CHUNK))
    return h.hexdigest()


def full_sha256(path: Path) -> str:
    """Exact digest. Only called on fingerprint collisions — never in the hot loop."""
    h = hashlib.sha256()
    with contract.open_source(path) as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def run(root: Path, derived: Path, rescan: bool = False, hydrate: bool = False) -> dict:
    """Walk the library and index it. Safe to interrupt and re-run at any time."""
    root = Path(root).resolve()
    if not root.is_dir():
        raise SystemExit(f"library root not found: {root}")

    # Recovered before the index is opened, so a differing root can be reported. Costs
    # nothing: recorded_root reads with immutable=1 and writes no journal.
    prior = index_db.recorded_root(derived)
    con = index_db.open_index(derived, root)          # 🔒 layer 2 lives inside this call
    if prior is not None and prior != root:
        print(f"  note: this index was built against {prior}\n"
              f"        now indexing {root} — safe, the index is content-keyed, but old\n"
              f"        paths persist until their directories are re-walked")
    run_id = index_db.start_run(con, "inventory", root)

    seen = new = skipped = failed = placeholder = 0
    known = {} if rescan else index_db.known_paths(con)
    t0 = time.time()
    _completed = False

    try:
        for path in contract._walk(root, MEDIA_EXTS):
            seen += 1
            key = str(path).replace("\\", "/")
            try:
                st = path.stat()
            except OSError:
                failed += 1
                continue

            # Resume check: unchanged size+mtime means we already have these bytes.
            if known.get(key) == (st.st_size, st.st_mtime_ns):
                skipped += 1
                continue

            # Cloud stub: reading it would fail or force a download. Skip by default.
            if contract.is_cloud_placeholder(st) and not hydrate:
                placeholder += 1
                continue

            ext = path.suffix.lower()
            try:
                qhash = fingerprint(path, st.st_size)
                md = dict(meta_reader.EMPTY)
                if ext in IMAGE_EXTS:
                    with contract.open_source(path) as fh:
                        md = meta_reader.read_meta(fh, ext)
            except OSError:
                failed += 1
                continue

            captured, source = meta_reader.best_date(md, path, st.st_mtime)
            is_new = index_db.upsert_asset(con, {
                "qhash": qhash, "size": st.st_size, "ext": ext,
                "mime": mimetypes.guess_type(path.name)[0],
                "width": md.get("width"), "height": md.get("height"),
                "captured_at": captured, "capture_source": source,
                "make": md.get("make"), "model": md.get("model"),
                "orientation": md.get("orientation"),
            })
            index_db.upsert_path(con, key, qhash, st.st_size, st.st_mtime_ns)
            new += int(is_new)

            if seen % 500 == 0:
                con.commit()
                rate = seen / max(time.time() - t0, 0.001)
                print(f"  {seen:>7,} seen · {new:>7,} new · {skipped:>7,} cached "
                      f"· {rate:,.0f}/s", flush=True)
        _completed = True
    finally:
        # Closing out the ledger belongs in the `finally`, not after it. This stage's whole
        # pitch is that killing it costs at most one batch of work — but with finish_run
        # outside, a Ctrl-C left `runs.finished_at` NULL forever and leaked the connection,
        # so the resumable design had an unresumable audit trail. `notes` records how it
        # ended, because a run that was interrupted at 60% is not a run that found 60%.
        con.commit()
        index_db.finish_run(con, run_id, seen, new, skipped, placeholder, failed,
                            notes=f"elapsed={time.time() - t0:.1f}s hydrate={hydrate} "
                                  f"exit={'ok' if _completed else 'interrupted'}")
        con.close()

    return {"seen": seen, "new": new, "skipped": skipped, "failed": failed,
            "placeholder": placeholder, "elapsed": time.time() - t0,
            "db": str(index_db.index_path(derived))}


def resolve_duplicates(derived: Path) -> int:
    """Phase two: record a full sha256 for every asset that has more than one path.

    Only touches assets that share a fingerprint with another asset — in practice a tiny
    slice of the library, which is the whole point of the two-phase design.

    WHAT THIS DOES NOT DO, STATED PLAINLY. It does not *confirm* that the paths in a group
    are byte-identical, and the name/docstring used to claim it did. It hashes ONE member
    (`LIMIT 1` below) and stores that digest on the asset. A false fingerprint collision —
    two different files with the same size, same head 256 KB and same tail 256 KB — is
    therefore invisible here, and worse, it was already lost upstream: `upsert_asset` uses
    ON CONFLICT DO NOTHING, so the second file's dimensions, capture date and camera were
    discarded at insert time and its path row now points at the first file's metadata.
    `tests/test_core.TestFingerprintCollision` builds exactly that case and pins the
    behaviour, so it is documented rather than rediscovered.

    Closing it properly means comparing every member and splitting divergent files into
    separate assets — an asset-splitting path that does not exist yet. Until then this
    function reports what it actually knows: how many multi-path groups now carry a digest.

    Takes no library root: it runs after `inventory`, so `open_existing` recovers the root
    from the index and cannot create one anywhere. It previously accepted a `root` it never
    used, whose caller defaulted it to the CWD — a guard argument that guarded nothing.
    """
    con = index_db.open_existing(derived)
    try:
        dupe_hashes = [r["qhash"] for r in con.execute(
            "SELECT qhash FROM paths GROUP BY qhash HAVING COUNT(*) > 1")]
        digested = 0
        for qh in dupe_hashes:
            row = con.execute("SELECT sha256 FROM assets WHERE qhash=?", (qh,)).fetchone()
            if row and row["sha256"]:
                digested += 1
                continue
            p = con.execute("SELECT path FROM paths WHERE qhash=? LIMIT 1", (qh,)).fetchone()
            if not p or not Path(p["path"]).exists():
                continue
            con.execute("UPDATE assets SET sha256=? WHERE qhash=?",
                        (full_sha256(Path(p["path"])), qh))
            digested += 1
        con.commit()
    finally:
        con.close()
    return digested


def census(derived: Path) -> str:
    """Human-readable report — the actual deliverable of stage 0.

    Opens through `open_existing`, which cannot create a database. It used to call
    `db.connect()`, so a *report* brought an index into being — and with no library root in
    its signature it had no way to prove that index was outside the library. The capability
    is removed rather than checked; that is why this still needs no root.
    """
    con = index_db.open_existing(derived)
    q = lambda sql, *a: con.execute(sql, a).fetchall()  # noqa: E731

    total, total_bytes = q("SELECT COUNT(*), COALESCE(SUM(size),0) FROM assets")[0]
    n_paths = q("SELECT COUNT(*) FROM paths")[0][0]
    if not total:
        # An empty index has two very different causes, and conflating them is dangerous.
        # A 100%-cloud library (the real iCloudPhotos case: 33,095 files, every one a stub)
        # walks fine and indexes nothing — reporting "run inventory first" there would be
        # flatly false and would send you round the same loop again.
        last = index_db.last_run(con)
        con.close()
        if last and last["n_placeholder"]:
            return (f"\n  index is empty, but `inventory` DID run.\n\n"
                    f"  All {last['n_placeholder']:,} of the {last['n_seen']:,} media files under\n"
                    f"    {last['root']}\n"
                    f"  are CLOUD PLACEHOLDERS — dehydrated stubs whose bytes are not on this\n"
                    f"  disk. Nothing could be read, so nothing could be indexed.\n\n"
                    f"  Make the files local (or move them to a drive with room), then re-run.\n"
                    f"  `--hydrate` will read them on demand, but that downloads real data.")
        if last:
            return (f"index is empty — `inventory` walked {last['n_seen']:,} files under "
                    f"{last['root']} and indexed none ({last['n_failed']:,} unreadable)")
        return "index is empty — run `inventory` first"

    lines = ["", "=" * 62, "  LIBRARY CENSUS", "=" * 62,
             f"  unique assets : {total:,}",
             f"  files on disk : {n_paths:,}   ({n_paths - total:,} exact duplicates)",
             f"  total bytes   : {total_bytes / 1e9:,.1f} GB", ""]

    lines.append("  BY FORMAT")
    for r in q("SELECT ext, COUNT(*) n, SUM(size) b FROM assets GROUP BY ext ORDER BY n DESC"):
        share = 100.0 * r["n"] / total
        lines.append(f"    {r['ext']:<7} {r['n']:>7,}  {share:>5.1f}%  {r['b'] / 1e9:>7.1f} GB")

    lines += ["", "  BY YEAR"]
    for r in q("SELECT substr(captured_at,1,4) y, COUNT(*) n FROM assets "
               "GROUP BY y ORDER BY y"):
        bar = "#" * max(1, round(40 * r["n"] / total))
        lines.append(f"    {r['y']}  {r['n']:>7,}  {bar}")

    lines += ["", "  DATE PROVENANCE  (mtime = untrustworthy, see meta.best_date)"]
    for r in q("SELECT capture_source s, COUNT(*) n FROM assets GROUP BY s ORDER BY n DESC"):
        lines.append(f"    {r['s']:<9} {r['n']:>7,}  {100.0 * r['n'] / total:>5.1f}%")

    heic = q("SELECT COUNT(*) FROM assets WHERE ext IN ('.heic','.heif')")[0][0]
    cams = q("SELECT make, model, COUNT(*) n FROM assets WHERE model IS NOT NULL "
             "GROUP BY make, model ORDER BY n DESC LIMIT 8")
    if cams:
        lines += ["", "  TOP CAMERAS"]
        for r in cams:
            lines.append(f"    {(r['make'] or '?'):<12} {(r['model'] or '?'):<22} {r['n']:>7,}")

    lines += ["", "  DECISIONS THIS CENSUS INFORMS",
              f"    HEIC/HEIF share ....... {100.0 * heic / total:.1f}%  "
              f"({'blocked by Immich v3.0.1 HEIC bug' if heic else 'HEIC bug is moot'})"]
    unknown = q("SELECT COUNT(*) FROM assets WHERE capture_source='mtime'")[0][0]
    lines.append(f"    dates from mtime only . {100.0 * unknown / total:.1f}%  "
                 f"({'needs a date-repair pass' if unknown > total * 0.1 else 'ok'})")

    last = index_db.last_run(con)
    if last and last["n_placeholder"]:
        ph, walked = last["n_placeholder"], last["n_seen"]
        lines += ["",
                  f"  !! {ph:,} of {walked:,} files ({100.0 * ph / max(walked, 1):.0f}%) are CLOUD",
                  "     PLACEHOLDERS — dehydrated stubs, bytes not on this disk. They are",
                  "     absent from every number above, so this census describes only the",
                  "     local subset. Make the files local, or re-run with --hydrate to pull",
                  "     them on demand (that downloads real data — check your disk first)."]
    lines.append("=" * 62)
    con.close()
    return "\n".join(lines)
