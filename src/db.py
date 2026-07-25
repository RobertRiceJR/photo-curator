"""SQLite index — the derived system of record, and the ONE door to it.

Everything the pipeline learns lives here, keyed by CONTENT, never by path. That one
choice buys most of the properties this project needs:

  * Idempotency  - re-running any stage over the same bytes is a no-op.
  * Resumability - a 100k-file job that dies at 60% resumes at 60%, which is not
    optional when face embedding takes hours.
  * Move-tolerance - reorganise your folders and the index still knows every asset;
    only the `paths` rows change.
  * Free exact-dedup - one asset, many paths, falls out of the schema for nothing.

WHY THIS MODULE OWNS `sqlite3` OUTRIGHT (contract layer 2)
`sqlite3.connect` is a WRITE: it creates the file, its `-wal`/`-shm` sidecars, and — with
`executescript` — a schema. "Just opening the index" is therefore enough to put files inside
the user's photo library, and for a while it did: `census()` and `resolve_duplicates()` each
opened the index with no guard, and a fully green test suite never noticed.

The fix is not another check. It is two doors and no other way in:

  * `open_index(derived, library_root)` — the only function that can CREATE the index. The
    root is a mandatory positional argument, so forgetting to guard is a `TypeError` at the
    call site rather than a silent hole in the contract.
  * `open_existing(derived)` — for stages that only read. It asks SQLite for URI mode `rw`,
    which errors on a missing file instead of creating one. A stage that *cannot* create a
    database cannot create one in your library; that is strictly stronger than remembering
    to check first, and it is why `census` needs no `--root` at all.

`contract.self_audit()` enforces that `sqlite3` is not imported anywhere else in `src/`, so
a future stage cannot walk around either door. See CONTRACT.md.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

# Imported as a MODULE, and guard_output is always called as `contract.guard_output(...)`.
# A `from contract import guard_output` would bind the function at import time and make
# `mock.patch.object(contract, "guard_output")` invisible — silently disabling the test
# that proves every index open is vetted (tests/test_contract_paths.py, T4).
import contract

INDEX_NAME = "index.db"

#: Stages annotate a handle without importing sqlite3 (which self_audit forbids).
Index = sqlite3.Connection


class IndexMissing(RuntimeError):
    """No index at this derived root, and a read-only stage will not create one."""


SCHEMA = """
CREATE TABLE IF NOT EXISTS assets (
    qhash          TEXT PRIMARY KEY,   -- fast content fingerprint (see inventory.fingerprint)
    sha256         TEXT,               -- full digest, only computed for dup candidates
    size           INTEGER NOT NULL,
    ext            TEXT NOT NULL,
    mime           TEXT,
    width          INTEGER,
    height         INTEGER,
    captured_at    TEXT,               -- ISO 8601
    capture_source TEXT,               -- exif | filename | mtime
    make           TEXT,
    model          TEXT,
    orientation    INTEGER,
    first_seen     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paths (
    path      TEXT PRIMARY KEY,        -- absolute, forward-slashed
    qhash     TEXT NOT NULL REFERENCES assets(qhash),
    size      INTEGER NOT NULL,
    mtime_ns  INTEGER NOT NULL,
    last_seen TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    stage         TEXT NOT NULL,
    root          TEXT NOT NULL,       -- load-bearing: how later stages recover the library root
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    n_seen        INTEGER DEFAULT 0,
    n_new         INTEGER DEFAULT 0,
    n_skipped     INTEGER DEFAULT 0,
    n_placeholder INTEGER DEFAULT 0,   -- cloud stubs skipped, not on this disk
    n_failed      INTEGER DEFAULT 0,
    notes         TEXT
);

CREATE INDEX IF NOT EXISTS idx_paths_qhash    ON paths(qhash);
CREATE INDEX IF NOT EXISTS idx_assets_capture ON assets(captured_at);
CREATE INDEX IF NOT EXISTS idx_assets_ext     ON assets(ext);
"""


# --------------------------------------------------------------------------
# The two doors
# --------------------------------------------------------------------------
def index_path(derived: Path) -> Path:
    """Where the index lives. A pure join — creates nothing, so it is safe to call
    before anything has been vetted (both `guard_output` and file: URIs need it)."""
    return Path(derived).resolve() / INDEX_NAME


def _file_uri(db_path: Path, query: str) -> str | None:
    """`file:` URI for SQLite, or None when the path cannot be expressed as one.

    `Path.as_uri()` percent-encodes spaces and `#`/`?` correctly, but on a UNC path it
    emits `file://server/share/x.db` — a non-empty authority that SQLite rejects. Callers
    fall back to a plain path in that case.
    """
    if db_path.drive.startswith("\\\\"):
        return None
    return f"{db_path.as_uri()}?{query}"


def _open(db_path: Path, *, create: bool) -> Index:
    """The only `sqlite3.connect` in this package — see `contract._FUNNELLED`.

    `create=False` asks for URI mode `rw`, which raises on a missing file rather than
    making one. That is deliberately a removed *capability* rather than one more check:
    the reason a census could create a database inside the photo library was not a missing
    if-statement, it was that it could. Checks get forgotten at the next call site;
    incapacity does not.

    Only `create=True` touches the schema. A report has no business running DDL — an index
    predating a future column is upgraded by the next `inventory`, not by a read.
    """
    if create:
        con = sqlite3.connect(db_path, timeout=60)
    else:
        uri = _file_uri(db_path, "mode=rw")
        con = (sqlite3.connect(uri, uri=True, timeout=60) if uri
               # UNC fallback: existence was already asserted by open_existing, so a
               # plain connect cannot create anything that wasn't there.
               else sqlite3.connect(db_path, timeout=60))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")      # survive a mid-run kill
    con.execute("PRAGMA synchronous=NORMAL")    # WAL makes this safe enough and much faster
    if create:
        con.executescript(SCHEMA)
    return con


def open_index(derived: Path, library_root: Path) -> Index:
    """Create-or-open the index for a stage that WRITES rows. The guarded door.

    `library_root` is mandatory and positional on purpose: layer 2 is only as strong as its
    weakest call site, and an optional argument is a forgettable one. `guard_output` is also
    the only thing in this package that creates the derived directory — one owner.
    """
    db_path = contract.guard_output(index_path(derived), library_root)   # 🔒 layer 2
    return _open(db_path, create=True)


def open_existing(derived: Path) -> Index:
    """Open an EXISTING index, for a stage that only reads. Cannot create, needs no root.

    `census` and `dupes` report on state that `inventory` already built — they run after it,
    never before, so the root is recoverable from `runs.root` rather than re-supplied. It is
    still re-checked when available: defence in depth for an index that got inside the
    library before this door existed.
    """
    db_path = index_path(derived)
    if not db_path.is_file():
        raise IndexMissing(
            f"no index at {db_path}\n"
            f"  this stage reports on state that `inventory` builds, and will not create a\n"
            f"  database — a stage that cannot create one cannot create one in your library")
    root = recorded_root(derived)
    if root is not None:
        contract.guard_output(db_path, root)     # 🔒 advisory: nothing to contain, but it speaks up
    return _open(db_path, create=False)


def recorded_root(derived: Path) -> Path | None:
    """The library root this derived state was built against, read WITHOUT any write.

    `immutable=1` promises SQLite the file will not change, so it opens with no journal and
    no `-wal`/`-shm` sidecar — not one byte touched. That property is load-bearing: to prove
    an index open is legal you must know the root *first*, and if learning it required a
    normal open you would already have done the thing you were checking.

    Nothing to migrate — `start_run` has written `runs.root` since the first commit. A marker
    file would have needed a write, a backfill, and a second source of truth free to disagree
    with this one. Returns None when there is no index or no run yet, in which case the
    caller has nothing to check against and must be told the root explicitly.
    """
    db_path = index_path(derived)
    if not db_path.is_file():
        return None
    uri = _file_uri(db_path, "immutable=1")
    if uri is None:
        return None
    try:
        con = sqlite3.connect(uri, uri=True)
        try:
            row = con.execute(
                "SELECT root FROM runs WHERE stage='inventory' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None      # corrupt, or predates the schema — unknown, not fatal
    return Path(row[0]) if row and row[0] else None


# --------------------------------------------------------------------------
# Rows
# --------------------------------------------------------------------------
def start_run(con: Index, stage: str, root: Path) -> int:
    """Open a run record. `root` is not telemetry — `recorded_root` reads it back, and it
    is how every later stage recovers the library root. Dropping it breaks layer 2."""
    cur = con.execute(
        "INSERT INTO runs (stage, root, started_at) VALUES (?, ?, datetime('now'))",
        (stage, str(root)),
    )
    con.commit()
    return int(cur.lastrowid)


def finish_run(con: Index, run_id: int, seen: int, new: int, skipped: int,
               placeholder: int = 0, failed: int = 0, notes: str = "") -> None:
    con.execute(
        "UPDATE runs SET finished_at=datetime('now'), n_seen=?, n_new=?, n_skipped=?,"
        " n_placeholder=?, n_failed=?, notes=? WHERE id=?",
        (seen, new, skipped, placeholder, failed, notes, run_id),
    )
    con.commit()


def last_run(con: Index, stage: str = "inventory") -> sqlite3.Row | None:
    return con.execute(
        "SELECT * FROM runs WHERE stage=? ORDER BY id DESC LIMIT 1", (stage,)
    ).fetchone()


def known_paths(con: Index) -> dict[str, tuple[int, int]]:
    """{path: (size, mtime_ns)} for the resume check.

    Loaded once up front: 100k rows is a few MB in memory and turns the per-file resume
    test into a dict lookup instead of a query.
    """
    return {r["path"]: (r["size"], r["mtime_ns"])
            for r in con.execute("SELECT path, size, mtime_ns FROM paths")}


def upsert_asset(con: Index, rec: dict) -> bool:
    """Insert an asset if new. Returns True when it was genuinely new content."""
    cur = con.execute(
        "INSERT INTO assets (qhash, size, ext, mime, width, height, captured_at,"
        " capture_source, make, model, orientation, first_seen)"
        " VALUES (:qhash, :size, :ext, :mime, :width, :height, :captured_at,"
        " :capture_source, :make, :model, :orientation, datetime('now'))"
        " ON CONFLICT(qhash) DO NOTHING",
        rec,
    )
    return cur.rowcount > 0


def upsert_path(con: Index, path: str, qhash: str, size: int, mtime_ns: int) -> None:
    con.execute(
        "INSERT INTO paths (path, qhash, size, mtime_ns, last_seen)"
        " VALUES (?, ?, ?, ?, datetime('now'))"
        " ON CONFLICT(path) DO UPDATE SET qhash=excluded.qhash, size=excluded.size,"
        " mtime_ns=excluded.mtime_ns, last_seen=excluded.last_seen",
        (path, qhash, size, mtime_ns),
    )
