// KlipperXL-Plus - the Prusa XL's own colour screen (ILI9488, 480x320)
//
// The host (klippy/extras/xl_display.py) lays out Prusa's screens; the
// pixels are made here.  Prusa's icons and font are stored in this firmware
// (xlplus_lcd_assets.c, generated at build time from xl_lcd_assets.bin by
// gen_xl_lcd_assets.py), so a tile swap is "draw icon 3 at x,y" over USB
// instead of 19 KB of pixels (owner 10/28: software SPI from the Pi painted
// each tile visibly and made the side LEDs stutter).
//
// Wiring (Prusa hwio_pindef_XL.h:114-116, hal_msp.cpp:616-624, FACTS.md 9):
// SPI6 MISO PG12 / SCK PG13 / MOSI PG14 (AF5), CS PD11, RS PD15, RST PG4.
// The XL's chip is an STM32F427 - SPI6 sits at APB2 + 0x5400 with RCC
// APB2ENR bit 21 - but Klipper builds it as an F407, whose header has no
// SPI6, so it is named here.
//
// PG14 also carries the WS2812 data of the side and front LED strips (Klipper
// neopixel, a bit-banged GPIO output) and PE9 + CS pick the target:
// CS LOW + PE9 LOW = display only, CS LOW + PE9 HIGH would feed the side
// strip too, CS HIGH = a strip (PE9: side / front) sees PG14.  So every
// command here: PE9 low, then per ILI9488 command CS low + PG14 to SPI6 ...
// PG14 a GPIO output held LOW + CS high, and at the end PE9 as it was.
// Commands run one at a time in Klipper's command task, so an LED update
// between two of them finds the pins as it left them.
//
// One command draws at most LCD_MAX_BYTES (about 1.2 ms at 21 MHz), so
// nothing else on this MCU (RS485 bridge, USB, ADC reports) waits longer;
// the host splits bigger things into rows.  Each ILI9488 command is framed
// as Prusa's ili9488_cmd (CS L, RS L, command, RS H, data, CS H - holding
// CS low lost the byte alignment, printer 10/28) and pixels go b, g, r
// (Prusa ili9488.cpp:680-682).
//
// Copyright (C) 2026  Richard Crook
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include "board/gpio.h" // gpio_out_setup
#include "board/internal.h" // gpio_peripheral, enable_pclock
#include "command.h" // DECL_COMMAND
#include "sched.h" // DECL_SHUTDOWN

#define LCD_SPI_BASE  (APB2PERIPH_BASE + 0x5400UL)   // SPI6 (STM32F427)
#define LCD_SPI       ((SPI_TypeDef *)LCD_SPI_BASE)
#define LCD_SPI_HZ    21000000        // Prusa: 20 MHz on panels that need it
#define LCD_READ_HZ   10000000        // Prusa reads at 10 MHz (ili9488_cmd_rd)
#define LCD_W         480
#define LCD_H         320
#define LCD_MAX_BYTES 3072
#define MOSI_PIN      GPIO('G', 14)
#define SELECT_BIT    (1 << 9)        // PE9, the LED / display selector

// xlplus_lcd_assets.c: per icon {width, height, offset, size} into the
// run-length data (see gen_xl_lcd_assets.py); per font 96 glyphs at 4 bits a
// pixel: 0 small 9x16, 1 normal bold 11x19, 2 big bold 13x22 (Prusa's fonts)
extern const uint8_t xl_lcd_icon_count;
extern const uint32_t xl_lcd_icon_index[][4];
extern const uint8_t xl_lcd_icon_data[];
extern const uint8_t xl_lcd_font_count;
extern const uint8_t xl_lcd_font_w[], xl_lcd_font_h[];
extern const uint16_t xl_lcd_font_bytes[];
extern const uint8_t * const xl_lcd_font_data[];

// The host's xl_display.py turns the pages on from this version: 2 = fonts,
// colour-swapped icons (the print screen and the menus, owner 11/06)
DECL_CONSTANT("XL_LCD_VER", 2);

static struct gpio_out cs, rs, rst;
static uint8_t lcd_setup_done, select_was_high, cs_low;

static uint32_t
spi_divider(uint32_t hz)
{
    uint32_t pclk = get_pclock_frequency(LCD_SPI_BASE), br = 0;
    while (br < 7 && (pclk >> (br + 1)) > hz)
        br++;
    return br << SPI_CR1_BR_Pos;
}

static void
spi_set_speed(uint32_t hz)
{
    LCD_SPI->CR1 = 0;
    LCD_SPI->CR1 = SPI_CR1_MSTR | SPI_CR1_SSM | SPI_CR1_SSI | spi_divider(hz);
    LCD_SPI->CR1 |= SPI_CR1_SPE;
}

static void
lcd_setup(void)
{
    if (lcd_setup_done)
        return;
    enable_pclock(LCD_SPI_BASE);
    gpio_peripheral(GPIO('G', 12), GPIO_FUNCTION(5), 1);   // MISO
    gpio_peripheral(GPIO('G', 13), GPIO_FUNCTION(5), 0);   // SCK
    cs = gpio_out_setup(GPIO('D', 11), 1);
    rs = gpio_out_setup(GPIO('D', 15), 1);
    rst = gpio_out_setup(GPIO('G', 4), 1);                 // not in reset
    spi_set_speed(LCD_SPI_HZ);
    lcd_setup_done = 1;
}

static void
spi_wait(void)
{
    while (!(LCD_SPI->SR & SPI_SR_TXE))
        ;
    while (LCD_SPI->SR & SPI_SR_BSY)
        ;
    (void)LCD_SPI->DR;                                     // clear overrun
    (void)LCD_SPI->SR;
}

// Whenever CS is high a strip sees PG14 (PE9 picks which), so PG14 is SPI6
// MOSI only while CS is low and otherwise a GPIO output held LOW (printer
// 10/28: MOSI left high after a byte, at a CS-high gap between two screen
// commands, latched a wrong colour into the front strip)
static void
mosi_low(void)
{
    GPIOG->BSRR = (1 << 14) << 16;                         // ODR low first
    gpio_peripheral(MOSI_PIN, GPIO_OUTPUT, 0);
}

static void
lcd_select(void)
{
    gpio_out_write(cs, 0);
    gpio_peripheral(MOSI_PIN, GPIO_FUNCTION(5), 0);        // PG14 = SPI6 MOSI
    cs_low = 1;
}

static void
lcd_deselect(void)
{
    spi_wait();
    mosi_low();
    gpio_out_write(cs, 1);
    cs_low = 0;
}

// Take PG14 from the LED strips for one command, and give it back
static void
lcd_begin(void)
{
    lcd_setup();
    mosi_low();
    select_was_high = !!(GPIOE->ODR & SELECT_BIT);
    GPIOE->BSRR = SELECT_BIT << 16;                        // PE9 low
}

static void
lcd_end(void)
{
    if (cs_low)
        lcd_deselect();
    if (select_was_high)
        GPIOE->BSRR = SELECT_BIT;
}

static inline void
spi_put(uint8_t b)
{
    while (!(LCD_SPI->SR & SPI_SR_TXE))
        ;
    *(volatile uint8_t *)&LCD_SPI->DR = b;
}

static inline void
put_pixel(uint8_t b, uint8_t g, uint8_t r)
{
    spi_put(b);
    spi_put(g);
    spi_put(r);
}

// Prusa ili9488_cmd: CS L, RS L, command, RS H, data, CS H
static void
lcd_cmd(uint8_t cmd, const uint8_t *data, uint32_t len)
{
    lcd_select();
    gpio_out_write(rs, 0);
    spi_put(cmd);
    spi_wait();
    gpio_out_write(rs, 1);
    while (len--)
        spi_put(*data++);
    lcd_deselect();
}

// A drawing the screen cannot take is dropped, never a shutdown: a screen
// mistake must not stop a print.  The host clips before it sends.
static int
rect_ok(uint32_t x, uint32_t y, uint32_t w, uint32_t h)
{
    return w && h && x + w <= LCD_W && y + h <= LCD_H
        && w * h * 3 <= LCD_MAX_BYTES;
}

// Column / row window, then RAMWR with CS left low: pixels follow
static void
lcd_window(uint32_t x, uint32_t y, uint32_t w, uint32_t h)
{
    uint32_t x1 = x + w - 1, y1 = y + h - 1;
    uint8_t col[4] = { x >> 8, x, x1 >> 8, x1 };
    uint8_t row[4] = { y >> 8, y, y1 >> 8, y1 };
    lcd_cmd(0x2A, col, sizeof(col));
    lcd_cmd(0x2B, row, sizeof(row));
    lcd_select();
    gpio_out_write(rs, 0);
    spi_put(0x2C);
    spi_wait();
    gpio_out_write(rs, 1);
}

// Raw ILI9488 command with its data (wake, MADCTL, COLMOD, ...)
void
command_xl_lcd_cmd(uint32_t *args)
{
    uint8_t len = args[1];
    uint8_t *data = command_decode_ptr(args[2]);
    lcd_begin();
    lcd_cmd(args[0], data, len);
    lcd_end();
}
DECL_COMMAND(command_xl_lcd_cmd, "xl_lcd_cmd cmd=%c data=%*s");

// One colour (0xRRGGBB) over a rectangle
void
command_xl_lcd_fill(uint32_t *args)
{
    uint32_t w = args[2], h = args[3], color = args[4];
    if (!rect_ok(args[0], args[1], w, h))
        return;
    lcd_begin();
    lcd_window(args[0], args[1], w, h);
    uint8_t r = color >> 16, g = color >> 8, b = color;
    for (uint32_t n = w * h; n; n--)
        put_pixel(b, g, r);
    lcd_end();
}
DECL_COMMAND(command_xl_lcd_fill,
             "xl_lcd_fill x=%hu y=%hu w=%hu h=%hu color=%u");

// An icon pixel, on a focused (white) menu row with black and white swapped:
// Prusa's ROPFN_SWAPBW (qoi_decoder.cpp swapbw, SWAPBW_TOLERANCE 64) inverts a
// pixel near grey and leaves a coloured one alone
static inline void
put_icon_pixel(const uint8_t *p, uint32_t swap)
{
    uint32_t b = p[0], g = p[1], r = p[2];
    if (swap) {
        uint32_t l = (r + g + b) / 3;
        uint32_t l0 = l >= 64 ? l - 64 : 0, l1 = l <= 255 - 64 ? l + 64 : 255;
        if (l0 <= r && r <= l1 && l0 <= g && g <= l1 && l0 <= b && b <= l1) {
            b = 255 - b;
            g = 255 - g;
            r = 255 - r;
        }
    }
    put_pixel(b, g, r);
}

// Rows [row, row + rows) of stored icon 'id', its top left at x, y.  The
// run-length data: a byte c < 128 = c + 1 literal pixels follow, c >= 128
// = the next pixel c - 126 times; pixels are b, g, r on black.
static void
draw_icon(uint32_t id, uint32_t x, uint32_t y, uint32_t row, uint32_t rows
          , uint32_t swap)
{
    if (id >= xl_lcd_icon_count)
        return;
    const uint32_t *ix = xl_lcd_icon_index[id];
    uint32_t w = ix[0];
    if (row + rows > ix[1] || !rect_ok(x, y + row, w, rows))
        return;
    lcd_begin();
    lcd_window(x, y + row, w, rows);
    const uint8_t *p = &xl_lcd_icon_data[ix[2]];
    uint32_t skip = row * w, left = rows * w;
    while (left) {
        uint32_t c = *p++;
        if (c < 128) {
            uint32_t n = c + 1;
            for (; n && skip; n--, skip--)
                p += 3;
            for (; n && left; n--, left--, p += 3)
                put_icon_pixel(p, swap);
            p += 3 * n;
        } else {
            uint32_t n = c - 126, s = n < skip ? n : skip;
            n -= s;
            skip -= s;
            for (; n && left; n--, left--)
                put_icon_pixel(p, swap);
            p += 3;
        }
    }
    lcd_end();
}

void
command_xl_lcd_icon(uint32_t *args)
{
    draw_icon(args[0], args[1], args[2], args[3], args[4], 0);
}
DECL_COMMAND(command_xl_lcd_icon,
             "xl_lcd_icon id=%c x=%hu y=%hu row=%hu rows=%hu");

void
command_xl_lcd_icon_swap(uint32_t *args)
{
    draw_icon(args[0], args[1], args[2], args[3], args[4], 1);
}
DECL_COMMAND(command_xl_lcd_icon_swap,
             "xl_lcd_icon_swap id=%c x=%hu y=%hu row=%hu rows=%hu");

// Text in one of Prusa's LiberationMono fonts: each pixel blended between bg
// and fg (0xRRGGBB) by its 16-level glyph value, as Prusa's display_ex.cpp.
// Characters 0x20-0x7e and 0xb0 (degree); anything else draws '?'.
static void
draw_text(uint32_t font, uint32_t x, uint32_t y, uint32_t fg, uint32_t bg
          , uint32_t len, const uint8_t *text)
{
    if (font >= xl_lcd_font_count)
        return;
    uint32_t fw = xl_lcd_font_w[font], fh = xl_lcd_font_h[font];
    uint32_t nbytes = xl_lcd_font_bytes[font];
    const uint8_t *data = xl_lcd_font_data[font];
    if (!rect_ok(x, y, len * fw, fh))
        return;
    uint8_t lut[16][3];
    for (uint32_t lv = 0; lv < 16; lv++) {
        uint32_t a = 255 * lv / 15;
        for (uint32_t c = 0; c < 3; c++) {       // b, g, r
            uint32_t f = (fg >> (8 * c)) & 0xff, k = (bg >> (8 * c)) & 0xff;
            lut[lv][c] = lv ? (a * f + (255 - a) * k) >> 8 : k;
        }
    }
    lcd_begin();
    lcd_window(x, y, len * fw, fh);
    for (uint32_t j = 0; j < fh; j++) {
        for (uint32_t k = 0; k < len; k++) {
            uint32_t ch = text[k];
            uint32_t gi = (ch >= 0x20 && ch < 0x7f) ? ch - 0x20
                          : (ch == 0xb0 ? 95 : '?' - 0x20);
            const uint8_t *g = &data[gi * nbytes];
            for (uint32_t i = 0; i < fw; i++) {
                uint32_t n = j * fw + i, v = g[n >> 1];
                uint32_t lv = (n & 1) ? v & 0x0f : v >> 4;
                put_pixel(lut[lv][0], lut[lv][1], lut[lv][2]);
            }
        }
    }
    lcd_end();
}

void
command_xl_lcd_text(uint32_t *args)
{
    draw_text(0, args[0], args[1], args[2], args[3], args[4]
              , command_decode_ptr(args[5]));
}
DECL_COMMAND(command_xl_lcd_text,
             "xl_lcd_text x=%hu y=%hu fg=%u bg=%u text=%*s");

void
command_xl_lcd_text_font(uint32_t *args)
{
    draw_text(args[0], args[1], args[2], args[3], args[4], args[5]
              , command_decode_ptr(args[6]));
}
DECL_COMMAND(command_xl_lcd_text_font,
             "xl_lcd_text_font font=%c x=%hu y=%hu fg=%u bg=%u text=%*s");

// Read one ILI9488 register as Prusa's ili9488_cmd_rd: CS L, RS L, the
// command and 3 dummy bytes at 10 MHz; the answer is byte 1
void
command_xl_lcd_read(uint32_t *args)
{
    uint8_t tx[4] = { args[0], 0, 0, 0 }, rx[4];
    lcd_begin();
    spi_set_speed(LCD_READ_HZ);
    (void)LCD_SPI->DR;
    (void)LCD_SPI->SR;
    lcd_select();
    gpio_out_write(rs, 0);
    for (uint32_t i = 0; i < sizeof(tx); i++) {
        spi_put(tx[i]);
        while (!(LCD_SPI->SR & SPI_SR_RXNE))
            ;
        rx[i] = LCD_SPI->DR;
    }
    lcd_deselect();
    gpio_out_write(rs, 1);
    spi_set_speed(LCD_SPI_HZ);
    lcd_end();
    sendf("xl_lcd_read_result cmd=%c value=%c", tx[0], rx[1]);
}
DECL_COMMAND(command_xl_lcd_read, "xl_lcd_read cmd=%c");

// A shutdown can stop a command half way: give PG14 back to the LEDs
void
xl_lcd_shutdown(void)
{
    if (!lcd_setup_done)
        return;
    mosi_low();
    gpio_out_write(cs, 1);
    cs_low = 0;
}
DECL_SHUTDOWN(xl_lcd_shutdown);
