# photo-curator

Automated, **non-destructive** curation for a large personal photo library: find and rank the
good photos of one person across many years, tag them, and organize them — without ever
moving, renaming, or deleting an original.

Read [CONTRACT.md](CONTRACT.md) first. It is the invariant everything else is built around.

## Status

**Stage 0 (discover + inventory + census) is built, tested, and has run against the real
library.** Stages 1–9 are designed in [CALL_TREE.md](CALL_TREE.md) but not implemented.
Stage 0's job is to answer "what is this library, actually?" before any architecture is
committed to — and on the first real run it did exactly that, with an answer that changed
the plan (see below).

```powershell
pip install -r requirements.txt         # pillow, pillow-heif

.\run test                              # 40 tests, stdlib unittest
.\run audit                             # contract layers 1+2, mechanized

.\run discover                          # find the photo library on this machine
.\run snapshot  --root <library>        # record tree state
.\run inventory --root <library>        # walk, fingerprint, index (READ-ONLY)
.\run census                            # what your library actually is
.\run verify    --root <library>        # prove nothing moved
.\run dupes                             # full sha256 for assets reached by several paths
```

`--derived <dir>` is a **global** flag (before the subcommand). It moves the state directory,
defaults to `./derived`, and may never resolve inside the library — the contract refuses.

## What the first real run found

The library is at `D:\iCloudPhotos\Photos` — **tens of thousands of media files, every one of
them a dehydrated cloud placeholder**, and still growing fast as iCloud syncs.
`C:\Users\terri\iCloudPhotos` is a *junction* to it, not a second library. Absolute counts and
the growth curve live in [CALL_TREE.md](CALL_TREE.md) §4.0 and nowhere else — this library
gains thousands of files an hour, so a number written down here would be wrong by the time you
read it.

Four consequences, all of which outrank any code in this repo:

- **No stage past 0 can read a byte** until the files are local. Zero assets are indexed.
- **Hydrating in place is now possible** — the library fits several times over in the 930 GB
  free on `D:`. An earlier revision of this file said it was not, because it believed the
  library was a 475 GB tree on `C:` with 64 GB free. That was two paths to one library counted
  twice.
- **HEIC is 71% of the files**, not absent. An earlier revision said there was none and
  concluded the library was a lossy JPEG derivative of originals still on the phone. It is
  not, and that removes the main argument for a USB pull.
- **The pipeline cannot run against this root.** `verify` returned 500 drifted files in
  twenty minutes — iCloud writing, not us. Work happens on an isolated copy; see
  [CONTRACT.md](CONTRACT.md).

The `(n)` suffix carried by roughly a quarter of the files is *not* duplication: when tested,
4,299 of 4,330 variants differed in size from their base — only 21 matched. They are distinct
photos that collided on filename because iPhone recycles `IMG_nnnn`. A name-based dedup here
would delete thousands of real photographs.

## Why the pipeline is staged

Each stage is idempotent and keyed by content fingerprint, so any of them can be interrupted
and resumed. This is not fastidiousness — face embedding a library this size runs for hours,
and a job you cannot resume is a job you will never finish.

Numbering is [CALL_TREE.md](CALL_TREE.md)'s 0–9 — the two files used to disagree, and this
table was the one that gave.

| Stage | Does | Status |
|---|---|---|
| 0 · inventory | walk, fingerprint, EXIF, census | **built** |
| 1 · derive | decode once, normalize, cache | designed |
| 2 · similar | perceptual hash, burst grouping | designed |
| 3 · faces | detect, embed, crop sharpness | designed |
| 4 · identity | chain clusters across years, label once | designed |
| 5 · quality | blur/exposure/eyes-open, then aesthetic score | designed |
| 6 · events | segment by time and location gap | designed |
| 7 · select | pick best-of-per-event | designed |
| 8 · export | XMP sidecars, album manifests, Immich person push | designed |
| 9 · evaluate | score identity and selection against a holdout | designed |

## The two design decisions that matter

**Cross-age identity (stage 4) is the hard problem, and no off-the-shelf tool solves it.**
Face embeddings are trained for adult identity invariance; a child from age 2 to age 15 gets
split into several separate "people" by every library that clusters globally — Immich
included. The approach here is to cluster within ~6-month windows, then chain adjacent
windows by centroid distance, co-occurrence (who else is in the frame), and capture
continuity. You confirm one identity per window and the chain propagates. This is the piece
that has to be built rather than bought.

**Quality scoring leads with objective metrics, not the aesthetic model.** Off-the-shelf
aesthetic scorers (CLIP+MLP, NIMA) are trained on crowd taste over generative imagery. A
family archive is a different distribution: a slightly soft, badly lit photo of your kid
laughing is a keeper that a LAION-trained head scores near the floor. So stage 5 leads with
distribution-independent measurements — sharpness *on the face crop*, exposure clipping,
eyes-open — and uses the aesthetic model only as a tiebreaker within an event, calibrated
against a hand-labeled sample.

## Immich is the substrate, not the system of record

Research (see the `dd`, `scorecard` and stack briefs in the companion research repo) landed on
Immich for storage, browsing and semantic search. But its iOS ingestion is lossy — it drops
Apple Photos edit history, portrait/cinematic depth, and `.AAE` sidecars, keeping only the
rendered image — so letting it own the library breaks the contract before any of our code runs.

Immich is therefore mounted as an **external library** over the working copy, read-only. It is
the viewer, the star-rating and tag search, and the CLIP semantic search. This project is the
system of record.

**It is not the face detector.** An earlier revision of this file said it was. Three separate
reports show face detection not running over external libraries at all
([23879](https://github.com/immich-app/immich/issues/23879),
[23131](https://github.com/immich-app/immich/issues/23131),
[23880](https://github.com/immich-app/immich/discussions/23880) — `facesRecognizedAt` NULL on
external assets while uploaded assets carry timestamps). And it would not matter if it did:
stage 4 needs raw per-face embeddings to chain identity across years, and Immich exposes
people and faces but never the vectors underneath. Detection and embedding stay in-house at
stage 3.

The mount must end in `:ro`. Immich's own docs warn that without it, "Immich will be able to
delete the files in this folder" — which makes it a third-party writer with delete rights
pointed at your photos.

## Verification

There is no CI yet. `.\run test` (40 tests) and `.\run audit` are the gates.
`.\run snapshot` / `.\run verify` around any run is the empirical proof that the library was
not touched.

`tests/test_contract_paths.py` is the important half: it asserts that real command paths are
guarded, not merely that the guard function raises when called. The distinction is not
academic — two write paths bypassed the contract entirely while a fully green suite tested
the guard in isolation. See [CONTRACT.md](CONTRACT.md).

`PHOTO_CURATOR_FIXTURES=<dir>` enables an opt-in regression against real camera files; it is
skipped by default so the suite stays self-contained and references no personal photos.

**Known gap, stated up front.** The exact half of the two-phase hash does not close: `dupes`
records a full sha256 for one member of a multi-path group and never compares the members, so
two different files sharing a size, head 256 KB and tail 256 KB collapse into one asset and are
reported as a success. `tests/test_core.TestFingerprintCollision` builds that case and pins the
behaviour. It is low-probability on camera JPEGs and not zero elsewhere; closing it needs an
asset-splitting path that does not exist yet.
