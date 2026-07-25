# The non-destructive contract

**Originals are read-only, forever.** This is the central invariant of the project and the
one thing that must never be weakened without an explicit amendment to this file.

No stage moves, renames, deletes, or rewrites a source file. Every artifact the pipeline
produces lands under a single `derived/` root outside the library tree. Delete that one
directory and you are back to the exact pre-run state, having lost nothing but compute.

This matters more than it sounds. The pipeline's job is to make judgments — "this photo is
blurry", "this is the same child" — and it will get some of them wrong. A wrong judgment
that only writes a score to a sidecar database costs nothing. A wrong judgment that deletes
a photo of your son costs something you cannot buy back.

## Three independent layers

Modelled on the three-layer keyless-source contract in the research repo this grew out of.
Each layer catches something the others miss.

**Layer 1 — Construction.** `contract.open_source()` is the only sanctioned way to touch an
original and hard-codes mode `'rb'`. Nothing in `src/` calls a mutator on a library path.

**Layer 2 — Containment.** `contract.guard_output()` refuses any write path that resolves
inside the library root. Derived state has exactly one home.

Layer 2 is reached through **exactly one door**, because a guard you have to remember to call
is a guard you will eventually forget:

- `db.open_index(derived, library_root)` — the only function that can *create* the index.
  `library_root` is mandatory and positional, so forgetting to guard is a `TypeError` at the
  call site rather than a silent hole in the contract.
- `db.open_existing(derived)` — for stages that only read. It opens with SQLite URI mode
  `rw`, which errors on a missing file instead of creating one. A stage that *cannot* create
  a database cannot create one inside your library; that is strictly stronger than checking
  first, and it is why `census` and `dupes` need no `--root`.
- `contract.self_audit()` forbids `import sqlite3` anywhere but `db.py`, so a future stage
  cannot walk around either door.

**Layer 3 — Verification.** `contract.snapshot()` records `(relpath, size, mtime_ns)` for the
whole tree before a run; `contract.verify()` diffs it afterward and reports drift as ADDED /
CHANGED / MISSING. This is the layer that makes an unattended overnight job over 100,000
files trustworthy, because it is the only one that catches a *third-party library* writing
behind our back — and stages 2+ will pull in third-party ML code.

**Layer 3's known blind spot, stated plainly.** `contract.SKIP_DIRS` prunes `derived/`, so a
database created at `<library>/derived/index.db` produces **zero drift** from `verify`. The
pruning is deliberate and stays — you do not want to snapshot a 12 GB decode cache — but it
means layer 3 cannot be the backstop for this class of mistake. It is not hypothetical:
`census()` and `resolve_duplicates()` opened the index with no guard at all, and every layer
missed it while a 16-test suite stayed green. That is precisely why layer 2 became structural
rather than one more check, and why `tests/test_contract_paths.py` asserts the *call path* is
guarded rather than that the guard function works in isolation.

`contract.self_audit()` mechanizes layers 1 and 2 by grepping this package for the forbidden
mutators and for database opens outside `db.py`, so the discipline cannot rot silently as the
code grows. There is no allowlist: the pipeline genuinely never needs `os.remove`,
`shutil.move`, or a rename. If a future stage truly does, that is a conversation and an
amendment here — not a quiet exception.

```
run snapshot --root <library>    # before
run <whatever>
run verify   --root <library>    # after; exit 2 means something wrote to your library
run audit                        # layer 1, mechanized
```

## What "output" means

The pipeline never edits your photos. It emits, all under `derived/`:

- `index.db` — SQLite, content-keyed. Scores, faces, clusters, tags.
- `manifest.jsonl` — the layer-3 tree snapshot.
- XMP **sidecar** files (later stages) — `IMG_1234.jpg.xmp` alongside the original, which is
  the industry-standard non-destructive tagging mechanism. Even these are opt-in and land in
  `derived/sidecars/` by default rather than next to the originals.
- Album manifests — lists of paths, not copies. Materialize them as symlinks or hardlinks if
  you want browsable folders; both leave the originals in place and cost no disk.

## Consequence for the Immich decision

Immich's own documentation of iOS ingestion is lossy — Apple Photos edit history, portrait
and cinematic depth data, and `.AAE` sidecars are dropped, keeping only the final rendered
image. Letting Immich *own* the library therefore violates this contract on day one, before
any of our code runs.

So Immich is mounted as an **external library** pointed at the existing tree in read-only
mode. It gets to be the viewer, the face detector, and the search UI. It does not get to be
the system of record. The originals stay exactly where they are.
