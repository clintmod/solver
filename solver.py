#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["watchdog>=4.0", "anthropic>=0.69"]
# ///
"""
Watch a folder for new screenshot BATCHES and solve the coding problem in each
with the Claude API (vision). Pairs with screenshot_hotkey.py: the trigger key
captures one-or-more pages into batch_<ts>/, and this watcher solves the whole
batch as a single multi-image request.

The batch contract (see BATCH_CONTRACT.md):
  batch_<ts>/
    page-01.png, page-02.png, ...   ordered pages (main display)
    manifest.json                   written LAST, atomically — the "done" signal

Flow per batch:
  1. detect a new manifest.json (recursive watch)
  2. read its `pages` in order
  3. send them as ordered image blocks in ONE Claude request (streaming)
  4. stream the solution to the terminal, post a macOS notification, and drop a
     `.solved` marker in the batch dir so a restart won't re-solve it

Defaults (override with env vars):
  SOLVER_DIR        folder to watch         (default: $SHOT_DIR or ~/Screenshots)
  SOLVER_MODEL      Claude model id         (default: claude-opus-4-8)
  SOLVER_LANG       preferred answer lang   (default: infer from screenshot)
  SOLVER_BACKLOG    solve batches already   (default: 0 — only new ones)
                    present at startup
  SOLVER_THINKING   stream reasoning too    (default: 0)
  SOLVER_NOTIFY     macOS notification      (default: 1)
  SOLVER_POLL       poll instead of native FS events — REQUIRED for a mounted/
                    network/shared volume, where FSEvents/inotify never fire for
                    another machine's writes (default: 1; set 0 for local-only)
  SOLVER_POLL_INTERVAL  seconds between polls (default: 1.0)
  SOLVER_SETTLE     secs each page (or a one-shot image) must be present and
                    size-stable before it's read — the "settle for multiple
                    pages" that matters on a mounted/network volume where page
                    bytes can lag the manifest                (default: 1.0)

Auth: ANTHROPIC_API_KEY in the environment, or an `ant auth login` profile —
the SDK resolves either. No key in code.
"""

import base64
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import anthropic
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver

WATCH_DIR = Path(
    os.environ.get("SOLVER_DIR") or os.environ.get("SHOT_DIR") or "~/Screenshots"
).expanduser()
MODEL = os.environ.get("SOLVER_MODEL", "claude-opus-4-8")
LANG = os.environ.get("SOLVER_LANG", "").strip()
SETTLE = float(os.environ.get("SOLVER_SETTLE", "1.0"))
BACKLOG = os.environ.get("SOLVER_BACKLOG", "0") == "1"
SHOW_THINKING = os.environ.get("SOLVER_THINKING", "0") == "1"
NOTIFY = os.environ.get("SOLVER_NOTIFY", "1") == "1"
# Native FS events (FSEvents/inotify) do NOT fire for files written by another
# machine on a mounted/network/shared volume. Poll by default so the watcher
# works across mounts; set SOLVER_POLL=0 for lower CPU on a purely-local folder.
POLL = os.environ.get("SOLVER_POLL", "1") != "0"
POLL_INTERVAL = float(os.environ.get("SOLVER_POLL_INTERVAL", "1.0"))

MANIFEST = "manifest.json"
SOLVED_MARKER = ".solved"

IMAGE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

# Sentinel the model returns when an image has no solvable code problem, so we
# can skip notifying for blank or irrelevant captures.
NO_PROBLEM = "NO_PROBLEM"

SYSTEM = (
    "You are shown one or more screenshots that are sequential pages of a SINGLE "
    "coding or algorithm problem (a LeetCode-style prompt, an interview question, "
    "a failing test, a code snippet with a bug, etc.). Read all pages together as "
    "one problem and solve it, outputting the solution as source code that will be "
    "printed straight to a terminal.\n\n"
    "Output rules:\n"
    "- Output ONLY the code. No Markdown, no ``` code fences, no prose before "
    "or after it.\n"
    "- Favor readability over cleverness. Write it the way you would to teach "
    "someone who just started programming.\n"
    "- Use clear, descriptive names and simple, obvious constructs. Avoid clever "
    "one-liners, nested comprehensions, bit tricks, dense chaining, and terse "
    "idioms a beginner would have to puzzle over. Straightforward and a little "
    "longer beats compact and surprising.\n"
    "- Begin with a short comment that says, in plain language, what the code "
    "does and the idea behind the approach. Add brief comments on any step that "
    "isn't self-explanatory. Don't over-comment the obvious.\n"
    "- Make it complete and runnable.\n"
    "- Pick the language the screenshot calls for; if none is specified"
    + (f", default to {LANG}.\n\n" if LANG else ", default to Python.\n\n")
    + "If the screenshots do NOT contain a solvable code problem (a blank screen, "
    f"a desktop, unrelated content), reply with exactly {NO_PROBLEM} and nothing "
    "else."
)


def wait_until_settled(path: Path, idle: float, timeout: float = 30.0) -> bool:
    """Block until `path` exists and its size stops changing for `idle` seconds.

    Waits for the file to APPEAR too (doesn't bail if it's not there yet): across
    a mounted/network volume the manifest can show up before the page bytes have
    finished propagating, so each page may still be absent or growing when we
    first look. Returns False if it never settles within `timeout`."""
    deadline = time.monotonic() + timeout
    last = -1
    stable_since = None
    while time.monotonic() < deadline:
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            stable_since = None  # not synced across the mount yet — keep waiting
            last = -1
            time.sleep(0.2)
            continue
        if size > 0 and size == last:
            if stable_since is None:
                stable_since = time.monotonic()
            elif time.monotonic() - stable_since >= idle:
                return True
        else:
            stable_since = None
        last = size
        time.sleep(0.2)
    return path.exists()


def notify(title: str, message: str) -> None:
    if not NOTIFY:
        return
    # Escape double quotes for the AppleScript string literals.
    t = title.replace('"', '\\"')
    m = message.replace('"', '\\"')
    try:
        subprocess.run(
            ["osascript", "-e", f'display notification "{m}" with title "{t}"'],
            check=False,
            timeout=5,
        )
    except Exception:
        pass


def image_block(path: Path) -> Optional[dict]:
    media_type = IMAGE_TYPES.get(path.suffix.lower())
    if media_type is None or not path.exists():
        return None
    data = base64.standard_b64encode(path.read_bytes()).decode("utf-8")
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media_type, "data": data},
    }


def stream_solution(content: list, client: anthropic.Anthropic, label: str) -> None:
    """Send one request with the given content blocks and stream the answer."""
    print(f"\n=== solving {label} ===", flush=True)

    thinking = {"type": "adaptive"}
    if SHOW_THINKING:
        thinking["display"] = "summarized"

    parts: list[str] = []
    try:
        with client.messages.stream(
            model=MODEL,
            max_tokens=64000,
            thinking=thinking,
            output_config={"effort": "high"},
            system=SYSTEM,
            messages=[{"role": "user", "content": content}],
        ) as stream:
            for event in stream:
                if event.type == "content_block_delta":
                    if event.delta.type == "text_delta":
                        parts.append(event.delta.text)
                        print(event.delta.text, end="", flush=True)
                    elif event.delta.type == "thinking_delta" and SHOW_THINKING:
                        print(event.delta.thinking, end="", flush=True)
            final = stream.get_final_message()
    except anthropic.AuthenticationError:
        print(
            "\n! auth failed — set ANTHROPIC_API_KEY or run `ant auth login`",
            file=sys.stderr,
            flush=True,
        )
        return
    except anthropic.APIError as e:
        print(f"\n! API error: {e}", file=sys.stderr, flush=True)
        return

    print(flush=True)
    answer = "".join(parts).strip()

    if final.stop_reason == "refusal":
        print(f"  ! {label}: refused", file=sys.stderr, flush=True)
        return
    if answer == NO_PROBLEM or not answer:
        print(f"  (no code problem in {label})", flush=True)
        return

    notify("Solver", f"Solved {label}")


def read_manifest(manifest_path: Path) -> Optional[dict]:
    """Read a manifest, tolerating a brief window where the rename is mid-flight."""
    for _ in range(5):
        try:
            return json.loads(manifest_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            time.sleep(0.1)
    return None


def solve_batch(manifest_path: Path, client: anthropic.Anthropic) -> None:
    batch_dir = manifest_path.parent
    marker = batch_dir / SOLVED_MARKER
    if marker.exists():
        return  # already solved (restart / duplicate event)

    manifest = read_manifest(manifest_path)
    if manifest is None:
        print(f"  ! {batch_dir.name}: unreadable manifest, skipping", file=sys.stderr, flush=True)
        return

    pages = manifest.get("pages") or sorted(
        p.name for p in batch_dir.glob("page-*.png")
    )
    # Wait for every listed page to appear and finish writing before reading it.
    # On a mounted/network volume the manifest can arrive ahead of the page
    # bytes, so a page may still be missing or growing right after detection.
    ready = []
    for name in pages:
        p = batch_dir / name
        if wait_until_settled(p, SETTLE):
            ready.append(p)
        else:
            print(f"  ! {batch_dir.name}/{name}: never settled, skipping page",
                  file=sys.stderr, flush=True)
    if len(ready) < len(pages):
        print(f"  ! {batch_dir.name}: {len(ready)}/{len(pages)} pages ready",
              file=sys.stderr, flush=True)

    blocks = [b for b in (image_block(p) for p in ready) if b]
    if not blocks:
        print(f"  ! {batch_dir.name}: no readable pages, skipping", file=sys.stderr, flush=True)
        return

    content: list = [
        {
            "type": "text",
            "text": (
                f"The following {len(blocks)} image(s) are sequential pages of one "
                "problem, in order. Read them together, then solve the problem."
            ),
        },
        *blocks,
    ]
    stream_solution(content, client, f"{batch_dir.name} ({len(blocks)} page(s))")
    marker.write_text("")  # idempotency: don't re-solve on restart


def solve_image(path: Path, client: anthropic.Anthropic) -> None:
    """One-shot: solve a single loose image (testing / `solve-one <img>`)."""
    if not wait_until_settled(path, SETTLE):
        print(f"  ! {path.name}: never settled, skipping", file=sys.stderr, flush=True)
        return
    block = image_block(path)
    if block is None:
        print(f"  ! {path.name}: not a readable image", file=sys.stderr, flush=True)
        return
    content = [block, {"type": "text", "text": "Solve the problem in this screenshot."}]
    stream_solution(content, client, path.name)


def daemonize() -> None:
    """Double-fork into a new session so a parent that tears down its process
    group can't reap us. Redirect std streams to the log file and record our own
    PID for `just solve-stop`. Mirrors the daemonize() in screenshot_hotkey.py.
    """
    pidfile = Path(os.environ.get("SOLVER_PIDFILE", ".solver.pid")).resolve()
    logfile = Path(os.environ.get("SOLVER_LOG", ".solver.log")).resolve()

    if os.fork() > 0:
        os._exit(0)  # original parent returns immediately
    os.setsid()  # new session — no controlling terminal, own process group
    if os.fork() > 0:
        os._exit(0)  # ensure we're not a session leader (can't reacquire a tty)

    sys.stdout.flush()
    sys.stderr.flush()
    with open(os.devnull, "rb") as devnull:
        os.dup2(devnull.fileno(), sys.stdin.fileno())
    log = open(logfile, "ab", buffering=0)
    os.dup2(log.fileno(), sys.stdout.fileno())
    os.dup2(log.fileno(), sys.stderr.fileno())
    pidfile.write_text(f"{os.getpid()}\n")


class Handler(FileSystemEventHandler):
    """Enqueue a batch when its manifest.json appears. A worker solves batches
    one at a time so concurrent batches don't interleave output."""

    def __init__(self, work: "queue.Queue[Path]") -> None:
        self.work = work

    def _maybe_enqueue(self, src: str) -> None:
        p = Path(src)
        if p.name == MANIFEST:
            self.work.put(p)

    def on_created(self, event) -> None:
        if not event.is_directory:
            self._maybe_enqueue(event.src_path)

    def on_moved(self, event) -> None:
        # The listener writes manifest.json via temp + rename, which surfaces as
        # a move to the final name.
        if not event.is_directory:
            self._maybe_enqueue(event.dest_path)


def worker(work: "queue.Queue[Path]", client: anthropic.Anthropic) -> None:
    seen: set[Path] = set()
    while True:
        manifest_path = work.get()
        try:
            key = manifest_path.parent.resolve()
            if key in seen:
                continue
            seen.add(key)
            solve_batch(manifest_path, client)
        except Exception as e:  # one bad batch shouldn't kill the watcher
            print(f"  ! error on {manifest_path.parent.name}: {e}", file=sys.stderr, flush=True)
        finally:
            work.task_done()


def main() -> None:
    daemon = "--daemon" in sys.argv[1:]
    args = [a for a in sys.argv[1:] if a != "--daemon"]

    client = anthropic.Anthropic()
    # Preflight in the foreground (before any fork) so a missing key fails
    # visibly instead of leaving a daemon's stale pid file behind.
    if not (
        os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("ANTHROPIC_AUTH_TOKEN")
        or getattr(client, "api_key", None)
        or getattr(client, "auth_token", None)
    ):
        print(
            "! No Anthropic credentials found.\n"
            "  Set ANTHROPIC_API_KEY in your environment, or run `ant auth login`.",
            file=sys.stderr,
        )
        sys.exit(1)

    # One-shot mode: `solver.py <arg>...` solves and exits. Each arg may be a
    # batch dir, a manifest.json, or a single image — handy for testing.
    if args:
        for raw in args:
            p = Path(raw).expanduser()
            if p.is_dir():
                solve_batch(p / MANIFEST if (p / MANIFEST).exists() else p, client)
            elif p.name == MANIFEST:
                solve_batch(p, client)
            else:
                solve_image(p, client)
        return

    if daemon:
        daemonize()

    WATCH_DIR.mkdir(parents=True, exist_ok=True)
    work: "queue.Queue[Path]" = queue.Queue()

    threading.Thread(target=worker, args=(work, client), daemon=True).start()

    if BACKLOG:
        for m in sorted(WATCH_DIR.glob(f"**/{MANIFEST}")):
            if not (m.parent / SOLVED_MARKER).exists():
                work.put(m)

    observer = PollingObserver(timeout=POLL_INTERVAL) if POLL else Observer()
    observer.schedule(Handler(work), str(WATCH_DIR), recursive=True)
    observer.start()

    watch_mode = f"polling every {POLL_INTERVAL}s" if POLL else "native FS events"
    print(
        f"Watching {WATCH_DIR} for new batches\n"
        f"  model:   {MODEL}\n"
        f"  watch:   {watch_mode}\n"
        f"  backlog: {'yes' if BACKLOG else 'no (new batches only)'}\n"
        "Press Ctrl-C to stop.",
        flush=True,
    )
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nstopped.", flush=True)
    finally:
        observer.stop()
        observer.join()


if __name__ == "__main__":
    main()
