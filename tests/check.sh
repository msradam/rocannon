#!/bin/sh
# Pre-commit code quality chain.
# Usage: ./tests/check.sh [src_dirs...] (defaults: auto-detect src/ tests/)
set -e

if [ $# -gt 0 ]; then
    dirs="$*"
    src_dirs="$*"
else
    dirs=""
    src_dirs=""
    for d in src/ tests/; do
        [ -d "$d" ] && dirs="$dirs $d"
    done
    [ -d "src/" ] && src_dirs="src/"
    dirs="${dirs# }"
    src_dirs="${src_dirs# }"
fi

if [ -z "$dirs" ]; then
    echo "No Python directories found." >&2
    exit 1
fi

if command -v uv >/dev/null 2>&1; then
    run="uv run"
else
    run=""
fi

step=0
total=4

step=$((step + 1))
echo "[$step/$total] ruff format --check $dirs"
$run ruff format --check $dirs

step=$((step + 1))
echo "[$step/$total] ruff check $dirs"
$run ruff check $dirs

step=$((step + 1))
echo "[$step/$total] mypy $src_dirs"
$run mypy $src_dirs

step=$((step + 1))
echo "[$step/$total] pytest -x -q"
$run pytest -x -q

echo "All checks passed."
