#!/usr/bin/env python3
# KlipperXL-Plus - put the XLBuddy firmware (.bbf) on a USB stick plugged into
# the Pi, for the Prusa bootloader to install (docs/INSTALL.md 4.3 -
# FAT32 stick in the printer, power cycle).
#
# Owner 10/08: "the installer asks for a USB stick in the Pi".  Done by hand
# 10/10 and made a step here, with the same rules:
#   - only a removable USB disk with a FAT (vfat) filesystem is offered, and
#     never the disk the Pi runs from (/ or /boot/firmware)
#   - the stick is shown (name, label, size, the .bbf files already on it)
#     and nothing is written without a yes
#   - nothing is ever deleted or formatted: a .bbf already in the root is
#     RENAMED <name>.<date-time>.old (the printer stick's own convention), so
#     the bootloader sees only the new klipper.bbf
#   - the copy is read back and its md5 compared, then the stick unmounted
#
#   xl_usb_stick.py FILE.bbf [--dry-run] [--device /dev/sdX1]
#     --dry-run   find + show only (mounts read-only), writes nothing
#     --device    use this partition (must still be FAT and not the Pi's disk)
# Exit: 0 copied (or dry run done), 2 skipped by the user, 1 error.
#
# Copyright (C) 2026  Richard Crook
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import argparse, hashlib, json, os, shutil, subprocess, sys, time

MOUNT_DIR = '/mnt/kxlplus-usb'


def run(cmd, check=True):
    return subprocess.run(cmd, check=check, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, universal_newlines=True)

def ask(prompt, choices):
    # web installer (KXL_WEB=1, owner 10/10): the question goes to the page as
    # "@@ASK {json}" with button choices, the answer comes back on stdin
    if os.environ.get('KXL_WEB') == '1':
        print("@@ASK " + json.dumps({'id': 'usb', 'text': prompt.strip(),
                                     'choices': choices}))
        sys.stdout.flush()
        return sys.stdin.readline().strip()
    # read from the terminal even when the installer's stdin is redirected
    try:
        with open('/dev/tty') as tty:
            sys.stdout.write(prompt)
            sys.stdout.flush()
            return tty.readline().strip()
    except OSError:
        return ''

def md5(path):
    h = hashlib.md5()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(65536), b''):
            h.update(block)
    return h.hexdigest()

def blockdevs():
    out = run(['lsblk', '-J', '-b', '-o',
               'NAME,PATH,TYPE,TRAN,RM,HOTPLUG,FSTYPE,LABEL,SIZE,MODEL,'
               'MOUNTPOINT,PKNAME']).stdout
    return json.loads(out)['blockdevices']

def flatten(devs, parent=None):
    for d in devs:
        d['_parent'] = parent
        yield d
        for c in flatten(d.get('children') or [], d):
            yield c

def system_disks():
    """The disks the Pi runs from - never offered."""
    disks = set()
    for mnt in ('/', '/boot/firmware', '/boot'):
        src = run(['findmnt', '-n', '-o', 'SOURCE', mnt], check=False)
        src = src.stdout.strip()
        if not src.startswith('/dev/'):
            continue
        pk = run(['lsblk', '-no', 'PKNAME', src], check=False).stdout.strip()
        disks.add(pk or os.path.basename(src))
    return disks

def flag(v):
    return v in (True, 1, '1', 'true')

def candidates(system):
    found, other = [], []
    for d in flatten(blockdevs()):
        disk = d if d['type'] == 'disk' else d['_parent']
        if disk is None or disk['name'] in system:
            continue
        if disk.get('tran') != 'usb' or not (flag(disk.get('rm'))
                                             or flag(disk.get('hotplug'))):
            continue
        if d['type'] not in ('part', 'disk') or not d.get('fstype'):
            continue
        if d['type'] == 'disk' and d.get('children'):
            continue
        entry = {'path': d['path'], 'label': d.get('label') or '',
                 'fstype': d['fstype'], 'size': int(d.get('size') or 0),
                 'model': (disk.get('model') or '').strip(),
                 'mountpoint': d.get('mountpoint'), 'disk': disk['name']}
        (found if d['fstype'] == 'vfat' else other).append(entry)
    return found, other

def describe(c):
    return "%s  %.1f GB  label '%s'  (%s)" % (c['path'], c['size'] / 1e9,
                                              c['label'], c['model'] or 'USB')

def mount(dev, ro):
    """Mount the stick; returns (mountpoint, mounted_by_us)."""
    cur = run(['findmnt', '-n', '-o', 'TARGET,OPTIONS', dev], check=False)
    cur = cur.stdout.strip().split()
    if cur:
        target, opts = cur[0], cur[1] if len(cur) > 1 else ''
        if not ro and 'ro' in opts.split(','):
            raise SystemExit("ERROR: %s is mounted read-only at %s - unmount"
                             " it and run again" % (dev, target))
        return target, False
    run(['sudo', 'mkdir', '-p', MOUNT_DIR])
    opts = 'uid=%d,gid=%d%s' % (os.getuid(), os.getgid(), ',ro' if ro else '')
    run(['sudo', 'mount', '-o', opts, dev, MOUNT_DIR])
    return MOUNT_DIR, True

def unmount(target, ours):
    run(['sync'], check=False)
    if ours:
        run(['sudo', 'umount', target])

def bbfs(target):
    return sorted(f for f in os.listdir(target) if f.lower().endswith('.bbf'))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('bbf')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--device')
    a = ap.parse_args()
    if not os.path.isfile(a.bbf):
        print("ERROR: no firmware file %s" % (a.bbf,))
        return 1
    want = md5(a.bbf)
    system = system_disks()
    print("  firmware: %s (%d B, md5 %s)" % (a.bbf, os.path.getsize(a.bbf),
                                            want[:8]))
    print("  the Pi runs from: %s - never offered" % (", ".join(sorted(system)),))
    while True:
        if a.device:
            found = [c for c in candidates(set())[0] if c['path'] == a.device]
            pk = run(['lsblk', '-no', 'PKNAME', a.device], check=False).stdout
            if pk.strip() in system or os.path.basename(a.device) in system:
                print("ERROR: %s is the Pi's own disk" % (a.device,))
                return 1
            if not found:
                fs = run(['lsblk', '-no', 'FSTYPE', a.device], check=False)
                if fs.stdout.strip() != 'vfat':
                    print("ERROR: %s is not a FAT (vfat) partition" % (a.device,))
                    return 1
                found = [{'path': a.device, 'label': '', 'fstype': 'vfat',
                          'size': 0, 'model': 'given', 'mountpoint': None,
                          'disk': ''}]
            other = []
        else:
            found, other = candidates(system)
        for o in other:
            print("  (not offered: %s is %s - the printer needs FAT32)"
                  % (describe(o), o['fstype']))
        if found:
            break
        if a.dry_run:
            print("  no FAT32 USB stick plugged into the Pi")
            return 0
        r = ask("  No FAT32 USB stick found. Plug one into the Pi and press"
                " Enter, or type s to skip and copy the file by hand: ",
                [['', "I plugged one into the Pi - look again"],
                 ['s', "Skip - I copy the file myself"]])
        if r.lower().startswith('s'):
            return 2
        time.sleep(2)
    if len(found) == 1:
        stick = found[0]
    else:
        for i, c in enumerate(found, 1):
            print("  %d. %s" % (i, describe(c)))
        r = ask("  Which stick (1-%d), or s to skip: " % (len(found),),
                [[str(i), describe(c)] for i, c in enumerate(found, 1)]
                + [['s', "Skip - I copy the file myself"]])
        if not r.isdigit() or not 1 <= int(r) <= len(found):
            return 2
        stick = found[int(r) - 1]
    # look before writing: read-only first
    target, ours = mount(stick['path'], ro=True)
    try:
        old = bbfs(target)
        free = shutil.disk_usage(target).free
    finally:
        unmount(target, ours)
    print("  USB stick: %s" % (describe(stick),))
    print("  .bbf files on it now: %s" % (", ".join(old) or "none"))
    stamp = time.strftime('%Y%m%d-%H%M')
    plan = ["rename %s -> %s.%s.old" % (f, f, stamp) for f in old]
    plan.append("copy klipper.bbf (md5 %s)" % (want[:8],))
    for p in plan:
        print("    will %s" % (p,))
    if free < os.path.getsize(a.bbf) + 65536:
        print("ERROR: not enough free space on the stick")
        return 1
    if a.dry_run:
        print("  dry run - nothing written")
        return 0
    if ask("  Write the firmware to this stick? Nothing is deleted or"
           " formatted. [y/N] ",
           [['y', "Write it to %s" % (stick['path'],)],
            ['n', "No - don't write"]]).lower() not in ('y', 'yes'):
        return 2
    target, ours = mount(stick['path'], ro=False)
    try:
        for f in bbfs(target):
            dst = os.path.join(target, "%s.%s.old" % (f, stamp))
            if os.path.exists(dst):
                print("ERROR: %s already exists - nothing written" % (dst,))
                return 1
            os.rename(os.path.join(target, f), dst)
            print("  renamed %s -> %s" % (f, os.path.basename(dst)))
        dst = os.path.join(target, 'klipper.bbf')
        with open(a.bbf, 'rb') as src, open(dst, 'wb') as out:
            out.write(src.read())
            out.flush()
            os.fsync(out.fileno())
        got = md5(dst)
        if got != want:
            print("ERROR: the copy on the stick does not match (md5 %s, wanted"
                  " %s) - do not flash it" % (got[:8], want[:8]))
            return 1
        print("  copied klipper.bbf, read back md5 %s - matches" % (got[:8],))
        left = bbfs(target)
        if left != ['klipper.bbf']:
            print("ERROR: other .bbf files are still in the root: %s" % (left,))
            return 1
    finally:
        unmount(target, ours)
    print("  unmounted - the stick can be pulled out")
    return 0

if __name__ == '__main__':
    sys.exit(main())
