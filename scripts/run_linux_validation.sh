#!/bin/sh
set -eu

SOURCE_DIR="${1:-/workspace}"
if [ ! -f "$SOURCE_DIR/pyproject.toml" ]; then
    echo "Project source is missing at $SOURCE_DIR." >&2
    exit 2
fi

TEMPORARY_ROOT="$(mktemp -d)"
trap 'rm -rf "$TEMPORARY_ROOT"' EXIT HUP INT TERM
PROJECT_DIR="$TEMPORARY_ROOT/project"
mkdir -p "$PROJECT_DIR"

tar -C "$SOURCE_DIR" \
    --exclude=.build-validation-* \
    --exclude=.git \
    --exclude=.pytest_cache \
    --exclude=.venv \
    --exclude=__pycache__ \
    --exclude='*.egg-info' \
    --exclude='*.pyc' \
    --exclude=app-data \
    --exclude=build \
    --exclude=dist \
    -cf - . | tar -C "$PROJECT_DIR" -xf -

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
    libdbus-1-3 \
    libegl1 \
    libfontconfig1 \
    libfreetype6 \
    libgl1 \
    libx11-6 \
    libx11-xcb1 \
    libxkbcommon0 \
    libxkbcommon-x11-0 \
    libxcb-cursor0
rm -rf /var/lib/apt/lists/*

export LIBGL_ALWAYS_SOFTWARE=1
export QT_QPA_PLATFORM=offscreen
export QT_QUICK_BACKEND=software

cd "$PROJECT_DIR"
python -m venv "$TEMPORARY_ROOT/environment"
. "$TEMPORARY_ROOT/environment/bin/activate"
python -m pip install --upgrade pip
python -m pip install --prefer-binary -e '.[dev]'
python scripts/verify_build.py --artifact-dir dist
