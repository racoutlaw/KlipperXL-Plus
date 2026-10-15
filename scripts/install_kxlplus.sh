#!/bin/bash
# =============================================================================
# KlipperXL-Plus - installer for a Prusa XL (1-5 tools), run on the printer's Pi
# Copyright (C) 2026 Richard Crook
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Started by scripts/bootstrap_kxlplus.sh on a MainsailOS Pi (or on any Pi
# with Klipper + Moonraker + Mainsail/Fluidd and KlipperXL-Plus cloned):
#   bash ~/KlipperXL-Plus/scripts/install_kxlplus.sh
# Run it again any time (after a KlipperXL-Plus update, or after flashing the
# XLBuddy) - it never overwrites your printer.cfg or your calibrations.
#
# Two stages, because the Pi can only talk to the tools once the XLBuddy runs
# KlipperXL-Plus firmware:
#   Stage 1  modules into Klipper (untracked + .git/info/exclude, so Klipper
#            stays stock and updates normally), build the XLBuddy .bbf and the
#            Dwarf firmware (Klipper sources restored after each build), and -
#            if the XLBuddy does not run KlipperXL-Plus firmware yet - the USB-stick
#            step.  Nothing is written to the printer in stage 1.
#   Stage 2  find the tools (read-only scan), confirm them, flash each Dwarf
#            (backup + read-back verify), generate the tool configs for exactly
#            those tools, create printer.cfg only if you have none, start
#            Klipper and check every board.
#
# Updates (owner 10/10, option A - klippy/extras/xl_fw_check.py): every build
# carries a KlipperXL-Plus firmware ID.  An XLBuddy on another ID goes first (the
# USB-stick step again); a tool that Klipper reports on THIS build's ID is not
# flashed again; a tool that is unknown (Klipper not running) is flashed.
#
# Options:  --yes              do not ask (accept the detected tools)
#           --skip-build       use the firmware already in ~/kxlplus-build
#           --no-flash-tools   do not flash the Dwarfs (scan + config only)
#           --flash-all        flash every tool, also one already on this build
#           --rebuild          build the firmware even if the last build is current
#           --config-dir DIR   default ~/printer_data/config
#
# Layout (docs/RELEASE_CONFIG.md): kxlplus/xl_base.cfg + kxlplus/tools/tN.cfg
# are this installer's and are replaced on every run; printer.cfg is yours.
# =============================================================================
set -euo pipefail

KXL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KLIPPER_DIR="${KLIPPER_DIR:-$HOME/klipper}"
PY="$HOME/klippy-env/bin/python"
CONFIG_DIR="$HOME/printer_data/config"
BUILD="$HOME/kxlplus-build"
YES=0; SKIP_BUILD=0; FLASH_TOOLS=1; FLASH_ALL=0
while [ $# -gt 0 ]; do
    case "$1" in
        --yes) YES=1 ;;
        --skip-build) SKIP_BUILD=1 ;;
        --no-flash-tools) FLASH_TOOLS=0 ;;
        --flash-all) FLASH_ALL=1 ;;
        --rebuild) export KXL_REBUILD=1 ;;
        --config-dir) CONFIG_DIR="$2"; shift ;;
        *) echo "unknown option: $1"; exit 1 ;;
    esac
    shift
done
# Web mode (owner 10/10, scripts/web_installer.py runs this with KXL_WEB=1):
# questions come as "@@ASK {json}" lines and are answered on stdin, and
# "@@..." markers tell the page where the install is.  Terminal mode as before.
WEB="${KXL_WEB:-0}"
mark() { if [ "$WEB" = "1" ]; then echo "@@$*"; fi; }

# The modules that make up KlipperXL-Plus (test diagnostics are not shipped)
MODULES="serial_bridge.py xl_puppies.py xl_leds.py xl_side_sensors.py
xl_tool_fsensor.py xl_dock_sensors.py xl_load_cell.py xl_load_cell_probe.py
xl_toolchanger.py xl_filament.py xl_wizards.py xl_pa_tuner.py xl_z_memory.py
xl_status_led.py xl_tool_buttons.py xl_tool_offsets.py xl_fw_check.py"

echo "=========================================="
echo " KlipperXL-Plus installer - Prusa XL, 1-5 tools"
echo "=========================================="

# ---- prerequisites (released KlipperXL list + ecdsa for the .bbf) ----------
MISSING=""
[ -d "$KLIPPER_DIR" ] || MISSING="$MISSING\n  - $KLIPPER_DIR (Klipper not installed)"
[ -x "$PY" ] || MISSING="$MISSING\n  - ~/klippy-env (Klipper Python environment)"
command -v arm-none-eabi-gcc >/dev/null || MISSING="$MISSING\n  - arm-none-eabi-gcc (sudo apt install gcc-arm-none-eabi)"
command -v make >/dev/null || MISSING="$MISSING\n  - make / build tools (sudo apt install build-essential)"
[ -d "$CONFIG_DIR" ] || MISSING="$MISSING\n  - $CONFIG_DIR (Klipper config folder)"
if [ -n "$MISSING" ]; then
    echo "ERROR: not ready. Missing:"; echo -e "$MISSING"
    echo; echo "Start from MainsailOS and run scripts/bootstrap_kxlplus.sh - it"
    echo "installs what is missing and then runs this installer."
    exit 1
fi
if ! "$PY" -c "import ecdsa" 2>/dev/null; then
    echo "Installing ecdsa into klippy-env (needed to pack the XLBuddy .bbf)..."
    "$PY" -m pip install -q ecdsa
fi

# ---- [1] modules into Klipper, untracked + excluded ------------------------
echo; echo "[1] KlipperXL-Plus modules -> $KLIPPER_DIR/klippy/extras"
EXCLUDE="$KLIPPER_DIR/.git/info/exclude"
for m in $MODULES; do
    # a LINK to KlipperXL-Plus's own file (owner 10/10): an update of KlipperXL-Plus
    # from Mainsail's update list (step [5b]) changes the module itself
    ln -sfn "$KXL_DIR/klippy/extras/$m" "$KLIPPER_DIR/klippy/extras/$m"
    line="klippy/extras/$m"
    if [ -f "$EXCLUDE" ] && ! grep -qxF "$line" "$EXCLUDE"; then
        echo "$line" >> "$EXCLUDE"
    fi
done
echo "  linked: $(echo $MODULES | wc -w) modules (git ignores them; Klipper stays stock)"
if [ -n "$(cd "$KLIPPER_DIR" && git status --short | grep -v '^??')" ]; then
    echo "  NOTE: Klipper has local changes to its own files:"
    (cd "$KLIPPER_DIR" && git status --short | grep -v '^??' | sed 's/^/    /')
    echo "  KlipperXL-Plus does not need any - Moonraker will call Klipper 'dirty'."
fi

# ---- [1b] pressure advance analyser, in its OWN venv (released step 4) -----
# scipy must never be able to disturb klippy-env; PA is an extra, so nothing
# here may abort the install (released: python3-venv is missing on Raspberry
# Pi OS Lite).
echo; echo "[1b] Pressure advance analyser -> ~/pa_analysis (venv ~/pa_analysis_env)"
PA_OK=1
# owner 10/10: a re-run leaves an up-to-date analyser alone (it used to be
# moved aside and numpy/scipy re-checked every time).  The analyser writes
# its results to /tmp; only Python's __pycache__ appears in ~/pa_analysis.
PA_SAME=0
if [ -d "$HOME/pa_analysis" ] && [ -z "$(diff -rq -x __pycache__ "$KXL_DIR/pa_analysis" \
        "$HOME/pa_analysis" 2>&1 | grep -v "^Only in $HOME/pa_analysis")" ]; then
    PA_SAME=1
    echo "  ~/pa_analysis is up to date - left as it is"
else
    if [ -d "$HOME/pa_analysis" ]; then
        rm -rf "$HOME/pa_analysis.old"
        mv "$HOME/pa_analysis" "$HOME/pa_analysis.old"
        echo "  existing ~/pa_analysis moved aside to ~/pa_analysis.old"
    fi
    cp -r "$KXL_DIR/pa_analysis" "$HOME/pa_analysis"
fi
if [ ! -x "$HOME/pa_analysis_env/bin/python" ]; then
    python3 -m venv "$HOME/pa_analysis_env" 2>/dev/null || PA_OK=0
fi
if [ "$PA_OK" = "1" ] && [ "$PA_SAME" = "1" ] \
        && "$HOME/pa_analysis_env/bin/python" -c "import numpy,scipy" 2>/dev/null; then
    "$HOME/pa_analysis_env/bin/python" -c "import numpy,scipy;print('  OK - numpy',numpy.__version__,'/ scipy',scipy.__version__,'(already installed)')"
elif [ "$PA_OK" = "1" ]; then
    echo "  numpy + scipy (can take several minutes on a Pi)..."
    if "$HOME/pa_analysis_env/bin/pip" install -q -r "$HOME/pa_analysis/requirements.txt"; then
        "$HOME/pa_analysis_env/bin/python" -c             "import numpy,scipy;print('  OK - numpy',numpy.__version__,'/ scipy',scipy.__version__)"
    else
        PA_OK=0
    fi
fi
if [ "$PA_OK" = "0" ]; then
    echo "  WARNING: the analyser is not set up - PA_TUNE_Tn will record but not"
    echo "  analyse until you run:"
    echo "    sudo apt install -y python3-venv"
    echo "    python3 -m venv ~/pa_analysis_env"
    echo "    ~/pa_analysis_env/bin/pip install -r ~/pa_analysis/requirements.txt"
fi

# ---- [2] firmware builds (Klipper sources restored after each) -------------
if [ "$SKIP_BUILD" = "0" ]; then
    # each build script skips itself when its last build has the same
    # firmware ID, Klipper commit and compiler (--rebuild forces it)
    echo; echo "[2] Building the XLBuddy and Dwarf firmware (takes a few minutes)"
    bash "$KXL_DIR/firmware/xlbuddy/build_xlbuddy_pi.sh" "$BUILD/xlbuddy" \
        > "$BUILD.xlbuddy.log" 2>&1 \
        || { echo "XLBuddy build FAILED - see $BUILD.xlbuddy.log"; exit 1; }
    tail -4 "$BUILD.xlbuddy.log" | sed 's/^/  /'
    bash "$KXL_DIR/firmware/dwarf/build_dwarf_pi.sh" "$BUILD/dwarf" \
        > "$BUILD.dwarf.log" 2>&1 \
        || { echo "Dwarf build FAILED - see $BUILD.dwarf.log"; exit 1; }
    tail -3 "$BUILD.dwarf.log" | sed 's/^/  /'
else
    echo; echo "[2] Builds skipped (--skip-build)"
fi
[ -f "$BUILD/xlbuddy/klipper.bbf" ] && [ -f "$BUILD/dwarf/klipper-dwarf.bin" ] \
    || { echo "ERROR: no firmware in $BUILD - run without --skip-build"; exit 1; }

# ---- does the XLBuddy run KlipperXL-Plus firmware? -----------------------------
FLASHER="$KXL_DIR/scripts/xl_flash_dwarf.py"
KLIPPER_WAS_ACTIVE=0
systemctl is-active --quiet klipper && KLIPPER_WAS_ACTIVE=1
stop_klipper() { [ "$KLIPPER_WAS_ACTIVE" = "1" ] && sudo systemctl stop klipper || true; }
start_klipper() { sudo systemctl start klipper; }
usb_stick_step() {
    mark STEP xlbuddy
    echo
    echo "=== Stage 1 done. Next: put KlipperXL-Plus firmware on the XLBuddy ==="
    # owner 10/08: the installer puts the .bbf on a USB stick plugged into
    # the Pi (scripts/xl_usb_stick.py: removable FAT32 USB only, never the
    # Pi's own disk, shown and confirmed, old .bbf files renamed .old -
    # nothing deleted or formatted, copy read back).  --yes never writes to
    # a removable drive unasked: then it is done by hand.
    RC=1
    if [ "$YES" = "0" ]; then
        set +e
        "$PY" "$KXL_DIR/scripts/xl_usb_stick.py" "$BUILD/xlbuddy/klipper.bbf"
        RC=$?
        set -e
    fi
    if [ "$RC" != "0" ]; then
        echo "  Copy $BUILD/xlbuddy/klipper.bbf to the root of a FAT32 USB stick"
        echo "  (rename any other .bbf on it, so the bootloader sees only this one)."
    fi
    # the printer steps of KlipperXL's INSTALLATION_GUIDE 6.2
    echo "  Then, at the printer:"
    echo "  1. Plug the stick into the printer's front USB port and power-cycle it."
    echo "  2. When the Prusa logo appears, press the knob 2-3 times - this opens"
    echo "     the bootloader's firmware update menu - and confirm klipper.bbf."
    echo "     (It goes in at 0x08020200 - the Prusa bootloader stays, so a stock"
    echo "     Prusa .bbf can always go back on.)"
    echo "  3. If it asks about the signature, press Ignore (unsigned firmware"
    echo "     needs the broken appendix)."
    echo "  4. The progress bar stops at about 50 % - that is normal (Klipper has"
    echo "     taken over and does not drive the screen)."
    echo "  5. Power the printer OFF, pull the stick out, power it ON."
    echo "  (Bootloader does not see the stick: press reset, then the knob 2-3 times.)"
    echo "  6. Run this installer again for stage 2 (tools + config):"
    echo "       bash $KXL_DIR/scripts/install_kxlplus.sh"
    # an update: the printer keeps working on its old firmware meanwhile
    [ "$KLIPPER_WAS_ACTIVE" = "1" ] && start_klipper
    mark STAGE1_DONE "$([ "$RC" = "0" ] && echo stick || echo by-hand)"
    exit 0
}

# ---- the firmware IDs of this build (xl_fw_check.py) ------------------------
NEW_XL=$(cat "$BUILD/xlbuddy/fw_id" 2>/dev/null || true)
NEW_DWARF=$(cat "$BUILD/dwarf/fw_id" 2>/dev/null || true)
[ -n "$NEW_XL" ] && [ -n "$NEW_DWARF" ] \
    || echo "NOTE: this build has no firmware ID (--skip-build of an older build) - versions not checked"
# tools that already run THIS build, asked while Klipper still runs (unknown =
# flashed, as before)
SAME_TOOLS=""
if [ "$FLASH_ALL" = "0" ] && [ -n "$NEW_DWARF" ] && [ "$KLIPPER_WAS_ACTIVE" = "1" ]; then
    SAME_TOOLS=$(curl -s "localhost:7125/printer/objects/query?xl_fw_check" \
        | "$PY" -c 'import sys, json
b = json.load(sys.stdin)["result"]["status"]["xl_fw_check"]["boards"]
print(" ".join(k[1:] for k, v in sorted(b.items())
               if k.startswith("T") and v.get("fw") == sys.argv[1]))' \
        "$NEW_DWARF" 2>/dev/null || true)
fi

echo; echo "[3] Looking for the XLBuddy (Klipper is stopped while the bus is used)"
stop_klipper
set +e
SCAN=$("$PY" "$FLASHER" --scan 2>&1)
RC=$?
set -e
if echo "$SCAN" | grep -q "Expected exactly one XLBuddy"; then
    echo "  no XLBuddy running Klipper firmware found."
    usb_stick_step
fi
if echo "$SCAN" | grep -q "NOT_KXLPLUS_FW"; then
    echo "  the XLBuddy runs other Klipper firmware (not KlipperXL-Plus)."
    usb_stick_step
fi
XL_FW=$(echo "$SCAN" | sed -n 's/^XLBUDDY_FW: //p')
if [ -n "$NEW_XL" ] && [ -n "$XL_FW" ] && [ "$XL_FW" != "$NEW_XL" ]; then
    if [ "$XL_FW" = "none" ]; then
        echo "  the XLBuddy runs KlipperXL-Plus firmware from before the version check."
    else
        echo "  the XLBuddy runs other KlipperXL-Plus firmware ($XL_FW, this build $NEW_XL)."
    fi
    echo "  The XLBuddy goes first - the tools follow when you run this again."
    usb_stick_step
fi
mark STEP tools
echo "$SCAN" | grep -E "^XLBuddy|^T[0-4]|present|not found" | sed 's/^/  /' || true
TOOLS=$(echo "$SCAN" | sed -n 's/^TOOLS: //p')
if [ $RC -ne 0 ] || [ -z "$TOOLS" ]; then
    echo "ERROR: no tools answered the scan."; start_klipper; exit 1
fi
TOOL_LIST=$(echo $TOOLS | tr ' ' ',')
echo "  tools found: T$(echo $TOOLS | sed 's/ /, T/g')"
if [ "$YES" = "0" ]; then
    mark "ASK {\"id\": \"tools\", \"text\": \"Found: T$(echo $TOOLS | sed 's/ /, T/g'). Is that every tool on this printer?\", \"choices\": [[\"y\", \"Yes - that is every tool\"], [\"n\", \"No - one is missing\"]]}"
    read -r -p "  Is that every tool on this printer? [y/N] " ok
    case "$ok" in y|Y|yes) ;; *) echo "Stopped - check the tool cables, then run again."; start_klipper; exit 1 ;; esac
fi

# ---- [4] flash the tools ----------------------------------------------------
FLASH_LIST=""
for t in $TOOLS; do
    case " $SAME_TOOLS " in
        *" $t "*) ;;
        *) FLASH_LIST="$FLASH_LIST $t" ;;
    esac
done
if [ "$FLASH_TOOLS" = "1" ] && [ -z "$FLASH_LIST" ]; then
    echo; echo "[4] Every tool already runs this firmware ($NEW_DWARF) - none flashed"
elif [ "$FLASH_TOOLS" = "1" ]; then
    echo; echo "[4] Flashing the Dwarf firmware (backup + read-back verify per tool)"
    for t in $SAME_TOOLS; do
        echo "  T$t already runs this firmware ($NEW_DWARF) - not flashed"
    done
    args=""; for t in $FLASH_LIST; do args="$args --tool $t"; done
    # shown LIVE, tool by tool (owner 10/10: the captured version sat silent
    # for minutes and looked hung) - python -u + line-buffered grep/sed
    set +e
    "$PY" -u "$FLASHER" $args --bin "$BUILD/dwarf/klipper-dwarf.bin" 2>&1 \
        | grep --line-buffered -E "^T[0-4]|backup|written|verified|FAILED|DONE" \
        | sed -u 's/^/  /'
    FRC=${PIPESTATUS[0]}
    set -e
    if [ $FRC -ne 0 ]; then
        echo "Dwarf flash FAILED (exit $FRC) - stopped here; the tools that"
        echo "verified are fine, a failed one is still in its bootloader."
        start_klipper; exit 1
    fi
else
    echo; echo "[4] Dwarf flashing skipped (--no-flash-tools)"
fi

# ---- [5] config -------------------------------------------------------------
echo; echo "[5] Config in $CONFIG_DIR"
"$PY" "$KXL_DIR/config/tools/kxlplus_config.py" tools --tools "$TOOL_LIST" \
    --out "$CONFIG_DIR/kxlplus" | sed 's/^/  /'
PCFG="$CONFIG_DIR/printer.cfg"
SERIAL=$(echo "$SCAN" | sed -n 's/^XLBuddy: //p')
if [ ! -f "$PCFG" ]; then
    FRONTEND=mainsail; [ -d "$HOME/fluidd" ] && [ ! -d "$HOME/mainsail" ] && FRONTEND=fluidd
    "$PY" "$KXL_DIR/config/tools/kxlplus_config.py" printer-cfg --tools "$TOOL_LIST" \
        --mcu-serial "$SERIAL" --frontend "$FRONTEND" --out "$PCFG" | sed 's/^/  /'
elif ! grep -q "^\[include kxlplus/xl_base.cfg\]" "$PCFG"; then
    NEW="$CONFIG_DIR/printer.cfg.kxlplus-new"
    rm -f "$NEW"
    "$PY" "$KXL_DIR/config/tools/kxlplus_config.py" printer-cfg --tools "$TOOL_LIST" \
        --mcu-serial "$SERIAL" --out "$NEW" >/dev/null
    echo "  Your printer.cfg is not a KlipperXL-Plus one - it was NOT touched."
    echo "  A KlipperXL-Plus printer.cfg for this printer is in:"
    echo "    $NEW"
    echo "  Back up your printer.cfg, then put that file in its place. Your old"
    echo "  calibrations do not carry over automatically (see the checklist below)."
else
    echo "  printer.cfg is yours and already KlipperXL-Plus - left as it is."
    for t in 0 1 2 3 4; do
        inc=$(grep -c "^\[include kxlplus/tools/t$t.cfg\]" "$PCFG" || true)
        have=0; for d in $TOOLS; do [ "$d" = "$t" ] && have=1; done
        [ "$have" = "1" ] && [ "$inc" = "0" ] && echo "  WARNING: T$t was found but printer.cfg does not include kxlplus/tools/t$t.cfg"
        [ "$have" = "0" ] && [ "$inc" != "0" ] && echo "  WARNING: printer.cfg includes kxlplus/tools/t$t.cfg but T$t was not found - Klipper will not start"
    done
fi
# KlipperScreen's Cooldown leaves the modular bed on unless told (only adds)
if [ -d "$HOME/KlipperScreen" ]; then
    "$PY" "$KXL_DIR/config/tools/kxlplus_config.py" klipperscreen \
        --conf "$CONFIG_DIR/KlipperScreen.conf" | sed 's/^/  /'
fi

# ---- [5b] Mainsail: KlipperXL-Plus in the update list, the installer page as a
# service (owner 10/10: after the install, updates come from Mainsail's list;
# a push to the KlipperXL-Plus repo shows up there).  The modules are links
# (step [1]), so an update from the list changes them and restarts Klipper.
# An update that changes configs / firmware / this installer needs the
# installer page: xl_fw_check.py compares the repo with the stamp written
# here and says so in Mainsail; the page is started from Mainsail's service
# control (KlipperXL-Plus-installer, in Moonraker's allowed services).
echo; echo "[5b] Mainsail: KlipperXL-Plus updates + the installer page as a service"
MCONF="$CONFIG_DIR/moonraker.conf"
MINC="[include kxlplus/moonraker-kxlplus.conf]"
ORIGIN=$(git -C "$KXL_DIR" remote get-url origin 2>/dev/null || true)
BRANCH=$(git -C "$KXL_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || true)
if [ -f "$MCONF" ] && [ -n "$ORIGIN" ] && [ -n "$BRANCH" ] && [ "$BRANCH" != "HEAD" ]; then
    cat > "$CONFIG_DIR/kxlplus/moonraker-kxlplus.conf" <<EOF
# KlipperXL-Plus - INSTALLER-OWNED, replaced on every run (install_kxlplus.sh [5b])
# KlipperXL-Plus in Mainsail's update list: an update pulls $KXL_DIR
# and restarts Klipper.
[update_manager KlipperXL-Plus]
type: git_repo
channel: dev
path: $KXL_DIR
origin: $ORIGIN
primary_branch: $BRANCH
managed_services: klipper
EOF
    echo "  wrote $CONFIG_DIR/kxlplus/moonraker-kxlplus.conf ($ORIGIN, $BRANCH)"
    if ! grep -qxF "$MINC" "$MCONF"; then
        cp "$MCONF" "$MCONF.kxlplus-bak"
        printf '\n# KlipperXL-Plus updates (added by the KlipperXL-Plus installer)\n%s\n' "$MINC" >> "$MCONF"
        echo "  moonraker.conf: added $MINC (backup: moonraker.conf.kxlplus-bak)"
    fi
else
    echo "  NOTE: no moonraker.conf or no git branch/origin - update list not set up"
fi
UNIT=/etc/systemd/system/KlipperXL-Plus-installer.service
sudo tee "$UNIT" > /dev/null <<EOF
# KlipperXL-Plus - INSTALLER-OWNED (install_kxlplus.sh [5b]): the installer page,
# started on demand from Mainsail's service control; it stops by itself
[Unit]
Description=KlipperXL-Plus installer page (port 8099)
After=network-online.target

[Service]
Type=simple
User=$(id -un)
WorkingDirectory=$HOME
ExecStart=/usr/bin/python3 $KXL_DIR/scripts/web_installer.py --kxl-dir $KXL_DIR --port 8099
Restart=no

[Install]
WantedBy=multi-user.target
EOF
# Moonraker lists only services systemd has LOADED when it starts
# (machine.py, systemd_dbus: ListUnitsFiltered(["loaded"])), and an idle,
# not-enabled unit gets unloaded - so Moonraker's own unit carries an ORDERING
# hint on it: that keeps it loaded (listed in Mainsail) and never starts it.
# Tested 10/10: listed, still inactive.
sudo mkdir -p /etc/systemd/system/moonraker.service.d
sudo tee /etc/systemd/system/moonraker.service.d/KlipperXL-Plus-installer.conf > /dev/null <<EOF
# KlipperXL-Plus - INSTALLER-OWNED (install_kxlplus.sh [5b]): keeps
# KlipperXL-Plus-installer.service loaded so Moonraker lists it for Mainsail.
# An ordering hint only - it never starts it.
[Unit]
After=KlipperXL-Plus-installer.service
EOF
ASVC="$HOME/printer_data/moonraker.asvc"
# the page's service was called kxlplus-installer before the KlipperXL-Plus
# name (owner 10/10): take the old one out - never stopped (it may be the
# page running this very update; systemd keeps a running unit going)
if [ -e /etc/systemd/system/kxlplus-installer.service ]; then
    sudo rm -f /etc/systemd/system/kxlplus-installer.service \
        /etc/systemd/system/moonraker.service.d/kxlplus-installer.conf
    [ -f "$ASVC" ] && sed -i '/^kxlplus-installer$/d' "$ASVC"
    echo "  old service name kxlplus-installer removed"
fi
sudo systemctl daemon-reload
if [ -f "$ASVC" ] && ! grep -qxF KlipperXL-Plus-installer "$ASVC"; then
    echo KlipperXL-Plus-installer >> "$ASVC"
    echo "  moonraker.asvc: KlipperXL-Plus-installer allowed (Mainsail can start it)"
fi
echo "  service KlipperXL-Plus-installer: $UNIT"
# the KlipperXL-Plus version this install set up (xl_fw_check.py reads it)
printf 'commit=%s\nkxl_dir=%s\n' "$(git -C "$KXL_DIR" rev-parse HEAD 2>/dev/null || echo unknown)" \
    "$KXL_DIR" > "$CONFIG_DIR/kxlplus/.installed"
sudo systemctl restart moonraker
for i in $(seq 1 20); do
    curl -s -o /dev/null localhost:7125/server/info && break
    sleep 1
done

# ---- [5c] the bedlet grid as a camera (owner 10/12) -------------------------
# The modular bed's 16 zone temperatures as a "camera" panel: a Mainsail /
# Fluidd webcam of type iframe showing kxlplus/bedlet_grid.html (released
# KlipperXL's page).  nginx serves it from the config folder with an
# exact-match location, so a Mainsail/Fluidd update (which replaces its web
# root) cannot delete it; Moonraker's file URL would not do - it sends
# "attachment" and the browser downloads the page.  Both only added once.
echo; echo "[5c] Bedlet grid (the 16 bed zone temperatures) as a camera"
BEDLET="$CONFIG_DIR/kxlplus/bedlet_grid.html"
SITE=""
for s in /etc/nginx/sites-available/mainsail /etc/nginx/sites-available/fluidd; do
    if [ -f "$s" ]; then SITE="$s"; break; fi
done
if [ -n "$SITE" ] && [ -f "$BEDLET" ]; then
    # v2 (owner 10/14): "Cache-Control: no-cache" - a browser kept the
    # v1.0.0 page after an update (it looked for the dropped Modular_Bed
    # sensor: "Target: off") - and any spelling of the address (the browser
    # offered bedlet_GRID.HTML from its history) is sent to the page.  A
    # v1.0.0 block is replaced.
    if grep -q "kxlplus-bedlet-v2" "$SITE"; then
        echo "  nginx: /bedlet_grid.html already set up ($SITE)"
    else
        sudo cp "$SITE" "$SITE.kxlplus-bak"
        if sudo python3 - "$SITE" "$BEDLET" <<'PYEOF'
import re, sys
site, page = sys.argv[1], sys.argv[2]
text = open(site).read()
# the v1.0.0 block (exact match only, no cache header) goes
text = re.sub(r'[ \t]*# KlipperXL-Plus \(installer \[5c\]\)[^\n]*\n'
              r'(?:[ \t]*#[^\n]*\n)*'
              r'[ \t]*location = /bedlet_grid\.html \{[^}]*\}\n\n?', '', text)
m = re.search(r'^([ \t]*)location / \{', text, re.M)
if m is None:
    sys.exit("no 'location / {' in " + site)
ind = m.group(1)
block = (ind + "# KlipperXL-Plus (installer [5c], kxlplus-bedlet-v2): the bedlet\n"
         + ind + "# grid page from the config folder (a Mainsail/Fluidd update cannot\n"
         + ind + "# delete it); browsers re-check it, so an update shows at once;\n"
         + ind + "# any other spelling of the address is sent to it\n"
         + ind + "location = /bedlet_grid.html {\n"
         + ind + "    alias %s;\n" % (page,)
         + ind + "    default_type text/html;\n"
         + ind + "    add_header Cache-Control \"no-cache\";\n"
         + ind + "}\n"
         + ind + "location ~* ^/bedlet_grid\\.html$ {\n"
         + ind + "    return 301 /bedlet_grid.html;\n"
         + ind + "}\n\n")
open(site, "w").write(text[:m.start()] + block + text[m.start():])
PYEOF
        then
            if sudo nginx -t >/dev/null 2>&1; then
                sudo systemctl reload nginx
                echo "  nginx: /bedlet_grid.html served from $BEDLET"
            else
                sudo cp "$SITE.kxlplus-bak" "$SITE"
                echo "  WARNING: nginx did not accept the change - put back as it was;"
                echo "  no bedlet grid page"
            fi
        else
            echo "  WARNING: could not add the bedlet grid page to $SITE"
        fi
    fi
    # the camera entry, in Moonraker's database - yours to change or delete
    # (POST /server/webcams/item adds one, or with its uid updates it -
    # moonraker components/webcam.py).  The address carries ?v=2: a browser
    # kept v1.0.0's page in its iframe after an update (owner 10/14) and a
    # new address is the one thing that makes it fetch again; a v1.0.0
    # camera (plain /bedlet_grid.html) is moved to it.  3:4: the whole page
    # fits in Mainsail's camera panel without a scroll (owner 10/15; 1:1 needed
    # one) - a v1.0.0 camera still at 1:1 gets it too, any other shape stays.
    CAM=$("$PY" - <<'PYEOF' 2>/dev/null
import json, urllib.request
URL = "/bedlet_grid.html?v=2"
def call(method, path, body=None):
    req = urllib.request.Request("http://localhost:7125" + path, method=method,
                                 data=json.dumps(body).encode() if body else None,
                                 headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=10))["result"]
cams = [c for c in call("GET", "/server/webcams/list")["webcams"]
        if c.get("stream_url", "").startswith("/bedlet_grid.html")]
if not cams:
    call("POST", "/server/webcams/item", {
        "name": "Modular Bed", "service": "iframe", "stream_url": URL,
        "aspect_ratio": "3:4", "location": "printer", "enabled": True})
    print("added")
elif cams[0]["stream_url"] == "/bedlet_grid.html":
    cam = dict(cams[0]); cam.pop("source", None); cam["stream_url"] = URL
    if cam.get("aspect_ratio") == "1:1":
        cam["aspect_ratio"] = "3:4"
    call("POST", "/server/webcams/item", cam)
    print("moved")
else:
    print("there")
PYEOF
)
    case "$CAM" in
        added) echo "  camera: 'Modular Bed' added (Mainsail: Webcams; Fluidd: Cameras)" ;;
        moved) echo "  camera: the bedlet grid camera now loads the new page (?v=2)" ;;
        there) echo "  camera: a bedlet grid camera is already there" ;;
        *)     echo "  WARNING: Moonraker did not take the camera entry - add it by hand:"
               echo "  name Modular Bed, service iframe, URL /bedlet_grid.html?v=2, 3:4" ;;
    esac
else
    echo "  NOTE: no Mainsail/Fluidd nginx site found - bedlet grid page not set up"
fi

# ---- [6] start and check ----------------------------------------------------
echo; echo "[6] Starting Klipper"
start_klipper
state=""
for i in $(seq 1 60); do
    sleep 3
    state=$(curl -s localhost:7125/printer/info | "$PY" -c "import sys,json;print(json.load(sys.stdin)['result']['state'])" 2>/dev/null || true)
    case "$state" in ready|error|shutdown) break ;; esac
done
echo "  Klipper: ${state:-no answer}"
LOG="$HOME/printer_data/logs/klippy.log"
if [ -f "$LOG" ]; then
    S=$(grep -n "^Start printer at" "$LOG" | tail -1 | cut -d: -f1)
    awk -v s="$S" 'NR>=s' "$LOG" | grep -o "Loaded MCU '[a-z_0-9]*' [0-9]* commands ([^ ]*" | sed 's/^/  /' || true
fi
FWC=$(curl -s "localhost:7125/printer/objects/query?xl_fw_check" | "$PY" -c 'import sys, json
s = json.load(sys.stdin)["result"]["status"]["xl_fw_check"]
if not s["checked"]:
    print("not checked (no firmware ID in this build)")
elif s["outdated"]:
    print("NOT on this KlipperXL-Plus firmware yet: " + ", ".join(s["outdated"]))
else:
    print("every board runs this KlipperXL-Plus firmware")' 2>/dev/null || true)
[ -n "$FWC" ] && echo "  Firmware: $FWC"
mark KLIPPER "${state:-no answer}"
mark FIRMWARE "${FWC:-unknown}"
mark STEP done

cat <<EOF

=== Calibrate: Macros -> CALIBRATION_WIZARDS (Mainsail or KlipperScreen) ===
Guided dialogs; each saves into YOUR printer.cfg.
WARNING: pick or park problems? Run 5. Dock positions FIRST - every other
         wizard picks and parks tools.
In this order:
  1. Loadcell reference  - every tool (Z homing needs it)
  2. Heater PID          - every tool, each starting cold (optional: Prusa
                           defaults are seeded)
  3. Filament sensors    - every tool: side sensor + tool sensor
  4. Z alignment         - levels the two Z motors against the top frame
  5. Dock positions      - where each dock really is (optional; one or all)
  6. Tool offsets        - calibration pin, all tools against T0 (multi-tool)
  7. Input shaper        - T0's accelerometer, X then Y
  8. Pressure advance    - every tool, filament loaded (~6 min each)
Then the first layer per tool: babystep while printing, Save in Mainsail.
EOF
