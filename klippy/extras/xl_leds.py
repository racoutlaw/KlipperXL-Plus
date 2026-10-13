# KlipperXL-Plus - Prusa XL side + front LED strips (one data pin, one selector)
#
# Both strips hang off the XLBuddy's PG14 (shared with the ILI9488 display's
# SPI6 MOSI).  A hardware selector picks the target (Prusa
# led_lcd_cs_selector.cpp, FACTS.md section 9):
#   display CS PD11 HIGH + PE9 HIGH -> side strip
#   display CS PD11 HIGH + PE9 LOW  -> front strip
# The display is not driven by Klipper; CS stays HIGH (deselected).
#
# No custom MCU code: each strip is Klipper's own config_neopixel on PG14,
# PE9 / PD11 are Klipper's own config_digital_out.  What plain [neopixel] +
# [output_pin] cannot give is ordering - the selector and the data travel
# on different queues, so an LED effect on one strip could land on the
# other.  Here every transfer is "update_digital_out PE9, neopixel_update,
# neopixel_send" on ONE command queue (the MCU runs one queue in order and
# neopixel_send bit-bangs inside the command) under one reactor mutex
# (so another strip's transfer can't slip in between Klipper's retries).
#
# Config (owner's printer, tested 2026-10-03):
#   [xl_leds side_leds]
#   strip: side              # side | front
#   chain_count: 2
#   color_order: RGB         # index 1 = colour; index 2 RED = white LED,
#                            # index 2 GREEN = enclosure fan (not a light)
#   [xl_leds front_leds]
#   strip: front
#   chain_count: 3
#   color_order: GRB
# SET_LED LED=side_leds / front_leds and [led_effect] (xl_leds:side_leds)
# work as for any Klipper LED.
#
# Copyright (C) 2026  Richard Crook
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging
from . import led

DATA_PIN = 'PG14'      # PFB hwio_pindef_XL.h (SPI6 MOSI / LED data)
SELECT_PIN = 'PE9'     # PFB hwio_pindef_XL.h:161 sideLed_LcdSelector_pin_e9
LCD_CS_PIN = 'PD11'    # PFB hwio_pindef_XL.h:114 display CS
STRIPS = {'side': 1, 'front': 0}   # PE9 level that selects the strip

# Same values as klippy/extras/neopixel.py
BACKGROUND_PRIORITY_CLOCK = 0x7fffffff00000000
BIT_MAX_TIME = .000004
RESET_MIN_TIME = .000050


class XLLedBus:
    """The shared pin, selector and command queue (one per printer)."""
    def __init__(self, config):
        self.printer = config.get_printer()
        self.mutex = self.printer.get_reactor().mutex()
        ppins = self.printer.lookup_object('pins')
        self.mcu = ppins.lookup_pin(SELECT_PIN)['chip']
        ppins.lookup_pin(LCD_CS_PIN)          # reserve: nothing else may use it
        self.select_oid = self.mcu.create_oid()
        self.cs_oid = self.mcu.create_oid()
        self.select_level = STRIPS['side']
        self.strips = []
        self.cmd_queue = None
        self.select_cmd = None
        self.mcu.register_config_callback(self._build_config)
    def _build_config(self):
        for oid, pin, value in ((self.cs_oid, LCD_CS_PIN, 1),
                                (self.select_oid, SELECT_PIN,
                                 self.select_level)):
            self.mcu.add_config_cmd(
                "config_digital_out oid=%d pin=%s value=%d default_value=%d"
                " max_duration=0" % (oid, pin, value, value))
        self.cmd_queue = self.mcu.alloc_command_queue()
        self.select_cmd = self.mcu.lookup_command(
            "update_digital_out oid=%c value=%c", cq=self.cmd_queue)
    def transmit(self, strip, updates, print_time):
        if self.printer.get_start_args().get('debugoutput') is not None:
            return
        minclock = 0
        if print_time is not None:
            minclock = self.mcu.print_time_to_clock(print_time)
        with self.mutex:
            if self.select_level != strip.select_level:
                self.select_cmd.send([self.select_oid, strip.select_level],
                                     reqclock=BACKGROUND_PRIORITY_CLOCK)
                self.select_level = strip.select_level
            for pos, data in updates:
                strip.update_cmd.send([strip.oid, pos, data],
                                      reqclock=BACKGROUND_PRIORITY_CLOCK)
            for i in range(8):
                params = strip.send_cmd.send(
                    [strip.oid], minclock=minclock,
                    reqclock=BACKGROUND_PRIORITY_CLOCK)
                if params['success']:
                    return
            logging.info("xl_leds: %s update did not succeed", strip.name)


class XLLedStrip:
    def __init__(self, config, bus):
        self.printer = config.get_printer()
        self.name = config.get_name().split()[-1]
        self.bus = bus
        self.select_level = config.getchoice('strip', STRIPS)
        ppins = self.printer.lookup_object('pins')
        ppins.lookup_pin(DATA_PIN, share_type='xl_leds')
        self.mcu = bus.mcu
        self.oid = self.mcu.create_oid()
        self.update_cmd = self.send_cmd = None
        # Colour map (same rules as neopixel.py)
        chain_count = config.getint('chain_count', 1, minval=1, maxval=32)
        color_order = config.getlist('color_order', ['GRB'])
        if len(color_order) == 1:
            color_order = [color_order[0]] * chain_count
        if len(color_order) != chain_count:
            raise config.error("color_order does not match chain_count")
        color_indexes = []
        for lidx, co in enumerate(color_order):
            if sorted(co) not in (sorted("RGB"), sorted("RGBW")):
                raise config.error("Invalid color_order '%s'" % (co,))
            color_indexes.extend([(lidx, "RGBW".index(c)) for c in co])
        self.color_map = list(enumerate(color_indexes))
        self.led_helper = led.LEDHelper(config, self.update_leds, chain_count)
        self.color_data = bytearray(len(self.color_map))
        self._fill(self.led_helper.get_status()['color_data'])
        self.old_color_data = bytearray([d ^ 1 for d in self.color_data])
        self.mcu.register_config_callback(self._build_config)
        self.printer.register_event_handler("klippy:connect", self._send)
    def _build_config(self):
        bmt = self.mcu.seconds_to_clock(BIT_MAX_TIME)
        rmt = self.mcu.seconds_to_clock(RESET_MIN_TIME)
        self.mcu.add_config_cmd(
            "config_neopixel oid=%d pin=%s data_size=%d bit_max_ticks=%d"
            " reset_min_ticks=%d" % (self.oid, DATA_PIN, len(self.color_data),
                                     bmt, rmt))
        cq = self.bus.cmd_queue
        self.update_cmd = self.mcu.lookup_command(
            "neopixel_update oid=%c pos=%hu data=%*s", cq=cq)
        self.send_cmd = self.mcu.lookup_query_command(
            "neopixel_send oid=%c", "neopixel_result oid=%c success=%c",
            oid=self.oid, cq=cq)
    def _fill(self, led_state):
        for cdidx, (lidx, cidx) in self.color_map:
            self.color_data[cdidx] = int(led_state[lidx][cidx] * 255. + .5)
    def _send(self, print_time=None):
        new, old = self.color_data, self.old_color_data
        if new == old:
            return
        # Changed byte runs (same batching as neopixel.py)
        diffs = [[i, 1] for i, (n, o) in enumerate(zip(new, old)) if n != o]
        for i in range(len(diffs) - 2, -1, -1):
            pos, count = diffs[i]
            nextpos, nextcount = diffs[i+1]
            if pos + 5 >= nextpos and nextcount < 16:
                diffs[i][1] = nextcount + (nextpos - pos)
                del diffs[i+1]
        updates = [(pos, bytes(new[pos:pos+count])) for pos, count in diffs]
        old[:] = new
        self.bus.transmit(self, updates, print_time)
    def update_leds(self, led_state, print_time):
        self._fill(led_state)
        self._send(print_time)
    def get_status(self, eventtime=None):
        return self.led_helper.get_status(eventtime)


def load_config_prefix(config):
    printer = config.get_printer()
    bus = printer.lookup_object('xl_leds_bus', None)
    if bus is None:
        bus = XLLedBus(config)
        printer.add_object('xl_leds_bus', bus)
    strip = XLLedStrip(config, bus)
    bus.strips.append(strip)
    return strip
