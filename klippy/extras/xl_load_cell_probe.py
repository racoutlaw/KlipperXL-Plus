# KlipperXL-Plus - every Prusa XL tool's loadcell is a Z probe; the picked one
# is "the" printer probe
#
# Klipper's [load_cell_probe] (load_cell_probe.py LoadCellPrinterProbe) is
# one probe per printer: it builds its own sensor + LoadCell and registers
# itself as 'probe'.  The XL needs one per tool - a single-tool print homes Z
# with its own tool, a multi-tool print with T0 (KlipperXL-Plus spec).  This
# module builds, per tool, the SAME Klipper pieces LoadCellPrinterProbe
# builds (config helper, MCU trigger_analog + sos filter, continuous tare
# filter, LoadCellProbingMove, TappingMove, TapSession, sample averaging),
# with xl_load_cell.XLLoadCell as the load cell so only the selected tool
# streams.  One [xl_load_cell_probe] object is 'probe' and routes the probe
# interface and PROBE / LOAD_CELL_TEST_TAP to the picked tool.
#
#   [xl_load_cell_probe]              # the router ('probe'); probe options
#   [xl_load_cell_probe T0]           # per tool: sensor + load cell + probe
#   sensor_type: hx717
#   dout_pin: dwarf_t0:PA3
#   sclk_pin: dwarf_t0:PA0
#   counts_per_gram: 52.083           # Prusa loadcell scale 0.0192 g/count
#   reference_tare_counts: ...        # per printer: XL_LOAD_CELL_REFERENCE
#   trigger_force: 125                # Prusa thresholdStatic -125 g
#   z_offset: 0                       # loadcell triggers at nozzle contact
#
# Every probe (Z home, each mesh point) is tared just before it moves:
# LoadCellProbingMove.probing_move -> _pause_and_tare (Klipper).  Klipper
# takes that tare the moment the last move ends, so after a travel move it
# caught the head still shaking (owner 10/18: the first tap at each point
# read up to 0.044 mm high).  The head now stands SETTLE_TIME first, as
# released KlipperXL did before every probe.
#
# Copyright (C) 2026  Richard Crook
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging
import numpy
from . import probe, trigger_analog, hx71x
from . import load_cell_probe as lcp
from .xl_load_cell import XLLoadCell, XLLoadCellSelect

# load_cell_probe.py imports NumPy only inside LoadCellPrinterProbe.__init__
# ("global np"); its other classes, used here, need the module-level name.
lcp.np = numpy

# Released KlipperXL, before every probe after a move (puppy_bootloader.py
# run_probe): 0.2 s "wait for vibration to dampen after travel move", then
# 0.3 s (Prusa Z_FIRST_PROBE_DELAY "let vibrations settle before tare"), then
# a 48-sample tare (tool_template.cfg tare_time 0.15).  A dwell, so the tare's
# samples (collected from the toolhead's last move time) start after it.
SETTLE_TIME = 0.5


class XLToolProbe:
    """One tool's probe: the pieces of LoadCellPrinterProbe.__init__."""
    def __init__(self, config, router):
        self.name = config.get_name().split()[-1].upper()
        printer = config.get_printer()
        sensors = {}
        sensors.update(hx71x.HX71X_SENSOR_TYPES)
        sensor = config.getchoice('sensor_type', sensors)(config)
        self.load_cell = XLLoadCell(config, sensor)
        config_helper = lcp.LoadCellProbeConfigHelper(config, self.load_cell)
        mcu = self.load_cell.get_sensor().get_mcu()
        self.mcu_trigger_analog = trigger_analog.MCU_trigger_analog(sensor)
        cmd_queue = self.mcu_trigger_analog.get_dispatch().get_command_queue()
        sos_filter = trigger_analog.MCU_SosFilter(mcu, cmd_queue, 4)
        self.mcu_trigger_analog.setup_sos_filter(sos_filter)
        tare_filter = lcp.ContinuousTareFilterHelper(config, sensor,
                                                     sos_filter)
        self.param_helper = lcp.LoadCellParameterHelper(config)
        self.probe_offsets = probe.ProbeOffsetsHelper(config)
        self.probing_move = lcp.LoadCellProbingMove(
            config, self.load_cell, self.mcu_trigger_analog,
            self.param_helper, tare_filter, config_helper)
        self.tapping_move = lcp.TappingMove(config, self.probing_move,
                                            config_helper)
        tap_session = lcp.TapSession(config, self.tapping_move,
                                     self.probe_offsets, self.param_helper)
        self.probe_session = probe.SampleAveragingHelper(
            config, self.param_helper, tap_session.start_probe_session)
    def get_status(self, eventtime):
        status = self.load_cell.get_status(eventtime)
        status.update(self.tapping_move.get_status(eventtime))
        return status


class _StreamingSession:
    """A probe session that holds a stream count on the selected loadcell
    until it ends."""
    def __init__(self, session, sel, router):
        self._session = session
        self._sel = sel
        self._router = router
        self._open = True
    def run_probe(self, gcmd):
        self._router.printer.lookup_object('toolhead').dwell(SETTLE_TIME)
        return self._session.run_probe(gcmd)
    def pull_probed_results(self):
        return self._session.pull_probed_results()
    def release(self):
        """Give back the stream count (once)."""
        if self._open:
            self._open = False
            self._sel.stream_end()
            if self in self._router.open_sessions:
                self._router.open_sessions.remove(self)
    def end_probe_session(self):
        try:
            self._session.end_probe_session()
        finally:
            self.release()


class XLLoadCellProbeRouter:
    """The printer 'probe': forwards to the selected tool's probe."""
    def __init__(self, config):
        self.printer = config.get_printer()
        self.tools = {}
        self._cmd_helper = probe.ProbeCommandHelper(config, self)
        probe.HomingViaProbeHelper(config, 0.)
        self.printer.add_object('probe', self)
        # A probe error never reaches the wrapper's end_probe_session:
        # Klipper ends the inner session itself on gcode:command_error
        # (probe.py SampleAveragingHelper._handle_command_error), so the
        # stream count it held leaked (KlipperXL-Plus 2026-10-04).  Release it
        # on the same event.
        self.open_sessions = []
        self.printer.register_event_handler("gcode:command_error",
                                            self._handle_command_error)
        gcode = self.printer.lookup_object('gcode')
        gcode.register_command("LOAD_CELL_TEST_TAP", self.cmd_TEST_TAP,
                               desc="Wait for a tap on the picked tool")
        gcode.register_command("XL_PROBE_Z", self.cmd_XL_PROBE_Z,
                               desc="Z home probe: fast tap, slow taps, set Z")
        gcode.register_command("XL_LOAD_CELL_REFERENCE",
                               self.cmd_XL_LOAD_CELL_REFERENCE,
                               desc="Record the picked tool's loadcell"
                               " reference_tare_counts (nothing touching)")
    def add_tool(self, config, tool):
        # Every XL loadcell probes at its own nozzle, so the probe offsets
        # are one fixed value for the printer, not a per-tool one (per-tool
        # nozzle differences are [xl_toolchanger] offsets).  bed_mesh reads
        # them once at connect, with no tool picked yet, to lay out its grid.
        offsets = tool.probe_offsets.get_offsets()
        for other in self.tools.values():
            if other.probe_offsets.get_offsets() != offsets:
                raise config.error(
                    "[xl_load_cell_probe %s] x/y/z_offset must match %s"
                    % (tool.name, other.name))
        self.tools[tool.name] = tool
    def _active(self):
        sel = self.printer.lookup_object('xl_load_cell_select', None)
        name = sel.selected if sel is not None else None
        if name is None or name not in self.tools:
            raise self.printer.command_error(
                "No tool picked - its loadcell is the probe (XL_PICK first)")
        return self.tools[name]
    # probe interface (probe.py PrinterProbe)
    def get_probe_params(self, gcmd=None):
        return self._active().param_helper.get_probe_params(gcmd)
    def get_offsets(self, gcmd=None):
        # identical on every tool (add_tool), so no tool needs to be picked
        if not self.tools:
            raise self.printer.command_error("No [xl_load_cell_probe Tn]")
        return next(iter(self.tools.values())).probe_offsets.get_offsets(gcmd)
    def _sel(self):
        return self.printer.lookup_object('xl_load_cell_select')
    def start_probe_session(self, gcmd):
        tool = self._active()
        sel = self._sel()
        sel.stream_begin()
        try:
            session = tool.probe_session.start_probe_session(gcmd)
        except Exception:
            sel.stream_end()
            raise
        wrapped = _StreamingSession(session, sel, self)
        self.open_sessions.append(wrapped)
        return wrapped
    def _handle_command_error(self):
        for wrapped in list(self.open_sessions):
            logging.info("xl_load_cell_probe: probe error - releasing its"
                         " loadcell stream")
            wrapped.release()
    def get_status(self, eventtime):
        status = self._cmd_helper.get_status(eventtime)
        try:
            status.update(self._active().get_status(eventtime))
        except self.printer.command_error:
            pass
        return status
    # Z homing touch, the released KlipperXL LOADCELL_PROBE sequence
    # (reference puppy_bootloader.py l.7293-7460) on Klipper's probe:
    # fast tap 5 mm/s, back off 2 mm, slow tap 0.5 mm/s, then repeat slow
    # taps (0.5 mm back off) until two in a row agree within 0.030 mm (up to
    # 3 retries); Z is set from the last good slow tap.  Klipper tares
    # before every tap (LoadCellProbingMove._pause_and_tare); the tap
    # analysis gives the contact Z, the lift after each tap is the back off.
    def _tap(self, speed, retract, lift_speed):
        gcode = self.printer.lookup_object('gcode')
        g = gcode.create_gcode_command("PROBE", "PROBE", {
            "PROBE_SPEED": "%.3f" % speed, "LIFT_SPEED": "%.3f" % lift_speed,
            "SAMPLE_RETRACT_DIST": "%.3f" % retract, "SAMPLES": "1"})
        return probe.run_single_probe(self, g).bed_z
    def cmd_XL_PROBE_Z(self, gcmd):
        tool = self._active()
        # Klipper refuses an uncalibrated load cell ("Load Cell not
        # calibrated", load_cell_probe.py) before any probing move; say what
        # an XL needs instead (docs/RELEASE_CONFIG.md, fresh install) - the
        # wizard first, as the install guide says (owner 10/10: it caught him
        # on a fresh install; a new user is sent to the wizard)
        if not tool.load_cell.is_calibrated():
            raise gcmd.error(
                "%s loadcell has no reference yet (fresh install?): run Macros"
                " -> CALIBRATION_WIZARDS -> Loadcell reference (or by hand: heat"
                " it to M109 S150 with the nozzle clear of the bed,"
                " XL_LOAD_CELL_REFERENCE, then SAVE_CONFIG)" % (tool.name,))
        sel = self._sel()
        sel.stream_begin()           # one stream for the whole sequence
        try:
            self._probe_z(gcmd, tool)
        finally:
            sel.stream_end()
    def _probe_z(self, gcmd, tool):
        fast = gcmd.get_float('SPEED', 5., above=0.)
        slow = gcmd.get_float('SLOW_SPEED', 0.5, above=0.)
        backoff = gcmd.get_float('BACKOFF', 2.0, above=0.)
        repeat_backoff = gcmd.get_float('REPEAT_BACKOFF', 0.5, above=0.)
        tolerance = gcmd.get_float('TOLERANCE', 0.030, above=0.)
        retries = gcmd.get_int('RETRIES', 3, minval=0)
        z_fast = self._tap(fast, backoff, fast)
        gcmd.respond_info("%s fast tap: contact z=%.4f" % (tool.name, z_fast))
        prev = self._tap(slow, repeat_backoff, slow)
        gcmd.respond_info("%s slow tap 1: contact z=%.4f" % (tool.name, prev))
        final = None
        for attempt in range(1, retries + 1):
            z = self._tap(slow, repeat_backoff, slow)
            gcmd.respond_info("%s slow tap %d: contact z=%.4f (diff %.4f)"
                              % (tool.name, attempt + 1, z, abs(z - prev)))
            if abs(z - prev) <= tolerance:
                final = z
                break
            prev = z
        if final is None:
            raise gcmd.error("%s: slow taps did not agree within %.3f mm"
                             " after %d retries" % (tool.name, tolerance,
                                                    retries))
        toolhead = self.printer.lookup_object('toolhead')
        z_now = toolhead.get_position()[2] - final
        self.printer.lookup_object('gcode').run_script_from_command(
            "SET_KINEMATIC_POSITION Z=%.4f" % (z_now,))
        gcmd.respond_info("%s: Z homed - nozzle now %.3f mm above the bed"
                          % (tool.name, z_now))
    # Per-printer loadcell reference (docs/RELEASE_CONFIG.md).  Prusa uses one
    # fixed scale (0.0192 g/count = counts_per_gram 52.083, seeded in
    # printer.cfg), so only reference_tare_counts is measured per printer.
    # Klipper's own LoadCell.set_calibration() applies it and stages both
    # values for SAVE_CONFIG on this tool's [xl_load_cell_probe Tn] section;
    # the counts are averaged as LOAD_CELL_TARE does (avg_counts).
    # Taken at PROBING temperature: measured 2026-10-04 on T0, the raw reading
    # moves ~0.55 % of range (~46000 counts, ~890 g) between a ~45 C and a
    # 150 C nozzle (Dwarf board 30 C both), and probing uses
    # reference +- force_safety_limit (2000 g) as its safety band
    # (load_cell_probe.py get_safety_range) - a cold reference would sit
    # almost half way to the band edge when probing hot.
    def cmd_XL_LOAD_CELL_REFERENCE(self, gcmd):
        tool = self._active()
        temp = gcmd.get_float('TEMP', 150., minval=0.)   # release PROBE_TEMP
        ext_name = "extruder" if tool.name == "T0" else "extruder" + tool.name[1:]
        ext = self.printer.lookup_object(ext_name)
        cur = ext.get_heater().get_status(
            self.printer.get_reactor().monotonic())['temperature']
        if abs(cur - temp) > 5.:
            raise gcmd.error("%s nozzle is %.1f C - heat it to the probing"
                             " temperature first (M109 S%.0f) so the reference"
                             " matches probing conditions" % (tool.name, cur,
                                                              temp))
        want = gcmd.get('TOOL', None)
        if want is not None:
            want = want.strip().upper()
            if not want.startswith("T"):
                want = "T" + want              # TOOL=0 and TOOL=T0 both work
        if want is not None and want != tool.name:
            raise gcmd.error("%s is not the picked tool (%s) - pick it first:"
                             " only the picked tool's loadcell streams"
                             % (want, tool.name))
        cell = tool.load_cell
        cpg = cell.counts_per_gram
        if cpg is None:
            raise gcmd.error("%s has no counts_per_gram (seed it in"
                             " printer.cfg: 52.083)" % (tool.name,))
        sel = self._sel()
        sel.stream_begin()
        try:
            counts = cell.avg_counts()
        finally:
            sel.stream_end()
        old = cell.reference_tare_counts
        cell.set_calibration(cpg, counts)
        gcmd.respond_info("%s: reference_tare_counts %s -> %d (counts_per_gram"
                          " %.3f kept). Run SAVE_CONFIG to store it."
                          % (tool.name, old, counts, cpg))
    # load_cell_probe.LoadCellProbeCommands.cmd_LOAD_CELL_TEST_TAP, same
    # logic, routed to the picked tool
    def cmd_TEST_TAP(self, gcmd):
        tool = self._active()
        taps = gcmd.get_int("TAPS", 3, minval=1, maxval=10)
        timeout = gcmd.get_float("TIMEOUT", 30., minval=1., maxval=120.)
        gcmd.respond_info("%s: tap the load cell %s times:" % (tool.name, taps))
        reactor = self.printer.get_reactor()
        sel = self._sel()
        sel.stream_begin()
        try:
            for i in range(taps):
                result = tool.probing_move.probing_test(gcmd, timeout)
                if result == 0.:
                    raise gcmd.error("Test timeout out")
                gcmd.respond_info("Tap Detected!")
                reactor.pause(reactor.monotonic() + 0.2)
        finally:
            sel.stream_end()
        gcmd.respond_info("Test complete, %s taps detected" % (taps,))


def load_config(config):
    printer = config.get_printer()
    router = XLLoadCellProbeRouter(config)
    printer.add_object('xl_load_cell_probe_router', router)
    return router

def load_config_prefix(config):
    printer = config.get_printer()
    router = printer.lookup_object('xl_load_cell_probe_router', None)
    if router is None:
        raise config.error("[xl_load_cell_probe] (the router) must come"
                           " before the per-tool sections")
    sel = printer.lookup_object('xl_load_cell_select', None)
    if sel is None:
        sel = XLLoadCellSelect(printer)
        printer.add_object('xl_load_cell_select', sel)
    tool = XLToolProbe(config, router)
    sel.add(tool.load_cell)
    router.add_tool(config, tool)
    return tool
