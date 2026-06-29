#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["watchdog>=4.0", "anthropic>=0.69"]
# ///
"""
Watch a folder for new screenshots and solve the coding problem in each one
with the Claude API (vision). Pairs with screenshot_hotkey.py: hotkey captures
a problem to ~/Screenshots, this watcher solves it.

Flow per new image:
  1. detect a new .png/.jpg dropped into the watched folder
  2. wait for the file to finish writing (size settles)
  3. send it to Claude (claude-opus-4-8, vision, streaming, adaptive thinking)
  4. stream the solution code to the terminal, and post a macOS notification

The answer is plain, readable code printed to the terminal — favoring clarity a
new programmer could follow over clever one-liners. Nothing is written to disk.

Defaults (override with env vars):
  SOLVER_DIR        folder to watch         (default: $SHOT_DIR or ~/Screenshots)
  SOLVER_MODEL      Claude model id         (default: claude-opus-4-8)
  SOLVER_LANG       preferred answer lang   (default: infer from screenshot)
  SOLVER_SETTLE     secs file must be idle  (default: 1.0)
  SOLVER_BACKLOG    solve files already     (default: 0 — only new files)
                    present at startup
  SOLVER_THINKING   stream reasoning too    (default: 0)
  SOLVER_NOTIFY     macOS notification      (default: 1)

Auth: ANTHROPIC_API_KEY in the environment, or an `ant auth login` profile —
the SDK resolves either. No key in code.
"""

import base64
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import anthropic
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

WATCH_DIR = Path(
    os.environ.get("SOLVER_DIR") or os.environ.get("SHOT_DIR") or "~/Screenshots"
).expanduser()
MODEL = os.environ.get("SOLVER_MODEL", "claude-opus-4-8")
LANG = os.environ.get("SOLVER_LANG", "").strip()
SETTLE = float(os.environ.get("SOLVER_SETTLE", "1.0"))
BACKLOG = os.environ.get("SOLVER_BACKLOG", "0") == "1"
SHOW_THINKING = os.environ.get("SOLVER_THINKING", "0") == "1"
NOTIFY = os.environ.get("SOLVER_NOTIFY", "1") == "1"

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
    "You are shown a screenshot that should contain a coding or algorithm "
    "problem (a LeetCode-style prompt, an interview question, a failing test, a "
    "code snippet with a bug, etc.). Solve it and output the solution as source "
    "code that will be printed straight to a terminal.\n\n"
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
    + "If the screenshot does NOT contain a solvable code problem (it's a blank "
    f"screen, a desktop, unrelated content), reply with exactly {NO_PROBLEM} and "
    "nothing else."
)


def wait_until_settled(path: Path, idle: float, timeout: float = 30.0) -> bool:
    """Block until path's size stops changing for `idle` seconds. screencapture
    and downloads write incrementally; reading mid-write yields a corrupt image."""
    deadline = time.monotonic() + timeout
    last = -1
    stable_since = None
    while time.monotonic() < deadline:
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return False
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


def solve(path: Path, client: anthropic.Anthropic) -> None:
    media_type = IMAGE_TYPES.get(path.suffix.lower())
    if media_type is None:
        return

    if not wait_until_settled(path, SETTLE):
        print(f"  ! {path.name}: never settled, skipping", file=sys.stderr, flush=True)
        return

    data = base64.standard_b64encode(path.read_bytes()).decode("utf-8")
    print(f"\n=== solving {path.name} ===", flush=True)

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
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": data,
                            },
                        },
                        {"type": "text", "text": "Solve the problem in this screenshot."},
                    ],
                }
            ],
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
        print(f"  ! {path.name}: refused", file=sys.stderr, flush=True)
        return
    if answer == NO_PROBLEM or not answer:
        print(f"  (no code problem in {path.name})", flush=True)
        return

    notify("Solver", f"Solved {path.name}")


def daemonize() -> None:
    """Double-fork into a new session so a parent that tears down its process
    group (e.g. rite's gosh interpreter) can't reap us. Redirect std streams to
    the log file and record our own PID for `rite solve-stop`. Mirrors the
    daemonize() in screenshot_hotkey.py.
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
    """Enqueue new image files; a worker thread solves them one at a time so
    overlapping captures (e.g. one file per display) don't interleave output."""

    def __init__(self, work: "queue.Queue[Path]") -> None:
        self.work = work

    def _maybe_enqueue(self, src: str) -> None:
        p = Path(src)
        if p.suffix.lower() in IMAGE_TYPES:
            self.work.put(p)

    def on_created(self, event) -> None:
        if not event.is_directory:
            self._maybe_enqueue(event.src_path)

    def on_moved(self, event) -> None:
        # Some tools write to a temp name then rename into place.
        if not event.is_directory:
            self._maybe_enqueue(event.dest_path)


def worker(work: "queue.Queue[Path]", client: anthropic.Anthropic) -> None:
    seen: set[Path] = set()
    while True:
        path = work.get()
        try:
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            solve(path, client)
        except Exception as e:  # one bad image shouldn't kill the watcher
            print(f"  ! error on {path.name}: {e}", file=sys.stderr, flush=True)
        finally:
            work.task_done()


def main() -> None:
    daemon = "--daemon" in sys.argv[1:]
    files = [a for a in sys.argv[1:] if a != "--daemon"]

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

    # One-shot mode: `solver.py <image>` solves that file and exits. Handy for
    # testing the API path without running the watcher.
    if files:
        for arg in files:
            solve(Path(arg).expanduser(), client)
        return

    if daemon:
        daemonize()

    WATCH_DIR.mkdir(parents=True, exist_ok=True)
    work: "queue.Queue[Path]" = queue.Queue()

    threading.Thread(target=worker, args=(work, client), daemon=True).start()

    if BACKLOG:
        for p in sorted(WATCH_DIR.iterdir()):
            if p.is_file() and p.suffix.lower() in IMAGE_TYPES:
                work.put(p)

    observer = Observer()
    observer.schedule(Handler(work), str(WATCH_DIR), recursive=False)
    observer.start()

    print(
        f"Watching {WATCH_DIR} for new screenshots\n"
        f"  model:   {MODEL}\n"
        f"  backlog: {'yes' if BACKLOG else 'no (new files only)'}\n"
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
