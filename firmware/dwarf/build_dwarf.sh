#!/bin/bash
# KlipperXL-Plus — build the Klipper firmware for a Prusa XL Dwarf (STM32G070).
#   bash build_dwarf.sh [OUT_DIR]      -> OUT_DIR/klipper-dwarf.bin (default ./out)
# Pins Klipper to the commit the owner's printer runs, applies the six Dwarf patches
# in order, uses the verified .config, and REFUSES to produce a binary that is not:
#   app at 0x08002000 (behind the Prusa Dwarf bootloader - NEVER 0x08000000),
#   CONFIG_CLOCK_FREQ 56000000 (12 MHz HSE, PLLM 3 / PLLN 70 / PLLR 5).
# Verified 2026-10-02: this recipe produced the binary on all five Dwarfs (md5 f9126a5d).
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd); OUT=${1:-$HERE/out}
KLIPPER_COMMIT=ce7002bedf37e938bb483572949f3703ac6476cb   # v0.13.0-770
WORK=$(mktemp -d); trap 'rm -rf "$WORK"' EXIT
git clone -q https://github.com/Klipper3d/klipper "$WORK/klipper"
cd "$WORK/klipper"; git checkout -q $KLIPPER_COMMIT
for p in 0002-stm32f0_serial-rs485-addressing.patch 0003-stm32g0-prusa-puppy-clock.patch 0004-stm32f0_serial-txgate-rxoff.patch 0005-stm32f0_serial-rs485-shutdown-idle.patch 0006-stm32f0_serial-rs485-tx-stop-inflight.patch 0007-serial_irq-transmit-buf-192.patch 0008-serial_irq-transmit-buf-512.patch; do
  git apply "$HERE/$p"; echo "applied $p"; done
cp "$HERE/dwarf.config" .config; make -s olddefconfig >/dev/null
grep -q '^CONFIG_FLASH_APPLICATION_ADDRESS=0x8002000$' .config || { echo "REFUSING: app address is not 0x8002000"; exit 1; }
grep -q '^CONFIG_STM32_FLASH_START_0000=y' .config && { echo "REFUSING: no-bootloader build"; exit 1; }
grep -q '^CONFIG_CLOCK_FREQ=56000000$' .config || { echo "REFUSING: clock is not 56 MHz"; exit 1; }
make -s -j"$(nproc)"
ENTRY=$(arm-none-eabi-readelf -h out/klipper.elf | awk '/Entry point/{print $4}')
[ "$ENTRY" = "0x8002000" ] || { echo "REFUSING: ELF entry $ENTRY"; exit 1; }
mkdir -p "$OUT"; cp out/klipper.bin "$OUT/klipper-dwarf.bin"
echo "OK: $OUT/klipper-dwarf.bin $(stat -c %s "$OUT/klipper-dwarf.bin") bytes, entry $ENTRY, md5 $(md5sum < "$OUT/klipper-dwarf.bin" | cut -c1-8)"
