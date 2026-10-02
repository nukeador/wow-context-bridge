#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
SOURCE="$ROOT/reader/native_pipewire_probe.c"
OUTPUT="$ROOT/reader/native_pipewire_probe"
CC=${CC:-cc}

if ! command -v "$CC" >/dev/null 2>&1; then
  echo "C compiler '$CC' not found. Install a Linux C toolchain, then retry." >&2
  exit 2
fi

if command -v pkg-config >/dev/null 2>&1 && pkg-config --exists libpipewire-0.3; then
  # PipeWire pkg-config includes both PipeWire and SPA headers and linker flags.
  # shellcheck disable=SC2046
  "$CC" -std=c11 -O2 -Wall -Wextra \
    $(pkg-config --cflags libpipewire-0.3) \
    "$SOURCE" -o "$OUTPUT" \
    $(pkg-config --libs libpipewire-0.3)
else
  "$CC" -std=c11 -O2 -Wall -Wextra \
    -I/usr/include/pipewire-0.3 -I/usr/include/spa-0.2 \
    "$SOURCE" -o "$OUTPUT" -lpipewire-0.3
fi

echo "Built $OUTPUT"
