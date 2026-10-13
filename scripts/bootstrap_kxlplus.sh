#!/bin/bash
# =============================================================================
# KlipperXL-Plus - bootstrap: from a fresh MainsailOS SD card to KlipperXL-Plus
# Copyright (C) 2026 Richard Crook
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Owner 10/10: KlipperXL-Plus installs automatically, so a new user only flashes
# MainsailOS and runs one command.  MainsailOS (3.0.0, Debian 13 trixie, in
# Raspberry Pi Imager) already has Klipper, Moonraker, Mainsail, Crowsnest,
# Timelapse and the input shaper dependencies, and no printer.cfg (the
# installer writes the KlipperXL-Plus one) - this adds what KlipperXL-Plus needs and
# hands over to scripts/install_kxlplus.sh, which builds the firmware, puts
# the XLBuddy firmware on a USB stick, flashes the tools and writes the
# configs.
#
# Owner 10/10: the install runs from a WEB PAGE (RatOS style, at the Pi's own
# address - the Pi already knows the network, no hotspot).  This script is
# the one command that starts it:
#   git clone <repo> ~/KlipperXL-Plus && bash ~/KlipperXL-Plus/scripts/bootstrap_kxlplus.sh
# It checks the Pi, starts scripts/web_installer.py (port 8099, detached -
# the terminal can be closed) and prints the address.  The page starts with
# the appendix warning, asks KlipperScreen yes/no and the password, and runs
# the steps below with its buttons.
#
#   (no option)        start the web installer (the documented way)
#   --engine           the steps themselves, run BY the web installer
#     --klipperscreen y|n
#   --terminal         the same steps in this terminal, with questions here
#
# Run as the normal user (not root).  Safe to run again.
# =============================================================================
set -euo pipefail

# The KlipperXL-Plus repository (owner to set the public URL before release)
KXL_REPO="${KXL_REPO:-https://github.com/racoutlaw/KlipperXL-Plus.git}"
KXL_BRANCH="${KXL_BRANCH:-main}"
KXL_DIR="${KXL_DIR:-$HOME/KlipperXL-Plus}"
# run from a checkout (the usual way): THAT checkout is KlipperXL-Plus, in
# every mode - also an older clone under another name (owner 10/10 rename)
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd || true)"
if [ -n "$HERE" ] && [ -f "$HERE/web_installer.py" ]; then
    KXL_DIR="$(cd "$HERE/.." && pwd)"
fi
KLIPPER_DIR="$HOME/klipper"
PY="$HOME/klippy-env/bin/python"
CONFIG_DIR="$HOME/printer_data/config"
WEB_PORT=8099
# The firmware toolchain (as Klipper's own scripts/install-debian.sh)
APT_PACKAGES="git build-essential gcc-arm-none-eabi binutils-arm-none-eabi libnewlib-arm-none-eabi python3-venv curl"

MODE=launch; ENGINE_KS=n
while [ $# -gt 0 ]; do
    case "$1" in
        --terminal) MODE=terminal ;;
        --engine) MODE=engine ;;
        --klipperscreen) ENGINE_KS="$2"; shift ;;
        *) echo "unknown option: $1"; exit 1 ;;
    esac
    shift
done

ask() {   # from the keyboard, also when the script is piped into bash
    local reply=""
    printf '%s' "$1" >&2
    if ! read -r reply 2>/dev/null < /dev/tty; then
        echo >&2
        echo "  (no terminal to answer on - run this in an SSH session)" >&2
    fi
    echo "$reply"
}

# ---- [1] checks (every mode) ----------------------------------------------------
check_pi() {
    echo; echo "[1] Checking this Pi"
    if [ "$(id -u)" = "0" ]; then
        echo "ERROR: run this as your normal user, not root (no sudo in front)."
        exit 1
    fi
    ARCH=$(uname -m)
    echo "  $(. /etc/os-release && echo "$PRETTY_NAME"), $ARCH"
    [ "$ARCH" = "aarch64" ] || echo "  NOTE: KlipperXL-Plus is tested on 64-bit (aarch64) only."
    MISSING=""
    [ -d "$KLIPPER_DIR/.git" ] || MISSING="$MISSING\n  - Klipper ($KLIPPER_DIR)"
    [ -x "$PY" ] || MISSING="$MISSING\n  - Klipper's Python environment (~/klippy-env)"
    [ -d "$HOME/moonraker" ] || MISSING="$MISSING\n  - Moonraker (~/moonraker)"
    systemctl cat klipper.service >/dev/null 2>&1 \
        || MISSING="$MISSING\n  - the klipper service"
    [ -d "$CONFIG_DIR" ] || MISSING="$MISSING\n  - $CONFIG_DIR"
    if [ -n "$MISSING" ]; then
        echo "ERROR: this Pi does not have the Klipper setup KlipperXL-Plus starts from:"
        echo -e "$MISSING"
        echo
        echo "  Flash MainsailOS (64-bit) with Raspberry Pi Imager, set the user,"
        echo "  Wi-Fi and SSH there, boot the Pi, then run this again."
        exit 1
    fi
    echo "  Klipper, Moonraker and Mainsail are here."
}

echo "=================================================================="
echo " KlipperXL-Plus - Klipper on all five Prusa XL tools - bootstrap"
echo "=================================================================="

# ---- launch: start the web installer and say where it is ----------------------
if [ "$MODE" = "launch" ]; then
    check_pi
    if [ ! -f "$KXL_DIR/scripts/web_installer.py" ]; then
        git clone -q -b "$KXL_BRANCH" "$KXL_REPO" "$KXL_DIR"
    fi
    if ! curl -s -o /dev/null "http://localhost:$WEB_PORT/api/state"; then
        # detached: closing this terminal does not stop the install
        setsid nohup python3 "$KXL_DIR/scripts/web_installer.py" \
            --kxl-dir "$KXL_DIR" --port "$WEB_PORT" \
            > "$HOME/kxlplus-web-installer.log" 2>&1 < /dev/null &
        for i in 1 2 3 4 5 6 7 8 9 10; do
            sleep 1
            curl -s -o /dev/null "http://localhost:$WEB_PORT/api/state" && break
        done
    fi
    if ! curl -s -o /dev/null "http://localhost:$WEB_PORT/api/state"; then
        echo "ERROR: the installer page did not start - see ~/kxlplus-web-installer.log"
        exit 1
    fi
    IP=$(hostname -I 2>/dev/null | awk '{print $1}')
    echo
    echo "  The KlipperXL-Plus installer is running. Open this page in a browser on"
    echo "  any computer on your network:"
    echo
    echo "      http://$IP:$WEB_PORT"
    echo "      (or http://$(hostname).local:$WEB_PORT)"
    echo
    echo "  It starts with the appendix warning - read it first. You can close"
    echo "  this terminal now; the install carries on in the page."
    exit 0
fi

# ---- the steps (--terminal here, --engine from the web page) --------------------
APPENDIX_TEXT='
  READ THIS BEFORE YOU START - THE APPENDIX

  The Prusa bootloader verifies firmware signatures and will reject
  unsigned Klipper firmware unless the APPENDIX (safety seal) on the
  XLBuddy board is broken. Breaking it is a PHYSICAL, IRREVERSIBLE
  modification to your mainboard - it cannot be undone, and it is the
  same requirement as for any custom firmware on the XL.

  Everything else here is reversible: you can flash stock Prusa firmware
  back at any time and the printer returns to normal. The appendix is the
  one step that does not come back. Decide about it before you begin,
  not halfway through.

  With the appendix intact, nothing below will get onto the printer: the
  bootloader refuses the KlipperXL-Plus firmware.

  THIS SOFTWARE IS PROVIDED "AS IS" WITHOUT WARRANTY OF ANY KIND. YOU ARE
  MODIFYING YOUR PRINTER'\''S FIRMWARE AND ACCEPT ALL RISKS OF DOING SO.
'
if [ "$MODE" = "terminal" ]; then
    echo "$APPENDIX_TEXT"
    ok=$(ask "  Is the appendix on your XLBuddy broken, and do you accept the above? Type yes: ")
    if [ "$ok" != "yes" ]; then
        echo "  Stopped - nothing was changed."
        exit 1
    fi
else
    echo "  Appendix: confirmed on the installer page."
fi

check_pi
if ! sudo -n true 2>/dev/null; then
    echo "  sudo will ask for your password for the package installs."
fi

# ---- [2] packages ---------------------------------------------------------------
echo; echo "[2] Installing the firmware build tools"
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq $APT_PACKAGES
echo "  $(arm-none-eabi-gcc --version | head -1)"

# ---- [3] Klipper up to date ----------------------------------------------------
# A fresh install builds the tools' and XLBuddy's firmware from THIS Klipper;
# the KlipperXL-Plus patches are made against current Klipper, and a MainsailOS
# image carries the Klipper of the day it was built.  The firmware build
# checks that every patch applies and stops with a clear message if not.
echo; echo "[3] Updating Klipper"
if [ -n "$(git -C "$KLIPPER_DIR" status --porcelain --untracked-files=no)" ]; then
    echo "  NOTE: Klipper has local changes - left as it is:"
    git -C "$KLIPPER_DIR" status --short --untracked-files=no | sed 's/^/    /'
else
    git -C "$KLIPPER_DIR" pull --ff-only -q
    "$PY" -m pip install -q -r "$KLIPPER_DIR/scripts/klippy-requirements.txt"
fi
echo "  Klipper $(git -C "$KLIPPER_DIR" describe --tags --always)"

# ---- [4] KlipperScreen (only with a screen on the Pi) ---------------------------
echo
if [ -d "$HOME/KlipperScreen" ]; then
    echo "[4] KlipperScreen is already installed"
else
    if [ "$MODE" = "terminal" ]; then
        ks=$(ask "[4] Is a touchscreen connected to the Pi for KlipperScreen? [y/N] ")
    else
        ks="$ENGINE_KS"
        echo "[4] KlipperScreen: $([ "$ks" = "y" ] && echo "yes" || echo "no") (chosen on the page)"
    fi
    case "$ks" in
        y|Y|yes)
            git clone -q https://github.com/KlipperScreen/KlipperScreen.git "$HOME/KlipperScreen"
            # its installer reads its answers from these when they are set.
            # NETWORK=N: with Y it installs NetworkManager and REBOOTS the Pi
            # at the end (cutting this script off); MainsailOS already runs
            # NetworkManager, so KlipperScreen's network panel works anyway.
            BACKEND=X NETWORK=N SERVICE=Y \
                bash "$HOME/KlipperScreen/scripts/KlipperScreen-install.sh" < /dev/null
            ;;
        *) echo "  skipped (it can be added later)" ;;
    esac
fi

# ---- [5] KlipperXL-Plus --------------------------------------------------------------
echo; echo "[5] KlipperXL-Plus -> $KXL_DIR"
if [ -d "$KXL_DIR/.git" ]; then
    # owner 10/10 (image test): a KlipperXL-Plus copy that cannot be updated (no
    # network, the repo not reachable) is installed as it is - the SD image
    # brings a complete one.  Never stop to ask for a login.
    if ! GIT_TERMINAL_PROMPT=0 git -C "$KXL_DIR" pull --ff-only -q; then
        echo "  NOTE: KlipperXL-Plus could not be updated from"
        echo "  $(git -C "$KXL_DIR" remote get-url origin) - installing the version on this Pi."
    fi
else
    git clone -q -b "$KXL_BRANCH" "$KXL_REPO" "$KXL_DIR"
fi
echo "  $(git -C "$KXL_DIR" log -1 --format='%h %s')"

# ---- [6] the KlipperXL-Plus installer -------------------------------------------
echo; echo "[6] Starting the KlipperXL-Plus installer"
if [ "$MODE" = "terminal" ]; then
    # its questions come from the keyboard even when this script was piped in
    exec bash "$KXL_DIR/scripts/install_kxlplus.sh" < /dev/tty
fi
# --engine: the web installer answers on stdin (KXL_WEB=1 questions)
exec bash "$KXL_DIR/scripts/install_kxlplus.sh"
