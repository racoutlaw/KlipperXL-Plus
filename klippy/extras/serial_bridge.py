# Serial bridge: expose Klipper-flashed Dwarf toolheads as Klipper MCUs
# through the XLBuddy's RS485 bus.
#
# Reconstruction of the lost 2026-03-04 serial_bridge.py (KlipperXL
# multi-tool research, task A), extended for the task B firmware
# (protocol 2, see below).  Written 2026-10-02 against Klipper
# v0.13.0-770 (the owner's host; identical to upstream v0.13.0-786 for
# every file cited here) and the XLBuddy firmware in
# research/multitool/bridge-era-source/modbus_stm32f4.c as patched by
# task B ("fw l.N"; the unpatched 03-02 file is "03-02 l.N").  It passes
# the offline harness in research/multitool/serial-bridge-harness/ and
# has NOT run on hardware.  See RESULT-serial-bridge.md and
# RESULT-fix5.md.
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
# ---------------------------------------------------------------------
# How it fits together
#
#   [mcu dwarf_t0]                 stock Klipper mcu object
#   serial: /tmp/klipper_host_dwarf_t0
#                 |  connect_pipe() (mcu.py:789-791, 866-870)
#                 v
#   PTY slave  <-------------------->  PTY master (this module)
#                                           |  TxFrameSplitter: one Klipper
#                                           |  block per bus transaction
#                                           v
#   BridgeEngine (one per XLBuddy): serial_bridge_append / _write
#                 |  via the XLBuddy's own serialqueue (usb)
#                 v
#   XLBuddy fw: [3 ms gap][0x80|dwarf][block] -> RS485 -> Dwarf
#               <- reply captured until a 500 us byte gap (fw l.2526-
#                  2552), streamed as serial_bridge_read chunks of
#                  <= 54 bytes and closed by serial_bridge_done
#                  (protocol 2, fw l.2072-2095).  Protocol 1 (the 03-02
#                  firmware, no SERIAL_BRIDGE_PROTOCOL constant) is still
#                  spoken: there the block's ack ends the reply.
#
# Dwarf addressing (dwarf-era-source/stm32f0_serial-rs485-addressing.
# patch): a Dwarf boots answering every frame on the bus and is locked
# to 0x80|dwarf_index with set_serial_address once its [mcu] has
# identified it.  The engine therefore brings the Dwarfs up one at a
# time (BridgeEngine._bring_up): boot, publish the PTY, serve only that
# channel until it is identified, lock, next.
#
# Config:
#
#   [serial_bridge]                # optional, one per XLBuddy
#   #mcu: mcu                      # the XLBuddy
#   #poll_interval: 0.100          # service a parked Dwarf at least this often
#   #stream_poll_interval: 0.025   # longest wait for a streaming Dwarf
#   #dwarf_tx_buffer: 192          # Dwarf serial_irq transmit_buf (patch 0007)
#   #poll_margin: 0.5              # re-poll before the buffer is this full
#   #max_retries: 2                # re-send a block the bus refused
#   #dedup_acks: True              # drop a re-sent block's redundant ack
#   #trsync_timeout: 0.100         # multi-MCU homing comm-loss timeout
#
#   [serial_bridge dwarf_t0]
#   dwarf_index: 1                 # 1-5, the RS485 address is 0x80|index
#   #mcu: dwarf_t0                 # the [mcu] section served (default: name)
#   #serial: /tmp/klipper_host_dwarf_t0
#   #boot: firmware                # firmware | none
#   #self_addressed: False         # Dwarf fw 0010 locks itself at boot:
#                                  # brought up without the bus to itself
#
#   [mcu dwarf_t0]
#   serial: /tmp/klipper_host_dwarf_t0
#   # no 'baud:' and no 'restart_method:' - both are unread options for
#   # a /tmp/klipper_host_* path (mcu.py:669-671, 789-791) and Klipper
#   # refuses to start on an unread option (configfile.py:440).
#
# Startup order.  Klipper identifies every [mcu] before klippy:connect
# (klippy.py:131-135), the XLBuddy first (mcu.py:1246-1249), and the
# XLBuddy's init leaves the Dwarfs in their bootloaders (fw l.1222-1233).
# So as soon as the XLBuddy has identified, the engine boots the first
# Dwarf through dwarf_boot_single (fw l.1417-1482) and only then creates
# its /tmp/klipper_host_* symlink; the next Dwarf is booted once the
# first is identified and locked.  [mcu dwarf_tN]'s connect_pipe() finds
# no device until then and retries the open every 5 s
# (serialhdl.py:174-180), exactly as for a USB board that is still
# powering up; its 5 s identify budget (serialhdl.py:97) then starts
# against a live MCU that has the bus to itself.  With 'boot: none' no
# boot command is sent (the Dwarf is assumed to be running already) but
# the symlink is still published in turn, for the same reason.
#
# The three-part fix (recovered-settings.md section 5):
#   1. frame parsing      -> TxFrameSplitter + bridge_chunks (below)
#   2. MIN_RTO = 500 ms   -> REMOVED 2026-10-04.  It covered for link
#                            stalls whose cause was the re-poll timing
#                            (FACTS 11-12).  Stock Klipper serialqueue,
#                            measured: 0 retransmits idle and through hot
#                            probing, a few blocks per Dwarf during the
#                            one-at-a-time startup, no reconnects.  The
#                            chelper patch is gone; Klipper stays stock.
#   3. engine lifecycle   -> there is deliberately NO module-level
#                            singleton.  klippy.py:354-368 builds a new
#                            Reactor and a new Printer on every restart;
#                            the engine is a Printer object made by
#                            printer.load_object() (klippy.py:90-113), so
#                            it is always bound to the live reactor.  The
#                            lost file kept a process-wide singleton and
#                            had to destroy it on the last unregister;
#                            the stock idiom makes that bug impossible.
# ---------------------------------------------------------------------
import os, errno, logging, collections, threading
import pty, tty, fcntl
import mcu, serialhdl, util


######################################################################
# Klipper wire format
######################################################################

# klippy/msgproto.py:13-24 (same values in klippy/chelper/msgblock.h
# and src/command.h)
MESSAGE_MIN = 5
MESSAGE_MAX = 64
MESSAGE_TRAILER_SIZE = 3
MESSAGE_SEQ_MASK = 0x0f
MESSAGE_DEST = 0x10
MESSAGE_SYNC = 0x7e

# XLBuddy bridge limits (fw = bridge-era-source/modbus_stm32f4.c)
BRIDGE_CHUNK_MAX = 48  # bytes of block data per bridge command (02-17 l.22833)
BRIDGE_TX_BUF = 72     # serial_bridge[].tx_buf[72] (fw l.2114), silently
                       # clamped at fw l.2233-2235
BRIDGE_RX_CHUNK = 54   # serial_bridge_read chunk size (fw l.2098)
BRIDGE_RX_BUF = 192    # serial_bridge[].rx_buf[192] (fw l.2111)

# A held partial block that is still incomplete when new bytes arrive
# this long after it was first held is junk, not a partial.  A genuinely
# split write completes within the same kernel flush.  This exists for
# stk500v2_leave() (serialhdl.py:203, 359-360), whose 7 bytes contain a
# plausible header (0e 11 ..) and are followed by a 50 ms pause.  It is
# only reachable on a connect_uart() path, ie when [mcu] has a baud.
STALE_PARTIAL_S = 0.025


def crc16_ccitt(buf):
    # klippy/msgproto.py:29-35
    crc = 0xffff
    for data in buf:
        data ^= crc & 0xff
        data ^= (data & 0x0f) << 4
        crc = ((data << 8) | (crc >> 8)) ^ (data >> 4) ^ (data << 3)
    return crc


def check_block(buf, pos):
    """>0: valid block of that length at buf[pos]; 0: need more bytes;
    -1: not a block.  Same tests as msgproto.MessageParser.check_packet()
    (msgproto.py:244-263) and the MCU's command_find_block()
    (src/command.c:276-297), except the length byte is rejected as soon
    as it is visible."""
    avail = len(buf) - pos
    if avail <= 0:
        return 0
    msglen = buf[pos]
    if msglen < MESSAGE_MIN or msglen > MESSAGE_MAX:
        return -1
    if avail < 2:
        return 0
    if (buf[pos + 1] & 0xf0) != MESSAGE_DEST:
        return -1
    if avail < msglen:
        return 0
    end = pos + msglen
    if buf[end - 1] != MESSAGE_SYNC:
        return -1
    crc = crc16_ccitt(buf[pos:end - MESSAGE_TRAILER_SIZE])
    if buf[end - 3] != (crc >> 8) or buf[end - 2] != (crc & 0xff):
        return -1
    return msglen


######################################################################
# Fix 1: host->Dwarf framing on the PTY read path
######################################################################

class TxFrameSplitter:
    """Byte stream from the PTY master in, whole Klipper blocks out.

    serialqueue writes several blocks per write (serialqueue.c:667-685,
    up to 768 bytes) and a retransmit is one bare 0x7e followed by every
    unacked block (serialqueue.c:435-445).  A block is 5..64 bytes
    (serialqueue.c:492) and 0x7e is NOT escaped: it occurs in payloads
    and CRCs, so boundaries come from the length byte and are
    CRC-checked, never from the sync byte.

    Never forwards a byte that is not part of a CRC-valid block, except
    a bare 0x7e that serialqueue put in front of a retransmit; that one
    is carried on the front of the next block, as serialqueue wrote it.
    The MCU drops a leading sync without a nak (src/command.c:312-316).

    Reconstructed offline by the 2026-10-01 fable session
    (research/multitool/fable-attempt/); copied here unchanged apart
    from line-number citations.
    """

    def __init__(self, name="", stale_s=STALE_PARTIAL_S, log_limit=20):
        self.name = name
        self.stale_s = stale_s
        self._buf = bytearray()
        self._held_since = None
        self._sync_pending = False
        self._log_left = log_limit
        # stats
        self.bytes_in = 0         # == serialqueue bytes_write+bytes_retransmit
        self.bytes_out = 0        #    minus anything tcflush() ate
        self.frames = 0
        self.sync_bytes = 0
        self.discarded = 0
        self.stale_drops = 0
        self.max_held = 0

    def reset(self, reason=""):
        """Drop held bytes (the PTY slave closed: whatever was half-read
        belongs to a dead serialqueue session, the next one restarts at
        seq 1, serialqueue.c:736)."""
        if self._buf:
            self._junk(bytes(self._buf), "reset " + reason)
        self._buf = bytearray()
        self._held_since = None
        self._sync_pending = False

    def feed(self, data, eventtime):
        buf = self._buf
        held_len = len(buf)
        stale = bool(held_len and self._held_since is not None
                     and eventtime - self._held_since > self.stale_s)
        buf.extend(data)
        self.bytes_in += len(data)
        out = []
        n = len(buf)
        pos = 0
        junk_start = -1
        while pos < n:
            if buf[pos] == MESSAGE_SYNC:
                # 0x7e can never be a length byte (126 > MESSAGE_MAX), so
                # at a block boundary it is serialqueue's resync prefix.
                junk_start = self._end_junk(buf, junk_start, pos)
                self._sync_pending = True
                self.sync_bytes += 1
                pos += 1
                continue
            ret = check_block(buf, pos)
            if ret > 0:
                junk_start = self._end_junk(buf, junk_start, pos)
                frame = bytes(buf[pos:pos + ret])
                if self._sync_pending:
                    frame = b'\x7e' + frame
                    self._sync_pending = False
                out.append(frame)
                self.bytes_out += ret
                self.frames += 1
                pos += ret
                continue
            if ret == 0 and not (stale and pos < held_len):
                break  # genuine partial block: keep it for the next read
            # Not a block here (or a stale partial left over from an
            # earlier read).  Slide ONE byte and look again; do not skip
            # to the next 0x7e like msgblock_check() does, because on this
            # lossless input the next real block usually starts right
            # after the junk.
            if ret == 0:
                self.stale_drops += 1
            if junk_start < 0:
                junk_start = pos
            pos += 1
        self._end_junk(buf, junk_start, pos)
        del buf[:pos]
        if buf:
            if pos >= held_len or self._held_since is None:
                self._held_since = eventtime  # partial started in this read
            if len(buf) > self.max_held:
                self.max_held = len(buf)
        else:
            self._held_since = None
        return out

    def _end_junk(self, buf, junk_start, pos):
        if junk_start >= 0 and pos > junk_start:
            self._junk(bytes(buf[junk_start:pos]), "non-block")
        return -1

    def _junk(self, junk, why):
        self.discarded += len(junk)
        if self._log_left > 0:
            self._log_left -= 1
            logging.warning("SerialBridge [%s]: TX dropped %d bytes (%s): %s",
                            self.name, len(junk), why, junk.hex())

    def get_stats(self):
        # invariant: bytes_in == bytes_out + sync + discarded + held
        return ("tx_in=%d tx_out=%d tx_frames=%d tx_sync=%d tx_discarded=%d"
                " tx_stale=%d tx_held=%d tx_max_held=%d" % (
                    self.bytes_in, self.bytes_out, self.frames,
                    self.sync_bytes, self.discarded, self.stale_drops,
                    len(self._buf), self.max_held))


def bridge_chunks(frame, chunk_max=BRIDGE_CHUNK_MAX):
    """One block (<= 64 bytes, 65 with a sync prefix) -> (append_chunks,
    write_chunk).  The write chunk is never empty: data_len == 0 makes
    serial_bridge_write a read-only poll that returns before TX and
    leaves tx_buf loaded for the next write (03-02 l.2126-2139; the
    patched firmware answers it with an empty 'done', fw l.2275-2282)."""
    if not 0 < len(frame) <= MESSAGE_MAX + 1:
        raise ValueError("not a single block: %d bytes" % (len(frame),))
    chunks = [frame[i:i + chunk_max] for i in range(0, len(frame), chunk_max)]
    appends, final = chunks[:-1], chunks[-1]
    if sum(len(c) for c in appends) > BRIDGE_TX_BUF:
        raise ValueError("append would overflow tx_buf")
    return appends, final


######################################################################
# PTY helper
######################################################################

def create_bridge_pty():
    """Create the pseudo-tty a bridged [mcu] opens with connect_pipe().

    Modelled on util.create_pty() (util.py:34-47) and on the Linux host
    mcu's console_setup() (src/linux/console.c:67-105), which is the
    stock example of a Klipper MCU served through a PTY and the reason
    /tmp/klipper_host_* paths get connect_pipe() (mcu.py:789-791).

    Two details matter:
    - The pair is put in raw mode.  console.c:70-72 opens its pty with a
      zeroed termios for the same reason: a cooked tty would rewrite
      0x0a (the length byte of a 10 byte block) as 0d 0a on output and
      treat 0x11/0x13 as flow control on input.
    - Our own slave descriptor stays open, exactly as util.create_pty()
      leaves its sfd open.  While some slave is open the master never
      reports POLLHUP and never reads EIO, so serialhdl closing and
      reopening its slave (serialhdl.py:100, 217-225) is invisible here
      and the reactor does not spin on a hung-up descriptor.
    Returns (master_fd, slave_fd); publish_pty() adds the symlink."""
    mfd, sfd = pty.openpty()
    tty.setraw(sfd)
    os.chmod(os.ttyname(sfd), 0o660)
    util.set_nonblock(mfd)
    for fd in (mfd, sfd):
        fcntl.fcntl(fd, fcntl.F_SETFD, fcntl.FD_CLOEXEC)
    return mfd, sfd


def publish_pty(sfd, path):
    """Point `path` at the slave (util.py:37-42, console.c:89-100)."""
    try:
        os.unlink(path)
    except OSError:
        pass
    os.symlink(os.ttyname(sfd), path)


def remove_stale_link(path):
    """Remove a symlink left at `path` by an earlier klippy.

    KlipperXL-Plus 2026-10-04: unpublish_pty() runs on klippy:disconnect, which
    a service stop does not reach, so the five /tmp/klipper_host_dwarf_*
    links outlived klippy (measured).  The new run's PTYs get the same
    /dev/pts numbers, so the old link already pointed at the new, unbooted
    channel: [mcu dwarf_t0] connected at once, its identify blocks were
    dropped ("not booted") while the modular bed and Dwarf 1 booted, and
    every start logged "Timeout on connect" (6 of 6 on 10/04; 0 of 1 with
    the links removed by hand).  Only a symlink is removed."""
    try:
        if os.path.islink(path):
            os.unlink(path)
            logging.info("SerialBridge: removed stale link %s", path)
    except OSError:
        pass


def unpublish_pty(sfd, path):
    """Remove `path` if it still points at our slave."""
    try:
        if os.readlink(path) == os.ttyname(sfd):
            os.unlink(path)
    except OSError:
        pass


######################################################################
# Bridge engine - one per XLBuddy, owns the bus
######################################################################

# What an idle Dwarf is sent so that it gets a turn to talk.  A Dwarf
# can only transmit while it is the addressed device (03-02 l.2170), and
# serial_bridge_write with no data never transmits (fw l.2275-2282), so
# draining its queued responses needs at least one byte on the wire.  A
# bare sync byte is ignored by the Dwarf's parser without a nak
# (src/command.c:312-316; with <5 bytes queued it simply waits,
# command.c:281-282) and costs 43 us of bus time.
POLL_FRAME = bytes([MESSAGE_SYNC])

# How a serialqueue session begins: its first block has sequence number 1
# (serialqueue.c:736) and carries 'identify offset=0 ...', message id 1
# (msgproto.py:9-11, sent by serialhdl.py:73).  Byte for byte that is
# [len=8][0x11][msgid 1][offset 0][count]...; the first four bytes never
# occur elsewhere in a session.  Seeing them without a sync prefix means
# the host started over, so everything remembered about what the host
# has been told is stale.
SESSION_START = bytes([8, MESSAGE_DEST | 1, 1, 0])

BRIDGE_APPEND_FMT = "serial_bridge_append dwarf=%c data=%*s"   # fw l.2240
BRIDGE_WRITE_FMT = "serial_bridge_write dwarf=%c data=%*s"     # fw l.2569
BRIDGE_READ_FMT = "serial_bridge_read dwarf=%c data=%*s"       # fw l.2197
BRIDGE_DONE_FMT = "serial_bridge_done dwarf=%c status=%c total=%c"  # fw l.2205
BOOT_CMD_FMT = "dwarf_boot_single dwarf=%c"                    # fw l.1482
BOOT_RESP_FMT = "dwarf_boot_single_response status=%c addr=%c" # fw l.1479
# Dwarf side (dwarf-era-source/stm32f0_serial-rs485-addressing.patch
# l.97-103, 121-125): my_address is compared with the wire address byte,
# so the argument is 0x80|dwarf_index; 0, the boot value, accepts every
# frame on the bus.
LOCK_CMD_FMT = "set_serial_address address=%u"
BOOT_RETRY_S = 5.0
READY_POLL_S = 0.100
# Protocol 2: how long to wait for serial_bridge_done.  The firmware's
# longest path is 50 ms of bus claim + 3 ms gap + ~3 ms TX + 15 ms
# capture cap (fw l.2311, 2327, 2181); the rest is USB and the XLBuddy
# link's own retransmit timer.
DONE_TIMEOUT_S = 0.500
# How long a freshly booted Dwarf gets the bus to itself before it is
# released unlocked: its [mcu] retries the open for 90 s
# (serialhdl.py:171-180) and then has 5 s to identify (serialhdl.py:97).
IDENTIFY_TIMEOUT_S = 100.0
LOCK_ACK_TIMEOUT_S = 5.0
# After a Dwarf's clock sync has connected, the next Dwarf is booted only
# once this one's frequency estimate has settled (owner 10/13; the March
# 5-tool build's "clock-sync convergence window").  A boot right after a
# connect delayed the new Dwarf's first get_clock samples and its estimate
# read low at Klipper's ready check (mcu.py _ready, 1 % / MHz rounding):
# 74 starts 10/10-10/13, the Dwarf booted just before the last read 55.81-
# 55.95 MHz, once 55.33 = "configured for 56Mhz but running at 55Mhz"; the
# crystal measures 56.000.  Settled = this many NEW estimates in a row
# (clocksync.py _handle_clock replaces clock_est per accepted sample; the
# one connect() leaves is CLOCK_FREQ itself) within the tolerance.
CLOCK_SETTLE_TOL = 0.001
CLOCK_SETTLE_SAMPLES = 2
CLOCK_SETTLE_TIMEOUT_S = 10.0

# serial_bridge_done status values (fw l.2103-2108) plus one of our own
# for "no done came back".  Everything but DONE_OK means nothing (or not
# all) of the frame went out and tx_buf is empty: re-send the block whole.
DONE_OK = 0
DONE_BAD_DWARF = 1
DONE_REFUSED = 2
DONE_DROPPED = 3
DONE_BUS_BUSY = 4
DONE_TX_FAIL = 5
DONE_TIMEOUT = -1
DONE_NAMES = {DONE_OK: 'ok', DONE_BAD_DWARF: 'bad_dwarf',
              DONE_REFUSED: 'refused', DONE_DROPPED: 'dropped',
              DONE_BUS_BUSY: 'bus_busy', DONE_TX_FAIL: 'tx_fail',
              DONE_TIMEOUT: 'timeout'}


class BridgeEngine:
    def __init__(self, config):
        self.printer = printer = config.get_printer()
        self.reactor = printer.get_reactor()
        self.mcu_name = config.get('mcu', 'mcu')
        self.mcu = mcu.get_printer_mcu(printer, self.mcu_name)
        # A Dwarf's sensor reports wait in its buffer until it is serviced,
        # and Klipper times a heater update 0.300 s after the report it is
        # based on (heaters.py pwm_delay = report time) - so the longest gap
        # between a parked Dwarf's services is taken out of that 0.300 s.
        # Measured 10/11 (gap meter, full pin run, all tools hot): at 0.250
        # a parked Dwarf was serviced every ~0.26 s, worst 0.324 s, on a bus
        # ~40% busy; "MCU 'dwarf_t0' shutdown: Timer too close" when T0's
        # heater update was based on a report 0.296 s old.  0.100: a gap of
        # ~0.10 s + the up to ~0.07 s seen behind other jobs leaves the
        # heater >= ~0.13 s, for ~+10% of the bus.
        self.poll_interval = config.getfloat('poll_interval', 0.100, above=0.)
        # KlipperXL-Plus 2026-10-03: a Dwarf streaming its loadcell (~2.5 KB/s)
        # fills its 192-byte transmit buffer in ~75 ms, and its bulk data
        # comes in batches, so a poll between batches is empty.  Waiting
        # poll_interval after such an empty reply let the buffer fill and
        # serial_irq drop messages - T0's temperature reports, then
        # "Missed scheduling of next digital out event" (measured: all 199
        # waits >75 ms followed an empty T0 poll).  A streaming channel
        # waits at most stream_poll_interval (set_stream_poll), also before
        # its rate has been measured.  0.025 s, measured 10/04: a streaming
        # T0 averages 2.4 KB/s but bursts to 4.7 KB/s, and the gap between
        # its polls is the wait plus ~10 ms of other jobs; with 0.040 s the
        # worst poll held 192 of 192 bytes, with 0.025 s 146 (p99 123).
        self.stream_poll_interval = config.getfloat(
            'stream_poll_interval', 0.025, above=0.)
        # KlipperXL-Plus 2026-10-04: an idle Dwarf is not quiet - its sensors
        # report every 0.1-0.3 s, ~660 B/s, in bursts, so a poll right
        # after a drain is empty.  Waiting the full poll_interval (0.25 s)
        # after such a reply let the 192-byte transmit buffer fill and
        # serial_irq drop reports (measured, 40 s idle: 25-36 full replies
        # per Dwarf, each after an empty poll + ~256 ms; T0/T3 dock reports
        # lost).  A channel is re-polled before its measured byte rate can
        # fill poll_margin of the buffer (_repoll_wait).
        self.dwarf_tx_buffer = config.getint('dwarf_tx_buffer', 192,
                                             minval=16)
        self.poll_margin = config.getfloat('poll_margin', 0.5, above=0.,
                                           maxval=1.)
        self.max_retries = config.getint('max_retries', 2, minval=0)
        self.retry_delay = config.getfloat('retry_delay', 0.010, minval=0.)
        self.boot_settle = config.getfloat('boot_settle', 0.500, minval=0.)
        self.dedup_acks = config.getboolean('dedup_acks', True)
        # Multi-MCU homing / probing over the bridge.  Klipper's
        # TRSYNC_TIMEOUT (mcu.py, 0.025 s) assumes MCUs on direct USB; a
        # Dwarf behind the RS485 bridge cannot renew trsync within 25 ms
        # under probe load: "MCU 'dwarf_t0' shutdown: Timer too close" on
        # the first G28 Z tap (10/03, and again 10/04 with every bus fix
        # in place).  It is the comm-loss safety stop, not the trigger
        # latency (the trigger position comes from the MCU clock): 0.100 s
        # = <= 0.5 mm extra travel at 5 mm/s on a dead link.
        # TriggerDispatch.start() reads the module constant each time a
        # move starts, so it is raised here at load instead of patching
        # mcu.py - Klipper's tracked files stay pristine (Moonraker "dirty",
        # owner 10/04).  If a Klipper update drops the constant, refuse to
        # load rather than probe with a 25 ms timeout.
        trsync = config.getfloat('trsync_timeout', 0.100, minval=0.025)
        if not hasattr(mcu, 'TRSYNC_TIMEOUT'):
            raise config.error(
                "serial_bridge: klippy/mcu.py has no TRSYNC_TIMEOUT - Klipper"
                " changed; bridged Dwarf homing needs it (see serial_bridge)")
        if mcu.TRSYNC_TIMEOUT < trsync:
            logging.info("serial_bridge: TRSYNC_TIMEOUT %.3f -> %.3f s"
                         " (bridged MCUs)", mcu.TRSYNC_TIMEOUT, trsync)
            mcu.TRSYNC_TIMEOUT = trsync
        self.channels = collections.OrderedDict()
        self._rr_pos = 0
        # Serializes every host-originated use of the RS485 bus.  The
        # XLBuddy only arbitrates one way round: a bridge write waits up
        # to 50 ms for the bus (fw l.2301-2324) but a modbus frame sent
        # while the bridge holds it fails at once with MODBUS_ERR_BUSY
        # (fw l.551-552).  puppy_bootloader should take this mutex
        # around its modbus sends; see RESULT-serial-bridge.md.
        self.bus_mutex = self.reactor.mutex()
        self._append_cmd = self._write_cmd = self._boot_cmd = None
        self._commands_ready = False
        self.protocol = 1
        self._rx_lock = threading.Lock()
        self._rx_pending = []
        self._inflight = None       # (dwarf, completion), under _rx_lock
        # The channel being brought up: while set, no other channel gets
        # the bus (its Dwarf is in accept-all mode, see _bring_up)
        self._exclusive = None
        self._dead = None
        self._timer = self.reactor.register_timer(self._arbiter_cb)
        self._ready_timer = self.reactor.register_timer(
            self._ready_cb, self.reactor.monotonic() + READY_POLL_S)
        # stats
        self.tx_transactions = self.tx_polls = self.tx_retries = 0
        self.rx_chunks = self.rx_bytes = 0
        self.rx_orphans = self.rx_misrouted = self.rx_incomplete = 0
        self.done_timeouts = self.done_orphans = 0
        printer.register_event_handler("klippy:disconnect",
                                       self._handle_disconnect)
        gcode = printer.lookup_object('gcode')
        gcode.register_command("SERIAL_BRIDGE_STATUS",
                               self.cmd_SERIAL_BRIDGE_STATUS,
                               desc=self.cmd_SERIAL_BRIDGE_STATUS_help)
    # Channel registry
    def register_channel(self, channel):
        if channel.reactor is not self.reactor:
            # Cannot happen when the engine is a Printer object; kept as
            # a loud guard against reintroducing a module-level cache.
            raise self.printer.config_error(
                "serial_bridge engine is bound to a stale reactor")
        if channel.dwarf_index in [c.dwarf_index
                                   for c in self.channels.values()]:
            raise self.printer.config_error(
                "serial_bridge: dwarf_index %d used twice"
                % (channel.dwarf_index,))
        self.channels[channel.name] = channel
    def unregister_channel(self, channel):
        self.channels.pop(channel.name, None)
    def get_bus_mutex(self):
        return self.bus_mutex
    def kick(self):
        # No-op while the arbiter callback is paused inside a
        # transaction (reactor.py:140-142); it re-checks the queues
        # before returning, so nothing is lost.
        self.reactor.update_timer(self._timer, self.reactor.NOW)
    def _channel_by_index(self, dwarf):
        for ch in self.channels.values():
            if ch.dwarf_index == dwarf:
                return ch
        return None
    def _ordered_channels(self):
        """Channels in the order klippy identifies their [mcu]s: the
        klippy:mcu_identify handlers run in registration order
        (klippy.py:131, mcu.py:777), ie the order of the [mcu] sections.
        Bringing the Dwarfs up in any other order would have klippy
        waiting in one Dwarf's connect_pipe() while the engine waits for
        another Dwarf's identify that klippy has not started."""
        names = [n for n, o in self.printer.lookup_objects(module='mcu')]
        def key(ch):
            n = 'mcu ' + ch.mcu_name
            return (names.index(n) if n in names else len(names),
                    ch.dwarf_index)
        return sorted(self.channels.values(), key=key)
    # XLBuddy command setup.  The engine is created at config time,
    # before any MCU has identified; an mcu_identify handler of our own
    # would run after every [mcu]'s (klippy.py:131, mcu.py:797), ie
    # after the bridged ones already tried to connect.  So the engine
    # polls for the XLBuddy's dictionary instead and sets up as soon as
    # it is there.
    def _setup_commands(self):
        if self._commands_ready:
            return self._dead is None
        constants = self.mcu.get_constants()
        if not constants:
            return False        # XLBuddy has not identified yet
        self._commands_ready = True
        self._append_cmd = self.mcu.try_lookup_command(BRIDGE_APPEND_FMT)
        self._write_cmd = self.mcu.try_lookup_command(BRIDGE_WRITE_FMT)
        if self._append_cmd is None or self._write_cmd is None:
            self._dead = ("XLBuddy mcu '%s' has no serial_bridge commands"
                          % (self.mcu_name,))
            logging.error("SerialBridge: %s", self._dead)
            return False
        # Protocol 1 is the 03-02 firmware (no constant): the reply is
        # complete when the write's block is acked.  Protocol 2
        # (fw l.2072-2095) ends every reply with serial_bridge_done.
        self.protocol = int(constants.get('SERIAL_BRIDGE_PROTOCOL', 1))
        # mcu.py has no public accessor for its SerialReader and
        # MCU.register_serial_response() defers registration until the
        # config is finalized (mcu.py:139-143), which is after we need it.
        # mcu.py:220 reaches into _serial the same way.
        serial = self.mcu._serial
        serial.register_response(self._handle_read, 'serial_bridge_read')
        if self.protocol >= 2:
            serial.register_response(self._handle_done, 'serial_bridge_done')
        try:
            self._boot_cmd = self.mcu.lookup_query_command(
                BOOT_CMD_FMT, BOOT_RESP_FMT)
        except serial.get_msgparser().error:
            self._boot_cmd = None
            logging.warning("SerialBridge: XLBuddy firmware has no"
                            " dwarf_boot_single; Dwarfs must already be"
                            " running Klipper")
        logging.info("SerialBridge: XLBuddy '%s' bridge protocol %d",
                     self.mcu_name, self.protocol)
        return True
    # Response path (runs in serialhdl's background thread,
    # serialhdl.py:41-66)
    def _handle_read(self, params):
        with self._rx_lock:
            self._rx_pending.append((params['dwarf'], params['data']))
    def _handle_done(self, params):
        with self._rx_lock:
            inflight = self._inflight
        if inflight is None or inflight[0] != params['dwarf']:
            self.done_orphans += 1
            return
        self.reactor.async_complete(inflight[1], params)
    def _take_rx(self):
        with self._rx_lock:
            chunks = self._rx_pending
            self._rx_pending = []
        return chunks
    def _route(self, chunks, dwarf=None):
        """Hand chunks for other Dwarfs to their channels (they are bus
        bytes those Dwarfs sent, late for their own transaction) and
        return the data for `dwarf`."""
        parts = []
        for dw, d in chunks:
            if dw == dwarf:
                parts.append(d)
                continue
            if dwarf is None:
                self.rx_orphans += 1
            else:
                self.rx_misrouted += 1
            ch = self._channel_by_index(dw)
            if ch is not None and d:
                ch.deliver(d)
        self.rx_chunks += len(parts)
        return b''.join(parts)
    # Arbiter
    def set_stream_poll(self, mcu_name):
        """The Dwarf serving [mcu <mcu_name>] is streaming (None: none)."""
        for ch in self.channels.values():
            ch.streaming = mcu_name is not None and ch.mcu_name == mcu_name
        logging.info("serial_bridge: streaming %s", mcu_name or "none")
        self.kick()
    def _repoll_wait(self, ch):
        """Wait before re-polling a channel whose reply was under
        poll_margin of the buffer: until its measured byte rate would fill
        poll_margin, capped at stream_poll_interval (streaming) or
        poll_interval."""
        cap = self.stream_poll_interval if ch.streaming else self.poll_interval
        if ch.rate <= 0.:
            return cap
        return min(cap, self.poll_margin * self.dwarf_tx_buffer / ch.rate)
    def _pick_channel(self, eventtime):
        if self._exclusive is not None:
            ch = self._exclusive
            if ch.txq or (ch.active and ch.booted
                          and eventtime >= ch.next_poll_time):
                return ch
            return None
        chans = list(self.channels.values())
        n = len(chans)
        if not n:
            return None
        order = [chans[(self._rr_pos + i) % n] for i in range(n)]
        for i, ch in enumerate(order):
            if ch.txq:
                self._rr_pos = (self._rr_pos + i + 1) % n
                return ch
        for i, ch in enumerate(order):
            if ch.active and ch.booted and eventtime >= ch.next_poll_time:
                self._rr_pos = (self._rr_pos + i + 1) % n
                return ch
        return None
    def _next_wake(self):
        chans = self.channels.values()
        if self._exclusive is not None:
            chans = [self._exclusive]
        times = [ch.next_poll_time for ch in chans
                 if ch.active and ch.booted]
        if not times:
            return self.reactor.NEVER
        return min(times)
    def _ready_cb(self, eventtime):
        """Bring the Dwarfs up, one at a time, once the XLBuddy can be
        talked to."""
        if self._dead is not None:
            return self.reactor.NEVER
        if not self._setup_commands():
            return eventtime + READY_POLL_S
        self.kick()     # frames may have queued while we waited
        try:
            busy = self._bring_up()
        except (serialhdl.error, self.printer.command_error) as e:
            self._dead = str(e)
            logging.warning("SerialBridge: XLBuddy link lost: %s", e)
            return self.reactor.NEVER
        if not busy:
            return self.reactor.NEVER
        return self.reactor.monotonic() + READY_POLL_S
    def _bring_up(self):
        """One Dwarf at a time: boot it, publish its PTY, give it the bus
        alone until its [mcu] has identified it, lock it to its address,
        then the next.  Returns True while a channel is still on the way.

        A Dwarf running the recovered RS485 firmware boots in accept-all
        mode and answers every frame on the bus until set_serial_address
        locks it (dwarf-era-source patch l.53-63, 153-157); the command
        can only be sent once the host has the Dwarf's dictionary, ie
        after identify.  Two unlocked Dwarfs, or one unlocked Dwarf and
        traffic for a locked one, collide.  So while a channel is in
        'identify' or 'lock' it is the only one the arbiter serves; the
        others' serialqueues see a stall of one boot + identify + lock;
        stock serialqueue retransmits a few blocks per Dwarf over the
        whole startup and never reconnects (measured 2026-10-04)."""
        for ch in self._ordered_channels():
            if ch.state == 'ready':
                continue
            if ch.state == 'boot':
                # Claim the bus before the reset pulse: from then on the
                # Dwarf answers every frame.  A Dwarf whose boot failed
                # is still in its bootloader, which does not parse
                # Klipper frames, so the bus is handed back until the
                # retry.
                # A self-addressed Dwarf answers only its own address from
                # its first byte, so the others keep being served while it
                # is brought up (KlipperXL-Plus 2026-10-04: the exclusive
                # bring-up starved the running Dwarfs' clock sync - the
                # 57-59 MHz startup warning).  The boot itself still holds
                # bus_mutex (_boot).
                if not ch.self_addressed:
                    self._exclusive = ch
                if not self._boot(ch, self.reactor.monotonic()):
                    self._exclusive = None
                    self.kick()
                    return True
                ch.state = 'identify'
                ch.state_deadline = self.reactor.monotonic() + IDENTIFY_TIMEOUT_S
                ch.publish()
                self.kick()
                return True
            if ch.state == 'identify':
                if ch.mcu_obj.get_constants():
                    ch.state = 'lock'
                elif self.reactor.monotonic() > ch.state_deadline:
                    logging.warning("SerialBridge [%s]: Dwarf %d was not"
                                    " identified within %.0f s; releasing"
                                    " the bus with the Dwarf unlocked",
                                    ch.name, ch.dwarf_index,
                                    IDENTIFY_TIMEOUT_S)
                    ch.identify_timeouts += 1
                    self._release(ch)
                    continue
                else:
                    return True
            if ch.state == 'lock':
                if ch.self_addressed:
                    self._check_self_address(ch)
                self._lock(ch)
                # Keep serving this Dwarf until Klipper has FINISHED connecting
                # it: mcu.py _attach() runs connect_pipe() (identify) and THEN
                # clocksync.connect() (get_uptime + 8 get_clock samples,
                # clocksync.py:33-52), which only sets get_clock_cmd at its
                # end (clocksync.py:17, 49).  Releasing at identify starved the
                # clock sync while the next Dwarf was brought up exclusively,
                # and klippy (which connects MCUs one after another) deadlocked
                # - two-tool test, 2026-10-02.
                ch.state = 'sync'
                ch.state_deadline = self.reactor.monotonic() + IDENTIFY_TIMEOUT_S
            if ch.state == 'sync':
                cs = getattr(ch.mcu_obj, '_clocksync', None)
                if getattr(cs, 'get_clock_cmd', None) is not None:
                    # connected: the other Dwarfs are served again (the
                    # lock is done), the next boot waits for the estimate
                    ch.state = 'settle'
                    ch.state_deadline = (self.reactor.monotonic()
                                         + CLOCK_SETTLE_TIMEOUT_S)
                    ch.settle_start = self.reactor.monotonic()
                    ch.settle_est = cs.clock_est
                    ch.settle_good = 0
                    if self._exclusive is ch:
                        self._exclusive = None
                        self.kick()
                if self.reactor.monotonic() > ch.state_deadline:
                    logging.warning("SerialBridge [%s]: Dwarf %d clock sync not"
                                    " finished within %.0f s; releasing the bus",
                                    ch.name, ch.dwarf_index, IDENTIFY_TIMEOUT_S)
                    self._release(ch)
                    continue
                return True
            if ch.state == 'settle':
                cs = ch.mcu_obj._clocksync
                est = cs.clock_est
                if est is not ch.settle_est:          # a new accepted sample
                    ch.settle_est = est
                    if abs(est[2] - cs.mcu_freq) <= CLOCK_SETTLE_TOL * cs.mcu_freq:
                        ch.settle_good += 1
                    else:
                        ch.settle_good = 0
                now = self.reactor.monotonic()
                if ch.settle_good >= CLOCK_SETTLE_SAMPLES:
                    logging.info("SerialBridge [%s]: Dwarf %d clock settled at"
                                 " %.4f MHz after %.1f s", ch.name,
                                 ch.dwarf_index, est[2] / 1e6,
                                 now - ch.settle_start)
                    self._release(ch)
                    continue
                if now > ch.state_deadline:
                    logging.warning("SerialBridge [%s]: Dwarf %d clock not"
                                    " settled within %.0f s (%.4f MHz);"
                                    " going on", ch.name, ch.dwarf_index,
                                    CLOCK_SETTLE_TIMEOUT_S, est[2] / 1e6)
                    self._release(ch)
                    continue
                return True
        return False
    def _release(self, ch):
        ch.state = 'ready'
        if self._exclusive is ch:
            self._exclusive = None
        self.kick()
    def _check_self_address(self, ch):
        """A channel configured self_addressed must run firmware that
        reports the bootloader address it locked to (patch 0009
        get_boot_address = 0x0A + dwarf_index).  Anything else means it
        booted in accept-all mode and was brought up without the bus to
        itself: say so loudly."""
        expect = 0x0A + ch.dwarf_index
        if ch.mcu_obj.try_lookup_command("get_boot_address") is None:
            ch.self_address_ok = False
            logging.error("SerialBridge [%s]: self_addressed is set but Dwarf"
                          " %d firmware has no get_boot_address (patches"
                          " 0009/0010): it booted in accept-all mode",
                          ch.name, ch.dwarf_index)
            return
        query = ch.mcu_obj.lookup_query_command("get_boot_address",
                                                "boot_address address=%c")
        result = {}
        def do_query(eventtime):
            try:
                result['addr'] = query.send()['address']
            except (serialhdl.error, self.printer.command_error) as e:
                result['error'] = str(e)
            return True
        completion = self.reactor.register_callback(do_query)
        completion.wait(self.reactor.monotonic() + LOCK_ACK_TIMEOUT_S)
        addr = result.get('addr')
        ch.self_address_ok = (addr == expect)
        if ch.self_address_ok:
            logging.info("SerialBridge [%s]: Dwarf %d self-addressed (boot"
                         " address 0x%02x)", ch.name, ch.dwarf_index, addr)
        else:
            logging.error("SerialBridge [%s]: self_addressed is set but Dwarf"
                          " %d reports boot address %s (expected 0x%02x): it"
                          " booted in accept-all mode", ch.name,
                          ch.dwarf_index,
                          ("0x%02x" % addr) if addr is not None
                          else result.get('error', 'no reply'), expect)
    def _lock(self, ch):
        """Lock the Dwarf to its wire address.  Sent through the Dwarf's
        own [mcu] (it is a Dwarf command, it has to be encoded with the
        Dwarf's dictionary) and so through this bridge; waits for the
        Dwarf's ack so the next Dwarf is only released into accept-all
        mode once this one no longer answers to everything."""
        cmd = ch.mcu_obj.try_lookup_command(LOCK_CMD_FMT)
        if cmd is None:
            ch.lock_missing = True
            logging.warning("SerialBridge [%s]: Dwarf %d firmware has no"
                            " '%s'; it stays in accept-all mode, which is"
                            " only safe with one Dwarf on the bus",
                            ch.name, ch.dwarf_index, LOCK_CMD_FMT)
            return
        addr = 0x80 | ch.dwarf_index
        def do_lock(eventtime):
            try:
                cmd.send_wait_ack([addr])
            except (serialhdl.error, self.printer.command_error) as e:
                logging.warning("SerialBridge [%s]: lock send failed: %s",
                                ch.name, e)
                return False
            return True
        completion = self.reactor.register_callback(do_lock)
        ok = completion.wait(self.reactor.monotonic() + LOCK_ACK_TIMEOUT_S,
                             waketime_result=False)
        if ok is not True:
            ch.lock_failures += 1
            logging.warning("SerialBridge [%s]: Dwarf %d did not ack"
                            " set_serial_address within %.1f s",
                            ch.name, ch.dwarf_index, LOCK_ACK_TIMEOUT_S)
            return
        ch.locked = True
        logging.info("SerialBridge [%s]: Dwarf %d locked to address 0x%02x",
                     ch.name, ch.dwarf_index, addr)
    def _arbiter_cb(self, eventtime):
        if self._dead is not None:
            return self.reactor.NEVER
        if not self._setup_commands():
            # bytes on a PTY before the XLBuddy identified
            return eventtime + READY_POLL_S
        while True:
            ch = self._pick_channel(eventtime)
            if ch is None:
                return self._next_wake()
            try:
                self._service(ch, eventtime)
            except (serialhdl.error, self.printer.command_error) as e:
                self._dead = str(e)
                logging.warning("SerialBridge: XLBuddy link lost: %s", e)
                return self.reactor.NEVER
            eventtime = self.reactor.monotonic()
    def _service(self, ch, eventtime):
        if not ch.booted:
            # Unreachable: the PTY is only published after the boot
            ch.drop_queue("not booted")
            return
        if ch.txq:
            frame = ch.txq.popleft()
            is_poll = False
            block = frame[1:] if frame[0] == MESSAGE_SYNC else frame
            if len(block) == len(frame) and block.startswith(SESSION_START):
                # New serialqueue session: its acks start from scratch
                ch.sent_history.clear()
                ch.last_delivered_ack_seq = None
                ch.sessions += 1
            is_resend = len(block) < len(frame) or block in ch.sent_history
            ch.sent_history.append(block)
        else:
            frame = POLL_FRAME
            is_poll = True
            is_resend = False
            self.tx_polls += 1
            ch.polls += 1
        data = self._transact(ch, frame, is_poll)
        # A Dwarf can only send when addressed and holds at most
        # dwarf_tx_buffer bytes (serial_irq transmit_buf, patch 0007).  A
        # reply of poll_margin of that or more means this poll came late
        # and more may be waiting: due again at once (the round-robin in
        # _pick_channel still serves the others in turn).  Otherwise wait
        # until the channel's rate would fill poll_margin (_repoll_wait).
        # KlipperXL-Plus 2026-10-04: re-polling at once after ANY data took
        # 2.4 KB/s from a streaming T0 in ~52-byte pieces at 73 polls/s
        # (41% of the bus; every poll costs ~5-6 ms however small).
        now = self.reactor.monotonic()
        ch.note_rx(len(data), now)
        ch.note_service(len(data), now, self.dwarf_tx_buffer)
        if len(data) >= self.poll_margin * self.dwarf_tx_buffer:
            ch.next_poll_time = now
        else:
            ch.next_poll_time = now + self._repoll_wait(ch)
        if is_resend and self.dedup_acks and self._is_redundant_ack(ch, data):
            # serialqueue's duplicate-ack rule (serialqueue.c:291-295)
            # assumes a re-sent block's nak reaches the host before the
            # host sends anything new, which holds on a direct link
            # (the nak is read in the same batch as the ack ahead of it)
            # but not when every block is its own bus transaction: by
            # the time the nak arrives the next block is in flight, the
            # nak is honoured as a fast retransmit, and that re-sent
            # block draws the next nak - one extra transaction per
            # block, for as long as traffic flows.  A bare ack carrying
            # the same sequence number as the last bare ack the host was
            # handed is exactly what serialqueue calls a duplicate ack
            # (serialqueue.c:289-295, last_ack_seq), so it is dropped;
            # any reply with data, or an ack with a newer sequence
            # number, is delivered.  Only bare acks count: a data block
            # also advances the host's receive_seq, but a send_wait_ack
            # completion is released by an ack message alone
            # (serialqueue.c:277), so if the firmware's RX window cut
            # the ack off a reply the host still needs the re-sent
            # block's nak.
            ch.dup_acks_dropped += 1
            data = b''
        if data:
            ch.deliver(data)
    def _is_redundant_ack(self, ch, data):
        if len(data) != MESSAGE_MIN or ch.last_delivered_ack_seq is None:
            return False
        if check_block(data, 0) != MESSAGE_MIN:
            return False
        return (data[1] & MESSAGE_SEQ_MASK) == ch.last_delivered_ack_seq
    def _transact(self, ch, frame, is_poll):
        """One bus transaction: one block (or the poll byte) out, the
        Dwarf's reply back.  Returns the reply bytes."""
        appends, final = bridge_chunks(frame)
        attempt = 0
        with self.bus_mutex:
            while True:
                self._route(self._take_rx())    # late bytes for anyone
                if self.protocol >= 2:
                    status, data = self._exchange_v2(ch, appends, final)
                else:
                    status, data = self._exchange_v1(ch, appends, final,
                                                     attempt)
                self.tx_transactions += 1
                ch.transactions += 1
                self.rx_bytes += len(data)
                if status == DONE_OK and (data or is_poll):
                    return data
                if status != DONE_OK:
                    ch.not_sent[DONE_NAMES.get(status, str(status))] += 1
                # A valid block always draws at least an ack from the
                # Dwarf (src/command.c:369-372), so an empty reply means
                # the block never made it: the XLBuddy gave the bus up
                # after 50 ms (fw l.2311-2319), bailed out of a wedged
                # TX (fw l.2463-2488), refused a write that arrived
                # while another was in flight (fw l.2258-2263) or the
                # Dwarf is silent.  Resending the same block is safe
                # (the Dwarf dedups by sequence number, command.c:301-
                # 305) and far cheaper than waiting for serialqueue's
                # retransmit timeout.
                if (attempt >= self.max_retries
                        or self.printer.is_shutdown()):
                    ch.empty_replies += 1
                    return data
                attempt += 1
                self.tx_retries += 1
                ch.retries += 1
                self.reactor.pause(self.reactor.monotonic()
                                   + self.retry_delay)
    def _exchange_v1(self, ch, appends, final, attempt):
        """Protocol 1 (03-02 firmware).  Both commands go on the default
        command queue, which serialqueue drains FIFO (serialqueue.c:482-
        497); tx_buf is per Dwarf.  send_wait_ack() returns only after
        the XLBuddy acked the block carrying the write, and that ack is
        sent after the command ran (src/command.c:364-374, usb_cdc.c:120)
        and after every serial_bridge_read it produced was queued ahead
        of it; serialqueue releases the notify on that ack, not on the
        data (serialqueue.c:271-286).  So when this returns the whole
        reply has already passed through _handle_read().

        A retry sends the final chunk only.  The bus-busy return of that
        firmware (03-02 l.2152-2154) happens before l.2234 clears tx_pos,
        so the appended head is still in tx_buf and a second append
        would double it (and hit the clamp).  After a tx_bailout (tx_pos
        cleared) or a silent Dwarf the tail alone is garbage to the
        Dwarf: it naks, and serialqueue's nak retransmit (serialqueue.c:
        293-295) recovers."""
        dwarf = ch.dwarf_index
        if attempt == 0:
            for chunk in appends:
                self._append_cmd.send([dwarf, chunk])
        self._write_cmd.send_wait_ack([dwarf, final])
        return DONE_OK, self._route(self._take_rx(), dwarf)
    def _exchange_v2(self, ch, appends, final):
        """Protocol 2 (fw l.2072-2095).  The write is answered by chunks
        and then serial_bridge_done; the block's ack is no longer the
        end of the reply, because the firmware can dispatch the host's
        later blocks from inside the command (BRIDGE_PUMP_USB_OUT) and
        ack them first.
        Every attempt re-sends the whole block: on every status but
        DONE_OK the firmware has cleared tx_buf (fw l.2258-2263, 2288-
        2293, 2311-2319, 2479)."""
        dwarf = ch.dwarf_index
        completion = self.reactor.completion()
        with self._rx_lock:
            self._inflight = (dwarf, completion)
        try:
            for chunk in appends:
                self._append_cmd.send([dwarf, chunk])
            self._write_cmd.send([dwarf, final])
            params = completion.wait(self.reactor.monotonic()
                                     + DONE_TIMEOUT_S)
        finally:
            with self._rx_lock:
                self._inflight = None
        data = self._route(self._take_rx(), dwarf)
        if params is None:
            self.done_timeouts += 1
            ch.done_timeouts += 1
            return (DONE_OK if data else DONE_TIMEOUT), data
        status = params['status']
        if status == DONE_OK and params['total'] != len(data):
            # A chunk was lost between the firmware and us (console_sendf
            # drops a message transmit_buf cannot hold, usb_cdc.c:70-74);
            # the Dwarf link's own CRC and retransmit cover the gap.
            self.rx_incomplete += 1
            ch.rx_incomplete += 1
        return status, data
    def _boot(self, ch, eventtime):
        """Get the Dwarf's Klipper application running.

        Needed once per Printer: the XLBuddy's init releases all Dwarfs
        into their bootloaders and nothing starts the application
        (fw l.1222-1233; puppy_boot_sequence() only runs on the
        'puppy_boot' command, fw l.1407-1412).  A fresh reset pulse here
        is also what makes FIRMWARE_RESTART work: the host's 'reset' to
        a bridged mcu is sent after the reactor loop has ended
        (klippy.py:193-195) so nothing relays it, and mcu.py:1063-1066
        refuses an mcu that still reports is_config after a firmware
        restart."""
        if ch.boot_method == 'none':
            ch.booted = True
            return True
        if eventtime < ch.boot_retry_time:
            return False
        puppy = self.printer.lookup_object('xl_puppies', None)
        host_boot = getattr(puppy, 'boot_dwarf_for_bridge', None)
        if host_boot is None:
            # The XLBuddy's own dwarf_boot_single is never used: it blocks
            # the MCU for > 1 s (delay_ms(1000) + the fingerprint polls,
            # fw command_dwarf_boot_single) with no watchdog_reset(), the
            # IWDG (src/stm32/watchdog.c:27, 410-512 ms) resets the board
            # and the host sees "Got EOF" (hardware test 1, 2026-10-02,
            # logs/klippy-fw3-dwarf_boot_single-EOF.log).  The boot is
            # the host-driven Prusa bootloader sequence in
            # puppy_bootloader.boot_dwarf_for_bridge (hardware-ready/).
            if not ch.boot_failures:
                logging.error("SerialBridge [%s]: cannot boot Dwarf %d:"
                              " puppy_bootloader.boot_dwarf_for_bridge is"
                              " not available (hardware-ready"
                              " puppy_bootloader.py needed); the firmware"
                              " dwarf_boot_single is not used, it trips"
                              " the XLBuddy watchdog", ch.name,
                              ch.dwarf_index)
            ch.boot_failures += 1
            ch.boot_retry_time = eventtime + BOOT_RETRY_S
            return False
        logging.info("SerialBridge [%s]: booting Dwarf %d", ch.name,
                     ch.dwarf_index)
        with self.bus_mutex:
            # Host-driven Prusa bootloader sequence (puppy_bootloader):
            # same frames and waits as its _auto_boot_dwarfs, every other
            # Dwarf and the modular bed held in reset meanwhile.
            # Bed first (Prusa order): boot the modular bed once, before the first
            # bridged Dwarf, so no bootloader traffic runs on a bus with live
            # Klipper Dwarfs (hardware test 2026-10-02: T4 went deaf during the
            # connect-time bed boot).
            bed_boot = getattr(puppy, 'boot_bed_for_bridge', None)
            if (bed_boot is not None and not getattr(self, '_bed_boot_tried', False)
                    and not getattr(puppy, 'modular_bed_booted', False)):
                self._bed_boot_tried = True
                bed_boot()
            # never reset a bridged Dwarf that is already running (two-tool fix)
            keep = 0
            for other in self.channels.values():
                if other is not ch and other.booted:
                    keep |= 1 << other.dwarf_index
            ok = host_boot(ch.dwarf_index, keep)
            params = {'status': 0 if ok else 1, 'addr': 0x0A + ch.dwarf_index}
        status = params['status']
        if status == 0:
            ch.booted = True
            ch.boots += 1
            # Let the application start and configure its uart
            self.reactor.pause(self.reactor.monotonic() + self.boot_settle)
            return True
        ch.boot_failures += 1
        ch.boot_retry_time = eventtime + BOOT_RETRY_S
        logging.warning("SerialBridge [%s]: Dwarf %d boot failed status=%d"
                        " (addr=0x%02x)", ch.name, ch.dwarf_index, status,
                        params.get('addr', 0))
        return False
    # Lifecycle
    def _handle_disconnect(self):
        self._dead = "disconnected"
        self.reactor.unregister_timer(self._timer)
        self.reactor.unregister_timer(self._ready_timer)
    # Reporting
    def get_status(self, eventtime=None):
        return {'protocol': self.protocol,
                'transactions': self.tx_transactions,
                'polls': self.tx_polls,
                'retries': self.tx_retries,
                'rx_bytes': self.rx_bytes,
                'channels': {ch.name: ch.get_status()
                             for ch in self.channels.values()}}
    def stats(self, eventtime):
        parts = ["serial_bridge: protocol=%d tx_transactions=%d tx_polls=%d"
                 " tx_retries=%d rx_chunks=%d rx_bytes=%d rx_orphans=%d"
                 " rx_misrouted=%d rx_incomplete=%d done_timeouts=%d"
                 " done_orphans=%d" % (
                     self.protocol, self.tx_transactions, self.tx_polls,
                     self.tx_retries, self.rx_chunks, self.rx_bytes,
                     self.rx_orphans, self.rx_misrouted, self.rx_incomplete,
                     self.done_timeouts, self.done_orphans)]
        # Channels have no stats() of their own: statistics.py:60-61
        # polls every printer object that has one, and the channel
        # lines belong with the engine's.
        for ch in self.channels.values():
            parts.append(ch.stats_line(eventtime))
        return False, '\n'.join(parts)
    cmd_SERIAL_BRIDGE_STATUS_help = "Report serial bridge counters"
    def cmd_SERIAL_BRIDGE_STATUS(self, gcmd):
        gcmd.respond_info(self.stats(self.reactor.monotonic())[1])


######################################################################
# Bridge channel - one per Dwarf
######################################################################

class BridgeChannel:
    def __init__(self, config):
        self.printer = printer = config.get_printer()
        self.reactor = printer.get_reactor()
        self.name = config.get_name().split()[-1]
        self.dwarf_index = config.getint('dwarf_index', minval=1, maxval=5)
        self.mcu_name = config.get('mcu', self.name)
        self.serial_path = config.get('serial',
                                      '/tmp/klipper_host_' + self.name)
        self.boot_method = config.getchoice(
            'boot', {'firmware': 'firmware', 'none': 'none'}, 'firmware')
        # The Dwarf firmware locks itself to the address the Prusa
        # bootloader assigned it (Dwarf patches 0009/0010): no accept-all
        # window, so its bring-up does not need the bus to itself.
        self.self_addressed = config.getboolean('self_addressed', False)
        self.self_address_ok = None     # BridgeEngine._check_self_address
        self.engine = printer.load_object(config, 'serial_bridge')
        self.mcu_obj = self._check_mcu_section()
        self.splitter = TxFrameSplitter(self.name)
        self.txq = collections.deque()
        self.active = False       # a session has written to the PTY
        self.booted = False
        self.published = False    # symlink exists
        # boot -> identify -> lock -> sync -> settle -> ready
        # (BridgeEngine._bring_up)
        self.state = 'boot'
        self.state_deadline = 0.
        self.locked = False
        self.lock_missing = False
        self.next_poll_time = 0.
        self.streaming = False    # BridgeEngine.set_stream_poll
        # measured reply byte rate (B/s), over windows of >= 1 s
        self.rate = 0.
        self._rate_start = None
        self._rate_bytes = 0
        self.boot_retry_time = 0.
        # Blocks sent recently (a retransmit is byte-identical to its
        # original) and the sequence number of the last bare ack handed
        # to the host, for the redundant-ack rule in the engine
        self.sent_history = collections.deque(maxlen=32)
        self.last_delivered_ack_seq = None
        # stats
        self.transactions = self.polls = self.retries = self.empty_replies = 0
        self.boots = self.boot_failures = self.dup_acks_dropped = 0
        self.rx_bytes = self.rx_dropped = self.frames_dropped = 0
        self.rx_incomplete = self.done_timeouts = 0
        self.lock_failures = self.identify_timeouts = 0
        self.not_sent = collections.Counter()
        self.sessions = 0
        # Service timing - measurement only, changes nothing (owner 10/11:
        # "MCU 'dwarf_t0' shutdown: Timer too close" with T0 last serviced
        # 0.296 s earlier while T3 was probing; a Dwarf's sensor reports wait
        # in its buffer until it is serviced, and Klipper times a heater
        # update 0.300 s after the report it is based on).  Per stats line:
        # the longest gap between services and the fullest reply; running
        # totals of gaps over 0.100 / 0.250 s and of full-buffer replies.
        self.last_service = None
        self.gap_max = 0.
        self.gaps_100 = self.gaps_250 = 0
        self.reply_max = 0
        self.replies_full = 0
        # nothing may reach this channel before publish() (after its boot)
        remove_stale_link(self.serial_path)
        self.mfd, self.sfd = create_bridge_pty()
        self.fd_handle = self.reactor.register_fd(self.mfd, self._pty_read_cb)
        self.engine.register_channel(self)
        printer.register_event_handler("klippy:disconnect",
                                       self._handle_disconnect)
        if not self.serial_path.startswith('/tmp/klipper_host_'):
            logging.warning("SerialBridge [%s]: serial '%s' is a"
                            " connect_uart() path (mcu.py:789-791); [mcu]"
                            " needs a baud and every connect attempt"
                            " writes 7 stk500v2 bytes that the splitter"
                            " will discard", self.name, self.serial_path)
    def note_service(self, nbytes, now, buffer_size):
        if self.last_service is not None:
            gap = now - self.last_service
            if gap > self.gap_max:
                self.gap_max = gap
            if gap > 0.100:
                self.gaps_100 += 1
            if gap > 0.250:
                self.gaps_250 += 1
        self.last_service = now
        if nbytes > self.reply_max:
            self.reply_max = nbytes
        if nbytes >= buffer_size:
            self.replies_full += 1
    def note_rx(self, nbytes, now):
        if self._rate_start is None:
            self._rate_start = now
        self._rate_bytes += nbytes
        if now - self._rate_start >= 1.0:
            self.rate = self._rate_bytes / (now - self._rate_start)
            self._rate_start = now
            self._rate_bytes = 0
    def _check_mcu_section(self):
        mcu_obj = self.printer.lookup_object('mcu ' + self.mcu_name, None)
        if mcu_obj is None:
            raise self.printer.config_error(
                "serial_bridge %s: no [mcu %s] section"
                % (self.name, self.mcu_name))
        helper = getattr(mcu_obj, '_conn_helper', None)
        if helper is None:
            return mcu_obj
        serialport, baud = helper.get_serialport()
        if serialport != self.serial_path:
            raise self.printer.config_error(
                "serial_bridge %s: [mcu %s] serial is '%s', expected '%s'"
                % (self.name, self.mcu_name, serialport, self.serial_path))
        return mcu_obj
    def publish(self):
        if not self.published and self.sfd is not None:
            publish_pty(self.sfd, self.serial_path)
            self.published = True
    # PTY read path (reactor thread)
    def _pty_read_cb(self, eventtime):
        try:
            data = os.read(self.mfd, 4096)
        except OSError as e:
            if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return
            if e.errno == errno.EIO:
                # Only possible if our slave descriptor were closed
                self.splitter.reset("slave closed")
                return
            raise
        if not data:
            return
        self.active = True
        frames = self.splitter.feed(data, eventtime)
        if frames:
            self.txq.extend(frames)
            self.engine.kick()
    # Response delivery (reactor thread, from the engine)
    def deliver(self, data):
        self.rx_bytes += len(data)
        remaining = data
        while remaining:
            try:
                n = os.write(self.mfd, remaining)
            except OSError as e:
                if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    # Nobody is reading the slave: the session is gone.
                    # Stop polling until it writes to us again.
                    self.rx_dropped += len(remaining)
                    self.active = False
                    self.last_delivered_ack_seq = None
                    return
                raise
            remaining = remaining[n:]
        # Remember the last complete bare ack (5-byte block) the host now
        # has; its sequence number is what serialqueue keeps as
        # last_ack_seq (serialqueue.c:289-292).
        pos = 0
        n = len(data)
        while pos < n:
            if data[pos] == MESSAGE_SYNC:
                pos += 1
                continue
            ret = check_block(data, pos)
            if ret <= 0:
                break
            if ret == MESSAGE_MIN:
                self.last_delivered_ack_seq = data[pos + 1] & MESSAGE_SEQ_MASK
            pos += ret
    def drop_queue(self, reason):
        if self.txq:
            self.frames_dropped += len(self.txq)
            logging.info("SerialBridge [%s]: dropped %d queued frames (%s)",
                         self.name, len(self.txq), reason)
            self.txq.clear()
    # Lifecycle
    def _handle_disconnect(self):
        self.engine.unregister_channel(self)
        if self.fd_handle is not None:
            self.reactor.unregister_fd(self.fd_handle)
            self.fd_handle = None
        if self.published:
            unpublish_pty(self.sfd, self.serial_path)
            self.published = False
        for fd in (self.mfd, self.sfd):
            try:
                os.close(fd)
            except OSError:
                pass
        self.mfd = self.sfd = None
    # Reporting
    def get_status(self, eventtime=None):
        return {'dwarf_index': self.dwarf_index, 'booted': self.booted,
                'published': self.published, 'state': self.state,
                'locked': self.locked, 'sessions': self.sessions,
                'self_addressed': self.self_addressed,
                'self_address_ok': self.self_address_ok,
                'active': self.active, 'transactions': self.transactions,
                'polls': self.polls, 'dup_acks_dropped': self.dup_acks_dropped,
                'retries': self.retries, 'empty_replies': self.empty_replies,
                'not_sent': dict(self.not_sent),
                'rx_incomplete': self.rx_incomplete,
                'done_timeouts': self.done_timeouts,
                'queued': len(self.txq), 'rx_bytes': self.rx_bytes,
                'rx_dropped': self.rx_dropped, 'tx_stats': self.splitter.get_stats()}
    def stats_line(self, eventtime):
        not_sent = ','.join('%s:%d' % kv for kv in sorted(self.not_sent.items()))
        line = ("serial_bridge %s: dwarf=%d state=%s locked=%d booted=%d"
                " published=%d active=%d sessions=%d queued=%d"
                " transactions=%d polls=%d retries=%d empty_replies=%d"
                " not_sent=%s rx_incomplete=%d done_timeouts=%d"
                " dup_acks_dropped=%d boots=%d boot_failures=%d"
                " lock_failures=%d rx_bytes=%d rx_dropped=%d"
                " frames_dropped=%d gap_max=%.3f gaps_100=%d gaps_250=%d"
                " reply_max=%d replies_full=%d %s" % (
                    self.name, self.dwarf_index, self.state, self.locked,
                    self.booted, self.published, self.active, self.sessions,
                    len(self.txq), self.transactions, self.polls,
                    self.retries, self.empty_replies, not_sent or '-',
                    self.rx_incomplete, self.done_timeouts,
                    self.dup_acks_dropped, self.boots, self.boot_failures,
                    self.lock_failures, self.rx_bytes, self.rx_dropped,
                    self.frames_dropped, self.gap_max, self.gaps_100,
                    self.gaps_250, self.reply_max, self.replies_full,
                    self.splitter.get_stats()))
        # per stats line (the totals keep running)
        self.gap_max = 0.
        self.reply_max = 0
        return line


def load_config(config):
    return BridgeEngine(config)

def load_config_prefix(config):
    return BridgeChannel(config)
