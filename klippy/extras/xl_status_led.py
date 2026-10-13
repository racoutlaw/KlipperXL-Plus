# KlipperXL-Plus - a Dwarf's status LED (one WS2812-type LED on PB6)
#
# Driven with Klipper's own MCU neopixel commands (src/neopixel.c, unchanged
# Dwarf firmware), set up exactly as klippy/extras/neopixel.py does, except
# for two timing limits:
#   BIT_MAX_TIME 40 us (Klipper: 4 us) - the MCU fails a frame when the gap
#     from one bit to the next passes this.  Every Dwarf's UART hears all
#     RS485 bus traffic, and the interrupts it takes between bits made most
#     updates fail at 4 us (printer 10/07: 5 of 7, "Neopixel update did not
#     succeed").  The LED itself only ends a frame after a long low: Prusa's
#     Dwarf firmware uses 280 us (src/puppy/dwarf/led.cpp time_reset_us), so
#     a 40 us gap inside a frame is still one frame.
#   RESET_MIN_TIME 300 us (Klipper: 50 us) - the low before a frame, so a
#     retry after a broken frame starts a new frame instead of finishing the
#     old one (Prusa: 280 us+).
# (Tried first: Dwarf patch 0011, the whole frame with interrupts off as
# stock sends it - every '0' bit then came out too long and read as '1',
# a full-white LED that took no colour.  Reverted; the stock MCU code is
# kept.)
# Colour order GRB (led.cpp set_rgb: green<<16 | red<<8 | blue).  Nothing is
# sent at connect - xl_toolchanger sets the LEDs one tool at a time.
#
#   [xl_status_led T0]
#   pin: dwarf_t0:PB6
#
# Copyright (C) 2026  Richard Crook
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging

BACKGROUND_PRIORITY_CLOCK = 0x7fffffff00000000
BIT_MAX_TIME = .000040
RESET_MIN_TIME = .000300
SEND_TRIES = 8                  # as klippy/extras/neopixel.py


class XLStatusLED:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.name = config.get_name().split()[-1]
        ppins = self.printer.lookup_object('pins')
        pin_params = ppins.lookup_pin(config.get('pin'))
        self.mcu = pin_params['chip']
        self.oid = self.mcu.create_oid()
        self.pin = pin_params['pin']
        self.mcu.register_config_callback(self._build_config)
        self.update_cmd = self.send_cmd = None
        self.color = None            # (r, g, b) 0..1 last sent OK
        self.failures = 0
        self.last_tries = 0
        gcode = self.printer.lookup_object('gcode')
        gcode.register_mux_command(
            "XL_STATUS_LED", "TOOL", self.name, self.cmd_XL_STATUS_LED,
            desc="Set a tool's status LED: TOOL=Tn RED= GREEN= BLUE= (0-1)")
    def _build_config(self):
        bmt = self.mcu.seconds_to_clock(BIT_MAX_TIME)
        rmt = self.mcu.seconds_to_clock(RESET_MIN_TIME)
        self.mcu.add_config_cmd("config_neopixel oid=%d pin=%s data_size=3"
                                " bit_max_ticks=%d reset_min_ticks=%d"
                                % (self.oid, self.pin, bmt, rmt))
        cmd_queue = self.mcu.alloc_command_queue()
        self.update_cmd = self.mcu.lookup_command(
            "neopixel_update oid=%c pos=%hu data=%*s", cq=cmd_queue)
        self.send_cmd = self.mcu.lookup_query_command(
            "neopixel_send oid=%c", "neopixel_result oid=%c success=%c",
            oid=self.oid, cq=cmd_queue)
    def set_rgb(self, r, g, b):
        """Send one colour (0..1).  Waits for the MCU's answer, so call it
        from a g-code command, a callback or a timer.  True when the LED
        took it."""
        def byte(v):
            return max(0, min(255, int(v * 255. + .5)))
        data = bytes([byte(g), byte(r), byte(b)])
        self.update_cmd.send([self.oid, 0, data],
                             reqclock=BACKGROUND_PRIORITY_CLOCK)
        for i in range(SEND_TRIES):
            params = self.send_cmd.send([self.oid],
                                        reqclock=BACKGROUND_PRIORITY_CLOCK)
            if params['success']:
                if i:
                    logging.info("xl_status_led %s: sent on try %d",
                                 self.name, i + 1)
                self.color = (r, g, b)
                self.last_tries = i + 1
                return True
        self.last_tries = SEND_TRIES
        self.failures += 1
        logging.info("xl_status_led %s: update did not succeed (%d tries)",
                     self.name, SEND_TRIES)
        return False
    def cmd_XL_STATUS_LED(self, gcmd):
        r = gcmd.get_float('RED', 0., minval=0., maxval=1.)
        g = gcmd.get_float('GREEN', 0., minval=0., maxval=1.)
        b = gcmd.get_float('BLUE', 0., minval=0., maxval=1.)
        ok = self.set_rgb(r, g, b)
        gcmd.respond_info("%s status LED R%.3f G%.3f B%.3f: %s (%d %s)"
                          % (self.name, r, g, b, "OK" if ok else "FAILED",
                             self.last_tries,
                             "try" if self.last_tries == 1 else "tries"))
    def get_status(self, eventtime):
        return {'color': self.color, 'failures': self.failures,
                'last_tries': self.last_tries}

def load_config_prefix(config):
    return XLStatusLED(config)
