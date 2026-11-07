# KlipperXL-Plus - the XL's own colour screen (ILI9488, 480x320) with Prusa's
# screens.  Step 1 (owner 10/28): the home screen live (clock, footer
# temperatures, bed grid, filament) and the knob moving the focus between
# the 6 tiles.  Step 2 (owner 11/06): the print screen, which opens by itself
# when a print starts (Tune / Pause / Stop as stock), and the Tune menu.  Step
# 3 (owner 11/06): the Filament tile - load and unload, the tool picker, the
# filament list and the progress page, which also come up for a load started
# from Mainsail or the side sensor - then Print (the file list), Preheat, Control
# (home, pick / park, move) and Info.  A button sends the same G-code Mainsail
# does (PAUSE, RESUME, CANCEL_PRINT, M220, FILAMENT_LOAD, G28 ...), so the screen
# and Mainsail cannot behave differently.  Settings has no page, on purpose
# (owner 10/28: setup and calibration stay in Mainsail).
#
# The pixels are made by the XLBuddy (firmware/xlbuddy/xlplus_lcd.c): it
# holds Prusa's icons and font and draws on hardware SPI6, so this module
# only sends small commands - "icon 3 at x,y rows 0-2", "text 'PR' at x,y",
# "fill" (owner 10/28: pushing the pixels from here painted each tile
# visibly and made the side LEDs stutter).  It also hands PG14 back to the
# LED strips after every command.
# Flow control: one command is at most MAX_BYTES (~0.3 ms of drawing) and
# after IN_FLIGHT of them this module waits for the XLBuddy's answer to a
# ping before sending more, so the XLBuddy never draws for more than ~1.3 ms
# at a stretch and its RS485 bridge to the tools gets its turn.  Printer
# 10/28: without it the start-up page (hundreds of ~1 ms commands, run back
# to back from the XLBuddy's input buffer) delayed the tools' traffic -
# "MCU 'dwarf_t0' shutdown: Timer too close" on all five.
# Without that firmware (no XL_LCD_ICONS constant) this module stays idle.  A
# firmware from before step 2 (no XL_LCD_VER constant) keeps the home screen
# only: its fonts and icons do not cover the other pages.
#
# Look: Prusa-Firmware-Buddy 6.10.3 screen_home.cpp (tiles), window_header.cpp
# (header), footer_*.cpp (footer), screen_printing.cpp + ScreenPrintingModel
# .cpp (print screen), i_window_menu*.cpp + WindowMenuSpin.cpp (menus),
# window_msgbox.cpp + i_radio_button.cpp (questions), screen_menu_filament.hpp,
# dialog_tool_select.cpp, screen_preheat.cpp, screen_filament_change.cpp
# (filament), screen_filebrowser.cpp + window_file_list.cpp (file list),
# screen_menu_control.hpp, screen_tool_pick_park.cpp, screen_menu_move.cpp,
# menu_vars.cpp (Control), screen_menu_info.hpp (Info), GuiDefaults.hpp, color.hpp.
# Knob (hwio_pindef_XL.h:117-119): PD13 / PD12, button PG3, pulled up.
#
# Config:
#   [xl_display]
#
# Copyright (C) 2026  Richard Crook
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging, os, socket, time
from . import xl_filament, xl_puppies

W, H = 480, 320
FW, FH = 9, 16                   # LiberationMono 9x16
SMALL, NORMAL, BIG = 0, 1, 2     # Prusa's Font::small, normal, big
FONT_SIZE = ((9, 16), (11, 19), (13, 22))
BIG_W, BIG_H = 30, 53            # Font::large, the print screen's digits
MAX_BYTES = 864                  # per command: 2 glyphs, 3 icon rows (the
                                 # firmware takes up to 3072)
IN_FLIGHT = 4                    # commands sent before waiting for a ping
TICK = .5                        # Prusa's footer blink period

# colours 0xRRGGBB (color.hpp)
BLACK, WHITE = 0x000000, 0xFFFFFF
BRAND = 0xF8651B                 # COLOR_BRAND
GRAY = 0x808080
DARK_GRAY = 0x5B5B5B             # COLOR_DARK_GRAY
SILVER = 0xC0C0C0                # COLOR_SILVER
AZURE = 0x129DFF                 # COLOR_AZURE

HEADER_H = 32                    # GuiDefaults::HeaderHeight
BODY_H = H - 23 - HEADER_H       # BodyHeight, the footer takes 23
MESSAGE_TIME = 15.               # s an M117 that does not change stays up

TITLE = 'PRUSA XL KlipperXL-Plus'
TILES = ['print', 'preheat', 'spool', 'calibrate', 'settings', 'info']
LABELS = ['Print', 'Preheat', 'Filament', 'Control', 'Settings', 'Info']
FOOTER = (4, 300, 472, 16)       # footer_positioning.hpp line rect
CLOCK = (421, 13, 45, 16)        # window_header.cpp, 24 h clock
NOZZLE_COLD, BED_COLD = 45, 40   # footer_items_nozzle_bed.hpp:62,79
HEAT_DIFF = 2                    # footer_items_heaters.hpp:64
MENU_DEMO = 'menus'              # XL_DISPLAY_DEMO STATE=menus or filament
DEMO_STEP = 2.                   # s a step lasts in that demo
GCODE_EXTS = ('.gcode', '.g', '.gco')    # virtual_sdcard VALID_GCODE_EXTS
MAX_FILES = 500                  # rows in one folder of the file list
FILE_TAIL = 128 * 1024           # bytes from the end of a file holding what the
                                 # slicer wrote about its filaments
# filament_presets.cpp heatbed_temperature; M1700 without E heats the nozzles
# to nozzle_preheat_temperature, 170, whatever the row says
PREHEAT_BED = {'PLA': 60, 'PETG': 85, 'ASA': 100, 'PC': 100, 'PVB': 75,
               'ABS': 100, 'HIPS': 100, 'PP': 100, 'FLEX': 50, 'PA': 100}
PREHEAT_NOZZLE = 170
MOVE_SPEED = {'X': 3000, 'Y': 3000, 'Z': 480, 'E': 240}  # MANUAL_FEEDRATE
Y_PRINT_MAX = 361.               # menu_vars.cpp: the bed area only, "to prevent
                                 # crashes of the tool at toolchange area"


def split(widths, x, w):
    """Rect16::horizontalSplit with a zero-width item at each end (the
    footer's centring, footer_line.cpp:122-129): x of every real item."""
    widths = [0] + widths + [0]
    xs, wsum = [], sum(widths)
    for n in range(len(widths), 0, -1):
        xs.append(x)
        if n > 1:
            first = widths[len(widths) - n]
            d = (w - wsum) // (n - 1) + first
            x, w, wsum = x + d, w - d, wsum - first
    return xs[1:-1]


def corner_rows(h, r):
    """draw_rounded_rect (display_ex.cpp:395-478): for each row of a rounded
    rectangle, how many pixels at each end are background."""
    rows = []
    for row in range(h):
        if row < r:
            d = r - row - 1
        elif row >= h - r:
            d = row - (h - r)
        else:
            rows.append(0)
            continue
        rows.append(r - sum(1 for col in range(r)
                            if col * col + d * d < r * r))
    return rows


def format_duration(seconds, display_seconds=False):
    """format_duration (print_time helper): 1d 2h, 3h  4m, 5m  6s, 7s, 8m."""
    s = int(max(seconds, 0))
    days, rest = divmod(s, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return '%dd %dh' % (days, hours)
    if hours:
        return '%dh %2dm' % (hours, minutes)
    if display_seconds:
        return '%dm %2ds' % (minutes, secs) if minutes else '%ds' % (secs,)
    return '%dm' % (minutes,)


def format_end_time(seconds, now):
    """print_timestamp_string_to_buffer (print_time_module.cpp): when the
    print will end, in 24 h like the header clock."""
    end = time.localtime(now + seconds)
    today, tomorrow = time.localtime(now), time.localtime(now + 86400)
    if end[:3] == today[:3]:
        return time.strftime('Today at %H:%M', end)
    if end[:3] == tomorrow[:3]:
        return time.strftime('%a at %H:%M', end)
    return time.strftime('%m-%d at %H:%M', end)


def wrap(text, width):
    """Whole words, at most width characters a line."""
    lines, line = [], ''
    for word in text.split():
        while len(word) > width:
            if line:
                lines.append(line)
                line = ''
            lines.append(word[:width])
            word = word[width:]
        if line and len(line) + 1 + len(word) > width:
            lines.append(line)
            line = word
        else:
            line = '%s %s' % (line, word) if line else word
    if line:
        lines.append(line)
    return lines


class Page:
    footer = True
    def __init__(self, d):
        self.d = d
    def draw(self, eventtime):
        pass
    def tick(self, eventtime):
        pass
    def knob(self, step):
        pass
    def press(self):
        pass


# Home screen (screen_home.cpp, window_header.cpp, footer_*.cpp).  Knob: one
# tile per click, no wrap, stops at the ends (knob_event.cpp).  Clicks faster
# than the drawing only move the target; the new white tile is drawn first,
# then the old one goes back to grey.
class HomePage(Page):
    def __init__(self, d):
        Page.__init__(self, d)
        self.focus = self.shown_focus = 0
    def _draw_tile(self, k, focused):
        r, c = divmod(k, 3)
        self.d._icon('%s_80x80%s' % (TILES[k],
                                     '_focused' if focused else ''),
                     80 + 120 * c, 53 + 117 * r)
    def draw(self, eventtime):
        d = self.d
        d._fill(0, 0, W, H)
        d._icon('home_shape_16x16', 14, 12)
        if d.demo is not None:
            d._text('DEMO - nothing moves', 34, 13, BRAND)
        else:
            d._text(TITLE, 34, 13)
        d._icon('lan_16x16', 401, 12)
        d._draw_clock()
        for k in range(6):
            r, c = divmod(k, 3)
            self._draw_tile(k, k == self.focus)
            d._text(LABELS[k],
                    70 + 120 * c + (99 - FW * len(LABELS[k])) // 2,
                    138 + 117 * r + 3)
        self.shown_focus = self.focus
        d.footer = None
    def knob(self, step):
        self.focus = min(max(self.focus + step, 0), len(TILES) - 1)
        if self.focus != self.shown_focus:
            old, self.shown_focus = self.shown_focus, self.focus
            self._draw_tile(self.shown_focus, True)
            self._draw_tile(old, False)
    def press(self):
        d = self.d
        page = TILE_PAGES.get(LABELS[self.focus]) if d.lcd_ver >= 2 else None
        if page is None:
            logging.info("xl_display: knob pressed on %s (no page)",
                         LABELS[self.focus])
            return
        d.open_page(page(d))


# Print screen states (screen_printing.hpp printing_state_t)
PRINTING, SKIPPABLE, PAUSING, PAUSED = 'printing', 'skip', 'pausing', 'paused'
RESUMING, ABORTING, STOPPED, PRINTED = 'resuming', 'aborting', 'stopped', 'done'
HEADER_TEXT = {PAUSING: 'PAUSING ...', PAUSED: 'PAUSED',
               ABORTING: 'ABORTING ...', STOPPED: 'STOPPED',
               PRINTED: 'FINISHED'}       # set_pause_icon_and_label; else
CAPTION = 'PRINTING ...'                  # screen_printing.hpp caption


def button_set(state):
    """set_tune/pause/stop_icon_and_label: (icon, label, enabled) of the three
    buttons in each state.  BtnRes::Settings is the Tune icon."""
    tune = ('settings', 'Tune', state in (PRINTING, SKIPPABLE, PAUSED))
    middle = {PRINTING: ('pause', 'Pause', True),
              SKIPPABLE: ('resume', 'Skip', True),
              PAUSING: ('pause', 'Pausing...', False),
              PAUSED: ('resume', 'Resume', True),
              RESUMING: ('resume', 'Resuming...', False),
              ABORTING: ('pause', 'Pause', False),
              STOPPED: ('reprint', 'Reprint', True),
              PRINTED: ('reprint', 'Reprint', True)}[state]
    if state in (STOPPED, PRINTED):
        right = ('home', 'Home', True)
    else:
        right = ('stop', 'Stop', state not in (PAUSING, RESUMING, ABORTING))
    return [tune, middle, right]


# Geometry: screen_printing.cpp:143-194, ScreenPrintingModel.cpp:11-18,84-90,
# screen_printing_end_result.cpp:126-130
class PrintPage(Page):
    BAR = (30, 65, 420, 16)
    def __init__(self, d):
        Page.__init__(self, d)
        self.focus = 0                       # Tune, as stock: no click stops
        self.state = None                    # anything by accident
        self.rotate = 0
        self.rotate_at = 0.
        self.reset()
    def reset(self):
        self.shown_state = self.shown_header = None
        self.shown_buttons = [None] * 3
        self.shown_name = self.shown_percent = None
        self.shown_bar = None
        self.shown_info = None
    def draw(self, eventtime):
        self.d._clear_body()
        self.reset()
        self.tick(eventtime)
    # Which of the four time fields hold something right now
    def _time_fields(self, eventtime):
        d = self.d
        left = d.print_time_left(eventtime)
        speed = d.gcode_move.get_status(eventtime)['speed_factor']
        unsure = '?' if abs(speed - 1.) > .001 else ''
        fields = []
        if left is not None:
            fields.append(('Remaining time', format_duration(left) + unsure))
            fields.append(('Print will end',
                           format_end_time(left, time.time()) + unsure))
        if d.time_to_change is not None:
            fields.append(('Next change in',
                           format_duration(d.time_to_change) + unsure))
        fields.append(('Printing time',
                       format_duration(d.print_duration(eventtime), True)))
        return fields
    def _info(self, eventtime):
        d, state = self.d, self.state
        if state in (STOPPED, PRINTED):
            return ('time', 'Printing time',
                    format_duration(d.print_duration(eventtime), True), 0, 0)
        message = d.status_message(eventtime)
        if message:
            return ('message', message)
        fields = self._time_fields(eventtime)
        if eventtime - self.rotate_at > 4.:  # rotation_time_s
            self.rotate += 1
            self.rotate_at = eventtime
        i = self.rotate % len(fields)
        return ('time', fields[i][0], fields[i][1], len(fields), i)
    def _draw_info(self, info):
        d, old = self.d, self.shown_info
        if old is not None and old[0] == 'time' == info[0]:
            # only what changed, the value every second at most
            if old[1] != info[1]:
                d._fill(30, 104, 150, 20)
                d._text(info[1], 30, 108, SILVER)
            if old[2] != info[2]:
                d._fill(30, 124, 200, 23)
                d._text(info[2], 30, 126, font=NORMAL)
            if old[3:] != info[3:]:
                self._draw_circles(*info[3:])
            return
        d._fill(30, 104, 250, 70)
        if info[0] == 'message':
            # message_popup: Rect16(30, 104, 250, 70), padding top 2
            for n, line in enumerate(wrap(info[1], 250 // 11)[:3]):
                d._text(line, 30, 106 + n * 19, font=NORMAL)
            return
        d._text(info[1], 30, 108, SILVER)
        d._text(info[2], 30, 126, font=NORMAL)
        self._draw_circles(*info[3:])
    def _draw_circles(self, count, index):
        # rotating_circles_rect (30, 148, 35, 5), one circle mode
        d = self.d
        d._fill(30, 148, 35, 5)
        if not count:
            return
        delim = (35 - count * 5) // count
        x = 30 + delim // 2
        for i in range(count):
            d._rrect(x, 148, 5, 5, 4, WHITE if i == index else GRAY)
            x += 5 + delim
    def _draw_percent(self, percent):
        d = self.d
        text = '%d%%' % (percent,)
        if self.shown_percent is None or len(text) != len(self.shown_percent):
            d._fill(300, 100, 135, 54)
        x = 435 - BIG_W * len(text)
        for i, ch in enumerate(text):
            if (self.shown_percent is None
                    or len(text) != len(self.shown_percent)
                    or self.shown_percent[i] != ch):
                d._icon('big_' + ch, x + i * BIG_W, 100)
        self.shown_percent = text
    def _draw_bar(self, percent):
        d = self.d
        x, y, w, h = self.BAR
        px = percent * w // 100
        old = self.shown_bar
        if old is None:
            d._fill(x, y, px, h, BRAND)
            d._fill(x + px, y, w - px, h, GRAY)
        elif px > old:
            d._fill(x + old, y, px - old, h, BRAND)
        elif px < old:
            d._fill(x + px, y, old - px, h, GRAY)
        self.shown_bar = px
    def _draw_buttons(self):
        d = self.d
        buttons = button_set(self.state)
        if not buttons[self.focus][2]:
            # DisableButton: the focus moves right to one that is enabled
            for i in (1, 2):
                if buttons[(self.focus + i) % 3][2]:
                    self.focus = (self.focus + i) % 3
                    break
        for i, (icon, label, enabled) in enumerate(buttons):
            focused = i == self.focus and enabled
            variant = ('_focused' if focused
                       else '' if enabled else '_disabled')
            shown = (icon, label, variant)
            old = self.shown_buttons[i]
            if old == shown:
                continue
            x = 90 + 110 * i
            if old is None or old[:1] + old[2:] != (icon, variant):
                d._icon('%s_80x80%s' % (icon, variant), x, 185)
            if old is None or old[1] != label:
                # GetButtonLabelRect: centred in x - 15, 270, 110, 17
                d._fill(x - 15, 270, 110, 17)
                d._text(label, x - 15 + (110 - FW * len(label)) // 2, 270)
            self.shown_buttons[i] = shown
    def tick(self, eventtime):
        d = self.d
        state = d.print_state(eventtime)
        if state is None:
            return
        self.state = state
        header = HEADER_TEXT.get(state, CAPTION)
        if header != self.shown_header:
            d._draw_header('print_16x16', header)
            self.shown_header = header
        name = d.print_name(eventtime)
        if name != self.shown_name:
            # w_filename (30, 38, 420, 24) in Font::big
            d._fill(30, 38, 420, 24)
            d._text(name, 30, 38, font=BIG, max_w=420)
            self.shown_name = name
        percent = d.print_percent(eventtime)
        self._draw_bar(percent)
        self._draw_percent(percent)
        self._draw_buttons()
        info = self._info(eventtime)
        if info != self.shown_info:
            self._draw_info(info)
            self.shown_info = info
    def knob(self, step):
        if self.state is None:
            return
        buttons = button_set(self.state)
        direction = 1 if step > 0 else -1
        for _ in range(abs(step)):
            # no wrap; a disabled button is passed over
            i = self.focus + direction
            while 0 <= i <= 2 and not buttons[i][2]:
                i += direction
            if not 0 <= i <= 2:
                break
            self.focus = i
        self._draw_buttons()
    def press(self):
        d, state = self.d, self.state
        if state is None:
            return
        if not button_set(state)[self.focus][2]:
            return
        if self.focus == 0:
            d.open_page(TunePage(d))
        elif self.focus == 1:
            if state == PRINTING:
                d.print_action('pause', 'PAUSE')
            elif state == SKIPPABLE:
                d.absorb_request('skip')
            elif state == PAUSED:
                d.print_action('resume', 'RESUME')
            else:                            # STOPPED, PRINTED
                d.reprint(self.d.reactor.monotonic())
        elif state in (STOPPED, PRINTED):
            d.go_home()
        else:
            d.open_page(QuestionPage(
                d, 'Are you sure to stop this printing?',
                [('Yes', self._stop), ('No', None)], 1))
    def _stop(self):
        d = self.d
        d.absorb_request('cancel')
        d.print_action('stop', 'CANCEL_PRINT')


# A question (window_msgbox.cpp MsgBoxWarning, i_radio_button.cpp).  buttons:
# (text, what to run on it or None), default = the button focused at first.
class QuestionPage(Page):
    footer = False
    BUTTONS = (6, 282, 468, 32)              # GuiDefaults::GetButtonRect
    def __init__(self, d, text, buttons, default=0):
        Page.__init__(self, d)
        self.text = text
        self.buttons = buttons
        self.focus = default
    def _rects(self):
        # Rect16::HorizontalSplit: the unused space shared out evenly
        x, y, w, h = self.BUTTONS
        count = len(self.buttons)
        usable = w - 6 * (count - 1)
        widths = [13 * len(b[0]) for b in self.buttons]
        spare = (usable - min(sum(widths), usable)) // count
        rects, used = [], 0
        for tw in widths:
            rects.append([x, y, tw + spare, h])
            x += tw + spare + 6
            used += tw + spare + 6
        rects[-1][2] += w - (used - 6)
        return rects
    def _draw_buttons(self):
        d = self.d
        rects = self._rects()
        for i, (x, y, w, h) in enumerate(rects):
            selected = i == self.focus
            back = BRAND if selected else GRAY
            d._rrect(x, y, w, h, 8, back)
            text = self.buttons[i][0]
            d._text(text, x + (w - 13 * len(text)) // 2, y + (h - 22) // 2,
                    BLACK if selected else WHITE, back, BIG)
    def draw(self, eventtime):
        d = self.d
        d._clear_body(keep_header=True)      # DialogFrameRect: under the header
        # MessageIconRect (70, 90), MessageTextRect (133, 90, 300, ..)
        d._icon('warning_48x48', 70, 90)
        for n, line in enumerate(wrap(self.text, 300 // 11)):
            d._text(line, 133, 90 + n * 19, font=NORMAL)
        self._draw_buttons()
    def knob(self, step):
        focus = min(max(self.focus + step, 0), len(self.buttons) - 1)
        if focus != self.focus:
            self.focus = focus
            self._draw_buttons()
    def press(self):
        # what lies under is drawn once, and not at all if the answer opens
        # another page
        d = self.d
        run = self.buttons[self.focus][1]
        d.drop_page()
        under = d.page
        if run is not None:
            run()
        if d.page is under:
            d._draw_page(d.reactor.monotonic())


# A menu (i_window_menu.cpp, i_window_menu_item.cpp, window_menu_adv.hpp)
ROW_H = 39                                   # font 19 + padding 10 + 10
ROW_PITCH = ROW_H + 1                        # the line between two rows
MENU_X, MENU_W = 5, 462                      # MenuPadding, scrollbar 8
BAR_X, BAR_W = 472, 8


class Item:
    def __init__(self, label, icon=None, run=None, enabled=None, info=None,
                 unit=None):
        self.label = label
        self.icon = icon
        self.run = run
        self.enabled = enabled
        self.info = info                     # WiInfo: the text on the right
        self.unit = unit
    def is_enabled(self, eventtime):
        return self.enabled is None or self.enabled(eventtime)
    def value(self, eventtime):
        return self.info(eventtime) if self.info is not None else None


# WiSpin: a click starts editing (the value turns orange), the knob changes
# it, a second click sends it.  'live' sends each step at once, as stock's
# Live Adjust Z; off = what 0 reads as.
class Spin(Item):
    def __init__(self, label, read, send, lo, hi, unit, step=1, digits=0,
                 off=False, live=False, enabled=None):
        Item.__init__(self, label, enabled=enabled)
        self.read, self.send = read, send
        self.lo, self.hi, self.unit = lo, hi, unit
        self.step, self.digits, self.off, self.live = step, digits, off, live
        self.editing = None
    def text(self, v):
        if self.off and v == 0:
            return 'Off'
        return '%.*f' % (self.digits, v)
    def value(self, eventtime):
        v = self.editing if self.editing is not None else self.read(eventtime)
        return self.text(v)


# A row that opens another menu: the arrow on the right
class SubMenu(Item):
    pass


class MenuPage(Page):
    def __init__(self, d, title, icon, items, footer=True):
        Page.__init__(self, d)
        self.title, self.icon, self.items = title, icon, items
        self.footer = footer
        height = BODY_H if footer else H - HEADER_H
        self.slots = max(height // ROW_PITCH, 1)
        self.height = height
        self.focus = self.top = 0
        self.shown = {}                      # item index -> (value, enabled)
        # What each slot on the screen holds: (background, label, icon, value
        # width, has the arrow).  Text paints its own background, so a row on
        # the same background only clears what the old row leaves over: a
        # scroll is six rows of text, not six rows of background (printer
        # 11/06: the full repaint showed at the bottom of Tune).
        self.painted = {}
    def _editing(self):
        item = self.items[self.focus]
        return isinstance(item, Spin) and item.editing is not None
    def _row_y(self, i):
        return HEADER_H + (i - self.top) * ROW_PITCH
    def _draw_scrollbar(self):
        # MenuScrollbar::unconditionalDraw: above, the bar, below
        d = self.d
        if len(self.items) <= self.slots:
            return
        bar = self.slots * self.height // len(self.items)
        off = (self.top * (self.height - bar)
               // (len(self.items) - self.slots))
        d._fill(BAR_X, HEADER_H, BAR_W, off)
        d._fill(BAR_X, HEADER_H + off, BAR_W, bar, SILVER)
        d._fill(BAR_X, HEADER_H + off + bar, BAR_W, self.height - off - bar)
    def _ext_width(self, item, value):
        if value is None:
            return 16 if isinstance(item, SubMenu) else 0  # expand_icon_width
        return 9 * (len(value) + len(item.unit or '')) + 6
    def _draw_value(self, i, eventtime):
        d, item = self.d, self.items[i]
        focused = i == self.focus
        enabled = item.is_enabled(eventtime)
        back = (WHITE if enabled else SILVER) if focused else BLACK
        text = BLACK if focused else (WHITE if enabled else SILVER)
        value = item.value(eventtime)
        y = self._row_y(i)
        right = MENU_X + 5 + MENU_W - 10
        # WiSpin::printExtension: small font, padding top 12, the value right
        # aligned then its unit, the unit grey, half a space before % or mm
        unit = '' if value is None or value == 'Off' else (item.unit or '')
        gap = 6 if unit and unit[0] != '\xb0' else 0
        width = 0 if value is None else 9 * (len(value) + len(unit)) + gap
        slot = i - self.top
        old = self.painted[slot]
        if old[3] > width:
            d._fill(right - old[3], y + 12, old[3] - width, FH, back)
        self.painted[slot] = old[:3] + (width,) + old[4:]
        if value is None:
            return
        color = BRAND if (isinstance(item, Spin)
                          and item.editing is not None) else text
        d._text(value, right - width, y + 12, color, back)
        if gap:
            d._fill(right - 9 * len(unit) - gap, y + 12, gap, FH, back)
        if unit:
            d._text(unit, right - 9 * len(unit), y + 12,
                    DARK_GRAY if focused else SILVER, back)
    def _draw_row(self, i, eventtime):
        # IWindowMenuItem::Print
        d, item = self.d, self.items[i]
        focused = i == self.focus
        enabled = item.is_enabled(eventtime)
        back = (WHITE if enabled else SILVER) if focused else BLACK
        text = BLACK if focused else (WHITE if enabled else SILVER)
        y = self._row_y(i)
        x = MENU_X + 5                       # MenuItemCornerRadius
        slot = i - self.top
        old = self.painted[slot]
        same = old[0] == back
        if not same:
            d._rrect(MENU_X, y, MENU_W, ROW_H, 5, back)
        value = item.value(eventtime)
        room = MENU_W - 10 - 16 - 12 - self._ext_width(item, value)
        label = item.label[:room // 11]
        arrow = isinstance(item, SubMenu)
        arrow_x = x + MENU_W - 10 - 13
        if same and old[2] and old[2] != item.icon:
            d._fill(x, y + 11, 16, 16, back)
        if item.icon and not (same and old[2] == item.icon):
            # printIcon: centred in icon_width 16
            icon_w = d.icons.get(item.icon, (0, 16, 16))[1]
            d._icon(item.icon, x + (16 - icon_w) // 2, y + 11, swap=focused)
        label_x = x + 16 + 6
        if same and len(old[1]) > len(label):
            d._fill(label_x + 11 * len(label), y + 10,
                    11 * (len(old[1]) - len(label)), 19, back)
        d._text(label, label_x, y + 10, text, back, NORMAL)
        if same and old[4] and not arrow:
            d._fill(arrow_x, y + 11, 10, 16, back)
        self.painted[slot] = (back, label, item.icon,
                              old[3] if same else 0, arrow)
        self._draw_value(i, eventtime)
        if arrow:
            d._icon('arrow_right_10x16', arrow_x, y + 11, swap=focused)
        self.shown[i] = (value, enabled)
    def _draw_rows(self, eventtime):
        for i in range(self.top, min(self.top + self.slots, len(self.items))):
            self._draw_row(i, eventtime)
        self._draw_scrollbar()
    def draw(self, eventtime):
        d = self.d
        d._clear_body()
        d._draw_header(self.icon, self.title)
        self.shown = {}
        self.painted = {slot: (BLACK, '', None, 0, False)
                        for slot in range(self.slots)}
        last = min(self.slots, len(self.items))
        for slot in range(last - 1):
            # the line between rows: Left + 41 .. Left + Width - 37
            d._fill(MENU_X + 41, HEADER_H + slot * ROW_PITCH + ROW_H,
                    MENU_W - 41 - 37, 1, DARK_GRAY)
        self._draw_rows(eventtime)
    def tick(self, eventtime):
        # a value that changed by itself (a temperature set from Mainsail, a
        # new fan speed from the G-code); what is being edited stays put
        for i in range(self.top, min(self.top + self.slots, len(self.items))):
            item = self.items[i]
            value = item.value(eventtime)
            enabled = item.is_enabled(eventtime)
            old = self.shown.get(i)
            if old is None or old[1] != enabled:
                self._draw_row(i, eventtime)
            elif old[0] != value:
                self._draw_value(i, eventtime)
                self.shown[i] = (value, enabled)
    def knob(self, step):
        item = self.items[self.focus]
        eventtime = self.d.reactor.monotonic()
        if self._editing():
            v = min(max(item.editing + step * item.step, item.lo), item.hi)
            v = round(v, item.digits + 3)
            if v != item.editing:
                change = v - item.editing
                item.editing = v
                if item.live:
                    item.send(change)
            self._draw_value(self.focus, eventtime)
            self.shown[self.focus] = (item.value(eventtime),
                                      item.is_enabled(eventtime))
            return
        old = self.focus
        self.focus = min(max(old + step, 0), len(self.items) - 1)
        if self.focus == old:
            return
        top = self.top
        if self.focus < top:
            top = self.focus
        elif self.focus >= top + self.slots:
            top = self.focus - self.slots + 1
        if top != self.top:
            self.top = top
            self.shown = {}
            self._draw_rows(eventtime)
            return
        self._draw_row(self.focus, eventtime)
        self._draw_row(old, eventtime)
    def press(self):
        item = self.items[self.focus]
        eventtime = self.d.reactor.monotonic()
        if not item.is_enabled(eventtime):
            return
        if isinstance(item, Spin):
            if item.editing is None:
                item.editing = item.read(eventtime)
            else:
                v, item.editing = item.editing, None
                if not item.live and v != item.read(eventtime):
                    item.send(v)
            self._draw_row(self.focus, eventtime)
        elif item.run is not None:
            item.run()


def return_item(d):
    # MI_RETURN: the first row of every menu
    return Item('Return', 'folder_up_16x16', d.close_page)


# screen_menu_filament.hpp: load and unload.  Stock's Change, Purge, the All
# rows and the filament management stay out for now; the Mainsail buttons
# these two send are the ones that exist today.
class FilamentPage(MenuPage):
    def __init__(self, d):
        items = [return_item(d),
                 SubMenu('Load Filament', run=lambda: self._choose(self._load)),
                 SubMenu('Unload Filament',
                         run=lambda: self._choose(d.filament_unload))]
        MenuPage.__init__(self, d, 'FILAMENT', 'spool_white_16x16', items)
    def _choose(self, then):
        d = self.d
        tools = d.tools()
        if len(tools) == 1:                  # select_tool_dialog: nothing to ask
            then(tools[0])
        else:
            d.open_page(ToolPage(d, then))
    def _load(self, tool):
        # MI_LOAD::click: the same question, No first
        d = self.d
        if d.tool_filament(tool)[1]:
            d.open_page(QuestionPage(
                d, 'Filament appears to be already loaded, are you sure you'
                ' want to load it anyway?',
                [('Yes', lambda: d.filament_load(tool)), ('No', None)], 1))
        else:
            d.filament_load(tool)


# dialog_tool_select.cpp: a row per tool, the arrow on the one on the carriage,
# the whole screen and an empty header.  A tool that is neither on the carriage
# nor in its dock cannot be picked (xl_toolchanger would refuse it): its row is
# greyed out.
class ToolPage(MenuPage):
    def __init__(self, d, then, all_tools=False):
        self.then = then
        active = d._tool()
        items = [return_item(d)]
        if all_tools:                        # then(None)
            items.append(Item('All Tools', None, lambda: self._pick(None)))
        for t in d.tools():
            items.append(Item('Tool %d' % (t + 1,),
                              'arrow_right_10x16' if t == active else None,
                              lambda t=t: self._pick(t),
                              lambda e, t=t: d.tool_ready(t),
                              lambda e, t=t: d.tool_details(t)))
        MenuPage.__init__(self, d, '', None, items, footer=False)
    def _pick(self, tool):
        self.d.drop_page()
        self.then(tool)


# screen_preheat.cpp: the filament list (xl_filament asks once the gears have
# the filament, or before an unload of a tool whose type is not known).
# Return = Mainsail's Cancel.
class FilamentTypePage(MenuPage):
    def __init__(self, d, ask):
        self.ask = ask
        items = [Item('Return', 'folder_up_16x16',
                      lambda: d.filament_answer(ask, None))]
        for name, temp in xl_filament.FILAMENT_TEMPS:
            items.append(Item(name, None,
                              lambda n=name: d.filament_answer(ask, n),
                              info=lambda e, t=temp: '%d' % (t,),
                              unit='\xb0C'))
        title = ('Preheating for load' if ask[1] == 'load'
                 else 'Preheating for unload')
        MenuPage.__init__(self, d, title, None, items, footer=False)


# What each step of xl_filament's load / unload says
# (screen_filament_change.cpp), in the order they come
FILAMENT_STEPS = {'tool': 'Changing tool', 'park': 'Parking',
                  'push': 'Push the filament into the extruder',
                  'gears': 'Inserting', 'heat': 'Waiting for temperature',
                  'nozzle': 'Loading to nozzle', 'purge': 'Purging',
                  'retract': 'Auto-retracting filament', 'ram': 'Ramming',
                  'unload': 'Unloading'}
STEP_ORDER = {'load': ('tool', 'park', 'push', 'gears', 'heat', 'nozzle',
                       'purge', 'retract'),
              'unload': ('tool', 'park', 'heat', 'ram', 'unload'),
              'purge': ('park', 'heat', 'purge')}
MODE_TITLE = {'load': 'Loading filament', 'unload': 'Unloading filament',
              'purge': 'Purging filament'}


# screen_filament_change.cpp FrameProgress, the big layout: title, rounded bar,
# percent, what it is doing.  It follows xl_filament, whoever started the load.
# No Stop button: the load has no way to stop in the middle, in Mainsail either.
class ProgressPage(Page):
    BAR = (24, 100, 432, 16)
    def __init__(self, d, mode='load', tool=None):
        Page.__init__(self, d)
        self.mode, self.tool = mode, tool
        self.percent = 0
        self.shown = {}
    def draw(self, eventtime):
        self.d._clear_body()
        self.shown = {}
        self.tick(eventtime)
    def _line(self, key, text, y, font):
        # centred, redrawn only when it changed
        if self.shown.get(key) == text:
            return
        fw, fh = FONT_SIZE[font]
        self.d._fill(0, y, W, fh)
        self.d._text(text, (W - fw * len(text)) // 2, y, font=font)
        self.shown[key] = text
    def _draw_bar(self):
        d = self.d
        x, y, w, h = self.BAR
        d._rrect(x, y, w, h, 4, DARK_GRAY)
        px = self.percent * w // 100
        if px:
            d._rrect(x, y, max(px, 8), h, 4, BRAND, right=DARK_GRAY)
    def tick(self, eventtime):
        source = self.d.filament_source()
        step = source.screen_step() if source is not None else None
        label = 'Finishing buffered gcodes'  # txt_init: not started yet
        if step is not None:
            self.mode, self.tool, name = step
            label = FILAMENT_STEPS[name]
            order = STEP_ORDER[self.mode]
            self.percent = order.index(name) * 100 // len(order)
        self._line('title', MODE_TITLE[self.mode], 74, BIG)
        if self.shown.get('percent') != self.percent:
            self._draw_bar()
            self.shown['percent'] = self.percent
        self._line('number', '%d%%' % (self.percent,), 142, BIG)
        lines = wrap(label, 52)[:2] + ['', '']
        self._line('label0', lines[0], 182, SMALL)
        self._line('label1', lines[1], 200, SMALL)
        self._line('tool', '' if self.tool is None
                   else 'Tool %d' % (self.tool + 1,), 233, NORMAL)


# XL_DISPLAY_DEMO STATE=filament: what xl_filament tells the screen during a
# load and an unload, on a timer.  Nothing is sent to the printer.
class DemoFilament:
    def __init__(self, d):
        self.d = d
        self.steps = []
        self.at = 0.
        self.ask = self.after = self.note = self.end_note = None
    def _run(self, mode, tool, names, after=None, note=None):
        self.steps = [(mode, tool, name) for name in names]
        self.at = self.d.reactor.monotonic() + DEMO_STEP
        self.after, self.end_note = after, note
    def load(self, tool):
        if self.d.demo == MENU_DEMO:
            self._run('load', tool, ('tool', 'park', 'push', 'gears'),
                      after=(tool, 'load'))
        else:                                # in a pause it does not ask
            self._run('load', tool, ('push', 'gears', 'heat', 'nozzle', 'purge'))
    def change(self, tool):
        self._run('unload', tool, ('park', 'heat', 'ram', 'unload'))
    def purge(self, tool):
        self._run('purge', tool, ('park', 'heat', 'purge'))
    def unload(self, tool):
        if self.d.tool_filament(tool)[1]:
            self.answer((tool, 'unload'), 'known')
        else:
            self.ask = (tool, 'unload')
    def answer(self, ask, ftype):
        tool, mode = ask
        self.ask = None
        if mode == 'load' and ftype:
            self._run('load', tool, ('heat', 'nozzle', 'purge', 'retract'))
        elif mode == 'load':
            self._run('unload', tool, ('unload',), note='Load cancelled. Pull'
                      ' the filament out of the extruder.')
        elif ftype:
            self._run('unload', tool, ('tool', 'park', 'heat', 'ram', 'unload'),
                      note='Please remove filament from filament sensor.')
    def screen_step(self):
        now = self.d.reactor.monotonic()
        if self.steps and now >= self.at:
            self.steps.pop(0)
            self.at = now + DEMO_STEP
            if not self.steps:
                self.ask, self.note = self.after, self.end_note
        return self.steps[0] if self.steps else None
    def screen_ask(self):
        return self.ask
    def screen_note(self):
        note, self.note = self.note, None
        return note


# ScreenMenuTune (screen_menu_tune.hpp): the items a Klipper print can use, in
# stock's order.  Filament sensors, stealth mode, input shaper and the like
# stay in Mainsail.  Stock's Change Filament runs unload, load and resume as one;
# M600 here stops after the unload, so it asks to load next, and Load Filament
# and Purge Filament are there for a pause (after a runout, if the load waited,
# or if it did not come out clean).
class ChangeTypePage(MenuPage):
    # Which filament goes in (owner 11/06: stock assumes the same one, and ABS
    # swapped for PLA then kept ABS's settings).  The one in it comes first with
    # the focus on it, so the same filament is one click.
    def __init__(self, d, tool):
        current = d.tool_filament(tool)[0].upper()
        temps = xl_filament.FILAMENT_TEMPS
        items = [return_item(d)]
        for name, temp in sorted(temps, key=lambda nt: nt[0] != current):
            same = name == current
            items.append(Item(
                name, None, lambda n=name, s=same: self._pick(tool, '' if s else n),
                info=lambda e, s=same, t=temp: 'same' if s else '%d' % (t,),
                unit=None if same else '\xb0C'))
        MenuPage.__init__(self, d, 'Changing filament', None, items, footer=False)
        if current in dict(temps):
            self.focus = 1
    def _pick(self, tool, ftype):
        self.d.drop_page()
        self.d.filament_change(tool, ftype)


class TunePage(MenuPage):
    def __init__(self, d):
        items = [return_item(d),
                 Item('Change Filament', run=self._change,
                      enabled=d.can_change),
                 Item('Load Filament', run=self._load, enabled=d.can_reload),
                 Item('Purge Filament', run=self._purge, enabled=d.can_reload),
                 Spin('Print Speed', d.read_speed, d.send_speed,
                      10, 300, '%'),
                 Spin('Nozzle Temperature', d.read_nozzle, d.send_nozzle,
                      0, d.nozzle_max(), '\xb0C', off=True,
                      enabled=d.has_tool),
                 Spin('Heatbed Temperature', d.read_bed, d.send_bed,
                      0, d.bed_max(), '\xb0C', off=True),
                 Spin('Print Fan Speed', d.read_fan, d.send_fan,
                      0, 100, '%', off=True),
                 Spin('Live Adjust Z', d.read_live_z, d.send_live_z,
                      -2., 2., 'mm', step=.005, digits=3, live=True),
                 Spin('Flow Factor', d.read_flow, d.send_flow,
                      50, 150, '%')]
        MenuPage.__init__(self, d, 'TUNE', 'settings_16x16', items)
    def _change(self):
        self.d.open_page(ChangeTypePage(self.d, self.d.change_target()))
    def _load(self):
        self.d.filament_load(self.d.change_target(), change=True)
    def _purge(self):
        self.d.filament_purge(self.d.change_target())


# screen_preheat.cpp.  Stock's tile heats every tool (M1700 T-1); owner 11/06:
# the filament, then which tool, All Tools first.  A tool off its dock stays
# cold under All Tools and cannot be picked: it may be on the bench.
class PreheatPage(MenuPage):
    def __init__(self, d):
        items = [return_item(d)]
        for name, temp in xl_filament.FILAMENT_TEMPS:
            items.append(Item(name, None,
                              lambda n=name: self._choose(PREHEAT_BED[n]),
                              info=lambda e, t=temp, n=name: '%3d/%-3d' % (
                                  t, PREHEAT_BED[n])))
        items.append(Item('Cooldown', None, self._cool))
        MenuPage.__init__(self, d, 'Preheating', None, items, footer=False)
    def _choose(self, bed):
        d = self.d
        tools = d.tools()
        if len(tools) == 1:                  # select_tool_dialog: nothing to ask
            self._heat(bed, tools[0])
        else:
            d.open_page(ToolPage(d, lambda t: self._heat(bed, t), all_tools=True))
    def _heat(self, bed, tool):
        d = self.d
        tools = ([tool] if tool is not None
                 else [t for t in d.tools() if d.tool_ready(t)])
        lines = ['M104 T%d S%d' % (t, PREHEAT_NOZZLE) for t in tools]
        d.run_gcode('\n'.join(lines + ['M140 S%d' % (bed,)]), show=True)
        d.close_page()
    def _cool(self):
        self.d.cooldown()
        self.d.close_page()


# screen_filebrowser.cpp: folders first, newest first (stock's default sort),
# the Pi's G-code folder.  Stock's preview with the thumbnail is a question here.
class FilePage(MenuPage):
    def __init__(self, d, folder=''):
        items = [return_item(d)]
        for name, is_dir in d.list_files(folder):
            path = os.path.join(folder, name)
            if is_dir:
                items.append(Item(name, 'folder_full_16x16',
                                  lambda p=path: d.open_page(FilePage(d, p))))
            else:
                items.append(Item(name, None,
                                  lambda p=path, n=name: self._ask(p, n)))
        MenuPage.__init__(self, d, 'PROJECTS', 'folder_full_16x16', items,
                          footer=False)
    def _ask(self, path, name):
        d = self.d
        wrong = d.filament_mismatch(path)
        if wrong:
            # the file against what is loaded: Back first
            text = ' '.join(wrong[:3] + ['Print anyway?'])
        else:
            text = 'Print %s?' % (name,)
        d.open_page(QuestionPage(
            d, text, [('Print', lambda: d.print_file(path)), ('Back', None)],
            1 if wrong else 0))


# screen_menu_control.hpp, the rows that have a Klipper side.  Stock's Disable
# Motors keeps Z on with M18 X Y E, which Klipper does not have: M84, Mainsail's.
class ControlPage(MenuPage):
    def __init__(self, d):
        items = [return_item(d),
                 Item('Auto Home', run=lambda: d.run_gcode('G28', show=True),
                      enabled=d.idle),
                 SubMenu('Pick/Park Tool',
                         run=lambda: d.open_page(PickParkPage(d))),
                 SubMenu('Move Axis', 'move_16x16', run=self._move),
                 Item('Disable Motors',
                      run=lambda: d.run_gcode('M84', show=True), enabled=d.idle)]
        MenuPage.__init__(self, d, 'CONTROL', 'calibrate_white_16x16', items)
    def _move(self):
        d = self.d
        if d.is_homed('xyz'):
            d.open_page(MovePage(d))
            return
        def home():
            d.run_gcode('G28', show=True)
            d.open_page(MovePage(d))
        d.open_page(QuestionPage(
            d, 'The printer is not homed. Home it now?',
            [('Yes', home), ('No', None)], 1))


# screen_tool_pick_park.cpp: the macros Mainsail's tool buttons run.  A tool off
# its dock cannot be picked.
class PickParkPage(MenuPage):
    def __init__(self, d):
        self.d = d
        self.active = d._tool()
        MenuPage.__init__(self, d, 'TOOL SELECT', None, self._rows(), footer=False)
    def _rows(self):
        d = self.d
        items = [return_item(d),
                 Item('Park Current Tool', None,
                      lambda: d.run_gcode('PARK_TOOL', show=True),
                      lambda e: d.idle(e) and self.active is not None)]
        for t in d.tools():
            picked = t == self.active
            items.append(Item(
                '%s Tool %d' % ('Park' if picked else 'Pick', t + 1),
                'arrow_right_10x16' if picked else None,
                lambda t=t, picked=picked: d.run_gcode(
                    'PARK_TOOL' if picked else 'PICK_TOOL T=%d' % (t,), show=True),
                lambda e, t=t: d.idle(e) and d.tool_ready(t)))
        return items
    def tick(self, eventtime):
        active = self.d._tool()
        if active != self.active:
            self.active = active
            self.items = self._rows()
            self.draw(eventtime)
            return
        MenuPage.tick(self, eventtime)


# screen_menu_move.cpp: a click starts editing, each step moves at once.
class MovePage(MenuPage):
    def __init__(self, d):
        lo, hi = d.axis_range()
        items = [return_item(d)]
        for i, axis in enumerate('XYZ'):
            items.append(Spin('Move %s' % (axis,),
                              lambda e, i=i: d.read_axis(i),
                              lambda change, a=axis: d.send_move(a, change),
                              lo[i], hi[i], 'mm', live=True,
                              enabled=lambda e, i=i: d.axis_ok(e, i)))
        items.append(Spin('Move E', lambda e: 0,
                          lambda change: d.send_move('E', change),
                          -1000, 1000, 'mm', live=True,
                          enabled=d.can_extrude))
        items.append(Item('Cooldown', None, d.cooldown))
        MenuPage.__init__(self, d, 'MOVE AXIS', 'move_16x16', items)


# screen_menu_info.hpp, what Klipper knows.  Stock's Statistics are Moonraker's
# history in Mainsail.
class InfoPage(MenuPage):
    def __init__(self, d):
        items = [return_item(d),
                 SubMenu('Network Status',
                         run=lambda: d.open_page(NetworkPage(d))),
                 SubMenu('Sensor Info',
                         run=lambda: d.open_page(SensorPage(d))),
                 SubMenu('Version Info',
                         run=lambda: d.open_page(VersionPage(d)))]
        MenuPage.__init__(self, d, 'INFO', 'info_16x16', items)


def fixed_rows(d, rows):
    return [return_item(d)] + [Item(label, info=lambda e, v=value: v)
                               for label, value in rows]


class NetworkPage(MenuPage):
    def __init__(self, d):
        MenuPage.__init__(self, d, 'NETWORK', None, fixed_rows(d, d.network_rows()))


class VersionPage(MenuPage):
    def __init__(self, d):
        MenuPage.__init__(self, d, 'VERSION INFO', None,
                          fixed_rows(d, d.version_rows()))


class SensorPage(MenuPage):
    def __init__(self, d):
        items = [return_item(d)]
        for t in d.tools():
            name = 'Tool %d' % (t + 1,)
            items.append(Item(name + ' Nozzle',
                              info=lambda e, t=t: d.nozzle_now(e, t),
                              unit='\xb0C'))
            items.append(Item(name + ' Side Sensor',
                              info=lambda e, t=t: d.side_sensor(t)))
            items.append(Item(name + ' Tool Sensor',
                              info=lambda e, t=t: d.tool_sensor(t)))
        items.append(Item('Heatbed', info=d.bed_now, unit='\xb0C'))
        MenuPage.__init__(self, d, 'SENSOR INFO', None, items)


TILE_PAGES = {'Print': FilePage, 'Preheat': PreheatPage, 'Filament': FilamentPage,
              'Control': ControlPage, 'Info': InfoPage}


class XLDisplay:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.mcu = self.printer.lookup_object('mcu')
        self.gcode = self.printer.lookup_object('gcode')
        self.gcode_move = self.printer.load_object(config, 'gcode_move')
        self.print_stats = self.printer.load_object(config, 'print_stats')
        self.display_status = self.printer.load_object(config,
                                                       'display_status')
        # Klipper reads only M73 P.  The slicer's R (minutes left) and C (to
        # the next change) are what stock's print screen shows.
        self.m73_base = self.gcode.register_command('M73', None)
        self.gcode.register_command('M73', self.cmd_M73)
        self.time_left = self.time_to_change = None
        buttons = self.printer.load_object(config, 'buttons')
        enc = config.getlist('encoder_pins', ['^PD13', '^PD12'], count=2)
        # 2 steps per click (printer 10/28: with 4 a tile moved every 2-3
        # clicks)
        detent = config.getchoice('encoder_steps_per_detent', {2: 2, 4: 4}, 2)
        buttons.register_rotary_encoder(enc[0], enc[1], self._knob_cw,
                                        self._knob_ccw, detent)
        buttons.register_button_push(config.get('click_pin', '^!PG3'),
                                     self._knob_press)
        self.icons = {}                  # name -> (id, w, h)
        self.cmd_queue = None
        self.lock = self.reactor.mutex()     # one drawing at a time
        self.unsynced = 0
        self.lcd_ver = 1
        self.home = HomePage(self)
        self.page = self.home
        self.stack = []                  # the pages under this one
        self.knob_steps = self.presses = 0
        self.pump_pending = False
        self.ready = False
        self.clock = None
        self.footer = None               # (xs, items) on the screen
        self.action = None               # the PAUSE etc. the screen is running
        self.last_print = None
        self.print_page = None
        self.z_pending = 0.
        self.z_running = False
        self.message = None
        self.message_at = 0.
        self.demo = None                 # XL_DISPLAY_DEMO: a look, no G-code
        self.demo_filament = DemoFilament(self)
        self.change = None               # Change Filament: which half is running
        self.change_type = ''            # and a different filament, if picked
        self.filament_running = 0        # the screen's filament G-code running
        self.screen_error = None        # what the screen's G-code was refused with
        self.gcode_running = 0           # run_gcode: still going
        self.move_pending = {}
        self.move_running = False
        self.mcu.register_config_callback(self._build_config)
        self.printer.register_event_handler("klippy:ready", self._ready)
        self.gcode.register_command(
            'XL_DISPLAY_REDRAW', self.cmd_XL_DISPLAY_REDRAW,
            desc=self.cmd_XL_DISPLAY_REDRAW_help)
        self.gcode.register_command(
            'XL_DISPLAY_DEMO', self.cmd_XL_DISPLAY_DEMO,
            desc=self.cmd_XL_DISPLAY_DEMO_help)
    def _build_config(self):
        self.cmd_queue = self.mcu.alloc_command_queue()
    def _ready(self):
        if self.printer.get_start_args().get('debugoutput') is not None:
            return
        constants = self.mcu.get_constants()
        icons = constants.get('XL_LCD_ICONS')
        if not icons:
            logging.info("xl_display: this XLBuddy firmware has no screen"
                         " support (xl_lcd) - flash the KlipperXL-Plus"
                         " XLBuddy firmware; the screen stays as it is")
            return
        for i, item in enumerate(icons.split(',')):
            name, w, h = item.split(':')
            self.icons[name] = (i, int(w), int(h))
        self.lcd_ver = constants.get('XL_LCD_VER', 1)
        if self.lcd_ver < 2:
            logging.info("xl_display: this XLBuddy firmware has the home"
                         " screen only - flash the current KlipperXL-Plus"
                         " XLBuddy firmware for the print screen")
        cq = self.cmd_queue
        self.cmd_lcd = self.mcu.lookup_command(
            "xl_lcd_cmd cmd=%c data=%*s", cq=cq)
        self.cmd_fill = self.mcu.lookup_command(
            "xl_lcd_fill x=%hu y=%hu w=%hu h=%hu color=%u", cq=cq)
        self.cmd_icon = self.mcu.lookup_command(
            "xl_lcd_icon id=%c x=%hu y=%hu row=%hu rows=%hu", cq=cq)
        self.cmd_text = self.mcu.lookup_command(
            "xl_lcd_text x=%hu y=%hu fg=%u bg=%u text=%*s", cq=cq)
        if self.lcd_ver >= 2:
            self.cmd_icon_swap = self.mcu.lookup_command(
                "xl_lcd_icon_swap id=%c x=%hu y=%hu row=%hu rows=%hu", cq=cq)
            self.cmd_text_font = self.mcu.lookup_command(
                "xl_lcd_text_font font=%c x=%hu y=%hu fg=%u bg=%u text=%*s",
                cq=cq)
        self.cmd_read = self.mcu.lookup_query_command(
            "xl_lcd_read cmd=%c", "xl_lcd_read_result cmd=%c value=%c",
            cq=cq)
        # debug_ping: nothing else waits for 'pong' (get_uptime is
        # xl_side_sensors' - two waiters on one reply would collide)
        self.cmd_sync = self.mcu.lookup_query_command(
            "debug_ping data=%*s", "pong data=%*s", cq=cq)
        self.reactor.register_callback(self._start)
    # Drawing commands (with self.lock held), split so one never exceeds
    # MAX_BYTES, and at most IN_FLIGHT of them not yet done by the XLBuddy
    def _send(self, cmd, args):
        cmd.send(args)
        self.unsynced += 1
        if self.unsynced >= IN_FLIGHT:
            self._sync()
    def _sync(self):
        if self.unsynced:
            self.cmd_sync.send([b''])
            self.unsynced = 0
    # Everything is clipped here: nothing out of bounds reaches the XLBuddy
    def _fill(self, x, y, w, h, color=BLACK):
        x0, y0 = max(x, 0), max(y, 0)
        w, h = min(x + w, W) - x0, min(y + h, H) - y0
        if w <= 0 or h <= 0:
            return
        x, y = x0, y0
        cols = min(w, MAX_BYTES // 3)
        rows = max(1, MAX_BYTES // (3 * cols))
        for r in range(0, h, rows):
            for c in range(0, w, cols):
                self._send(self.cmd_fill, [x + c, y + r, min(cols, w - c),
                                           min(rows, h - r), color])
    def _icon(self, name, x, y, swap=False):
        item = self.icons.get(name)
        if item is None:
            logging.info("xl_display: no icon %s in this firmware", name)
            return
        iid, w, h = item
        if x < 0 or y < 0 or x + w > W or y + h > H:
            return
        cmd = self.cmd_icon_swap if swap and self.lcd_ver >= 2 else self.cmd_icon
        rows = max(1, MAX_BYTES // (3 * w))
        for r in range(0, h, rows):
            self._send(cmd, [iid, x, y, r, min(rows, h - r)])
    def _text(self, s, x, y, color=WHITE, back=BLACK, font=SMALL, max_w=None):
        if self.lcd_ver < 2:
            font = SMALL
        fw, fh = FONT_SIZE[font]
        room = W - x if max_w is None else min(max_w, W - x)
        if x < 0 or y < 0 or y + fh > H or room < fw:
            return
        data = s.encode('latin-1', 'replace')[:room // fw]
        n = max(1, MAX_BYTES // (3 * fw * fh))
        for i in range(0, len(data), n):
            if font == SMALL:
                self._send(self.cmd_text, [x + i * fw, y, color, back,
                                           data[i:i + n]])
            else:
                self._send(self.cmd_text_font, [font, x + i * fw, y, color,
                                                back, data[i:i + n]])
    # draw_rounded_rect: the rows with the same corners as one rectangle
    def _rrect(self, x, y, w, h, r, color, back=BLACK, right=None):
        rows = corner_rows(h, r)
        j = 0
        while j < h:
            inset = rows[j]
            k = j
            while k < h and rows[k] == inset:
                k += 1
            if inset:
                self._fill(x, y + j, inset, k - j, back)
                self._fill(x + w - inset, y + j, inset, k - j,
                           back if right is None else right)
            self._fill(x + inset, y + j, w - 2 * inset, k - j, color)
            j = k
    # A new page: all but the header's right end (network, clock)
    def _clear_body(self, keep_header=False):
        if not keep_header:
            self._fill(0, 0, 400, HEADER_H)
        self._fill(0, HEADER_H, W, H - HEADER_H)
        self.footer = None
    def _draw_header(self, icon, title):
        # window_header: icon (14, 12), label (34, 13) in the small font
        # without an icon the label starts at the header's padding
        self._fill(0, 0, 400, HEADER_H)
        if icon is not None:
            self._icon(icon, 14, 12)
        self._text(title, 34 if icon is not None else 14, 13, max_w=360)
    # Start: wake as Prusa's ili9488_init after its bootloader, full page
    def _start(self, eventtime):
        try:
            with self.lock:
                self._send(self.cmd_lcd, [0x11, b''])        # sleep out
                self._sync()
                self.reactor.pause(self.reactor.monotonic() + .12)
                for cmd, data in ((0x36, b'\xE0'), (0x3A, b'\x66'),
                                  (0x21, b''), (0x29, b''), (0x51, b'\xFF')):
                    self._send(self.cmd_lcd, [cmd, data])
                madctl = self.cmd_read.send([0x0B])['value']
                self.unsynced = 0                    # the read ran after them
                logging.info("xl_display: screen MADCTL 0x%02x"
                             " (0xe0 = answering)", madctl)
                self._draw_page(self.reactor.monotonic())
                self._sync()
            self.ready = True
            self.reactor.register_timer(self._tick,
                                        self.reactor.monotonic() + TICK)
        except Exception:
            logging.exception("xl_display: start failed")
    def _draw_clock(self):
        self.clock = time.strftime('%H:%M')
        self._text(self.clock, CLOCK[0] + CLOCK[2] - FW * len(self.clock),
                   CLOCK[1])
    # The page on the screen, whole
    def _draw_page(self, eventtime):
        self.page.draw(eventtime)
        if self.page.footer:
            self._draw_footer(eventtime)
    # Pages (with self.lock held)
    def open_page(self, page):
        self.stack.append(self.page)
        self.page = page
        self._draw_page(self.reactor.monotonic())
    def drop_page(self):
        # the page under this one, not drawn yet
        self.page = self.stack.pop() if self.stack else self.home
    def close_page(self):
        self.drop_page()
        self._draw_page(self.reactor.monotonic())
    def go_home(self):
        self.stack = []
        self.demo = None
        self.page = self.home
        self.print_page = None
        self._draw_page(self.reactor.monotonic())
    def _show_print_page(self):
        self.stack = []
        self.print_page = PrintPage(self)
        self.page = self.print_page
        self._draw_page(self.reactor.monotonic())
    # What the print is doing
    def print_state(self, eventtime):
        """The print screen's state (screen_printing.cpp change_print_state);
        None = no print to show."""
        if self.demo is not None:
            return self.demo
        state = self.print_stats.get_status(eventtime)['state']
        if self.action == 'stop':
            return ABORTING
        if state in ('printing', 'paused'):
            if self.action == 'pause':
                return PAUSING
            if self.action == 'resume':
                return RESUMING
            if state == 'paused':
                return PAUSED
            pup = self.printer.lookup_object('xl_puppies', None)
            if pup is not None and pup.absorbing():
                return SKIPPABLE
            return PRINTING
        if state == 'complete':
            return PRINTED
        if state in ('cancelled', 'error'):
            return STOPPED
        return None
    def print_name(self, eventtime):
        if self.demo is not None:
            return 'demo_print_0.4n_0.2mm_PLA_XL_2h30m.gcode'
        name = self.print_stats.get_status(eventtime)['filename']
        return os.path.basename(name)
    def print_percent(self, eventtime):
        if self.demo is not None:
            return 100 if self.demo == PRINTED else 42
        if self.print_state(eventtime) == PRINTED:
            return 100
        p = self.display_status.get_status(eventtime)['progress']
        return min(max(int(p * 100.), 0), 100)
    def print_duration(self, eventtime):
        if self.demo is not None:
            return 3930.
        return self.print_stats.get_status(eventtime)['total_duration']
    def print_time_left(self, eventtime):
        if self.demo is not None:
            return 5400.
        if self.time_left is not None:
            return self.time_left
        # no M73 R in this file: from the progress so far
        p = self.display_status.get_status(eventtime)['progress']
        if p < .01:
            return None
        return self.print_duration(eventtime) * (1. - p) / p
    # An M117 is shown while it is news: "Absorbing heat 42%" changes all the
    # time, a line a slicer's start G-code left must not hide the times for
    # the whole print
    def status_message(self, eventtime):
        if self.demo is not None:
            return None
        message = self.display_status.get_status(eventtime)['message']
        if message != self.message:
            self.message = message
            self.message_at = eventtime
        if message and eventtime - self.message_at < MESSAGE_TIME:
            return message
        return None
    def cmd_M73(self, gcmd):
        left = gcmd.get_float('R', None)
        if left is not None:
            self.time_left = left * 60.
        change = gcmd.get_float('C', None)
        if change is not None:
            self.time_to_change = change * 60.
        self.m73_base(gcmd)
    # The print screen opens by itself when a print starts, from any page, and
    # again when it ends if another page was left open
    def _watch_print(self, eventtime):
        if self.lcd_ver < 2:
            return
        state = self.print_stats.get_status(eventtime)['state']
        running = state in ('printing', 'paused')
        if running:
            self.demo = None                 # a real print ends a demo
        if self.demo is not None or state == self.last_print:
            return
        last, self.last_print = self.last_print, state
        was_running = last in ('printing', 'paused')
        if running and not was_running:
            self.time_left = self.time_to_change = None
            self.change_type = ''
            if self.page is not self.print_page:
                self._show_print_page()
        elif state == 'standby':
            if self.print_page is not None:
                self.go_home()
        elif was_running and not running:
            if self.page is not self.print_page:
                self._show_print_page()
    # Filament: xl_filament has the last word on what shows, so a load from
    # Mainsail or the side sensor brings up the same pages and an answer given
    # in Mainsail closes the question here
    def filament_source(self):
        if self.demo is not None:
            return self.demo_filament
        return self.printer.lookup_object('xl_filament', None)
    def _show_filament(self, page):
        if isinstance(self.page, (ProgressPage, FilamentTypePage)):
            self.drop_page()
        self.open_page(page)
    def _watch_filament(self, eventtime):
        source = self.filament_source()
        if source is None or self.lcd_ver < 2:
            return
        if self.filament_running or source.screen_step() is not None:
            if not isinstance(self.page, ProgressPage):
                self._show_filament(ProgressPage(self))
            return
        ask = source.screen_ask()
        if ask is not None:
            if not (isinstance(self.page, FilamentTypePage)
                    and self.page.ask == ask):
                self._show_filament(FilamentTypePage(self, ask))
            return
        if isinstance(self.page, (ProgressPage, FilamentTypePage)):
            self.close_page()
        error, self.screen_error = self.screen_error, None
        note = source.screen_note()
        change, self.change = self.change, None
        if change is not None and change[0] == 'unload' and not error:
            # (the unload's "remove the filament" is in this question)
            tool = change[1]
            self.open_page(QuestionPage(
                self, 'Remove the old filament, then press Load and push the'
                ' new filament in.',
                [('Load', lambda: self.filament_load(tool, change=True)),
                 ('Later', None)]))
        elif error or note:
            self.open_page(QuestionPage(self, error or note, [('OK', None)]))
        elif change is not None and self.print_page is not None:
            # loaded: the print screen, one click from Resume
            self.change_type = ''
            self.stack, self.page = [], self.print_page
            self.page.focus = 1
            self._draw_page(eventtime)
    def _filament_gcode(self, script):
        self.filament_running += 1
        def run(eventtime):
            try:
                self.gcode.run_script(script)
            except self.printer.command_error as e:
                self.screen_error = str(e)  # already on the console too
            except Exception:
                logging.exception("xl_display: %s failed", script)
            finally:
                self.filament_running -= 1
        self.reactor.register_callback(run)
    def filament_load(self, tool, change=False):
        self.change = ('load', tool) if change else None
        if self.demo is not None:
            self.demo_filament.load(tool)
        elif change and self.change_type:
            self._filament_gcode('LOAD_FILAMENT TOOL=%d TYPE=%s'
                                 % (tool, self.change_type))
        else:
            self._filament_gcode('FILAMENT_LOAD TOOL=%d' % (tool,))
        self.open_page(ProgressPage(self, 'load', tool))
    def filament_purge(self, tool):
        self.change = ('load', tool)         # then the print screen, as a load
        if self.demo is not None:
            self.demo_filament.purge(tool)
        else:
            self._filament_gcode('PURGE_FILAMENT TOOL=%d' % (tool,))
        self.open_page(ProgressPage(self, 'purge', tool))
    def filament_change(self, tool, ftype=''):
        self.change = ('unload', tool)
        self.change_type = ftype
        if self.demo is not None:
            self.demo_filament.change(tool)
        else:
            self._filament_gcode('M600')
        self.open_page(ProgressPage(self, 'unload', tool))
    def filament_unload(self, tool):
        if self.demo is not None:
            self.demo_filament.unload(tool)
        else:
            self._filament_gcode('FILAMENT_UNLOAD TOOL=%d' % (tool,))
        self.open_page(ProgressPage(self, 'unload', tool))
    def filament_answer(self, ask, ftype):
        tool, mode = ask
        if self.demo is not None:
            self.demo_filament.answer(ask, ftype)
        else:
            self._filament_gcode('XL_FILAMENT_TYPE TOOL=%d %s' % (
                tool, 'TYPE=%s' % (ftype,) if ftype else 'CANCEL=1'))
        self._show_filament(ProgressPage(self, mode if ftype else 'unload',
                                         tool))
    # Change Filament: the tool on the carriage (M600 can have no other), and
    # in a demo with none picked, a tool to look at
    def change_target(self):
        tool = self._tool()
        return 0 if tool is None and self.demo is not None else tool
    def can_change(self, eventtime):
        return (self.change_target() is not None
                and self.print_state(eventtime) in (PRINTING, PAUSED))
    def can_reload(self, eventtime):
        return (self.change_target() is not None
                and self.print_state(eventtime) == PAUSED)
    def tools(self):
        return [t for t in range(xl_filament.NUM_TOOLS)
                if self._dock(t) is not None]
    def _dock(self, tool):
        return self.printer.lookup_object('xl_dock_sensors T%d' % (tool,), None)
    def tool_ready(self, tool):
        return tool == self._tool() or self._dock(tool).is_parked()
    def tool_filament(self, tool):
        # (type, temperature) the last load put in this tool
        sv = self.printer.lookup_object('save_variables', None)
        f = sv.allVariables.get('filament_t%d' % (tool,)) if sv else None
        if not isinstance(f, dict):
            return '', 0
        return str(f.get('type', '')), f.get('temp', 0)
    def tool_details(self, tool):
        # build_details: nozzle, then the filament in it
        ftype = self.tool_filament(tool)[0] or '---'
        nozzle = getattr(self._extruder(tool), 'nozzle_diameter', None)
        if nozzle is None:
            return ftype
        return '%s mm %s' % (('%.2f' % (nozzle,)).rstrip('0').lstrip('0'),
                             ftype)
    # G-code from the screen, in the order Mainsail would send it.  Never with
    # self.lock held: a PAUSE takes a while and the screen keeps drawing.
    def _run_gcode(self, script, show=False):
        try:
            self.gcode.run_script(script)
        except self.printer.command_error as e:
            if show:                         # already on the console too
                self.screen_error = str(e)
        except Exception:
            logging.exception("xl_display: %s failed", script)
    def run_gcode(self, script, show=False):
        # show: a refusal comes up on the screen, for a press that is not
        # followed by a page of its own
        if self.demo is not None:
            logging.info("xl_display: demo, not sent: %s", script)
            return
        self.gcode_running += 1
        def run(eventtime):
            try:
                self._run_gcode(script, show)
            finally:
                self.gcode_running -= 1
        self.reactor.register_callback(run)
    def idle(self, eventtime):
        # stock greys these rows while G-code runs (is_processing)
        return not self.gcode_running
    def cooldown(self):
        # every heater, and stock's print fan off (M1700 Cooldown)
        self.run_gcode('TURN_OFF_HEATERS\nM107', show=True)
    # The file list
    def list_files(self, folder):
        sd = self.printer.lookup_object('virtual_sdcard', None)
        if sd is None:
            return []
        rows = []
        try:
            for entry in os.scandir(os.path.join(sd.sdcard_dirname, folder)):
                if entry.name.startswith('.'):
                    continue
                is_dir = entry.is_dir()
                if is_dir or entry.name.lower().endswith(GCODE_EXTS):
                    rows.append((not is_dir, -entry.stat().st_mtime, entry.name))
        except OSError:
            logging.exception("xl_display: cannot list %s", folder)
        return [(name, not is_file)
                for is_file, _, name in sorted(rows)[:MAX_FILES]]
    # What the slicer wrote near the end of the file: the tools it uses and
    # their filament.  Read once, when a file is picked, from the Pi's disk.
    def file_filaments(self, path):
        sd = self.printer.lookup_object('virtual_sdcard', None)
        if sd is None:
            return {}
        used = types = None
        try:
            with open(os.path.join(sd.sdcard_dirname, path), 'rb') as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(f.tell() - FILE_TAIL, 0))
                tail = f.read().decode('latin-1')
            for line in tail.splitlines():
                if line.startswith('; filament used [mm] ='):
                    used = [float(v) for v in line.split('=')[1].split(',')]
                elif line.startswith('; filament_type ='):
                    types = [v.strip() for v in line.split('=')[1].split(';')]
        except (OSError, ValueError):
            logging.exception("xl_display: cannot read %s", path)
            return {}
        if used is None or types is None or len(used) != len(types):
            return {}
        return {t: types[t] for t in range(len(used)) if used[t] > 0.}
    def filament_mismatch(self, path):
        wrong = []
        tools = self.tools()
        for tool, wanted in sorted(self.file_filaments(path).items()):
            if tool not in tools:
                continue
            loaded = self.tool_filament(tool)[0]
            name = 'Tool %d' % (tool + 1,)
            if loaded and loaded.upper() != wanted.upper():
                wrong.append('%s: file wants %s, %s is loaded.'
                             % (name, wanted, loaded))
            elif not loaded and self.side_sensor(tool) == 'No filament':
                wrong.append('%s: file wants %s, no filament in it.'
                             % (name, wanted))
        return wrong
    def print_file(self, path):
        self.run_gcode('SDCARD_PRINT_FILE FILENAME="%s"'
                       % (path.replace('"', ''),), show=True)
    # Control
    def _toolhead(self, eventtime):
        return self.printer.lookup_object('toolhead').get_status(eventtime)
    def is_homed(self, axes):
        homed = self._toolhead(self.reactor.monotonic())['homed_axes']
        return all(a in homed for a in axes)
    def axis_range(self):
        st = self._toolhead(self.reactor.monotonic())
        lo, hi = list(st['axis_minimum'][:3]), list(st['axis_maximum'][:3])
        hi[1] = min(hi[1], Y_PRINT_MAX)
        lo[2] = max(lo[2], 0.)               # Z_MIN_POS: not into the bed
        return lo, hi
    def read_axis(self, i):
        return self._toolhead(self.reactor.monotonic())['position'][i]
    def axis_ok(self, eventtime, i):
        # homed and inside the range: from out of it (a tool in the dock area)
        # the first click would jump to the edge.  A move to the edge can end a
        # hair past it (printer 11/06: Z at -3e-15 greyed Move Z at Z 0, with no
        # way back up from here)
        lo, hi = self.axis_range()
        return (self.is_homed('xyz'[i]) and self.idle(eventtime)
                and lo[i] - .01 <= self.read_axis(i) <= hi[i] + .01)
    def can_extrude(self, eventtime):
        tool = self._tool()
        ext = self._extruder(tool) if tool is not None else None
        return bool(ext and ext.get_status(eventtime).get('can_extrude'))
    # Mainsail's jog: one command at a time, the clicks that come in meanwhile
    # go together, so a fast knob never builds a queue
    def send_move(self, axis, change):
        if self.demo is not None:
            return
        self.move_pending[axis] = self.move_pending.get(axis, 0.) + change
        if not self.move_running:
            self.move_running = True
            self.reactor.register_callback(self._run_moves)
    def _run_moves(self, eventtime):
        try:
            while self.move_pending:
                pending, self.move_pending = self.move_pending, {}
                for axis, change in sorted(pending.items()):
                    if abs(change) > 1e-6:
                        self._run_gcode('G91\nG1 %s%.3f F%d\nG90' % (
                            axis, change, MOVE_SPEED[axis]), True)
        finally:
            self.move_running = False
    # Info
    def network_rows(self):
        address = '---'
        try:
            # no packet goes out: this only asks which address the route uses
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(('10.255.255.255', 1))
            address = s.getsockname()[0]
            s.close()
        except OSError:
            pass
        return [('Hostname', socket.gethostname()), ('IP Address', address)]
    def version_rows(self):
        rows = [('Klipper', self.printer.get_start_args().get(
            'software_version', '---')[:30])]
        check = self.printer.lookup_object('xl_fw_check', None)
        boards = check.boards if check is not None else {}
        for label in sorted(boards, key=lambda b: (b[0] == 'T', b)):
            name = ('Tool %d' % (int(label[1:]) + 1,) if label[0] == 'T'
                    else label)
            rows.append((name, str(boards[label]['fw'] or 'no ID')[:30]))
        return rows
    def nozzle_now(self, eventtime, tool):
        ext = self._extruder(tool)
        return '%d' % (ext.get_status(eventtime)['temperature'] + .5,) if ext else '---'
    def bed_now(self, eventtime):
        bed = self.printer.lookup_object('heater_bed', None)
        return '%d' % (bed.get_status(eventtime)['temperature'] + .5,) if bed else '---'
    def _sensor_text(self, state):
        return {'has_filament': 'Has filament',
                'no_filament': 'No filament'}.get(state, state or '---')
    def side_sensor(self, tool):
        side = self.printer.lookup_object('xl_side_sensors', None)
        return self._sensor_text(side.state.get(tool) if side else None)
    def tool_sensor(self, tool):
        fs = self.printer.lookup_object('xl_tool_fsensor T%d' % (tool,), None)
        return self._sensor_text(fs.state if fs else None)
    def print_action(self, action, script):
        if self.demo is not None:
            logging.info("xl_display: demo, not sent: %s", script)
            self.demo = {'pause': PAUSED, 'resume': PRINTING,
                         'stop': STOPPED}[action]
            return
        self.action = action
        def run(eventtime):
            try:
                self._run_gcode(script)
            finally:
                self.action = None
        self.reactor.register_callback(run)
    def absorb_request(self, end):
        if self.demo is not None:
            return
        pup = self.printer.lookup_object('xl_puppies', None)
        if pup is not None:
            pup.absorb_request(end)
    def reprint(self, eventtime):
        name = self.print_stats.get_status(eventtime)['filename']
        if name:
            self.run_gcode('SDCARD_PRINT_FILE FILENAME="%s"'
                           % (name.replace('"', ''),))
    # Tune: read what Klipper has, send what Mainsail would
    def _tool(self):
        tc = self.printer.lookup_object('xl_toolchanger', None)
        tool = tc.active_tool if tc is not None else -1
        return tool if tool is not None and tool >= 0 else None
    def has_tool(self, eventtime):
        return self._tool() is not None
    def _extruder(self, tool):
        return self.printer.lookup_object(
            'extruder%s' % (tool if tool else ''), None)
    def nozzle_max(self):
        # numeric_input_config nozzle_temperature: 15 under the heater's limit
        ext = self._extruder(self._tool() or 0)
        return int(ext.get_heater().max_temp) - 15 if ext else 290
    def bed_max(self):
        return 115                           # BED_MAXTEMP 120 less its margin
    def read_speed(self, eventtime):
        return int(self.gcode_move.get_status(eventtime)['speed_factor']
                   * 100. + .5)
    def send_speed(self, v):
        self.run_gcode('M220 S%d' % (v,))
    def read_flow(self, eventtime):
        return int(self.gcode_move.get_status(eventtime)['extrude_factor']
                   * 100. + .5)
    def send_flow(self, v):
        self.run_gcode('M221 S%d' % (v,))
    def read_nozzle(self, eventtime):
        tool = self._tool()
        ext = self._extruder(tool) if tool is not None else None
        return int(ext.get_status(eventtime)['target'] + .5) if ext else 0
    def send_nozzle(self, v):
        tool = self._tool()
        if tool is not None:
            self.run_gcode('M104 T%d S%d' % (tool, v))
    def read_bed(self, eventtime):
        bed = self.printer.lookup_object('heater_bed', None)
        return int(bed.get_status(eventtime)['target'] + .5) if bed else 0
    def send_bed(self, v):
        self.run_gcode('M140 S%d' % (v,))
    def read_fan(self, eventtime):
        tc = self.printer.lookup_object('xl_toolchanger', None)
        return int(tc.print_fan_speed * 100. + .5) if tc else 0
    def send_fan(self, v):
        self.run_gcode('M106 S%.1f' % (v * 255. / 100.,))
    def read_live_z(self, eventtime):
        origin = self.gcode_move.get_status(eventtime)['homing_origin']
        return round(origin.z, 3)
    # Mainsail's babystep: one command at a time, the clicks that come in
    # meanwhile go together, so a fast knob never builds a queue
    def send_live_z(self, change):
        if self.demo is not None:
            return
        self.z_pending += change
        if not self.z_running:
            self.z_running = True
            self.reactor.register_callback(self._run_live_z)
    def _run_live_z(self, eventtime):
        try:
            while abs(self.z_pending) > 1e-6:
                change, self.z_pending = self.z_pending, 0.
                self._run_gcode('SET_GCODE_OFFSET Z_ADJUST=%.4f MOVE=1'
                                % (change,))
        finally:
            self.z_running = False
    def _heater(self, temp, target, cold, eventtime):
        cur, tgt = int(temp + .5), int(target + .5)
        text = ('%d/%d\xb0C' % (cur, tgt)) if tgt else ('%d\xb0C' % (cur,))
        color = None
        if tgt - cur > HEAT_DIFF:
            color = BRAND                            # heating
        elif tgt - cur < -HEAT_DIFF and cur > cold:
            color = AZURE                            # cooling
        if color is not None and int(eventtime / TICK) % 2:
            return text, color                       # blink: colour / white
        return text, WHITE
    def _footer_items(self, eventtime):
        tc = self.printer.lookup_object('xl_toolchanger', None)
        tool = tc.active_tool if tc is not None else -1
        if tool is None or tool < 0:
            nozzle, ftype = ('---', WHITE), '---'
        else:
            ext = self.printer.lookup_object(
                'extruder%s' % (tool if tool else ''), None)
            st = ext.get_status(eventtime) if ext else {}
            nozzle = self._heater(st.get('temperature', 0.),
                                  st.get('target', 0.), NOZZLE_COLD, eventtime)
            ftype = self.tool_filament(tool)[0] or '---'
        bed = self.printer.lookup_object('heater_bed', None)
        st = bed.get_status(eventtime) if bed else {}
        bed_text = self._heater(st.get('temperature', 0.),
                                st.get('target', 0.), BED_COLD, eventtime)
        pup = self.printer.lookup_object('xl_puppies', None)
        grid = []
        for gx in range(4):
            for gy in range(4):
                i = xl_puppies.BEDLET_MAP[gy][gx]
                if pup is not None and pup.bed_targets[i] > 0:
                    grid.append(2)                   # heating
                elif pup is not None and pup.bed_measured.get(i, 0.) > BED_COLD:
                    grid.append(1)                   # warm
                else:
                    grid.append(0)
        return [('nozzle_16x16',) + nozzle,
                ('bed', bed_text[0], bed_text[1], tuple(grid)),
                ('spool_16x16', ftype, WHITE),
                ('enclosure_16x16', '---', WHITE)]
    def _draw_footer_item(self, item, x):
        y = FOOTER[1]
        if item[0] == 'bed':
            # XL: heatbed icon hidden, 4x4 bedlets at a 4 px pitch, front
            # row at the bottom (footer_items_nozzle_bed.cpp:46-71)
            for n, state in enumerate(item[3]):
                gx, gy = divmod(n, 4)
                px, py = x + gx * 4, y + 16 - 4 - gy * 4
                if state == 1:
                    for dx, dy, w, h in ((1, 0, 1, 3), (0, 1, 1, 1),
                                         (2, 1, 1, 1)):
                        self._fill(px + dx, py + dy, w, h, BRAND)
                else:
                    self._fill(px, py, 3, 3, BRAND if state else GRAY)
        else:
            self._icon(item[0], x, y)
        self._text(item[1], x + 16 + 3, y, item[2])
    def _draw_footer(self, eventtime):
        items = self._footer_items(eventtime)
        widths = [16 + 3 + FW * len(it[1]) for it in items]
        xs = split(widths, FOOTER[0], FOOTER[2])
        if self.footer is None or xs != self.footer[0]:
            self._fill(*FOOTER)
            for it, x in zip(items, xs):
                self._draw_footer_item(it, x)
        else:
            for it, old, x, w in zip(items, self.footer[1], xs, widths):
                if it != old:
                    self._fill(x, FOOTER[1], w, FOOTER[3])
                    self._draw_footer_item(it, x)
        self.footer = (xs, items)
    def _tick(self, eventtime):
        if self.printer.is_shutdown():
            return self.reactor.NEVER
        try:
            with self.lock:
                self._watch_print(eventtime)
                self._watch_filament(eventtime)
                if time.strftime('%H:%M') != self.clock:
                    self._draw_clock()
                self.page.tick(eventtime)
                if self.page.footer:
                    self._draw_footer(eventtime)
                self._sync()
        except Exception:
            logging.exception("xl_display: update failed")
        return eventtime + TICK
    # Knob and press: clicks faster than the drawing only add up, and one task
    # at a time hands them to the page on the screen
    def _pump_later(self):
        if self.ready and not self.pump_pending:
            self.pump_pending = True
            self.reactor.register_callback(self._pump)
    def _pump(self, eventtime):
        with self.lock:
            self.pump_pending = False
            while ((self.knob_steps or self.presses)
                   and not self.printer.is_shutdown()):
                steps, self.knob_steps = self.knob_steps, 0
                presses, self.presses = self.presses, 0
                try:
                    if steps:
                        self.page.knob(steps)
                    for _ in range(presses):
                        self.page.press()
                except Exception:
                    logging.exception("xl_display: knob failed")
            self._sync()
    def _knob_cw(self, eventtime):
        self.knob_steps += 1
        self._pump_later()
    def _knob_ccw(self, eventtime):
        self.knob_steps -= 1
        self._pump_later()
    def _knob_press(self, eventtime):
        self.presses += 1
        self._pump_later()
    cmd_XL_DISPLAY_DEMO_help = ("Show the screens to look at them, nothing"
                                " moves: STATE=menus (every tile), filament,"
                                " printing, paused, done, stopped or off."
                                "  Their buttons send no G-code")
    def cmd_XL_DISPLAY_DEMO(self, gcmd):
        if not self.ready or self.lcd_ver < 2:
            raise gcmd.error("The XL screen has no print screen: flash the"
                             " current XLBuddy firmware")
        state = gcmd.get('STATE', 'printing').lower()
        states = {'printing': PRINTING, 'paused': PAUSED, 'done': PRINTED,
                  'stopped': STOPPED, 'filament': MENU_DEMO,
                  'menus': MENU_DEMO, 'off': None}
        if state not in states:
            raise gcmd.error("STATE: menus, filament, printing, paused, done,"
                             " stopped or off")
        real = self.print_stats.get_status(self.reactor.monotonic())['state']
        if real in ('printing', 'paused'):
            raise gcmd.error("A print is running")
        with self.lock:
            self.demo = states[state]
            self.demo_filament = DemoFilament(self)
            self.change = None
            self.change_type = ''
            if self.demo is None:
                self.go_home()
            elif self.demo == MENU_DEMO:
                self.stack, self.page, self.print_page = [], self.home, None
                if state == 'filament':
                    self.open_page(FilamentPage(self))
                else:
                    self._draw_page(self.reactor.monotonic())
            else:
                self._show_print_page()
            self._sync()
    cmd_XL_DISPLAY_REDRAW_help = ("Redraw the XL's screen (COUNT=n times,"
                                  " a load test for the XLBuddy)")
    def cmd_XL_DISPLAY_REDRAW(self, gcmd):
        if not self.ready:
            raise gcmd.error("The XL screen is not running")
        count = gcmd.get_int('COUNT', 1, minval=1, maxval=100)
        start = self.reactor.monotonic()
        for i in range(count):
            with self.lock:
                self._draw_page(self.reactor.monotonic())
                self._sync()
        gcmd.respond_info("XL screen redrawn %d times in %.2f s"
                          % (count, self.reactor.monotonic() - start))
    def get_status(self, eventtime=None):
        return {'focus': LABELS[self.home.focus], 'ready': self.ready,
                'page': type(self.page).__name__}


def load_config(config):
    return XLDisplay(config)
