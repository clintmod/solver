#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pynput>=1.7"]
# ///
"""
Listen for a TRIPLE-CLICK and capture the screen, grouping rapid captures into
ordered, multi-page BATCHES for the solver to consume as a unit.

Workflow: triple-click (3 quick left-clicks at the same spot) once per page.
Each trigger captures the main display into the current batch as page-NN.png.
After SHOT_BATCH_WINDOW seconds with no new capture, the batch closes — an
atomic manifest.json is written, which is the solver's "batch done" signal. A
single capture is just a batch of one.

Layout written under SHOT_DIR:
  batch_2026-06-29_15.10.00/
    page-01.png
    page-02.png
    manifest.json        <- written LAST, atomically; the "batch done" signal

Config (env vars):
  SHOT_DIR           base folder for batches            (default: ~/Screenshots)
  SHOT_CLICKS        clicks that make a trigger         (default: 3)
  SHOT_CLICK_WINDOW  max seconds to land all the clicks (default: 0.8)
  SHOT_CLICK_RADIUS  max pixels the clicks may drift    (default: 12)
  SHOT_BATCH_WINDOW  idle seconds before a batch closes (default: 3.0)
  SHOT_PAGE_SETTLE   seconds to wait after the trigger before grabbing
                     (lets any click menu/selection clear) (default: 0.1)

macOS permissions (required, one-time):
  - System Settings > Privacy & Security > Accessibility (and Input Monitoring
    if prompted) -> enable for whatever runs this (your terminal app). Without
    it pynput receives zero mouse/key events.
  - Screen Recording is NOT needed for full-screen `screencapture`.

CAVEAT: a triple-click is also how apps select a paragraph/line, so while this
listener runs, triple-clicking text fires a capture too. Raise SHOT_CLICKS or
tighten SHOT_CLICK_WINDOW/RADIUS if that bites.
"""

import json
import os
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Optional

from pynput import mouse

SHOT_DIR = Path(os.environ.get("SHOT_DIR", "~/Screenshots")).expanduser()
CLICKS = int(os.environ.get("SHOT_CLICKS", "3"))
CLICK_WINDOW = float(os.environ.get("SHOT_CLICK_WINDOW", "0.8"))
CLICK_RADIUS = float(os.environ.get("SHOT_CLICK_RADIUS", "12"))
BATCH_WINDOW = float(os.environ.get("SHOT_BATCH_WINDOW", "3.0"))
PAGE_SETTLE = float(os.environ.get("SHOT_PAGE_SETTLE", "0.1"))

# (x, y, monotonic_timestamp) for the last CLICKS left-button presses.
_clicks: deque = deque(maxlen=CLICKS)
_last_fire = 0.0

# Batch state, guarded by _lock. on_click is serialized by pynput, but the
# close timer fires on its own thread, so both touch _batch under the lock.
_lock = threading.Lock()
_batch: Optional[dict] = None


def grab(target: Path) -> bool:
    """Silently capture the main display into `target`. Returns True on success."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if PAGE_SETTLE > 0:
        time.sleep(PAGE_SETTLE)
    try:
        subprocess.run(["screencapture", "-x", str(target)], check=True)
        return True
    except subprocess.CalledProcessError as e:
        print(f"capture failed: {e}", file=sys.stderr, flush=True)
        return False


def finalize(batch: dict) -> None:
    """Close `batch`: write manifest.json atomically (temp + rename) as the
    completion signal, then clear the active batch if it's still this one."""
    global _batch
    with _lock:
        if _batch is not batch:
            return  # superseded by a newer batch — already handled
        d: Path = batch["dir"]
        pages = sorted(p.name for p in d.glob("page-*.png"))
        manifest = {
            "batch": d.name,
            "created": batch["created"],
            "closed": datetime.now().isoformat(timespec="seconds"),
            "count": len(pages),
            "pages": pages,
        }
        tmp = d / ".manifest.json.tmp"
        tmp.write_text(json.dumps(manifest, indent=2))
        tmp.replace(d / "manifest.json")  # atomic on the same filesystem
        print(f"[batch] closed {d.name} ({len(pages)} page(s))", flush=True)
        _batch = None


def schedule_close(batch: dict) -> None:
    timer = threading.Timer(BATCH_WINDOW, finalize, args=(batch,))
    timer.daemon = True
    batch["timer"] = timer
    timer.start()


def capture_page() -> None:
    """Add one page to the current batch (starting a new batch if none is open)
    and (re)arm the idle-close timer."""
    global _batch
    with _lock:
        if _batch is not None and _batch.get("timer"):
            _batch["timer"].cancel()  # no-op if it already fired (identity check guards)
        if _batch is None:
            stamp = datetime.now().strftime("%Y-%m-%d_%H.%M.%S")
            _batch = {
                "dir": SHOT_DIR / f"batch_{stamp}",
                "created": datetime.now().isoformat(timespec="seconds"),
                "count": 0,
                "timer": None,
            }
            print(f"[batch] new {_batch['dir'].name}", flush=True)
        _batch["count"] += 1
        n = _batch["count"]
        target = _batch["dir"] / f"page-{n:02d}.png"
        batch = _batch

    if grab(target):
        print(f"[batch] {batch['dir'].name} <- page-{n:02d}.png", flush=True)

    with _lock:
        if _batch is batch:  # still the active batch; (re)arm the close timer
            schedule_close(batch)


def on_click(x, y, button, pressed) -> None:
    """Fire capture_page() when CLICKS left-button presses land within
    CLICK_WINDOW seconds and within CLICK_RADIUS pixels of each other."""
    global _last_fire
    if button != mouse.Button.left or not pressed:
        return

    now = time.monotonic()
    _clicks.append((x, y, now))

    if len(_clicks) < CLICKS:
        return
    # All clicks within the time window...
    if _clicks[-1][2] - _clicks[0][2] > CLICK_WINDOW:
        return
    # ...and clustered in space (max drift from the first click).
    x0, y0, _ = _clicks[0]
    if any((cx - x0) ** 2 + (cy - y0) ** 2 > CLICK_RADIUS**2 for cx, cy, _ in _clicks):
        return
    # Debounce so one deliberate trigger can't double-fire.
    if now - _last_fire < 0.5:
        return

    _last_fire = now
    _clicks.clear()
    capture_page()


def main() -> None:
    print(
        f"Listening for {CLICKS}x-click -> batches under {SHOT_DIR}\n"
        f"(clicks within {CLICK_WINDOW}s & {CLICK_RADIUS}px; batch closes after {BATCH_WINDOW}s idle)\n"
        "Press Ctrl-C to stop.",
        flush=True,
    )
    try:
        with mouse.Listener(on_click=on_click) as listener:
            listener.join()
    except KeyboardInterrupt:
        print("\nstopped.", flush=True)


if __name__ == "__main__":
    main()
