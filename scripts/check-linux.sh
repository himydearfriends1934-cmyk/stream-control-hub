#!/usr/bin/env sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT_DIR"

PYTHON=${PYTHON:-python3}

echo "Checking Python syntax..."
"$PYTHON" -m compileall -q stream_control_hub tests

echo "Checking POSIX shell syntax..."
for script in scripts/*.sh; do
  sh -n "$script"
done

echo "Running test suite..."
"$PYTHON" -m unittest discover -s tests -v

echo "Linux checks passed."

