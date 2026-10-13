# KlipperXL-Plus - the two buttons on each tool: up = retract, down = extrude
#
# Behaviour = released KlipperXL (puppy_bootloader.py _check_tool_buttons,
# f236cde): the PICKED tool only, 170 C or hotter (Prusa EXTRUDE_MINTEMP),
# 2 mm moves at 5 mm/s for as long as the button is held, up wins if both
# are pressed.  (Stock Prusa 6.4.0 reads the buttons - Dwarf.cpp
# is_button_up/down_pressed - but nothing acts on them.)
#
# The release polled the buttons over the RS485 bus and, while one was held,
# stayed in a tight loop inside its bus poll, so the other Dwarfs went
# unpolled (owner 10/07: holding a button "would crash the bus").  Here each
# Dwarf watches its own pins with Klipper's buttons support and sends a
# message only when a button changes - PA15 up, PC10 down, active low with a
# pull-up (hwio_pindef_XL_dwarf.h l.86-90, 134-135; ModbusControl.cpp
# l.328-329).  The moves go through the g-code queue like any other, one 2 mm
# move at a time (M400 after each), so letting go stops within 2 mm.
# Not while a print is printing (a paused print is fine - e.g. a runout).
#
#   [xl_tool_buttons T0]
#   up_pin: ^!dwarf_t0:PA15
#   down_pin: ^!dwarf_t0:PC10
#
# Copyright (C) 2026  Richard Crook
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging

MIN_TEMP = 170.     # released: "heat above 170C to use buttons"
CHUNK = 2.0         # mm per move (released)
SPEED = 5.0         # mm/s (released)
BTN_UP, BTN_DOWN = 1, 2


class XLToolButtons:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        self.name = config.get_name().split()[-1]          # 'T0'
        self.tool = int(self.name[1:])
        buttons = self.printer.load_object(config, 'buttons')
        buttons.register_buttons([config.get('up_pin'),
                                  config.get('down_pin')], self._callback)
        self.state = 0              # BTN_UP | BTN_DOWN, as pressed now
        self._running = False
        self._told = False          # one message per press
    def _callback(self, eventtime, state):
        self.state = state
        if not state:
            self._told = False
            return
        if not self._running:
            self._running = True
            self.reactor.register_callback(self._hold)
    def _tell(self, msg):
        if not self._told:
            self._told = True
            self.gcode.respond_info(msg)
    def _may_move(self):
        tc = self.printer.lookup_object('xl_toolchanger', None)
        if tc is None or tc.active_tool != self.tool:
            return False            # only the picked tool (released)
        stats = self.printer.lookup_object('print_stats', None)
        if stats is not None and stats.get_status(
                self.reactor.monotonic())['state'] == 'printing':
            self._tell("%s buttons do nothing while a print is running"
                       % (self.name,))
            return False
        ext = 'extruder' if self.tool == 0 else 'extruder%d' % (self.tool,)
        temp = self.printer.lookup_object(ext).get_heater().get_temp(
            self.reactor.monotonic())[0]
        if temp < MIN_TEMP:
            self._tell("%s too cold (%.0fC) - heat above %.0fC to use the"
                       " buttons" % (self.name, temp, MIN_TEMP))
            return False
        return True
    def _hold(self, eventtime):
        try:
            while self.state and self._may_move():
                move = -CHUNK if self.state & BTN_UP else CHUNK
                self.gcode.run_script(
                    "SAVE_GCODE_STATE NAME=_xl_tool_button\nM83\n"
                    "G1 E%.1f F%d\nM400\n"
                    "RESTORE_GCODE_STATE NAME=_xl_tool_button"
                    % (move, SPEED * 60.))
        except Exception:
            logging.exception("xl_tool_buttons %s", self.name)
        finally:
            self._running = False
    def get_status(self, eventtime):
        return {'up': bool(self.state & BTN_UP),
                'down': bool(self.state & BTN_DOWN)}

def load_config_prefix(config):
    return XLToolButtons(config)
