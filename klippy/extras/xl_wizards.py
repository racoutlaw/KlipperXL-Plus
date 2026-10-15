# KlipperXL-Plus - guided calibrations ("wizards") on KlipperScreen / Mainsail
#
# A fresh install has no calibration to carry over (owner 2026-10-05), so each
# one is a guided procedure like stock Prusa's wizards, shown as Klipper
# action:prompt dialogs (docs/CALIBRATION_WIZARDS.md).  KlipperScreen
# (ks_includes/widgets/prompts.py @ed40799 on the printer) shows ONE
# prompt_text per dialog - each one replaces the last - sends a button's g-code
# without closing the dialog, and its X sends action:prompt_end; Mainsail /
# Fluidd read the same lines.  So every step opens the next dialog itself
# (a prompt_begin replaces the open one).
#
# The calibrations are the existing commands, unchanged:
#   loadcell  per tool: pick, M109 150, "nothing touching", XL_LOAD_CELL_REFERENCE
#   pid       per tool: COLD (owner 10/04: nozzle within 3 C of the coolest
#             other tool), pick, PID_CALIBRATE 215, heater off, park
#   fsensor   per tool, Prusa selftest_fsensor: pick, unload if loaded, both
#             sensors empty, filament pushed in by hand until the tool
#             sensor sees it, both loaded, pulled out until both read
#             empty - autoload held off meanwhile
#   zalign    the released PRUSA_Z_LEVEL (xl_macros.cfg), then home all -
#             nothing to save
#   docks     the released DOCK_CALIBRATE (xl_toolchanger.py), one dock or
#             all: each step's text in a dialog, Continue = DOCK_CAL_CONTINUE,
#             Cancel = DOCK_CAL_ABORT; the result shown, Save = SAVE_CONFIG
#   offsets   the released CALIBRATE_TOOL_OFFSETS (xl_tool_offsets.py): ONE
#             session for all tools (offsets are against T0) - sheet,
#             cleaning, home, sheet -> pin, probing, pin -> sheet, one save
#   shaper    the released CALIBRATE_INPUT_SHAPER (Klipper SHAPER_CALIBRATE on
#             T0's accelerometer), the result shown, Save = SAVE_CONFIG
#   pa        per tool: filament check, PLA/PETG, the released PA sweep
#             (CALIBRATE_PA) + ANALYZE_PA, K shown with the other estimates ->
#             Save (SAVE_TOOL_PA + SAVE_CONFIG) / fine sweep around K / Discard
# Each tool ends with park + SAVE_CONFIG (owner 10/04: save after EACH tool,
# a restart loses pending values).  SAVE_CONFIG restarts Klipper, so the
# progress is kept in [save_variables] (variable kxl_wizard) and the next tool
# is offered once Klipper is back.
#
#   XL_WIZARD                          menu (button: gcode_macro CALIBRATION_WIZARDS)
#   XL_WIZARD NAME=pid [TOOLS=all|0,2]
#   XL_WIZARD_ANSWER CHOICE=..         the dialog buttons
#   XL_WIZARD_CANCEL
#
# Copyright (C) 2026  Richard Crook
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging

NUM_TOOLS = 5
LOADCELL_TEMP = 150.   # XL_LOAD_CELL_REFERENCE default (released PROBE_TEMP)
PID_TEMP = 215.        # owner 10/04: PID_CALIBRATE ... TARGET=215
COLD_MARGIN = 3.       # owner 10/04: tool within 3 C of the coolest other tool
TEMP_REPORTED = 5.     # below: no reading yet (0.0 right after a restart)
COLD_POLL = 5.         # s between cold checks
COLD_SHOW_STEP = 2.    # re-show the waiting dialog when the tool moved this much
FS_POLL = 0.5          # s between filament sensor checks (insert / remove)
RESUME_DELAY = 8.      # s after klippy:ready: tools detected, screens back
# One-tool printer: no other tool to compare with; the stock-firmware bed
# reads room temperature at rest too (printer 10/05: bed 27.0, nozzles
# 27.5-28.7 C).  The bed's average is heater_bed (xl_puppies BedHeater;
# the 'temperature_sensor Modular_Bed' it replaced was dropped 10/14).
ROOM_SENSOR = 'heater_bed'
SAVE_VARIABLE = 'kxl_wizard'
# filament sensor calibration: with only the empty reading known, a sensor
# sees filament once its reading is more than this far from empty - Prusa
# filament_sensor_adc_eval.cpp "only ref nins is calibrated", XL values
# filament_sensor_adc.cpp: extruder_fs_value_span (XLBUDDY) 1000,
# side_fs_value_span 310 (both 12-bit counts, as our sensors read)
TOOL_FS_SPAN = 1000
SIDE_FS_SPAN = 310
# pressure advance: test temperatures (PA_TUNE default 215 = PLA; released
# hint "PETG e.g. PA_TUNE_T1 TEMP=240"), estimates "agree" within PA_AGREE,
# a fine sweep spans K +- PA_FINE_HALF (released example 0.05-0.08), analyser
# polled every PA_POLL s
PA_PRESETS = (('PLA', 215.), ('PETG', 240.))
PA_AGREE = 0.005
PA_FINE_HALF = 0.015
PA_POLL = 2.

WIZARDS = [
    ('loadcell', 'Loadcell reference',
     "Each tool is picked, heated to 150 C (the probing temperature) and its"
     " loadcell zero is recorded with nothing touching the nozzle, then saved."
     " Klipper restarts after each tool and the wizard carries on."),
    ('pid', 'Heater PID',
     "Each tool must start COLD: the wizard waits until it is, picks it, tunes"
     " its heater at 215 C (a few minutes), parks it and saves. Klipper"
     " restarts after each tool and the wizard carries on."),
    ('fsensor', 'Filament sensors',
     "For each tool both sensors are calibrated: the side sensor at the spool"
     " and the tool sensor in the extruder. Empty first, then with filament:"
     " you push it in through the side sensor until the tool sensor sees it,"
     " then pull it back out. Only an unload (a tool that still has"
     " filament) heats. Autoload is off meanwhile. Klipper restarts after"
     " each tool and the wizard carries on."),
    # menu order = the installer's list and docs (stock Prusa's Calibrations
    # menu also has Z Alignment before Tool Offset Calibration)
    ('zalign', 'Z alignment',
     "Aligns the two Z motors (they share one driver): any tool is parked,"
     " the bed is driven up into the top frame at 0.7 A so the side that lags"
     " catches up, then backed off 5 mm. It is loud - that is the bed"
     " stopping against the frame. Nothing may be on the bed. Afterwards the"
     " printer homes all, Z with T0."),
    # stock: Z Alignment, then Dock Position (selftest_snake_config.hpp)
    ('docks', 'Dock positions',
     "Measures where each dock really is (KlipperXL's DOCK_CALIBRATE): you"
     " free the dock, lock the carriage onto its tool by hand - the motors"
     " then hold it - and tighten the dock; the printer backs the tool out"
     " and homes to measure it, then checks a slow park and 3 pick/park"
     " cycles. One dock or all of them. You choose whether to save the"
     " positions at the end."),
    ('offsets', 'Tool offsets (calibration pin)',
     "Every tool is probed on the calibration pin and its offset from T0 is"
     " saved (all tools in one go, about 7-14 minutes). Needs the loadcell"
     " reference done on every tool, the calibration pin and a brass brush."
     " Start with the heatbed sheet ON: Z is homed on it and each nozzle is"
     " heated and brought to the front to be cleaned. Then you swap the sheet"
     " for the pin, the tools are probed at 70 C, and you put the sheet back."
     " Klipper restarts once at the end to save."),
    ('shaper', 'Input shaper',
     "Measures how the printer frame rings and picks the input shaper for X"
     " and Y (Klipper's SHAPER_CALIBRATE, as KlipperXL used it). T0 goes on"
     " the carriage, moves to the bed centre and shakes X, then Y - about 5"
     " minutes, and loud. Nothing on the bed; do not touch the printer while"
     " it runs. You see the result and choose whether to save it."),
    ('pa', 'Pressure advance',
     "Measures each tool's pressure advance with its loadcell (the released"
     " PA tuner): the tool heats, pushes about 850 mm of filament into free"
     " air at Z80 for about 6 minutes, and the recording is analysed. You see"
     " K and choose Save, a finer sweep around it, or Discard. Filament must"
     " be loaded. Klipper restarts after each saved tool and the wizard"
     " carries on."),
]
TITLES = {name: title for name, title, _ in WIZARDS}
INTROS = {name: intro for name, _, intro in WIZARDS}


class XLWizards:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        self.name = None          # running wizard
        self.tools = []
        self.i = 0                # index into tools
        self.expect = {}          # answer CHOICE -> step
        self.data = {}            # the current tool's readings
        self.last_prompt = None
        self.autoload_saved = None
        self.cold_timer = self.reactor.register_timer(self._cold_poll)
        self.pa_timer = self.reactor.register_timer(self._pa_poll)
        self.fs_timer = self.reactor.register_timer(self._fs_poll)
        self.cold_shown = None
        for cmd, desc in (
                ('XL_WIZARD', "Guided calibrations (menu, or NAME= TOOLS=)"),
                ('XL_WIZARD_ANSWER', "Answer the open calibration dialog"),
                ('XL_WIZARD_CANCEL', "Stop the running calibration wizard")):
            self.gcode.register_command(cmd, getattr(self, 'cmd_' + cmd),
                                        desc=desc)
        self.printer.register_event_handler("klippy:connect", self._connect)
        self.printer.register_event_handler("klippy:ready", self._ready)
    def _connect(self):
        if self.printer.lookup_object('save_variables', None) is None:
            raise self.printer.config_error(
                "[xl_wizards] needs [save_variables] (kxlplus/xl_macros.cfg)")
    # ---- dialogs -------------------------------------------------------
    def _title(self):
        if self.name is None:
            return "Calibration wizards"
        if self.i < len(self.tools) and self.name != 'offsets':
            return "%s - T%d (%d of %d)" % (TITLES[self.name], self._tool(),
                                            self.i + 1, len(self.tools))
        return TITLES[self.name]
    def _prompt(self, text, buttons=(), group=(), footer=(), title=None):
        self.last_prompt = (text, buttons, group, footer, title)
        lines = ["prompt_begin %s" % (title or self._title(),)]
        if text:
            lines.append("prompt_text %s" % (" ".join(text.split()),))
        if group:
            lines.append("prompt_button_group_start")
            lines += ["prompt_button %s|%s|%s" % b for b in group]
            lines.append("prompt_button_group_end")
        lines += ["prompt_button %s|%s|%s" % b for b in buttons]
        lines += ["prompt_footer_button %s|%s|%s" % b for b in footer]
        lines.append("prompt_show")
        for line in lines:
            self.gcode.respond_raw("// action:" + line)
    def _close(self):
        self.last_prompt = None
        self.gcode.respond_raw("// action:prompt_end")
    def _ask(self, text, choices):
        """choices: (label, CHOICE, step, style); Cancel is always offered"""
        self.expect = {c: step for _, c, step, _ in choices}
        self._prompt(text, buttons=[
            (label, "XL_WIZARD_ANSWER CHOICE=%s" % (c,), style)
            for label, c, _, style in choices],
            footer=[("Cancel", "XL_WIZARD_CANCEL", "error")])
    def _busy(self, text):
        # nothing to press while a step runs (the X only closes the dialog)
        self.expect = {}
        self._prompt(text)
        self.gcode.respond_info(text)
    # ---- helpers -------------------------------------------------------
    def _run(self, script):
        self.gcode.run_script_from_command(script)
    def _tool(self):
        return self.tools[self.i]
    def _ext(self, tool):
        return 'extruder' if tool == 0 else 'extruder%d' % (tool,)
    def _installed(self):
        tc = self.printer.lookup_object('xl_toolchanger')
        return tc.get_status(self.reactor.monotonic())['installed_tools']
    def _print_state(self):
        stats = self.printer.lookup_object('print_stats', None)
        if stats is None:
            return 'standby'
        return stats.get_status(self.reactor.monotonic())['state']
    def _home_xy(self):
        # XL_PICK and XL_PARK need X/Y homed (the filament load does the same).
        # A tool can already be on the carriage after a restart, so the pick
        # is skipped and nothing homed - the park must not rely on it.
        homed = self.printer.lookup_object('toolhead').get_status(
            self.reactor.monotonic())['homed_axes']
        if 'x' not in homed or 'y' not in homed:
            self._run("G28 X Y")
    def _park(self):
        if self.printer.lookup_object('xl_toolchanger').active_tool >= 0:
            self._home_xy()
            self._run("XL_PARK")
    def _pick(self, tool):
        if self.printer.lookup_object('xl_toolchanger').active_tool == tool:
            return
        self._home_xy()
        self._run("XL_PICK TOOL=%d" % (tool,))
    def _heater_off(self):
        if self.name == 'offsets':
            # every tool is heated in this one; any held fan let go
            for t in self.tools:
                self._run("M104 T%d S0" % (t,))
            self._offsets().release_fans()
            return
        if self.name is not None and self.i < len(self.tools):
            self._run("M104 T%d S0" % (self._tool(),))
    def _autoload_off(self):
        fil = self.printer.lookup_object('xl_filament', None)
        if fil is not None and self.autoload_saved is None:
            self.autoload_saved = fil.autoload_enabled
            fil.autoload_enabled = False
    def _autoload_restore(self):
        fil = self.printer.lookup_object('xl_filament', None)
        if fil is not None and self.autoload_saved is not None:
            fil.autoload_enabled = self.autoload_saved
        self.autoload_saved = None
    # progress across the SAVE_CONFIG restarts
    def _persist(self, value):
        sv = self.printer.lookup_object('save_variables')
        sv.cmd_SAVE_VARIABLE(self.gcode.create_gcode_command(
            "SAVE_VARIABLE", "SAVE_VARIABLE",
            {'VARIABLE': SAVE_VARIABLE, 'VALUE': repr(value)}))
    def _saved(self):
        sv = self.printer.lookup_object('save_variables', None)
        if sv is None:
            return None
        return sv.allVariables.get(SAVE_VARIABLE)
    # ---- flow ----------------------------------------------------------
    def _step(self, step):
        try:
            step()
        except self.printer.command_error as e:
            msg = str(e)
            try:
                self._heater_off()
            except self.printer.command_error:
                pass
            self._ask("Failed: %s" % (msg,),
                      [("Retry", "retry", step, "primary")])
            raise
    def _tool_begin(self):
        getattr(self, '_%s_begin' % (self.name,))()
    def _tool_done(self):
        tool = self._tool()
        if self.i + 1 < len(self.tools):
            after = "then the wizard offers T%d" % (self.tools[self.i + 1],)
        else:
            after = "then the wizard shows it is finished"
        self._busy("T%d done - parking and saving. Klipper restarts, %s."
                   % (tool, after))
        self._park()
        nxt ={'name': self.name, 'tools': list(self.tools), 'i': self.i + 1}
        configfile = self.printer.lookup_object('configfile')
        pending = configfile.get_status(
            self.reactor.monotonic())['save_config_pending']
        if not pending:
            # nothing staged (cannot normally happen): carry on in place
            self.i += 1
            self._offer_next()
            return
        self._persist(nxt)
        try:
            self._run("SAVE_CONFIG")
        except self.printer.command_error:
            self._persist({'name': self.name, 'tools': list(self.tools),
                           'i': self.i})
            raise
    def _offer_next(self, done_tool=None):
        if self.i >= len(self.tools):
            self._finish()
            return
        said = ("T%d saved. " % (done_tool,)) if done_tool is not None else ""
        self._ask("%sNext: T%d." % (said, self._tool()),
                  [("Continue", "go", self._tool_begin, "primary")])
    def _finish(self):
        title = TITLES[self.name]
        tools = ", ".join("T%d" % (t,) for t in self.tools)
        self._autoload_restore()
        self._persist(None)
        self.name, self.tools, self.i, self.expect = None, [], 0, {}
        if tools:
            text = "%s finished and saved for %s." % (title, tools)
        else:
            text = "%s finished and saved." % (title,)   # whole printer
        self._prompt(text, buttons=[("Close", "XL_WIZARD_CANCEL", "primary")],
                     title=title)
        self.gcode.respond_info("%s finished: %s" % (title, tools or "saved"))
    def _end(self):
        if self.name == 'offsets':
            self._offsets().end()
        self.reactor.update_timer(self.cold_timer, self.reactor.NEVER)
        self.reactor.update_timer(self.pa_timer, self.reactor.NEVER)
        self.reactor.update_timer(self.fs_timer, self.reactor.NEVER)
        self._autoload_restore()
        self.name, self.tools, self.i, self.expect = None, [], 0, {}
        self.data = {}
    # ---- loadcell reference ----------------------------------------------
    def _loadcell_begin(self):
        tool = self._tool()
        self._busy("T%d: picking it and heating to %.0f C..."
                   % (tool, LOADCELL_TEMP))
        self._pick(tool)
        self._run("M109 T%d S%.0f" % (tool, LOADCELL_TEMP))
        self._ask("T%d is at %.0f C. Make sure NOTHING touches its nozzle - no"
                  " bed, sheet or purge blob - then Record."
                  % (tool, LOADCELL_TEMP),
                  [("Record", "go", self._loadcell_record, "primary")])
    def _loadcell_record(self):
        tool = self._tool()
        self._busy("T%d: recording the loadcell reference..." % (tool,))
        self._run("XL_LOAD_CELL_REFERENCE TOOL=T%d TEMP=%.0f"
                  % (tool, LOADCELL_TEMP))
        self._run("M104 T%d S0" % (tool,))
        self._tool_done()
    # ---- heater PID ------------------------------------------------------
    def _cold_state(self, tool):
        """(tool temp, room reference) or None while not all reported"""
        now = self.reactor.monotonic()
        temps = {}
        for t in self._installed():
            heater = self.printer.lookup_object(self._ext(t)).get_heater()
            temps[t] = heater.get_status(now)['temperature']
        others = [v for t, v in temps.items() if t != tool]
        if not others:
            room = self.printer.lookup_object(ROOM_SENSOR, None)
            if room is not None:
                others = [room.get_status(now)['temperature']]
        if not others or min(list(temps.values()) + others) <= TEMP_REPORTED:
            return None
        return temps[tool], min(others)
    def _pid_begin(self):
        tool = self._tool()
        state = self._cold_state(tool)
        if state is not None and state[0] - state[1] <= COLD_MARGIN:
            self._pid_tune()
            return
        self._show_cold(state)
        self.expect = {'_cold': self._pid_tune}
        self.reactor.update_timer(self.cold_timer,
                                  self.reactor.monotonic() + COLD_POLL)
    def _show_cold(self, state):
        tool = self._tool()
        if state is None:
            text = "T%d: waiting for the temperatures to report..." % (tool,)
        else:
            text = ("T%d must start cold: it is %.1f C, the room reference is"
                    " %.1f C - it starts at %.1f C or below. Waiting..."
                    % (tool, state[0], state[1], state[1] + COLD_MARGIN))
        self.cold_shown = state
        self._prompt(text, footer=[("Cancel", "XL_WIZARD_CANCEL", "error")])
    def _cold_poll(self, eventtime):
        if self.name != 'pid' or '_cold' not in self.expect:
            return self.reactor.NEVER
        state = self._cold_state(self._tool())
        if state is None or state[0] - state[1] > COLD_MARGIN:
            shown = self.cold_shown
            if (state is None) != (shown is None) or (
                    state is not None
                    and abs(state[0] - shown[0]) >= COLD_SHOW_STEP):
                self._show_cold(state)
            return eventtime + COLD_POLL
        self.reactor.register_callback(lambda e: self._internal('_cold'))
        return self.reactor.NEVER
    def _internal(self, choice):
        # a timer cannot run g-code itself: queue the answer like a button
        try:
            self.gcode.run_script("XL_WIZARD_ANSWER CHOICE=%s" % (choice,))
        except Exception:
            logging.exception("xl_wizards: step %s failed", choice)
    def _pid_tune(self):
        tool = self._tool()
        self._busy("T%d: PID tuning at %.0f C - a few minutes. Do not touch"
                   " the printer." % (tool, PID_TEMP))
        self._pick(tool)
        self._run("PID_CALIBRATE HEATER=%s TARGET=%.0f"
                  % (self._ext(tool), PID_TEMP))
        # the autotune ends with its target still set (210 C, pid_calibrate.py
        # ControlAutoTune): turn the heater off before parking
        self._run("M104 T%d S0" % (tool,))
        self._tool_done()
    # ---- filament sensors ------------------------------------------------
    def _side(self):
        return self.printer.lookup_object('xl_side_sensors')
    def _tfs(self, tool):
        return self.printer.lookup_object('xl_tool_fsensor T%d' % (tool,))
    def _fsensor_begin(self):
        tool = self._tool()
        self._autoload_off()
        self._busy("T%d: picking it..." % (tool,))
        self._pick(tool)
        self._ask("Is there filament in T%d - in its side sensor (at the"
                  " spool) or in the extruder?" % (tool,),
                  [("Yes - unload it", "yes", self._fs_unload, "secondary"),
                   ("No - it is empty", "no", self._fs_empty, "primary")])
    def _fs_unload(self):
        tool = self._tool()
        self._busy("T%d: unloading - heating, ramming, retracting..." % (tool,))
        self._run("UNLOAD_FILAMENT TOOL=%d" % (tool,))
        self._run("M104 T%d S0" % (tool,))
        self._ask("Pull T%d's filament out completely - back out past the side"
                  " sensor." % (tool,),
                  [("Done", "done", self._fs_empty, "primary")])
    def _fs_empty(self):
        tool = self._tool()
        side, tfs = self._side(), self._tfs(tool)
        self._run("CALIBRATE_SIDE_SENSOR TOOL=%d" % (tool,))
        self._run("CALIBRATE_FILAMENT_SENSOR TOOL=%d" % (tool,))
        # step 1 keeps the reading only when the sensor reports a valid value
        side_empty = side._cal_empty.get(tool)
        tool_empty = tfs._cal_empty
        if side_empty is None or tool_empty is None:
            raise self.printer.command_error(
                "T%d: a sensor is not reporting (side raw %s, tool raw %s) -"
                " check its cable" % (tool, side.raw.get(tool), tfs.raw))
        self.data = {'side_empty': side_empty, 'tool_empty': tool_empty}
        self._fs_wait_insert()
    # with only the empty reading known (stock: the state "approximated from
    # the nins reference" while calibrating)
    def _fs_sees(self, tool):
        side, tfs = self._side(), self._tfs(tool)
        s_raw, t_raw = side.raw.get(tool), tfs.raw
        s_ok = (s_raw is not None
                and abs(s_raw - self.data['side_empty']) > SIDE_FS_SPAN)
        t_ok = (t_raw is not None
                and abs(t_raw - self.data['tool_empty']) > TOOL_FS_SPAN)
        return s_ok, t_ok, s_raw, t_raw
    # stock waits on the sensors, not on a button: insertion_wait (both see
    # the filament) and enforce_remove (it reads empty again)
    def _fs_wait(self, key, step, text):
        self._prompt(text, footer=[("Cancel", "XL_WIZARD_CANCEL", "error")])
        self.expect = {key: step}
        self.reactor.update_timer(self.fs_timer,
                                  self.reactor.monotonic() + FS_POLL)
    def _fs_poll(self, eventtime):
        if self.name != 'fsensor':
            return self.reactor.NEVER
        tool = self._tool()
        if '_fsin' in self.expect:
            key = '_fsin'
            s_ok, t_ok, _, _ = self._fs_sees(tool)
            done = s_ok and t_ok
        elif '_fsout' in self.expect:
            key = '_fsout'
            done = (self._side().state.get(tool) == 'no_filament'
                    and self._tfs(tool).state == 'no_filament')
        else:
            return self.reactor.NEVER
        if not done:
            return eventtime + FS_POLL
        self.reactor.register_callback(lambda e: self._internal(key))
        return self.reactor.NEVER
    def _fs_wait_insert(self):
        # stock "Insert the filament through the side filament sensor into the
        # extruder until the tool filament sensor detects the filament"
        tool = self._tool()
        self._fs_wait('_fsin', self._fs_inserted,
                      "Insert the filament through T%d's side sensor into the"
                      " extruder until the tool sensor detects it (empty"
                      " readings: side %d, tool %d)."
                      % (tool, self.data['side_empty'],
                         self.data['tool_empty']))
    def _fs_inserted(self):
        # stock insertion_ok: confirmed while the filament is held in
        tool = self._tool()
        _, _, s_raw, t_raw = self._fs_sees(tool)
        self._ask("Both T%d sensors see the filament (side %d, tool %d). Hold"
                  " it there, then Record." % (tool, s_raw, t_raw),
                  [("Record", "go", self._fs_record, "primary")])
    def _fs_record(self):
        tool = self._tool()
        side, tfs = self._side(), self._tfs(tool)
        s_ok, t_ok, s_raw, t_raw = self._fs_sees(tool)
        if not (s_ok and t_ok):
            # stock: a sensor lost the filament -> back to the insert step
            self._fs_wait_insert()
            return
        before_s, before_t = side.cal.get(tool), tfs.cal
        self._run("CALIBRATE_SIDE_SENSOR_STEP2 TOOL=%d" % (tool,))
        self._run("CALIBRATE_FILAMENT_SENSOR_STEP2 TOOL=%d" % (tool,))
        # a successful step 2 stores a NEW [empty, filament] list
        after_s, after_t = side.cal.get(tool), tfs.cal
        bad = [n for n, a, b in (("side", after_s, before_s),
                                 ("tool", after_t, before_t))
               if a is None or a is b]
        if bad:
            self._ask("T%d %s sensor: the loaded reading is too close to the"
                      " empty one (side %s, tool %s). Pull the filament out"
                      " completely - back out past the side sensor - then"
                      " Done, and T%d starts again."
                      % (tool, " and ".join(bad), s_raw, t_raw, tool),
                      [("Done", "done", self._fs_empty, "primary")])
            return
        # stock "Remove filament to finish": waits until it reads empty
        # (stock checks the tool sensor; both are checked here, with their
        # new calibration)
        self._fs_wait('_fsout', self._tool_done,
                      "T%d calibrated (side %d/%d, tool %d/%d - empty/filament)."
                      " Remove the filament - pull it back out, past the side"
                      " sensor." % (tool, after_s[0], after_s[1], after_t[0],
                                    after_t[1]))
    # ---- tool offsets (calibration pin) ------------------------------------
    # The released steps (xl_tool_offsets.py) in the released order; its
    # WAIT_TIME pauses are the three Continue dialogs.
    def _offsets(self):
        return self.printer.lookup_object('xl_tool_offsets')
    def _offsets_begin(self):
        p = self.data['params']
        self._busy("Turning off heaters...")
        self._offsets().step_begin(p)
        if p['skip_clean']:
            self.gcode.respond_info("Skipping nozzle cleaning (SKIP_CLEAN=1)")
            self._offsets_home()
            return
        self._ask("Install the heatbed SHEET - it is needed for nozzle"
                  " cleaning. Then Continue: every nozzle is heated to %.0f C"
                  " and brought to the front, one at a time for %.0f s, for"
                  " you to clean with a brass brush - the parking plates as"
                  " well." % (p['clean_temp'], p['clean_time']),
                  [("Continue", "go", self._offsets_clean, "primary")])
    def _offsets_clean(self):
        self._offsets().step_clean(self.data['params'], self._busy)
        self._offsets_home()
    def _offsets_home(self):
        p = self.data['params']
        eng = self._offsets()
        eng.step_cool(p)
        eng.step_home_park(p, self._busy)
        self._ask("The carriage is at the back. 1. REMOVE the heatbed sheet."
                  " 2. INSTALL the calibration pin at the bed centre (X%.0f"
                  " Y%.0f). Then Continue: the nozzles go to %.0f C and every"
                  " tool is probed on the pin." % (p['pin_x'], p['pin_y'],
                                                   p['cal_temp']),
                  [("Continue", "go", self._offsets_probe, "primary")])
    def _offsets_probe(self):
        p = self.data['params']
        eng = self._offsets()
        eng.step_wait_temp(p, self._busy)
        eng.step_probe(p, self._busy)
        self._ask("Probing done. 1. REMOVE the calibration pin. 2. INSTALL the"
                  " heatbed sheet. Then Continue %s."
                  % ("to see the result - DRY RUN, nothing is saved"
                     if p['dry_run'] else "to save the offsets"),
                  [("Continue", "go", self._offsets_finish, "primary")])
    def _offsets_finish(self):
        p = self.data['params']
        if not self._offsets().step_finish(p):
            # DRY_RUN: the old offsets are back, nothing written
            title = TITLES[self.name]
            self._end()
            self._persist(None)
            self._prompt("Dry run finished - nothing was saved. The measured"
                         " offsets and MEASURED vs CURRENT are in the console.",
                         buttons=[("Close", "XL_WIZARD_CANCEL", "primary")],
                         title=title)
            return
        self._busy("Saving the tool offsets - Klipper restarts, then the"
                   " wizard shows it is finished.")
        self._persist({'name': self.name, 'tools': list(self.tools),
                       'i': len(self.tools)})
        try:
            self._run("SAVE_CONFIG")
        except self.printer.command_error:
            self._persist(None)
            raise
    # ---- Z alignment ----------------------------------------------------
    def _zalign_begin(self):
        self._busy("Aligning the Z motors - parking any tool, then the bed"
                   " goes up into the top frame and backs off 5 mm...")
        self._run("PRUSA_Z_LEVEL")
        self._ask("Z motors aligned. Z is not homed now (its height was"
                  " counted from the top frame). Home all: X/Y, then Z with T0.",
                  [("Home all", "go", self._zalign_home, "primary")])
    def _zalign_home(self):
        self._busy("Homing all...")
        self._run("G28")
        title = TITLES[self.name]
        self._end()
        self._prompt("Z alignment finished - the Z motors are aligned and the"
                     " printer is homed.",
                     buttons=[("Close", "XL_WIZARD_CANCEL", "primary")],
                     title=title)
        self.gcode.respond_info("%s finished" % (title,))
    # ---- dock positions -------------------------------------------------
    # DOCK_CALIBRATE's steps that move the printer (the dialog says so first)
    DOCKS_MOVING = ('tighten_top', 'install_pins', 'pick_park_verify',
                    'dock_complete')
    def _docks_tc(self):
        return self.printer.lookup_object('xl_toolchanger')
    def _docks_begin(self):
        docks = self.data['params']['docks']
        self._busy("Starting the dock calibration...")
        if len(docks) > 1:
            self._run("DOCK_CALIBRATE")
        else:
            self._run("DOCK_CALIBRATE DOCK=%d" % (docks[0],))
        self._docks_show()
    def _docks_next(self):
        tc = self._docks_tc()
        if tc._cal_state in self.DOCKS_MOVING:
            self._busy("DO NOT TOUCH the printer - it is moving...")
        try:
            self._run("DOCK_CAL_CONTINUE")
        except self.printer.command_error as e:
            self._docks_show(error=str(e))   # it stopped itself: say why
            return
        self._docks_show()
    def _docks_show(self, error=None):
        tc = self._docks_tc()
        text = tc.dock_cal_text()
        if error:
            text = ("%s %s" % (text, error)).strip()
        if tc.dock_cal_active:
            self._ask(text, [("Continue", "next", self._docks_next, "primary")])
            return
        if tc.dock_cal_result == 'complete':
            self._ask("%s Save the dock positions? Klipper restarts to save."
                      % (text,),
                      [("Save", "save", self._docks_save, "primary"),
                       ("Don't save", "skip", self._docks_skip, "secondary")])
            return
        # stopped: the dock that failed is back to its old position
        title = TITLES[self.name]
        if tc._cal_dock_idx > 0:
            text += (" Docks finished before it are staged - SAVE_CONFIG keeps"
                     " them.")
        self._end()
        self._prompt(text or "Dock calibration stopped - see the console.",
                     buttons=[("Close", "XL_WIZARD_CANCEL", "primary")],
                     title=title)
        self.gcode.respond_info("%s stopped" % (title,))
    def _docks_save(self):
        self._busy("Saving the dock positions - Klipper restarts, then the"
                   " wizard shows it is finished.")
        self._persist({'name': self.name, 'tools': [], 'i': 0})
        try:
            self._run("SAVE_CONFIG")
        except self.printer.command_error:
            self._persist(None)
            raise
    def _docks_skip(self):
        title = TITLES[self.name]
        self._end()
        self._prompt("Not saved. The measured positions stay in use until"
                     " Klipper restarts; the saved ones come back then.",
                     buttons=[("Close", "XL_WIZARD_CANCEL", "primary")],
                     title=title)
        self.gcode.respond_info("%s: not saved" % (title,))
    # ---- input shaper ---------------------------------------------------
    def _shaper_begin(self):
        self._busy("Input shaper: homing if needed, T0 on the carriage, then"
                   " shaking X and then Y - about 5 minutes, loud. Do not touch"
                   " the printer.")
        self._run("CALIBRATE_INPUT_SHAPER")
        # SHAPER_CALIBRATE stages its result for SAVE_CONFIG
        status = self.printer.lookup_object('configfile').get_status(
            self.reactor.monotonic())
        found = status.get('save_config_pending_items', {}).get(
            'input_shaper', {})
        try:
            result = "X %s %.1f Hz, Y %s %.1f Hz" % (
                found['shaper_type_x'], float(found['shaper_freq_x']),
                found['shaper_type_y'], float(found['shaper_freq_y']))
        except (KeyError, TypeError, ValueError):
            raise self.printer.command_error(
                "Input shaper: no result was staged - see the console")
        self._ask("Measured: %s. The console lists every shaper Klipper tried"
                  " and the suggested max acceleration. Save it? Klipper"
                  " restarts to save." % (result,),
                  [("Save", "save", self._shaper_save, "primary"),
                   ("Don't save", "skip", self._shaper_skip, "secondary")])
    def _shaper_save(self):
        self._busy("Saving the input shaper - Klipper restarts, then the wizard"
                   " shows it is finished.")
        self._persist({'name': self.name, 'tools': [], 'i': 0})
        try:
            self._run("SAVE_CONFIG")
        except self.printer.command_error:
            self._persist(None)
            raise
    def _shaper_skip(self):
        title = TITLES[self.name]
        self._end()
        self._prompt("Not saved. The measured values stay active until Klipper"
                     " restarts; your saved ones come back then.",
                     buttons=[("Close", "XL_WIZARD_CANCEL", "primary")],
                     title=title)
        self.gcode.respond_info("%s: not saved" % (title,))
    # ---- pressure advance -----------------------------------------------
    def _pa(self):
        return self.printer.lookup_object('xl_pa_tuner')
    def _pa_begin(self):
        tool = self._tool()
        tfs = self.printer.lookup_object('xl_tool_fsensor T%d' % (tool,), None)
        if tfs is not None and tfs.state == 'no_filament':
            self._ask("T%d has no filament - its tool sensor reads empty. Load"
                      " it first (Filament - Load), then Check again." % (tool,),
                      [("Check again", "go", self._pa_begin, "primary")])
            return
        self._ask("T%d: which filament is loaded? The test runs at that"
                  " temperature." % (tool,),
                  [("%s %.0f C" % (n, t), n.lower(),
                    (lambda n=n, t=t: self._pa_warn(n, t)), "primary")
                   for n, t in PA_PRESETS])
    def _pa_warn(self, fil, temp):
        tool = self._tool()
        self.data = {'fil': fil, 'temp': temp}
        self._ask("Nothing on the bed. T%d heats to %.0f C and pushes about"
                  " 850 mm of %s out into free air at Z80 - about 6 minutes -"
                  " then the recording is analysed. Clear the filament away"
                  " afterwards." % (tool, temp, fil),
                  [("Start", "go", self._pa_sweep, "primary")])
    def _pa_sweep(self, start_k=None, end_k=None):
        tool = self._tool()
        d = self.data
        fine = start_k is not None
        self._busy("T%d: PA %ssweep at %.0f C - about %d minutes. Do not touch"
                   " the printer." % (tool, "fine " if fine else "", d['temp'],
                                      7 if fine else 6))
        if fine:
            self._run("CALIBRATE_PA_FINE TOOL=%d TEMP=%.0f START_K=%.4f"
                      " END_K=%.4f" % (tool, d['temp'], start_k, end_k))
        else:
            self._run("CALIBRATE_PA TOOL=%d TEMP=%.0f" % (tool, d['temp']))
        self._run("ANALYZE_PA TOOL=%d SAVE=0 FILAMENT=%s" % (tool, d['fil']))
        self._busy("T%d: analysing the recording..." % (tool,))
        # the analyser runs in the background (xl_pa_tuner): wait for it
        self.expect = {'_pa': self._pa_result}
        self.reactor.update_timer(self.pa_timer,
                                  self.reactor.monotonic() + PA_POLL)
    def _pa_poll(self, eventtime):
        if self.name != 'pa' or '_pa' not in self.expect:
            return self.reactor.NEVER
        if self._pa().an_proc is not None:
            return eventtime + PA_POLL
        self.reactor.register_callback(lambda e: self._internal('_pa'))
        return self.reactor.NEVER
    def _pa_result(self):
        tool = self._tool()
        last = self._pa().last
        if not last or last.get('tool') != tool or last.get('k') is None:
            why = (last or {}).get('error') or "no K came out (see the console)"
            self._ask("T%d: %s." % (tool, why),
                      [("Try again", "retry", self._pa_sweep, "primary"),
                       ("Skip T%d" % (tool,), "skip", self._pa_skip,
                        "secondary")])
            return
        k = float(last['k'])
        res = last.get('res') or {}
        self.data['k'] = k
        others = []
        seg = res.get('bd_k_from_segments')
        if isinstance(seg, (int, float)):
            others.append(("per-segment", float(seg)))
        fit = res.get('integral_fit')
        if (isinstance(fit, dict) and isinstance(fit.get('k_opt'), (int, float))
                and isinstance(fit.get('r_squared'), float)
                and fit['r_squared'] >= 0.9):
            others.append(("integral", float(fit['k_opt'])))
        if others:
            spread = max(abs(v - k) for _, v in others)
            names = ", ".join("%s %.4f" % o for o in others)
            if spread <= PA_AGREE:
                agree = "%s - they agree (within %.4f)" % (names, spread)
            else:
                agree = ("%s - they differ by up to %.4f, a fine sweep may"
                         " help" % (names, spread))
        else:
            agree = "none strong enough to compare"
        lo, hi = max(0., k - PA_FINE_HALF), k + PA_FINE_HALF
        self._ask("T%d %s: K = %.4f. Other estimates: %s. The console lists"
                  " every fit." % (tool, self.data['fil'], k, agree),
                  [("Save", "save", self._pa_save, "primary"),
                   ("Fine sweep %.3f-%.3f" % (lo, hi), "fine",
                    (lambda: self._pa_sweep(lo, hi)), "secondary"),
                   ("Discard", "discard", self._pa_skip, "secondary")])
    def _pa_save(self):
        tool = self._tool()
        self._run("SAVE_TOOL_PA TOOL=%d K=%.4f FILAMENT=%s"
                  % (tool, self.data['k'], self.data['fil']))
        self._tool_done()
    def _pa_skip(self):
        tool = self._tool()
        self._run("M104 T%d S0" % (tool,))
        self._park()
        self.i += 1
        if self.i < len(self.tools):
            self._offer_next()
            return
        title = TITLES[self.name]
        self._end()
        self._persist(None)
        self._prompt("%s finished - T%d not saved (tools saved before it stay"
                     " saved)." % (title, tool),
                     buttons=[("Close", "XL_WIZARD_CANCEL", "primary")],
                     title=title)
        self.gcode.respond_info("%s finished, T%d not saved" % (title, tool))
    # ---- commands --------------------------------------------------------
    def _menu(self):
        self.expect = {}
        # owner 10/13: the order stays stock's, with this warning
        self._prompt("Choose a calibration. Pick or park problems? Run Dock"
                     " positions first - every other calibration picks and"
                     " parks tools.", buttons=[
            (title, "XL_WIZARD NAME=%s" % (name,), "primary")
            for name, title, _ in WIZARDS],
            footer=[("Close", "XL_WIZARD_CANCEL", "secondary")],
            title="Calibration wizards")
    def _choose_tools(self, name):
        installed = self._installed()
        self._prompt(INTROS[name], group=[
            ("T%d" % (t,), "XL_WIZARD NAME=%s TOOLS=%d" % (name, t),
             "secondary") for t in installed],
            buttons=[("All tools", "XL_WIZARD NAME=%s TOOLS=all" % (name,),
                      "primary")],
            footer=[("Back", "XL_WIZARD", "secondary"),
                    ("Cancel", "XL_WIZARD_CANCEL", "error")],
            title=TITLES[name])
    def _parse_tools(self, gcmd, arg):
        installed = self._installed()
        if arg.strip().lower() == 'all':
            return list(installed)
        try:
            tools = [int(t) for t in arg.replace('T', '').replace('t', '')
                     .split(',') if t.strip()]
        except ValueError:
            raise gcmd.error("TOOLS must be 'all' or tool numbers, e.g. 0,2")
        for t in tools:
            if t not in installed:
                raise gcmd.error("T%d is not installed on this printer" % (t,))
        if not tools:
            raise gcmd.error("No tools given")
        return sorted(set(tools))
    def cmd_XL_WIZARD(self, gcmd):
        name = gcmd.get('NAME', None)
        if name is None:
            if self.name is not None and self.last_prompt is not None:
                self._prompt(*self.last_prompt)       # show it again
            elif self.name is None and self._saved():
                self._resume()
            else:
                self._menu()
            return
        name = name.strip().lower()
        if name not in TITLES:
            raise gcmd.error("Unknown wizard '%s' (%s)"
                             % (name, ", ".join(TITLES)))
        arg = gcmd.get('TOOLS', None)
        if name == 'offsets':
            # all tools in one session against T0: no tool choice
            p = self._offsets().params(gcmd, self._installed())
            self.start(name, p['tools'], params=p)
            return
        if name in ('zalign', 'shaper'):
            self.start(name, [])         # the whole printer, no tools
            return
        if name == 'docks':
            # one session: DOCK_CALIBRATE takes one dock or all of them
            if arg is None:
                self._check_free()
                self._choose_tools(name)
                return
            docks = self._parse_tools(gcmd, arg)
            if 1 < len(docks) < len(self._installed()):
                raise gcmd.error("Dock positions: one dock or all of them")
            self.start(name, [], params={'docks': docks})
            return
        if arg is None:
            self._check_free()
            self._choose_tools(name)
            return
        self.start(name, self._parse_tools(gcmd, arg))
    def _check_free(self):
        if self.name is not None:
            raise self.printer.command_error(
                "%s is running - finish or cancel it first"
                % (TITLES[self.name],))
        state = self._print_state()
        if state in ('printing', 'paused'):
            raise self.printer.command_error(
                "Calibration refused: a print is %s" % (state,))
    def start(self, name, tools, params=None):
        """Start a wizard on these tools (XL_WIZARD, CALIBRATE_TOOL_OFFSETS)"""
        self._check_free()
        self.tools = list(tools)
        self.name, self.i, self.data = name, 0, {}
        intro = INTROS[name]
        if params is not None:
            self.data['params'] = params
            if params.get('dry_run'):
                intro = "DRY RUN - nothing is saved. " + intro
        self._ask(intro, [("Start", "go", self._tool_begin, "primary")])
    def cmd_XL_WIZARD_ANSWER(self, gcmd):
        choice = gcmd.get('CHOICE').strip().lower()
        step = self.expect.get(choice)
        if step is None:
            raise gcmd.error("XL_WIZARD: '%s' does not answer the current step"
                             % (choice,))
        self.expect = {}
        self._step(step)
    def cmd_XL_WIZARD_CANCEL(self, gcmd):
        if self.name is None and not self._saved():
            self._close()
            return
        title = TITLES.get(self.name) if self.name else "Calibration wizard"
        # tool offsets save once, at the end: a stop saves nothing
        if self.name == 'offsets':
            saved = "Nothing was saved - the tool offsets are as before."
        elif self.name == 'zalign':
            saved = "Home all (G28) before printing."
        elif self.name == 'docks':
            if self._docks_tc().dock_cal_active:
                self._run("DOCK_CAL_ABORT")
            saved = ("The dock being calibrated is back to its old position;"
                     " docks finished before it stay staged for SAVE_CONFIG.")
        elif self.name == 'shaper':
            saved = ("Nothing was saved - a measured result stays active only"
                     " until Klipper restarts.")
        elif self.name == 'pa':
            saved = "Tools already saved stay saved; the one running was not."
        else:
            saved = "Tools already done are saved."
        self._heater_off()
        self._end()
        self._persist(None)
        self._close()
        gcmd.respond_info("%s stopped. %s" % (title, saved))
    # ---- after a SAVE_CONFIG restart -------------------------------------
    def _ready(self):
        if self._saved():
            self.reactor.register_timer(
                self._resume_timer, self.reactor.monotonic() + RESUME_DELAY)
    def _resume_timer(self, eventtime):
        if self.name is None and self._print_state() not in ('printing',
                                                             'paused'):
            try:
                self._resume()
            except Exception:
                logging.exception("xl_wizards: resume failed")
        return self.reactor.NEVER
    def _resume(self):
        st = self._saved()
        try:
            name, tools, i = st['name'], list(st['tools']), int(st['i'])
            if name not in TITLES:
                raise ValueError(name)
        except (TypeError, KeyError, ValueError):
            logging.warning("xl_wizards: ignoring saved state %r", st)
            self._persist(None)
            return
        self.name, self.tools, self.i, self.data = name, tools, i, {}
        done = tools[i - 1] if 0 < i <= len(tools) else None
        self._offer_next(done)
    def get_status(self, eventtime):
        return {'running': self.name,
                'tool': (self._tool() if self.name is not None
                         and self.i < len(self.tools) else None),
                'tools': list(self.tools)}

def load_config(config):
    return XLWizards(config)
