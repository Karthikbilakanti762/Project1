#!/usr/bin/env sh
# macOS / Linux launcher. Same as `python3 run.py ...`. Extra words are passed on:
#   ./run.sh            ./run.sh check      ./run.sh login      ./run.sh replay 2026-11-06
# First time only:  chmod +x run.sh
cd "$(dirname "$0")" || exit 1
if command -v python3 >/dev/null 2>&1; then PY=python3
elif command -v python >/dev/null 2>&1; then PY=python
else echo "Python 3.8+ is needed: https://www.python.org/downloads/"; exit 1
fi
exec "$PY" run.py "$@"
