# solver

Two small macOS tools that turn a screenshot of a coding problem into a worked
solution:

1. **`screenshot_hotkey.py`** — a listener that watches for a global hotkey and
   saves a silent full-screen capture to a folder (`~/Screenshots` by default).
2. **`solver.py`** — a watcher that picks up each new capture, sends it to the
   Claude API (vision), and streams back a solution to the problem in the image.

Hit the hotkey on a LeetCode prompt / interview question / failing test, and a
few seconds later the solution streams into the solver's terminal. The answer is
plain, readable code — written the way you'd explain it to a new programmer, not
clever one-liners — and nothing is written to disk. Run both detached and you
have a hands-off capture→solve loop.

## Setup

Tasks are driven by **`just`**, and the toolchain (`uv` + `just`) is pinned in
`mise.toml`. On a fresh machine:

```sh
mise install     # installs the pinned uv + just (or: just setup, once just exists)
just --list      # see every task
```

`uv` resolves each script's dependencies from its inline PEP 723 metadata
(`pynput` for the listener; `watchdog` + `anthropic` for the solver) on first
run — no manual venv.

## Run

```sh
# capture side
just run                              # listen for the hotkey, foreground (Ctrl-C to stop)
just shot                             # take one capture now, to verify the path works
just open                             # open the screenshots folder

# solve side
just solve                            # watch the folder and solve new captures
just solve-one ~/Screenshots/x.png    # solve a single image now (no watcher)
```

### Detached

Both sides have background variants that survive closing the terminal (not a
reboot — they inherit the launching terminal's Accessibility permission):

| listener                | solver                        |
|-------------------------|-------------------------------|
| `just start`            | `just solve-start`            |
| `just stop`             | `just solve-stop`             |
| `just status`           | `just solve-status`           |
| `just logs`             | `just solve-logs`             |

Each writes a pidfile (`.listener.pid` / `.solver.pid`) and a log
(`.listener.log` / `.solver.log`) in the repo root.

## Auth (solver only)

The solver needs Anthropic credentials — either works:

```sh
export ANTHROPIC_API_KEY=sk-ant-...   # in your shell / env
# or
ant auth login                        # OAuth profile the SDK picks up
```

It fails fast with a clear message if neither is present (before forking, so a
detached start won't leave a stale pidfile behind).

## Config (env vars)

### Capture — `screenshot_hotkey.py`

| var          | default         | meaning                                          |
|--------------|-----------------|--------------------------------------------------|
| `SHOT_HOTKEY`| `##`            | typed character sequence that triggers a capture |
| `SHOT_DIR`   | `~/Screenshots` | where captures are saved                         |
| `SHOT_WINDOW`| `0.6`           | max seconds between the sequence's keystrokes     |

### Solve — `solver.py`

| var              | default                       | meaning                                       |
|------------------|-------------------------------|-----------------------------------------------|
| `SOLVER_DIR`     | `$SHOT_DIR` or `~/Screenshots`| folder to watch                               |
| `SOLVER_MODEL`   | `claude-opus-4-8`             | Claude model id                               |
| `SOLVER_LANG`    | *(infer from screenshot)*     | preferred answer language (e.g. `Go`)         |
| `SOLVER_SETTLE`  | `1.0`                         | seconds a file's size must hold before reading|
| `SOLVER_BACKLOG` | `0`                           | `1` = also solve images already in the folder |
| `SOLVER_THINKING`| `0`                           | `1` = stream Claude's reasoning summary too   |
| `SOLVER_NOTIFY`  | `1`                           | `0` = no macOS notification on completion     |

Example: `SOLVER_LANG=Go SOLVER_THINKING=1 just solve`

## How the solver works

- Detects new `.png` / `.jpg` / `.gif` / `.webp` files via `watchdog` (FSEvents).
- Waits for the file's size to settle, so it never reads a half-written capture.
- Streams the request to `claude-opus-4-8` with adaptive thinking, high effort,
  and the image as a vision content block; the solution streams to the terminal
  as plain code (no file is written).
- The prompt asks for readable, beginner-friendly code — clear names, simple
  constructs, a short comment on the approach — over clever or terse idioms.
- A single worker thread solves one image at a time, so a multi-display capture
  (one PNG per screen) doesn't interleave its output.
- If a screenshot has no solvable problem, the model returns a sentinel and the
  solver skips the notification — so a blank second monitor costs nothing but
  the detection call.

## macOS permissions (one-time)

- **Accessibility** — System Settings ▸ Privacy & Security ▸ Accessibility ▸
  enable for the app that launches the listener (Terminal / iTerm). Without it,
  `pynput` receives **zero** keystrokes and nothing fires. (The solver needs no
  special permission — it only reads files and calls an API.)
- **Screen Recording** is *not* required for full-screen `screencapture`.

## ⚠️ The `##` default collides with Markdown

`##` is a plain character sequence, so it fires whenever you type an H2 header.
Two ways out:

1. **Different sequence** — set `SHOT_HOTKEY` to something you never type,
   e.g. `;;;` or `\`\``.
2. **Real modifier chord (no collisions)** — swap the listener in
   `screenshot_hotkey.py` for `pynput.keyboard.GlobalHotKeys`:

   ```python
   from pynput import keyboard
   with keyboard.GlobalHotKeys({"<cmd>+<shift>+9": capture}) as h:
       h.join()
   ```

   This only fires on the exact chord and never on typed text.
