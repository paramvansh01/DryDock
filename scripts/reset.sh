#!/bin/sh
# One command, under 10 seconds. See scripts/reset.py.
cd "$(dirname "$0")/.." && exec uv run python scripts/reset.py "$@"
