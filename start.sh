#!/bin/bash
# ---------------------------------------------------------------------------
# start.sh — start the Beamline Watchdog under Gunicorn
#
# Edit the two variables below to match your setup, then make executable:
#   chmod +x start.sh
#
# Usage:
#   ./start.sh           start (exits with error if port is already in use)
#   ./start.sh --force   kill whatever holds the port, then start
# ---------------------------------------------------------------------------

# ---- configure these for your machine ----
CONDA_ENV="watchdog"       # conda environment name (leave blank to skip conda)
CONDA_BASE=""              # set this if conda is not on PATH (e.g. /opt/anaconda3)
                           # leave blank to auto-detect via 'conda info --base'
VENV_PATH="venv"           # path to virtualenv relative to project dir (used if conda not found)
# ------------------------------------------

cd "$(dirname "$0")"       # always run from the project directory

# ---- read PORT from .env, default 5001 ----
PORT=5001
if [ -f .env ]; then
    _p=$(grep -E '^PORT=' .env | head -1 | cut -d= -f2 | tr -d ' \r')
    [ -n "$_p" ] && PORT=$_p
fi

# ---- port check ----
port_pids() { lsof -ti :"$PORT" 2>/dev/null; }

PIDS=$(port_pids)
if [ -n "$PIDS" ]; then
    if [ "$1" = "--force" ]; then
        echo "Port $PORT is in use by PID(s): $PIDS — killing..."
        echo "$PIDS" | xargs kill -9
        sleep 1
        if [ -n "$(port_pids)" ]; then
            echo "ERROR: could not free port $PORT. Aborting." >&2
            exit 1
        fi
        echo "Port $PORT is now free."
    else
        echo "ERROR: port $PORT is already in use:" >&2
        lsof -i :"$PORT" >&2
        echo "" >&2
        echo "To kill the existing process and restart:  $0 --force" >&2
        exit 1
    fi
fi
echo "Port $PORT is free."

# ---- activate environment ----
ACTIVATED=0

if [ -n "$CONDA_ENV" ]; then
    # Use hardcoded CONDA_BASE if set, otherwise auto-detect via 'conda info --base'
    if [ -z "$CONDA_BASE" ]; then
        CONDA_BASE=$(conda info --base 2>/dev/null)
    fi
    if [ -n "$CONDA_BASE" ] && [ -f "$CONDA_BASE/etc/profile.d/conda.sh" ]; then
        source "$CONDA_BASE/etc/profile.d/conda.sh"
        if conda activate "$CONDA_ENV" 2>/dev/null; then
            echo "Activated conda environment: $CONDA_ENV"
            ACTIVATED=1
        else
            echo "WARNING: conda env '$CONDA_ENV' not found — trying venv..." >&2
        fi
    else
        echo "WARNING: conda not found at '$CONDA_BASE' — trying venv..." >&2
    fi
fi

if [ "$ACTIVATED" -eq 0 ] && [ -f "$VENV_PATH/bin/activate" ]; then
    source "$VENV_PATH/bin/activate"
    echo "Activated virtualenv: $VENV_PATH"
    ACTIVATED=1
fi

if [ "$ACTIVATED" -eq 0 ]; then
    echo "WARNING: no conda env or venv activated — using system/current PATH." >&2
fi

# ---- verify gunicorn is available ----
if ! command -v gunicorn &>/dev/null; then
    echo "ERROR: gunicorn not found. Install it or check your environment." >&2
    exit 1
fi

# ---- start ----
mkdir -p logs
echo "Starting Beamline Watchdog on port $PORT..."
exec gunicorn -c gunicorn.conf.py wsgi:app
