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
- XMP **sidecar** files (later stages) — `IMG_1234.heic.xmp`, written *beside the media file in
  the working copy*, which is the industry-standard non-destructive tagging mechanism. This is
  not a preference: Immich resolves a sidecar only as `<filename>.<ext>.xmp` adjacent to its
  media file, so a sidecar anywhere else cannot reach the consumer it exists for. The working
  copy is not an original — the originals are the iCloud library and the phone — so writing
  next to it breaks nothing here. Writing beside the *source* tree stays forbidden. See
  "Why the pipeline runs against a working copy" below.
- `derived/sidecars/` — retained only as an **export format** for tools that accept a sidecar
  by path. It is not the default and it can never feed Immich.
- Album manifests — lists of paths, not copies. Materialize them as symlinks or hardlinks if
  you want browsable folders; both leave the originals in place and cost no disk.

## Consequence for the Immich decision

Immich's own documentation of iOS ingestion is lossy — Apple Photos edit history, portrait
and cinematic depth data, and `.AAE` sidecars are dropped, keeping only the final rendered
image. Letting Immich *own* the library therefore violates this contract on day one, before
any of our code runs.

So Immich is mounted as an **external library** pointed at the working tree in read-only
mode. It gets to be the viewer and the search UI. It does not get to be the system of record.
The originals stay exactly where they are.

**It does not get to be the face detector — corrected 2026-07-25.** An earlier revision of
this file said it did. Two independent reasons it cannot:

- **Face detection is reported not to run over external libraries at all.** Three separate
  reports: a 90k-photo external library where the job finished in one minute with no People
  section and no tag-people control ([issue 23879](https://github.com/immich-app/immich/issues/23879)),
  the same on v2.1.0 after running the job in `missing` mode
  ([issue 23131](https://github.com/immich-app/immich/issues/23131)), and a third reporter's
  SQL showing `facesRecognizedAt` NULL for every external asset while uploaded assets carried
  timestamps ([discussion 23880](https://github.com/immich-app/immich/discussions/23880),
  never resolved). Two of the three were closed as Done; none was verified against v3.0.1.
- **Even if it ran, it would not help.** Stage 4 needs raw per-face embeddings to chain
  identity across years. Immich exposes people and faces, never the underlying vectors. Its
  own clustering is a DBSCAN-style density grouping over a single embedding space — precisely
  the mechanism childhood morphology defeats.

So detection and embedding stay in-house at stage 3, and Immich's role narrows to viewing,
star-rating and tag search, and CLIP semantic search.

**The mount mode is now a contract concern, not a deployment detail.** Immich's own
documentation warns that a volume not ending in `:ro` means "Immich will be able to delete the
files in this folder" ([libraries docs](https://docs.immich.app/features/libraries/)). That
makes it a third-party writer with delete rights pointed straight at the tree — exactly the
class of actor layer 3 exists to catch. Any Immich mount used here ends in `:ro`, and the
library it points at must be one nothing else writes to (see below).

## Why the pipeline runs against a working copy, not the sync root

Measured 2026-07-25: `verify --root D:\iCloudPhotos\Photos` returned **`CONTRACT VIOLATED —
500 file(s) drifted`** in roughly twenty minutes, every one of them `ADDED`. Nothing of ours
wrote a byte; iCloud's sync engine did. The library grew roughly 28% over that single working
session and has kept growing since — figures and the growth curve live in `CALL_TREE.md` §4.0,
which is the only place that carries absolute counts for this library.

Layer 3 cannot function against a root another process actively manages. This is the same
lesson the OneDrive incident taught — a verifier that cries wolf stops being read — but
stronger, because iCloud is not an occasional writer like a game overlay, it is a permanent
one whose whole job is to change that directory.

So the pipeline runs against a **hydrated working copy on a drive nothing else writes to**.
That is not a performance choice or a fidelity choice; it is what buys layer 3 back. The copy
is made by `robocopy` with no deleting or moving flag, so it is read-only with respect to the
source by construction, and no code in this package participates in it.

### The copy needs a second step, and skipping it silently empties the index

Measured on a 20-file probe, 2026-07-25. `robocopy` **does** hydrate iCloud placeholders — the
destination files carry real bytes (`....ftypheic` magic, `exiftool` reads a 4032x3024 iPhone
15 Pro HEIC with a true `DateTimeOriginal`). Backup mode would have copied the stub instead,
which is why `/B` and `/ZB` are banned below.

But **robocopy propagates `FILE_ATTRIBUTE_OFFLINE` to the destination**, where it is stale and
meaningless — `F:` is not a cloud-synced volume. The reparse-point, sparse and recall bits are
correctly dropped; only `OFFLINE` survives, leaving `0x1020` instead of `0x20`.

`is_cloud_placeholder()` tests `OFFLINE | RECALL_ON_OPEN | RECALL_ON_DATA_ACCESS`, so it reads
that bit and calls the file a placeholder. Observed directly:

```
inventory --root F:\photo-work\_probe     BEFORE strip
  0 newly indexed · 20 cloud placeholders skipped     <- fully readable files, all skipped
inventory --root F:\photo-work\_probe     AFTER strip
  20 newly indexed · 0 cloud placeholders skipped · 100% exif date provenance
```

Over a 200 GB copy this fails **silently and completely**: a successful robocopy, a clean
`inventory` run, and an empty index — the same shape as the "test whose input cannot exercise
the claim" pattern §11 already names as this project's characteristic defect.

So the copy is two steps, not one. `robocopy` has no flag for this — `/A-:O` is not valid, `O`
is absent from the `RASHCNET` attribute set:

```powershell
robocopy "D:\iCloudPhotos\Photos" "F:\photo-work\library" /E /XJ /R:2 /W:5 /NP /TEE /LOG:F:\photo-work\copy.log
Get-ChildItem 'F:\photo-work\library' -File -Recurse | ForEach-Object {
  if ([int]$_.Attributes -band 0x1000) { $_.Attributes = $_.Attributes -band -bnot [IO.FileAttributes]::Offline }
}
```

Clearing an attribute on our own copy touches no original, so this stays outside the contract's
concern. Doing it to the *source* would not.

**Flag discipline on that first line is load-bearing.** No `/MIR`, `/MOV`, `/MOVE` or `/PURGE`
— all of them delete. No `/B` or `/ZB`: backup mode is precisely what copies the reparse stub
instead of hydrating it. `/XJ` excludes junctions, so the `C:` → `D:` link can never cause a
loop.

One consequence worth stating, because it reads as a contradiction otherwise: **sidecars may
be written beside the working copy.** Immich can only read a sidecar named
`<filename>.jpg.xmp` adjacent to its media file, so `derived/sidecars/` can never feed it. The
working copy is not an original — the originals are the iCloud library and the phone — so
writing next to it breaks nothing. Writing beside the *source* tree remains forbidden.
