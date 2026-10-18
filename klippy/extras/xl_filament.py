# KlipperXL-Plus - filament load / unload / autoload / M600
#
# Written fresh.  Behaviour = released KlipperXL puppy_bootloader.py
# @1b681b3 (Prusa M701 / M702 / M1701 via the release):
#   _do_autoload l.2980-3125, _check_side_autoload l.2880-2911,
#   cmd_LOAD_FILAMENT l.3510-3593, cmd_UNLOAD_FILAMENT l.3449-3508,
#   cmd_M600 l.3595-3613, cmd_AUTOLOAD_FILAMENT / cmd_SET_AUTOLOAD.
# One change, owner 2026-10-05: the release had two load copies that had
# drifted (LOAD_FILAMENT waited for the tool sensor without turning the
# extruder, which a hand cannot push far enough to reach, fast-loaded 40 mm
# not 50, and relied on an M302 that only logged).  There is now ONE load
# sequence - the release's autoload one - used by autoload and
# LOAD_FILAMENT.  The heater stays on only when loading during a pause (so
# RESUME can continue: runout -> reload the same tool); otherwise it is
# turned off at the end, as the release's autoload did.
#
# Runout = released _check_side_filament_runout (l.2821-2878) / cmd_M591
# (l.3429-3447): the picked tool's side sensor reads no filament on
# RUNOUT_DEBOUNCE readings in a row while a print is printing -> PAUSE.  A
# tool below MIN_TEMP is ignored (released 08-25: a cold nozzle is not using
# filament; an empty tool left on the carriage paused START_PRINT, and PAUSE
# then failed on the cold extruder).  Stock Prusa: the side sensor is the
# primary runout sensor (filament_sensors_handler.cpp l.199, 263).
# The pause itself is the way Klipper's runout helper does it
# (filament_switch_sensor.py _runout_event_handler).
#
#   [xl_filament]
#   #autoload: True
#
# Commands: LOAD_FILAMENT [TOOL=n] [TEMP=t]   UNLOAD_FILAMENT [TOOL=n] [TEMP=t]
#           AUTOLOAD_FILAMENT [TOOL=n]        SET_AUTOLOAD [ENABLE=0|1]
#           M600 [T=n]                        M591 [S=0|1] (runout off/on)
# TEMP absent or 0: the tool's current target, else 215 C (DEFAULT_TEMP).
# xl_macros.cfg wraps LOAD_FILAMENT / UNLOAD_FILAMENT in gcode_macros of the
# same name (rename_existing) - KlipperScreen only shows its Load/Unload
# buttons for macros.  TOOL other than the picked one is refused while a
# print is printing or paused (it would change tools mid-print).
#
# Copyright (C) 2026  Richard Crook
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging

NUM_TOOLS = 5
# Prusa constants via the release (_do_autoload / cmd_UNLOAD_FILAMENT)
ASSIST_CHUNK = 2.0        # mm per assist step
ASSIST_SPEED = 5.4        # mm/s, FILAMENT_CHANGE_SLOW_LOAD_FEEDRATE
ASSIST_TIMEOUT = 40.0     # s, Prusa assist_insertion
SLOW_LOAD = (30.0, 5.4)   # into the gears (cold)
FAST_LOAD = (50.0, 18.0)  # gears -> nozzle
PURGE = (27.0, 2.7)
LOAD_RETRACT = (4.0, 35.0)
MIN_TEMP = 170.           # Prusa EXTRUDE_MINTEMP
# ---- stock Prusa XL filament handling (owner 10/11: "follow stock") --------
# Ramming sequences: (mm, mm/min) steps, executed in order.
#   AUTO_RETRACT = standard_ramming_sequence.cpp StandardRammingSequence::
#     auto_retract: pulls the filament out of the nozzle (net 47 mm) and is
#     remembered per tool, so a loaded / finished tool does not ooze;
#     AutoRetract::minimum_auto_retract_distance 20 = "safely retracted"
#   UNLOAD_RAM / RUNOUT_RAM = Configuration_XL_adv.h FILAMENT_UNLOAD_ /
#     FILAMENT_RUNOUT_RAMMING_SEQUENCE (runout = M600 during a print)
AUTO_RETRACT_SEQ = ((8, 995), (-43, 6000), (-8, 3000), (-4, 1800),
                    (20, 600), (-20, 470), (55, 1740), (-55, 6000),
                    (20, 340), (-20, 210))
UNLOAD_RAM_SEQ = ((20, 1500), (-50, 2700), (-5, 50), (-50, 1500))
RUNOUT_RAM_SEQ = ((7, 1500), (-50, 2700), (-5, 50), (-50, 1500))
SAFELY_RETRACTED = 20.    # minimum_auto_retract_distance
PROBE_RETRACT = 20.       # probe.cpp: "Retract to standard distance"
PROBE_RETRACT_MIN = 5.    # probe.cpp: already retracted this much -> skip
# Retract tracker = retract_tracker.cpp RetractTracker: how far each tool's
#   filament sits back from the nozzle, from every E move since start-up,
#   0..40 mm.  Starts at 40 (unknown); trusted once it has come down to 0
#   (filament all the way in), not any more once it is back at 40.  In a
#   multi-tool print the slicer retracts at each tool change and the firmware
#   does not know - at print end / cancel every trusted tool's distance
#   becomes its record (marlin_server.cpp pre_finalize_print), so a parked
#   tool gets the slicer's retract back on its first extrusion next print.
TRACK_MAX = 40.           # RetractTracker::extruder_to_nozzle_distance
UNLOAD_LENGTH = 105.      # FILAMENT_CHANGE_UNLOAD_LENGTH
UNLOAD_SPEED = 27.        # FILAMENT_CHANGE_UNLOAD_FEEDRATE mm/s
DERETRACT_SPEED = 18.     # FILAMENT_CHANGE_FAST_LOAD_FEEDRATE mm/s
# extruder-only accelerations (Marlin uses retract_acceleration for every E-only
# move): DEFAULT_RETRACT_ACCELERATION 1200 (Configuration_XL.h l.751); the load's
# fast load at FILAMENT_CHANGE_FAST_LOAD_ACCEL 100 and the unload's final pull at
# FILAMENT_CHANGE_UNLOAD_ACCEL 1100 (Configuration_XL_adv.h l.953, 962;
# pause.cpp l.615-626, 1470-1485).  Klipper's own default here is ~8300.
E_ACCEL = 1200.
FAST_LOAD_ACCEL = 100.
UNLOAD_ACCEL = 1100.
PAUSE_RETRACT = (1.0, 10.8)   # PAUSE_PARK_RETRACT_LENGTH / _FEEDRATE
# Load / unload park (Configuration_XL.h l.1428-1448, parking.hpp l.76-86,
# M701_2.cpp l.108 / l.162, pause.cpp park_nozzle_and_notify): X unchanged,
# Y = Y_AXIS_PURGE_POS -7 (where the XL purges), Z first to
# max(Z + Z_NOZZLE_PARK_RISE 20, Z_AXIS_LOAD_POS 40 / Z_AXIS_UNLOAD_POS 20),
# then XY at NOZZLE_PARK_XY_FEEDRATE 100 mm/s (Z at NOZZLE_PARK_Z_FEEDRATE 5)
PARK_Y = -7.
PARK_Z_RISE = 20.
PARK_Z_LOAD = 40.
PARK_Z_UNLOAD = 20.
PARK_XY_SPEED = 100.
PARK_Z_SPEED = 5.
# Load/unload temperature when TEMP is not given and the tool has no target
# >= MIN_TEMP.  The release used 200 C; owner 2026-10-05: 215 C, between
# PLA and PETG, so either loads/unloads without picking a temperature.
DEFAULT_TEMP = 215.
RUNOUT_DEBOUNCE = 2       # released FS_RUNOUT_DEBOUNCE (consecutive readings)
RUNOUT_PAUSE_DELAY = 0.5  # s, Klipper runout helper pause_delay default


class XLFilament:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        self.autoload_enabled = config.getboolean('autoload', True)
        self._in_progress = False         # a load is running
        self._autoload_pending = False    # an autoload is queued
        self.runout_enabled = True        # save_variables fs_runout_enabled
        self._runout_count = {}           # tool -> no_filament readings in a row
        self._runout_pending = False      # a runout PAUSE is queued
        self._pending = set()             # tools with a pulled-back filament
        self._tracked = [TRACK_MAX] * NUM_TOOLS   # retract tracker, mm back
        self._track_ok = [False] * NUM_TOOLS      # retract tracker trusted
        self._gm = None
        for cmd, desc in (
                ('LOAD_FILAMENT', "Load filament into a tool (Prusa M701)"),
                ('UNLOAD_FILAMENT', "Unload filament from a tool (Prusa M702)"),
                ('AUTOLOAD_FILAMENT', "Run the autoload sequence for a tool"),
                ('SET_AUTOLOAD', "Enable/disable filament autoload"),
                ('M600', "Filament change: pause, unload, then load + RESUME"),
                ('M591', "Filament runout detection: S0 off, S1 on"),
                ('XL_AUTO_RETRACT', "Stock auto retract: the picked tool's"
                 " filament out of the nozzle, remembered"),
                ('XL_PROBE_RETRACT', "Stock: pull the probe tool back 20 mm"
                 " before bed probing"),
                ('XL_GIVE_BACK', "Give back what a tool has pulled back")):
            self.gcode.register_command(cmd, getattr(self, 'cmd_' + cmd),
                                        desc=desc)
        self.printer.register_event_handler("klippy:connect", self._connect)
        self.printer.register_event_handler("xl_side_sensors:state",
                                            self._side_state)
        self.printer.register_event_handler("xl_side_sensors:reading",
                                            self._side_reading)
    def _connect(self):
        self._gm = self.printer.lookup_object('gcode_move')
        for cmd in ('G1', 'G0'):
            orig = self.gcode.register_command(cmd, None)
            if orig is None:
                raise self.printer.config_error("xl_filament: no %s to wrap"
                                                % (cmd,))
            self.gcode.register_command(
                cmd, lambda gcmd, o=orig: self._cmd_move(gcmd, o))
        self._pending = set(t for t in range(NUM_TOOLS)
                            if self._melt(t) > 0.)
        # retract tracker: stock counts every E move in the planner
        # (planner.cpp l.2411); Klipper checks each move on the extruder that
        # makes it
        tracked = []
        for t in range(NUM_TOOLS):
            ext = self.printer.lookup_object(self._ext_name(t), None)
            if ext is not None:
                self._track_extruder(t, ext)
                tracked.append("T%d" % (t,))
        logging.info("xl_filament: retract tracker on %s", " ".join(tracked))
        sv = self.printer.lookup_object('save_variables', None)
        if sv is not None:
            self.runout_enabled = bool(
                sv.allVariables.get('fs_runout_enabled', 1))
        logging.info("xl_filament: runout detection %s",
                     "enabled" if self.runout_enabled else "disabled")
    # helpers
    def _installed(self, tool):
        return (0 <= tool < NUM_TOOLS and self.printer.lookup_object(
            'xl_dock_sensors T%d' % (tool,), None) is not None)
    def _ext_name(self, tool):
        return 'extruder' if tool == 0 else 'extruder%d' % (tool,)
    def _fsensor(self, tool):
        return self.printer.lookup_object('xl_tool_fsensor T%d' % (tool,),
                                          None)
    def _print_state(self):
        stats = self.printer.lookup_object('print_stats', None)
        if stats is None:
            return 'standby'
        return stats.get_status(self.reactor.monotonic())['state']
    def _tool_param(self, gcmd):
        tc = self.printer.lookup_object('xl_toolchanger')
        tool = gcmd.get_int('TOOL', tc.active_tool)
        if tool is None or tool < 0:
            raise gcmd.error("No TOOL given and no tool picked")
        if not self._installed(tool):
            raise gcmd.error("T%d is not installed on this printer" % (tool,))
        # during a print (M600) or a pause (runout) only the tool on the
        # carriage: another one would change tools in the middle of the job
        state = self._print_state()
        if state in ('printing', 'paused') and tool != tc.active_tool:
            raise gcmd.error("A print is %s with T%s picked - T%d cannot be"
                             " loaded/unloaded now (it would change tools"
                             " mid-print)" % (state, tc.active_tool, tool))
        return tool
    def _pick(self, tool):
        # The release's TOOL_PICK homed by itself; KlipperXL-Plus's pick needs X/Y
        # homed, so home them here first (XY only: a full G28 would probe Z).
        tc = self.printer.lookup_object('xl_toolchanger')
        if tc.active_tool == tool:
            return
        toolhead = self.printer.lookup_object('toolhead')
        homed = toolhead.get_status(self.reactor.monotonic())['homed_axes']
        if 'x' not in homed or 'y' not in homed:
            self.gcode.respond_info("XY not homed - homing X Y first...")
            self.gcode.run_script_from_command("G28 X Y")
        self.gcode.respond_info("Picking T%d..." % (tool,))
        self.gcode.run_script_from_command("T%d" % (tool,))
    def _temp(self, tool, temp):
        if temp <= 0.:
            heater = self.printer.lookup_object(
                self._ext_name(tool)).get_heater()
            temp = heater.get_status(self.reactor.monotonic())['target']
        if temp < MIN_TEMP:
            temp = DEFAULT_TEMP
            self.gcode.respond_info("Using default temp: %.0fC" % (temp,))
        return temp
    def _e_move(self, mm, speed, accel=E_ACCEL):
        toolhead = self.printer.lookup_object('toolhead')
        extruder = toolhead.get_extruder()
        saved = getattr(extruder, 'max_e_accel', None)
        if saved is not None:
            extruder.max_e_accel = accel     # extruder.py check_move limit
        try:
            pos = list(toolhead.get_position())
            pos[3] += mm
            toolhead.manual_move(pos, speed)
            toolhead.wait_moves()
        finally:
            if saved is not None:
                extruder.max_e_accel = saved
    # retract tracker (RetractTracker::track_extruder_move), after Klipper
    # has accepted the move (a refused one did not happen)
    def _track_extruder(self, tool, ext):
        check = ext.check_move
        def check_move(move, *args):
            check(move, *args)
            self._track(tool, move.axes_d[args[0] if args else 3])
        ext.check_move = check_move
    def _track(self, tool, de):
        d = min(max(self._tracked[tool] - de, 0.), TRACK_MAX)
        self._tracked[tool] = d
        if not self._track_ok[tool] and d == 0.:
            self._track_ok[tool] = True
        elif self._track_ok[tool] and d == TRACK_MAX:
            self._track_ok[tool] = False
    # stock pre_finalize_print: every trusted tool's tracked distance becomes
    # its record, in whole mm (config_store rounds: std::round)
    def _save_tracked(self, gcmd):
        saved = []
        for t in range(NUM_TOOLS):
            if not self._track_ok[t]:
                continue
            mm = float(int(self._tracked[t] + .5))
            if mm != self._melt(t):
                self._set_melt(t, mm)
            if mm:
                saved.append("T%d %.0f mm" % (t, mm))
        if saved:
            gcmd.respond_info("Retract tracker: %s pulled back"
                              % (", ".join(saved),))
    # melt_retract_tN = how many mm this tool's filament is pulled back
    # (stock AutoRetract::retracted_distance, kept in config_store; here in
    # save_variables).  Every pull-back records it, so does the retract
    # tracker at print end; the give-back clears it.
    def _melt(self, tool):
        sv = self.printer.lookup_object('save_variables', None)
        if sv is None:
            return 0.
        try:
            return float(sv.allVariables.get('melt_retract_t%d' % (tool,), 0))
        except (TypeError, ValueError):
            return 0.
    def _set_melt(self, tool, mm):
        self.gcode.run_script_from_command(
            "SAVE_VARIABLE VARIABLE=melt_retract_t%d VALUE=%s"
            % (tool, ('%.2f' % (mm,)) if mm else '0'))
        if mm > 0.:
            self._pending.add(tool)
        else:
            self._pending.discard(tool)
    def _run_seq(self, seq):
        for mm, mm_min in seq:
            self._e_move(float(mm), mm_min / 60.)
    def _cold_ok(self, tool):
        # allow_cold_extrude on THIS tool for a block (as _load)
        heater = self.printer.lookup_object(self._ext_name(tool)).get_heater()
        saved = heater.min_extrude_temp
        heater.min_extrude_temp = 0.
        heater.can_extrude = True
        return heater, saved
    def _hot(self, tool):
        heater = self.printer.lookup_object(self._ext_name(tool)).get_heater()
        temp = heater.get_temp(self.reactor.monotonic())[0]
        return temp >= heater.min_extrude_temp, temp
    def _park_front(self, z_min):
        # stock park for a load / unload: Z up first, then XY to the front
        toolhead = self.printer.lookup_object('toolhead')
        st = toolhead.get_status(self.reactor.monotonic())
        if 'x' not in st['homed_axes'] or 'y' not in st['homed_axes']:
            self.gcode.respond_info("XY not homed - homing X Y first...")
            self.gcode.run_script_from_command("G28 X Y")
            st = toolhead.get_status(self.reactor.monotonic())
        pos = toolhead.get_position()
        if 'z' in st['homed_axes']:
            z = min(max(pos[2] + PARK_Z_RISE, z_min), st['axis_maximum'][2])
            if z > pos[2]:
                toolhead.manual_move([None, None, z], PARK_Z_SPEED)
        toolhead.manual_move([None, PARK_Y, None], PARK_XY_SPEED)
        toolhead.wait_moves()
    def _fan_hold(self, tool, off):
        # stock: print fan off while loading / purging (pause.cpp
        # FSM_HolderLoadUnload), put back after
        tc = self.printer.lookup_object('xl_toolchanger', None)
        if tc is not None:
            tc.hold_fan(tool, 0. if off else None)
    def _clear_melt_retract(self, tool):
        # release _clear_melt_retract: a load refills the melt zone and an
        # unload empties the path, so END_PRINT's deep-retract record is stale
        sv = self.printer.lookup_object('save_variables', None)
        if sv is None or not sv.allVariables.get('melt_retract_t%d' % (tool,)):
            self._pending.discard(tool)
            return
        self._set_melt(tool, 0)
    # the load sequence (release _do_autoload).  arrived: when the filament
    # has reached the tool sensor - by default its calibrated state.
    def _load(self, tool, temp, arrived=None, autoload=False):
        fs = self._fsensor(tool)
        if arrived is None:
            if fs is None or fs.cal is None:
                self.gcode.respond_info(
                    "T%d: Tool sensor not calibrated - run"
                    " CALIBRATE_FILAMENT_SENSOR TOOL=%d first" % (tool, tool))
                return False
            arrived = lambda: fs.state == 'has_filament'
        self._pick(tool)
        # stock M1701 (pause.cpp l.408-413): an autoload whose filament is
        # already at the tool sensor (pushed in while the tool was picked)
        # skips the assist and goes straight into the gears; a LOAD_FILAMENT
        # by hand still refuses (stock asks the user there)
        at_sensor = arrived()
        if at_sensor and not autoload:
            self.gcode.respond_info("T%d: Sensor already reads filament -"
                                    " check filament is removed" % (tool,))
            return False
        keep_hot = self._print_state() == 'paused'
        # stock: to the front (Y -7, Z up) before the filament goes in, print
        # fan off while loading
        self.gcode.respond_info("T%d: to the front for loading..." % (tool,))
        self._park_front(PARK_Z_LOAD)
        self._fan_hold(tool, True)
        # cold extrusion for the whole sequence (release / Prusa
        # allow_cold_extrude during assist insertion) - on THIS tool's
        # heater only; Klipper recomputes can_extrude from it (heaters.py)
        heater, saved_min = self._cold_ok(tool)
        try:
            # 1: assist insertion - extruder turns, stop on the tool sensor
            if at_sensor:
                self.gcode.respond_info("T%d: filament already at the tool"
                                        " sensor - into the gears" % (tool,))
            else:
                self.gcode.respond_info("T%d: Extruder turning - push"
                                        " filament in..." % (tool,))
            start = self.reactor.monotonic()
            while not arrived():
                if self.reactor.monotonic() - start >= ASSIST_TIMEOUT:
                    self.gcode.respond_info(
                        "T%d: Tool sensor not triggered after %.0fs -"
                        " aborting" % (tool, ASSIST_TIMEOUT))
                    return False
                self._e_move(ASSIST_CHUNK, ASSIST_SPEED)
            self.gcode.respond_info("T%d: Filament at tool sensor" % (tool,))
            # 1b: slow load into the gears (cold)
            self._e_move(*SLOW_LOAD)
            # 2: heat
            temp = self._temp(tool, temp)
            self.gcode.respond_info("T%d: Heating to %.0fC..." % (tool, temp))
            self.gcode.run_script_from_command("M109 T%d S%.0f" % (tool, temp))
            # 3-5: fast load, purge, retract
            self.gcode.respond_info("T%d: Fast loading to nozzle..." % (tool,))
            self._e_move(FAST_LOAD[0], FAST_LOAD[1], FAST_LOAD_ACCEL)
            self.gcode.respond_info("T%d: Purging..." % (tool,))
            self._e_move(*PURGE)
            self._e_move(-LOAD_RETRACT[0], LOAD_RETRACT[1])
            self._clear_melt_retract(tool)
            if keep_hot:
                # stock load_prime_process (filament change): 4 mm back in,
                # then the pause retract again - RESUME gives that 1 mm back
                self._e_move(LOAD_RETRACT[0], 10.)
                self._e_move(-PAUSE_RETRACT[0], PAUSE_RETRACT[1])
                self.gcode.respond_info("T%d: Print is paused - heater left"
                                        " on for RESUME" % (tool,))
            else:
                # stock: outside a print the filament is auto-retracted out
                # of the nozzle (pause.cpp load_prime_process -> auto_retract)
                self.gcode.respond_info("T%d: pulling the filament out of the"
                                        " nozzle (stock auto retract)..."
                                        % (tool,))
                self._run_seq(AUTO_RETRACT_SEQ)
                self._set_melt(tool, self._seq_retracted(AUTO_RETRACT_SEQ))
                self.gcode.run_script_from_command("M104 T%d S0" % (tool,))
            self.gcode.respond_info("T%d: Filament loaded and purged" % (tool,))
            return True
        finally:
            heater.min_extrude_temp = saved_min
            self._fan_hold(tool, False)
    @staticmethod
    def _seq_retracted(seq):
        # RammingSequence::retracted_distance: -sum(e), never below 0
        return max(-sum(mm for mm, _ in seq), 0.)
    # commands
    def cmd_LOAD_FILAMENT(self, gcmd):
        tool = self._tool_param(gcmd)
        if self._in_progress:
            raise gcmd.error("A load is already in progress")
        self._in_progress = True
        try:
            self._load(tool, gcmd.get_float('TEMP', 0.))
        finally:
            self._in_progress = False
    def cmd_AUTOLOAD_FILAMENT(self, gcmd):
        tool = self._tool_param(gcmd)
        if self._in_progress:
            raise gcmd.error("A load is already in progress")
        self._in_progress = True
        try:
            self._load(tool, gcmd.get_float('TEMP', 0.), autoload=True)
        finally:
            self._in_progress = False
    def cmd_SET_AUTOLOAD(self, gcmd):
        enable = gcmd.get_int('ENABLE', None)
        if enable is not None:
            self.autoload_enabled = bool(enable)
        gcmd.respond_info("Filament autoload: %s"
                          % ("ENABLED" if self.autoload_enabled else "DISABLED"))
    def cmd_UNLOAD_FILAMENT(self, gcmd):
        # stock M702 (pause.cpp ram_sequence / unload_filament): to the front,
        # print fan off; a tool already pulled back >= 20 mm is unloaded cold
        # (no heating, no ramming); else heat, ram, then the rest of the
        # 105 mm out at 27 mm/s
        tool = self._tool_param(gcmd)
        if self._in_progress:
            raise gcmd.error("A load is already in progress")
        self._in_progress = True
        try:
            self._unload(gcmd, tool)
        finally:
            self._in_progress = False
    def _unload(self, gcmd, tool):
        self._pick(tool)
        gcmd.respond_info("T%d: to the front for unloading..." % (tool,))
        self._park_front(PARK_Z_UNLOAD)
        self._fan_hold(tool, True)
        heater = saved_min = None
        try:
            pulled = self._melt(tool)
            if pulled >= SAFELY_RETRACTED:
                gcmd.respond_info("T%d: filament already %.0f mm out of the"
                                  " nozzle - unloading cold" % (tool, pulled))
                rammed = pulled
            else:
                temp = self._temp(tool, gcmd.get_float('TEMP', 0.))
                gcmd.respond_info("Heating T%d to %.0fC for unload..."
                                  % (tool, temp))
                self.gcode.run_script_from_command("M109 T%d S%.0f"
                                                   % (tool, temp))
                seq = (RUNOUT_RAM_SEQ if self._print_state() in
                       ('printing', 'paused') else UNLOAD_RAM_SEQ)
                gcmd.respond_info("Ramming...")
                self._run_seq(seq)
                rammed = self._seq_retracted(seq)
            heater, saved_min = self._cold_ok(tool)
            gcmd.respond_info("Retracting filament...")
            rest = max(UNLOAD_LENGTH - rammed, 0.)
            if rest:
                self._e_move(-rest, UNLOAD_SPEED, UNLOAD_ACCEL)
        finally:
            if heater is not None:
                heater.min_extrude_temp = saved_min
            self._fan_hold(tool, False)
        self._clear_melt_retract(tool)
        fs = self._fsensor(tool)
        # a reading taken after the pull, not the last one before it
        # (printer 10/11: 'still reads has_filament' right after a good unload)
        if fs is not None and fs.state == 'has_filament':
            deadline = self.reactor.monotonic() + 1.5
            while (fs.state == 'has_filament'
                   and self.reactor.monotonic() < deadline):
                self.reactor.pause(self.reactor.monotonic() + 0.1)
        state = fs.state if fs is not None else 'not_calibrated'
        if state == 'no_filament':
            gcmd.respond_info("T%d: Filament unloaded successfully" % (tool,))
        elif state in ('not_calibrated', 'unknown'):
            gcmd.respond_info("T%d: Unload complete (sensor not calibrated)"
                              % (tool,))
        else:
            # Normal after stock's ramming + 105 mm: the tip is out of the
            # gears but still at the tool sensor, and the user takes it out -
            # stock's plain unload just finishes, its confirmed unload waits
            # for that (pause.cpp unload_process, filament_not_in_fs_process).
            # Printer 10/12: T4 hot unload, sensor still read filament, the
            # filament slid out with a clean tip.  Not a warning.
            gcmd.respond_info("T%d: unloaded - pull the filament out of the"
                              " extruder" % (tool,))
    # stock marlin_server.cpp pre_finalize_print (print end and cancel): the
    # retract tracker's distances saved, then AutoRetract::
    # maybe_retract_from_nozzle - the picked tool, if hot and not already
    # pulled back >= 20 mm
    def cmd_XL_AUTO_RETRACT(self, gcmd):
        self._save_tracked(gcmd)
        tc = self.printer.lookup_object('xl_toolchanger')
        tool = gcmd.get_int('TOOL', tc.active_tool)
        if tool < 0 or tool != tc.active_tool:
            gcmd.respond_info("Auto retract: T%s is not on the carriage -"
                              " skipped" % (tool,))
            return
        hot, temp = self._hot(tool)
        if not hot:
            gcmd.respond_info("Auto retract: T%d at %.0f C is too cold -"
                              " skipped" % (tool, temp))
            return
        if self._melt(tool) >= SAFELY_RETRACTED:
            return
        # stock checks the temperature once, at the start; Mainsail's
        # CANCEL_PRINT has already turned the heaters off, and the ~17 s
        # sequence must not stop half way when the nozzle passes 170 C
        heater, saved_min = self._cold_ok(tool)
        try:
            # stock maybe_deretract_to_nozzle first: what the slicer left
            # pulled back goes in, so the ramming starts from a full nozzle
            self.give_back(tool)
            gcmd.respond_info("T%d: pulling the filament out of the nozzle"
                              " (stock auto retract)..." % (tool,))
            self._run_seq(AUTO_RETRACT_SEQ)
        finally:
            heater.min_extrude_temp = saved_min
        self._set_melt(tool, self._seq_retracted(AUTO_RETRACT_SEQ))
    # stock probe.cpp (before bed probing): the probe tool pulled back to the
    # standard 20 mm unless already back >= 5 mm, so the nozzle is empty
    # while it probes.  Heats to TEMP for it (stock: the filament preset).
    def cmd_XL_PROBE_RETRACT(self, gcmd):
        tc = self.printer.lookup_object('xl_toolchanger')
        tool = gcmd.get_int('TOOL', tc.active_tool)
        if tool < 0 or tool != tc.active_tool:
            raise gcmd.error("XL_PROBE_RETRACT: T%s is not on the carriage"
                             % (tool,))
        pulled = self._melt(tool)
        if pulled >= PROBE_RETRACT_MIN:
            return
        fs = self._fsensor(tool)
        if fs is not None and fs.state == 'no_filament':
            return
        temp = self._temp(tool, gcmd.get_float('TEMP', 0.))
        gcmd.respond_info("T%d: pulling the filament back %.0f mm before"
                          " probing (heating to %.0f C)..."
                          % (tool, PROBE_RETRACT - pulled, temp))
        self.gcode.run_script_from_command("M109 T%d S%.0f" % (tool, temp))
        self._e_move(-(PROBE_RETRACT - pulled), DERETRACT_SPEED)
        self._set_melt(tool, PROBE_RETRACT)
    # stock maybe_deretract_to_nozzle: give back what is pulled back, at
    # 18 mm/s, on the picked tool once it can extrude
    def give_back(self, tool, gcmd=None):
        pulled = self._melt(tool)
        if pulled <= 0.:
            return False
        tc = self.printer.lookup_object('xl_toolchanger')
        if tool != tc.active_tool or not self._hot(tool)[0]:
            return False
        self.gcode.respond_info("T%d: giving back the %.0f mm pulled out of"
                                " the nozzle" % (tool, pulled))
        gcode_e = self._gm.get_status(
            self.reactor.monotonic())['gcode_position'][3]
        self._e_move(pulled, DERETRACT_SPEED)
        # stock: "fake" the original E position - the give-back is not part of
        # the print's extrusion, so absolute-E G-code carries on unchanged
        self.gcode.run_script_from_command("G92 E%.5f" % (gcode_e,))
        self._set_melt(tool, 0)
        return True
    def cmd_XL_GIVE_BACK(self, gcmd):
        tc = self.printer.lookup_object('xl_toolchanger')
        tool = gcmd.get_int('TOOL', tc.active_tool)
        if not self.give_back(tool, gcmd) and self._melt(tool) > 0.:
            gcmd.respond_info("T%d: %.0f mm still pulled back (not on the"
                              " carriage or not hot yet)"
                              % (tool, self._melt(tool)))
    # G0 / G1 wrapped: the first forward extrusion of a tool that has a
    # pulled-back filament gives it back first - stock planner.cpp l.2416
    # (any forward E move, printing or not: the purge line, a mid-print tool
    # change, the PA test, an Extrude button).  Not during a load / unload
    # (they move E themselves, not through G1).  Fast path when nothing is
    # pulled back.
    def _cmd_move(self, gcmd, orig):
        if self._pending and not self._in_progress:
            self._move_give_back(gcmd)
        orig(gcmd)
    def _move_give_back(self, gcmd):
        params = gcmd.get_command_parameters()
        if 'E' not in params:
            return
        tool = self.printer.lookup_object('xl_toolchanger').active_tool
        if tool not in self._pending:
            return
        try:
            v = float(params['E'])
        except ValueError:
            return
        gm = self._gm
        if gm.absolute_coord and gm.allow_absolute_extrude:
            forward = (v * gm.extrude_factor + gm.base_position[3]
                       > gm.last_position[3])
        else:
            forward = v > 0.
        if forward:
            self.give_back(tool)
    def cmd_M600(self, gcmd):
        tc = self.printer.lookup_object('xl_toolchanger')
        tool = gcmd.get_int('T', tc.active_tool)
        gcmd.respond_info("Filament change on T%d - pausing print..." % (tool,))
        self.gcode.run_script_from_command("PAUSE")
        gcmd.respond_info("Unloading old filament...")
        self.gcode.run_script_from_command("UNLOAD_FILAMENT TOOL=%d" % (tool,))
        gcmd.respond_info("Old filament unloaded. Insert new filament and run:\n"
                          "  LOAD_FILAMENT TOOL=%d\n  RESUME" % (tool,))
    def cmd_M591(self, gcmd):
        s = gcmd.get_int('S', None)
        if s in (0, 1):
            self.runout_enabled = bool(s)
            sv = self.printer.lookup_object('save_variables', None)
            if sv is not None:
                sv.cmd_SAVE_VARIABLE(self.gcode.create_gcode_command(
                    "SAVE_VARIABLE", "SAVE_VARIABLE",
                    {"VARIABLE": "fs_runout_enabled", "VALUE": str(s)}))
            gcmd.respond_info("Filament runout detection %s"
                              % ("ENABLED" if s else "DISABLED"))
        else:
            gcmd.respond_info("Filament runout detection: %s"
                              % ("ENABLED" if self.runout_enabled
                                 else "DISABLED"))
    # runout (release _check_side_filament_runout), on every side sensor
    # reading
    def _side_reading(self, tool, state):
        if not self.runout_enabled:
            return
        tc = self.printer.lookup_object('xl_toolchanger')
        if tool != tc.active_tool:
            return
        heater = self.printer.lookup_object(self._ext_name(tool)).get_heater()
        temp = heater.get_temp(self.reactor.monotonic())[0]
        if temp < MIN_TEMP:
            self._runout_count[tool] = 0
            return
        if self._print_state() != 'printing':
            return
        if state != 'no_filament':
            self._runout_count[tool] = 0
            return
        count = self._runout_count.get(tool, 0) + 1
        self._runout_count[tool] = count
        if count < RUNOUT_DEBOUNCE:
            logging.info("xl_filament: side sensor T%d no_filament (%d/%d)",
                         tool, count, RUNOUT_DEBOUNCE)
            return
        self._runout_count[tool] = 0
        if self._runout_pending:
            return
        logging.warning("xl_filament: SIDE SENSOR RUNOUT on T%d (%d readings)",
                        tool, count)
        self.gcode.respond_info("!! Filament runout detected on T%d (side"
                                " sensor)! Pausing print." % (tool,))
        self._runout_pending = True
        self.reactor.register_callback(self._runout_pause)
    def _runout_pause(self, eventtime):
        # Klipper runout helper: stop the print file at once, then the PAUSE
        # macro (Mainsail's: retract, lift, park) through the g-code queue
        try:
            self.printer.lookup_object('pause_resume').send_pause_command()
            self.reactor.pause(eventtime + RUNOUT_PAUSE_DELAY)
            self.gcode.run_script("PAUSE\nM400")
        except Exception:
            logging.exception("xl_filament: runout PAUSE failed")
        finally:
            self._runout_pending = False
    # autoload trigger (release _check_side_autoload): filament pushed into a
    # side sensor while the printer is idle
    def _side_state(self, tool, old, new):
        if (not self.autoload_enabled or self._in_progress
                or self._autoload_pending):
            return
        if old != 'no_filament' or new != 'has_filament':
            return
        if self._print_state() in ('printing', 'paused'):
            return
        if not self._installed(tool):
            return
        logging.info("xl_filament: side sensor T%d insertion - autoload", tool)
        self.gcode.respond_info("Filament detected in T%d side sensor -"
                                " autoloading..." % (tool,))
        self._autoload_pending = True
        self.reactor.register_callback(lambda e, t=tool: self._autoload(t))
    def _autoload(self, tool):
        # Through the g-code queue (takes the g-code mutex like any command,
        # so it cannot interleave with something else running)
        try:
            self.gcode.run_script("AUTOLOAD_FILAMENT TOOL=%d" % (tool,))
        except Exception as e:
            logging.exception("xl_filament: autoload T%d failed", tool)
            self.gcode.respond_info("Autoload T%d failed: %s" % (tool, e))
        finally:
            self._autoload_pending = False
    def get_status(self, eventtime):
        return {'autoload': self.autoload_enabled,
                'runout': self.runout_enabled,
                'in_progress': self._in_progress or self._autoload_pending}

def load_config(config):
    return XLFilament(config)
