#!/bin/bash
# =============================================================================
# KlipperXL-Plus - build the Dwarf (STM32G070) firmware on the printer's Pi,
# WITHOUT leaving the Klipper repo dirty
# Copyright (C) 2026 Richard Crook
#
# Same rule as KlipperXL's scripts/build_klipperxl.sh: the Dwarf patches edit
# TRACKED Klipper sources, so they are applied only for the length of the
# build and taken out again by a trap - on success, failure or interrupt.
# The file list comes from the patches themselves (git apply --numstat), and
# they are checked (git apply --check) before anything is touched.  The repo
# is clean at rest, so Moonraker reports Klipper clean and updates apply.
#
#   0002 RS485 addressing      0003 Prusa puppy clock (12 MHz HSE, 56 MHz)
#   0004 TX gate / RX off      0005 shutdown idle
#   0006 TX stop in flight     0007 serial_irq transmit buffer 192
#   0008 serial_irq transmit buffer 512 (16-bit positions)
#   0009 keep + report the Prusa bootloader address (RAM 0x20000000)
#   0010 lock to that address at boot (no accept-all window)
#
# The build gets its own .config and output folder, so it never touches a
# ~/klipper/.config or ~/klipper/out/ that already exists.
#
# Refuses to produce a binary that is not:
#   app at 0x08002000 (behind the Prusa Dwarf bootloader - NEVER 0x08000000)
#   CONFIG_CLOCK_FREQ 56000000
#
# Firmware ID (owner 10/10, klippy/extras/xl_fw_check.py): a hash of the
# patches + dwarf.config is compiled in as the constant KXLPLUS_FW (a
# generated src/kxlplus_fw_id.c + one src/Makefile line, both taken out by
# the trap) and written to BUILD_DIR/fw_id.  Klipper's version is not in it.
#
# Usage:  bash build_dwarf_pi.sh [BUILD_DIR]        (default ~/kxlplus-build/dwarf)
#         KLIPPER_DIR=~/klipper bash build_dwarf_pi.sh
# Output: BUILD_DIR/klipper-dwarf.bin, BUILD_DIR/klipper.dict, BUILD_DIR/fw_id
# =============================================================================
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
KLIPPER_DIR="${KLIPPER_DIR:-$HOME/klipper}"
BUILD=${1:-$HOME/kxlplus-build/dwarf}
PATCHES="0002-stm32f0_serial-rs485-addressing.patch
0003-stm32g0-prusa-puppy-clock.patch
0004-stm32f0_serial-txgate-rxoff.patch
0005-stm32f0_serial-rs485-shutdown-idle.patch
0006-stm32f0_serial-rs485-tx-stop-inflight.patch
0007-serial_irq-transmit-buf-192.patch
0008-serial_irq-transmit-buf-512.patch
0009-stm32g0-prusa-app-args-boot-address.patch
0010-stm32g0-dwarf-self-address-at-boot.patch"

cd "$KLIPPER_DIR" || { echo "ERROR: no such directory: $KLIPPER_DIR"; exit 1; }
[ -f src/stm32/Makefile ] || { echo "ERROR: $KLIPPER_DIR is not a Klipper tree"; exit 1; }
for p in $PATCHES dwarf.config; do
    [ -f "$HERE/$p" ] || { echo "ERROR: missing $HERE/$p"; exit 1; }
done
FW_ID="dwarf-$(cd "$HERE" && cat $PATCHES dwarf.config | tr -d '\r' \
    | sha256sum | cut -c1-12)"
ID_SRC=src/kxlplus_fw_id.c

# ---- already built? Same KlipperXL-Plus firmware ID + Klipper commit + compiler
# = the same firmware (only the build time differs) - owner 10/10: a re-run
# of the installer no longer rebuilds what it built minutes ago
BUILD_KEY="$FW_ID $(git rev-parse HEAD) gcc-$(arm-none-eabi-gcc -dumpversion)"
if [ "${KXL_REBUILD:-0}" != "1" ] && [ -f "$BUILD/klipper-dwarf.bin" ] \
        && [ "$(cat "$BUILD/build_key" 2>/dev/null)" = "$BUILD_KEY" ]; then
    echo "UP TO DATE: $BUILD/klipper-dwarf.bin ($FW_ID, same Klipper + compiler)"
    echo "    not rebuilt (KXL_REBUILD=1 or the installer's --rebuild forces it)"
    exit 0
fi

# ---- the files the patches touch, from the patches themselves (+ the
# src/Makefile line that builds the firmware ID)
FILES=$( (for p in $PATCHES; do git apply --numstat "$HERE/$p" | awk '{print $3}'; done
          echo src/Makefile) | sort -u)
echo "patches touch:"; echo "$FILES" | sed 's/^/  /'

# ---- refuse a tree that already has edits or leftovers we did not make
for f in $FILES; do
    if ! git diff --quiet -- "$f"; then
        echo "ERROR: $f is already modified."
        echo "       Inspect it, then restore with: git checkout -- $f"
        exit 1
    fi
done
if [ -e "$ID_SRC" ]; then
    echo "ERROR: $ID_SRC already exists (left by an interrupted build?)."
    echo "       Remove it, then run this again."
    exit 1
fi

# ---- ALWAYS put them back, even on a failed or interrupted build
restore_tree() {
    cd "$KLIPPER_DIR" 2>/dev/null || return
    for f in $FILES; do git checkout -- "$f" 2>/dev/null || true; done
    rm -f "$ID_SRC"
    echo "  Klipper sources restored - repo is clean, Klipper updates will work"
}
trap restore_tree EXIT INT TERM

echo
echo "=== patching the sources (temporarily) ==="
for p in $PATCHES; do
    git apply --check "$HERE/$p" || {
        echo "ERROR: $p does not apply to this Klipper version"
        echo "       ($(git describe --tags --always)) - KlipperXL-Plus must be updated"
        echo "       for it before the Dwarf firmware can be rebuilt."
        exit 1
    }
    git apply "$HERE/$p"; echo "  applied $p"
done
cat > "$ID_SRC" <<EOF
// KlipperXL-Plus firmware ID - generated by build_dwarf_pi.sh, removed after it
#include "command.h" // DECL_CONSTANT_STR
DECL_CONSTANT_STR("KXLPLUS_FW", "$FW_ID");
EOF
echo 'src-y += kxlplus_fw_id.c' >> src/Makefile
echo "  + firmware ID $FW_ID"

echo
echo "=== configuring ($BUILD) ==="
rm -rf "$BUILD"; mkdir -p "$BUILD"
cp "$HERE/dwarf.config" "$BUILD/.config"
MK="make KCONFIG_CONFIG=$BUILD/.config OUT=$BUILD/out/"
$MK olddefconfig >/dev/null
grep -q '^CONFIG_FLASH_APPLICATION_ADDRESS=0x8002000$' "$BUILD/.config" \
    || { echo "REFUSING: application address is not 0x8002000 (the Prusa bootloader must stay)"; exit 1; }
grep -q '^CONFIG_STM32_FLASH_START_0000=y' "$BUILD/.config" \
    && { echo "REFUSING: no-bootloader build (0x08000000)"; exit 1; }
grep -q '^CONFIG_CLOCK_FREQ=56000000$' "$BUILD/.config" \
    || { echo "REFUSING: clock is not 56 MHz"; exit 1; }

echo
echo "=== building ==="
$MK -j"$(nproc)"
ENTRY=$(arm-none-eabi-readelf -h "$BUILD/out/klipper.elf" | awk '/Entry point/{print $4}')
[ "$ENTRY" = "0x8002000" ] || { echo "REFUSING: ELF entry $ENTRY"; exit 1; }

python3 - "$BUILD/out/klipper.dict" "$FW_ID" <<'PY'
import json, sys
got = json.load(open(sys.argv[1])).get('config', {}).get('KXLPLUS_FW')
if got != sys.argv[2]:
    print("REFUSING: firmware ID %r in the build, expected %r" % (got, sys.argv[2]))
    sys.exit(1)
print("  firmware ID %s compiled in" % (got,))
PY

cp "$BUILD/out/klipper.bin" "$BUILD/klipper-dwarf.bin"
cp "$BUILD/out/klipper.dict" "$BUILD/klipper.dict"
echo "$FW_ID" > "$BUILD/fw_id"
echo "$BUILD_KEY" > "$BUILD/build_key"
echo
echo "OK: $BUILD/klipper-dwarf.bin $(stat -c %s "$BUILD/klipper-dwarf.bin") bytes," \
     "entry $ENTRY, md5 $(md5sum < "$BUILD/klipper-dwarf.bin" | cut -c1-8)"
echo "    Flash each tool over the bus with scripts/xl_flash_dwarf.py (Klipper stopped)."
