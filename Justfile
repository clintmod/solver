# Screenshot hotkey + solver — book of spells.
# uv resolves each script's inline-metadata deps automatically (PEP 723):
#   pynput (listener); watchdog + anthropic (solver).
#
# screenshot_hotkey.py reads (batches captures into batch_<ts>/ folders):
#   SHOT_DIR           (default ~/Screenshots)  base folder for batches
#   SHOT_KEY           (default page_down)      pynput key name that captures a page
#   SHOT_BATCH_WINDOW  (default 3.0)            idle seconds before a batch closes
#   SHOT_PAGE_SETTLE   (default 0.25)           wait after the key before grabbing
#   (see BATCH_CONTRACT.md for the batch/manifest format the solver consumes)
# solver.py reads:
#   SOLVER_DIR   (default $SHOT_DIR or ~/Screenshots), SOLVER_OUT, SOLVER_MODEL,
#   SOLVER_LANG, ANTHROPIC_API_KEY (required) — see README.

set positional-arguments

# Resolve the mise binary once (PATH, else the standard install location the
# `mise.run` installer uses). Tools are always invoked as `{{mise}} exec -- ...`
# so they work WITHOUT mise shell-activation — bare `uv` only resolves via mise
# shims, which require activation and is exactly why a fresh machine reports
# "uv not installed".
mise := `command -v mise 2>/dev/null || echo "$HOME/.local/bin/mise"`

# List tasks
default:
    @just --list

# Install mise (if missing) + the pinned toolchain (uv, just) — idempotent.
# Every uv-using recipe depends on this, so a cold machine self-bootstraps.
setup:
    #!/usr/bin/env bash
    set -euo pipefail
    if ! command -v mise >/dev/null 2>&1 && [ ! -x "$HOME/.local/bin/mise" ]; then
        echo "mise not found — installing from https://mise.run ..."
        curl -fsSL https://mise.run | sh
    fi
    "{{mise}}" trust
    "{{mise}}" install
    echo "toolchain ready: $("{{mise}}" exec -- uv --version)"

# --- capture: foreground ----------------------------------------------------

# Run the listener in the foreground (Ctrl-C to stop).
run: setup
    {{mise}} exec -- uv run screenshot_hotkey.py

# Take a single full-screen capture now (verifies screencapture + folder).
shot:
    #!/usr/bin/env bash
    set -euo pipefail
    dir="${SHOT_DIR:-$HOME/Screenshots}"
    mkdir -p "$dir"
    f="$dir/Screenshot_$(date '+%Y-%m-%d_%H.%M.%S').png"
    screencapture -x "$f"
    echo "saved -> $f"

# Open the screenshots folder in Finder.
open:
    open "${SHOT_DIR:-$HOME/Screenshots}"

# --- capture: detached ------------------------------------------------------
# `just` runs recipes in real bash, so a plain nohup child truly detaches and
# survives closing the terminal (NOT a reboot). Inherits Accessibility
# permission from the terminal you launch it from.

# Start the listener detached in the background.
start: setup
    #!/usr/bin/env bash
    set -euo pipefail
    if [ -f .listener.pid ] && kill -0 "$(cat .listener.pid)" 2>/dev/null; then
        echo "already running (pid $(cat .listener.pid))"; exit 0
    fi
    nohup {{mise}} exec -- uv run screenshot_hotkey.py >> .listener.log 2>&1 </dev/null &
    echo $! > .listener.pid
    echo "started (pid $(cat .listener.pid)) -> logs: just logs"

# Stop the detached listener.
stop:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ ! -f .listener.pid ]; then echo "not running"; exit 0; fi
    pid="$(cat .listener.pid)"
    kill "$pid" 2>/dev/null && echo "stopped (pid $pid)" || echo "pid $pid not alive"
    rm -f .listener.pid

# Show whether the detached listener is running.
status:
    #!/usr/bin/env bash
    if [ -f .listener.pid ] && kill -0 "$(cat .listener.pid)" 2>/dev/null; then
        echo "running (pid $(cat .listener.pid))"
    else
        echo "not running"
    fi

# Tail the detached listener's log.
logs:
    tail -f .listener.log

# --- solver (solver.py built in a separate session) -------------------------

# Watch the screenshots folder and solve new captures (Ctrl-C to stop).
solve: setup
    {{mise}} exec -- uv run solver.py

# Solve a single image now, e.g.: just solve-one ~/Screenshots/foo.png
solve-one *ARGS: setup
    {{mise}} exec -- uv run solver.py "$@"

# Start the solver watcher detached in the background.
solve-start: setup
    #!/usr/bin/env bash
    set -euo pipefail
    if [ -f .solver.pid ] && kill -0 "$(cat .solver.pid)" 2>/dev/null; then
        echo "already running (pid $(cat .solver.pid))"; exit 0
    fi
    nohup {{mise}} exec -- uv run solver.py >> .solver.log 2>&1 </dev/null &
    echo $! > .solver.pid
    echo "started (pid $(cat .solver.pid)) -> logs: just solve-logs"

# Stop the detached solver watcher.
solve-stop:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ ! -f .solver.pid ]; then echo "not running"; exit 0; fi
    pid="$(cat .solver.pid)"
    kill "$pid" 2>/dev/null && echo "stopped (pid $pid)" || echo "pid $pid not alive"
    rm -f .solver.pid

# Show whether the detached solver watcher is running.
solve-status:
    #!/usr/bin/env bash
    if [ -f .solver.pid ] && kill -0 "$(cat .solver.pid)" 2>/dev/null; then
        echo "running (pid $(cat .solver.pid))"
    else
        echo "not running"
    fi

# Tail the detached solver's log.
solve-logs:
    tail -f .solver.log
