# KlipperXL-Plus - check that every board runs the firmware this KlipperXL-Plus built,
# and that an update from Mainsail's list did not leave the installer's part
# undone
#
# Owner 10/10 (option A): stock Prusa firmware checks the tools at every boot
# and re-flashes one whose firmware differs ("Verifying dwarf" / "Flashing
# dwarf", src/puppies/PuppyBootstrap.cpp).  KlipperXL-Plus flashes from the
# installer, so here it CHECKS at every Klipper start and says plainly what to
# do - after an update nobody should be left guessing why something is odd.
#
# 1. Firmware: firmware/dwarf/build_dwarf_pi.sh and
#    firmware/xlbuddy/build_xlbuddy_pi.sh hash the KlipperXL-Plus inputs of each
#    firmware (patches or sources + .config), compile the result in as the
#    constant KXLPLUS_FW and write it to <build>/fw_id.  The Klipper version
#    is not part of it: a Klipper update alone does not call for a re-flash.
#    At connect every board's KXLPLUS_FW is compared with the last build's
#    fw_id; firmware built before the check has no KXLPLUS_FW and counts as
#    different.  With no build to compare against, nothing is said.
# 2. Update from Mainsail (owner 10/10): KlipperXL-Plus is in Mainsail's update
#    list and its modules are links, so an update changes the code at once.
#    Configs, firmware and the installer itself are the installer's job: it
#    writes kxlplus/.installed (the commit it set up); if the repo has moved
#    on with changes there, the installer page has to finish the update.
#
# Either one -> ONE Mainsail warning (configfile.runtime_warning, the same
# channel as Klipper's own MCU clock warning) saying how to start the
# installer page.  get_status() is read by the installer to flash only the
# tools that differ.
#
#   [xl_fw_check]
#   #build_dir: ~/kxlplus-build
#
# Copyright (C) 2026  Richard Crook
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging, os, socket, subprocess

CONSTANT = 'KXLPLUS_FW'
# what an update from Mainsail cannot do by itself (install_kxlplus.sh [5b])
INSTALLER_PATHS = ['config', 'firmware', 'pa_analysis',
                   'scripts/install_kxlplus.sh']


def this_pi():
    # the address a browser on the network reaches this Pi at (no traffic is
    # sent - a UDP "connect" only picks the outgoing interface)
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(('192.0.2.1', 9))
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return socket.gethostname() + '.local'

def how_to_start():
    # Mainsail 2.19: the power menu (top right) -> Service Control lists it as
    # "KlipperXL-Plus-installer" (checked by the owner 10/10)
    return ("start the KlipperXL-Plus installer: in Mainsail's power menu (top"
            " right) -> Service Control -> KlipperXL-Plus-installer -> start, then"
            " open http://%s:8099" % (this_pi(),))


class XLFwCheck:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.build_dir = os.path.expanduser(
            config.get('build_dir', '~/kxlplus-build'))
        self.boards = {}
        self.outdated = []
        self.checked = False
        self.update_pending = []
        self.printer.register_event_handler("klippy:connect", self._connect)
    def _built(self, kind):
        try:
            with open(os.path.join(self.build_dir, kind, 'fw_id')) as f:
                return f.read().strip() or None
        except OSError:
            return None
    # ---- 1. the boards' firmware -------------------------------------------
    def _check_boards(self):
        built = {'xlbuddy': self._built('xlbuddy'),
                 'dwarf': self._built('dwarf')}
        boards = {}
        for name, mcu in self.printer.lookup_objects('mcu'):
            short = name.split()[-1]           # 'mcu' or 'dwarf_t2'
            if short == 'mcu':
                label, kind = 'XLBuddy', 'xlbuddy'
            elif short.startswith('dwarf_t'):
                label, kind = 'T' + short[len('dwarf_t'):], 'dwarf'
            else:
                continue
            boards[label] = {'mcu': short, 'kind': kind,
                             'fw': mcu.get_constants().get(CONSTANT),
                             'built': built[kind]}
        self.boards = boards
        for label, b in sorted(boards.items()):
            logging.info("xl_fw_check: %s runs %s, last build %s", label,
                         b['fw'] or "firmware without an ID", b['built'])
        if None in built.values():
            logging.info("xl_fw_check: no firmware build ID in %s - the"
                         " boards are not checked", self.build_dir)
            return
        self.checked = True
        # XLBuddy first, then the tools in order
        order = sorted(boards, key=lambda lb: (lb != 'XLBuddy', lb))
        self.outdated = [lb for lb in order
                         if boards[lb]['fw'] != boards[lb]['built']]
    # ---- 2. an update from Mainsail that needs the installer ---------------
    def _check_update(self):
        cfg = self.printer.get_start_args().get('config_file', '')
        stamp = os.path.join(os.path.dirname(cfg), 'kxlplus', '.installed')
        try:
            with open(stamp) as f:
                vals = dict(l.strip().split('=', 1) for l in f if '=' in l)
        except (OSError, ValueError):
            return
        commit, kxl_dir = vals.get('commit'), vals.get('kxl_dir')
        if not commit or not kxl_dir or commit == 'unknown':
            return
        try:
            r = subprocess.run(['git', '-C', kxl_dir, 'diff', '--name-only',
                                commit, 'HEAD', '--'] + INSTALLER_PATHS,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               universal_newlines=True, timeout=10)
        except (OSError, subprocess.SubprocessError) as e:
            logging.info("xl_fw_check: update check skipped (%s)", e)
            return
        if r.returncode != 0:
            logging.info("xl_fw_check: update check skipped (%s)",
                         r.stderr.strip())
            return
        self.update_pending = [l for l in r.stdout.splitlines() if l]
        if self.update_pending:
            logging.info("xl_fw_check: KlipperXL-Plus moved on from %s - %d"
                         " installer file(s) changed: %s", commit[:8],
                         len(self.update_pending),
                         " ".join(self.update_pending[:20]))
    def _connect(self):
        self._check_boards()
        self._check_update()
        msgs = []
        if self.outdated:
            msgs.append("the firmware on %s is not the one this KlipperXL-Plus"
                        " needs" % (", ".join(self.outdated),))
        if self.update_pending:
            msgs.append("an update still has to be finished by the installer"
                        " (configs / firmware changed)")
        if msgs:
            self.printer.lookup_object('configfile').runtime_warning(
                "KlipperXL-Plus: %s. To finish, %s"
                % ("; ".join(msgs), how_to_start()))
    def get_status(self, eventtime):
        return {'checked': self.checked, 'outdated': list(self.outdated),
                'boards': self.boards,
                'update_pending': list(self.update_pending)}

def load_config(config):
    return XLFwCheck(config)
