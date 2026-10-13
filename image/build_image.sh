#!/bin/bash
# =============================================================================
# KlipperXL-Plus - build the KlipperXL-Plus SD card image from the official MainsailOS
# image (owner 10/10: "like ratos does - download their image, flash it with
# Raspberry Pi Imager, no command line needed").
# Copyright (C) 2026 Richard Crook
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# The image = MainsailOS, unchanged, plus:
#   /home/pi/KlipperXL-Plus            a git clone of KlipperXL-Plus (origin = the
#                                      public repo, so Mainsail's update list
#                                      can update it)
#   KlipperXL-Plus-installer.service          the installer page (port 8099); its
#                                      user is filled in at first boot
#   moonraker.service.d/KlipperXL-Plus-installer.conf
#                                      keeps that service listed in Mainsail
#                                      (ordering hint only, never starts it)
#   kxlplus-firstboot.service          at every boot until KlipperXL-Plus is
#                                      installed, after MainsailOS's own
#                                      first-boot user rename (postrename):
#                                      fills in the user, allows the service in
#                                      Moonraker, starts the installer page
# MainsailOS renames the image's user 'pi' to the one set in Raspberry Pi
# Imager and MOVES /home/pi (prerename: usermod -d /home/<user> -m), so the
# clone goes along; nothing else in MainsailOS is touched.
#
# Run as root on a Linux machine (losetup, mount, xz, git):
#   bash build_image.sh --base MainsailOS-...-raspberry-arm64.img.xz \
#        --repo /path/to/KlipperXL-Plus --version v1.0.0 --out /path/to/out \
#        [--origin https://github.com/racoutlaw/KlipperXL-Plus.git]
# --repo is a clone with the KlipperXL-Plus files at its top (the public repo);
# --origin sets the clone's origin in the image (default: --repo's origin).
# Output: KlipperXL-Plus-<version>-<base name>.img.xz + .sha256 and the
# Raspberry Pi Imager list KlipperXL-Plus-imager.json (Imager: App Options ->
# Content Repository -> Use custom URL; the same URL for the Raspberry Pi form).
# The list points at the GitHub release by default:
#   --download-base URL  where the .img.xz will be (default
#                        https://github.com/racoutlaw/KlipperXL-Plus/releases/download/<version>)
#   --icon-url URL       the 40x40 icon (default: image/ on the public repo's main)
#   --date YYYY-MM-DD    release_date (default: today on this machine)
#   --test               a test build: " [TEST]" in the list's name
# =============================================================================
set -euo pipefail

BASE=""; REPO=""; VERSION=""; OUT=""; ORIGIN=""
DL_BASE=""; ICON_URL=""; RDATE=""; TEST=0
while [ $# -gt 0 ]; do
    case "$1" in
        --base) BASE="$2"; shift ;;
        --repo) REPO="$2"; shift ;;
        --origin) ORIGIN="$2"; shift ;;   # default: --repo's own origin
        --version) VERSION="$2"; shift ;;
        --out) OUT="$2"; shift ;;
        --download-base) DL_BASE="$2"; shift ;;
        --icon-url) ICON_URL="$2"; shift ;;
        --date) RDATE="$2"; shift ;;
        --test) TEST=1 ;;
        *) echo "unknown option: $1"; exit 1 ;;
    esac
    shift
done
GH=https://github.com/racoutlaw/KlipperXL-Plus
[ -n "$DL_BASE" ] || DL_BASE="$GH/releases/download/$VERSION"
[ -n "$ICON_URL" ] || ICON_URL="https://raw.githubusercontent.com/racoutlaw/KlipperXL-Plus/main/image/KlipperXL-Plus-icon-40x40.png"
[ -n "$RDATE" ] || RDATE=$(date +%F)
[ "$(id -u)" = "0" ] || { echo "ERROR: run as root (loop devices + mount)"; exit 1; }
for v in BASE REPO VERSION OUT; do
    [ -n "${!v}" ] || { echo "ERROR: --$(echo $v | tr A-Z a-z) is required"; exit 1; }
done
[ -f "$BASE" ] || { echo "ERROR: no such image: $BASE"; exit 1; }
[ -f "$REPO/scripts/web_installer.py" ] || { echo "ERROR: $REPO is not a KlipperXL-Plus clone"; exit 1; }
for t in xz losetup git sha256sum; do
    command -v $t >/dev/null || { echo "ERROR: $t is missing"; exit 1; }
done
mkdir -p "$OUT"
NAME="KlipperXL-Plus-$VERSION-$(basename "$BASE" .img.xz)"
WORK=$(mktemp -d -p "$OUT" .build-XXXX)
LOOP=""
cleanup() {
    mountpoint -q "$WORK/root" 2>/dev/null && umount "$WORK/root"
    [ -n "$LOOP" ] && losetup -d "$LOOP" 2>/dev/null
    rm -rf "$WORK"
}
trap cleanup EXIT

echo "=== unpacking $(basename "$BASE")"
xz -dc "$BASE" > "$WORK/os.img"
LOOP=$(losetup -fP --show "$WORK/os.img")
mkdir -p "$WORK/root"
mount "${LOOP}p2" "$WORK/root"
R="$WORK/root"
# the right image? MainsailOS keeps its user 'pi' (uid 1000) until first boot
[ -d "$R/home/pi/klipper" ] && [ -d "$R/home/pi/moonraker" ] \
    && [ -f "$R/usr/local/sbin/mainsailos-postrename" ] \
    || { echo "ERROR: this is not a MainsailOS image (no /home/pi/klipper, moonraker, mainsailos-postrename)"; exit 1; }
[ "$(awk -F: '$1=="pi"{print $3}' "$R/etc/passwd")" = "1000" ] \
    || { echo "ERROR: user pi is not uid 1000 in this image"; exit 1; }

echo "=== KlipperXL-Plus -> /home/pi/KlipperXL-Plus ($(git -C "$REPO" log -1 --format='%h %s'))"
git clone -q "$REPO" "$R/home/pi/KlipperXL-Plus"
[ -n "$ORIGIN" ] || ORIGIN=$(git -C "$REPO" remote get-url origin)
git -C "$R/home/pi/KlipperXL-Plus" remote set-url origin "$ORIGIN"
echo "    origin $ORIGIN, $(git -C "$R/home/pi/KlipperXL-Plus" describe --tags --always)"
chown -R 1000:1000 "$R/home/pi/KlipperXL-Plus"

echo "=== the installer page service + its Mainsail listing"
cat > "$R/etc/systemd/system/KlipperXL-Plus-installer.service" <<'EOF'
# KlipperXL-Plus (image; the installer rewrites it on every run): the installer
# page, started at first boot and on demand from Mainsail; stops by itself
[Unit]
Description=KlipperXL-Plus installer page (port 8099)
After=network-online.target

[Service]
Type=simple
User=@KXL_USER@
WorkingDirectory=@KXL_HOME@
ExecStart=/usr/bin/python3 @KXL_HOME@/KlipperXL-Plus/scripts/web_installer.py --kxl-dir @KXL_HOME@/KlipperXL-Plus --port 8099
Restart=no

[Install]
WantedBy=multi-user.target
EOF
mkdir -p "$R/etc/systemd/system/moonraker.service.d"
cat > "$R/etc/systemd/system/moonraker.service.d/KlipperXL-Plus-installer.conf" <<'EOF'
# KlipperXL-Plus: keeps KlipperXL-Plus-installer.service loaded so Moonraker lists it for
# Mainsail. An ordering hint only - it never starts it.
[Unit]
After=KlipperXL-Plus-installer.service
EOF

echo "=== first boot: start the installer page for the user Imager set up"
cat > "$R/usr/local/sbin/kxlplus-firstboot" <<'EOF'
#!/bin/bash
# KlipperXL-Plus image: at EVERY boot until KlipperXL-Plus is installed
# (kxlplus-firstboot.service), after MainsailOS has renamed 'pi' to the Imager
# user and moved its home: start the installer page.  Tested 10/10: run only
# once, the page started on the very first boot was gone after MainsailOS's
# own first-boot reboot - so it runs until the installer's stamp exists.
set -u
U=$(getent passwd 1000 | cut -d: -f1)
H=$(getent passwd 1000 | cut -d: -f6)
if [ -f "$H/printer_data/config/kxlplus/.installed" ]; then
    echo "KlipperXL-Plus is installed - the installer page is started from Mainsail"
    exit 0
fi
echo "KlipperXL-Plus not installed yet: user $U, home $H - starting the installer page"
sed -i "s|@KXL_USER@|$U|g; s|@KXL_HOME@|$H|g" /etc/systemd/system/KlipperXL-Plus-installer.service
# Moonraker writes moonraker.asvc on its first start - on the first boot that
# can be after this (tested 10/10); missing = write Moonraker's own defaults
# (docs + its file) plus ours, so nothing of Moonraker's is lost
A="$H/printer_data/moonraker.asvc"
if [ ! -f "$A" ]; then
    printf '%s\n' klipper_mcu webcamd MoonCord KlipperScreen moonraker-telegram-bot \
        moonraker-obico sonar crowsnest octoeverywhere ratos-configurator > "$A"
fi
grep -qxF KlipperXL-Plus-installer "$A" || echo KlipperXL-Plus-installer >> "$A"
chown "$U:$U" "$A"
systemctl daemon-reload
systemctl restart moonraker
systemctl start KlipperXL-Plus-installer
echo "KlipperXL-Plus: installer page started on port 8099"
EOF
chmod 0755 "$R/usr/local/sbin/kxlplus-firstboot"
cat > "$R/etc/systemd/system/kxlplus-firstboot.service" <<'EOF'
[Unit]
Description=KlipperXL-Plus until installed: start the installer page at boot
# after MainsailOS's own first boot: the user is renamed and paths fixed;
# the script itself does nothing once KlipperXL-Plus is installed
After=cloud-final.service mainsailos-postrename.service
Wants=cloud-final.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/kxlplus-firstboot
StandardOutput=journal+console
StandardError=journal+console

[Install]
WantedBy=cloud-init.target
EOF
mkdir -p "$R/etc/systemd/system/cloud-init.target.wants"
ln -sf /etc/systemd/system/kxlplus-firstboot.service \
    "$R/etc/systemd/system/cloud-init.target.wants/kxlplus-firstboot.service"
echo "$VERSION" > "$R/home/pi/KlipperXL-Plus/.image_version"
chown 1000:1000 "$R/home/pi/KlipperXL-Plus/.image_version"

sync
umount "$R"
losetup -d "$LOOP"; LOOP=""
echo "=== the unpacked image's size and sha256 (the Imager list checks both)"
EXTRACT_SIZE=$(stat -c %s "$WORK/os.img")
EXTRACT_SHA=$(sha256sum "$WORK/os.img" | cut -d' ' -f1)
echo "=== compressing (xz) -> $OUT/$NAME.img.xz"
xz -T0 -6 -c "$WORK/os.img" > "$OUT/$NAME.img.xz"
(cd "$OUT" && sha256sum "$NAME.img.xz" > "$NAME.img.xz.sha256")
DL_SIZE=$(stat -c %s "$OUT/$NAME.img.xz")
DL_SHA=$(cut -d' ' -f1 "$OUT/$NAME.img.xz.sha256")

# ---- the Raspberry Pi Imager list -------------------------------------------
# Format tested 10/10 with Imager 2.0.12 (owner's card): the "imager.devices"
# block is needed or Imager's customisation (user, Wi-Fi, SSH) is greyed out;
# image_download_sha256 is required by Raspberry Pi's docs for a list.
MOS=$(basename "$BASE" .img.xz | sed -n 's/.*-\([0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*\)$/\1/p')
LABEL="KlipperXL-Plus $VERSION - Raspberry Pi (64-bit)"
[ "$TEST" = "1" ] && LABEL="$LABEL [TEST]"
cat > "$OUT/KlipperXL-Plus-imager.json" <<EOF
{
  "imager": {
    "latest_version": "2.0.12",
    "url": "https://www.raspberrypi.com/software/",
    "devices": [
      {
        "name": "Raspberry Pi 5",
        "tags": ["pi5-64bit", "pi5-32bit"],
        "icon": "https://downloads.raspberrypi.com/imager/icons/RPi_5.png",
        "description": "Raspberry Pi 5, 500 / 500+, and Compute Module 5",
        "matching_type": "exclusive",
        "capabilities": []
      },
      {
        "name": "Raspberry Pi 4",
        "tags": ["pi4-64bit", "pi4-32bit"],
        "default": false,
        "icon": "https://downloads.raspberrypi.com/imager/icons/RPi_4.png",
        "description": "Raspberry Pi 4 Model B, 400, and Compute Module 4 / 4S",
        "matching_type": "inclusive",
        "capabilities": []
      }
    ]
  },
  "os_list": [
    {
      "name": "$LABEL",
      "description": "Klipper on every tool of the Prusa XL. MainsailOS ${MOS:-} + KlipperXL-Plus - boots into the KlipperXL-Plus installer page (port 8099).",
      "icon": "$ICON_URL",
      "devices": ["pi4-64bit", "pi5-64bit"],
      "init_format": "cloudinit-rpi",
      "url": "$DL_BASE/$NAME.img.xz",
      "release_date": "$RDATE",
      "extract_size": $EXTRACT_SIZE,
      "extract_sha256": "$EXTRACT_SHA",
      "image_download_size": $DL_SIZE,
      "image_download_sha256": "$DL_SHA"
    }
  ]
}
EOF
echo "OK: $OUT/$NAME.img.xz"
cat "$OUT/$NAME.img.xz.sha256"
echo "Imager list: $OUT/KlipperXL-Plus-imager.json (image URL $DL_BASE/$NAME.img.xz)"
