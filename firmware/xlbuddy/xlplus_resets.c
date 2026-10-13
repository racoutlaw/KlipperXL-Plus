// KlipperXL+ - Dwarf / modular bed reset lines on the Prusa XL XLBuddy
//
// The reset lines of the five Dwarfs and the modular bed sit on a PCA9557
// i2c expander (FACTS.md section 5).  They are driven here with Klipper's
// own stm32 i2c driver because:
//   - every tool and the bed must be HELD in reset from power-on until the
//     host boots them in order (bed, then T0 -> T4), before any host exists;
//   - the host boots the Dwarfs while this MCU is identified but not yet
//     configured, when a configured [i2c] object cannot be used yet.
//
// Copyright (C) 2026  Richard Crook
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include "board/gpio.h" // i2c_setup
#include "command.h" // DECL_COMMAND
#include "sched.h" // DECL_INIT

#define PCA9557_BUS        6       // i2c2_PF1_PF0 (src/stm32/i2c.c), SDA PF0 SCL PF1
#define PCA9557_ADDR       0x19    // fw4 l.58
#define PCA9557_RATE       100000
#define PCA9557_REG_OUTPUT 0x01
#define PCA9557_REG_CONFIG 0x03

// Output bits (fw4 l.1089-1092): bit1..5 = Dwarf 1..5, bit7 = modular bed,
// bit6 = fan power switch (low, as fw4 left it), bit0 = unused dwarf6.
// HIGH = held in reset.
#define HOLD_ALL           0xBE    // Dwarfs 1-5 + bed held, fan bit low

DECL_CONSTANT("XL_RESET_HOLD_ALL", HOLD_ALL);

static struct i2c_config pca;
static uint8_t pca_value;

static int
pca_write(uint8_t reg, uint8_t value)
{
    uint8_t msg[2] = { reg, value };
    return i2c_write(pca, sizeof(msg), msg);
}

void
xl_resets_init(void)
{
    pca = i2c_setup(PCA9557_BUS, PCA9557_RATE, PCA9557_ADDR);
    // Output register first, then make the pins outputs, so no line is
    // ever released on the way (same order as fw4 l.1096-1099).
    pca_value = HOLD_ALL;
    pca_write(PCA9557_REG_OUTPUT, pca_value);
    pca_write(PCA9557_REG_CONFIG, 0x00);
}
DECL_INIT(xl_resets_init);

// Set all reset lines at once.  Reply carries the i2c status (0 = ok).
void
command_xl_reset_lines(uint32_t *args)
{
    uint8_t value = args[0];
    int ret = pca_write(PCA9557_REG_OUTPUT, value);
    if (!ret)
        pca_value = value;
    sendf("xl_reset_lines_result status=%c value=%c", ret ? 1 : 0, pca_value);
}
DECL_COMMAND(command_xl_reset_lines, "xl_reset_lines value=%c");
