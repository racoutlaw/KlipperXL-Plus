#!/usr/bin/env python3
# KlipperXL-Plus - flash a Prusa XL Dwarf's application through the XLBuddy
#
# Runs with Klipper STOPPED.  Talks to the XLBuddy (KlipperXL-Plus firmware:
# xlplus_rs485.c rs485_frame_*, xlplus_resets.c xl_reset_lines) with
# Klipper's own serialhdl, and drives the Prusa Dwarf bootloader exactly as
# Prusa does (include/puppies/BootloaderProtocol.hpp,
# src/puppies/BootloaderProtocol.cpp):
#   frame [addr][cmd][data][crc16-modbus LE], reply [addr][status][len][data][crc]
#   WRITE_FLASH 0x06 [offset u32 BE][<= 247 B], FINALIZE_FLASH 0x07,
#   READ_FLASH 0x08 [offset u32 BE][len], GET_HARDWARE_INFO 0x03 (11 B:
#   hw_type, hw_rev u16, bl_version u32, application_size u32), Dwarf
#   hw_type 42 (include/puppies/puppy_constants.hpp:162-166).
# Offsets are inside the application area the bootloader owns (the app runs
# at 0x08002000).  The bootloader itself is never written - it is what
# executes these commands.
#
# Per tool: hold all in reset, release only this Dwarf, assign its address,
# check it is a Dwarf, BACK UP the whole application area to a file, write,
# finalize, read back and compare every byte.  The Dwarf is left in its
# bootloader; the next Klipper start boots it.
#
#   ~/klippy-env/bin/python xl_flash_dwarf.py --tool 0 --bin klipper-dwarf.bin
#   ~/klippy-env/bin/python xl_flash_dwarf.py --scan     (read only: which
#       tools answer; prints "TOOLS: 0 1 2" and "XLBUDDY_FW: <id>" for the
#       installer)
#
# The XLBuddy is found as /dev/serial/by-id/usb-Klipper_stm32f407xx_*
# (KlipperXL-Plus firmware); --serial overrides.
#
# Copyright (C) 2026  Richard Crook
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import argparse, glob, logging, os, sys, threading, time, hashlib

sys.path.insert(0, os.path.expanduser('~/klipper/klippy'))
import reactor, serialhdl   # noqa: E402

XLBUDDY_GLOB = '/dev/serial/by-id/usb-Klipper_stm32f407xx_*'
HOLD_ALL = 0xBE                 # xlplus_resets.c: Dwarfs 1-5 + bed held
BL_GET_PROTOCOL_VERSION, BL_SET_ADDRESS, BL_GET_HARDWARE_INFO = 0x00, 0x01, 0x03
BL_WRITE_FLASH, BL_FINALIZE_FLASH, BL_READ_FLASH = 0x06, 0x07, 0x08
DWARF_HW_TYPE = 42
WRITE_BLOCK = 247               # Prusa MAX_FLASH_BLOCK_LENGTH
READ_BLOCK = 240
FRAME_LOAD_MAX = 48
FRAME_OK, FRAME_TIMEOUT = 0, 1


def crc16_modbus(data):
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc

def frame(addr, cmd, data=b''):
    f = bytes([addr, cmd]) + bytes(data)
    c = crc16_modbus(f)
    return f + bytes([c & 0xFF, c >> 8])


class FlashError(Exception):
    pass


class XLBuddy:
    def __init__(self, r, port):
        self.r = r
        self.port = port
        self.ser = None
        self._lock = threading.Lock()
        self._rx = bytearray()
        self._done = None
    def connect(self):
        self.ser = serialhdl.SerialReader(self.r)
        self.ser.connect_uart(self.port, 250000)
        mp = self.ser.get_msgparser()
        cfg = self.ser.send_with_response("get_config", "config")
        if cfg['is_shutdown']:
            print("XLBuddy is in shutdown - resetting it")
            self.ser.send("reset")
            self.r.pause(self.r.monotonic() + 0.5)
            self.ser.disconnect()
            self.r.pause(self.r.monotonic() + 4.)
            return self.connect()
        self.cq = self.ser.get_default_command_queue()
        # KlipperXL-Plus XLBuddy firmware has the rs485_frame_* bridge commands;
        # released KlipperXL (or any other Klipper) firmware on the XLBuddy
        # shows up as the same usb-Klipper_stm32f407xx device but lacks them
        try:
            mp.lookup_command("rs485_frame_load data=%*s")
        except Exception:
            raise FlashError("NOT_KXLPLUS_FW: the XLBuddy runs Klipper"
                             " firmware without the KlipperXL-Plus RS485 bridge -"
                             " flash the KlipperXL-Plus .bbf from a USB stick first")
        # the KlipperXL-Plus firmware ID (build_xlbuddy_pi.sh; None = built
        # before the ID existed) - printed for the installer's version check
        self.fw_id = mp.get_constants().get('KXLPLUS_FW')
        self.f_load = mp.lookup_command("rs485_frame_load data=%*s")
        self.f_send = mp.lookup_command("rs485_frame_send boot=%c data=%*s")
        self.f_reset = mp.lookup_command("xl_reset_lines value=%c")
        self.ser.register_response(self._handle_read, 'rs485_frame_read')
        self.ser.register_response(self._handle_done, 'rs485_frame_done')
        self.ser.register_response(self._handle_done, 'xl_reset_lines_result')
    def _handle_read(self, params):
        with self._lock:
            self._rx.extend(params['data'])
    def _handle_done(self, params):
        with self._lock:
            done = self._done
        if done is not None:
            self.r.async_complete(done, params)
    def _send(self, fmt, args):
        with self._lock:
            self._rx = bytearray()
            self._done = self.r.completion()
            done = self._done
        self.ser.raw_send(fmt.encode(args), 0, 0, self.cq)
        return done
    def resets(self, value):
        params = self._send(self.f_reset, [value]).wait(
            self.r.monotonic() + 1.)
        if params is None or params['status'] != 0:
            raise FlashError("xl_reset_lines 0x%02x failed: %s" % (value, params))
    def transact(self, data, boot=True):
        last = ((len(data) - 1) // FRAME_LOAD_MAX) * FRAME_LOAD_MAX
        with self._lock:
            self._rx = bytearray()
        for pos in range(0, last, FRAME_LOAD_MAX):
            self.ser.raw_send(self.f_load.encode(
                [data[pos:pos + FRAME_LOAD_MAX]]), 0, 0, self.cq)
        params = self._send(self.f_send, [1 if boot else 0, data[last:]]).wait(
            self.r.monotonic() + 2.)
        with self._lock:
            self._done = None
            rx = bytes(self._rx)
        if params is None:
            return FRAME_TIMEOUT, b''
        status = params['status']
        if status == FRAME_OK and params['total'] != len(rx):
            status = 2
        return status, rx


class Dwarf:
    def __init__(self, xl, tool):
        self.xl = xl
        self.dwarf = tool + 1
        self.addr = 0x0A + self.dwarf
    def bl(self, cmd, data=b'', retries=3):
        last = None
        for attempt in range(retries):
            status, rx = self.xl.transact(frame(self.addr, cmd, data))
            if status == FRAME_OK and len(rx) >= 5 and rx[0] == self.addr:
                if rx[1] != 0:
                    raise FlashError("bootloader cmd 0x%02x refused, status %d"
                                     % (cmd, rx[1]))
                return rx[3:3 + rx[2]]
            last = (status, rx[:8].hex())
            self.xl.r.pause(self.xl.r.monotonic() + 0.05)
        raise FlashError("bootloader cmd 0x%02x: no valid reply %s" % (cmd, last))
    def enter_bootloader(self):
        self.xl.resets(HOLD_ALL)
        self.xl.r.pause(self.xl.r.monotonic() + 0.05)
        self.xl.resets(HOLD_ALL & ~(1 << self.dwarf))
        self.xl.r.pause(self.xl.r.monotonic() + 0.5)
        self.xl.transact(frame(0x00, BL_SET_ADDRESS, [self.addr, 0]),
                         boot=False)         # no reply expected
        self.xl.r.pause(self.xl.r.monotonic() + 0.05)
        ver = self.bl(BL_GET_PROTOCOL_VERSION)
        hw = self.bl(BL_GET_HARDWARE_INFO)
        if len(hw) != 11:
            raise FlashError("hardware info %d bytes, expected 11" % len(hw))
        self.hw_type = hw[0]
        self.app_size = int.from_bytes(hw[7:11], 'big')
        print("  bootloader protocol 0x%04x, hw_type %d rev %d, bl_version %d,"
              " application_size %d" % (int.from_bytes(ver, 'big'), hw[0],
              int.from_bytes(hw[1:3], 'big'), int.from_bytes(hw[3:7], 'big'),
              self.app_size))
        if self.hw_type != DWARF_HW_TYPE:
            raise FlashError("hw_type %d is not a Dwarf (%d) - refusing"
                             % (self.hw_type, DWARF_HW_TYPE))
    def read(self, length):
        out = bytearray()
        for off in range(0, length, READ_BLOCK):
            n = min(READ_BLOCK, length - off)
            data = self.bl(BL_READ_FLASH, off.to_bytes(4, 'big') + bytes([n]))
            if len(data) != n:
                raise FlashError("READ_FLASH at %d: %d bytes" % (off, len(data)))
            out += data
        return bytes(out)
    def write(self, fw):
        for off in range(0, len(fw), WRITE_BLOCK):
            self.bl(BL_WRITE_FLASH, off.to_bytes(4, 'big') + fw[off:off + WRITE_BLOCK])
            if off % (WRITE_BLOCK * 40) == 0:
                print("  written %d / %d" % (off, len(fw)))
        print("  written %d / %d" % (len(fw), len(fw)))
        self.bl(BL_FINALIZE_FLASH, retries=1)   # never resend a finalize


def flash_tool(xl, tool, fw, backup_dir):
    d = Dwarf(xl, tool)
    print("T%d (Dwarf %d, bootloader address 0x%02x)" % (tool, d.dwarf, d.addr))
    d.enter_bootloader()
    if len(fw) > d.app_size:
        raise FlashError("firmware %d B larger than application area %d B"
                         % (len(fw), d.app_size))
    old = d.read(d.app_size)
    path = os.path.join(backup_dir, "dwarf%d-T%d-app-backup-%s.bin" % (
        d.dwarf, tool, time.strftime("%Y%m%d-%H%M%S")))
    with open(path, 'wb') as f:
        f.write(old)
    print("  backup %s (%d B, md5 %s)" % (path, len(old),
                                         hashlib.md5(old).hexdigest()[:8]))
    d.write(fw)
    back = d.read(len(fw))
    if back != fw:
        bad = next(i for i in range(len(fw)) if back[i] != fw[i])
        raise FlashError("READ-BACK MISMATCH at offset %d" % bad)
    print("  verified %d B (md5 %s) - T%d left in its bootloader"
          % (len(fw), hashlib.md5(fw).hexdigest()[:8], tool))
    xl.resets(HOLD_ALL)


def scan_tools(xl):
    """Read only: which Dwarfs answer their bootloader (hw_type 42).
    Nothing is written; every Dwarf is left held in reset, as after a
    flash, and the next Klipper start boots them."""
    found = []
    for tool in range(5):
        d = Dwarf(xl, tool)
        print("T%d (Dwarf %d, bootloader address 0x%02x)" % (tool, d.dwarf,
                                                              d.addr))
        try:
            d.enter_bootloader()
            found.append(tool)
            print("  present")
        except FlashError as e:
            print("  not found (%s)" % (e,))
        xl.resets(HOLD_ALL)
    return found

def find_xlbuddy():
    ports = sorted(glob.glob(XLBUDDY_GLOB))
    if len(ports) != 1:
        raise SystemExit(
            "Expected exactly one XLBuddy running KlipperXL-Plus firmware (%s),"
            " found %d: %s\n  Flash the XLBuddy .bbf first, or pass --serial."
            % (XLBUDDY_GLOB, len(ports), ports))
    return ports[0]

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--tool', type=int, action='append',
                    choices=range(5), help='tool 0-4, repeatable')
    ap.add_argument('--bin')
    ap.add_argument('--scan', action='store_true',
                    help='read only: list the tools that answer')
    ap.add_argument('--backup-dir', default=os.path.expanduser(
        '~/kxlplus-dwarf-backups'))
    ap.add_argument('--serial', default=None)
    args = ap.parse_args()
    if not args.scan and (not args.tool or not args.bin):
        ap.error("flashing needs --tool and --bin (or use --scan)")
    logging.basicConfig(level=logging.WARNING)
    if args.serial is None:
        args.serial = find_xlbuddy()
    print("XLBuddy: %s" % (args.serial,))
    fw = None
    if not args.scan:
        fw = open(args.bin, 'rb').read()
        os.makedirs(args.backup_dir, exist_ok=True)
        print("firmware %s: %d B, md5 %s" % (args.bin, len(fw),
                                             hashlib.md5(fw).hexdigest()[:8]))
    r = reactor.Reactor()
    result = {'ok': False}
    def run(eventtime):
        xl = XLBuddy(r, args.serial)
        try:
            xl.connect()
            print("XLBUDDY_FW: %s" % (xl.fw_id or "none",))
            if args.scan:
                found = scan_tools(xl)
                print("TOOLS: %s" % (" ".join(str(t) for t in found),))
                result['ok'] = bool(found)
                return
            for tool in args.tool:
                flash_tool(xl, tool, fw, args.backup_dir)
            result['ok'] = True
        except FlashError as e:
            print("FAILED: %s" % (e,))
            try:
                xl.resets(HOLD_ALL)
            except Exception:
                pass
        finally:
            if xl.ser is not None:
                xl.ser.disconnect()
            r.end()
    r.register_callback(run)
    r.run()
    print("DONE" if result['ok'] else "NOT DONE")
    sys.exit(0 if result['ok'] else 1)

if __name__ == '__main__':
    main()
