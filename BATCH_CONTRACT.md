# Capture → Solve batch contract

The listener (`screenshot_hotkey.py`) now emits **batches**, not loose PNGs.
This is the contract `solver.py` must consume. (Written by the capture-side
session for the solver-side session — keep them in sync.)

## What the listener writes

Per batch, under `SHOT_DIR` (default `~/Screenshots`):

```
batch_<YYYY-MM-DD_HH.MM.SS>/
  page-01.png
  page-02.png
  ...
  manifest.json        # written LAST, atomically (temp file + rename)
```

- Pages are the **main display only**, one PNG per trigger press, zero-padded
  (`page-01`, `page-02`, …) so lexical sort == capture order.
- A batch opens on the first capture and closes after `SHOT_BATCH_WINDOW`
  seconds (default 3.0) with no new capture. A single capture → a batch of one.

## The completion signal

**`manifest.json` is the "batch is done" signal.** It is written via temp +
`rename`, so its appearance is atomic — when the solver sees it, every
`page-*.png` is fully written. Do **not** trigger off the `page-*.png` files
themselves (you'd race a half-filled batch).

`manifest.json` schema:

```json
{
  "batch": "batch_2026-06-29_15.10.00",
  "created": "2026-06-29T15:10:00",
  "closed":  "2026-06-29T15:10:07",
  "count": 2,
  "pages": ["page-01.png", "page-02.png"]
}
```

## What the solver should do

1. Watch `SOLVER_DIR` (== `SHOT_DIR`) **recursively** for `manifest.json`
   creation (watchdog: `FileCreatedEvent` / `FileMovedEvent` — the atomic
   rename may surface as a move to the final name).
2. Read the manifest, load `pages` in listed order.
3. Send all pages as ordered image blocks in **one** Claude request → one
   solution. Write it to e.g. `SOLVER_OUT/<batch>.md`.
4. Mark the batch handled (e.g. write a sibling `.solved` marker or move the
   folder) so a watcher restart doesn't re-solve it.

### Backlog on startup
To solve pre-existing batches, scan for `*/manifest.json` without a `.solved`
sibling, not for bare images.
