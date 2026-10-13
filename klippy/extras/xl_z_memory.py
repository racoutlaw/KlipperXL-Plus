# KlipperXL-Plus - remember the bed height across a Klipper restart
#
# Owner item A (10/06-10/07): homing X/Y with Z unknown (released
# homing_override: SET_KINEMATIC_POSITION Z=0, then G1 Z10) drops the bed
# 10 mm, and the calibration wizards restart Klipper after every tool, so the
# bed walked down 10 mm per tool.  When Klipper is restarted (RESTART,
# FIRMWARE_RESTART, SAVE_CONFIG) with Z known, the height is saved; the next
# X/Y home uses it instead of faking Z=0 (xl_base.cfg [homing_override]).
#
# Safety:
# - the saved height is used ONCE: it is cleared at the next start, so a
#   power cut or crash later can never bring back a stale value;
# - only a requested restart saves (gcode:request_restart - Klipper does not
#   send it after a shutdown or emergency stop, where the commanded position
#   is not where the axis stopped); a service stop or power off saves
#   nothing, and the X/Y home drops the bed 10 mm as before;
# - Klipper turns the motors off and forgets the homing at a restart BEFORE
#   this module's handler runs (stepper_enable registers first), so whether
#   Z was known is taken from a poll of homed_axes made just before; the
#   leadscrews hold the bed with the motors off.
# A bed turned by hand while Klipper restarts is the one case it cannot see.
#
#   [xl_z_memory]
#   (needs [save_variables]; the height is kept as kxl_bed_z)
#
# Copyright (C) 2026  Richard Crook
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging

VARIABLE = 'kxl_bed_z'
POLL_TIME = 0.5


class XLZMemory:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.restored_z = None       # from the last restart, until Z is known
        self._z_known = False
        self._timer = self.reactor.register_timer(self._poll)
        self.printer.register_event_handler("klippy:ready", self._ready)
        self.printer.register_event_handler("gcode:request_restart",
                                            self._request_restart)
    # save_variables writes the file through an executor and waits for it
    # (reactor pause) - allowed in a g-code command or a callback, NOT in a
    # klippy:ready handler ("reactor pause disabled": printer 10/07, startup
    # shut down).  A failed save is logged, never fatal.
    def _save(self, value):
        sv = self.printer.lookup_object('save_variables', None)
        if sv is None:
            return
        gcode = self.printer.lookup_object('gcode')
        try:
            sv.cmd_SAVE_VARIABLE(gcode.create_gcode_command(
                "SAVE_VARIABLE", "SAVE_VARIABLE",
                {"VARIABLE": VARIABLE, "VALUE": repr(value)}))
        except Exception:
            logging.exception("xl_z_memory: saving %s=%r failed",
                              VARIABLE, value)
    def _ready(self):
        sv = self.printer.lookup_object('save_variables', None)
        z = sv.allVariables.get(VARIABLE) if sv is not None else None
        if z is not None:
            # used once - cleared right after startup, in a callback
            self.reactor.register_callback(lambda e: self._save(None))
            toolhead = self.printer.lookup_object('toolhead')
            zmax = toolhead.get_status(
                self.reactor.monotonic())['axis_maximum'][2]
            if isinstance(z, (int, float)) and 0. <= z <= zmax:
                self.restored_z = float(z)
                logging.info("xl_z_memory: bed height %.3f kept from the"
                             " last restart", self.restored_z)
            else:
                logging.warning("xl_z_memory: saved bed height %r ignored", z)
        self.reactor.update_timer(self._timer, self.reactor.NOW)
    def _poll(self, eventtime):
        toolhead = self.printer.lookup_object('toolhead')
        self._z_known = 'z' in toolhead.get_status(eventtime)['homed_axes']
        if self._z_known:
            self.restored_z = None           # known now, nothing to restore
        return eventtime + POLL_TIME
    def _request_restart(self, print_time):
        if not self._z_known:
            return
        z = self.printer.lookup_object('toolhead').get_position()[2]
        self._save(round(z, 4))
        logging.info("xl_z_memory: bed height %.3f saved for the restart", z)
    def get_status(self, eventtime):
        return {'restored_z': self.restored_z}

def load_config(config):
    return XLZMemory(config)
