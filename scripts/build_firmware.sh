#!/bin/sh
set -eu
STACKCHAN_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
export PLATFORMIO_CORE_DIR="$STACKCHAN_ROOT/.platformio"
if [ -x "$STACKCHAN_ROOT/.venv/bin/pio" ]; then
  STACKCHAN_PIO="$STACKCHAN_ROOT/.venv/bin/pio"
elif command -v pio >/dev/null 2>&1; then
  STACKCHAN_PIO=$(command -v pio)
elif [ -x "$STACKCHAN_ROOT/../heartrate-monitor/.venv/bin/pio" ]; then
  STACKCHAN_PIO="$STACKCHAN_ROOT/../heartrate-monitor/.venv/bin/pio"
else
  echo 'PlatformIO ontbreekt. Installeer platformio==6.2.0 in de project-venv.' >&2
  exit 1
fi
exec "$STACKCHAN_PIO" run --project-dir "$STACKCHAN_ROOT/firmware" "$@"
