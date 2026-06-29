#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pynput>=1.7"]
# ///
"""
Listen for a trigger key and capture the screen, grouping rapid captures into
ordered, multi-page BATCHES for the solver to consume as a single unit.

Workflow: press the trigger (Page Down by default) once per page. Each press
captures the main display into the current batch as page-NN.png. After
SHOT_BATCH_WINDOW seconds with no new capture, the batch closes — an atomic
manifest.json is written, which is the solver's signal that the batch is
complete. A single capture is just a batch of one.

Layout written under SHOT_DIR:
  batch_2026-06-29_15.10.00/
    page-01.png
    page-02.png
    manifest.json        <- written LAST, atomically; the "batch done" signal

Config (env vars):
  SHOT_DIR           base folder for batches            (default: ~/Screenshots)
  SHOT_KEY           pynput Key name that triggers a capture (default: page_down)
                     e.g. page_down, page_up, f13, end, home, insert
  SHOT_BATCH_WINDOW  idle seconds before a batch closes (default: 3.0)
  SHOT_PAGE_SETTLE   seconds to wait after the key, so the page finishes
                     scrolling/rendering, before grabbing it (default: 0.25)

macOS permissions (required, one-time):
  - System Settings > Privacy & Security > Accessibility -> enable for whatever
    runs this (your terminal app). Without it pynput receives zero key events.
  - Screen Recording is NOT needed for full-screen `screencapture`.

CAVEAT on the Page Down default: it is a normal navigation key, so while this
listener runs, EVERY Page Down anywhere (browser, editor, mail) triggers a
capture. Run it only during a capture session, or set SHOT_KEY=f13 (or another
key you never press) to make it collision-free.
"""

import json
import os
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from pynput import keyboard

SHOT_DIR = Path(os.environ.get("SHOT_DIR", "~/Screenshots")).expanduser()
KEY_NAME = os.environ.get("SHOT_KEY", "page_down")
BATCH_WINDOW = float(os.environ.get("SHOT_BATCH_WINDOW", "3.0"))
PAGE_SETTLE = float(os.environ.get("SHOT_PAGE_SETTLE", "0.25"))

# Resolve the trigger to a pynput special key; fall back to Page Down.
TRIGGER = getattr(keyboard.Key, KEY_NAME, keyboard.Key.page_down)

# Batch state, guarded by _lock. on_press is serialized by pynput, but the
# close timer fires on its own thread, so both touch _batch under the lock.
_lock = threading.Lock()
_batch: Optional[dict] = None


def grab(target: Path) -> bool:
    """Silently capture the main display into `target` (after a settle delay so
    a just-scrolled page has rendered). Returns True on success."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if PAGE_SETTLE > 0:
        threading.Event().wait(PAGE_SETTLE)
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


def on_press(key) -> None:
    if key == TRIGGER:
        capture_page()


def main() -> None:
    print(
        f"Listening for {KEY_NAME!r} -> batches under {SHOT_DIR}\n"
        f"(batch closes after {BATCH_WINDOW}s idle; {PAGE_SETTLE}s settle before each grab)\n"
        "Press Ctrl-C to stop.",
        flush=True,
    )
    try:
        with keyboard.Listener(on_press=on_press) as listener:
            listener.join()
    except KeyboardInterrupt:
        print("\nstopped.", flush=True)


if __name__ == "__main__":
    main()
