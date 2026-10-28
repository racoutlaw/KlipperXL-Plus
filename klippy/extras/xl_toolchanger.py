# KlipperXL-Plus - Prusa XL tool pick / park
#
# Written fresh.  The motion is the released KlipperXL sequence, which used
# Prusa's toolchanger_utils.h constants unchanged (reference
# puppy_bootloader.py l.1826-1856, pick l.7640-7790, park l.8060-8200);
# the released printer never saved calibrated docks, so Prusa's default dock
# positions are the proven ones.  Picked/parked come from each Dwarf's hall
# sensors ([xl_dock_sensors], Prusa Cheese.hpp thresholds).  Picking selects
# that tool's loadcell ([xl_load_cell]); parking deselects it.
#
#   [xl_toolchanger]
#   offset_t1: <x>, <y>, <z>    # vs T0 (CALIBRATE_TOOLS)
#   XL_PICK TOOL=<0-4>    XL_PARK    SET_Z_DATUM_TOOL TOOL=<n> SINGLE=<0|1>
#   NOZZLE_LEDS [STATE=ON|OFF|ALWAYS] [TOOL=<n>]   nozzle lights (Tn_light)
#   XL_ALIGN_LOCKS HOME_X= HOME_Y= [X= Y=]   stock G28 lock alignment
#                                  ([homing_override] runs it)
#
# Tool offsets as released KlipperXL applied them (reference
# puppy_bootloader.py l.7838-7871, 2389-2463): on pick
#   SET_GCODE_OFFSET X=-ox Y=-oy Z=-(oz - datum_z) MOVE=0
# (Prusa stores ref - tool; Klipper's offset shifts the other way), where
# datum_z is the Z offset of the tool that homed Z (a plain G28: T0 -> 0;
# a print homes with its first tool, as stock, so that tool's own Z cancels
# and the others ride on their offset from it).  Cleared on park,
# re-applied when SET_Z_DATUM_TOOL records a new datum after G28 Z.
# Plus the owner's first-layer fine-tune (released tool_offsets.py, autosave
# [tool_offsets]): z_adjust_tN for multi-tool prints, z_adjust_single_tN for
# this tool's single-tool prints (defaults to the multi
# value); both clamped to -0.3 .. +0.5 mm.  Owner 10/24: also per filament
# type - z_adjust[_single]_tN_<type> while the tool is loaded with that type
# (the load pop-up's choice); the babystep save goes there, FILAMENT= picks
# another slot (NONE = the tool's own).
#   SET_TOOL_Z_OFFSET TOOL=n Z= [SINGLE=] [FILAMENT=]
#   SAVE_TOOL_Z_OFFSET [TOOL=n] [Z=] [SINGLE=] [FILAMENT=]   GET_TOOL_Z_OFFSETS
#
# Copyright (C) 2026  Richard Crook
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging, math, re
from . import homing

# Prusa toolchanger_utils.h (via released KlipperXL)
DOCK_FIRST_X, DOCK_OFFSET_X, DOCK_Y = 25.0, 82.0, 455.0
SAFE_Y_WITH_TOOL, SAFE_Y_WITHOUT_TOOL = 360.0, 425.0
PICK_Y_OFFSET = -5.0
PICK_X_OFFSETS = (-11.8, -12.8, -9.9)     # lock engage start / engaged / final
PARK_X_OFFSET_1, PARK_X_OFFSET_2, PARK_X_OFFSET_3 = -10.0, -9.0, 0.5
DOCK_WIGGLE = 0.5
# Dock calibration - released KlipperXL puppy_bootloader.py @1e677ba
# l.6374-7013 (DOCK_CALIBRATE, constants l.6383-6386); stock Prusa calibrates
# each dock the same way (selftest_dock.cpp)
DOCK_CAL_UNLOCK_X = -9.9       # X unlock distance (Prusa X_UNLOCK_DISTANCE_MM)
DOCK_CAL_RETREAT_Y = 95.0      # Y retreat distance from dock to safe area
DOCK_INVALID_OFFSET = 6.0      # Max deviation from default (mm, Euclidean)
DOCK_CAL_VERIFY_CYCLES = 3     # Pick/park verification cycles
DOCK_CAL_SPEED = 30.           # mm/s, unlock + retreat (released F1800)
DOCK_Y_MAX = 460.0             # wiggle overflow protection (Y max 461)
SLOW_SPEED, TRAVEL_SPEED = 50., 200.      # mm/s
PARKING_CURRENT, NORMAL_CURRENT = 0.950, 0.650   # A
SAFE_Z = 20.0
# s, released pause before reading; pick / park: the longest wait for a
# right reading (stock returns the instant it reads right)
SENSOR_SETTLE = 0.3
NUM_TOOLS = 5
Z_ADJUST_MIN, Z_ADJUST_MAX = -0.3, 0.5   # released tool_offsets.py z_min/z_max
# per filament type (owner 10/24): z_adjust_tN_abs / z_adjust_single_tN_abs
Z_ADJUST_TYPE_RE = re.compile(r'^z_adjust_(single_)?t(\d+)_([a-z0-9]+)$')
# fresh-install seed (kxlplus_config.py PA_SEED, released [extruder] value)
PA_STARTING_VALUE = 0.025
PA_MIN, PA_MAX = 0., 1.0                  # released tool_pa.py k_min/k_max
# Nozzle light (picked, docked) brightness per NOZZLE_LEDS STATE - released
# puppy_bootloader.py cmd_NOZZLE_LEDS (255 = full)
LIGHT_STATES = {'ON': (1., 0.), 'OFF': (0., 0.), 'ALWAYS': (1., 1.)}
# Dwarf status LED ([xl_status_led Tn]) = stock Dwarf firmware's own states
# (src/puppy/dwarf/module_marlin.cpp l.90-96: selected 0x0f,0x0f,0x0f; not
# parked 0x0f,0x0c,0; parked 0,0x0f,0 - 0x0f = 6 %), shown SOLID: stock
# blinks them on the Dwarf itself, here every flash would be a message on the
# shared RS485 bus.  Only a change is sent; at startup one tool every
# STATUS_STAGGER s (the 0f95380 lesson: no burst to all Dwarfs at once).
STATUS_COLORS = {'picked': (0.059, 0.059, 0.059),
                 'off_dock': (0.059, 0.047, 0.),
                 'docked': (0., 0.059, 0.)}
STATUS_STAGGER = 0.25
# Stock G28 toolchanger lock alignment - Prusa-Firmware-Buddy 6.4.0 G28.cpp
# l.718-731, toolchanger.cpp align_locks() l.715-757, motion.cpp homeaxis()
# l.1258-1395 / homeaxis_single_run() l.1396-1520, Configuration_XL(_adv).h,
# marlin_stubs/XL/configuration.hpp
X_MAX_POS = 361.0           # X_BED_SIZE 360 - X_MIN_OFFSET -1 (stepper_x max)
X_MIN_POS = -8.0            # -7 - X_MAX_OFFSET (stepper_x min)
HOMING_FEEDRATE_XY = 52.0   # 52 * 60 mm/min
X_HOME_BUMP_MM = 20.0       # back off between the two hits (divisor 1)
MOVE_BACK_BEFORE_HOMING_DISTANCE = 3.0
HOMING_MAX_ATTEMPTS = 10
HOME_INVERT_DIFF = 1.0      # axis_home_invert_min/max_diff: -1 .. +1 mm
HOMING_BACKOFF_POST_MM = 2.0
TRAVEL_MOVE_MM_S = 400.0    # toolchanger_utils.h (SLOW_MOVE_MM_S 50 = SLOW_SPEED)
# After the right edge bump the way back to the left frame is the whole X
# width; less than this and the two hits were not the right edge
ALIGN_MIN_WIDTH = 0.9 * (X_MAX_POS - X_MIN_POS)


class XLToolchanger:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        self.active_tool = -1
        self.offsets = {}
        # which tools have a measured offset (T0 is the datum, always 0,0,0);
        # a missing offset_tN is 0,0,0 but NOT calibrated - an uncalibrated
        # tool would print misaligned, so it is reported, not hidden
        self.offset_calibrated = {}
        for t in range(NUM_TOOLS):
            v = config.getfloatlist('offset_t%d' % (t,), None, count=3)
            self.offsets[t] = tuple(v) if v is not None else (0., 0., 0.)
            self.offset_calibrated[t] = (t == 0) or v is not None
        self.z_adjust = {}
        self.z_adjust_single = {}
        for t in range(NUM_TOOLS):
            multi = config.getfloat('z_adjust_t%d' % (t,), 0.,
                                    minval=Z_ADJUST_MIN, maxval=Z_ADJUST_MAX)
            self.z_adjust[t] = multi
            self.z_adjust_single[t] = config.getfloat(
                'z_adjust_single_t%d' % (t,), multi,
                minval=Z_ADJUST_MIN, maxval=Z_ADJUST_MAX)
        # Owner 10/24: a fine-tune per filament type too - used while the tool
        # is loaded with that type (xl_filament's filament_tN, picked in the
        # load pop-up); a type without one uses the tool's own value above.
        # Every such key is read here, or Klipper refuses it as unknown.
        self._read_z_adjust_types(config)
        # optional filament label per tool's pressure advance (released
        # tool_pa.py t<N>_label); [extruderN] cannot hold it
        self.pa_label = {t: config.get('pa_label_t%d' % (t,), '')
                         for t in range(NUM_TOOLS)}
        # Dock positions: the nominal one (Prusa toolchanger_utils.h), or the
        # one DOCK_CALIBRATE measured, saved as dock_pos_tN = x, y - released
        # KlipperXL names and _load_dock_positions l.2533-2571.  Pick and park
        # run relative to it, as stock (toolchanger_xl.cpp l.675-873).
        self.dock_positions = {}
        self.dock_calibrated = {}
        for t in range(NUM_TOOLS):
            self.dock_positions[t] = (DOCK_FIRST_X + t * DOCK_OFFSET_X, DOCK_Y)
            self.dock_calibrated[t] = False
            value = config.get('dock_pos_t%d' % (t,), None)
            if value is None:
                continue
            try:
                x, y = [float(v.strip()) for v in value.split(',')]
            except ValueError:
                logging.warning("xl_toolchanger: dock_pos_t%d '%s' is not"
                                " 'x, y' - using the default", t, value)
                continue
            if y > DOCK_Y_MAX:
                logging.warning("xl_toolchanger: T%d dock_y=%.1f exceeds %.1f"
                                " - clamping", t, y, DOCK_Y_MAX)
                y = DOCK_Y_MAX
            self.dock_positions[t] = (x, y)
            self.dock_calibrated[t] = True
            logging.info("xl_toolchanger: T%d dock X=%.4f Y=%.4f (calibrated)",
                         t, x, y)
        self.dock_cal_active = False
        self.dock_cal_lines = []         # this step's messages (wizard)
        self.dock_cal_result = None      # running / complete / failed / aborted
        self._cal_state = None
        # Print fan (released KlipperXL 2026-09-02, Prusa toolchanger.cpp:345-
        # 348 + hwio_XLBuddy.cpp:398): bare M106 is THE print fan - its speed
        # is kept here and lives on the picked tool's [fan_generic
        # Tn_print_fan]; parking zeroes the outgoing tool's fan, picking puts
        # the print fan speed on the incoming one.
        self.print_fan_speed = 0.         # 0..1
        self._fan_failed = {}
        # tool -> speed held by the tool offsets calibration (hold_fan)
        self.fan_held = {}
        # Nozzle lights ([output_pin Tn_light], Dwarf PC11).  Stock Prusa:
        # the Dwarf firmware switched its own light between two brightnesses,
        # picked / docked (register 0xE004, LED_SELECTED_PWM 255 /
        # LED_NOT_SELECTED_PWM 0 - released KlipperXL puppy_bootloader.py
        # l.1817-1819).  The Dwarfs run Klipper here, so pick/park switch it.
        # NOZZLE_LEDS sets the policy (released command, same states); like
        # the release it is not saved across a restart.
        self.light_policy = LIGHT_STATES['ON']
        self.light_tool_policy = {}       # TOOL= overrides
        self.gcode.register_command("NOZZLE_LEDS", self.cmd_NOZZLE_LEDS,
                                    desc=self.cmd_NOZZLE_LEDS_help)
        self.z_home_tool = 0
        self.single_mode = False
        self.gcode.register_command("SET_Z_DATUM_TOOL",
                                    self.cmd_SET_Z_DATUM_TOOL,
                                    desc="Record which tool homed Z")
        # Per-tool Z fine-tune (first-layer squish), released KlipperXL
        # tool_offsets.py behaviour and names: two slots per tool - multi-tool
        # (Z homed by T0) and single-tool (the tool homed itself) - stored as
        # z_adjust_tN / z_adjust_single_tN and staged for SAVE_CONFIG
        # (docs/RELEASE_CONFIG.md).  Mainsail's Z-offset Save button sends
        # Z_OFFSET_APPLY_PROBE; Klipper's own version (registered by the
        # loadcell probe router, [xl_load_cell_probe] in xl_base.cfg before
        # this section) would save the PROBE's z_offset, so it is replaced.
        self.gcode.register_command('Z_OFFSET_APPLY_PROBE', None)
        self.gcode.register_command(
            'Z_OFFSET_APPLY_PROBE', self.cmd_Z_OFFSET_APPLY_PROBE,
            desc="Save the active tool's Z fine-tune (Mainsail Save button)")
        self.gcode.register_command(
            'SAVE_TOOL_Z_OFFSET', self.cmd_SAVE_TOOL_Z_OFFSET,
            desc="Set a tool's Z fine-tune and stage it for SAVE_CONFIG")
        self.gcode.register_command(
            'SET_TOOL_Z_OFFSET', self.cmd_SET_TOOL_Z_OFFSET,
            desc="Set a tool's Z fine-tune for now (not saved)")
        self.gcode.register_command(
            'GET_TOOL_Z_OFFSETS', self.cmd_GET_TOOL_Z_OFFSETS,
            desc="Show every tool's Z fine-tune, both slots")
        # Pressure advance per tool, released tool_pa.py names.  Each tool is
        # its own Klipper extruder here, so Klipper keeps PA per extruder and
        # nothing needs re-applying on a pick (the release, with one shared
        # extruder, had to wrap T0-T4 for that).  Saved as [extruderN]
        # pressure_advance via SAVE_CONFIG (docs/RELEASE_CONFIG.md).
        self.gcode.register_command(
            'SAVE_TOOL_PA', self.cmd_SAVE_TOOL_PA,
            desc="Set a tool's pressure advance and stage it for SAVE_CONFIG")
        self.gcode.register_command(
            'SET_TOOL_PA', self.cmd_SET_TOOL_PA,
            desc="Set a tool's pressure advance for now (not saved)")
        self.gcode.register_command(
            'GET_TOOL_PA', self.cmd_GET_TOOL_PA,
            desc="Show every tool's pressure advance")
        self.gcode.register_command(
            'APPLY_TOOL_PA', self.cmd_APPLY_TOOL_PA,
            desc="Compatibility: PA is per tool natively in KlipperXL-Plus")
        self.gcode.register_command("M106", self.cmd_M106,
                                    desc="Print fan (follows the tool)")
        self.gcode.register_command("M107", self.cmd_M107,
                                    desc="Print fan off")
        # T0-T4: what slicers send (released KlipperXL registered them in
        # puppy_bootloader).  Same as XL_PICK, plus one guard: during a print,
        # a tool with no calibrated offset is refused unless it is the tool
        # that homed Z for a single-tool job (no offset involved) - the slicer
        # only says how MANY tools a job uses (START_PRINT TOOLS=), not which,
        # so the tool change is the one place this can be checked exactly.
        for t in range(NUM_TOOLS):
            self.gcode.register_command(
                "T%d" % (t,), lambda gcmd, t=t: self.cmd_T(gcmd, t),
                desc="Pick tool T%d (parks the current one)" % (t,))
        self.gcode.register_command("XL_PICK", self.cmd_XL_PICK,
                                    desc="Pick a tool from its dock")
        self.gcode.register_command("XL_PARK", self.cmd_XL_PARK,
                                    desc="Park the tool on the carriage")
        self.gcode.register_command(
            "DOCK_POSITIONS", self.cmd_DOCK_POSITIONS,
            desc="Show the dock positions (default or calibrated)")
        self.gcode.register_command(
            "DOCK_CALIBRATE", self.cmd_DOCK_CALIBRATE,
            desc="Interactive dock position calibration [DOCK=n]")
        self.gcode.register_command(
            "XL_ALIGN_LOCKS", self.cmd_XL_ALIGN_LOCKS,
            desc="Stock G28 step: bump the right edge to align the tool"
            " locks ([homing_override] runs it)")
        self.tool_remap = {}             # spool join: G-code tool -> tool
        for cmd, desc in (
                ('SPOOL_JOIN', "After a runout: carry on with another tool"
                 " (TO=n [FROM=n] [TEMP=t])"),
                ('SPOOL_JOIN_STATUS', "Show active spool joins"),
                ('SPOOL_JOIN_RESET', "Clear all spool joins")):
            self.gcode.register_command(cmd, getattr(self, 'cmd_' + cmd),
                                        desc=desc)
        self.printer.register_event_handler("klippy:ready", self._ready)
        self._detected = False           # startup detection has run
        self._sync_pending = False       # a _XL_TOOL_SYNC is queued
        self._status_shown = {}          # tool -> status LED state sent
        self._status_queue = []          # startup: tools still to set
        self._status_timer = self.reactor.register_timer(self._status_startup)
        self.printer.register_event_handler("xl_dock_sensors:changed",
                                            self._dock_changed)
        self.gcode.register_command("_XL_TOOL_SYNC", self.cmd__XL_TOOL_SYNC,
                                    desc="Follow a tool picked/parked by hand")
    def _ready(self):
        self._detect_tries = 0
        self.reactor.register_timer(self._detect_picked, self.reactor.NOW)
    # A picked tool after a restart is detected, not assumed - once every
    # dock sensor has delivered its first reading (state None = no sample
    # yet; at klippy:ready none has arrived).
    def _detect_picked(self, eventtime):
        docks = {t: self._dock(t) for t in self._installed()}
        if any(d.picked.state is None or d.parked.state is None
               for d in docks.values()):
            self._detect_tries += 1
            if self._detect_tries < 50:
                return eventtime + 0.1
            logging.warning("xl_toolchanger: dock sensors silent - tool"
                            " state unknown")
            return self.reactor.NEVER
        for t, d in docks.items():
            if d.is_picked():
                self.active_tool = t
                self._select_loadcell(t)   # its loadcell streams again
                self._activate_extruder(t, from_timer=True)
                self._apply_offset(t, from_timer=True)   # restart reset it
                logging.info("xl_toolchanger: T%d found on the carriage", t)
        # lights first: SET_PIN is scheduled only ~0.1 s ahead and must not
        # queue behind the rate change below on the same Dwarf
        self._lights_apply_all(from_timer=True)   # lit on the carriage only
        # every tool starts at the parked rate (xl_dock_sensors); only the
        # tool on the carriage goes fast - one Dwarf, not all of them
        for t, d in docks.items():
            d.set_active(t == self.active_tool)
        self._detected = True
        # status LEDs one tool at a time, after the rate changes above
        self._status_queue = list(docks)
        self.reactor.update_timer(self._status_timer,
                                  eventtime + 2 * STATUS_STAGGER)
        return self.reactor.NEVER
    # Dwarf status LED
    def _status_state(self, tool):
        if tool == self.active_tool:
            return 'picked'
        if not self._dock(tool).parked.state:
            return 'off_dock'
        return 'docked'
    def _status_apply(self, tool):
        led = self.printer.lookup_object('xl_status_led T%d' % (tool,), None)
        if led is None:
            return
        state = self._status_state(tool)
        if self._status_shown.get(tool) == state:
            return
        # recorded only when the LED took it, so the next refresh retries
        if led.set_rgb(*STATUS_COLORS[state]):
            self._status_shown[tool] = state
    # at the end of a pick / park, so the LED changes when the tool does
    # (owner 10/11: the dock-sensor sync waits behind the running command -
    # through a whole autoload the picked T0 stayed green).  Same messages as
    # before, sent on time: the sync after it finds the LED shown and sends
    # nothing.  An LED problem never fails the tool change.
    def _status_now(self, tool):
        if self._status_queue:           # startup sets them one at a time
            return
        try:
            self._status_apply(tool)
        except Exception:
            logging.exception("xl_toolchanger: status LED")
    def _status_startup(self, eventtime):
        if not self._status_queue:
            return self.reactor.NEVER
        try:
            self._status_apply(self._status_queue.pop(0))
        except Exception:
            logging.exception("xl_toolchanger: status LED")
        return eventtime + STATUS_STAGGER
    # A tool picked or parked BY HAND is followed, as stock Prusa does
    # (toolchanger.cpp PrusaToolChanger::loop l.553-562: "When user parked or
    # picked manually", not printing or paused, nothing queued -> T<n> S1 =
    # the tool state only, no move).  Owner 10/07: a hand-picked T0 was picked
    # again by the loadcell wizard, and after T0 was parked by hand G28 Z
    # probed with no tool on the carriage until the emergency stop.
    def _dock_changed(self, name):
        if not self._detected or self._sync_pending:
            return
        self._sync_pending = True
        self.reactor.register_callback(self._sync_from_event)
    def _sync_from_event(self, eventtime):
        # through the g-code queue: never in the middle of another command
        try:
            self.gcode.run_script("_XL_TOOL_SYNC")
        except Exception:
            logging.exception("xl_toolchanger: tool sync failed")
            self._sync_pending = False
    def cmd__XL_TOOL_SYNC(self, gcmd):
        self._sync_pending = False       # a change from here on syncs again
        self._sync(gcmd)
        # status LEDs follow every dock change, printing or not (only a
        # changed one is sent; startup sets them itself)
        if not self._status_queue:
            for t in self._installed():
                self._status_apply(t)
    def _printing(self):
        stats = self.printer.lookup_object('print_stats', None)
        return stats is not None and stats.get_status(
            self.reactor.monotonic())['state'] in ('printing', 'paused')
    def _physical_tool(self):
        """The tool the dock sensors say is on the carriage, -1 for none,
        None if more than one reads picked (sensor fault: change nothing)."""
        picked = [t for t in self._installed() if self._dock(t).picked.state]
        if len(picked) > 1:
            return None
        return picked[0] if picked else -1
    def _sync(self, gcmd):
        if not self._detected or self._printing():
            return
        tool = self._physical_tool()
        if tool is None or tool == self.active_tool:
            return
        old = self.active_tool
        # as park / _detect_picked; the rate change goes last - SET_PIN and
        # the fan are scheduled ~0.1 s ahead and must not queue behind it
        if old >= 0:
            self.active_tool = -1
            self._select_loadcell(-1)
            self._clear_offset()
            self._fan_set(old, 0., fatal=False)
            self._light_apply(old)
            self._dock(old).set_active(False)
            gcmd.respond_info("T%d was taken off the carriage by hand - no"
                              " tool picked" % (old,))
        if tool >= 0:
            self.active_tool = tool
            self._select_loadcell(tool)
            self._activate_extruder(tool)
            self._apply_offset(tool)
            self._fan_apply(tool)
            self._light_apply(tool)
            self._dock(tool).set_active(True)
            gcmd.respond_info("T%d was put on the carriage by hand - it is"
                              " the picked tool now" % (tool,))
    # helpers
    # The tools this printer has = the tools with config (kxlplus/tools/tN.cfg,
    # written by the installer for the tools it found) - 1 to 5, no count to
    # set, as released KlipperXL used whichever Dwarfs booted (README "any
    # toolhead count, 1-5", DISABLE_ALL_HOTENDS "the tools that actually
    # booted rather than a hardcoded five").
    def _installed(self):
        return [t for t in range(NUM_TOOLS)
                if self.printer.lookup_object('xl_dock_sensors T%d' % (t,),
                                              None) is not None]
    def _dock(self, tool):
        return self.printer.lookup_object('xl_dock_sensors T%d' % (tool,))
    def _run(self, script):
        self.gcode.run_script_from_command(script)
    # Pick / park queue their moves back to back (stock move() =
    # line_to_current_position) and stop only where stock does
    # (planner.synchronize: before a sensor read, before a current change,
    # the pause in front of the dock).  Each stop costs Klipper's restart
    # (BUFFER_TIME_START 0.25 s) - one after every move was ~4 s a change.
    def _queue(self, x=None, y=None, speed=TRAVEL_SPEED):
        axes = "".join(" %s%.3f" % (a, v) for a, v in (("X", x), ("Y", y))
                       if v is not None)
        self._run("G90\nG1%s F%d" % (axes, speed * 60.))
    def _move(self, x=None, y=None, speed=TRAVEL_SPEED):
        self._queue(x, y, speed)
        self.printer.lookup_object('toolhead').wait_moves()
    def _xy_current(self, amps):
        self._run("SET_TMC_CURRENT STEPPER=stepper_x CURRENT=%.3f\n"
                  "SET_TMC_CURRENT STEPPER=stepper_y CURRENT=%.3f"
                  % (amps, amps))
    def _settle(self):
        self.reactor.pause(self.reactor.monotonic() + SENSOR_SETTLE)
    def _check_homed(self, gcmd):
        homed = self.printer.lookup_object('toolhead').get_status(
            self.reactor.monotonic())['homed_axes']
        if 'x' not in homed or 'y' not in homed:
            raise gcmd.error("Home X and Y first (G28 X Y)")
    def _safe_z(self):
        toolhead = self.printer.lookup_object('toolhead')
        # Z not homed (an X/Y-only home leaves it so, as stock): the bed height
        # is unknown - nothing to compare, and the homing already dropped it
        # 10 mm for clearance (homing_override); stock does not move an
        # un-homed Z with a normal move either (pause.cpp unhomed_z_lift)
        homed = toolhead.get_status(self.reactor.monotonic())['homed_axes']
        if 'z' not in homed:
            return
        if toolhead.get_position()[2] < SAFE_Z:
            self._run("G90\nG1 Z%.1f F600" % (SAFE_Z,))
            toolhead.wait_moves()
    def _select_loadcell(self, tool):
        sel = self.printer.lookup_object('xl_load_cell_select', None)
        if sel is not None:
            sel.select('NONE' if tool < 0 else 'T%d' % (tool,))
    # offsets
    def _apply_offset(self, tool, from_timer=False):
        ox, oy, oz = self.offsets[tool]
        datum_z = self.offsets[self.z_home_tool][2]
        ftype = self._loaded_type(tool)
        adj, by_type = self._z_adjust_for(tool, self._single_slot(tool), ftype)
        script = ("SET_GCODE_OFFSET X=%.4f Y=%.4f Z=%.4f MOVE=0"
                  % (-ox, -oy, -(oz - datum_z) + adj))
        if from_timer:
            self.gcode.run_script(script)     # not inside a g-code command
        else:
            self._run(script)
        if by_type:
            self.gcode.respond_info("T%d Z fine-tune for %s: %+.3f"
                                    % (tool, ftype, adj))
    def filament_changed(self, tool):
        # xl_filament: a load / unload changed this tool's filament type -
        # the picked tool takes that type's Z fine-tune now
        if tool == self.active_tool:
            self._apply_offset(tool)
    # each tool is a Klipper extruder: T0 'extruder', Tn 'extruderN'
    def _activate_extruder(self, tool, from_timer=False):
        name = 'extruder' if tool == 0 else 'extruder%d' % (tool,)
        if self.printer.lookup_object(name, None) is None:
            return
        script = "ACTIVATE_EXTRUDER EXTRUDER=%s" % (name,)
        if from_timer:
            self.gcode.run_script(script)
        else:
            self._run(script)
    # Z fine-tune
    def _single_slot(self, tool):
        # the single-tool slot applies exactly when _apply_offset uses it
        return self.single_mode and tool == self.z_home_tool
    def _read_z_adjust_types(self, config):
        self.z_adjust_type = {}          # (tool, 'ABS') -> multi
        self.z_adjust_single_type = {}   # (tool, 'ABS') -> single
        for opt in config.get_prefix_options('z_adjust_'):
            m = Z_ADJUST_TYPE_RE.match(opt)
            if m is None:
                continue
            value = config.getfloat(opt, minval=Z_ADJUST_MIN,
                                    maxval=Z_ADJUST_MAX)
            t = int(m.group(2))
            if t < NUM_TOOLS:
                table = (self.z_adjust_single_type if m.group(1)
                         else self.z_adjust_type)
                table[(t, m.group(3).upper())] = value
    def _loaded_type(self, tool):
        # the filament type the last load put in this tool (xl_filament's
        # filament_tN in save_variables), '' when not known
        sv = self.printer.lookup_object('save_variables', None)
        f = (sv.allVariables.get('filament_t%d' % (tool,))
             if sv is not None else None)
        if not isinstance(f, dict):
            return ''
        return str(f.get('type', '')).upper()
    def _z_adjust_for(self, tool, single, ftype):
        """(value, True when it is the filament type's own).  Single falls
        back to multi within a type, as z_adjust_single_tN does."""
        if ftype:
            if single and (tool, ftype) in self.z_adjust_single_type:
                return self.z_adjust_single_type[(tool, ftype)], True
            if (tool, ftype) in self.z_adjust_type:
                return self.z_adjust_type[(tool, ftype)], True
        return (self.z_adjust_single[tool] if single
                else self.z_adjust[tool]), False
    def _z_type_param(self, gcmd, tool):
        # FILAMENT=ABS sets that type's slot, FILAMENT=NONE the tool's own;
        # default: the type loaded in the tool
        ftype = gcmd.get('FILAMENT', None)
        if ftype is None:
            return self._loaded_type(tool)
        ftype = ftype.strip().upper()
        if ftype in ('', 'NONE'):
            return ''
        if not re.match(r'^[A-Z0-9]+$', ftype):
            raise gcmd.error("FILAMENT must be a type name (PLA, PETG, ABS"
                             " ...) or NONE")
        return ftype
    def _set_z_adjust(self, tool, value, single, ftype=''):
        value = max(Z_ADJUST_MIN, min(Z_ADJUST_MAX, value))
        if ftype:
            old = self._z_adjust_for(tool, single, ftype)[0]
            table = (self.z_adjust_single_type if single
                     else self.z_adjust_type)
            table[(tool, ftype)] = value
            key = 'z_adjust_%st%d_%s' % ('single_' if single else '', tool,
                                         ftype.lower())
        elif single:
            old, self.z_adjust_single[tool] = self.z_adjust_single[tool], value
            key = 'z_adjust_single_t%d' % (tool,)
        else:
            old, self.z_adjust[tool] = self.z_adjust[tool], value
            key = 'z_adjust_t%d' % (tool,)
        return key, old, value
    def _stage(self, key, value):
        self._stage_text(key, "%.4f" % (value,))
    def _stage_text(self, key, text):
        self.printer.lookup_object('configfile').set('xl_toolchanger', key,
                                                     text)
    def cmd_Z_OFFSET_APPLY_PROBE(self, gcmd):
        tool = self.active_tool
        if tool < 0:
            raise gcmd.error("No tool picked - nothing to save")
        # applied Z = -(oz - datum_z) + adj  =>  adj = total + (oz - datum_z)
        total = self.printer.lookup_object('gcode_move').homing_position[2]
        oz = self.offsets[tool][2]
        datum_z = self.offsets[self.z_home_tool][2]
        key, old, new = self._set_z_adjust(tool, total + (oz - datum_z),
                                           self._single_slot(tool),
                                           self._loaded_type(tool))
        self._stage(key, new)
        gcmd.respond_info("T%d %s: %.4f (was %.4f)\nThe SAVE_CONFIG command"
                          " will update the printer config file and restart"
                          " the printer." % (tool, key, new, old))
    # dock position: default or calibrated (DOCK_CALIBRATE)
    def _dock_x(self, tool):
        return self.dock_positions[tool][0]
    def _dock_y(self, tool):
        return self.dock_positions[tool][1]
    def _tool_param(self, gcmd):
        tool = gcmd.get_int('TOOL', -1, minval=-1, maxval=NUM_TOOLS - 1)
        if tool < 0:
            tool = self.active_tool
        if tool < 0:
            raise gcmd.error("No tool picked and no TOOL= given")
        return tool
    def cmd_SAVE_TOOL_Z_OFFSET(self, gcmd):
        tool = self._tool_param(gcmd)
        single = bool(gcmd.get_int('SINGLE', int(self._single_slot(tool))))
        z = gcmd.get_float('Z', None)
        if z is None:
            # without Z= the value is read from the picked tool's live offset
            if tool != self.active_tool:
                raise gcmd.error("T%d is not picked - give Z= to set its"
                                 " fine-tune" % (tool,))
            total = self.printer.lookup_object('gcode_move').homing_position[2]
            z = total + (self.offsets[tool][2]
                         - self.offsets[self.z_home_tool][2])
        key, old, new = self._set_z_adjust(tool, z, single,
                                           self._z_type_param(gcmd, tool))
        self._stage(key, new)
        gcmd.respond_info("T%d %s: %.4f (was %.4f)\nRun SAVE_CONFIG to"
                          " persist." % (tool, key, new, old))
    def cmd_SET_TOOL_Z_OFFSET(self, gcmd):
        tool = gcmd.get_int('TOOL', minval=0, maxval=NUM_TOOLS - 1)
        single = bool(gcmd.get_int('SINGLE', int(self._single_slot(tool))))
        key, old, new = self._set_z_adjust(tool, gcmd.get_float('Z'), single,
                                           self._z_type_param(gcmd, tool))
        gcmd.respond_info("T%d %s: %.4f (was %.4f) - not saved"
                          " (SAVE_TOOL_Z_OFFSET to persist)"
                          % (tool, key, new, old))
    def cmd_GET_TOOL_Z_OFFSETS(self, gcmd):
        lines = ["Per-tool Z fine-tune      multi    single",
                 "                       (2+ tools)  (1 tool)"]
        for t in self._installed():
            ftype = self._loaded_type(t)
            lines.append("  T%d:              %8.4f  %8.4f   loaded: %s"
                         % (t, self.z_adjust[t], self.z_adjust_single[t],
                            ftype or "not known"))
            for (tt, ft) in sorted(set(self.z_adjust_type)
                                   | set(self.z_adjust_single_type)):
                if tt != t:
                    continue
                lines.append("    %-6s           %8.4f  %8.4f%s"
                             % (ft, self._z_adjust_for(t, False, ft)[0],
                                self._z_adjust_for(t, True, ft)[0],
                                "   <- in use" if ft == ftype else ""))
        lines.append("Active mode: %s (Z datum T%d)"
                     % ("SINGLE-TOOL" if self.single_mode else "MULTI-TOOL",
                        self.z_home_tool))
        gcmd.respond_info("\n".join(lines))
    # Pressure advance
    def _ext_name(self, tool):
        return 'extruder' if tool == 0 else 'extruder%d' % (tool,)
    def _pa_args(self, gcmd):
        tool = gcmd.get_int('TOOL', minval=0, maxval=NUM_TOOLS - 1)
        k = gcmd.get_float('K')
        if k < PA_MIN or k > PA_MAX:
            raise gcmd.error("K=%.4f outside %.2f..%.2f - refusing"
                             % (k, PA_MIN, PA_MAX))
        ext = self.printer.lookup_object(self._ext_name(tool), None)
        if ext is None:
            raise gcmd.error("T%d has no extruder configured" % (tool,))
        return tool, k, ext
    def _set_pa(self, tool, k, ext):
        old = ext.get_status(self.reactor.monotonic())['pressure_advance']
        self._run("SET_PRESSURE_ADVANCE EXTRUDER=%s ADVANCE=%.4f"
                  % (self._ext_name(tool), k))
        return old
    def cmd_SAVE_TOOL_PA(self, gcmd):
        tool, k, ext = self._pa_args(gcmd)
        label = gcmd.get('FILAMENT', '')
        old = self._set_pa(tool, k, ext)
        configfile = self.printer.lookup_object('configfile')
        configfile.set(self._ext_name(tool), 'pressure_advance', "%.4f" % (k,))
        if label:
            self.pa_label[tool] = label
            configfile.set('xl_toolchanger', 'pa_label_t%d' % (tool,), label)
        gcmd.respond_info("T%d pressure advance: %.4f (was %.4f)%s\nRun"
                          " SAVE_CONFIG to persist." % (
                              tool, k, old, (" [%s]" % label) if label else ""))
    def cmd_SET_TOOL_PA(self, gcmd):
        tool, k, ext = self._pa_args(gcmd)
        old = self._set_pa(tool, k, ext)
        gcmd.respond_info("T%d pressure advance: %.4f (was %.4f) - not saved"
                          " (SAVE_TOOL_PA to persist)" % (tool, k, old))
    def cmd_GET_TOOL_PA(self, gcmd):
        now = self.reactor.monotonic()
        lines = ["Per-tool pressure advance:"]
        for t in range(NUM_TOOLS):
            ext = self.printer.lookup_object(self._ext_name(t), None)
            if ext is None:
                continue
            k = ext.get_status(now)['pressure_advance']
            lab = self.pa_label.get(t, '')
            if k == 0. and not lab:
                # released tool_pa: 0.0 is a legitimate K, so an untuned tool
                # shows no number at all
                lines.append("  T%d:   --      (never tuned)" % (t,))
                continue
            if k == PA_STARTING_VALUE and not lab:
                # the installer's seed (released starting value), not a tune
                lines.append("  T%d: %.4f  (starting value - not tuned yet)"
                             % (t, k))
                continue
            lines.append("  T%d: %.4f%s" % (t, k, ("  [%s]" % lab) if lab
                                              else ""))
        gcmd.respond_info("\n".join(lines))
    def cmd_APPLY_TOOL_PA(self, gcmd):
        gcmd.respond_info("KlipperXL-Plus: each tool is its own extruder and keeps"
                          " its own pressure advance - nothing to apply")
    def _clear_offset(self):
        self._run("SET_GCODE_OFFSET X=0 Y=0 Z=0 MOVE=0")
    def cmd_SET_Z_DATUM_TOOL(self, gcmd):
        tool = gcmd.get_int('TOOL', None, minval=0, maxval=NUM_TOOLS - 1)
        if tool is None:
            tool = self.active_tool if self.active_tool >= 0 else 0
        self.z_home_tool = tool
        self.single_mode = bool(gcmd.get_int('SINGLE', 0))
        logging.info("xl_toolchanger: Z datum T%d (%s)", tool,
                     "single" if self.single_mode else "multi")
        # the datum just moved: an offset applied at pick time is stale
        if self.active_tool >= 0:
            self._apply_offset(self.active_tool)
    # pick
    def pick(self, gcmd, tool):
        if tool not in self._installed():
            raise gcmd.error("T%d is not installed on this printer" % (tool,))
        self._check_homed(gcmd)
        self._sync(gcmd)                 # what is really on the carriage
        if self.active_tool == tool:
            gcmd.respond_info("T%d already picked" % (tool,))
            return
        # Stock: "Before we try to pick up new tool, check that its parked
        # properly" - a tool not in its dock is refused, nothing moves
        # (toolchanger.cpp l.353-357).  The release only warned and drove to
        # the empty dock.  Checked before the current tool is parked, and
        # again on a fresh reading below.
        dock = self._dock(tool)
        if not dock.is_parked():
            raise gcmd.error(self._not_docked_msg(tool))
        if self.active_tool >= 0:
            self.park(gcmd)
        dock_x = self._dock_x(tool)
        dock_y = self._dock_y(tool)
        self._safe_z()
        gcmd.respond_info("Picking T%d from dock X=%.1f Y=%.1f"
                          % (tool, dock_x, dock_y))
        # its sensors report fast before anything reads them
        dock.set_active(True)
        if not dock.wait_fresh():
            raise gcmd.error("T%d dock sensors are not reporting" % (tool,))
        if not dock.is_parked():
            dock.set_active(False)
            raise gcmd.error(self._not_docked_msg(tool))
        self._queue(x=dock_x, y=SAFE_Y_WITHOUT_TOOL)
        self._queue(y=dock_y + PICK_Y_OFFSET)
        self._move(y=dock_y, speed=SLOW_SPEED)      # stop: sensor read
        if not dock.wait_until(dock.is_picked, SENSOR_SETTLE):
            gcmd.respond_info("Not picked, wiggling...")
            self._queue(y=dock_y + DOCK_WIGGLE, speed=SLOW_SPEED)
            self._move(y=dock_y, speed=SLOW_SPEED)
            if not dock.wait_until(dock.is_picked, SENSOR_SETTLE):
                raise gcmd.error("T%d not detected as picked after wiggle!"
                                 % (tool,))
        self._xy_current(PARKING_CURRENT)
        for off in PICK_X_OFFSETS[:-1]:
            self._queue(x=dock_x + off, speed=SLOW_SPEED)
        self._move(x=dock_x + PICK_X_OFFSETS[-1], speed=SLOW_SPEED)  # stop
        self._xy_current(NORMAL_CURRENT)
        if not dock.wait_until(lambda: not dock.is_parked(), SENSOR_SETTLE):
            gcmd.respond_info("WARNING: T%d still reporting parked after lock!"
                              % (tool,))
        self._queue(y=SAFE_Y_WITH_TOOL)    # tool out - no stop (stock)
        self.active_tool = tool
        self._select_loadcell(tool)
        self._activate_extruder(tool)
        self._apply_offset(tool)
        self._fan_apply(tool)            # print fan follows the tool
        self._light_apply(tool)          # nozzle light: picked brightness
        self._status_now(tool)           # status LED: picked
        gcmd.respond_info("T%d picked" % (tool,))
        if not self.offset_calibrated[tool]:
            gcmd.respond_info("WARNING: T%d has no calibrated tool offset -"
                              " it would print misaligned with the other tools"
                              " (calibrate the tool offsets first)" % (tool,))
    def _not_docked_msg(self, tool):
        return ("T%d is not in its dock (its parked sensor reads off) - not"
                " picking it. Put it back in the dock, or put it on the"
                " carriage by hand." % (tool,))
    # park
    def park(self, gcmd):
        self._check_homed(gcmd)
        self._sync(gcmd)                 # what is really on the carriage
        tool = self.active_tool
        if tool < 0:
            gcmd.respond_info("No tool picked")
            return
        dock = self._dock(tool)
        dock_x = self._dock_x(tool)
        dock_y = self._dock_y(tool)
        self._select_loadcell(-1)
        self._clear_offset()
        self._safe_z()
        gcmd.respond_info("Parking T%d at dock X=%.1f Y=%.1f"
                          % (tool, dock_x, dock_y))
        self._queue(x=dock_x + PARK_X_OFFSET_1, y=SAFE_Y_WITH_TOOL)
        # stop in front of the dock: stock's pause "which allow the resonance
        # in the tool to be damped before insertion" (toolchanger.cpp l.655)
        self._move(y=dock_y)
        self._xy_current(PARKING_CURRENT)
        self._queue(x=dock_x + PARK_X_OFFSET_2, speed=SLOW_SPEED)
        self._move(x=dock_x + PARK_X_OFFSET_3, speed=SLOW_SPEED)   # stop
        self._xy_current(NORMAL_CURRENT)
        self._move(x=dock_x, y=dock_y, speed=SLOW_SPEED)   # stop: sensor read
        if not dock.wait_until(dock.is_parked, SENSOR_SETTLE):
            gcmd.respond_info("Not parked, wiggling...")
            self._queue(x=dock_x - DOCK_WIGGLE, speed=SLOW_SPEED)
            self._move(x=dock_x, speed=SLOW_SPEED)
            if not dock.wait_until(dock.is_parked, SENSOR_SETTLE):
                # still the active tool: give back what the park took away
                # above, or a later T<n> ("already picked") prints without
                # its offset and loadcell
                self._select_loadcell(tool)
                self._apply_offset(tool)
                raise gcmd.error("T%d not reporting parked after insert!"
                                 % (tool,))
        self.active_tool = -1
        self._fan_set(tool, 0., fatal=False)   # toolchanger.cpp:348
        self._light_apply(tool)          # nozzle light: docked brightness
        self._status_now(tool)           # status LED: docked
        # stop: the reading below must be taken with the carriage gone
        self._move(y=SAFE_Y_WITHOUT_TOOL)
        # Slow reports only once a reading taken AFTER the carriage left the
        # dock is in: until then 'picked' still shows the carriage.  Owner
        # 10/08: switched to slow at the dock, T4 read picked for up to 1 s
        # after its park, the next pick's _sync took that for a hand pick and
        # parked T4 again - the empty carriage into T4 in its dock.
        if not dock.wait_fresh():
            gcmd.respond_info("WARNING: T%d dock sensors did not report after"
                              " parking" % (tool,))
        dock.set_active(False)           # parked: slow reports
        gcmd.respond_info("T%d parked" % (tool,))
    # Toolchanger lock alignment, as stock Prusa G28 does it.  Owner 10/08: a
    # crash left the carriage lock part-closed and the next pick missed T0;
    # stock bumps the right edge before every X+Y home so the locking plates
    # line up.  Klipper's G28 homes X one way only, so this runs Klipper's
    # homing move (homing.py HomingMove) to the right on the same X stall
    # endstop.  [homing_override] calls it with the stall detection set up:
    #   XL_ALIGN_LOCKS HOME_X=0|1 HOME_Y=0|1 [X= Y=]
    # X / Y = the real position when that axis was homed before this G28 (the
    # override has faked 180/180 by then).
    def cmd_XL_ALIGN_LOCKS(self, gcmd):
        home_x = gcmd.get_int('HOME_X', 1)
        home_y = gcmd.get_int('HOME_Y', 1)
        known_x = gcmd.get_float('X', None)
        known_y = gcmd.get_float('Y', None)
        x_unknown = known_x is None
        # G28.cpp l.718-726: homing X and Y -> align the locking plates
        if home_x and home_y:
            why = self._tool_on_carriage()
            if why:
                gcmd.respond_info("Lock alignment skipped: %s" % (why,))
            else:
                self._align_locks(gcmd, known_x, known_y)
                x_unknown = True         # align_locks: X needs homing after
        # G28.cpp l.728-731: X unknown or near the right edge -> a bit left
        # ("Move a bit left to avoid unlocking the tool")
        if x_unknown or known_x > X_MAX_POS - MOVE_BACK_BEFORE_HOMING_DISTANCE:
            if x_unknown or home_x:
                self._homing_move_rel(0, -MOVE_BACK_BEFORE_HOMING_DISTANCE,
                                      HOMING_FEEDRATE_XY, must_trigger=False)
            else:
                # X stays homed in this G28: in its real position
                toolhead = self.printer.lookup_object('toolhead')
                toolhead.manual_move(
                    [toolhead.get_position()[0]
                     - MOVE_BACK_BEFORE_HOMING_DISTANCE, None, None],
                    HOMING_FEEDRATE_XY)
                toolhead.wait_moves()
    def _tool_on_carriage(self):
        """Why the locks must not be bumped, or None.  align_locks(): with a
        tool picked "It would catapult picked dwarf"."""
        if self.active_tool >= 0:
            return "T%d is on the carriage" % (self.active_tool,)
        for t in self._installed():
            state = self._dock(t).picked.state
            if state is None:
                return "T%d dock sensor has not reported yet" % (t,)
            if state:
                return "T%d reads picked" % (t,)
        return None
    def _align_locks(self, gcmd, known_x, known_y):
        """PrusaToolChanger::align_locks() (toolchanger.cpp l.715-757)."""
        toolhead = self.printer.lookup_object('toolhead')
        gcmd.respond_info("Aligning the tool locks (right edge bump)")
        if known_x is not None and known_y is not None:
            # can_move_safely(): X and Y were homed - real position back
            toolhead.wait_moves()
            pos = toolhead.get_position()
            pos[0], pos[1] = known_x, known_y
            toolhead.set_position(pos, homing_axes="xy")
            if known_y > SAFE_Y_WITHOUT_TOOL:
                toolhead.manual_move([None, SAFE_Y_WITHOUT_TOOL, None],
                                     SLOW_SPEED)
            # "Go to the front right corner quickly"
            toolhead.manual_move([X_MAX_POS, 0., None], TRAVEL_MOVE_MM_S)
        else:
            # "A bit back in case the carriage is near tool"
            self._homing_move_rel(1, SAFE_Y_WITHOUT_TOOL - DOCK_Y,
                                  HOMING_FEEDRATE_XY, must_trigger=False)
        toolhead.wait_moves()
        self._home_x_inverted(gcmd)          # "Bump right edge"
        # "Return to left edge before homing": stock sets X = X_MAX_POS and
        # travels to X 0 at 400 mm/s, trusting the bump.  Owner 10/24: the
        # two hits once tripped near the front-left corner (0.613 mm apart,
        # the carriage never went right) and that return drove into the left
        # frame.  Here the return watches the X stall endstop at homing
        # speed and measures the way to the left frame.
        width = self._homing_move_rel(0, -1.5 * (X_MAX_POS - X_MIN_POS),
                                      HOMING_FEEDRATE_XY, must_trigger=True)
        logging.info("xl_toolchanger: right edge to left frame %.1f mm",
                     width)
        if width < ALIGN_MIN_WIDTH:
            raise gcmd.error("Toolchanger lock alignment: the right edge hits"
                             " were only %.0f mm from the left frame - not"
                             " the right edge.  Check nothing blocks the"
                             " carriage, then home again." % (width,))
        # at the left frame (X_MIN_POS); on to X 0, where stock's return ends
        pos = toolhead.get_position()
        pos[0] = X_MIN_POS
        toolhead.set_position(pos, homing_axes="x")
        toolhead.manual_move([0., None, None], HOMING_FEEDRATE_XY)
        toolhead.wait_moves()
    def _home_x_inverted(self, gcmd):
        """homeaxis(X_AXIS, 0, true): 3 mm away, hit the right edge at
        52 mm/s, 20 mm back, hit it again (bump divisor 1 = same speed); the
        two hits must agree within 1 mm, else again (10 attempts); then 2 mm
        off the edge (HOMING_BACKOFF_POST_MM)."""
        length = X_MAX_POS - X_MIN_POS                  # max_length(X)
        for attempt in range(HOMING_MAX_ATTEMPTS):
            self._move_rel(0, -MOVE_BACK_BEFORE_HOMING_DISTANCE,
                           HOMING_FEEDRATE_XY)
            self._homing_move_rel(0, 1.5 * length, HOMING_FEEDRATE_XY,
                                  must_trigger=True)
            self._move_rel(0, -X_HOME_BUMP_MM, HOMING_FEEDRATE_XY)
            travel = self._homing_move_rel(0, 2 * X_HOME_BUMP_MM,
                                           HOMING_FEEDRATE_XY,
                                           must_trigger=True)
            diff = travel - X_HOME_BUMP_MM               # 2nd hit - 1st hit
            logging.info("xl_toolchanger: right edge hits %.3f mm apart"
                         " (attempt %d)", diff, attempt + 1)
            if -HOME_INVERT_DIFF <= diff <= HOME_INVERT_DIFF:
                break
            gcmd.respond_info("Right edge hits %.3f mm apart - again"
                              % (diff,))
        else:
            raise gcmd.error("Toolchanger lock alignment failed: the right"
                             " edge hits never agreed within %.0f mm"
                             % (HOME_INVERT_DIFF,))
        self._move_rel(0, -HOMING_BACKOFF_POST_MM, HOMING_FEEDRATE_XY)
    def _frame(self, axis, start):
        # this axis is re-homed after the alignment: put it at `start` so
        # the move's end is inside the axis range (check_move tests the end)
        toolhead = self.printer.lookup_object('toolhead')
        toolhead.wait_moves()
        pos = toolhead.get_position()
        pos[axis] = start
        toolhead.set_position(pos, homing_axes="xyz"[axis])
        return pos
    def _move_rel(self, axis, dist, speed):
        """do_homing_move_axis_rel(): a plain move of `dist` mm."""
        toolhead = self.printer.lookup_object('toolhead')
        lo, hi = toolhead.get_kinematics().rails[axis].get_range()
        pos = self._frame(axis, hi if dist < 0 else lo)
        coord = [None, None, None]
        coord[axis] = pos[axis] + dist
        toolhead.manual_move(coord, speed)
        toolhead.wait_moves()
    def _homing_move_rel(self, axis, dist, speed, must_trigger):
        """do_homing_move(): move `dist` mm along X (0) / Y (1), stopping
        on that axis' stall endstop.  Returns the distance moved before the
        trigger (the full length when it did not trigger)."""
        toolhead = self.printer.lookup_object('toolhead')
        rail = toolhead.get_kinematics().rails[axis]
        lo, hi = rail.get_range()
        start = hi - dist if dist > 0 else lo - dist
        pos = self._frame(axis, start)
        target = list(pos)
        target[axis] = start + dist
        hmove = homing.HomingMove(self.printer, rail.get_endstops())
        trig = hmove.homing_move(target, speed, probe_pos=True,
                                 check_triggered=must_trigger)
        if must_trigger and hmove.check_no_movement() is not None:
            raise self.printer.command_error(
                "%s stall endstop triggered before moving" % ("xy"[axis],))
        return abs(trig[axis] - start)
    # g-code
    # Print fan
    def _fan_set(self, tool, speed, fatal):
        """SET_FAN_SPEED on the tool's print fan.  A fan problem must never
        abort a print (released KlipperXL: stock Prusa throws the set_pwm
        result away, hwio_XLBuddy.cpp:398) - only an explicit M106 T/P,
        someone addressing a fan by hand, raises."""
        name = "T%d_print_fan" % (tool,)
        speed = self.fan_held.get(tool, speed)
        try:
            if self.printer.lookup_object("fan_generic " + name, None) is None:
                raise self.printer.command_error("no [fan_generic %s]" % name)
            self._run("SET_FAN_SPEED FAN=%s SPEED=%.4f" % (name, speed))
        except self.printer.command_error as e:
            if fatal:
                raise
            if not self._fan_failed.get(tool):
                self._fan_failed[tool] = True
                self.gcode.respond_info("!! T%d fan: %s - print continues"
                                        % (tool, e))
            return
        if self._fan_failed.get(tool):
            self._fan_failed[tool] = False
            self.gcode.respond_info("T%d fan recovered" % (tool,))
    def _fan_apply(self, tool):
        self._fan_set(tool, self.print_fan_speed, fatal=False)
    # Tool offsets calibration (xl_tool_offsets.py): a hot tool's print fan
    # held at 100 % while it cools to the calibration temperature, through
    # its pick and park - the released Dwarf fan latch (_fan_selftest_enter /
    # _set / _exit), Prusa FanCoolingManager.
    def hold_fan(self, tool, speed):
        """Hold a tool's print fan at speed (0..1); None lets go of it and
        sets what it would be (the print fan speed when picked, else off)."""
        if speed is None:
            if self.fan_held.pop(tool, None) is None:
                return
            speed = self.print_fan_speed if tool == self.active_tool else 0.
        else:
            self.fan_held[tool] = speed
        self._fan_set(tool, speed, fatal=False)
    def _fan_target(self, gcmd):
        # T takes precedence over P; absent both = the print fan
        t = gcmd.get_int('T', None)
        return t if t is not None else gcmd.get_int('P', None)
    def cmd_M106(self, gcmd):
        # get_float: PrusaSlicer emits S127.5 (released fix, 2026-09-04)
        speed = max(0., min(255., gcmd.get_float('S', 255.))) / 255.
        explicit = self._fan_target(gcmd)
        if explicit is not None:
            if explicit < 0 or explicit >= NUM_TOOLS:
                raise gcmd.error("No tool fan %d" % (explicit,))
            self._fan_set(explicit, speed, fatal=True)
            return
        self.print_fan_speed = speed
        if self.active_tool >= 0:        # nothing picked: stored for the pick
            self._fan_set(self.active_tool, speed, fatal=False)
    def cmd_M107(self, gcmd):
        explicit = self._fan_target(gcmd)
        if explicit is not None:
            if explicit < 0 or explicit >= NUM_TOOLS:
                raise gcmd.error("No tool fan %d" % (explicit,))
            self._fan_set(explicit, 0., fatal=True)
            return
        self.print_fan_speed = 0.
        if self.active_tool >= 0:
            self._fan_set(self.active_tool, 0., fatal=False)
    # Nozzle lights
    def _light_pin(self, tool):
        name = "T%d_light" % (tool,)
        if self.printer.lookup_object("output_pin " + name, None) is None:
            return None                  # tool not installed
        return name
    def _light_apply(self, tool, from_timer=False):
        name = self._light_pin(tool)
        if name is None:
            return
        picked, docked = self.light_tool_policy.get(tool, self.light_policy)
        script = "SET_PIN PIN=%s VALUE=%.3f" % (
            name, picked if tool == self.active_tool else docked)
        if from_timer:
            self.gcode.run_script(script)
        else:
            self._run(script)
    def _lights_apply_all(self, from_timer=False):
        for t in range(NUM_TOOLS):
            self._light_apply(t, from_timer)
    cmd_NOZZLE_LEDS_help = ("Nozzle lights: ON = lit when picked (default),"
                            " OFF, ALWAYS = lit in the dock too")
    def cmd_NOZZLE_LEDS(self, gcmd):
        state = gcmd.get('STATE', 'ON').upper()
        if state not in LIGHT_STATES:
            raise gcmd.error("NOZZLE_LEDS: STATE must be ON, OFF or ALWAYS"
                             " (got '%s')" % (state,))
        tool = gcmd.get_int('TOOL', None)
        if tool is None:
            # every installed tool; a TOOL= override no longer applies
            self.light_policy = LIGHT_STATES[state]
            self.light_tool_policy.clear()
            self._lights_apply_all()
            gcmd.respond_info("NOZZLE_LEDS: %s on every tool" % (state,))
            return
        if (tool < 0 or tool >= NUM_TOOLS
                or self._light_pin(tool) is None):
            raise gcmd.error("NOZZLE_LEDS: T%d not available" % (tool,))
        self.light_tool_policy[tool] = LIGHT_STATES[state]
        self._light_apply(tool)
        gcmd.respond_info("NOZZLE_LEDS: %s on T%d" % (state, tool))
    # released cmd_TOOL_PICK / cmd_TOOL_PARK l.7466 / l.7936
    def _no_dock_cal(self, gcmd):
        if self.dock_cal_active:
            raise gcmd.error("Dock calibration in progress! Complete or abort"
                             " calibration before changing tools.")
    def cmd_T(self, gcmd, tool):
        self._no_dock_cal(gcmd)
        joined = self.tool_remap.get(tool, tool)
        if joined != tool:
            gcmd.respond_info("Spool join: T%d -> T%d" % (tool, joined))
            tool = joined
        if tool not in self._installed():
            raise gcmd.error("T%d is not installed on this printer" % (tool,))
        stats = self.printer.lookup_object('print_stats', None)
        printing = stats is not None and stats.get_status(
            self.reactor.monotonic())['state'] in ('printing', 'paused')
        if (printing and not self.offset_calibrated[tool]
                and not (self.single_mode and tool == self.z_home_tool)):
            raise gcmd.error("T%d has no calibrated tool offset - refusing to"
                             " print with it (it would be misaligned). Calibrate"
                             " the tool offsets first." % (tool,))
        self.pick(gcmd, tool)
        # a pulled-back filament is given back on this tool's first forward
        # extrusion (xl_filament wraps G1, as stock planner.cpp l.2416)
    # Spool join (released puppy_bootloader cmd_SPOOL_JOIN l.7666-7729, macro
    # JOIN_SPOOL): after a runout, carry on with another tool that has the
    # same filament - T<from> in the rest of the G-code picks T<to>.  Only the
    # T commands are joined (as released), not M104/M109 T<n>.
    # Fixed against the release (owner 10/07): RESUME's RESTORE_GCODE_STATE
    # put the OLD tool's offsets back and returned the carriage to where the
    # old nozzle was (gcode_move.py l.248-260), so the new tool printed
    # shifted by the offset difference - the saved pause state now gets the
    # new tool's offsets; a join of a tool that was itself joined follows;
    # the new tool is hot, its melt zone refilled and it is back at the park
    # spot before RESUME (Mainsail's RESUME refuses a cold tool); START_PRINT
    # and END_PRINT clear the joins too (the release only on cancel).
    def cmd_SPOOL_JOIN(self, gcmd):
        if not self.printer.lookup_object('pause_resume').is_paused:
            raise gcmd.error("SPOOL_JOIN: only while a print is paused"
                             " (after a filament runout)")
        to_tool = gcmd.get_int('TO', minval=0, maxval=NUM_TOOLS - 1)
        from_tool = gcmd.get_int('FROM', self.active_tool)
        if from_tool < 0 or from_tool >= NUM_TOOLS:
            raise gcmd.error("SPOOL_JOIN: no tool picked - give FROM=n")
        if from_tool == to_tool:
            raise gcmd.error("SPOOL_JOIN: FROM and TO must be different")
        if to_tool not in self._installed():
            raise gcmd.error("T%d is not installed on this printer"
                             % (to_tool,))
        if not self.offset_calibrated[to_tool]:
            raise gcmd.error("T%d has no calibrated tool offset - it would"
                             " print misaligned" % (to_tool,))
        if not self._dock(to_tool).is_parked():
            raise gcmd.error(self._not_docked_msg(to_tool))
        # print temperature: TEMP=, else the old tool's target, else what the
        # pause timeout saved for it (_KXL_RESUME)
        from_ext = self._ext_name(from_tool)
        resume = self.printer.lookup_object('gcode_macro _KXL_RESUME', None)
        saved = dict(resume.variables.get('heaters', {})) if resume else {}
        temp = gcmd.get_float('TEMP', 0.)
        if temp <= 0.:
            temp = self.printer.lookup_object(from_ext).get_heater(
                ).get_status(self.reactor.monotonic())['target']
        if temp <= 0.:
            temp = saved.get(from_ext, 0.)
        if temp < 170.:
            raise gcmd.error("SPOOL_JOIN: no print temperature known for T%d"
                             " - give TEMP=" % (from_tool,))
        # the new tool heats during the swap; the empty one goes off and is
        # not re-heated by RESUME after a pause timeout
        self._run("M104 T%d S%.0f\nM104 T%d S0" % (to_tool, temp, from_tool))
        if resume is not None and from_ext in saved:
            del saved[from_ext]
            resume.variables = dict(resume.variables, heaters=saved)
        for k, v in list(self.tool_remap.items()):
            if v == from_tool:
                self.tool_remap[k] = to_tool
        self.tool_remap[from_tool] = to_tool
        self.tool_remap = {k: v for k, v in self.tool_remap.items() if k != v}
        gcmd.respond_info("Spool join: T%d -> T%d (%.0f C)"
                          % (from_tool, to_tool, temp))
        if self.active_tool != from_tool:
            gcmd.respond_info("T%d is not on the carriage - from now on T%d in"
                              " the G-code picks T%d"
                              % (from_tool, from_tool, to_tool))
            return
        gm = self.printer.lookup_object('gcode_move')
        state = gm.saved_states.get('PAUSE_STATE')
        if state is None:
            raise gcmd.error("SPOOL_JOIN: no saved pause position")
        park_x, park_y = gm.get_status()['gcode_position'][:2]
        self.park(gcmd)
        self.pick(gcmd, to_tool)
        # RESUME puts the NEW nozzle where the old one stopped, and keeps the
        # new tool's offsets (base/homing position; the carriage target moves
        # with them)
        for i in range(3):
            state['last_position'][i] += (gm.base_position[i]
                                          - state['base_position'][i])
            state['base_position'][i] = gm.base_position[i]
        state['homing_position'] = list(gm.homing_position)
        self._run("G90\nG1 X%.3f Y%.3f F%d"
                  % (park_x, park_y, TRAVEL_SPEED * 60.))
        gcmd.respond_info("Heating T%d to %.0f C..." % (to_tool, temp))
        self._run("M109 T%d S%.0f" % (to_tool, temp))
        # its filament back into the nozzle now (stock: at the first
        # extrusion; here before RESUME so the print carries on at once).
        # The same give-back as everywhere (xl_filament): a G1 here would
        # itself trip the first-extrusion give-back and push it twice.
        fil = self.printer.lookup_object('xl_filament', None)
        if fil is not None:
            fil.give_back(to_tool)
        gcmd.respond_info("Spool join done: T%d is at %.0f C - press RESUME"
                          % (to_tool, temp))
    def cmd_SPOOL_JOIN_STATUS(self, gcmd):
        if not self.tool_remap:
            gcmd.respond_info("No spool join active")
            return
        gcmd.respond_info("\n".join("T%d -> T%d" % kv
                                    for kv in sorted(self.tool_remap.items())))
    def cmd_SPOOL_JOIN_RESET(self, gcmd):
        if self.tool_remap:
            gcmd.respond_info("Spool joins cleared")
        self.tool_remap = {}
    def cmd_XL_PICK(self, gcmd):
        self._no_dock_cal(gcmd)
        self.pick(gcmd, gcmd.get_int('TOOL', minval=0, maxval=NUM_TOOLS - 1))
    def cmd_XL_PARK(self, gcmd):
        self._no_dock_cal(gcmd)
        self.park(gcmd)
    # ------------------------------------------------------------------
    # Dock calibration - released KlipperXL DOCK_CALIBRATE (puppy_bootloader
    # @1e677ba l.6361-7013), same steps, messages, moves and maths.  What
    # changed to work here, nothing else:
    # - tool state comes from the dock sensors (_sync): the Dwarfs run
    #   Klipper, there are no TMC-enable / select coils to write;
    # - the dock is marked with its nozzle light ALWAYS (released: cheese LED
    #   255/255) and every light goes back to its setting at the end;
    # - Z keeps its real height and is only marked homed if it was
    #   (SET_HOMED; owner 10/12: no made-up Z) - released set Z=100;
    # - step 3/4 hold the carriage with the motors (SET_STEPPER_ENABLE) -
    #   the released comment meant to ("Re-enable steppers to hold
    #   position"), stock does (selftest_dock state_hold_position); a motor
    #   switched on later, with the dock tightened, can snap the carriage up
    #   to 2 full steps that the step count never sees;
    # - a dock is staged for SAVE_CONFIG once its pick/park checks pass, and
    #   a failed dock reverts only itself (released staged at the
    #   measurement, so a dock that then failed was still saved).
    def cmd_DOCK_POSITIONS(self, gcmd):
        lines = []
        for t in range(NUM_TOOLS):
            x, y = self.dock_positions[t]
            lines.append("T%d: X=%.4f Y=%.4f (%s)" % (
                t, x, y, "calibrated" if self.dock_calibrated[t] else "default"))
        lines.append("Safe Y with tool: %.1f" % (SAFE_Y_WITH_TOOL,))
        lines.append("Safe Y without tool: %.1f" % (SAFE_Y_WITHOUT_TOOL,))
        gcmd.respond_info("\n".join(lines))
    def _cal_say(self, gcmd, text):
        gcmd.respond_info(text)
        self.dock_cal_lines.append(text)
    def dock_cal_text(self):
        """This step's messages for the Dock positions wizard (xl_wizards):
        the "Type DOCK_CAL_CONTINUE / DOCK_CAL_ABORT" lines are left out -
        the dialog has Continue and Cancel buttons for them."""
        out = []
        for msg in self.dock_cal_lines:
            for line in msg.split('\n'):
                line = line.strip()
                if line and 'DOCK_CAL_CONTINUE' not in line \
                        and 'DOCK_CAL_ABORT' not in line:
                    out.append(line)
        return " ".join(out)
    def cmd_DOCK_CALIBRATE(self, gcmd):
        self.dock_cal_lines = []
        if self.dock_cal_active:
            raise gcmd.error("Dock calibration already in progress! Use"
                             " DOCK_CAL_ABORT to cancel.")
        if self._printing():
            raise gcmd.error("DOCK_CALIBRATE: not while printing")
        dock = gcmd.get_int('DOCK', -1)
        installed = self._installed()
        if dock >= 0:
            if dock > NUM_TOOLS - 1:
                raise gcmd.error("Invalid dock %d (must be 0-%d)"
                                 % (dock, NUM_TOOLS - 1))
            if dock not in installed:
                raise gcmd.error("Dock %d (T%d) not available" % (dock, dock))
            self._cal_dock_list = [dock]
        else:
            self._cal_dock_list = list(installed)
        # originals, for a revert
        self._cal_saved = {t: (self.dock_positions[t], self.dock_calibrated[t])
                           for t in range(NUM_TOOLS)}
        self._cal_lights = dict(self.light_tool_policy)
        self._cal_dock_idx = 0
        self._cal_state = 'init'
        self._cal_verify_count = 0
        self._cal_measured = {}
        self.dock_cal_active = True
        self.dock_cal_result = 'running'
        try:
            self.gcode.register_command(
                'DOCK_CAL_CONTINUE', self._cmd_dock_cal_continue,
                desc="Continue dock calibration")
            self.gcode.register_command(
                'DOCK_CAL_ABORT', self._cmd_dock_cal_abort,
                desc="Abort dock calibration")
        except Exception:
            self.dock_cal_active = False
            raise gcmd.error("Failed to register calibration commands - is"
                             " another calibration running?")
        self._dock_cal_begin(gcmd)
    def _dock_cal_cleanup(self, result='failed'):
        self.dock_cal_active = False
        self.dock_cal_result = result
        for cmd in ('DOCK_CAL_CONTINUE', 'DOCK_CAL_ABORT'):
            try:
                self.gcode.register_command(cmd, None)
            except Exception:
                pass
        self.light_tool_policy = dict(self._cal_lights)
        self._lights_apply_all()
        # docks without their tool on the carriage: back to slow reports
        for t in self._installed():
            if t != self.active_tool:
                self._dock(t).set_active(False)
    def _dock_cal_revert(self, dock):
        self.dock_positions[dock], self.dock_calibrated[dock] = \
            self._cal_saved[dock]
        self._cal_measured.pop(dock, None)
    def _cmd_dock_cal_abort(self, gcmd):
        self.dock_cal_lines = []
        for dock in self._cal_dock_list:
            if dock in self._cal_measured:      # measured, not yet staged
                self._dock_cal_revert(dock)
        self._dock_cal_cleanup('aborted')
        self._cal_say(gcmd, "Dock calibration ABORTED. Positions reverted.")
    def _cmd_dock_cal_continue(self, gcmd):
        self.dock_cal_lines = []
        try:
            self._dock_cal_step(gcmd)
        except Exception as e:
            if self._cal_dock_idx < len(self._cal_dock_list):
                dock = self._cal_dock_list[self._cal_dock_idx]
                if dock in self._cal_measured:
                    self._dock_cal_revert(dock)
            self._dock_cal_cleanup()
            raise gcmd.error("Dock calibration error: %s" % (e,))
    def _dock_default(self, dock):
        return DOCK_FIRST_X + dock * DOCK_OFFSET_X, DOCK_Y
    def _dock_cal_mark(self, dock):
        self.light_tool_policy[dock] = LIGHT_STATES['ALWAYS']
        self._light_apply(dock)
    def _dock_cal_set_position(self, dock):
        # the carriage is at the dock: expected X/Y, the real Z if Z is known
        x, y = self._dock_default(dock)
        if self._cal_z is not None:
            self._run("SET_KINEMATIC_POSITION X=%.1f Y=%.1f Z=%.3f"
                      " SET_HOMED=xyz" % (x, y, self._cal_z))
        else:
            self._run("SET_KINEMATIC_POSITION X=%.1f Y=%.1f SET_HOMED=xy"
                      % (x, y))
    def _dock_cal_begin(self, gcmd):
        dock = self._cal_dock_list[self._cal_dock_idx]
        default_x, default_y = self._dock_default(dock)
        self._cal_say(gcmd, "\n=== DOCK CALIBRATION: DOCK %d (T%d) ===\n"
                          "Default position: X=%.1f Y=%.1f"
                          % (dock, dock, default_x, default_y))
        self._dock_cal_mark(dock)
        # Ensure Z is high enough for clearance
        toolhead = self.printer.lookup_object('toolhead')
        homed = toolhead.get_status(self.reactor.monotonic())['homed_axes']
        self._cal_z = None
        if 'z' in homed:
            if toolhead.get_position()[2] < 100.0:
                self._cal_say(gcmd, "Raising Z to 100mm for clearance...")
                self._run("G90\nG1 Z100 F600")
                toolhead.wait_moves()
            self._cal_z = toolhead.get_position()[2]
        # Check if a tool is currently picked
        self._sync(gcmd)
        if self.active_tool >= 0:
            self._cal_state = 'park_tool'
            self._cal_say(gcmd, "\nT%d is currently picked.\nXY steppers will be"
                              " disabled. Manually park the tool,\nthen type"
                              " DOCK_CAL_CONTINUE\n(or DOCK_CAL_ABORT to"
                              " cancel)" % (self.active_tool,))
            self._run("M84 X Y")
        else:
            self._cal_state = 'remove_pins'
            self._cal_say(gcmd, "\nSTEP 1: Remove the dock pins from Dock %d\n"
                              "Type DOCK_CAL_CONTINUE when done, or"
                              " DOCK_CAL_ABORT to cancel" % (dock,))
    def _dock_cal_step(self, gcmd):
        dock = self._cal_dock_list[self._cal_dock_idx]
        state = self._cal_state
        if state == 'park_tool':
            # the user parked the tool by hand - the dock sensors say so
            self._sync(gcmd)
            if self.active_tool >= 0:
                self._cal_say(gcmd, "T%d still reads picked - park it in its"
                                  " dock, then DOCK_CAL_CONTINUE"
                                  % (self.active_tool,))
                return
            self._cal_state = 'remove_pins'
            self._cal_say(gcmd, "\nSTEP 1: Remove the dock pins from Dock %d\n"
                              "Type DOCK_CAL_CONTINUE when done" % (dock,))
        elif state == 'remove_pins':
            self._cal_state = 'loosen_screws'
            self._cal_say(gcmd, "\nSTEP 2: Loosen BOTH screws on the right side"
                              " of Dock %d's pillar\nType DOCK_CAL_CONTINUE"
                              " when done" % (dock,))
        elif state == 'loosen_screws':
            # Disable XY steppers so user can move carriage by hand
            self._run("M84 X Y")
            self._cal_state = 'slide_carriage'
            self._cal_say(gcmd, "\nSTEP 3: Slide the carriage to align with T%d"
                              " and lock it onto the tool in Dock %d\n"
                              "    XY steppers are disabled - move by hand\n"
                              "Type DOCK_CAL_CONTINUE when locked"
                              % (dock, dock))
        elif state == 'slide_carriage':
            self._dock_cal_set_position(dock)
            # hold the carriage here while the dock is tightened
            self._run("SET_STEPPER_ENABLE STEPPER=stepper_x ENABLE=1\n"
                      "SET_STEPPER_ENABLE STEPPER=stepper_y ENABLE=1")
            # Verify dock sensors (fresh, fast readings - as pick does)
            d = self._dock(dock)
            d.set_active(True)
            if not d.wait_fresh():
                self._cal_say(gcmd, "WARNING: T%d dock sensors are not reporting"
                                  % (dock,))
            self._sync(gcmd)
            if not d.is_picked():
                self._cal_say(gcmd, "WARNING: T%d not detected as picked"
                                  " (is_picked=%s). Check alignment!"
                                  % (dock, d.is_picked()))
            self._cal_say(gcmd, "Dock sensors: picked=%s parked=%s"
                              % (d.is_picked(), d.is_parked()))
            self._cal_state = 'tighten_top'
            self._cal_say(gcmd, "\nSTEP 4: DO NOT MOVE THE CARRIAGE\n"
                              "    Tighten the TOP dock screw ONLY\n"
                              "Type DOCK_CAL_CONTINUE when done")
        elif state == 'tighten_top':
            self._cal_state = 'measuring'
            self._cal_say(gcmd, "\nSTEP 5: DO NOT TOUCH THE PRINTER - measuring"
                              " dock position...")
            self._dock_cal_measure(gcmd, dock)
        elif state == 'tighten_bottom':
            self._cal_state = 'install_pins'
            self._cal_say(gcmd, "\nSTEP 7: Install and tighten dock pins on Dock"
                              " %d\nType DOCK_CAL_CONTINUE when done" % (dock,))
        elif state == 'install_pins':
            self._cal_state = 'park_verify'
            self._cal_say(gcmd, "\nSTEP 8: DO NOT TOUCH - running park"
                              " verification...")
            self._dock_cal_park_verify(gcmd, dock)
        elif state == 'pick_park_verify':
            self._cal_state = 'verifying'
            self._cal_say(gcmd, "\nSTEP 9: DO NOT TOUCH - running %dx pick/park"
                              " verification..." % (DOCK_CAL_VERIFY_CYCLES,))
            self._cal_verify_count = 0
            self._dock_cal_pick_park_verify(gcmd, dock)
        elif state == 'dock_complete':
            self._dock_cal_next_dock(gcmd)
        else:
            self._cal_say(gcmd, "Unknown calibration state: %s" % (state,))
            self._dock_cal_cleanup()
    def _dock_cal_measure(self, gcmd, dock):
        """Released l.6626-6795: record the motor step counts at the dock,
        undock (X unlock + Y retreat), home X and Y, and the dock position is
        the home position + the displacement the step counts show."""
        toolhead = self.printer.lookup_object('toolhead')
        self._sync(gcmd)                 # the tool on the carriage, if any
        self._select_loadcell(-1)
        self._clear_offset()             # moves below = carriage position
        toolhead.wait_moves()
        stepper_a = stepper_b = None
        for s in toolhead.get_kinematics().get_steppers():
            if s.get_name() == 'stepper_x':
                stepper_a = s            # CoreXY A = X + Y
            elif s.get_name() == 'stepper_y':
                stepper_b = s            # CoreXY B = X - Y
        if stepper_a is None or stepper_b is None:
            self._dock_cal_cleanup()
            self._cal_say(gcmd, "FAILED: Could not find stepper_x/stepper_y")
            return
        pre_a = stepper_a.get_mcu_position()
        pre_b = stepper_b.get_mcu_position()
        step_dist_a = stepper_a.get_step_dist()
        step_dist_b = stepper_b.get_step_dist()
        logging.info("xl_toolchanger: dock cal T%d pre-dock MCU steps: A=%d"
                     " B=%d step_dist=%.6f", dock, pre_a, pre_b, step_dist_a)
        # Set approximate position for valid moves
        expected_x, expected_y = self._dock_default(dock)
        self._dock_cal_set_position(dock)
        # Unlock move: X displacement to release from dock, then Y retreat
        self._move(x=expected_x + DOCK_CAL_UNLOCK_X, speed=DOCK_CAL_SPEED)
        self._move(y=expected_y - DOCK_CAL_RETREAT_Y, speed=DOCK_CAL_SPEED)
        # Verify undocked
        self._settle()
        d = self._dock(dock)
        d.wait_fresh()
        if d.is_parked():
            self._cal_say(gcmd, "WARNING: Still reporting parked after undock!")
        self._cal_say(gcmd, "After undock: picked=%s parked=%s"
                          % (d.is_picked(), d.is_parked()))
        # Home XY to establish true absolute position
        self._cal_say(gcmd, "Homing X and Y...")
        self._run("G28 X Y")
        toolhead.wait_moves()
        post_a = stepper_a.get_mcu_position()
        post_b = stepper_b.get_mcu_position()
        home_pos = toolhead.get_position()
        logging.info("xl_toolchanger: dock cal T%d post-home MCU steps: A=%d"
                     " B=%d home_pos=(%.3f, %.3f)", dock, post_a, post_b,
                     home_pos[0], home_pos[1])
        # displacement dock -> home in joint mm, to cartesian
        delta_a_mm = (pre_a - post_a) * step_dist_a
        delta_b_mm = (pre_b - post_b) * step_dist_b
        delta_x = (delta_a_mm + delta_b_mm) / 2.0
        delta_y = (delta_a_mm - delta_b_mm) / 2.0
        measured_x = home_pos[0] + delta_x
        measured_y = home_pos[1] + delta_y
        logging.info("xl_toolchanger: dock cal T%d measured: X=%.4f Y=%.4f"
                     " (delta_a=%.3f delta_b=%.3f delta_x=%.3f delta_y=%.3f)",
                     dock, measured_x, measured_y, delta_a_mm, delta_b_mm,
                     delta_x, delta_y)
        # Validate against expected position
        offset = math.sqrt((measured_x - expected_x) ** 2
                           + (measured_y - expected_y) ** 2)
        if offset > DOCK_INVALID_OFFSET:
            self._cal_say(gcmd, "FAILED: Dock %d measured X=%.4f Y=%.4f\n"
                              "Offset %.2fmm exceeds maximum %.1fmm from"
                              " default (X=%.1f Y=%.1f)\nCheck dock alignment"
                              " and try again."
                              % (dock, measured_x, measured_y, offset,
                                 DOCK_INVALID_OFFSET, expected_x, expected_y))
            self._dock_cal_cleanup()
            return
        if measured_y > DOCK_Y_MAX:
            logging.warning("xl_toolchanger: dock cal T%d measured_y=%.4f"
                            " clamped to %.1f", dock, measured_y, DOCK_Y_MAX)
            measured_y = DOCK_Y_MAX
        self._cal_say(gcmd, "Dock %d measured: X=%.4f Y=%.4f (offset: %.2fmm"
                          " from default - OK)"
                          % (dock, measured_x, measured_y, offset))
        # in use from here on (the pick/park checks run on it)
        self.dock_positions[dock] = (measured_x, measured_y)
        self.dock_calibrated[dock] = True
        self._cal_measured[dock] = (measured_x, measured_y)
        self._cal_state = 'tighten_bottom'
        self._cal_say(gcmd, "\nSTEP 6: Tighten the BOTTOM dock screw on Dock %d"
                          "\nType DOCK_CAL_CONTINUE when done" % (dock,))
    def _dock_cal_park_verify(self, gcmd, dock):
        """Released l.6797-6890: a slow park at the measured position."""
        dock_x, dock_y = self._dock_x(dock), self._dock_y(dock)
        d = self._dock(dock)
        self._sync(gcmd)
        if self.active_tool != dock:
            self._cal_say(gcmd, "FAILED: T%d is not on the carriage (dock"
                              " sensors) - nothing to park" % (dock,))
            self._dock_cal_revert(dock)
            self._dock_cal_cleanup()
            return
        self._select_loadcell(-1)
        self._clear_offset()
        # approach, dock Y, unlock with parking current, insert
        self._move(x=dock_x + PARK_X_OFFSET_1, y=SAFE_Y_WITH_TOOL)
        self._move(y=dock_y)
        self._xy_current(PARKING_CURRENT)
        self._move(x=dock_x + PARK_X_OFFSET_2, speed=SLOW_SPEED)
        self._move(x=dock_x + PARK_X_OFFSET_3, speed=SLOW_SPEED)
        self._xy_current(NORMAL_CURRENT)
        self._move(x=dock_x, y=dock_y, speed=SLOW_SPEED)
        # Verify parked
        self._settle()
        parked = d.is_parked()
        if not parked:
            self._cal_say(gcmd, "FAILED: T%d not detected as parked!\nCheck dock"
                              " alignment and try again." % (dock,))
            self._dock_cal_revert(dock)
            self._dock_cal_cleanup()
            return
        # parked: the same bookkeeping as park()
        self.active_tool = -1
        self._fan_set(dock, 0., fatal=False)
        self._light_apply(dock)
        self._status_now(dock)
        # Retract from dock, verify not picked
        self._move(y=SAFE_Y_WITHOUT_TOOL)
        d.wait_fresh()
        if d.is_picked():
            self._cal_say(gcmd, "WARNING: T%d still reports picked after"
                              " retract!" % (dock,))
        d.set_active(False)
        self._cal_say(gcmd, "Park test: parked=%s - OK" % (parked,))
        self._cal_state = 'pick_park_verify'
        self._cal_say(gcmd, "\nPark verification passed!\nType DOCK_CAL_CONTINUE"
                          " to run %dx pick/park verification"
                          % (DOCK_CAL_VERIFY_CYCLES,))
    def _dock_cal_pick_park_verify(self, gcmd, dock):
        """Released l.6892-6968: pick/park cycles with the normal pick and
        park, on the measured position."""
        d = self._dock(dock)
        for cycle in range(self._cal_verify_count, DOCK_CAL_VERIFY_CYCLES):
            self._cal_verify_count = cycle + 1
            self._cal_say(gcmd, "Pick/Park cycle %d/%d..."
                              % (cycle + 1, DOCK_CAL_VERIFY_CYCLES))
            try:
                self.pick(gcmd, dock)
            except Exception as e:
                self._cal_say(gcmd, "FAILED: Pick failed on cycle %d: %s"
                                  % (cycle + 1, e))
                self._dock_cal_revert(dock)
                self._dock_cal_cleanup()
                return
            if not d.is_picked():
                self._cal_say(gcmd, "FAILED: T%d not picked on cycle %d"
                                  % (dock, cycle + 1))
                self._dock_cal_revert(dock)
                self._dock_cal_cleanup()
                return
            try:
                self.park(gcmd)
            except Exception as e:
                self._cal_say(gcmd, "FAILED: Park failed on cycle %d: %s"
                                  % (cycle + 1, e))
                self._dock_cal_revert(dock)
                self._dock_cal_cleanup()
                return
            if not d.is_parked():
                self._cal_say(gcmd, "FAILED: T%d not parked on cycle %d"
                                  % (dock, cycle + 1))
                self._dock_cal_revert(dock)
                self._dock_cal_cleanup()
                return
            self._cal_say(gcmd, "Pick/Park cycle %d/%d: OK"
                              % (cycle + 1, DOCK_CAL_VERIFY_CYCLES))
        self._cal_say(gcmd, "\nDock %d calibration COMPLETE!" % (dock,))
        # passed: stage it for SAVE_CONFIG (released key, this section)
        mx, my = self._cal_measured.pop(dock)
        self._stage_text('dock_pos_t%d' % (dock,), "%.4f, %.4f" % (mx, my))
        # the dock's light back to its own setting
        if dock in self._cal_lights:
            self.light_tool_policy[dock] = self._cal_lights[dock]
        else:
            self.light_tool_policy.pop(dock, None)
        self._light_apply(dock)
        self._cal_state = 'dock_complete'
        self._dock_cal_next_dock(gcmd)
    def _dock_cal_next_dock(self, gcmd):
        self._cal_dock_idx += 1
        if self._cal_dock_idx < len(self._cal_dock_list):
            dock = self._cal_dock_list[self._cal_dock_idx]
            self._cal_state = 'remove_pins'
            self._cal_verify_count = 0
            # Move carriage to center so user can access next dock
            self._move(x=180., y=350., speed=5000. / 60.)
            self._run("M84 X Y")
            default_x, default_y = self._dock_default(dock)
            self._cal_say(gcmd, "Carriage moved to center - steppers disabled\n"
                              "\n=== DOCK CALIBRATION: DOCK %d (T%d) ===\n"
                              "Default position: X=%.1f Y=%.1f"
                              % (dock, dock, default_x, default_y))
            self._dock_cal_mark(dock)
            self._cal_say(gcmd, "\nSTEP 1: Remove the dock pins from Dock %d\n"
                              "Type DOCK_CAL_CONTINUE when done, or"
                              " DOCK_CAL_ABORT to cancel" % (dock,))
        else:
            self._dock_cal_cleanup('complete')
            lines = ["\n=== ALL DOCK CALIBRATION COMPLETE ===",
                     "Calibrated positions:"]
            for dock in self._cal_dock_list:
                x, y = self.dock_positions[dock]
                lines.append("  T%d: X=%.4f Y=%.4f" % (dock, x, y))
            lines.append("\nRun SAVE_CONFIG to persist to printer.cfg")
            self._cal_say(gcmd, "\n".join(lines))
    def get_status(self, eventtime):
        return {'active_tool': self.active_tool,
                'spool_join': {'T%d' % k: v
                               for k, v in self.tool_remap.items()},
                'installed_tools': self._installed(),
                'z_home_tool': self.z_home_tool,
                'single_mode': self.single_mode,
                'print_fan_speed': self.print_fan_speed,
                'nozzle_leds': [k for k, v in LIGHT_STATES.items()
                                if v == self.light_policy][0],
                # START_PRINT refuses a multi-tool job using an uncalibrated tool
                'offset_calibrated': [self.offset_calibrated[t]
                                      for t in range(NUM_TOOLS)]}

def load_config(config):
    return XLToolchanger(config)
