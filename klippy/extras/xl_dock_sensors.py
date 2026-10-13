# KlipperXL-Plus - Prusa XL Dwarf "picked" / "parked" hall sensors
#
# Each Dwarf has two hall sensors on its dock latch.  Read with Klipper's
# own ADC on the Dwarf and judged exactly as Prusa's Dwarf firmware does
# (src/puppy/dwarf/Cheese.hpp / Cheese.cpp):
#   picked = AdcGet::picked1() = ADC ch1 = PA1, parked = picked0() = ch2 = PA2
#   (Dwarf ADC rank order, device/stm32g0/peripherals.cpp + common/adc.hpp)
#   12-bit: magnet 1470, none 761; ON above 1165, OFF below 1065 (midpoint
#   +/- HALL_HYSTERESIS/2), state kept in between.
#
#   [xl_dock_sensors T0]
#   picked_pin: dwarf_t0:PA1
#   parked_pin: dwarf_t0:PA2
#   #report_time: 0.100          # the picked tool (and during pick / park)
#   #parked_report_time: 1.000   # a parked tool
#
# Parked tools report slowly (owner design 2026-10-04, as stock Prusa reads
# an unselected Dwarf only in its slow refresh, src/puppies/puppy_task.cpp):
# every idle Dwarf sent ~660 B/s, mostly these 0.1 s reports, on the one
# shared RS485 bus.  set_active() switches a tool's dock and tool filament
# sensors between the two rates; the toolchanger makes a tool fast before
# it reads its sensors (pick) and slow once it is parked.
# Every tool STARTS at the parked rate; only the tool found on the carriage
# is switched fast.  Starting fast and switching the docked ones at ready
# sent 3 commands to every Dwarf at once (printer 10/06): a warm tool's
# heatbreak fan command, scheduled ~95 ms ahead by Klipper at ready, queued
# behind that burst on the bus and reached its Dwarf 26-33 ms late ->
# "Timer too close" shutdown on every restart with a tool above 35 C.
#
# Copyright (C) 2026  Richard Crook
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging, math

HALL_ON_STATE, HALL_OFF_STATE, HALL_HYSTERESIS = 1470, 761, 100  # Cheese.hpp
HALL_MID = (HALL_ON_STATE + HALL_OFF_STATE) // 2
HALL_LOW = HALL_MID - HALL_HYSTERESIS // 2
HALL_HIGH = HALL_MID + HALL_HYSTERESIS // 2
REPORT_TIME = 0.100
SAMPLE_TIME = 0.001
SAMPLE_COUNT = 8
ADC_RANGE = 4095.


QUERY_ANALOG_IN = ("query_analog_in oid=%c clock=%u sample_ticks=%u"
                   " sample_count=%c rest_ticks=%u bytes_per_report=%c"
                   " min_value=%hu max_value=%hu range_check_count=%c")
QUERY_ANALOG_IN_OLD = ("query_analog_in oid=%c clock=%u sample_ticks=%u"
                       " sample_count=%c rest_ticks=%u min_value=%hu"
                       " max_value=%hu range_check_count=%c")

def set_adc_report_time(adc, report_time, lead=0.250):
    """Change a running Klipper ADC's report interval.

    Klipper sets it once (mcu.py MCU_adc._build_config); the MCU accepts
    query_analog_in at any time (src/adccmds.c: cancels and restarts the
    sampling timer).  The command is built exactly as _build_config builds
    it, and the host side's _report_clock (used to timestamp samples) is
    updated with it.  Sampling restarts `lead` seconds ahead, so the start
    clock is not in the past by the time it crosses the RS485 bridge."""
    if adc._oid is None:
        return
    mcu = adc.get_mcu()
    max_adc = adc._sample_count * mcu.get_constant_float("ADC_MAX")
    min_sample = max(0, min(0xffff, int(adc._min_sample * max_adc)))
    max_sample = max(0, min(0xffff, int(
        math.ceil(adc._max_sample * max_adc))))
    sample_ticks = mcu.seconds_to_clock(adc._sample_time)
    report_clock = mcu.seconds_to_clock(report_time)
    reactor = mcu.get_printer().get_reactor()
    start = mcu.print_time_to_clock(
        mcu.estimated_print_time(reactor.monotonic()) + lead)
    adc._report_time = report_time
    adc._report_clock = report_clock
    if adc._batch_num == 1 and mcu.try_lookup_command(
            QUERY_ANALOG_IN_OLD) is not None:
        mcu.lookup_command(QUERY_ANALOG_IN_OLD).send([
            adc._oid, start, sample_ticks, adc._sample_count, report_clock,
            min_sample, max_sample, adc._range_check_count])
        return
    mcu.lookup_command(QUERY_ANALOG_IN).send([
        adc._oid, start, sample_ticks, adc._sample_count, report_clock,
        adc._batch_num * 2, min_sample, max_sample, adc._range_check_count])


class HallSensor:
    def __init__(self, owner, ppins, pin, report_time, label):
        self.owner = owner
        self.label = label
        self.raw = None
        self.state = None            # unknown until the first sample
        self.time = 0.               # print time of the last sample
        self.adc = ppins.setup_pin('adc', pin)
        self.adc.setup_adc_sample(report_time, SAMPLE_TIME, SAMPLE_COUNT)
        self.adc.setup_adc_callback(self._callback)
    def _callback(self, samples):
        value = int(samples[-1][1] * ADC_RANGE + .5)
        t = samples[-1][0]
        self.owner.reactor.register_async_callback(
            lambda e, v=value, t=t: self._update(v, t))
    def _update(self, value, t=0.):
        self.time = t
        self.raw = value
        old = self.state
        if old is None:
            new = value > HALL_MID
        elif old and value < HALL_LOW:
            new = False
        elif not old and value > HALL_HIGH:
            new = True
        else:
            new = old
        self.state = new
        if new != old:
            logging.info("xl_dock_sensors %s %s: %s -> %s (raw %d)",
                         self.owner.name, self.label, old, new, value)
            if old is not None:
                # xl_toolchanger follows a tool picked / parked by hand
                self.owner.printer.send_event("xl_dock_sensors:changed",
                                              self.owner.name)


class XLDockSensors:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.name = config.get_name().split()[-1]
        ppins = self.printer.lookup_object('pins')
        rt = config.getfloat('report_time', REPORT_TIME, minval=0.05)
        self.fast_time = rt
        self.slow_time = config.getfloat('parked_report_time', 1.0,
                                         minval=rt)
        self.active = False          # configured at the parked rate
        self.picked = HallSensor(self, ppins, config.get('picked_pin'),
                                 self.slow_time, 'picked')
        self.parked = HallSensor(self, ppins, config.get('parked_pin'),
                                 self.slow_time, 'parked')
        gcode = self.printer.lookup_object('gcode')
        gcode.register_mux_command("QUERY_DOCK", "TOOL", self.name,
                                   self.cmd_QUERY_DOCK,
                                   desc="Report a tool's picked/parked sensors")
    def set_active(self, active):
        """Fast reports (picked tool, pick / park) or slow (parked)."""
        if active == self.active:
            return
        self.active = active
        t = self.fast_time if active else self.slow_time
        adcs = [self.picked.adc, self.parked.adc]
        fs = self.printer.lookup_object('xl_tool_fsensor ' + self.name, None)
        if fs is not None:
            adcs.append(fs.adc)
        for adc in adcs:
            set_adc_report_time(adc, t)
        logging.info("xl_dock_sensors %s: %s reports (%.2f s)", self.name,
                     "fast" if active else "parked", t)
    def wait_fresh(self, timeout=1.5):
        """Wait until both sensors have reported a sample taken after now.
        Returns False if none came within `timeout` seconds."""
        mcu = self.picked.adc.get_mcu()
        now = self.reactor.monotonic()
        since = mcu.estimated_print_time(now)
        end = now + timeout
        while self.reactor.monotonic() < end:
            if self.picked.time > since and self.parked.time > since:
                return True
            self.reactor.pause(self.reactor.monotonic() + 0.020)
        return False
    def is_picked(self):
        return bool(self.picked.state)
    def is_parked(self):
        return bool(self.parked.state)
    def cmd_QUERY_DOCK(self, gcmd):
        gcmd.respond_info("%s: picked=%s (raw %s) parked=%s (raw %s)" % (
            self.name, self.picked.state, self.picked.raw,
            self.parked.state, self.parked.raw))
    def get_status(self, eventtime):
        return {'picked': self.picked.state, 'parked': self.parked.state,
                'picked_raw': self.picked.raw, 'parked_raw': self.parked.raw}

def load_config_prefix(config):
    return XLDockSensors(config)
