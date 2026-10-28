# KlipperXL-Plus - Prusa XL side filament sensors (on the XLBuddy)
#
# Five hall sensors near the spools, multiplexed onto two ADC inputs.  All
# hardware access is Klipper's own: [adc] analog_in on PC0 / PF6 and
# digital_out on the mux select pins PF12 / PG6.  This module only steps
# the mux and keeps samples taken after each switch.
#
# Facts (Prusa-Firmware-Buddy, FACTS.md section 7):
#   ADC X = PC0, Y = PF6 (common/adc.hpp:44-45); select A = PF12 (AD2setA),
#   B = PG6 (AD2setB) (hwio_pindef_XL.h:147-148); A = bit0, B = bit1
#   (adc.hpp:350-351); position p: X = sfs(p+1), Y = sfs5, sfs6, sandwich,
#   ambient (adc.hpp:58-67); tool -> sensor T0 sfs1, T1 sfs2, T2 sfs3,
#   T3 sfs6, T4 sfs5 (common/appmain.cpp:272-279).
#   => pos 0: X=T0 Y=T4 | pos 1: X=T1 Y=T3 | pos 2: X=T2
# Detection = Prusa filament_sensor_adc_eval: midpoint of the two
# calibration values, 1/6 of their span as hysteresis.
# Readings pass through Prusa's side-sensor median filter first
# (filament_sensors_handler_XL.cpp side_fs_process_sample -> MedianFilter):
# the median of the last 3, and no state at all until 3 are in.  Without it
# one bad reading changed the state - printer 10/05-10/06: on about half the
# Klipper starts the FIRST reading of the PF6 input (T3, T4) came back
# 3713-4094 (real: ~1400 empty, ~900 loaded), so a loaded T4 went
# no_filament -> has_filament and autoload homed X/Y and picked T4.
#
# Config:
#   [xl_side_sensors]
#   cal_t0: <empty>, <inserted>   # 12-bit ADC: no filament, filament inserted
#   ...                    # (per printer: printer.cfg / SAVE_CONFIG)
#
# Calibration (released KlipperXL procedure, puppy_bootloader.py
# cmd_CALIBRATE_*_SENSOR): step 1 with NO filament records the empty value,
# step 2 with filament INSERTED records the inserted value; they must differ
# by CAL_MIN_DIFF (released FS_CAL_MIN_DIFF / SIDE_FS_CAL_MIN_DIFF = 200).
# The result is applied at once and staged for SAVE_CONFIG
# (docs/RELEASE_CONFIG.md: calibrations live in printer.cfg / SAVE_CONFIG).
#   CALIBRATE_SIDE_SENSOR [TOOL=n]        (no filament)
#   CALIBRATE_SIDE_SENSOR_STEP2 [TOOL=n]  (filament inserted)
#   then SAVE_CONFIG
#
# Copyright (C) 2026  Richard Crook
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging, threading

ADC_X_PIN, ADC_Y_PIN = 'PC0', 'PF6'
MUX_A_PIN, MUX_B_PIN = 'PF12', 'PG6'
# mux position -> {'x': tool, 'y': tool}
POSITIONS = [(0, {'x': 0, 'y': 4}), (1, {'x': 1, 'y': 3}), (2, {'x': 2})]
NUM_TOOLS = 5

REPORT_TIME = 0.010
SAMPLE_TIME = 0.001
SAMPLE_COUNT = 8
SETTLE_TIME = 0.005    # ignore samples that started this soon after a switch
STEP_TIME = 0.060      # time spent on each mux position
ADC_RANGE = 4095.      # values are compared in 12-bit counts (Prusa / fw4)
MEDIAN_SAMPLES = 3     # Prusa MedianFilter fs_raw_buffer_size (median of 3)
ADC_MIN = 20           # below = sensor not connected (released KlipperXL)
ADC_MAX = 4096         # released FS_ADC_MAX
CAL_MIN_DIFF = 200     # released SIDE_FS_CAL_MIN_DIFF


class XLSideSensors:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        self.cal = {}
        for t in range(NUM_TOOLS):
            v = config.getintlist('cal_t%d' % (t,), None, count=2)
            if v is not None:
                self.cal[t] = v
        ppins = self.printer.lookup_object('pins')
        self.adcs = {}
        for ch, pin in (('x', ADC_X_PIN), ('y', ADC_Y_PIN)):
            adc = ppins.setup_pin('adc', pin)
            adc.setup_adc_sample(REPORT_TIME, SAMPLE_TIME, SAMPLE_COUNT)
            adc.setup_adc_callback(
                lambda samples, ch=ch: self._adc_callback(ch, samples))
            self.adcs[ch] = adc
        self.mcu = self.adcs['x'].get_mcu()
        ppins.lookup_pin(MUX_A_PIN)
        ppins.lookup_pin(MUX_B_PIN)
        self.mux_oids = [self.mcu.create_oid(), self.mcu.create_oid()]
        # Unknown until the first step sends both: a Klipper restart that
        # keeps the XLBuddy configured keeps the last session's mux pins
        # (printer 10/28: T0 / T4 first read T1's / T3's values; left on
        # position 2 or 3 that is the 10/05 first reading 3713-4094)
        self.mux_level = [None, None]
        self.mux_cmd = None
        self.mcu.register_config_callback(self._build_config)
        self._lock = threading.Lock()
        self._latest = {'x': None, 'y': None}   # (read_time, value)
        self.raw = {t: None for t in range(NUM_TOOLS)}   # filtered value
        self._hist = {t: [] for t in range(NUM_TOOLS)}   # last raw readings
        self.state = {t: 'unknown' for t in range(NUM_TOOLS)}
        self._pos_index = 0
        self._switch_time = 0.
        self._timer = self.reactor.register_timer(self._step)
        self._shut = False
        self.printer.register_event_handler("klippy:connect", self._add_rows)
        self.printer.register_event_handler("klippy:ready", self._ready)
        self.printer.register_event_handler("klippy:shutdown", self._shutdown)
        self._cal_empty = {}
        self.gcode.register_command(
            "CALIBRATE_SIDE_SENSOR", self.cmd_CALIBRATE_SIDE_SENSOR,
            desc="Side filament sensor calibration, step 1 (no filament)")
        self.gcode.register_command(
            "CALIBRATE_SIDE_SENSOR_STEP2", self.cmd_CALIBRATE_SIDE_SENSOR_STEP2,
            desc="Side filament sensor calibration, step 2 (inserted)")
        self.gcode.register_command("QUERY_SIDE_SENSORS",
                                    self.cmd_QUERY_SIDE_SENSORS,
                                    desc="Report the side filament sensors")
    def _build_config(self):
        for oid, pin in zip(self.mux_oids, (MUX_A_PIN, MUX_B_PIN)):
            self.mcu.add_config_cmd(
                "config_digital_out oid=%d pin=%s value=0 default_value=0"
                " max_duration=0" % (oid, pin))
        cq = self.mcu.alloc_command_queue()
        self.mux_cmd = self.mcu.lookup_command(
            "update_digital_out oid=%c value=%c", cq=cq)
        self.mux_done = self.mcu.lookup_query_command(
            "get_uptime", "uptime high=%u clock=%u", cq=cq)
    # ADC samples arrive on the serial thread
    def _adc_callback(self, ch, samples):
        with self._lock:
            self._latest[ch] = samples[-1]
    def _ready(self):
        self.reactor.update_timer(self._timer, self.reactor.NOW)
    # In a shutdown the XLBuddy keeps sampling its ADCs (Klipper adccmds.c
    # analog_in_shutdown: temperatures still report) but the mux select pins
    # go to their default 0 = position 0 (X=T0, Y=T4) - the readings no longer
    # belong to the tools this scan files them under.  Printer 10/11: T4's
    # Dwarf shut down ("Timer too close"), T3 (empty) read T4's loaded ~900
    # and fired an autoload.  The scan stops with the shutdown; the states
    # stay as they were last read (FIRMWARE_RESTART starts a fresh scan).
    def _shutdown(self):
        self._shut = True
        self.reactor.update_timer(self._timer, self.reactor.NEVER)
    # When the switch really ran, not when it was sent: the XLBuddy answers
    # get_uptime on the same queue only after the switch, and its clock in
    # the answer is from after it - readings taken from then on are the new
    # position's.  Taking the send time as the switch time filed the old
    # position's reading under the new one whenever the XLBuddy had other
    # work queued (printer 10/28: behind the screen's drawing T3 / T4 read
    # ~3790 - Y at position 2, Prusa's "sandwich" input, measured 3754 there
    # idle; position 3 reads 4094 - and a loaded T4 autoloaded; the same as
    # the 10/05 first readings of 3713-4094 at Klipper start).  Idle test:
    # PF6 steady 933-941 while a full page was drawn, so not electrical.
    def _set_mux(self, pos, eventtime):
        for i, level in enumerate((pos & 1, (pos >> 1) & 1)):
            if self.mux_level[i] != level:
                self.mux_cmd.send([self.mux_oids[i], level])
                self.mux_level[i] = level
        clock = self.mcu.clock32_to_clock64(self.mux_done.send()['clock'])
        self._switch_time = self.mcu.clock_to_print_time(clock)
    def _step(self, eventtime):
        if self._shut:
            return self.reactor.NEVER
        pos, tools = POSITIONS[self._pos_index]
        # Collect the position that has been selected for STEP_TIME
        if self._switch_time:
            with self._lock:
                latest = dict(self._latest)
            for ch, tool in tools.items():
                sample = latest.get(ch)
                if (sample is not None
                        and sample[0] >= self._switch_time + SETTLE_TIME):
                    self._sample(tool, int(sample[1] * ADC_RANGE + .5))
            self._pos_index = (self._pos_index + 1) % len(POSITIONS)
            pos, tools = POSITIONS[self._pos_index]
        self._set_mux(pos, eventtime)
        return eventtime + STEP_TIME
    def _sample(self, tool, value):
        # Prusa MedianFilter: "output not valid, insufficient number of
        # samples collected yet" until 3 are in; then the median of the last 3
        hist = self._hist[tool]
        hist.append(value)
        del hist[:-MEDIAN_SAMPLES]
        if len(hist) < MEDIAN_SAMPLES:
            return
        value = sorted(hist)[MEDIAN_SAMPLES // 2]
        if self.raw[tool] is None:
            logging.info("xl_side_sensors: T%d first readings %s -> %d",
                         tool, hist, value)
        self._update(tool, value)
    def _evaluate(self, tool, value):
        if value < ADC_MIN:
            return 'not_connected'
        cal = self.cal.get(tool)
        if cal is None:
            return 'not_calibrated'
        nins, ins = cal
        mid = (nins + ins) // 2
        hyst = abs(nins - ins) // 6
        prev = self.state[tool]
        if prev in ('has_filament', 'no_filament') and abs(value - mid) < hyst:
            return prev
        return 'has_filament' if (value > mid) == (ins > mid) else 'no_filament'
    def _update(self, tool, value):
        self.raw[tool] = value
        new = self._evaluate(tool, value)
        old = self.state[tool]
        self.state[tool] = new
        if new != old:
            logging.info("xl_side_sensors: T%d %s -> %s (raw %d)",
                         tool, old, new, value)
            self.printer.send_event("xl_side_sensors:state", tool, old, new)
        # every filtered reading (xl_filament runout counts consecutive ones)
        self.printer.send_event("xl_side_sensors:reading", tool, new)
    def has_filament(self, tool):
        return self.state.get(tool) == 'has_filament'
    # Mainsail rows (owner 10/12): Mainsail's Miscellaneous panel lists every
    # filament_switch_sensor object (filament_detected / enabled, Mainsail
    # getFilamentSensors) - one row per side sensor and per tool sensor of
    # each installed tool, Detected / Empty, from the states already kept
    # here and in [xl_tool_fsensor Tn].  Show only: switching one sensor off
    # (stock: Filament sensors menu, fsensor_side/extruder_enabled_bits) is
    # not there, and the row's switch says so.
    def _add_rows(self):
        for t in self._installed():
            rows = [('T%d_side' % (t,), lambda t=t: self.state[t])]
            tfs = self.printer.lookup_object('xl_tool_fsensor T%d' % (t,), None)
            if tfs is not None:
                rows.append(('T%d_tool' % (t,), lambda f=tfs: f.state))
            for name, get_state in rows:
                self.printer.add_object('filament_switch_sensor ' + name,
                                        _MainsailSensorRow(get_state))
                self.gcode.register_mux_command(
                    "SET_FILAMENT_SENSOR", "SENSOR", name,
                    lambda gcmd, n=name: self._cmd_row(gcmd, n),
                    desc="KlipperXL-Plus filament sensor rows (show only)")
    def _cmd_row(self, gcmd, name):
        gcmd.respond_info("%s: shown only - one sensor cannot be switched off"
                          " yet (runout for all tools: M591 S0 / M591 S1)"
                          % (name,))
    # The tools this printer has = the tools with config (xl_toolchanger
    # _installed); the mux still scans all five positions, an empty one
    # just is not reported or calibrated.
    def _installed(self):
        return [t for t in range(NUM_TOOLS)
                if self.printer.lookup_object('xl_dock_sensors T%d' % (t,),
                                              None) is not None]
    def _cal_tools(self, gcmd):
        tool = gcmd.get_int('TOOL', None, minval=0, maxval=NUM_TOOLS - 1)
        if tool is None:
            return self._installed()
        if tool not in self._installed():
            raise gcmd.error("T%d is not installed on this printer" % (tool,))
        return [tool]
    def cmd_CALIBRATE_SIDE_SENSOR(self, gcmd):
        for t in self._cal_tools(gcmd):
            raw = self.raw[t]
            if raw is None or raw < ADC_MIN or raw > ADC_MAX:
                self._cal_empty.pop(t, None)
                gcmd.respond_info("T%d: side sensor not connected (raw=%s)"
                                  % (t, raw))
                continue
            self._cal_empty[t] = raw
            gcmd.respond_info("T%d: side sensor no-filament reference = %d"
                              % (t, raw))
        gcmd.respond_info("Step 1 complete. Now INSERT filament into the side"
                          " sensor(s) and run:\n"
                          "  CALIBRATE_SIDE_SENSOR_STEP2 [TOOL=n]")
    def cmd_CALIBRATE_SIDE_SENSOR_STEP2(self, gcmd):
        configfile = self.printer.lookup_object('configfile')
        saved = 0
        for t in self._cal_tools(gcmd):
            nins = self._cal_empty.get(t)
            if nins is None:
                gcmd.respond_info("T%d: run CALIBRATE_SIDE_SENSOR first"
                                  " (step 1)" % (t,))
                continue
            raw = self.raw[t]
            if raw is None or raw < ADC_MIN or raw > ADC_MAX:
                gcmd.respond_info("T%d: side sensor not connected (raw=%s)"
                                  % (t, raw))
                continue
            if abs(raw - nins) < CAL_MIN_DIFF:
                gcmd.respond_info(
                    "T%d: FAILED - inserted value (%d) too close to empty value"
                    " (%d). Difference %d must be >= %d. Check filament is in"
                    " the sensor." % (t, raw, nins, abs(raw - nins),
                                      CAL_MIN_DIFF))
                self._cal_empty.pop(t, None)
                continue
            self.cal[t] = [nins, raw]
            self._cal_empty.pop(t, None)
            self._update(t, raw)
            configfile.set('xl_side_sensors', 'cal_t%d' % (t,),
                           "%d, %d" % (nins, raw))
            saved += 1
            gcmd.respond_info("T%d: side sensor calibrated, nins=%d ins=%d"
                              " midpoint=%d hysteresis=+-%d"
                              % (t, nins, raw, (nins + raw) // 2,
                                 abs(nins - raw) // 6))
        if saved:
            gcmd.respond_info("Run SAVE_CONFIG to store the calibration.")
    def cmd_QUERY_SIDE_SENSORS(self, gcmd):
        lines = []
        for t in self._installed():
            cal = self.cal.get(t)
            lines.append("T%d: %s (raw %s, cal %s)"
                         % (t, self.state[t], self.raw[t],
                            "%d/%d" % tuple(cal) if cal else "none"))
        gcmd.respond_info("\n".join(lines))
    def get_status(self, eventtime):
        return {'T%d' % t: {'state': self.state[t], 'raw': self.raw[t]}
                for t in self._installed()}

class _MainsailSensorRow:
    """What Mainsail reads from a filament_switch_sensor (Klipper's
    filament_switch_sensor.py get_status)."""
    def __init__(self, get_state):
        self._get_state = get_state
    def get_status(self, eventtime):
        return {'filament_detected': self._get_state() == 'has_filament',
                'enabled': True}

def load_config(config):
    return XLSideSensors(config)
