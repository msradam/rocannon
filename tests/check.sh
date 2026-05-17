#!/bin/sh
# Pre-commit code quality chain.
#
# Usage:
#   ./tests/check.sh                 # check-only (CI mode)
#   ./tests/check.sh --fix           # auto-fix formatting + lint where possible
#   ./tests/check.sh [dirs...]       # override which directories to check
#   ./tests/check.sh --fix [dirs...] # both
set -e

fix=0
if [ "$1" = "--fix" ]; then
    fix=1
    shift
fi

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
if [ $fix -eq 1 ]; then
    echo "[$step/$total] ruff format $dirs"
    $run ruff format $dirs
else
    echo "[$step/$total] ruff format --check $dirs"
    $run ruff format --check $dirs
fi

step=$((step + 1))
if [ $fix -eq 1 ]; then
    echo "[$step/$total] ruff check --fix $dirs"
    $run ruff check --fix $dirs
else
    echo "[$step/$total] ruff check $dirs"
    $run ruff check $dirs
fi

step=$((step + 1))
echo "[$step/$total] mypy $src_dirs"
$run mypy $src_dirs

step=$((step + 1))
echo "[$step/$total] pytest -x -q"
$run pytest -x -q

echo "All checks passed."
