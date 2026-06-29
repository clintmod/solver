#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pynput>=1.7"]
# ///
"""
Listen for a DOUBLE-CLICK and capture the screen, grouping captures into
ordered, multi-page BATCHES for the solver to consume as a unit.

Workflow:
  - double-click          -> capture a page into the current batch (opens a new
                             batch if none is open). The batch stays open.
  - <modifier>+double-click -> capture the final page AND close the batch. The
                             modifier defaults to Option (Alt). Closing writes
                             manifest.json, which is the solver's "done" signal.

There is NO idle timer — a batch only closes when you explicitly close it, so
take all the time you need to scroll between pages. A single screenshot is just
one Option+double-click.

Layout written under SHOT_DIR:
  batch_2026-06-29_15.10.00/
    page-01.png
    page-02.png
    manifest.json        <- written on close, atomically; the "batch done" signal

Config (env vars):
  SHOT_DIR           base folder for batches            (default: ~/Screenshots)
  SHOT_CLICKS        clicks that make a trigger         (default: 2 = double)
  SHOT_CLICK_WINDOW  max seconds to land all the clicks (default: 0.5)
  SHOT_CLICK_RADIUS  max pixels the clicks may drift    (default: 12)
  SHOT_CLOSE_MODIFIER  modifier that closes the batch: cmd|alt|ctrl|shift
                       (default: cmd = Command)
  SHOT_PAGE_SETTLE   seconds to wait after the click before grabbing (default: 0.1)

macOS permissions (required, one-time):
  - System Settings > Privacy & Security > Accessibility (and Input Monitoring
    if prompted) -> enable for whatever runs this (your terminal app). Without
    it pynput receives zero mouse/key events.
  - Screen Recording is NOT needed for full-screen `screencapture`.

CAVEAT: a double-click is the most common mouse action there is (select a word,
open a file...), so while this listener runs, ANY double-click fires a capture —
and with no idle timer those stray captures accumulate in the open batch until
you close it. Run it only during a capture session (`just start` / `just stop`),
or set SHOT_CLICKS=3 for a triple-click trigger that collides far less.
"""

import json
import os
import subprocess
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Optional

from pynput import keyboard, mouse

SHOT_DIR = Path(os.environ.get("SHOT_DIR", "~/Screenshots")).expanduser()
CLICKS = int(os.environ.get("SHOT_CLICKS", "2"))
CLICK_WINDOW = float(os.environ.get("SHOT_CLICK_WINDOW", "0.5"))
CLICK_RADIUS = float(os.environ.get("SHOT_CLICK_RADIUS", "12"))
PAGE_SETTLE = float(os.environ.get("SHOT_PAGE_SETTLE", "0.1"))
CLOSE_MODIFIER = os.environ.get("SHOT_CLOSE_MODIFIER", "cmd").lower()


def _mod_keys(name: str) -> set:
    """The pynput Key variants for a modifier name (left/right/generic)."""
    keys = set()
    for suffix in ("", "_l", "_r"):
        k = getattr(keyboard.Key, name + suffix, None)
        if k is not None:
            keys.add(k)
    return keys


CLOSE_KEYS = _mod_keys(CLOSE_MODIFIER) or _mod_keys("cmd")

# (x, y, monotonic_timestamp) for the last CLICKS left-button presses.
_clicks: deque = deque(maxlen=CLICKS)
_last_fire = 0.0
_close_mod_down = False  # set by the keyboard listener; read on click

# on_click runs on pynput's single mouse-listener thread (serialized), and is the
# only toucher of _batch, so no lock is needed.
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


def write_manifest(batch: dict) -> None:
    """Write manifest.json atomically (temp + rename) as the completion signal."""
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


def capture_page(close: bool) -> None:
    """Capture one page into the current batch (opening one if needed). If
    `close`, finalize the batch afterward so the solver picks it up."""
    global _batch
    if _batch is None:
        stamp = datetime.now().strftime("%Y-%m-%d_%H.%M.%S")
        _batch = {
            "dir": SHOT_DIR / f"batch_{stamp}",
            "created": datetime.now().isoformat(timespec="seconds"),
            "count": 0,
        }
        print(f"[batch] new {_batch['dir'].name}", flush=True)

    _batch["count"] += 1
    n = _batch["count"]
    if grab(_batch["dir"] / f"page-{n:02d}.png"):
        print(f"[batch] {_batch['dir'].name} <- page-{n:02d}.png", flush=True)

    if close:
        write_manifest(_batch)
        _batch = None


def on_key_press(key) -> None:
    global _close_mod_down
    if key in CLOSE_KEYS:
        _close_mod_down = True


def on_key_release(key) -> None:
    global _close_mod_down
    if key in CLOSE_KEYS:
        _close_mod_down = False


def on_click(x, y, button, pressed) -> None:
    """Fire when CLICKS left-button presses land within CLICK_WINDOW seconds and
    CLICK_RADIUS pixels. With the close modifier held, also close the batch."""
    global _last_fire
    if button != mouse.Button.left or not pressed:
        return

    now = time.monotonic()
    _clicks.append((x, y, now))

    if len(_clicks) < CLICKS:
        return
    if _clicks[-1][2] - _clicks[0][2] > CLICK_WINDOW:
        return
    x0, y0, _ = _clicks[0]
    if any((cx - x0) ** 2 + (cy - y0) ** 2 > CLICK_RADIUS**2 for cx, cy, _ in _clicks):
        return
    if now - _last_fire < 0.4:  # debounce so one gesture can't double-fire
        return

    _last_fire = now
    _clicks.clear()
    capture_page(close=_close_mod_down)


def main() -> None:
    click_word = {2: "double", 3: "triple"}.get(CLICKS, f"{CLICKS}x")
    print(
        f"Listening for {click_word}-click -> batches under {SHOT_DIR}\n"
        f"  {click_word}-click            : capture a page (batch stays open)\n"
        f"  {CLOSE_MODIFIER}+{click_word}-click : capture last page AND close (solve it)\n"
        "Press Ctrl-C to stop.",
        flush=True,
    )
    kb = keyboard.Listener(on_press=on_key_press, on_release=on_key_release)
    kb.daemon = True
    kb.start()
    try:
        with mouse.Listener(on_click=on_click) as listener:
            listener.join()
    except KeyboardInterrupt:
        print("\nstopped.", flush=True)
    finally:
        kb.stop()


if __name__ == "__main__":
    main()
