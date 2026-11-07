#!/usr/bin/env python3
# KlipperXL-Plus - pack the XL screen's look (Prusa's own icons and font)
# into firmware/xlbuddy/xl_lcd_assets.bin; the XLBuddy build turns it into
# xlplus_lcd_assets.c (gen_xl_lcd_assets.py), so no build needs Pillow.
#
# Sources (Prusa-Firmware-Buddy 6.10.3, GPLv3):
#   src/gui/res/png/<name>.png                      icons
#   src/module/gui/font_data/png/LiberationMono-<style>_<w>x<h>.png
#       16 characters per row from 0x20, glyph opacity (255 - avg) >> 4,
#       pure red pixels are guide dots (font_data/font.py).  Prusa's fonts
#       (gui/fonts.cpp): small regular 9x16, normal bold 11x19, big bold
#       13x22, large bold 30x53 - the large one only draws the print screen's
#       percentage, so its digits are stored as white-on-black icons big_0...
# Icons are blended onto black once (Prusa: (a*fg + (255-a)*bg) >> 8,
# everything on the home screen sits on black) and stored b, g, r - the
# order the ILI9488 takes them (ili9488.cpp:680-682).
#
# Usage (any python3 with Pillow, e.g. the Pi's system python3):
#   build_xl_lcd_assets.py <prusa png dir> <prusa font png dir> <out .bin>
#
# Copyright (C) 2026  Richard Crook
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import base64, json, os, sys, zlib
from PIL import Image

ICONS = ['%s_80x80%s' % (n, v)
         for n in ('print', 'preheat', 'spool', 'calibrate', 'settings', 'info')
         for v in ('', '_focused', '_disabled')]
ICONS += ['%s_80x80%s' % (n, v)
          for n in ('pause', 'stop', 'resume', 'home', 'reprint')
          for v in ('', '_focused', '_disabled')]
ICONS += ['home_shape_16x16', 'lan_16x16', 'usb_20x16', 'nozzle_16x16',
          'spool_16x16', 'enclosure_16x16', 'heatbed_16x16', 'print_16x16',
          'settings_16x16', 'preheat_white_16x16', 'spool_white_16x16',
          'calibrate_white_16x16', 'info_16x16', 'move_16x16',
          'home_full_16x16', 'folder_up_16x16', 'folder_full_16x16',
          'arrow_right_10x16', 'warning_48x48']
CHARS = [chr(c) for c in range(0x20, 0x7F)] + ['\xb0']
FONTS = {'small': ('regular', 9, 16), 'normal': ('bold', 11, 19),
         'big': ('bold', 13, 22)}
BIG = ('bold', 30, 53)
BIG_CHARS = '0123456789%'


def icon_bgr(path):
    im = Image.open(path).convert('RGBA')
    out = bytearray()
    for r, g, b, a in im.getdata():
        out += bytes(((a * b) >> 8, (a * g) >> 8, (a * r) >> 8))
    return im.size, bytes(out)


def glyph_levels(px, ch, fw, fh):
    row, col = divmod(ord(ch) - 0x20, 16)
    lv = bytearray()
    for j in range(fh):
        for i in range(fw):
            r, g, b = px[col * fw + i, row * fh + j]
            lv.append(0 if (r, g, b) == (255, 0, 0)
                      else (255 - (r + g + b) // 3) >> 4)
    return bytes(lv)


def atlas(font_dir, style, fw, fh):
    path = os.path.join(font_dir, 'LiberationMono-%s_%dx%d.png'
                        % (style, fw, fh))
    return Image.open(path).convert('RGB').load()


def glyphs(font_dir, style, fw, fh):
    px = atlas(font_dir, style, fw, fh)
    return {str(ord(ch)):
            base64.b64encode(glyph_levels(px, ch, fw, fh)).decode()
            for ch in CHARS}


def main():
    png_dir, font_dir, out_path = sys.argv[1:4]
    data = {'icons': {}, 'fonts': {}}
    for name, (style, fw, fh) in FONTS.items():
        data['fonts'][name] = {'w': fw, 'h': fh,
                               'glyphs': glyphs(font_dir, style, fw, fh)}
    for name in ICONS:
        (w, h), bgr = icon_bgr(os.path.join(png_dir, name + '.png'))
        data['icons'][name] = [w, h, base64.b64encode(bgr).decode()]
    # the large digits: white on black, the blend of xlplus_lcd.c's text
    style, fw, fh = BIG
    px = atlas(font_dir, style, fw, fh)
    for ch in BIG_CHARS:
        out = bytearray()
        for lv in glyph_levels(px, ch, fw, fh):
            c = ((255 * lv // 15) * 255) >> 8 if lv else 0
            out += bytes((c, c, c))
        data['icons']['big_%s' % (ch,)] = [
            fw, fh, base64.b64encode(bytes(out)).decode()]
    with open(out_path, 'wb') as f:
        f.write(zlib.compress(json.dumps(data).encode(), 9))
    print("%s: %d icons, %d fonts" % (out_path, len(data['icons']),
                                      len(data['fonts'])))


if __name__ == '__main__':
    main()
