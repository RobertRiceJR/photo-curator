# photo-curator

<!-- crucible:core v0.1.0 -->

## Domain knowledge

This project's domain knowledge lives in the **crucible hub**, not in this repo:

```
C:/Users/terri/Repos/crucible/knowledge/packs/photo-curator
```

**Read it before answering domain questions**, starting at that folder's `index.md`. Crucible is
the system of record for it — one tree, no per-repo copies to drift.

### What's in there, and what isn't

The hub holds what **isn't already written down in this repo**: decisions and their reasoning,
gotchas found the hard way, things established in conversation that live nowhere else.

It deliberately does **not** paraphrase this repo's own docs. Where a file here already says it
well, the hub links to that file instead — a summary is a second source of truth, and the two
disagree the moment one changes.

### Adding to it

Use the **`knowledge-distill`** skill (global — available in every repo). It writes into the hub,
records which file a node came from, and fingerprints that file so the lint can tell you when a
node has gone stale against its source.

Do not hand-copy knowledge into this repo. A local copy is invisible to the hub's lint and is the
exact drift this arrangement exists to prevent.

<!-- /crucible:core -->

## Working in this repo

**Originals are read-only, forever.** [CONTRACT.md](CONTRACT.md) is the invariant everything
else is built around — read it before changing anything that touches a path.

- **Never write to `D:\iCloudPhotos\Photos`**, to `C:\Users\terri\iCloudPhotos` (a junction to
  the same bytes), or to any other library root. Every artifact goes under `derived/`.
- The library is a **live iCloud sync root**. `verify` against it reports hundreds of drifted
  files that are not ours, so layer 3 is inoperative there. The pipeline runs against an
  isolated working copy.
- Absolute file counts for the library belong in `CALL_TREE.md` §4.0 and nowhere else. It is
  still syncing and gains thousands of files an hour; a count copied into another file is
  wrong within the hour and nothing will tell you.
- Entry point is `.\run <command>` — not `python src/cli.py`. `--derived` is global and must
  precede the subcommand.

```powershell
.\run test     # 40 tests, 1 skipped, stdlib unittest
.\run audit    # contract layers 1+2, mechanized
```

Stage 0 is the entire built surface. Everything in `CALL_TREE.md` §4.1–§4.9 is unwritten, and
the blocker is data, not code — there are no readable photographs on this machine yet.
