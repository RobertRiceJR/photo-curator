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
