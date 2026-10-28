# KlipperXL-Plus - XL "puppies" on the XLBuddy RS485 bus: reset lines, Prusa
# bootloader boots (modular bed, Dwarfs) and the modular bed (stock Prusa
# firmware, MODBUS 0x1A).
#
# Written fresh for KlipperXL-Plus.  Facts (frames, waits, registers, bedlet
# map, adaptive heating) come from the KlipperXL code that ran on the
# printer (klippy/reference/puppy_bootloader.py, cited "ref l.N") and from
# Prusa-Firmware-Buddy where noted.  Talks to the XLBuddy firmware in
# firmware/xlbuddy/: xlplus_rs485.c (rs485_frame_*) and xlplus_resets.c
# (xl_reset_lines).  The Dwarfs themselves are Klipper MCUs through
# serial_bridge.py, which calls boot_bed_for_bridge() and
# boot_dwarf_for_bridge() here.
#
# Config:
#   [xl_puppies]
#   #mcu: mcu                  # the XLBuddy
#   #bed_poll_interval: 1.0    # bed watchdog is 30 s
#
# Copyright (C) 2026  Richard Crook
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging, math, random, threading

# Prusa bootloader (FACTS.md section 4, ref l.1796-1801, 1861-1865)
BL_GET_PROTOCOL_VERSION = 0x00
BL_SET_ADDRESS = 0x01
BL_START_APPLICATION = 0x05
BL_GET_FINGERPRINT = 0x0E
BL_COMPUTE_FINGERPRINT = 0x0F
ADDR_DEFAULT = 0x00
ADDR_FIRST_BOOT = 0x0A          # bed 0x0A, Dwarf N 0x0A+N
ADDR_MODULAR_BED = 0x1A
ADDR_XL_CAN_BOOT = 0x13         # Prusa XL+ only: its XL-CAN board's bootloader

XL_PLUS_MSG = ("This printer is a Prusa XL+ (its XL-CAN board answered on the"
               " bus). KlipperXL-Plus is for the original Prusa XL only, so it"
               " started neither the bed nor the tools. Put stock Prusa"
               " firmware back on the XLBuddy.")

# Reset lines (PCA9557 via xl_reset_lines; HIGH = held, FACTS.md section 5)
ALL_DWARF_BITS = 0x3E
BED_BIT = 0x80

# rs485_frame_done status (xlplus_rs485.c)
FRAME_OK, FRAME_TIMEOUT, FRAME_CRC, FRAME_REFUSED, FRAME_TX_FAIL = 0, 1, 2, 3, 5
FRAME_LOAD_MAX = 48

# MODBUS
MB_READ_INPUT = 0x04
MB_WRITE_COIL = 0x05
MB_WRITE_MULTIPLE = 0x10

# Modular bed registers (ref l.1874-1884, Prusa modular_bed registers)
NUM_BEDLETS = 16
BED_REG_TARGET_TEMP = 0xE010
BED_REG_MEASURED_TEMP = 0xA010
BED_REG_FAULT_STATUS = 0x8000
BED_REG_BEDLET_FAULT = 0xA000
BED_TEMP_SCALE = 10
BED_COIL_CLEAR_FAULT = 0x4000
BED_COIL_RESET_OVERCURRENT = 0x4001
BED_MAX_TEMP = 120.
BED_MIN_INTERVAL = 0.1          # between bed transactions (ref l.2078)

# Bed fault bits (ref l.1912-1932, Prusa modular_bed_errors.hpp)
BED_FAULTS = {0x0001: "PREHEAT_ERROR", 0x0002: "MINTEMP", 0x0004: "MAXTEMP",
              0x0008: "THERMAL_RUNAWAY", 0x0010: "BEDLET_ERROR",
              0x0020: "OVERCURRENT", 0x0040: "UNEXPECTED_CURRENT"}
BED_CRITICAL = 0x0008 | 0x0004 | 0x0020
BEDLET_FAULTS = {0x0001: "HEATER_DISCONNECTED", 0x0002: "SHORT_CIRCUIT",
                 0x0004: "TEMP_BELOW_MIN", 0x0008: "TEMP_ABOVE_MAX",
                 0x0010: "TEMP_DROP", 0x0020: "TEMP_PEAK", 0x0040: "PREHEAT",
                 0x0080: "TEST_HEATING"}
BEDLET_CRITICAL = 0x0002 | 0x0008

# Bedlet wiring: grid (row, col) -> register index.  Prusa modular_bed.cpp
# l.344 (1-based there).  Row 0 = front (Y 0-90), col 0 = left (X 0-90).
BEDLET_MAP = [[6, 7, 8, 9],
              [5, 4, 11, 10],
              [2, 3, 12, 13],
              [1, 0, 15, 14]]
BEDLET_GRID = {BEDLET_MAP[r][c]: (c, r) for r in range(4) for c in range(4)}
BEDLET_SIZE = 90.
# Prusa defaults (ref l.2085-2087)
GRADIENT_CUTOFF = 2.0
GRADIENT_EXPONENT = 2.0


def crc16_modbus(data):
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc

def build_frame(addr, cmd, data=()):
    frame = bytes([addr, cmd] + list(data))
    crc = crc16_modbus(frame)
    return frame + bytes([crc & 0xFF, crc >> 8])


######################################################################
# Raw frames over the XLBuddy (bed MODBUS + bootloader)
######################################################################

class RawBus:
    def __init__(self, printer, mcu):
        self.printer = printer
        self.reactor = printer.get_reactor()
        self.mcu = mcu
        self.mutex = self.reactor.mutex()
        self._lock = threading.Lock()
        self._rx = bytearray()
        self._done = None
        self._ready = False
        self._load_cmd = self._send_cmd = self._reset_cmd = None
    def _setup(self):
        if self._ready:
            return
        self._load_cmd = self.mcu.lookup_command("rs485_frame_load data=%*s")
        self._send_cmd = self.mcu.lookup_command(
            "rs485_frame_send boot=%c data=%*s")
        self._reset_cmd = self.mcu.lookup_query_command(
            "xl_reset_lines value=%c",
            "xl_reset_lines_result status=%c value=%c", is_async=True)
        # Needed before the XLBuddy is configured (serial_bridge boots the
        # Dwarfs during mcu_identify); same access as serial_bridge.py.
        serial = self.mcu._serial
        serial.register_response(self._handle_read, 'rs485_frame_read')
        serial.register_response(self._handle_done, 'rs485_frame_done')
        self._stats_cmd = self.mcu.lookup_command("rs485_stats")
        serial.register_response(self._handle_stats, 'rs485_stats_a')
        serial.register_response(self._handle_stats, 'rs485_stats_b')
        self._ready = True
    # Engine diagnostics (xlplus_rs485.c rs485_stats)
    stats = {}
    def _handle_stats(self, params):
        with self._lock:
            self.stats[params['#name']] = {
                k: v for k, v in params.items() if not k.startswith('#')}
    def request_stats(self):
        self._setup()
        self._stats_cmd.send([])
    def stats_text(self):
        with self._lock:
            a = self.stats.get('rs485_stats_a', {})
            b = self.stats.get('rs485_stats_b', {})
        return " ".join("%s=%s" % kv for kv in list(a.items())
                        + list(b.items()))
    # Background thread (serialhdl)
    def _handle_read(self, params):
        with self._lock:
            self._rx.extend(params['data'])
    def _handle_done(self, params):
        with self._lock:
            done = self._done
        if done is not None:
            self.reactor.async_complete(done, params)
    def transact(self, frame, boot=False, retries=3):
        """Send one frame, return (status, reply bytes)."""
        self._setup()
        with self.mutex:
            for attempt in range(retries):
                with self._lock:
                    self._rx = bytearray()
                    self._done = self.reactor.completion()
                    done = self._done
                # Frames longer than one message go in pieces first
                last = ((len(frame) - 1) // FRAME_LOAD_MAX) * FRAME_LOAD_MAX
                for pos in range(0, last, FRAME_LOAD_MAX):
                    self._load_cmd.send([frame[pos:pos + FRAME_LOAD_MAX]])
                self._send_cmd.send([1 if boot else 0, frame[last:]])
                params = done.wait(self.reactor.monotonic() + 2.0)
                with self._lock:
                    self._done = None
                    rx = bytes(self._rx)
                if params is None:
                    return FRAME_TIMEOUT, b''
                status = params['status']
                if status == FRAME_REFUSED:       # MCU queue full - retry
                    self.reactor.pause(self.reactor.monotonic() + 0.010)
                    continue
                if status == FRAME_OK and params['total'] != len(rx):
                    status = FRAME_CRC            # a chunk was lost
                return status, rx
            return FRAME_REFUSED, b''
    def set_resets(self, value):
        self._setup()
        params = self._reset_cmd.send([value])
        if params['status'] != 0:
            raise self.printer.command_error(
                "xl_reset_lines 0x%02x failed (i2c)" % (value,))


######################################################################
# Bed temperature sensors (names read by Mainsail and the bedlet grid)
######################################################################

class _SensorName:
    def __init__(self, name):
        self._name = name
    def get_name(self):
        return self._name
    def get(self, key, default=None):
        return default

class BedSensor:
    """One bedlet (_Bedlet_NN: hidden in Mainsail, read by bedlet_grid.html).
    The whole bed's average + target is heater_bed (BedHeater) - the
    'Modular_Bed' sensor that showed the same was dropped (owner 10/14)."""
    def __init__(self, puppies, bedlet):
        self.puppies = puppies
        self.bedlet = bedlet
        self.name = "_Bedlet_%02d" % (bedlet + 1,)
    def get_temp(self, eventtime):
        return self.puppies.bed_measured.get(self.bedlet, 0.), 0.
    def get_status(self, eventtime):
        # its own target too: the grid page shows which zones are on
        return {'temperature': round(self.get_temp(eventtime)[0], 1),
                'target': round(self.puppies.bed_targets[self.bedlet], 1)}
    def stats(self, eventtime):
        return False, '%s: temp=%.1f' % (self.name, self.get_temp(eventtime)[0])

class BedHeater:
    """The modular bed as 'heater_bed' for Mainsail / Fluidd: a bed row with a
    target box and presets (owner 10/13; Teaching Tech, KlipperXL #21: "I
    can't see a way to manually control or see the bed temp").  The bed's
    own firmware heats it; a target set here goes the M140 way
    (set_bed_target: the whole bed, or the print area SET_BED_AREA chose).
    Listed in heaters.available_heaters (what Mainsail / Fluidd show), as
    sensor 'heater_bed' (M105 "B:", TEMPERATURE_WAIT) and with its own
    SET_HEATER_TEMPERATURE HEATER=heater_bed - NOT in PrinterHeaters.heaters:
    PID_CALIBRATE, heater_fan, homing_heaters, verify_heater look heaters up
    there and would call PID methods this bed does not have (pid_calibrate
    turns an unknown heater into a plain error).  The bed does not report
    its heater power, so no 'power' is given."""
    def __init__(self, puppies):
        self.puppies = puppies
    def get_temp(self, eventtime):
        return self.puppies.bed_average(), self.puppies.bed_target
    def set_temp(self, degrees):
        self.puppies.set_bed_target(degrees)
    def check_busy(self, eventtime):
        temp, target = self.get_temp(eventtime)
        return target > 0. and abs(temp - target) > 2.    # M190's tolerance
    def get_status(self, eventtime):
        temp, target = self.get_temp(eventtime)
        return {'temperature': round(temp, 1), 'target': round(target, 1)}


######################################################################
# Main object
######################################################################

class XLPuppies:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        mcu_name = config.get('mcu', 'mcu')
        self.mcu = self.printer.lookup_object(
            'mcu' if mcu_name == 'mcu' else 'mcu ' + mcu_name)
        self.bus = RawBus(self.printer, self.mcu)
        self.poll_interval = config.getfloat('bed_poll_interval', 1.0,
                                             minval=0.2, maxval=10.)
        self.reset_value = None       # unknown until first write
        self.running_dwarfs = 0       # reset-line bits of Dwarfs we started
        self.xl_plus = None           # unknown until the first bed boot
        self.modular_bed_booted = False
        self.bed_measured = {}
        self.bed_targets = [0.] * NUM_BEDLETS
        self.bed_target = 0.
        self.bed_area = None
        self.bed_zones = None         # SET_BED_ZONES: chosen bedlets (mask)
        self.bed_mask = 0xFFFF
        self.bed_faults_cleared = False
        self._bed_last = 0.
        self._poll_count = 0
        self._poll_timer = self.reactor.register_timer(self._poll_bed)
        self._stats_timer = self.reactor.register_timer(self._log_stats)
        # Sensors exist from config time so Mainsail always lists them
        pheaters = self.printer.load_object(config, 'heaters')
        self.sensors = [BedSensor(self, i) for i in range(NUM_BEDLETS)]
        for s in self.sensors:
            full = "temperature_sensor " + s.name
            pheaters.register_sensor(_SensorName(full), s)
            self.printer.add_object(full, s)
        # the bed as heater_bed (BedHeater: display list + its own commands).
        # Its limits come from a [heater_bed] section (xl_base.cfg, after this
        # one): read here they are config settings - Mainsail checks a typed
        # target against settings.heater_bed.max_temp ("TempTooHigh" without
        # it) and KlipperScreen lists a bed only if the config has the section
        bed_cfg = config.getsection('heater_bed')
        self.bed_min_temp = bed_cfg.getfloat('min_temp', 0., minval=0.)
        self.bed_max_temp = bed_cfg.getfloat('max_temp', BED_MAX_TEMP,
                                             above=self.bed_min_temp,
                                             maxval=BED_MAX_TEMP)
        # KlipperScreen's number pad reads config[heater]['control'] (KeyError
        # without it, owner 10/14) to offer PID / MPC calibration - the bed's
        # own firmware regulates it, so only 'watermark' (no calibration)
        bed_cfg.getchoice('control', {'watermark': 'watermark'}, 'watermark')
        self.bed_heater = BedHeater(self)
        self.printer.add_object('heater_bed', self.bed_heater)
        pheaters.available_heaters.append('heater_bed')
        pheaters.register_sensor(_SensorName('heater_bed'), self.bed_heater,
                                 gcode_id='B')
        self.gcode.register_mux_command(
            "SET_HEATER_TEMPERATURE", "HEATER", "heater_bed",
            self.cmd_SET_HEATER_TEMPERATURE_bed,
            desc="Set the modular bed temperature (as M140)")
        self.printer.register_event_handler("klippy:connect",
                                            self._handle_connect)
        self.printer.register_event_handler("klippy:shutdown",
                                            self._handle_shutdown)
        for cmd in ('M140', 'M190', 'SET_BED_AREA', 'CLEAR_BED_AREA',
                    'SET_BED_ZONES', 'BED_STATUS', 'RS485_STATS'):
            self.gcode.register_command(cmd, getattr(self, 'cmd_' + cmd),
                                        desc=getattr(self, 'cmd_%s_help'
                                                     % cmd, None))
        # TURN_OFF_HEATERS = all heaters: Klipper's own (the nozzles) and the
        # modular bed, which is not a Klipper heater - Mainsail's Cooldown,
        # the idle timeout, CANCEL_PRINT (released KlipperXL
        # cmd_TURN_OFF_HEATERS, ref l.8868-8884)
        self._base_turn_off_heaters = self.gcode.register_command(
            'TURN_OFF_HEATERS', None)
        self.gcode.register_command('TURN_OFF_HEATERS',
                                    self.cmd_TURN_OFF_HEATERS,
                                    desc=self.cmd_TURN_OFF_HEATERS_help)
    # Reset lines
    def _resets(self, value):
        self.bus.set_resets(value)
        self.reset_value = value
    def _pause(self, s):
        self.reactor.pause(self.reactor.monotonic() + s)
    # Prusa bootloader start sequence (ref l.3872-3977 / 4004-4102):
    # SET_ADDRESS at 0x00, discover, COMPUTE_FINGERPRINT with a random
    # salt, poll GET_FINGERPRINT, START_APPLICATION with salt+fingerprint.
    def _bl(self, addr, cmd, data=()):
        status, rx = self.bus.transact(build_frame(addr, cmd, data), boot=True)
        if status != FRAME_OK or len(rx) < 5:
            logging.info("xl_puppies: bootloader 0x%02x cmd 0x%02x: frame"
                         " status %d, %d reply bytes %s", addr, cmd, status,
                         len(rx), rx[:8].hex())
            return None
        return rx                     # [addr][status][len][data][crc]
    def _start_application(self, addr, what):
        self.bus.transact(build_frame(ADDR_DEFAULT, BL_SET_ADDRESS,
                                      [addr, 0x00]), retries=1)
        self._pause(0.05)             # SET_ADDRESS has no reply (short wait)
        rx = self._bl(addr, BL_GET_PROTOCOL_VERSION)
        if rx is None or rx[1] != 0:
            logging.warning("xl_puppies: %s: no bootloader at 0x%02x",
                            what, addr)
            return False
        salt = list(random.getrandbits(32).to_bytes(4, 'big'))
        self._bl(addr, BL_COMPUTE_FINGERPRINT, salt)
        self._pause(0.8)              # SHA256 on the puppy takes 330-600 ms
        fingerprint = None
        for attempt in range(10):
            self._pause(0.2)
            rx = self._bl(addr, BL_GET_FINGERPRINT, [0, 32])
            if rx is not None and rx[1] == 0 and len(rx) >= 37:
                fingerprint = list(rx[3:35])
                break
        if fingerprint is None:
            logging.warning("xl_puppies: %s: no fingerprint", what)
            return False
        for attempt in range(3):
            rx = self._bl(addr, BL_START_APPLICATION, salt + fingerprint)
            if rx is not None and rx[1] == 0:
                logging.info("xl_puppies: %s application started", what)
                return True
            self._pause(0.1)
        logging.warning("xl_puppies: %s: START_APPLICATION refused", what)
        return False
    def _is_xl_plus(self, hold):
        """Owner 10/27: KlipperXL-Plus must not run on a Prusa XL+.  Prusa's
        own check (6.10.3 src/puppies/xl_can_bootstrap.cpp xl_can_probe,
        out_of_band_bootstrap.cpp:150-163): on an XL+ the bed reset line
        resets the XL-CAN board on a HIGH->LOW edge; hold it HIGH 100 ms,
        then up to 3x {LOW, HIGH, 50 ms, GET_PROTOCOL_VERSION at 0x13}.
        On a plain XL nothing answers at 0x13 and the pulse only lets the
        held bed go for a moment.  Asked once per Klipper start."""
        if self.xl_plus is None:
            self._resets(hold | BED_BIT)
            self._pause(0.1)
            self.xl_plus = False
            for attempt in range(3):
                self._resets(hold)
                self._resets(hold | BED_BIT)
                self._pause(0.05)
                status, rx = self.bus.transact(build_frame(
                    ADDR_XL_CAN_BOOT, BL_GET_PROTOCOL_VERSION))
                if (status == FRAME_OK and len(rx) >= 5
                        and rx[0] == ADDR_XL_CAN_BOOT and rx[1] == 0):
                    self.xl_plus = True
                    break
            logging.info("xl_puppies: XL-CAN board %s", "found - Prusa XL+"
                         if self.xl_plus else "not found (original XL)")
        return self.xl_plus
    def boot_bed_for_bridge(self, attempts=3):
        """Boot the modular bed first (Prusa order).  Dwarfs that are not
        running are held; a running Dwarf is never reset (printer
        2026-10-03: a connect-time bed boot that held every Dwarf killed
        all five running tools).  Retried: right after power-on the bed's
        bootloader may not answer yet (same test, first attempt)."""
        if self.modular_bed_booted:
            return True
        hold = ALL_DWARF_BITS & ~self.running_dwarfs
        if self._is_xl_plus(hold):
            self.printer.invoke_shutdown(XL_PLUS_MSG)
            return False
        for attempt in range(attempts):
            self._resets(hold | BED_BIT)
            self._pause(0.05)
            self._resets(hold)        # release the bed only
            self._pause(0.5)          # bootloader start
            if self._start_application(ADDR_FIRST_BOOT, "modular bed"):
                self._pause(0.5)      # bed init before first MODBUS command
                self.modular_bed_booted = True
                self.bed_faults_cleared = False
                return True
            logging.info("xl_puppies: modular bed boot attempt %d failed",
                         attempt + 1)
            self._pause(1.0)
        return False
    def boot_dwarf_for_bridge(self, dwarf, keep_running=0):
        """Boot Dwarf 1-5 into its (Klipper) application.  Every other
        Dwarf is held except those in keep_running (already running and
        locked to 0x8N - never reset them); the bed is never reset once
        it runs."""
        if self.xl_plus:
            return False
        bed = 0 if self.modular_bed_booted else BED_BIT
        hold = (ALL_DWARF_BITS | bed) & ~keep_running
        self._resets(hold)
        self._pause(0.05)
        self._resets(hold & ~(1 << dwarf))
        self._pause(0.5)
        ok = self._start_application(ADDR_FIRST_BOOT + dwarf,
                                     "Dwarf %d" % (dwarf,))
        if ok:
            self.running_dwarfs |= 1 << dwarf
        return ok
    # Modular bed MODBUS
    def _bed_transact(self, fc, data):
        wait = self._bed_last + BED_MIN_INTERVAL - self.reactor.monotonic()
        if wait > 0:
            self._pause(wait)
        status, rx = self.bus.transact(build_frame(ADDR_MODULAR_BED, fc, data))
        self._bed_last = self.reactor.monotonic()
        if status != FRAME_OK or len(rx) < 4 or rx[1] != fc:
            return None               # timeout, CRC, or MODBUS exception
        return rx
    def _bed_read_input(self, reg, count):
        rx = self._bed_transact(MB_READ_INPUT, [reg >> 8, reg & 0xFF,
                                                0, count])
        if rx is None or len(rx) < 5 + 2 * count:
            return None
        return [(rx[3 + 2*i] << 8) | rx[4 + 2*i] for i in range(count)]
    def _bed_write_regs(self, reg, values):
        data = [reg >> 8, reg & 0xFF, 0, len(values), 2 * len(values)]
        for v in values:
            data += [(v >> 8) & 0xFF, v & 0xFF]
        for attempt in range(3):
            if self._bed_transact(MB_WRITE_MULTIPLE, data) is not None:
                return True
            self._pause(0.2)
        return False
    def _bed_write_coil(self, coil, on):
        rx = self._bed_transact(MB_WRITE_COIL, [coil >> 8, coil & 0xFF,
                                                0xFF if on else 0, 0])
        return rx is not None
    # Polling: keeps the bed's 30 s MODBUS watchdog fed, reads temps/faults
    def _handle_connect(self):
        if not self.modular_bed_booted:
            if self.running_dwarfs:
                # Never send bootloader frames past live Klipper Dwarfs
                # (the 2026-10-02 deaf-Dwarf hazard): report it instead.
                logging.error("xl_puppies: modular bed did not boot before"
                              " the Dwarfs - bed NOT running")
            else:
                # No [serial_bridge]: boot the bed on its own
                try:
                    self.boot_bed_for_bridge()
                except self.printer.command_error as e:
                    logging.warning("xl_puppies: bed boot: %s", e)
        self.reactor.update_timer(self._poll_timer, self.reactor.NOW)
        self.reactor.update_timer(self._stats_timer, self.reactor.NOW)
    # Every 5 s, also after a shutdown (the command is allowed there)
    def _log_stats(self, eventtime):
        text = self.bus.stats_text()
        if text:
            logging.info("xl_puppies: rs485 %s", text)
        try:
            self.bus.request_stats()
        except Exception as e:
            logging.info("xl_puppies: rs485_stats request failed: %s", e)
        return eventtime + 5.
    cmd_RS485_STATS_help = "Show the XLBuddy RS485 engine counters"
    def cmd_RS485_STATS(self, gcmd):
        self.bus.request_stats()
        self._pause(0.2)
        gcmd.respond_info(self.bus.stats_text() or "no reply")
    def _handle_shutdown(self):
        self.reactor.update_timer(self._poll_timer, self.reactor.NEVER)
        if self.modular_bed_booted:
            self.reactor.register_callback(self._shutdown_bed)
    def _shutdown_bed(self, eventtime):
        self.bed_target = 0.
        self._bed_write_regs(BED_REG_TARGET_TEMP, [0] * NUM_BEDLETS)
    def _poll_bed(self, eventtime):
        if not self.modular_bed_booted or self.printer.is_shutdown():
            return eventtime + self.poll_interval
        regs = self._bed_read_input(BED_REG_MEASURED_TEMP, NUM_BEDLETS)
        if regs is not None:
            self.bed_measured = {i: (v - 0x10000 if v >= 0x8000 else v)
                                 / BED_TEMP_SCALE for i, v in enumerate(regs)}
        self._check_faults()
        return self.reactor.monotonic() + self.poll_interval
    def _check_faults(self):
        regs = self._bed_read_input(BED_REG_FAULT_STATUS, 1)
        if regs and regs[0]:
            f = regs[0]
            desc = ", ".join(n for b, n in BED_FAULTS.items() if f & b)
            if f & BED_CRITICAL:
                # Confirm with a second read before stopping (ref l.4776-4785)
                again = self._bed_read_input(BED_REG_FAULT_STATUS, 1)
                if again and again[0] == f:
                    self.printer.invoke_shutdown(
                        "Modular bed fault: %s (0x%04x)" % (desc, f))
                    return
            logging.warning("xl_puppies: bed fault %s (0x%04x)", desc, f)
        self._poll_count += 1
        if self._poll_count % 10:
            return
        regs = self._bed_read_input(BED_REG_BEDLET_FAULT, NUM_BEDLETS)
        for i, f in enumerate(regs or []):
            if not f:
                continue
            desc = ", ".join(n for b, n in BEDLET_FAULTS.items() if f & b)
            if f & BEDLET_CRITICAL:
                self.printer.invoke_shutdown(
                    "Modular bed bedlet %d fault: %s (0x%04x)" % (i, desc, f))
                return
            logging.warning("xl_puppies: bedlet %d fault %s (0x%04x)",
                            i, desc, f)
    # Bed heating (adaptive area: Prusa modular_bed.cpp / print_area.cpp)
    def bed_average(self):
        temps = [self.bed_measured[i] for i in range(NUM_BEDLETS)
                 if self.bed_mask & (1 << i) and i in self.bed_measured]
        if not temps:
            temps = list(self.bed_measured.values())
        return sum(temps) / len(temps) if temps else 0.
    def _area_mask(self, x0, y0, x1, y1):
        mask = 0
        for r in range(4):
            for c in range(4):
                if (min(x1, (c + 1) * BEDLET_SIZE) > max(x0, c * BEDLET_SIZE)
                        and min(y1, (r + 1) * BEDLET_SIZE)
                        > max(y0, r * BEDLET_SIZE)):
                    mask |= 1 << BEDLET_MAP[r][c]
        return mask
    def _touch_side(self, mask, dim, sign):
        best = (99, 0)
        for c in range(4):
            for r in range(4):
                if not mask & (1 << BEDLET_MAP[r][c]):
                    continue
                cost, add, i = 0, 0, (c if dim == 0 else r) + sign
                while 0 <= i < 4:
                    cost += 1
                    add |= 1 << (BEDLET_MAP[r][i] if dim == 0
                                 else BEDLET_MAP[i][c])
                    i += sign
                if cost < best[0]:
                    best = (cost, add)
        return best
    def _expand_to_sides(self, mask):
        sides = sorted([self._touch_side(mask, 0, 1),
                        self._touch_side(mask, 0, -1),
                        self._touch_side(mask, 1, 1),
                        self._touch_side(mask, 1, -1)], key=lambda s: s[0])
        return mask | sides[0][1] | sides[1][1]
    def _gradient(self, mask, target):
        temps = [target if mask & (1 << i) else 0. for i in range(NUM_BEDLETS)]
        for i in range(NUM_BEDLETS):
            if mask & (1 << i):
                continue
            c1, r1 = BEDLET_GRID[i]
            for j in range(NUM_BEDLETS):
                if not mask & (1 << j):
                    continue
                c2, r2 = BEDLET_GRID[j]
                dist = math.hypot(c1 - c2, r1 - r2)
                if dist <= GRADIENT_CUTOFF:
                    t = target - target * (dist / GRADIENT_CUTOFF) \
                        ** GRADIENT_EXPONENT
                    temps[i] = max(temps[i], t)
        return temps
    def _write_targets(self, temps):
        if not self.bed_faults_cleared and any(temps):
            # Prusa refresh(): clear faults + overcurrent before heating
            self._bed_write_coil(BED_COIL_CLEAR_FAULT, True)
            self._bed_write_coil(BED_COIL_RESET_OVERCURRENT, True)
            self.bed_faults_cleared = True
        temps = [min(t, BED_MAX_TEMP) for t in temps]
        if not self._bed_write_regs(BED_REG_TARGET_TEMP,
                                    [int(t * BED_TEMP_SCALE) for t in temps]):
            raise self.gcode.error("Modular bed: target write failed")
        self.bed_targets = temps
    def set_bed_target(self, target):
        if not self.modular_bed_booted:
            raise self.gcode.error("Modular bed not running")
        # [heater_bed] max_temp, at most the bed's own BED_MAX_TEMP
        if target > self.bed_max_temp:
            raise self.gcode.error("Bed target %.1f above %.0f"
                                   % (target, self.bed_max_temp))
        self.bed_target = target
        if target <= 0 or (self.bed_area is None and self.bed_zones is None):
            self._write_targets([max(target, 0.)] * NUM_BEDLETS)
            return
        if self.bed_zones is not None:          # SET_BED_ZONES: as chosen
            self.bed_mask = self.bed_zones
        else:                                   # a print's area (stock)
            self.bed_mask = self._expand_to_sides(
                self._area_mask(*self.bed_area))
        self._write_targets(self._gradient(self.bed_mask, target))
    # G-Code
    cmd_TURN_OFF_HEATERS_help = "Turn off all heaters, the modular bed too"
    def cmd_TURN_OFF_HEATERS(self, gcmd):
        self._base_turn_off_heaters(gcmd)
        if self.modular_bed_booted:
            self.set_bed_target(0.)
    def cmd_SET_HEATER_TEMPERATURE_bed(self, gcmd):
        self.set_bed_target(gcmd.get_float('TARGET', 0.))
    cmd_M140_help = "Set modular bed temperature"
    def cmd_M140(self, gcmd):
        bedlet = gcmd.get_int('B', None, minval=0, maxval=NUM_BEDLETS - 1)
        target = gcmd.get_float('S', 0.)
        if bedlet is None:
            self.set_bed_target(target)
        else:
            # one bedlet: the same checks as set_bed_target
            if not self.modular_bed_booted:
                raise self.gcode.error("Modular bed not running")
            if target > self.bed_max_temp:
                raise self.gcode.error("Bed target %.1f above %.0f"
                                       % (target, self.bed_max_temp))
            temps = list(self.bed_targets)
            temps[bedlet] = min(max(target, 0.), BED_MAX_TEMP)
            self._write_targets(temps)
    cmd_M190_help = "Set modular bed temperature and wait"
    def cmd_M190(self, gcmd):
        self.cmd_M140(gcmd)
        bedlet = gcmd.get_int('B', None, minval=0, maxval=NUM_BEDLETS - 1)
        target = gcmd.get_float('S', 0.)
        if target <= 0:
            return
        self.printer.lookup_object('toolhead').wait_moves()
        eventtime = self.reactor.monotonic()
        while not self.printer.is_shutdown():
            temp = (self.bed_measured.get(bedlet, 0.) if bedlet is not None
                    else self.bed_average())
            if abs(temp - target) <= 2.0:      # ref l.8570, 2 C tolerance
                break
            gcmd.respond_raw("B:%.1f /%.1f" % (temp, target))
            eventtime = self.reactor.pause(eventtime + 1.)
    cmd_SET_BED_AREA_help = "Heat only the bedlets under the print area"
    def cmd_SET_BED_AREA(self, gcmd):
        x0 = min(max(gcmd.get_float('X0', 0.), 0.), 360.)
        y0 = min(max(gcmd.get_float('Y0', 0.), 0.), 360.)
        x1 = min(max(gcmd.get_float('X1', 360.), 0.), 360.)
        y1 = min(max(gcmd.get_float('Y1', 360.), 0.), 360.)
        if x1 <= x0 or y1 <= y0:
            raise gcmd.error("Invalid bed area")
        self.bed_area = (x0, y0, x1, y1)
        self.bed_zones = None         # the print's area wins over a choice
        self.bed_mask = self._expand_to_sides(self._area_mask(*self.bed_area))
        if self.bed_target > 0:
            self.set_bed_target(self.bed_target)
        gcmd.respond_info("Bed area (%.0f,%.0f)-(%.0f,%.0f): %d/16 bedlets"
                          % (x0, y0, x1, y1, bin(self.bed_mask).count('1')))
    cmd_CLEAR_BED_AREA_help = "Heat the whole bed again"
    def cmd_CLEAR_BED_AREA(self, gcmd):
        self.bed_area = None
        self.bed_zones = None
        self.bed_mask = 0xFFFF
        if self.bed_target > 0:
            self.set_bed_target(self.bed_target)
    cmd_SET_BED_ZONES_help = ("Heat chosen bedlets: ZONES=1,2,5 (the B numbers"
                              " of the bedlet grid) or ZONES=ALL [S=temp]")
    def cmd_SET_BED_ZONES(self, gcmd):
        """Owner 10/14: heat the whole bed or just the bedlets you pick (the
        bedlet grid page).  Exactly the chosen ones get the target; their
        neighbours are warmed part-way by Prusa's print-area gradient
        (_gradient) so no hot bedlet sits next to a cold one - not stretched
        to the bed edges as a print area is (_expand_to_sides).  Not while a
        print runs: START_PRINT's SET_BED_AREA chooses then, and END_PRINT /
        CANCEL_PRINT go back to the whole bed (CLEAR_BED_AREA)."""
        stats = self.printer.lookup_object('print_stats', None)
        state = (stats.get_status(self.reactor.monotonic())['state']
                 if stats is not None else '')
        if state in ('printing', 'paused'):
            raise gcmd.error("SET_BED_ZONES refused: a print is %s" % (state,))
        arg = gcmd.get('ZONES').strip().upper()
        if arg == 'ALL':
            mask = 0xFFFF
        else:
            mask = 0
            for part in arg.replace(' ', '').split(','):
                try:
                    n = int(part.lstrip('B'))
                except ValueError:
                    raise gcmd.error("SET_BED_ZONES: '%s' is not a bedlet"
                                     " number (1-16)" % (part,))
                if not 1 <= n <= NUM_BEDLETS:
                    raise gcmd.error("SET_BED_ZONES: bedlet %d - there are"
                                     " 1 to 16" % (n,))
                mask |= 1 << (n - 1)
            if not mask:
                raise gcmd.error("SET_BED_ZONES: no bedlets given")
        # every check before anything changes: a refused command leaves the
        # bed as it was (test 10/14: S=130 had already changed the zones)
        target = gcmd.get_float('S', self.bed_target, minval=0.)
        if not self.modular_bed_booted:
            raise gcmd.error("Modular bed not running")
        if target > self.bed_max_temp:
            raise gcmd.error("Bed target %.1f above %.0f"
                             % (target, self.bed_max_temp))
        self.bed_area = None
        self.bed_zones = None if mask == 0xFFFF else mask
        self.bed_mask = mask
        self.set_bed_target(target)
        names = ("all 16 bedlets" if mask == 0xFFFF else ", ".join(
            "B%02d" % (i + 1,) for i in range(NUM_BEDLETS) if mask & (1 << i)))
        gcmd.respond_info("Bed: %s at %s" % (
            names, "%.0f C" % (target,) if target > 0 else "off"))
    cmd_BED_STATUS_help = "Show bedlet temperatures and targets"
    def cmd_BED_STATUS(self, gcmd):
        lines = ["Modular bed %s, target %.1f, avg %.1f"
                 % ("running" if self.modular_bed_booted else "NOT running",
                    self.bed_target, self.bed_average()),
                 "  REAR / TOOL DOCKS"]
        for r in range(3, -1, -1):
            cells = []
            for c in range(4):
                i = BEDLET_MAP[r][c]
                cells.append("%5.1f/%-5.1f" % (self.bed_measured.get(i, 0.),
                                               self.bed_targets[i]))
            lines.append("  " + "  ".join(cells))
        lines.append("  FRONT / LCD")
        gcmd.respond_info("\n".join(lines))
    def get_status(self, eventtime):
        return {'modular_bed_booted': self.modular_bed_booted,
                'bed_target': self.bed_target,
                'bed_area': self.bed_area,
                'bed_zones': ([i + 1 for i in range(NUM_BEDLETS)
                               if self.bed_zones & (1 << i)]
                              if self.bed_zones is not None else None),
                'bed_mask': self.bed_mask,
                'reset_lines': self.reset_value}

def load_config(config):
    return XLPuppies(config)
