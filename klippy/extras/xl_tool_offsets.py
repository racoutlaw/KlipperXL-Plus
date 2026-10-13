# KlipperXL-Plus - tool offsets on the calibration pin
#
# The released KlipperXL CALIBRATE_TOOL_OFFSETS (puppy_bootloader.py @1b681b3:
# cmd_CALIBRATE_TOOL_OFFSETS l.1063-1718, run_probe l.462-632, _arc_move_to
# l.798-830, _probe_xy_verified l.832-876, _probe_xy_at_angle l.878-1007,
# _check_xy_deviation l.1009-1022, _fit_circle_center l.1024-1061), ported.
# Every move is the released one - same targets, order, speeds, the arc
# around the pin, the lifts - and so is the acceleration it ran at: 500 from
# the start until the G28 of step 4 (its homing_override puts the printer
# max_accel back), then 500 only inside each touch.  Only the plumbing
# changes (docs/CALIBRATION_WIZARDS.md section 2, "KlipperXL-Plus changes"):
#   - the WAIT_TIME pauses are wizard dialogs with Continue: xl_wizards.py
#     runs the steps below between them (XL_WIZARD NAME=offsets)
#   - SET_DWARF_TEMP -> M104 Tn; the Dwarf print fan latch -> xl_toolchanger
#     hold_fan; tools are picked with XL_PICK (a spool join left over would
#     turn T<n> into another tool)
#   - the touches are Klipper's load cell probe on the Dwarf instead of the
#     XLBuddy firmware's MODBUS loadcell endstop:
#       Z   the tool's LoadCellProbingMove tare (125 g) and a probing move to
#           max(Z - travel, position_min), the released run_probe target -
#           not Klipper's tap, which runs to position_min and fits the lift
#       XY  the same trigger at 40 g with the X/Y (A/B) motors on its trsync
#           instead of Z (_xy_probing_move).  Klipper's trigger is "abs_ge"
#           (load_cell_probe.py _pause_and_tare), so a push from any side
#           trips it, as the released xy_mode=1 did.
#   - the offsets are saved as [xl_toolchanger] offset_tN by SAVE_CONFIG
#
#   CALIBRATE_TOOL_OFFSETS [TOOLS=0,1,..] [SKIP_CLEAN=1] [DRY_RUN=1]
#       [PIN_X= PIN_Y= PIN_DIAMETER= PIN_HEIGHT= SPEED= PROBE_SPEED=
#        CLEAN_TEMP= CAL_TEMP= CLEAN_TIME=]      released names and defaults
#
# Copyright (C) 2026  Richard Crook
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging
import math
from . import probe

# released run_probe / loadcell_probe constants (puppy_bootloader.py
# l.259-271): Z 1.0 mm/s, lift 5.0 mm/s, sample retract 0.2 mm, Z 125 g,
# XY 40 g (Prusa loadcell.hpp XY_PROBE_THRESHOLD)
Z_PROBE_SPEED = 1.0
LIFT_SPEED = 5.0
SAMPLE_RETRACT_DIST = 0.2
Z_THRESHOLD = 125.
XY_THRESHOLD = 40.
PROBE_ACCEL = 500.        # Prusa XY_ACCELERATION_MMSS
PROBE_TRAVEL = 15.0       # run_probe default travel

# Prusa G425 phased probing constants (released l.1118-1131)
PROBE_Z_BORE_MM = 1.0
PROBE_Z_UNCERTAIN_DIST_MM = 5.0
PROBE_Z_CERTAIN_DIST_MM = 1.0
PROBE_XY_UNCERTAIN_DIST_MM = 8.0
# Prusa G425.cpp: a hit that moved less than MIN_TRAVELED_DISTANCE_MM
# triggered too early and is discarded (l.139, 331); agreed hits further
# than PROBE_FAIL_THRESHOLD_MM from where the pin should be stop the
# calibration (l.136, 352 synthetic_probe, 404); final-pass points that
# deviate more than MAX_DEVIATION_MM from their circle = "Tool N center not
# found": stop, nothing saved (l.140, 495, 525, 708).  The release only
# warned and kept such hits (owner 10/10: one early T3 hit -> Y 1.55 mm).
MIN_TRAVELED_DISTANCE_MM = 0.1
PROBE_FAIL_THRESHOLD_MM = 3.0
MAX_DEVIATION_MM = 0.2
# Stock decides an XY touch on a band-pass filtered loadcell signal
# (loadcell.cpp:217-228, |filtered| > XY_PROBE_THRESHOLD 40 g), not on the
# raw force: loadcell.hpp XYFilterParam "Tweaked butter(2, [0.005 0.08])",
# B = [1 0 -2 0 1] / GAIN, A = [1 -3.661929127367906 5.041628953899732
# -3.096320393316955 0.716633873504158], GAIN = 1 / 1.185768264324116e-02.
# Here as the two second-order sections Klipper's sos filter takes (poles
# paired, gain split evenly; same response to 3 decimals): at 320 sps -3 dB
# at 0.8 and 12.8 Hz, gain 1.00 at 2-5 Hz, 0.08 at 40 Hz.  Printer 10/10:
# with the raw force, the jolt as a +Y probe move starts tripped 40 g after
# 0.05 mm (T1, T3 at 270 deg) and slow builds tripped late (T0 120 deg,
# 4.35-5.31 mm travel at one bearing).  Z probing keeps the raw force.
PRUSA_XY_SOS = (
    (0.108892987116899, 0., -0.108892987116899,
     1., -1.683790041692252, 0.73244548588146),
    (0.108892987116899, 0., -0.108892987116899,
     1., -1.978139085675652, 0.978412574475389))


class _PrusaXYFilter:
    """A fixed filter design for trigger_analog.MCU_SosFilter (the same
    calls as trigger_analog.DigitalFilter) - no scipy needed."""
    def get_filter_sections(self):
        return [list(sec) for sec in PRUSA_XY_SOS]
    def get_initial_state(self):
        return [[0., 0.] for _ in PRUSA_XY_SOS]
    def get_size(self):
        return len(PRUSA_XY_SOS)
NUM_Z_MEASUREMENTS = 20
PHASE_XY_HITS = [3, 3, 12]
PHASE_Z_HITS = [1, 0, NUM_Z_MEASUREMENTS]
PHASE_CLEARANCE = [PROBE_XY_UNCERTAIN_DIST_MM, PROBE_Z_UNCERTAIN_DIST_MM,
                   PROBE_Z_CERTAIN_DIST_MM]
PHASE_NAMES = ["first (rough)", "second (refine)", "final (accurate)"]

# cleaning position - front centre, bed low (released l.1164-1166)
CLEAN_X, CLEAN_Y, CLEAN_Z = 180.0, 50.0, 100.0
TRAVEL_Z = 50.0


class XLToolOffsets:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        self.z_min = probe.lookup_minimum_z(config)
        self.xy_steppers = []
        self.result = None        # (offsets, offsets in use before)
        self.saved_accel = None   # until the step-4 G28 puts max_accel back
        self.printer.register_event_handler("klippy:mcu_identify",
                                            self._identify)
        self.printer.register_event_handler("klippy:ready", self._ready)
        self.gcode.register_command(
            "CALIBRATE_TOOL_OFFSETS", self.cmd_CALIBRATE_TOOL_OFFSETS,
            desc="Tool offsets on the calibration pin (guided)")
    def _identify(self):
        # the motors an XY move drives (CoreXY: stepper_x / stepper_y = A/B)
        kin = self.printer.lookup_object('toolhead').get_kinematics()
        self.xy_steppers = [s for s in kin.get_steppers()
                            if s.is_active_axis('x') or s.is_active_axis('y')]
    def _ready(self):
        # the XY touch plumbing as found (log only): each tool trigger's
        # trsyncs and the motors the XY touch puts on them
        router = self.printer.lookup_object('xl_load_cell_probe_router', None)
        xy = ", ".join("%s@%s" % (s.get_name(), s.get_mcu().get_name())
                       for s in self.xy_steppers)
        for name, tp in sorted((router.tools if router else {}).items()):
            trsyncs = getattr(tp.mcu_trigger_analog.get_dispatch(),
                              '_trsyncs', [])
            logging.info("xl_tool_offsets: %s trigger trsyncs %s; XY touch"
                         " motors %s", name, "; ".join(
                             "%s [%s]" % (t.get_mcu().get_name(), ", ".join(
                                 s.get_name() for s in t.get_steppers()))
                             for t in trsyncs), xy)
    # ---- parameters (released names and defaults, l.1101-1116) -----------
    def params(self, gcmd=None, installed=()):
        def get_float(name, default, **kw):
            if gcmd is None:
                return default
            return gcmd.get_float(name, default, **kw)
        def get_int(name, default):
            if gcmd is None:
                return default
            return gcmd.get_int(name, default)
        p = {'pin_x': get_float('PIN_X', 180.0),
             'pin_y': get_float('PIN_Y', 180.0),
             'pin_diameter': get_float('PIN_DIAMETER', 6.0, above=0.),
             'pin_height': get_float('PIN_HEIGHT', 9.0, above=0.),
             'speed': get_float('SPEED', 3.0, above=0.),
             'probe_speed': get_float('PROBE_SPEED', Z_PROBE_SPEED, above=0.),
             'skip_clean': get_int('SKIP_CLEAN', 0),
             # owner: 200 -> 100 (10/08, loaded nozzles oozed), 100 -> 140
             # (10/10, stuck plastic would not come off at 100)
             'clean_temp': get_float('CLEAN_TEMP', 140.0, minval=0.),
             'cal_temp': get_float('CAL_TEMP', 70.0, minval=0.),
             'clean_time': get_float('CLEAN_TIME', 30.0, minval=0.),
             'dry_run': get_int('DRY_RUN', 0)}
        tools = gcmd.get('TOOLS', None) if gcmd is not None else None
        if tools is None or tools.strip().lower() == 'all':
            p['tools'] = list(installed)
        else:
            try:
                p['tools'] = [int(t.strip()) for t in
                              tools.replace('T', '').replace('t', '').split(',')
                              if t.strip()]
            except ValueError:
                raise gcmd.error("Invalid TOOLS parameter - use"
                                 " comma-separated numbers")
        for t in p['tools']:
            if t not in installed:
                raise self.printer.command_error(
                    "T%d is not installed on this printer" % (t,))
        if 0 not in p['tools']:
            raise self.printer.command_error(
                "Tool 0 must be calibrated as reference")
        return p
    def cmd_CALIBRATE_TOOL_OFFSETS(self, gcmd):
        wizards = self.printer.lookup_object('xl_wizards')
        p = self.params(gcmd, self._tc()._installed())
        wizards.start('offsets', p['tools'], params=p)
    # ---- helpers -----------------------------------------------------------
    def _tc(self):
        return self.printer.lookup_object('xl_toolchanger')
    def _toolhead(self):
        return self.printer.lookup_object('toolhead')
    def _run(self, script):
        self.gcode.run_script_from_command(script)
    def _info(self, msg):
        self.gcode.respond_info(msg)
    def _pause(self, seconds):
        self.reactor.pause(self.reactor.monotonic() + seconds)
    def _temp(self, tool):
        ext = 'extruder' if tool == 0 else 'extruder%d' % (tool,)
        heater = self.printer.lookup_object(ext).get_heater()
        return heater.get_status(self.reactor.monotonic())['temperature']
    def _set_accel(self, accel):
        self._toolhead().set_max_velocities(None, accel, None, None)
    def _heaters_fans_off(self, tools):
        # heaters off and any fan held by this routine released and off
        # (released: SET_DWARF_TEMP 0, _fan_selftest_exit, _fan_write 0)
        tc = self._tc()
        for t in tools:
            self._run("M104 T%d S0" % (t,))
            tc.hold_fan(t, None)
            tc._fan_set(t, 0., fatal=False)
    def release_fans(self):
        tc = self.printer.lookup_object('xl_toolchanger', None)
        if tc is not None:
            for t in list(tc.fan_held):
                tc.hold_fan(t, None)
    def end(self):
        """Wizard stopped or finished: nothing held, accel as before, and the
        offsets in use put back if probing zeroed them and nothing was saved
        (a stop at the remove-the-pin dialog)."""
        self.release_fans()
        if self.result is not None:
            self._restore_offsets(self.result[1])
        if self.saved_accel is not None:
            self._set_accel(self.saved_accel)
            self.saved_accel = None
        self.result = None
    # ---- steps (xl_wizards runs them between the dialogs) -----------------
    def step_begin(self, p):
        """Released start + STEP 1 (l.1145-1203)."""
        tools = p['tools']
        if p['dry_run']:
            self._info("*** DRY RUN - nothing will be saved ***")
        self._info("Tools: %s   XY speed: %.2f mm/s   Z speed: %.2f mm/s"
                   % (','.join(str(t) for t in tools), p['speed'],
                      p['probe_speed']))
        # Prusa XY_ACCELERATION_MMSS limit (500 mm/s^2) for precision probing;
        # the G28 of step 4 puts the printer max_accel back (homing_override)
        toolhead = self._toolhead()
        if self.saved_accel is None:
            self.saved_accel = toolhead.max_accel
        self._set_accel(min(PROBE_ACCEL, toolhead.max_accel))
        self._info("=== TOOL OFFSET CALIBRATION (Prusa G425 Method) ===")
        self._info("Pin: X=%.1f Y=%.1f D=%.1f H=%.1f"
                   % (p['pin_x'], p['pin_y'], p['pin_diameter'],
                      p['pin_height']))
        self._info("Probes: %d XY radial, %d Z averaged (Prusa standard)"
                   % (sum(PHASE_XY_HITS), sum(PHASE_Z_HITS)))
        self._info("=== STEP 1: TURNING OFF HEATERS ===")
        self._heaters_fans_off(tools)
    def step_clean(self, p, say):
        """Released STEP 3 (l.1215-1279)."""
        tools = p['tools']
        toolhead = self._toolhead()
        self._info("=== STEP 3: NOZZLE CLEANING PHASE ===")
        homed = toolhead.get_status(self.reactor.monotonic())['homed_axes']
        if not all(axis in homed for axis in 'xyz'):
            say("Homing before cleaning...")
            self._info("Machine not homed (homed_axes=%r) - homing before"
                       " cleaning..." % (homed,))
            self._run("G28")
            toolhead.wait_moves()
            self.saved_accel = None      # G28 put max_accel back
        say("Heating all nozzles to %.0f C for cleaning..." % (p['clean_temp'],))
        for t in tools:
            self._run("M104 T%d S%.0f" % (t, p['clean_temp']))
        for t in tools:
            self._info("Waiting for T%d to reach %.0fC..." % (t, p['clean_temp']))
            while self._temp(t) < p['clean_temp'] - 5:
                self._pause(1.0)
        self._info("All nozzles at cleaning temperature!")
        self._info(">>> CLEAN ALL NOZZLES with brass brush")
        self._info(">>> CLEAN PARKING PLATES as well")
        self._info(">>> Each tool will be presented for %.0f seconds"
                   % (p['clean_time'],))
        for t in tools:
            say("Presenting T%d for cleaning..." % (t,))
            self._run("XL_PICK TOOL=%d" % (t,))
            toolhead.wait_moves()
            self._run("G1 X%.1f Y%.1f Z%.1f F6000" % (CLEAN_X, CLEAN_Y, CLEAN_Z))
            toolhead.wait_moves()
            say("CLEAN T%d NOW with the brass brush - %.0f seconds. Clean the"
                " parking plates as well." % (t, p['clean_time']))
            self._info(">>> CLEAN T%d NOW! (%.0f seconds) <<<"
                       % (t, p['clean_time']))
            self._pause(p['clean_time'])
            self._info("T%d cleaning time complete" % (t,))
        self._run("XL_PARK")
        toolhead.wait_moves()
        self._info("All nozzles cleaned!")
    def step_cool(self, p):
        """Released "START COOLING NOW" (l.1281-1305): every tool to CAL_TEMP,
        a hot one gets its print fan held at 100 % in the background."""
        tc = self._tc()
        self._info("Setting nozzles to %.0fC and cooling in the background..."
                   % (p['cal_temp'],))
        for t in p['tools']:
            self._run("M104 T%d S%.0f" % (t, p['cal_temp']))
            if self._temp(t) > p['cal_temp'] + 5:
                tc.hold_fan(t, 1.)
    def step_home_park(self, p, say):
        """Released STEP 4 (l.1307-1326)."""
        toolhead = self._toolhead()
        tc = self._tc()
        self._info("=== STEP 4: HOMING AND PARKING FOR PIN INSTALL ===")
        if tc.active_tool < 0:
            say("Picking T0...")
            self._info("Picking T0...")
            self._run("XL_PICK TOOL=0")
            toolhead.wait_moves()
        say("Homing X, Y, Z (Z on the sheet with the loadcell)...")
        self._info("Homing X, Y, Z (Z uses T0 loadcell on bed sheet)...")
        self._run("G28")
        toolhead.wait_moves()
        self.saved_accel = None          # G28 put max_accel back
        self._info("All axes homed")
        say("Parking...")
        self._info("Parking T%d..." % (tc.active_tool,))
        self._run("XL_PARK")
        toolhead.wait_moves()
    def step_wait_temp(self, p, say):
        """Released STEP 6 (l.1339-1400)."""
        tc = self._tc()
        tools, cal = p['tools'], p['cal_temp']
        self._info("=== STEP 6: WAITING FOR CALIBRATION TEMP ===")
        self._info("Setting nozzles to %.0fC for calibration..." % (cal,))
        for t in tools:
            self._run("M104 T%d S%.0f" % (t, cal))
        cooling = set()
        for t in tools:
            temp = self._temp(t)
            if temp > cal + 5:
                tc.hold_fan(t, 1.)
                cooling.add(t)
                self._info("T%d at %.0fC - fan ON, cooling to %.0fC..."
                           % (t, temp, cal))
            else:
                # already there: the background cooling fan goes off
                tc.hold_fan(t, None)
                self._info("Waiting for T%d to reach %.0fC..." % (t, cal))
        try:
            pending = list(tools)
            shown = 0.
            while pending:
                for t in list(pending):
                    temp = self._temp(t)
                    if cal - 5 <= temp <= cal + 5:
                        pending.remove(t)
                        if t in cooling:
                            tc.hold_fan(t, None)
                            cooling.discard(t)
                        self._info("T%d at %.0fC - ready" % (t, temp))
                if pending:
                    now = self.reactor.monotonic()
                    if now - shown >= 5.:
                        shown = now
                        say("Waiting for the nozzles to reach %.0f C: %s"
                            % (cal, ", ".join("T%d %.0f C" % (t, self._temp(t))
                                              for t in pending)))
                    self._pause(1.0)
        finally:
            for t in list(cooling):
                tc.hold_fan(t, None)
        self._info("All nozzles at calibration temperature!")
    def step_probe(self, p, say):
        """Released STEP 7 + offsets + park + heaters off (l.1402-1649)."""
        tc = self._tc()
        tools = p['tools']
        self._info("=== STEP 7: CALIBRATION PROBING ===")
        self._info("Clearing bed mesh for calibration...")
        self._run("BED_MESH_CLEAR")
        # what was in use before zeroing: the MEASURED vs CURRENT table and
        # DRY_RUN put it back (released l.1411-1417)
        saved = dict(tc.offsets)
        self._info("Resetting all tool offsets to zero...")
        for t in tools:
            tc.offsets[t] = (0.0, 0.0, 0.0)
        self._run("SET_GCODE_OFFSET X=0 Y=0 Z=0 MOVE=0")
        self._info("All offsets zeroed")
        try:
            centers = {}
            for n, t in enumerate(tools):
                say("T%d (%d of %d): probing the calibration pin..."
                    % (t, n + 1, len(tools)))
                centers[t] = self._calibrate_tool(p, t)
            offsets = self._offsets(centers)
        except Exception:
            # released left the zeroed offsets in use until a restart - the
            # next print would be misaligned; put them back
            self._restore_offsets(saved)
            raise
        self._info("Parking tool...")
        self._run("XL_PARK")
        self._toolhead().wait_moves()
        self._info("Turning off heaters...")
        self._heaters_fans_off(tools)
        self.result = (offsets, saved)
    def step_finish(self, p):
        """Released STEP 8 report + DRY_RUN / apply (l.1661-1714).  Returns
        True when the offsets are staged for SAVE_CONFIG."""
        tc = self._tc()
        offsets, saved = self.result
        self._info("=== CALIBRATION COMPLETE ===")
        self._info("=== MEASURED vs CURRENT ===")
        lines = ["        current Z    measured Z     change"]
        for t in sorted(offsets):
            cur_z = saved.get(t, (0., 0., 0.))[2]
            new_z = offsets[t][2]
            lines.append("  T%d:   %+8.4f    %+8.4f    %+8.4f"
                         % (t, cur_z, new_z, new_z - cur_z))
        self._info("\n".join(lines))
        if p['dry_run']:
            self._restore_offsets(saved)
            self._info("=== DRY RUN - NOTHING WAS SAVED ===\n"
                       "Tool offsets restored to their previous values,\n"
                       "and printer.cfg was not written.\n"
                       "Re-run without DRY_RUN=1 to apply.\n\n"
                       "NOTE: applying these means the per-tool Z fine-tune\n"
                       "values (z_adjust_tN) were tuned against the OLD\n"
                       "offsets, so re-do the first-layer fine-tune on\n"
                       "each tool afterwards.")
            return False
        configfile = self.printer.lookup_object('configfile')
        for t, (ox, oy, oz) in sorted(offsets.items()):
            tc.offsets[t] = (ox, oy, oz)
            tc.offset_calibrated[t] = True
            configfile.set('xl_toolchanger', 'offset_t%d' % (t,),
                           "%.4f, %.4f, %.4f" % (ox, oy, oz))
        self._info("Offsets staged for printer.cfg [xl_toolchanger]"
                   " - SAVE_CONFIG writes them")
        return True
    def _restore_offsets(self, saved):
        tc = self._tc()
        tc.offsets.update(saved)
        if tc.active_tool >= 0:
            try:
                tc._apply_offset(tc.active_tool)
            except Exception as e:
                logging.warning("xl_tool_offsets: offset restore failed: %s"
                                % (e,))
    # ---- one tool (released l.1431-1609) ----------------------------------
    def _calibrate_tool(self, p, tool_num):
        toolhead = self._toolhead()
        tc = self._tc()
        pin_x, pin_y, pin_height = p['pin_x'], p['pin_y'], p['pin_height']
        safe_z = pin_height + PROBE_Z_UNCERTAIN_DIST_MM      # 9 + 5 = 14
        start_radius = p['pin_diameter'] / 2 + 5.0           # 8
        probe_distance = p['pin_diameter'] / 2 + 2.0         # 5
        self._info("--- Calibrating Tool %d ---" % (tool_num,))
        self._info("  Picking T%d..." % (tool_num,))
        self._run("XL_PICK TOOL=%d" % (tool_num,))
        toolhead.wait_moves()
        self._pause(0.5)
        active_tool = tc.active_tool
        if active_tool < 0:
            raise self.printer.command_error(
                "Tool %d not picked properly (active_tool=%d)"
                % (tool_num, active_tool))
        if active_tool != tool_num:
            raise self.printer.command_error(
                "Wrong tool active: expected T%d, got T%d"
                % (tool_num, active_tool))
        tp = self._tool_probe(tool_num)
        self._info("  T%d picked successfully" % (tool_num,))
        current_center = (pin_x, pin_y, pin_height)
        curpos = toolhead.get_position()
        self._info("  Current position: X=%.1f Y=%.1f Z=%.1f"
                   % (curpos[0], curpos[1], curpos[2]))
        self._info("  Moving Z up to %.1fmm for safe travel..." % (TRAVEL_Z,))
        toolhead.manual_move([None, None, TRAVEL_Z], 10.0)
        toolhead.wait_moves()
        self._info("  Moving to pin XY location (X=%.1f, Y=%.1f)..."
                   % (pin_x, pin_y))
        toolhead.manual_move([pin_x, pin_y, None], 50.0)
        toolhead.wait_moves()
        self._info("  Moving Z down to %.1fmm (above pin)..." % (safe_z,))
        toolhead.manual_move([None, None, safe_z], 10.0)
        toolhead.wait_moves()
        for phase in range(3):
            num_z = PHASE_Z_HITS[phase]
            num_xy = PHASE_XY_HITS[phase]
            clearance = PHASE_CLEARANCE[phase]
            self._info("  --- Phase %d: %s ---\n      %d Z probes, %d XY"
                       " probes, %.1fmm clearance"
                       % (phase, PHASE_NAMES[phase], num_z, num_xy, clearance))
            if num_z > 0:
                self._info("    Probing Z (%d samples)..." % (num_z,))
                # RAISE Z FIRST, THEN TRAVEL IN XY (released 2026-08-21
                # lateral crash: out of an XY sweep the nozzle is below the
                # pin top)
                toolhead.manual_move([None, None, pin_height + clearance], 10.0)
                toolhead.wait_moves()
                toolhead.manual_move([current_center[0], current_center[1],
                                      None], 25.0)
                toolhead.wait_moves()
                z_samples = []
                for z_i in range(num_z):
                    z_result = self._run_probe(tp, clearance + 5.0, p)
                    z_samples.append(z_result[2])
                    if num_z <= 3:
                        self._info("      Z sample %d/%d: %.4f"
                                   % (z_i + 1, num_z, z_result[2]))
                    toolhead.manual_move([None, None, z_result[2] + clearance],
                                         10.0)
                    toolhead.wait_moves()
                avg_z = sum(z_samples) / len(z_samples)
                current_center = (current_center[0], current_center[1], avg_z)
                if num_z > 3:
                    self._info("      Z average: %.4f (range: %.4f)"
                               % (avg_z, max(z_samples) - min(z_samples)))
                else:
                    self._info("      Z: %.4f" % (avg_z,))
            self._info("    Probing XY (%d probes)..." % (num_xy,))
            xy_probe_z = current_center[2] - PROBE_Z_BORE_MM
            hits = []
            for i in range(num_xy):
                angle = 2 * math.pi * i / num_xy
                try:
                    hit = self._probe_xy_verified(
                        tp, current_center[0], current_center[1], xy_probe_z,
                        angle, start_radius, probe_distance, p['speed'],
                        toolhead, p['pin_diameter'] / 2.)
                    hits.append(hit)
                except Exception as e:
                    self._info("      XY probe %d FAILED: %s" % (i + 1, e))
                    # out of an XY sweep the nozzle is below the pin top: up
                    # first, so nothing after this moves it sideways into it
                    try:
                        toolhead.manual_move(
                            [None, None, current_center[2] + clearance], 10.0)
                        toolhead.wait_moves()
                    except Exception:
                        logging.exception("xl_tool_offsets: lift after a"
                                          " failed XY probe")
                    raise
                # NO Z retract between angles: the next one arcs round the pin
                toolhead.wait_moves()
            # SWEEP FINISHED - LIFT CLEAR NOW (released 2026-08-21 lesson)
            toolhead.manual_move([None, None, current_center[2] + clearance],
                                 10.0)
            toolhead.wait_moves()
            center_xy = self._fit_circle_center(hits)
            current_center = (center_xy[0], center_xy[1], current_center[2])
            if phase == 2:
                passed, max_dev = self._check_xy_deviation(center_xy, hits,
                                                           MAX_DEVIATION_MM)
                if not passed:
                    # stock: "G425: Tool N center not found" - stop, save
                    # nothing (the sweep already lifted clear above)
                    raise self.printer.command_error(
                        "T%d centre not found: its final XY points deviate"
                        " %.3f mm (more than %.1f mm). The calibration"
                        " stopped and nothing was saved"
                        % (tool_num, max_dev, MAX_DEVIATION_MM))
                self._info("    XY deviation: %.3fmm (OK)" % (max_dev,))
            self._info("    Center: X=%.4f Y=%.4f Z=%.4f" % current_center)
        self._info("  Tool %d COMPLETE: X=%.4f Y=%.4f Z=%.4f"
                   % ((tool_num,) + current_center))
        self._info("  Moving to safe height...")
        toolhead.manual_move([None, None, safe_z], 10.0)
        toolhead.wait_moves()
        return current_center
    def _offsets(self, centers):
        """Released l.1611-1636: offset = T0 - tool; bounds warn only."""
        self._info("=== CALCULATING OFFSETS ===")
        if 0 not in centers:
            raise self.printer.command_error(
                "Tool 0 must be calibrated as reference")
        ref = centers[0]
        offsets = {}
        for t, c in sorted(centers.items()):
            offsets[t] = (ref[0] - c[0], ref[1] - c[1], ref[2] - c[2])
            self._info("Tool %d offset: X=%.4f Y=%.4f Z=%.4f"
                       % ((t,) + offsets[t]))
        for t, (ox, oy, oz) in sorted(offsets.items()):
            if t == 0:
                continue
            if ox < -1.0 or ox > 1.0:
                self._info("WARNING: T%d X offset %.4f out of bounds (+-1mm)"
                           % (t, ox))
            if oy < -1.0 or oy > 1.0:
                self._info("WARNING: T%d Y offset %.4f out of bounds (+-1mm)"
                           % (t, oy))
            if oz < -2.0 or oz > 1.45:
                self._info("WARNING: T%d Z offset %.4f out of bounds"
                           " (-2/+1.45mm)" % (t, oz))
        return offsets
    # ---- the touches -------------------------------------------------------
    def _tool_probe(self, tool):
        router = self.printer.lookup_object('xl_load_cell_probe_router')
        tp = router.tools.get('T%d' % (tool,))
        if tp is None:
            raise self.printer.command_error(
                "T%d has no [xl_load_cell_probe T%d]" % (tool, tool))
        sel = self.printer.lookup_object('xl_load_cell_select')
        if sel.selected != tp.name:
            raise self.printer.command_error(
                "T%d picked but loadcell %s is selected" % (tool, sel.selected))
        if not tp.load_cell.is_calibrated():
            raise self.printer.command_error(
                "T%d loadcell has no reference yet - run Macros ->"
                " CALIBRATION_WIZARDS -> Loadcell reference first" % (tool,))
        return tp
    def _tare(self, tp, grams):
        # Klipper's own tare before a load cell probing move, with the
        # released threshold (trigger "abs_ge" at this force)
        g = self.gcode.create_gcode_command(
            "CALIBRATE_TOOL_OFFSETS", "CALIBRATE_TOOL_OFFSETS",
            {"TRIGGER_FORCE": "%.1f" % (grams,)})
        tp.probing_move._pause_and_tare(g)
    def _load_now(self, tp):
        """Mean force (g) over the next 0.1 s - the released pre-check read
        the Dwarf's current load after taring (l.941-948)."""
        sps = tp.load_cell.get_sensor().get_samples_per_second()
        collector = tp.load_cell.get_collector()
        collector.start_collecting()
        samples, errors = collector.collect_min(max(2, int(0.1 * sps)))
        grams = [s[1] for s in samples if s[1] is not None]
        if not grams:
            raise self.printer.command_error(
                "%s loadcell: no reading (errors %s)" % (tp.name, errors))
        return sum(grams) / len(grams)
    def _run_probe(self, tp, travel, p):
        """Released run_probe (l.462-632): one Z touch from here, down at
        most `travel` (never below position_min), back up 0.2 mm."""
        toolhead = self._toolhead()
        phoming = self.printer.lookup_object('homing')
        orig_max_accel = toolhead.max_accel
        self._set_accel(min(PROBE_ACCEL, orig_max_accel))
        try:
            toolhead.wait_moves()
            curpos = toolhead.get_position()
            self._pause(0.2)                  # VIBRATION_SETTLE_TIME
            self._tare(tp, Z_THRESHOLD)
            target_z = max(curpos[2] - travel, self.z_min)
            pos = list(curpos)
            pos[2] = target_z
            logging.info("xl_tool_offsets: Z probe from %.3f to %.3f (min %.2f)",
                         curpos[2], target_z, self.z_min)
            try:
                epos = phoming.probing_move(tp.mcu_trigger_analog, pos,
                                            p['probe_speed'])
            except self.printer.command_error as e:
                if "prior to movement" in str(e):
                    raise self.printer.command_error(
                        "Loadcell triggered before probe - check tare")
                raise
        finally:
            self._set_accel(orig_max_accel)
        if SAMPLE_RETRACT_DIST > 0:
            toolhead.manual_move(
                [None, None, min(epos[2] + SAMPLE_RETRACT_DIST, curpos[2])],
                LIFT_SPEED)
            toolhead.wait_moves()
        return epos[:3]
    def _xy_probing_move(self, tp, pos, speed):
        """Klipper probing move with the X/Y motors stopped by this tool's
        loadcell trigger.

        The released XY endstop was a second endstop object with the X/Y
        steppers registered (puppy_bootloader.py loadcell_endstop_xy).  A
        load cell has one MCU_trigger_analog, whose TriggerDispatch holds the
        Z motors (load_cell_probe.py LookupZSteppers); a second dispatch
        cannot share its command queue (the home command must follow its
        trsync_start), and keeping X/Y on it for good would fail every Z
        probe ("Probe triggered prior to movement": homing.py
        check_no_movement sees a motor that did not move).  Klipper has no
        call to take a stepper off a dispatch, so for this one move its
        trsyncs carry the X/Y motors instead of Z, and Z is put back after.
        """
        mta = tp.mcu_trigger_analog
        dispatch = mta.get_dispatch()
        trsyncs = getattr(dispatch, '_trsyncs', None)
        if not trsyncs or not all(hasattr(t, '_steppers') for t in trsyncs):
            raise self.printer.command_error(
                "XY touch: Klipper's TriggerDispatch is not what"
                " xl_tool_offsets.py was written for (klippy/mcu.py)")
        by_mcu = {}
        for s in self.xy_steppers:
            by_mcu.setdefault(s.get_mcu(), []).append(s)
        mcus = [t.get_mcu() for t in trsyncs]
        if not self.xy_steppers or any(m not in mcus for m in by_mcu):
            raise self.printer.command_error(
                "XY touch: the X/Y motors are not on an MCU of %s's trigger"
                % (tp.name,))
        # stock's XY band-pass for this move only: the probe sends the
        # filter in use to the tool board at every move start
        # (trigger_analog home_start -> _reset_filter), so the next Z probe
        # gets its own (none) back.  The raw safety range is not filtered.
        sos = mta.get_sos_filter()
        if sos is None or not hasattr(sos, '_design'):
            raise self.printer.command_error(
                "XY touch: Klipper's sos filter is not what"
                " xl_tool_offsets.py was written for (trigger_analog.py)")
        saved_design = sos._design
        saved = [(t, t._steppers) for t in trsyncs]
        phoming = self.printer.lookup_object('homing')
        try:
            sos.set_filter_design(_PrusaXYFilter())
            for t, _ in saved:
                t._steppers = list(by_mcu.get(t.get_mcu(), []))
            return phoming.probing_move(mta, pos, speed)
        finally:
            for t, steppers in saved:
                t._steppers = steppers
            sos.set_filter_design(saved_design)
    def _arc_move_to(self, toolhead, cx, cy, radius, target_angle,
                     speed=25.0, max_step_deg=10.0):
        """Released l.798-830: round the pin at constant radius and Z."""
        cur = toolhead.get_position()
        dx, dy = cur[0] - cx, cur[1] - cy
        cur_r = math.hypot(dx, dy)
        cur_a = math.atan2(dy, dx) if cur_r > 1e-6 else target_angle
        # radially out to the safety circle along the current bearing
        if abs(cur_r - radius) > 1e-3:
            toolhead.manual_move([cx + radius * math.cos(cur_a),
                                  cy + radius * math.sin(cur_a), None], speed)
        # sweep to the target angle the short way round, in small steps
        delta = (target_angle - cur_a + math.pi) % (2 * math.pi) - math.pi
        steps = max(1, int(math.ceil(abs(math.degrees(delta)) / max_step_deg)))
        for i in range(1, steps + 1):
            a = cur_a + delta * i / steps
            toolhead.manual_move([cx + radius * math.cos(a),
                                  cy + radius * math.sin(a), None], speed)
        toolhead.wait_moves()
    def _probe_xy_verified(self, tp, center_x, center_y, probe_z, angle_rad,
                           start_radius, probe_distance, speed, toolhead,
                           pin_radius):
        """Released l.832-876 (2 samples within 0.03 mm, up to 5 tries) with
        stock probe_xy_verify's rejections (G425.cpp l.357-412): a sample that
        triggered too early is no hit and is probed again; agreed samples
        further than PROBE_FAIL_THRESHOLD_MM from the expected point, or no
        agreement in NUM_PROBE_TRIES, stop the calibration."""
        PROBE_ALLOWED_ERROR = 0.03
        NUM_PROBE_SAMPLES = 2
        NUM_PROBE_TRIES = 5
        deg = math.degrees(angle_rad) % 360.
        for attempt in range(NUM_PROBE_TRIES):
            samples = []
            for _ in range(NUM_PROBE_SAMPLES):
                hit = self._probe_xy_at_angle(tp, center_x, center_y, probe_z,
                                              angle_rad, start_radius,
                                              probe_distance, speed)
                samples.append(hit)
                # back to the start point along the same bearing, Z untouched
                start_x = center_x + start_radius * math.cos(angle_rad)
                start_y = center_y + start_radius * math.sin(angle_rad)
                toolhead.manual_move([start_x, start_y, None], 25.0)
                toolhead.wait_moves()
            if any(s is None for s in samples):
                logging.warning("xl_tool_offsets: XY probe at %.0f deg"
                                " triggered too early, probing again", deg)
                continue
            avg_x = sum(s[0] for s in samples) / len(samples)
            avg_y = sum(s[1] for s in samples) / len(samples)
            max_dist = max(math.sqrt((s[0] - avg_x)**2 + (s[1] - avg_y)**2)
                           for s in samples)
            if max_dist <= PROBE_ALLOWED_ERROR:
                logging.info("xl_tool_offsets: XY samples agree within %.4fmm",
                             max_dist)
                # stock synthetic_probe: where this hit should be
                exp_x = center_x + (pin_radius + PROBE_Z_BORE_MM) * math.cos(
                    angle_rad)
                exp_y = center_y + (pin_radius + PROBE_Z_BORE_MM) * math.sin(
                    angle_rad)
                exp_dist = math.sqrt((avg_x - exp_x)**2 + (avg_y - exp_y)**2)
                if exp_dist > PROBE_FAIL_THRESHOLD_MM:
                    raise self.printer.command_error(
                        "XY probe at %.0f deg touched %.2f mm from where the"
                        " pin should be (more than %.0f mm) - check the pin"
                        " and the nozzle. The calibration stopped and nothing"
                        " was saved" % (deg, exp_dist, PROBE_FAIL_THRESHOLD_MM))
                return (avg_x, avg_y)
            logging.warning("xl_tool_offsets: XY sample deviation %.4fmm >"
                            " %smm, retrying...", max_dist, PROBE_ALLOWED_ERROR)
        raise self.printer.command_error(
            "XY probe at %.0f deg did not give two agreeing touches in %d tries"
            " (probe unstable). The calibration stopped and nothing was saved"
            % (deg, NUM_PROBE_TRIES))
    def _probe_xy_at_angle(self, tp, center_x, center_y, probe_z, angle_rad,
                           start_radius, probe_distance, speed):
        """Released l.878-1007: arc to the start point, settle, tare, check
        the load is steady, probe toward the centre, retract 5 mm."""
        toolhead = self._toolhead()
        orig_max_accel = toolhead.max_accel
        self._set_accel(min(PROBE_ACCEL, orig_max_accel))
        try:
            start_x = center_x + start_radius * math.cos(angle_rad)
            start_y = center_y + start_radius * math.sin(angle_rad)
            self._arc_move_to(toolhead, center_x, center_y, start_radius,
                              angle_rad)
            # assert the probing height (zero-length once the sweep is going)
            toolhead.manual_move([None, None, probe_z], 10.0)
            toolhead.wait_moves()
            # settle 500 ms (Prusa RESONANCE_DAMPER_WAIT_MS), tare, verify the
            # load is steady near zero (< 50 % of the threshold, re-tare up
            # to 5 times)
            self._pause(0.5)
            self._tare(tp, XY_THRESHOLD)
            max_retries = 5
            for attempt in range(max_retries):
                current_grams = abs(self._load_now(tp))
                logging.info("xl_tool_offsets: XY pre-check %.1fg attempt %d",
                             current_grams, attempt + 1)
                if current_grams < XY_THRESHOLD * 0.5:
                    break
                logging.warning("xl_tool_offsets: load high (%.1fg),"
                                " re-taring...", current_grams)
                self._pause(0.3)
                self._tare(tp, XY_THRESHOLD)
            else:
                raise self.printer.command_error(
                    "XY probe loadcell not stable after %d retries"
                    % (max_retries,))
            # toward the centre, past where the pin should be
            target_x = center_x - probe_distance * math.cos(angle_rad)
            target_y = center_y - probe_distance * math.sin(angle_rad)
            curpos = toolhead.get_position()
            pos = [target_x, target_y, curpos[2], curpos[3]]
            epos = self._xy_probing_move(tp, pos, speed)
            # Prusa MIN_TRAVELED_DISTANCE_MM check (0.1mm)
            travel_dist = math.sqrt((epos[0] - start_x)**2
                                    + (epos[1] - start_y)**2)
            early = travel_dist < MIN_TRAVELED_DISTANCE_MM
            if early:
                logging.warning("xl_tool_offsets: XY travel too short (%.3fmm)"
                                " - triggered too early, discarded",
                                travel_dist)
            logging.info("xl_tool_offsets: XY contact (%.3f,%.3f), traveled"
                         " %.2fmm", epos[0], epos[1], travel_dist)
            # retract away from the pin
            retract_dist = 5.0
            toolhead.manual_move([epos[0] + retract_dist * math.cos(angle_rad),
                                  epos[1] + retract_dist * math.sin(angle_rad),
                                  None], 25.0)
            toolhead.wait_moves()
        finally:
            self._set_accel(orig_max_accel)
        if early:
            return None
        return (epos[0], epos[1])
    def _check_xy_deviation(self, center, points, max_dev=0.2):
        """Released l.1009-1022 (Prusa MAX_DEVIATION_MM 0.2)."""
        cx, cy = center
        radii = [math.sqrt((q[0] - cx)**2 + (q[1] - cy)**2) for q in points]
        avg_radius = sum(radii) / len(radii)
        max_deviation = max(abs(r - avg_radius) for r in radii)
        if max_deviation > max_dev:
            logging.warning("xl_tool_offsets: XY deviation %.3fmm exceeds"
                            " %smm", max_deviation, max_dev)
            return False, max_deviation
        return True, max_deviation
    def _fit_circle_center(self, points):
        """Released l.1024-1061: iterative least squares, damped x0.5."""
        if len(points) < 3:
            cx = sum(q[0] for q in points) / len(points)
            cy = sum(q[1] for q in points) / len(points)
            return (cx, cy)
        cx = sum(q[0] for q in points) / len(points)
        cy = sum(q[1] for q in points) / len(points)
        for _ in range(10):
            radii = [math.sqrt((q[0] - cx)**2 + (q[1] - cy)**2) for q in points]
            avg_radius = sum(radii) / len(radii)
            dx, dy, weight_sum = 0.0, 0.0, 0.0
            for i, q in enumerate(points):
                dist = radii[i]
                if dist > 0.001:
                    residual = dist - avg_radius
                    weight = residual * residual
                    ux, uy = (q[0] - cx) / dist, (q[1] - cy) / dist
                    dx += weight * residual * ux
                    dy += weight * residual * uy
                    weight_sum += weight
            if weight_sum > 0.001:
                cx += dx / weight_sum * 0.5
                cy += dy / weight_sum * 0.5
        return (cx, cy)


def load_config(config):
    return XLToolOffsets(config)
