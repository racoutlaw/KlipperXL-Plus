# KlipperXL-Plus - Prusa XL tool (extruder) filament sensor on each Dwarf
#
# A hall sensor in each toolhead, read with Klipper's own ADC on the Dwarf
# (the Dwarfs are Klipper MCUs in KlipperXL-Plus).
#
# Facts (Prusa-Firmware-Buddy): Dwarf ADC1 converts in channel-number
# order (ADC_RANK_CHANNEL_NUMBER, device/stm32g0/peripherals.cpp:73-110:
# ch 1,2,4,5,7,8,10,11); AdcChannel order picked1, picked0, TFS, ntc,
# ntc_internal, heater_current, ntc2, dwarf_24V (common/adc.hpp:177-187)
# => TFS = ch4 = PA4 (ntc = ch5 = PA5, ntc_internal = ch7 = PA7, ntc2 =
# ch10 = PB2 - all already verified on the printer).
# Detection = Prusa filament_sensor_adc_eval: midpoint of the two
# calibration values, 1/6 of their span as hysteresis.
#
# Config:
#   [xl_tool_fsensor T0]
#   pin: dwarf_t0:PA4
#   cal: <empty>, <inserted>   # 12-bit ADC: no filament, filament inserted
#                         # (per printer: printer.cfg / SAVE_CONFIG)
#
# Calibration (released KlipperXL procedure, puppy_bootloader.py
# cmd_CALIBRATE_*_SENSOR): step 1 with NO filament records the empty value,
# step 2 with filament INSERTED records the inserted value; they must differ
# by CAL_MIN_DIFF (released FS_CAL_MIN_DIFF / SIDE_FS_CAL_MIN_DIFF = 200).
# The result is applied at once and staged for SAVE_CONFIG
# (docs/RELEASE_CONFIG.md: calibrations live in printer.cfg / SAVE_CONFIG).
#   CALIBRATE_FILAMENT_SENSOR [TOOL=n]        (no filament)
#   CALIBRATE_FILAMENT_SENSOR_STEP2 [TOOL=n]  (filament inserted)
#   then SAVE_CONFIG
#
# Copyright (C) 2026  Richard Crook
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging

# Starts at the parked rate, like the dock sensors (xl_dock_sensors: a tool
# starts slow; set_active() makes the picked tool's sensors fast).
REPORT_TIME = 1.000
SAMPLE_TIME = 0.001
SAMPLE_COUNT = 8
ADC_RANGE = 4095.     # compared in 12-bit counts (Prusa / released KlipperXL)
ADC_MIN = 20          # below = sensor not connected (released KlipperXL)
ADC_MAX = 4096        # released FS_ADC_MAX
CAL_MIN_DIFF = 200    # released FS_CAL_MIN_DIFF


class XLToolFSensor:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.name = config.get_name().split()[-1]
        self.cal = config.getintlist('cal', None, count=2)
        ppins = self.printer.lookup_object('pins')
        self.adc = ppins.setup_pin('adc', config.get('pin'))
        report_time = config.getfloat('report_time', REPORT_TIME,
                                      minval=0.05)
        self.adc.setup_adc_sample(report_time, SAMPLE_TIME, SAMPLE_COUNT)
        self.adc.setup_adc_callback(self._adc_callback)
        self.raw = None
        self.state = 'unknown'
        self._cal_empty = None       # step 1 of CALIBRATE_FILAMENT_SENSOR
        gcode = self.printer.lookup_object('gcode')
        # the first [xl_tool_fsensor Tn] registers the shared commands
        if not self.printer.lookup_object('xl_tool_fsensor_cal', None):
            self.printer.add_object('xl_tool_fsensor_cal', self)
            gcode.register_command(
                "CALIBRATE_FILAMENT_SENSOR", self.cmd_CALIBRATE,
                desc="Tool filament sensor calibration, step 1 (no filament)")
            gcode.register_command(
                "CALIBRATE_FILAMENT_SENSOR_STEP2", self.cmd_CALIBRATE_STEP2,
                desc="Tool filament sensor calibration, step 2 (inserted)")
        gcode.register_mux_command("QUERY_TOOL_FSENSOR", "TOOL", self.name,
                                   self.cmd_QUERY_TOOL_FSENSOR,
                                   desc="Report a tool filament sensor")
    # Serial thread: hand the value to the reactor
    def _adc_callback(self, samples):
        value = int(samples[-1][1] * ADC_RANGE + .5)
        self.reactor.register_async_callback(
            lambda e, v=value: self._update(v))
    def _evaluate(self, value):
        if value < ADC_MIN:
            return 'not_connected'
        if self.cal is None:
            return 'not_calibrated'
        nins, ins = self.cal
        mid = (nins + ins) // 2
        hyst = abs(nins - ins) // 6
        if (self.state in ('has_filament', 'no_filament')
                and abs(value - mid) < hyst):
            return self.state
        return 'has_filament' if (value > mid) == (ins > mid) else 'no_filament'
    def _update(self, value):
        self.raw = value
        new = self._evaluate(value)
        old = self.state
        self.state = new
        if new != old:
            logging.info("xl_tool_fsensor %s: %s -> %s (raw %d)",
                         self.name, old, new, value)
            self.printer.send_event("xl_tool_fsensor:state", self.name,
                                    old, new)
    # Calibration (commands registered once, act on the TOOL=n sensor(s))
    def _sensors(self, gcmd):
        tool = gcmd.get_int('TOOL', None, minval=0, maxval=4)
        names = ["T%d" % tool] if tool is not None else ["T%d" % t
                                                          for t in range(5)]
        out = []
        for n in names:
            s = self.printer.lookup_object('xl_tool_fsensor ' + n, None)
            if s is None:
                if tool is not None:
                    raise gcmd.error("No [xl_tool_fsensor %s]" % (n,))
                continue
            out.append(s)
        return out
    def cmd_CALIBRATE(self, gcmd):
        for s in self._sensors(gcmd):
            raw = s.raw
            if raw is None or raw < ADC_MIN or raw > ADC_MAX:
                s._cal_empty = None
                gcmd.respond_info("%s: sensor not connected (raw=%s)"
                                  % (s.name, raw))
                continue
            s._cal_empty = raw
            gcmd.respond_info("%s: no-filament reference = %d" % (s.name, raw))
        gcmd.respond_info("Step 1 complete. Now INSERT filament into the"
                          " tool(s) and run:\n"
                          "  CALIBRATE_FILAMENT_SENSOR_STEP2 [TOOL=n]")
    def cmd_CALIBRATE_STEP2(self, gcmd):
        configfile = self.printer.lookup_object('configfile')
        saved = 0
        for s in self._sensors(gcmd):
            if s._cal_empty is None:
                gcmd.respond_info("%s: run CALIBRATE_FILAMENT_SENSOR first"
                                  " (step 1)" % (s.name,))
                continue
            raw = s.raw
            if raw is None or raw < ADC_MIN or raw > ADC_MAX:
                gcmd.respond_info("%s: sensor not connected (raw=%s)"
                                  % (s.name, raw))
                continue
            nins = s._cal_empty
            if abs(raw - nins) < CAL_MIN_DIFF:
                gcmd.respond_info(
                    "%s: FAILED - inserted value (%d) too close to empty value"
                    " (%d). Difference %d must be >= %d. Check that filament is"
                    " fully inserted." % (s.name, raw, nins, abs(raw - nins),
                                          CAL_MIN_DIFF))
                s._cal_empty = None
                continue
            s.cal = [nins, raw]
            s._cal_empty = None
            s._update(raw)
            configfile.set('xl_tool_fsensor ' + s.name, 'cal',
                           "%d, %d" % (nins, raw))
            saved += 1
            gcmd.respond_info("%s: calibrated, nins=%d ins=%d midpoint=%d"
                              " hysteresis=+-%d" % (s.name, nins, raw,
                                                    (nins + raw) // 2,
                                                    abs(nins - raw) // 6))
        if saved:
            gcmd.respond_info("Run SAVE_CONFIG to store the calibration.")
    def has_filament(self):
        return self.state == 'has_filament'
    def cmd_QUERY_TOOL_FSENSOR(self, gcmd):
        gcmd.respond_info("%s: %s (raw %s, cal %s)" % (
            self.name, self.state, self.raw,
            "%d/%d" % tuple(self.cal) if self.cal else "none"))
    def get_status(self, eventtime):
        return {'state': self.state, 'raw': self.raw}

def load_config_prefix(config):
    return XLToolFSensor(config)
