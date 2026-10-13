// KlipperXL - MODBUS Master + Puppy Bootloader for Prusa XL
// Copyright (C) 2026 Richard Crook
//
// Based on Klipper 3D Printer Firmware
//   Copyright (C) 2016-2024 Kevin O'Connor <kevin@koconnor.net>
// Portions derived from Prusa-Firmware-Buddy
//   Copyright (C) 2019-2024 Prusa Research a.s. - www.prusa3d.com
//
// This program is free software: you can redistribute it and/or modify
// it under the terms of the GNU General Public License as published by
// the Free Software Foundation, either version 3 of the License, or
// (at your option) any later version.
//
// This program is distributed in the hope that it will be useful,
// but WITHOUT ANY WARRANTY; without even the implied warranty of
// MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
// GNU General Public License for more details.
//
// You should have received a copy of the GNU General Public License
// along with this program. If not, see <https://www.gnu.org/licenses/>.
//
// For STM32F407 (XLBuddy board)

#include <string.h>
#include "autoconf.h"
#include "board/gpio.h"
#include "board/irq.h"
#include "board/misc.h"
#include "basecmd.h"
#include "command.h"
#include "sched.h"
#include "trsync.h"
#include "board/internal.h"
#include "board/armcm_boot.h"

// Forward declarations
void USART3_IRQHandler(void);
void watchdog_reset(void);
void usb_bulk_in_task(void);  // USB TX flush (src/generic/usb_cdc.c:43)
void usb_bulk_out_task(void); // USB RX + dispatch (src/generic/usb_cdc.c:103)

/****************************************************************
 * Configuration
 ****************************************************************/

// RS485 UART for Puppy communication
#define MODBUS_USART           USART3
#define MODBUS_USART_IRQn      USART3_IRQn
#define MODBUS_BAUD            230400

// RS485 direction control (active high = TX)
#define RS485_DIR_PORT         GPIOG
#define RS485_DIR_PIN          1

// I2C2 for PCA9557 (PF0=SDA, PF1=SCL)
#define PCA9557_I2C            I2C2
#define PCA9557_ADDR           0x19  // 7-bit address (0x32 >> 1)

// PCA9557 registers
#define PCA9557_REG_INPUT      0x00
#define PCA9557_REG_OUTPUT     0x01
#define PCA9557_REG_POLARITY   0x02
#define PCA9557_REG_CONFIG     0x03

// Side filament sensors (sandwich board, ADC3 via mux)
// Matches Prusa XLBuddy hardware: 2x2 analog mux on ADC3
// ADC channels: PC0 = ADC3_IN10 (mux_x), PF6 = ADC3_IN4 (mux_y)
// Mux select: PF12 = setA, PG6 = setB
// Mux positions:
//   pos 0 (A=0,B=0): X=sfs1(T0), Y=sfs2(T1)
//   pos 1 (A=1,B=0): X=sfs3(T2), Y=sfs4(unused)
//   pos 2 (A=0,B=1): X=sfs5(T4), Y=sfs6(T3)
//   pos 3 (A=1,B=1): X=sandwich_temp, Y=ambient_temp
#define SFS_MUX_A_PORT    GPIOF
#define SFS_MUX_A_PIN     12
#define SFS_MUX_B_PORT    GPIOG
#define SFS_MUX_B_PIN     6
#define SFS_ADC_X_CHANNEL 10    // PC0 = ADC3 channel 10
#define SFS_ADC_Y_CHANNEL 4     // PF6 = ADC3 channel 4

// Bootloader protocol
#define BOOTLOADER_CMD_GET_PROTOCOL_VERSION  0x00
#define BOOTLOADER_CMD_SET_ADDRESS           0x01
#define BOOTLOADER_CMD_GET_HARDWARE_INFO     0x03
#define BOOTLOADER_CMD_START_APPLICATION     0x05
#define BOOTLOADER_CMD_GET_FINGERPRINT       0x0E
#define BOOTLOADER_CMD_COMPUTE_FINGERPRINT   0x0F

#define BOOTLOADER_ADDR_DEFAULT    0x00
#define BOOTLOADER_ADDR_FIRST      0x0A
#define MODBUS_ADDR_OFFSET         0x1A

// Buffer sizes
#define MODBUS_TX_BUF_SIZE     256
#define MODBUS_RX_BUF_SIZE     256

// Timeouts - must stay under Klipper's ~500ms watchdog
#define MODBUS_RESPONSE_TIMEOUT_MS  100    // 100ms max total response wait
#define MODBUS_INTER_FRAME_US       3000   // 3ms inter-frame gap between transactions
#define MODBUS_BYTE_TIMEOUT_US      100000 // 100ms max gap between bytes within a response
#define MODBUS_FIRST_BYTE_US        100000 // 100ms wait for first response byte
#define BOOTLOADER_TIMEOUT_MS       400    // 400ms for bootloader commands

/****************************************************************
 * Error Codes
 ****************************************************************/

#define MODBUS_OK              0
#define MODBUS_ERR_TIMEOUT     1
#define MODBUS_ERR_CRC         2
#define MODBUS_ERR_EXCEPTION   3
#define MODBUS_ERR_FRAME       4
#define MODBUS_ERR_BUSY        0xFE  // Disambiguate from bootloader status=5

/****************************************************************
 * Global State
 ****************************************************************/

static struct {
    uint8_t tx_buf[MODBUS_TX_BUF_SIZE];
    uint8_t rx_buf[MODBUS_RX_BUF_SIZE];
    volatile uint16_t tx_pos;
    volatile uint16_t tx_len;
    volatile uint16_t rx_pos;
    volatile uint8_t state;
    volatile uint8_t error;
    uint32_t last_activity;
    uint32_t rx_start_time;
    uint32_t tx_count;
    uint32_t rx_count;
    uint32_t timeout_count;
    uint32_t crc_error_count;
    uint8_t initialized;
    uint8_t dwarfs_booted;
    uint8_t discovered_dwarfs;  // Bitmask of discovered dwarfs
    uint32_t rx_total_bytes;    // Total bytes received by ISR (debug)
    uint32_t rx_errors;         // Total UART errors (debug)
    uint32_t rx_state0_bytes;   // Bytes received in state 0 (idle)
    uint32_t rx_state1_bytes;   // Bytes received in state 1 (TX) - echo
    uint32_t rx_state2_bytes;   // Bytes received in state 2 (RX) - stored
    uint32_t rx_max_pos;        // Max rx_pos ever seen
} modbus;

/****************************************************************
 * CRC16 Table (polynomial 0xA001, init 0x0000 for bootloader)
 ****************************************************************/

static const uint16_t crc_table[256] = {
    0x0000, 0xC0C1, 0xC181, 0x0140, 0xC301, 0x03C0, 0x0280, 0xC241,
    0xC601, 0x06C0, 0x0780, 0xC741, 0x0500, 0xC5C1, 0xC481, 0x0440,
    0xCC01, 0x0CC0, 0x0D80, 0xCD41, 0x0F00, 0xCFC1, 0xCE81, 0x0E40,
    0x0A00, 0xCAC1, 0xCB81, 0x0B40, 0xC901, 0x09C0, 0x0880, 0xC841,
    0xD801, 0x18C0, 0x1980, 0xD941, 0x1B00, 0xDBC1, 0xDA81, 0x1A40,
    0x1E00, 0xDEC1, 0xDF81, 0x1F40, 0xDD01, 0x1DC0, 0x1C80, 0xDC41,
    0x1400, 0xD4C1, 0xD581, 0x1540, 0xD701, 0x17C0, 0x1680, 0xD641,
    0xD201, 0x12C0, 0x1380, 0xD341, 0x1100, 0xD1C1, 0xD081, 0x1040,
    0xF001, 0x30C0, 0x3180, 0xF141, 0x3300, 0xF3C1, 0xF281, 0x3240,
    0x3600, 0xF6C1, 0xF781, 0x3740, 0xF501, 0x35C0, 0x3480, 0xF441,
    0x3C00, 0xFCC1, 0xFD81, 0x3D40, 0xFF01, 0x3FC0, 0x3E80, 0xFE41,
    0xFA01, 0x3AC0, 0x3B80, 0xFB41, 0x3900, 0xF9C1, 0xF881, 0x3840,
    0x2800, 0xE8C1, 0xE981, 0x2940, 0xEB01, 0x2BC0, 0x2A80, 0xEA41,
    0xEE01, 0x2EC0, 0x2F80, 0xEF41, 0x2D00, 0xEDC1, 0xEC81, 0x2C40,
    0xE401, 0x24C0, 0x2580, 0xE541, 0x2700, 0xE7C1, 0xE681, 0x2640,
    0x2200, 0xE2C1, 0xE381, 0x2340, 0xE101, 0x21C0, 0x2080, 0xE041,
    0xA001, 0x60C0, 0x6180, 0xA141, 0x6300, 0xA3C1, 0xA281, 0x6240,
    0x6600, 0xA6C1, 0xA781, 0x6740, 0xA501, 0x65C0, 0x6480, 0xA441,
    0x6C00, 0xACC1, 0xAD81, 0x6D40, 0xAF01, 0x6FC0, 0x6E80, 0xAE41,
    0xAA01, 0x6AC0, 0x6B80, 0xAB41, 0x6900, 0xA9C1, 0xA881, 0x6840,
    0x7800, 0xB8C1, 0xB981, 0x7940, 0xBB01, 0x7BC0, 0x7A80, 0xBA41,
    0xBE01, 0x7EC0, 0x7F80, 0xBF41, 0x7D00, 0xBDC1, 0xBC81, 0x7C40,
    0xB401, 0x74C0, 0x7580, 0xB541, 0x7700, 0xB7C1, 0xB681, 0x7640,
    0x7200, 0xB2C1, 0xB381, 0x7340, 0xB101, 0x71C0, 0x7080, 0xB041,
    0x5000, 0x90C1, 0x9181, 0x5140, 0x9301, 0x53C0, 0x5280, 0x9241,
    0x9601, 0x56C0, 0x5780, 0x9741, 0x5500, 0x95C1, 0x9481, 0x5440,
    0x9C01, 0x5CC0, 0x5D80, 0x9D41, 0x5F00, 0x9FC1, 0x9E81, 0x5E40,
    0x5A00, 0x9AC1, 0x9B81, 0x5B40, 0x9901, 0x59C0, 0x5880, 0x9841,
    0x8801, 0x48C0, 0x4980, 0x8941, 0x4B00, 0x8BC1, 0x8A81, 0x4A40,
    0x4E00, 0x8EC1, 0x8F81, 0x4F40, 0x8D01, 0x4DC0, 0x4C80, 0x8C41,
    0x4400, 0x84C1, 0x8581, 0x4540, 0x8701, 0x47C0, 0x4680, 0x8641,
    0x8201, 0x42C0, 0x4380, 0x8341, 0x4100, 0x81C1, 0x8081, 0x4040,
};

/****************************************************************
 * CRC Calculation
 ****************************************************************/

static uint16_t
calc_crc16(uint8_t *data, uint16_t len)
{
    uint16_t crc = 0xFFFF;  // Init 0xFFFF for both bootloader AND MODBUS (CRC16-IBM)
    for (uint16_t i = 0; i < len; i++) {
        crc = (crc >> 8) ^ crc_table[(crc ^ data[i]) & 0xFF];
    }
    return crc;
}

/****************************************************************
 * RS485 Direction Control
 ****************************************************************/

static inline void rs485_tx_enable(void) {
    RS485_DIR_PORT->BSRR = (1 << RS485_DIR_PIN);
}

static inline void rs485_rx_enable(void) {
    RS485_DIR_PORT->BSRR = (1 << (RS485_DIR_PIN + 16));
}

/****************************************************************
 * Simple Delay
 ****************************************************************/

static void delay_us(uint32_t us)
{
    uint32_t end = timer_read_time() + timer_from_us(us);
    while (timer_is_before(timer_read_time(), end)) {
        // busy wait
    }
}

/****************************************************************
 * Hardware I2C2 for PCA9557 (PF0=SDA, PF1=SCL)
 ****************************************************************/

// I2C pins: PF0 = SDA, PF1 = SCL
#define I2C_SDA_PORT  GPIOF
#define I2C_SDA_PIN   0
#define I2C_SCL_PORT  GPIOF
#define I2C_SCL_PIN   1

static void hw_i2c_init(void)
{
    // Enable clocks
    RCC->AHB1ENR |= RCC_AHB1ENR_GPIOFEN;
    RCC->APB1ENR |= RCC_APB1ENR_I2C2EN;
    
    for (volatile int i = 0; i < 10000; i++) {}  // Wait for clocks
    
    // Configure PF0 (SDA) and PF1 (SCL) for I2C2 (AF4)
    GPIOF->MODER &= ~((3 << (0 * 2)) | (3 << (1 * 2)));
    GPIOF->MODER |= (2 << (0 * 2)) | (2 << (1 * 2));  // Alternate function
    GPIOF->OTYPER |= (1 << 0) | (1 << 1);  // Open-drain
    GPIOF->PUPDR &= ~((3 << (0 * 2)) | (3 << (1 * 2)));
    GPIOF->PUPDR |= (1 << (0 * 2)) | (1 << (1 * 2));  // Pull-up
    GPIOF->AFR[0] &= ~((0xF << (0 * 4)) | (0xF << (1 * 4)));
    GPIOF->AFR[0] |= (4 << (0 * 4)) | (4 << (1 * 4));  // AF4 = I2C2
    
    // Reset I2C2
    I2C2->CR1 = I2C_CR1_SWRST;
    for (volatile int i = 0; i < 1000; i++) {}
    I2C2->CR1 = 0;
    
    // Configure I2C2: 100kHz from 42MHz APB1
    I2C2->CR2 = 42;  // APB1 = 42MHz
    I2C2->CCR = 210;  // 42MHz / (2 * 100kHz) = 210
    I2C2->TRISE = 43;  // (42MHz / 1MHz) + 1
    
    // Enable I2C
    I2C2->CR1 = I2C_CR1_PE;
}

static int hw_i2c_write(uint8_t addr, uint8_t reg, uint8_t value)
{
    int timeout;
    
    // Wait for bus free
    timeout = 10000;
    while ((I2C2->SR2 & I2C_SR2_BUSY) && --timeout > 0) {}
    if (timeout == 0) return -1;
    
    // Start
    I2C2->CR1 |= I2C_CR1_START;
    timeout = 10000;
    while (!(I2C2->SR1 & I2C_SR1_SB) && --timeout > 0) {}
    if (timeout == 0) return -2;
    
    // Send address (write)
    I2C2->DR = addr << 1;
    timeout = 10000;
    while (!(I2C2->SR1 & I2C_SR1_ADDR) && --timeout > 0) {
        if (I2C2->SR1 & I2C_SR1_AF) {
            I2C2->CR1 |= I2C_CR1_STOP;
            I2C2->SR1 &= ~I2C_SR1_AF;
            return -3;  // NACK
        }
    }
    if (timeout == 0) return -4;
    (void)I2C2->SR2;  // Clear ADDR
    
    // Send register
    I2C2->DR = reg;
    timeout = 10000;
    while (!(I2C2->SR1 & I2C_SR1_TXE) && --timeout > 0) {}
    if (timeout == 0) return -5;
    
    // Send value
    I2C2->DR = value;
    timeout = 10000;
    while (!(I2C2->SR1 & I2C_SR1_TXE) && --timeout > 0) {}
    if (timeout == 0) return -6;
    timeout = 10000;
    while (!(I2C2->SR1 & I2C_SR1_BTF) && --timeout > 0) {}
    if (timeout == 0) return -7;
    
    // Stop
    I2C2->CR1 |= I2C_CR1_STOP;
    
    return 0;
}


/****************************************************************
 * USART3 Initialization
 ****************************************************************/

static void modbus_uart_init(void)
{
    if (modbus.initialized)
        return;
    
    // Enable clocks
    RCC->AHB1ENR |= RCC_AHB1ENR_GPIODEN;
    RCC->AHB1ENR |= RCC_AHB1ENR_GPIOGEN;
    RCC->APB1ENR |= RCC_APB1ENR_USART3EN;

    // Configure PD8 (TX) and PD9 (RX) as alternate function AF7
    GPIOD->MODER &= ~(GPIO_MODER_MODER8 | GPIO_MODER_MODER9);
    GPIOD->MODER |= (2 << GPIO_MODER_MODER8_Pos) | (2 << GPIO_MODER_MODER9_Pos);
    GPIOD->AFR[1] &= ~((0xF << 0) | (0xF << 4));
    GPIOD->AFR[1] |= (7 << 0) | (7 << 4);
    GPIOD->OSPEEDR |= GPIO_OSPEEDER_OSPEEDR8 | GPIO_OSPEEDER_OSPEEDR9;
    // Add pull-up on RX pin
    GPIOD->PUPDR &= ~GPIO_PUPDR_PUPD9;
    GPIOD->PUPDR |= (1 << GPIO_PUPDR_PUPD9_Pos);  // Pull-up

    // Configure PG1 as output for RS485 direction
    GPIOG->MODER &= ~GPIO_MODER_MODER1;
    GPIOG->MODER |= (1 << GPIO_MODER_MODER1_Pos);
    GPIOG->OSPEEDR |= GPIO_OSPEEDER_OSPEEDR1;
    rs485_rx_enable();

    // Configure USART3: 230400 baud @ 42MHz APB1
    MODBUS_USART->CR1 = 0;
    MODBUS_USART->BRR = 182;  // 42000000 / 230400 ≈ 182
    MODBUS_USART->CR1 = USART_CR1_TE | USART_CR1_RE;  // No RXNEIE - use polling
    MODBUS_USART->CR2 = 0;
    MODBUS_USART->CR3 = 0;
    MODBUS_USART->CR1 |= USART_CR1_UE;

    armcm_enable_irq(USART3_IRQHandler, MODBUS_USART_IRQn, 2);

    modbus.initialized = 1;
}

/****************************************************************
 * USART3 Interrupt Handler
 ****************************************************************/

void USART3_IRQHandler(void)
{
    uint32_t sr = MODBUS_USART->SR;

    if ((sr & USART_SR_TXE) && (MODBUS_USART->CR1 & USART_CR1_TXEIE)) {
        if (modbus.tx_pos < modbus.tx_len) {
            MODBUS_USART->DR = modbus.tx_buf[modbus.tx_pos++];
        } else {
            MODBUS_USART->CR1 &= ~USART_CR1_TXEIE;
            MODBUS_USART->CR1 |= USART_CR1_TCIE;
        }
    }

    if ((sr & USART_SR_TC) && (MODBUS_USART->CR1 & USART_CR1_TCIE)) {
        MODBUS_USART->CR1 &= ~USART_CR1_TCIE;
        // Switch to RX mode
        rs485_rx_enable();
        // Small delay for transceiver to settle
        for (volatile int i = 0; i < 100; i++) {}
        // Drain all echo bytes (DR + shift register).
        // Must use 3-stage pattern: drain DR, clear ORE, drain again.
        // Single SR+DR read leaves last echo byte in DR (see
        // serial_bridge_write lines 2220-2228 for the working template).
        while (MODBUS_USART->SR & USART_SR_RXNE)
            (void)MODBUS_USART->DR;
        (void)MODBUS_USART->SR;
        (void)MODBUS_USART->DR;
        while (MODBUS_USART->SR & USART_SR_RXNE)
            (void)MODBUS_USART->DR;
        // Now ready to receive - polling will handle RX (no RXNEIE)
        modbus.state = 2;  // Receiving
        modbus.rx_pos = 0;
        modbus.rx_start_time = timer_read_time();
        modbus.last_activity = timer_read_time();
    }

    // RX is handled by polling in send_frame_wait - don't touch DR here
}

/****************************************************************
 * Send Frame and Wait for Response
 ****************************************************************/

static int
send_frame_wait(uint8_t *data, uint16_t len, uint8_t *response, uint16_t *resp_len, uint16_t max_resp, uint32_t timeout_ms, uint8_t bootloader_resp)
{
    watchdog_reset();  // Keep watchdog alive for long bootloader ops (400ms)
    if (!modbus.initialized)
        modbus_uart_init();
    if (modbus.state != 0)
        return MODBUS_ERR_BUSY;
    if (len > MODBUS_TX_BUF_SIZE)
        return MODBUS_ERR_FRAME;

    // Wait for inter-frame gap
    while (timer_read_time() - modbus.last_activity < timer_from_us(MODBUS_INTER_FRAME_US)) {
        // busy wait
    }

    // Flush any stale UART data before transmitting (3-stage drain)
    while (MODBUS_USART->SR & USART_SR_RXNE)
        (void)MODBUS_USART->DR;
    (void)MODBUS_USART->SR;
    (void)MODBUS_USART->DR;
    while (MODBUS_USART->SR & USART_SR_RXNE)
        (void)MODBUS_USART->DR;

    // Copy data to TX buffer
    memcpy(modbus.tx_buf, data, len);
    modbus.tx_len = len;
    modbus.tx_pos = 0;
    modbus.rx_pos = 0;
    modbus.state = 1;  // Transmitting
    modbus.error = MODBUS_OK;

    // Disable USB IRQ for entire TX+RX cycle to prevent byte overrun.
    // USB ISR (priority 1) preempts USART3 polling, causing ORE at
    // 230400 baud on single-byte STM32F4 USART.  Previously disabled
    // only after state==2 (RX), but that left a race window between
    // TC ISR setting state=2 and the polling loop's next iteration.
    NVIC_DisableIRQ(OTG_FS_IRQn);
    int usb_irq_disabled = 1;

    // Start transmission
    irq_disable();
    rs485_tx_enable();
    delay_us(10);  // Small delay for transceiver
    MODBUS_USART->DR = modbus.tx_buf[modbus.tx_pos++];
    MODBUS_USART->CR1 |= USART_CR1_TXEIE;
    irq_enable();

    modbus.tx_count++;

    // Wait for response using pure polling (no RX interrupts)
    uint32_t timeout = timer_read_time() + timer_from_us(timeout_ms * 1000);
    // First byte must arrive within timeout, otherwise no response.
    // Bootloader ops get the full timeout — page erase on STM32G070
    // can take 5-10ms+ on top of TX time, easily exceeding 100ms.
    uint32_t first_byte_timeout = timer_read_time() + timer_from_us(
        bootloader_resp ? timeout_ms * 1000 : MODBUS_FIRST_BYTE_US);

    while (modbus.state != 0) {

        // Overall timeout
        if (timer_is_before(timeout, timer_read_time())) {
            irq_disable();  // Prevent race with ISR

            // CRITICAL FIX: Release DE pin to RX mode!
            // Without this, DE stays high and blocks ALL future RX
            rs485_rx_enable();

            // Disable any pending UART interrupts
            MODBUS_USART->CR1 &= ~(USART_CR1_TXEIE | USART_CR1_TCIE);

            // Clear any pending flags
            (void)MODBUS_USART->SR;
            (void)MODBUS_USART->DR;

            modbus.state = 0;
            modbus.timeout_count++;

            irq_enable();

            if (modbus.rx_pos > 0 && response && resp_len) {
                uint16_t copy_len = modbus.rx_pos;
                if (copy_len > max_resp) copy_len = max_resp;
                memcpy(response, modbus.rx_buf, copy_len);
                *resp_len = copy_len;
            } else if (resp_len) {
                *resp_len = 0;
            }
            if (usb_irq_disabled)
                NVIC_EnableIRQ(OTG_FS_IRQn);
            return MODBUS_ERR_TIMEOUT;
        }

        // First byte timeout: if nothing received in 200ms, give up
        if (modbus.state == 2 && modbus.rx_pos == 0) {
            if (timer_is_before(first_byte_timeout, timer_read_time())) {
                irq_disable();

                // CRITICAL FIX: Ensure DE pin is in RX mode
                rs485_rx_enable();
                MODBUS_USART->CR1 &= ~(USART_CR1_TXEIE | USART_CR1_TCIE);
                (void)MODBUS_USART->SR;
                (void)MODBUS_USART->DR;

                modbus.state = 0;
                modbus.timeout_count++;

                irq_enable();

                if (resp_len) *resp_len = 0;
                if (usb_irq_disabled)
                    NVIC_EnableIRQ(OTG_FS_IRQn);
                return MODBUS_ERR_TIMEOUT;
            }
        }

        // Poll UART for received bytes
        if (modbus.state == 2) {
            uint32_t sr = MODBUS_USART->SR;
            if (sr & USART_SR_RXNE) {
                uint8_t data = MODBUS_USART->DR;
                modbus.rx_total_bytes++;
                if (modbus.rx_pos < MODBUS_RX_BUF_SIZE) {
                    modbus.rx_buf[modbus.rx_pos++] = data;
                    modbus.last_activity = timer_read_time();
                    if (modbus.rx_pos > modbus.rx_max_pos)
                        modbus.rx_max_pos = modbus.rx_pos;
                }
            } else if (sr & (USART_SR_ORE | USART_SR_FE | USART_SR_NE)) {
                // Clear error by reading SR then DR
                (void)MODBUS_USART->DR;
                modbus.rx_errors++;
            }
        }

        // Protocol-aware response detection:
        // MODBUS exception: [addr][func|0x80][exception_code][crc16] = 5 bytes
        // FC 0x06 (Write Single Register): [addr][func][reg_hi][reg_lo][val_hi][val_lo][crc16] = 8 bytes
        // FC 0x10 (Write Multiple Regs):   [addr][func][reg_hi][reg_lo][qty_hi][qty_lo][crc16] = 8 bytes
        // MODBUS read resp: [addr][func][byte_count][data...][crc16]
        // FC 0x18 (FIFO):   [addr][0x18][byte_count_hi][byte_count_lo][...][crc16]
        // Bootloader resp:  [addr][status][len][data...][crc16]
        if (modbus.state == 2 && modbus.rx_pos >= 3) {
            uint16_t expected_len = 0;
            if (bootloader_resp) {
                // Bootloader response: [addr][status][len][data...][crc16]
                uint8_t payload_len = modbus.rx_buf[2];
                expected_len = 3 + payload_len + 2;
            } else {
            uint8_t func_code = modbus.rx_buf[1];
            if (func_code & 0x80) {
                // MODBUS exception response - always 5 bytes
                expected_len = 5;
            } else if (func_code == 0x05 || func_code == 0x06 || func_code == 0x0F || func_code == 0x10) {
                // FC 0x05 Write Single Coil / FC 0x06 Write Single Register
                // FC 0x0F Write Multiple Coils / FC 0x10 Write Multiple Registers
                // Response is echo: [addr][func][addr_hi][addr_lo][val_or_qty_hi][val_or_qty_lo][crc16]
                expected_len = 8;
            } else if (func_code == 0x18) {
                // FC 0x18 Read FIFO Queue - 2-byte byte count
                if (modbus.rx_pos >= 4) {
                    uint16_t byte_count = ((uint16_t)modbus.rx_buf[2] << 8)
                                        | modbus.rx_buf[3];
                    expected_len = 4 + byte_count + 2;  // addr+func+bc(2)+data+crc(2)
                }
                // else: need more bytes before we can determine length
            } else {
                // Default: treat byte[2] as payload length
                uint8_t payload_len = modbus.rx_buf[2];
                expected_len = 3 + payload_len + 2;
            }
            } // end else (MODBUS parsing)
            if (expected_len > 0) {
                if (expected_len > MODBUS_RX_BUF_SIZE) expected_len = MODBUS_RX_BUF_SIZE;
                if (modbus.rx_pos >= expected_len) {
                    modbus.state = 3;  // Frame complete
                    break;
                }
            }
        }

        // Byte gap timeout: if bytes started but gap > 500ms, frame done
        if (modbus.state == 2 && modbus.rx_pos > 0) {
            if (timer_read_time() - modbus.last_activity > timer_from_us(MODBUS_BYTE_TIMEOUT_US)) {
                modbus.state = 3;  // Frame complete (gap timeout)
                break;
            }
        }
    }

    // Re-enable USB IRQ after RX polling complete
    if (usb_irq_disabled)
        NVIC_EnableIRQ(OTG_FS_IRQn);

    // Validate CRC on received frame
    if (modbus.rx_pos >= 5) {
        uint16_t expected_crc = calc_crc16(modbus.rx_buf, modbus.rx_pos - 2);
        uint16_t received_crc = modbus.rx_buf[modbus.rx_pos - 2]
                              | (modbus.rx_buf[modbus.rx_pos - 1] << 8);
        if (expected_crc != received_crc) {
            modbus.crc_error_count++;
            modbus.state = 0;
            // Return raw data even on CRC error for debugging
            if (response && resp_len && modbus.rx_pos > 0) {
                uint16_t copy_len = modbus.rx_pos;
                if (copy_len > max_resp) copy_len = max_resp;
                memcpy(response, modbus.rx_buf, copy_len);
                *resp_len = copy_len;
            } else if (resp_len) {
                *resp_len = 0;
            }
            // Inter-frame delay
            uint32_t gap_end = timer_read_time() + timer_from_us(MODBUS_INTER_FRAME_US);
            while (timer_is_before(timer_read_time(), gap_end)) {}
            return MODBUS_ERR_CRC;
        }
    }

    // Copy response
    if (response && resp_len && modbus.rx_pos > 0) {
        uint16_t copy_len = modbus.rx_pos;
        if (copy_len > max_resp) copy_len = max_resp;
        memcpy(response, modbus.rx_buf, copy_len);
        *resp_len = copy_len;
        modbus.rx_count++;
    } else if (resp_len) {
        *resp_len = 0;
    }

    modbus.state = 0;

    // Inter-frame delay before next transaction
    uint32_t gap_end = timer_read_time() + timer_from_us(MODBUS_INTER_FRAME_US);
    while (timer_is_before(timer_read_time(), gap_end)) {}

    return MODBUS_OK;
}

#define NUM_DWARFS          5
#define ALL_DWARF_BITS      0x3E  // bits 1-5 for Dwarfs 1-5
/****************************************************************
 * Side Filament Sensors - ADC3 with analog mux (Prusa sandwich board)
 *
 * Hardware: 5 filament sensors multiplexed onto 2 ADC3 channels
 * via 2 GPIO select pins. Matches Prusa XLBuddy hardware exactly.
 *
 * Mux positions (setA=PF12, setB=PG6):
 *   pos 0 (A=0,B=0): X(PC0)=sfs1(T0), Y(PF6)=sfs2(T1)
 *   pos 1 (A=1,B=0): X(PC0)=sfs3(T2), Y(PF6)=sfs4(unused)
 *   pos 2 (A=0,B=1): X(PC0)=sfs5(T4), Y(PF6)=sfs6(T3)
 ****************************************************************/

// Side sensor latest readings (12-bit ADC, 0-4095)
static uint16_t side_fs_values[5] = {0};

static void side_fs_init(void)
{
    // Enable GPIO clocks for mux select and ADC pins
    RCC->AHB1ENR |= RCC_AHB1ENR_GPIOCEN | RCC_AHB1ENR_GPIOFEN
                   | RCC_AHB1ENR_GPIOGEN;
    // Enable ADC3 clock (APB2)
    RCC->APB2ENR |= RCC_APB2ENR_ADC3EN;
    for (volatile int i = 0; i < 1000; i++) {}

    // Configure mux select pins as push-pull outputs
    // PF12 (setA)
    SFS_MUX_A_PORT->MODER &= ~(3 << (SFS_MUX_A_PIN * 2));
    SFS_MUX_A_PORT->MODER |= (1 << (SFS_MUX_A_PIN * 2));   // Output
    SFS_MUX_A_PORT->OTYPER &= ~(1 << SFS_MUX_A_PIN);       // Push-pull
    SFS_MUX_A_PORT->BSRR = (1 << (SFS_MUX_A_PIN + 16));    // Start LOW
    // PG6 (setB)
    SFS_MUX_B_PORT->MODER &= ~(3 << (SFS_MUX_B_PIN * 2));
    SFS_MUX_B_PORT->MODER |= (1 << (SFS_MUX_B_PIN * 2));   // Output
    SFS_MUX_B_PORT->OTYPER &= ~(1 << SFS_MUX_B_PIN);       // Push-pull
    SFS_MUX_B_PORT->BSRR = (1 << (SFS_MUX_B_PIN + 16));    // Start LOW

    // Configure ADC input pins as analog mode
    // PC0 = ADC3_IN10
    GPIOC->MODER |= (3 << (0 * 2));    // Analog mode (0b11)
    // PF6 = ADC3_IN4
    GPIOF->MODER |= (3 << (6 * 2));    // Analog mode (0b11)

    // Configure ADC3
    ADC3->CR1 = 0;                      // 12-bit resolution, no scan
    ADC3->CR2 = ADC_CR2_ADON;           // Enable ADC
    ADC3->SMPR1 = 0x07 << (0 * 3);     // CH10: 480 cycles sample time
    ADC3->SMPR2 = 0x07 << (4 * 3);     // CH4: 480 cycles sample time
    ADC3->SQR1 = 0;                     // 1 conversion in sequence
}

// Read a single ADC3 channel (blocking, ~15us)
static uint16_t side_fs_read_channel(uint8_t channel)
{
    ADC3->SQR3 = channel;               // Select channel
    ADC3->CR2 |= ADC_CR2_SWSTART;       // Start conversion
    while (!(ADC3->SR & ADC_SR_EOC)) {}  // Wait for completion
    return (uint16_t)(ADC3->DR & 0xFFF); // Read 12-bit result
}

// Set mux position and read both X and Y channels
static void side_fs_read_mux_pos(uint8_t setA, uint8_t setB,
                                  uint16_t *val_x, uint16_t *val_y)
{
    // Set mux select pins
    if (setA)
        SFS_MUX_A_PORT->BSRR = (1 << SFS_MUX_A_PIN);       // HIGH
    else
        SFS_MUX_A_PORT->BSRR = (1 << (SFS_MUX_A_PIN + 16)); // LOW
    if (setB)
        SFS_MUX_B_PORT->BSRR = (1 << SFS_MUX_B_PIN);       // HIGH
    else
        SFS_MUX_B_PORT->BSRR = (1 << (SFS_MUX_B_PIN + 16)); // LOW

    // Wait for mux to settle (~10us)
    for (volatile int i = 0; i < 200; i++) {}

    // Read both channels
    *val_x = side_fs_read_channel(SFS_ADC_X_CHANNEL);
    *val_y = side_fs_read_channel(SFS_ADC_Y_CHANNEL);
}

// Read all 5 side filament sensors via mux cycling
static void side_fs_read_all(void)
{
    uint16_t x, y;

    // Pos 0 (A=0,B=0): X=sfs1(T0), Y=sfs5(T4)
    side_fs_read_mux_pos(0, 0, &x, &y);
    side_fs_values[0] = x;  // T0 = sfs1
    side_fs_values[4] = y;  // T4 = sfs5

    // Pos 1 (A=1,B=0): X=sfs2(T1), Y=sfs6(T3)
    side_fs_read_mux_pos(1, 0, &x, &y);
    side_fs_values[1] = x;  // T1 = sfs2
    side_fs_values[3] = y;  // T3 = sfs6

    // Pos 2 (A=0,B=1): X=sfs3(T2), Y=sandwich_temp(unused)
    side_fs_read_mux_pos(0, 1, &x, &y);
    side_fs_values[2] = x;  // T2 = sfs3

    // Return mux to pos 0
    SFS_MUX_A_PORT->BSRR = (1 << (SFS_MUX_A_PIN + 16));  // A=LOW
    SFS_MUX_B_PORT->BSRR = (1 << (SFS_MUX_B_PIN + 16));  // B=LOW
}

// Klipper command: query all 5 side filament sensors
void command_query_side_filament_sensors(uint32_t *args)
{
    side_fs_read_all();
    sendf("side_filament_sensors_result s0=%u s1=%u s2=%u s3=%u s4=%u",
          side_fs_values[0], side_fs_values[1], side_fs_values[2],
          side_fs_values[3], side_fs_values[4]);
}
DECL_COMMAND(command_query_side_filament_sensors,
             "query_side_filament_sensors");

/****************************************************************
 * Early Init - Runs at MCU power-on before Klipper connects
 ****************************************************************/

static uint8_t early_init_done = 0;


void
modbus_early_init(void)
{
    // Enable GPIO clocks first
    RCC->AHB1ENR |= RCC_AHB1ENR_GPIOEEN;
    RCC->AHB1ENR |= RCC_AHB1ENR_GPIOFEN;  // For I2C
    RCC->AHB1ENR |= RCC_AHB1ENR_GPIOGEN;
    
    // Delay for clocks
    for (volatile int i = 0; i < 20000; i++) {}
    
    // PE7 = GPIOReset - Prusa sets this HIGH (open-drain)
    // This might be a master reset or enable for the I2C expander
    // Configure PE7 as open-drain output, set HIGH
    GPIOE->OTYPER |= (1 << 7);  // Open-drain
    GPIOE->MODER = (GPIOE->MODER & ~(3 << (7 * 2))) | (1 << (7 * 2));  // Output mode
    GPIOE->BSRR = (1 << 7);  // Set HIGH
    
    // PE14 = splitter5vEnable - powers Dwarfs 3-6 on splitter board
    // Configure PE14 as output, set HIGH to enable 5V to splitter
    GPIOE->MODER = (GPIOE->MODER & ~(3 << (14 * 2))) | (1 << (14 * 2));  // Output mode
    GPIOE->BSRR = (1 << 14);  // Set HIGH to enable splitter 5V
    
    // Small delay after enabling power
    for (volatile int i = 0; i < 10000; i++) {}

    // Initialize side filament sensor ADC3 + mux
    side_fs_init();

    // Initialize hardware I2C2
    hw_i2c_init();
    
    // PCA9557 I2C expander controls Dwarf reset pins
    // Pin mapping: p0=dwarf6, p1=dwarf1, p2=dwarf2, p3=dwarf3, p4=dwarf4, p5=dwarf5
    // p6=fanPowerSwitch, p7=modularBedReset
    // LOW = release from reset, HIGH = hold in reset

    // Release all Dwarfs from reset so they boot into bootloader (BLUE LED).
    // KlipperXL+: the host boots the bed first, then each Klipper Dwarf through
    // its Prusa bootloader (modbus_send_raw_boot) - nothing more happens here.
    hw_i2c_write(PCA9557_ADDR, PCA9557_REG_OUTPUT, 0x00);
    for (volatile int i = 0; i < 5000; i++) {}
    // Configure all pins as outputs (0 = output)
    hw_i2c_write(PCA9557_ADDR, PCA9557_REG_CONFIG, 0x00);
    
    // Mark early init done
    early_init_done = 1;
    modbus.dwarfs_booted = 0;
}
DECL_INIT(modbus_early_init);

/****************************************************************
 * Klipper Commands
 ****************************************************************/

DECL_CONSTANT("HAVE_MODBUS_MASTER", 1);

void command_config_modbus(uint32_t *args)
{
    // Just init UART - boot sequence is driven from Python host
    modbus_uart_init();
}
DECL_COMMAND(command_config_modbus, "config_modbus");

// Set PCA9557 output register (controls Dwarf reset pins)
// Called from Python to isolate Dwarfs during boot sequence
void command_pca9557_output(uint32_t *args)
{
    uint8_t value = args[0];
    int ret = hw_i2c_write(PCA9557_ADDR, PCA9557_REG_OUTPUT, value);
    sendf("pca9557_output_response status=%c value=%c", ret == 0 ? 0 : 1, value);
}
DECL_COMMAND(command_pca9557_output, "pca9557_output value=%c");

// Send raw bytes and receive response
void command_modbus_send_raw(uint32_t *args)
{
    uint8_t data_len = args[0];
    uint8_t *data = command_decode_ptr(args[1]);

    uint8_t response[128];
    uint16_t resp_len = 0;

    int ret = send_frame_wait(data, data_len, response, &resp_len, sizeof(response), MODBUS_RESPONSE_TIMEOUT_MS, 0);

    // Cap response to fit within MESSAGE_MAX (64 bytes)
    uint16_t send_len = resp_len;
    if (send_len > 50) send_len = 50;
    sendf("modbus_raw_response status=%c data=%*s", ret, send_len, response);
}
DECL_COMMAND(command_modbus_send_raw, "modbus_send_raw data=%*s");

// Send raw RS485 frame with bootloader-aware response parsing.
// Identical to modbus_send_raw but uses bootloader response format
// [addr][status][len][data...][crc16] instead of MODBUS function codes.
void command_modbus_send_raw_boot(uint32_t *args)
{
    uint8_t data_len = args[0];
    uint8_t *data = command_decode_ptr(args[1]);

    uint8_t response[128];
    uint16_t resp_len = 0;

    int ret = send_frame_wait(data, data_len, response, &resp_len, sizeof(response), MODBUS_RESPONSE_TIMEOUT_MS, 1);

    uint16_t send_len = resp_len;
    if (send_len > 50) send_len = 50;
    sendf("modbus_raw_boot_response status=%c data=%*s", ret, send_len, response);
}
DECL_COMMAND(command_modbus_send_raw_boot, "modbus_send_raw_boot data=%*s");


void command_modbus_status(uint32_t *args)
{
    sendf("modbus_status_response tx=%u rx=%u timeout=%u crc_err=%u init=%c booted=%c dwarfs=%c rx_bytes=%u rx_errs=%u s0=%u s1=%u maxpos=%u",
          modbus.tx_count, modbus.rx_count,
          modbus.timeout_count, modbus.crc_error_count,
          modbus.initialized, modbus.dwarfs_booted, modbus.discovered_dwarfs,
          modbus.rx_total_bytes, modbus.rx_errors,
          modbus.rx_state0_bytes, modbus.rx_state1_bytes, modbus.rx_max_pos);
}
DECL_COMMAND(command_modbus_status, "modbus_status");



/****************************************************************
 * Task - Check for response timeout
 ****************************************************************/

void modbus_task(void)
{
    if (modbus.state == 2) {  // Receiving
        uint32_t now = timer_read_time();
        if (now - modbus.last_activity > timer_from_us(MODBUS_BYTE_TIMEOUT_US)) {
            modbus.state = 3;
        }
    }
}
DECL_TASK(modbus_task);

/****************************************************************
 * Serial Bridge - Relay Klipper serial frames to/from Dwarf MCUs
 *
 * Supports up to 5 Dwarfs sharing the RS485 bus via multiprocessor
 * addressing.  Each bridge_write sends an address byte (0x80|dwarf)
 * before the Klipper frame data.  The addressed Dwarf processes
 * the frame; others stay muted via USART idle line detection.
 *
 * Blocking design: each command blocks for the full TX→RX cycle
 * (~2-7ms). watchdog_reset() is called before the RX poll.
 *
 * Wire protocol: [3ms idle gap] [0x80|dwarf] [klipper_frame_bytes]
 * Address bytes 0x81-0x85 can never collide with Klipper message
 * length bytes (always < 0x80 / CONFIG_MESSAGE_MAX = 64).
 *
 * Protocol towards the host, SERIAL_BRIDGE_PROTOCOL=2 (2026-10-02,
 * research/multitool/RESULT-fix5.md).  Protocol 1 (the 03-02 build)
 * answered a write with serial_bridge_read chunks, or one empty read
 * when nothing came back, and the host took the block's ack as the
 * end of the reply.  That ordering does not hold once the host's
 * blocks are dispatched from inside the command (BRIDGE_PUMP_USB_OUT):
 * the host's next block is then dispatched, and acked, before this
 * command returns, and the ack is cumulative (src/command.c:364-374;
 * serialqueue.c update_receive_seq pops every older block).  So:
 *  - every serial_bridge_write is answered by zero or more
 *    serial_bridge_read chunks of <= BRIDGE_RX_CHUNK bytes followed by
 *    exactly one serial_bridge_done carrying a status and the number
 *    of reply bytes the chunks add up to.  'done' ends the reply.
 *  - status != BRIDGE_DONE_OK means nothing (or not all of the frame)
 *    went out on the bus and tx_buf is empty: the host re-sends the
 *    whole block, serial_bridge_append included.
 *  - chunks are streamed while the reply is still arriving.  The USB
 *    transmit buffer is 192 bytes (src/generic/usb_cdc.c:34) and
 *    console_sendf() silently drops a message that does not fit
 *    (usb_cdc.c:70-74); four chunks of a 192 byte reply would not.
 *  - the RX capture ends on a byte gap (or when rx_buf is full), not
 *    on a clock, so a Dwarf's queued reports in front of its answer
 *    are no longer cut off.
 ****************************************************************/

#define MAX_BRIDGE_DWARFS 5
#define BRIDGE_RX_CHUNK   54    // bytes of reply per serial_bridge_read

DECL_CONSTANT("SERIAL_BRIDGE_PROTOCOL", 2);

// serial_bridge_done status values
#define BRIDGE_DONE_OK         0  // frame sent, total = reply bytes streamed
#define BRIDGE_DONE_BAD_DWARF  1  // dwarf index outside 1..MAX_BRIDGE_DWARFS
#define BRIDGE_DONE_REFUSED    2  // another write was in flight: nothing sent
#define BRIDGE_DONE_DROPPED    3  // the preceding append was refused: nothing sent
#define BRIDGE_DONE_BUS_BUSY   4  // bus claim timed out (50ms): nothing sent
#define BRIDGE_DONE_TX_FAIL    5  // TX wedged (tx_bailout): frame incomplete

static struct {
    uint8_t rx_buf[192];        // RS485 RX buffer for this Dwarf
    uint8_t rx_pos;            // Current RX position
    uint8_t rx_sent;           // Bytes already sent to host
    uint8_t tx_buf[72];         // Accumulated TX data from bridge_append
    uint8_t tx_pos;             // Current TX accumulation position
    uint8_t tx_dropped;         // An append was refused (see in_bridge_write)
} serial_bridge[MAX_BRIDGE_DWARFS];

// Fix 5: re-entrancy guard.  bridge_pump_usb() below can run the usb
// tasks from inside command_serial_bridge_write.  usb_bulk_out_task()
// reads the host's next block and dispatches the commands in it
// (src/generic/usb_cdc.c:103-132), and that block can carry another
// serial_bridge_append / serial_bridge_write.  A nested write must not
// run: the outer one holds the bus (modbus.state == 4), has the USART
// IRQ disabled and is in the middle of a polled TX or RX.  While the
// flag is set, an append is dropped (it could land in the tx_buf the
// outer TX is reading from) and marks its Dwarf tx_dropped; a write is
// answered BRIDGE_DONE_REFUSED, and a later write for a Dwarf whose
// append was dropped is answered BRIDGE_DONE_DROPPED.  Both mean
// "nothing sent, re-send the whole block" to the host.  Cleared on
// every return path and by the shutdown handler (a shutdown longjmps
// out of this function and skips the clear at its end).  The guard is
// kept whatever BRIDGE_PUMP_USB_OUT is set to.
static uint8_t in_bridge_write;

// How often the blocking waits service USB, and whether they also
// dispatch the host's next block.  usb_bulk_in_task() flushes the
// responses queued so far (safe to call from here: it only touches
// transmit_buf/transmit_pos, which nothing above us on the stack is in
// the middle of using).  usb_bulk_out_task() reads at most one USB
// packet and dispatches at most one block per call (usb_cdc.c:103-131)
// and is what the recipe (recovered-settings.md section 3, fix 2 +
// fix 5) pumps inside the RX poll.  It is OFF by default here, for a
// reason the recipe does not cover and fix 5 does not fix: we are
// running *inside* usb_bulk_out_task() -> command_find_and_dispatch(),
// and that outer call stores its buffer position (receive_pos =
// rpos) only after the dispatch returns (usb_cdc.c:130).  A nested
// usb_bulk_out_task() therefore reads the next packet at the STALE
// receive_pos, i.e. over the block being dispatched - the 'data' this
// command transmits at the TX loop below points into that buffer - and
// over the outer call's unprocessed bytes; the outer memmove then
// moves garbage.  Every packet the pump would pick up corrupts the
// host->XLBuddy stream (naks and retransmits on that link) and, before
// TX, the frame sent to the Dwarf.  Fix 5's guard stops the nested
// serial_bridge_write; it cannot stop this.  Dispatching inside the
// capture has a second cost: the capture polls a one-byte receive
// register every 43us and a dispatched command that takes longer (a
// software i2c write, dwarf_boot_single's delay_ms(1000)) overruns it.
// BRIDGE_PUMP_USB_OUT=1 restores the recipe's behaviour for an A/B on
// the printer (RESULT-fix5.md section 4); the host side (protocol 2)
// is correct either way.
#define BRIDGE_USB_PUMP_US     500
#define BRIDGE_PUMP_USB_OUT    0

static inline void
bridge_pump_usb(void)
{
    usb_bulk_in_task();
    if (BRIDGE_PUMP_USB_OUT)
        usb_bulk_out_task();
}

// RX capture limits.  A reply is complete after BRIDGE_RX_GAP_US of
// silence; it is abandoned if nothing arrives within
// BRIDGE_RX_FIRST_BYTE_US (the old 3ms absolute limit, now only for
// the no-response case) and it can never exceed rx_buf.  The hard cap
// is a backstop against a device that never stops sending with gaps
// just under BRIDGE_RX_GAP_US; 192 back-to-back bytes take 8.3ms.
#define BRIDGE_RX_GAP_US         500
#define BRIDGE_RX_FIRST_BYTE_US  3000
#define BRIDGE_RX_HARD_CAP_US    15000

// Stream rx_buf[rx_sent..rx_pos) to the host in BRIDGE_RX_CHUNK byte
// chunks: from the capture loop whenever a full chunk is available,
// and once more at the end (tail) for the remainder.  Each chunk is
// flushed towards USB at once so transmit_buf holds at most one chunk
// plus whatever the IN endpoint has not taken yet.
static void
bridge_send_chunks(uint8_t dwarf, uint8_t idx, uint8_t tail)
{
    for (;;) {
        uint8_t pending = serial_bridge[idx].rx_pos
                        - serial_bridge[idx].rx_sent;
        if (!pending || (pending < BRIDGE_RX_CHUNK && !tail))
            return;
        uint8_t chunk = pending > BRIDGE_RX_CHUNK ? BRIDGE_RX_CHUNK : pending;
        sendf("serial_bridge_read dwarf=%c data=%*s", dwarf, chunk,
              serial_bridge[idx].rx_buf + serial_bridge[idx].rx_sent);
        serial_bridge[idx].rx_sent += chunk;
        usb_bulk_in_task();
    }
}

static inline void
bridge_send_done(uint8_t dwarf, uint8_t status, uint8_t total)
{
    sendf("serial_bridge_done dwarf=%c status=%c total=%c",
          dwarf, status, total);
}

// Accumulate data for a subsequent serial_bridge_write.
// Used when a Klipper frame exceeds bridge_write's payload limit
// (~56 bytes due to MESSAGE_MAX=64).  The accumulated data is
// prepended to the next bridge_write's payload before RS485 TX.
void command_serial_bridge_append(uint32_t *args)
{
    uint8_t dwarf = args[0];
    uint8_t data_len = args[1];
    uint8_t *data = command_decode_ptr(args[2]);

    if (dwarf < 1 || dwarf > MAX_BRIDGE_DWARFS)
        return;
    uint8_t idx = dwarf - 1;

    // Fix 5: dispatched from inside command_serial_bridge_write (see
    // in_bridge_write).  Refuse; the write that follows is answered
    // "nothing sent" and the host re-sends append and write together.
    if (in_bridge_write) {
        serial_bridge[idx].tx_dropped = 1;
        return;
    }

    uint8_t avail = sizeof(serial_bridge[idx].tx_buf)
                  - serial_bridge[idx].tx_pos;
    if (data_len > avail) data_len = avail;
    memcpy(serial_bridge[idx].tx_buf + serial_bridge[idx].tx_pos,
           data, data_len);
    serial_bridge[idx].tx_pos += data_len;
}
DECL_COMMAND(command_serial_bridge_append,
             "serial_bridge_append dwarf=%c data=%*s");

// Send raw bytes on RS485 to a specific Dwarf and capture response.
// dwarf param (1-5) selects target; address byte 0x80|dwarf is sent
// on RS485 before the data payload.
// Blocks for the full TX→RX cycle. watchdog_reset() prevents timeout.
void command_serial_bridge_write(uint32_t *args)
{
    watchdog_reset();  // Survive rapid back-to-back calls during identify

    uint8_t dwarf = args[0];    // 1-5
    uint8_t data_len = args[1];
    uint8_t *data = command_decode_ptr(args[2]);

    // Fix 5: refuse to nest (see in_bridge_write above).  The host
    // will re-send append and write, so a dropped append of this
    // Dwarf is accounted for by this answer.
    if (in_bridge_write) {
        if (dwarf >= 1 && dwarf <= MAX_BRIDGE_DWARFS)
            serial_bridge[dwarf - 1].tx_dropped = 0;
        bridge_send_done(dwarf, BRIDGE_DONE_REFUSED, 0);
        return;
    }
    in_bridge_write = 1;

    // Validate dwarf index
    if (dwarf < 1 || dwarf > MAX_BRIDGE_DWARFS) {
        in_bridge_write = 0;
        bridge_send_done(dwarf, BRIDGE_DONE_BAD_DWARF, 0);
        return;
    }
    uint8_t idx = dwarf - 1;
    uint8_t addr_byte = 0x80 | dwarf;  // Wire address (0x81-0x85)

    // data_len == 0: read-only poll.  Protocol 1 returned reply bytes
    // left over from a capture here; every reply is now streamed in
    // full by the write that produced it, so there is never anything
    // pending and the poll is answered with an empty 'done'.
    if (data_len == 0) {
        in_bridge_write = 0;
        bridge_send_done(dwarf, BRIDGE_DONE_OK, 0);
        return;
    }

    // An append for this Dwarf was refused while another write was in
    // flight (see in_bridge_write): the frame is incomplete, send
    // nothing and have the host re-send the whole block.
    if (serial_bridge[idx].tx_dropped) {
        serial_bridge[idx].tx_dropped = 0;
        serial_bridge[idx].tx_pos = 0;
        in_bridge_write = 0;
        bridge_send_done(dwarf, BRIDGE_DONE_DROPPED, 0);
        return;
    }

    // Atomic bus claim: wait for idle then claim with IRQ disabled.
    // This wait (up to 50ms while a MODBUS transaction finishes) and
    // the inter-frame gap below are the longest the main loop is
    // blocked, so USB is serviced every BRIDGE_USB_PUMP_US.
    uint32_t wait_start = timer_read_time();
    uint32_t last_pump = wait_start;
    while (1) {
        irq_disable();
        if (modbus.state == 0) {
            modbus.state = 4;  // Bridge owns bus
            irq_enable();
            break;
        }
        irq_enable();
        uint32_t now = timer_read_time();
        if (now - wait_start > timer_from_us(50000)) {
            // Bus busy: nothing was transmitted.  Drop what
            // serial_bridge_append queued so the host can re-send the
            // whole block; before protocol 2 this return left tx_buf
            // loaded and only a successful TX (below) cleared it.
            serial_bridge[idx].tx_pos = 0;
            in_bridge_write = 0;
            bridge_send_done(dwarf, BRIDGE_DONE_BUS_BUSY, 0);
            return;
        }
        if (now - last_pump > timer_from_us(BRIDGE_USB_PUMP_US)) {
            bridge_pump_usb();
            last_pump = now;
        }
    }

    // Wait for inter-frame gap (3000us).
    // This creates the idle period that Dwarf USART idle line detection
    // needs to wake all Dwarfs from mute before the address byte.
    // 3000us = ~69 character times at 230400 baud — must exceed Dwarf
    // RTOR timeout (500 bit-times = 2170us) so all Dwarfs reset
    // address detection between frames on the shared RS485 bus.
    while (timer_read_time() - modbus.last_activity < timer_from_us(3000)) {
        uint32_t now = timer_read_time();
        if (now - last_pump > timer_from_us(BRIDGE_USB_PUMP_US)) {
            bridge_pump_usb();
            last_pump = now;
        }
    }

    // Disable USART IRQ to prevent MODBUS ISR from touching DR
    NVIC_DisableIRQ(MODBUS_USART_IRQn);

    // Bus quiet check: verify no Dwarf is still transmitting.
    // With tx_allowed gating on Dwarfs, a previously-addressed Dwarf
    // may still be finishing its last byte when we want to send to a
    // new target.  Wait for 200us of actual silence (no RXNE) to
    // avoid bus collision.  Max wait 3ms.
    {
        uint32_t quiet_start = timer_read_time();
        uint32_t quiet_limit = quiet_start + timer_from_us(3000);
        while (1) {
            if (MODBUS_USART->SR & USART_SR_RXNE) {
                (void)MODBUS_USART->DR;  // Discard stray byte
                quiet_start = timer_read_time();
            }
            uint32_t now = timer_read_time();
            if (now - quiet_start > timer_from_us(200))
                break;  // 200us silence = bus quiet
            if (now > quiet_limit)
                break;  // Timeout
        }
    }

    // Flush any remaining stale UART data + clear error flags
    while (MODBUS_USART->SR & USART_SR_RXNE)
        (void)MODBUS_USART->DR;
    (void)MODBUS_USART->SR;
    (void)MODBUS_USART->DR;

    // TX: send address byte first, then Klipper frame data.
    // No gap between address and data — they are one continuous
    // transmission.  The addressed Dwarf stays awake after matching
    // the address byte and processes all subsequent bytes.
    //
    // Interrupts stay enabled during this polled TX.  The March bridge
    // cleared SysTick's TICKINT here "to prevent byte gaps", and that
    // is what reset the XLBuddy in hardware test 1 (2026-10-02, "Got
    // EOF" right after the Dwarf was configured; hardware-test-1/
    // RESULT-fix.md): Klipper programs SysTick as a one-shot
    // (src/generic/armcm_timer.c:34-40, LOAD=diff, VAL=0, LOAD=0) that
    // the handler re-arms after each dispatch (:155-162).  A count-to-
    // zero while TICKINT is clear raises no SysTick exception (ARMv7-M
    // SYST_CSR.TICKINT), nothing re-arms it, and every Klipper timer
    // stops - including periodic_event (src/sched.c:35-41), the only
    // thing that wakes the task loop out of irq_wait() (sched.c:245-253)
    // when the host is silent.  watchdog_reset() is a task
    // (src/stm32/watchdog.c:16-20), so the first 410-512 ms of USB
    // silence then trips the IWDG.  With the bridge busy (identify,
    // config) a SysTick expiry lands inside one of the hundreds of 2-5 ms
    // TX windows with near certainty.
    //
    // Gaps are tolerated instead: the USART is double buffered, so an
    // interrupt shorter than one byte time (43 us) leaves no gap at all,
    // and Klipper's timer dispatch yields after TIMER_REPEAT_TICKS
    // (100 us, armcm_timer.c:115, 135-144) when tasks are pending, so
    // a gap is ~100 us at most.  The Dwarf (task D, patches/0004) finds
    // the end of our frame by counting the block length, honours IDLE
    // only for a frame without a block, and only its 500-bit receiver
    // timeout (2.17 ms) resets its frame state.  The release's own
    // MODBUS TX (send_frame_wait, USART3 interrupt at the same priority
    // as SysTick) has run with these gaps against Prusa's 33-bit
    // (143 us) timeout every day.
    rs485_tx_enable();
    delay_us(10);

    // TX_TIMEOUT: 500us = ~11 byte times at 230400 baud.
    // If TXE or TC doesn't set within this window, the UART peripheral
    // is wedged (bus collision, FE/NE error flag blocking state machine,
    // or clock glitch).  Without this timeout, the MCU main loop hangs
    // forever and ALL bridges die — the "dead man's switch" bug.
    // The budget also has to cover an interrupt taken inside the loop
    // (interrupts are enabled, see above): ~100 us for a timer dispatch
    // burst, less for USB, against the 43 us a byte takes.
    #define TX_TIMEOUT_US  500
    uint32_t tx_deadline;

    // Address byte (wakes all Dwarfs, only matching one stays awake)
    tx_deadline = timer_read_time() + timer_from_us(TX_TIMEOUT_US);
    while (!(MODBUS_USART->SR & USART_SR_TXE)) {
        if (timer_is_before(tx_deadline, timer_read_time()))
            goto tx_bailout;
    }
    MODBUS_USART->DR = addr_byte;

    // Send accumulated data from bridge_append (if any)
    for (uint8_t i = 0; i < serial_bridge[idx].tx_pos; i++) {
        tx_deadline = timer_read_time() + timer_from_us(TX_TIMEOUT_US);
        while (!(MODBUS_USART->SR & USART_SR_TXE)) {
            if (timer_is_before(tx_deadline, timer_read_time()))
                goto tx_bailout;
        }
        MODBUS_USART->DR = serial_bridge[idx].tx_buf[i];
    }
    serial_bridge[idx].tx_pos = 0;

    // Klipper frame data (remainder from this command)
    for (uint8_t i = 0; i < data_len; i++) {
        tx_deadline = timer_read_time() + timer_from_us(TX_TIMEOUT_US);
        while (!(MODBUS_USART->SR & USART_SR_TXE)) {
            if (timer_is_before(tx_deadline, timer_read_time()))
                goto tx_bailout;
        }
        MODBUS_USART->DR = data[i];
    }

    // Wait for transmission complete (last byte leaves shift register)
    tx_deadline = timer_read_time() + timer_from_us(TX_TIMEOUT_US);
    while (!(MODBUS_USART->SR & USART_SR_TC)) {
        if (timer_is_before(tx_deadline, timer_read_time()))
            goto tx_bailout;
    }

    // Normal path: switch to RX mode
    rs485_rx_enable();
    delay_us(10);

    // Flush echo bytes (RS485 half-duplex: we receive our own TX).
    //
    // During TX of N bytes, the USART receiver sees all N echo bytes.
    // STM32F4 USART has no FIFO — only DR (1 byte) + shift register.
    // With overrun: DR holds the FIRST echo byte (never read), shift
    // register holds the LAST (all intermediate bytes were lost to ORE).
    // At TC, the last echo byte is fully received (TX and RX are
    // synchronous on the same APB clock, bus propagation is ~ns).
    //
    // First pass: drain DR (first echo) + shift register byte (last echo).
    // Reading SR then DR clears ORE, letting the shift register byte
    // move to DR on the next read.
    while (MODBUS_USART->SR & USART_SR_RXNE)
        (void)MODBUS_USART->DR;
    // Clear ORE explicitly (read SR then DR) in case the loop above
    // exited before ORE was cleared.
    (void)MODBUS_USART->SR;
    (void)MODBUS_USART->DR;
    // Drain any final byte that moved from shift register after ORE clear.
    while (MODBUS_USART->SR & USART_SR_RXNE)
        (void)MODBUS_USART->DR;
    //
    // NOTE: Previous code had delay_us(60) + second drain here.  This
    // was WRONG — fast Dwarf responses (get_clock: ~63us turnaround)
    // had their first byte arrive during the 60us window and get
    // discarded by the second drain.  The first pass already handles
    // all echo bytes since they complete by TC.  No delay needed.

    if (0) {
tx_bailout:
        // EMERGENCY BAILOUT: TXE or TC timed out.
        // UART peripheral is wedged — bus collision, error flag, or glitch.
        // Full cleanup to prevent zombie MCU state:
        //
        // 1. Force DE pin to RX (stop driving the bus)
        rs485_rx_enable();
        // 2. (SysTick was never disabled, see the TX comment above)
        // 3. Clear all UART error flags (FE, NE, ORE can block TC)
        (void)MODBUS_USART->SR;
        (void)MODBUS_USART->DR;
        // 4. Disable any stale interrupt enables
        MODBUS_USART->CR1 &= ~(USART_CR1_TXEIE | USART_CR1_TCIE);
        // 5. Release bus and re-enable ISR
        serial_bridge[idx].tx_pos = 0;
        modbus.last_activity = timer_read_time();
        modbus.state = 0;
        modbus.timeout_count++;
        NVIC_EnableIRQ(MODBUS_USART_IRQn);
        // 6. Tell the host so it doesn't hang (and re-sends)
        in_bridge_write = 0;
        bridge_send_done(dwarf, BRIDGE_DONE_TX_FAIL, 0);
        return;
    }

    // Keep watchdog alive before blocking RX poll
    watchdog_reset();

    // NOTE: USB IRQ is NOT disabled during RX poll.  Previous code
    // disabled OTG_FS_IRQn here to prevent byte overrun, but 5ms of
    // USB blackout causes "Got EOF" / USB disconnects — especially
    // on systemctl restart where USB state carries over.  A missed
    // USART byte is recoverable (Klipper CRC + retransmit); a USB
    // disconnect is not.  If overrun becomes a problem, use DMA for
    // USART3 RX instead.

    // Capture the reply.
    // The reply ends on a BRIDGE_RX_GAP_US byte gap, or when rx_buf is
    // full.  The 3ms figure that used to end the capture
    // unconditionally (54-byte frame = 2.3ms wire time + turnaround)
    // now only applies while nothing has arrived: a Dwarf can only
    // transmit when addressed, so whatever it queued since its last
    // turn (stats, analog_in_state, ...) leads its answer, and
    // 13 + 49 + 5 bytes is already past 3ms.  Everything the 3ms
    // limit cut off was discarded by the next transaction's flush
    // above and showed up on the host as bytes_invalid.
    //
    // Pump USB every BRIDGE_USB_PUMP_US: usb_bulk_in_task flushes
    // the responses queued so far (without it they sit in
    // transmit_buf until we return to the task loop, starving the
    // host serialqueue); usb_bulk_out_task only if
    // BRIDGE_PUMP_USB_OUT, see the note at bridge_pump_usb.
    //
    // Every full BRIDGE_RX_CHUNK of reply is handed to the host as it
    // completes (bridge_send_chunks), so the reply is never queued in
    // transmit_buf as a whole.
    serial_bridge[idx].rx_pos = 0;
    serial_bridge[idx].rx_sent = 0;
    uint32_t rx_start = timer_read_time();
    uint32_t last_byte_time = rx_start;
    uint32_t last_usb_pump = rx_start;
    while (1) {
        uint32_t now = timer_read_time();
        if (MODBUS_USART->SR & USART_SR_RXNE) {
            uint8_t byte = MODBUS_USART->DR;
            serial_bridge[idx].rx_buf[serial_bridge[idx].rx_pos++] = byte;
            last_byte_time = now;
            if (serial_bridge[idx].rx_pos >= sizeof(serial_bridge[idx].rx_buf))
                break;  // Reply is bounded by rx_buf
            if (serial_bridge[idx].rx_pos - serial_bridge[idx].rx_sent
                >= BRIDGE_RX_CHUNK)
                bridge_send_chunks(dwarf, idx, 0);
        }
        if (serial_bridge[idx].rx_pos > 0) {
            // Done: have data + BRIDGE_RX_GAP_US of silence
            if (now - last_byte_time > timer_from_us(BRIDGE_RX_GAP_US))
                break;
            if (now - rx_start > timer_from_us(BRIDGE_RX_HARD_CAP_US))
                break;  // Backstop, see BRIDGE_RX_HARD_CAP_US
        } else if (now - rx_start > timer_from_us(BRIDGE_RX_FIRST_BYTE_US)) {
            break;  // No response at all
        }
        if (now - last_usb_pump > timer_from_us(BRIDGE_USB_PUMP_US)) {
            bridge_pump_usb();
            last_usb_pump = now;
        }
    }
    modbus.last_activity = timer_read_time();
    modbus.state = 0;  // Release bus claim
    NVIC_EnableIRQ(MODBUS_USART_IRQn);

    // Hand the tail of the reply to the host (the full chunks went out
    // during the capture; Klipper MESSAGE_MAX=64, so 54 bytes is the
    // most one serial_bridge_read carries) and end the reply with
    // 'done'.  Protocol 1 sent the whole reply here and only sent an
    // empty read when nothing came back; the host took the block ack
    // as the end of the reply, which bridge_pump_usb() can now send
    // early (see the section comment).
    bridge_send_chunks(dwarf, idx, 1);
    bridge_send_done(dwarf, BRIDGE_DONE_OK, serial_bridge[idx].rx_pos);
    serial_bridge[idx].rx_pos = 0;
    serial_bridge[idx].rx_sent = 0;
    in_bridge_write = 0;
}
DECL_COMMAND(command_serial_bridge_write,
             "serial_bridge_write dwarf=%c data=%*s");

// Shutdown handler: restore USART3 state after longjmp() from shutdown.
// If shutdown() fires while inside command_serial_bridge_write, longjmp
// unwinds the stack and NVIC_EnableIRQ(MODBUS_USART_IRQn) is never reached.
// Without this, USART3 IRQ stays disabled permanently, killing all MODBUS.
void serial_bridge_shutdown(void)
{
    NVIC_EnableIRQ(MODBUS_USART_IRQn);
    NVIC_EnableIRQ(OTG_FS_IRQn);
    rs485_rx_enable();
    MODBUS_USART->CR1 &= ~(USART_CR1_TXEIE | USART_CR1_TCIE);
    (void)MODBUS_USART->SR;
    (void)MODBUS_USART->DR;
    modbus.state = 0;
    in_bridge_write = 0;    // the longjmp skipped the clear at the end
    for (int i = 0; i < MAX_BRIDGE_DWARFS; i++) {
        serial_bridge[i].rx_pos = 0;
        serial_bridge[i].rx_sent = 0;
        serial_bridge[i].tx_pos = 0;
        serial_bridge[i].tx_dropped = 0;
    }
}
DECL_SHUTDOWN(serial_bridge_shutdown);

/****************************************************************
 * Dwarf Flash Commands - Flash Klipper firmware to Dwarf via
 * the Prusa bootloader protocol (WRITE_FLASH / FINALIZE_FLASH)
 ****************************************************************/

#define BOOTLOADER_CMD_WRITE_FLASH       0x06
#define BOOTLOADER_CMD_FINALIZE_FLASH    0x07

// Flash a chunk of data to a Dwarf in bootloader mode
// address: bootloader address (0x0A-0x0E)
// offset: byte offset in flash (from app start 0x08002000)
// data: firmware bytes (up to 247 bytes per chunk)
void command_dwarf_flash_chunk(uint32_t *args)
{
    uint8_t addr = args[0];
    uint32_t offset = args[1];
    uint8_t data_len = args[2];
    uint8_t *data = command_decode_ptr(args[3]);

    // Build WRITE_FLASH frame: [addr][0x06][offset_4B_BE][data...][crc16]
    uint8_t frame[256];
    frame[0] = addr;
    frame[1] = BOOTLOADER_CMD_WRITE_FLASH;
    // Offset is big-endian
    frame[2] = (offset >> 24) & 0xFF;
    frame[3] = (offset >> 16) & 0xFF;
    frame[4] = (offset >> 8) & 0xFF;
    frame[5] = offset & 0xFF;
    if (data_len > 247) data_len = 247;
    memcpy(&frame[6], data, data_len);

    uint16_t frame_len = 6 + data_len;
    uint16_t crc = calc_crc16(frame, frame_len);
    frame[frame_len] = crc & 0xFF;
    frame[frame_len + 1] = (crc >> 8) & 0xFF;
    frame_len += 2;

    uint8_t response[32];
    uint16_t resp_len = 0;

    int ret = send_frame_wait(frame, frame_len, response, &resp_len,
                              sizeof(response), BOOTLOADER_TIMEOUT_MS, 1);

    uint8_t status = 0xFF;
    if (ret == MODBUS_OK && resp_len >= 5) {
        status = response[1];  // Bootloader status byte
    }
    sendf("dwarf_flash_response status=%c offset=%u", status, offset);
}
DECL_COMMAND(command_dwarf_flash_chunk,
             "dwarf_flash_chunk address=%c offset=%u data=%*s");

// Finalize flash on a Dwarf (triggers flash erase and program)
void command_dwarf_flash_finalize(uint32_t *args)
{
    uint8_t addr = args[0];

    // Build FINALIZE_FLASH frame: [addr][0x07][crc16]
    uint8_t frame[8];
    frame[0] = addr;
    frame[1] = BOOTLOADER_CMD_FINALIZE_FLASH;
    uint16_t crc = calc_crc16(frame, 2);
    frame[2] = crc & 0xFF;
    frame[3] = (crc >> 8) & 0xFF;

    uint8_t response[32];
    uint16_t resp_len = 0;

    // Finalize can take a while (flash erase)
    int ret = send_frame_wait(frame, 4, response, &resp_len,
                              sizeof(response), BOOTLOADER_TIMEOUT_MS, 1);

    uint8_t status = 0xFF;
    if (ret == MODBUS_OK && resp_len >= 5) {
        status = response[1];
    }
    sendf("dwarf_flash_finalize_response status=%c", status);
}
DECL_COMMAND(command_dwarf_flash_finalize, "dwarf_flash_finalize address=%c");

/****************************************************************
 * Buffered Flash Commands - Bypass MESSAGE_MAX=64 limit
 *
 * Klipper's USB protocol limits messages to 64 bytes, but the
 * Prusa bootloader expects ~247-byte WRITE_FLASH frames.
 * Solution: accumulate data on the MCU in a static buffer via
 * multiple small "load" commands, then build and send the full
 * RS485 frame with a single "send" command.
 ****************************************************************/

static struct {
    uint8_t buf[256];   // Accumulation buffer for flash data
    uint16_t pos;       // Current write position
    // Cached last-send result for retransmit idempotency
    uint8_t last_status;
    uint32_t last_offset;
    uint16_t last_len;
} flash_buf;

// Append data to the flash buffer (multiple calls to fill it)
// Returns current buffer position so host can verify
void command_dwarf_flash_load(uint32_t *args)
{
    uint8_t data_len = args[0];
    uint8_t *data = command_decode_ptr(args[1]);

    uint16_t space = sizeof(flash_buf.buf) - flash_buf.pos;
    if (data_len > space)
        data_len = space;

    memcpy(&flash_buf.buf[flash_buf.pos], data, data_len);
    flash_buf.pos += data_len;

    sendf("dwarf_flash_load_response pos=%u", flash_buf.pos);
}
DECL_COMMAND(command_dwarf_flash_load, "dwarf_flash_load data=%*s");

// Build WRITE_FLASH frame from buffer and send on RS485
// Uses all data accumulated via dwarf_flash_load calls.
// Retransmit-safe: if buffer is empty (already sent), returns cached result.
void command_dwarf_flash_send(uint32_t *args)
{
    uint8_t addr = args[0];
    uint32_t offset = args[1];

    // Retransmit detection: buffer empty means we already sent this chunk
    if (flash_buf.pos == 0) {
        sendf("dwarf_flash_send_response status=%c offset=%u len=%u",
              flash_buf.last_status, flash_buf.last_offset,
              flash_buf.last_len);
        return;
    }

    uint16_t data_len = flash_buf.pos;
    if (data_len > 247) data_len = 247;  // Prusa max

    // Build WRITE_FLASH frame: [addr][0x06][offset_4B_BE][data...][crc16]
    uint8_t frame[256];
    frame[0] = addr;
    frame[1] = BOOTLOADER_CMD_WRITE_FLASH;
    frame[2] = (offset >> 24) & 0xFF;
    frame[3] = (offset >> 16) & 0xFF;
    frame[4] = (offset >> 8) & 0xFF;
    frame[5] = offset & 0xFF;
    memcpy(&frame[6], flash_buf.buf, data_len);

    uint16_t frame_len = 6 + data_len;
    uint16_t crc = calc_crc16(frame, frame_len);
    frame[frame_len] = crc & 0xFF;
    frame[frame_len + 1] = (crc >> 8) & 0xFF;
    frame_len += 2;

    // Clear buffer — next load sequence will refill it
    flash_buf.pos = 0;

    uint8_t response[32];
    uint16_t resp_len = 0;

    int ret = send_frame_wait(frame, frame_len, response, &resp_len,
                              sizeof(response), BOOTLOADER_TIMEOUT_MS, 1);

    uint8_t status = 0xFF;
    if (ret == MODBUS_OK && resp_len >= 5) {
        status = response[1];
    }

    // Cache result for retransmit idempotency
    flash_buf.last_status = status;
    flash_buf.last_offset = offset;
    flash_buf.last_len = data_len;

    sendf("dwarf_flash_send_response status=%c offset=%u len=%u",
          status, offset, data_len);
}
DECL_COMMAND(command_dwarf_flash_send,
             "dwarf_flash_send address=%c offset=%u");

// Reset flash buffer (call before starting a new flash sequence)
void command_dwarf_flash_buf_reset(uint32_t *args)
{
    flash_buf.pos = 0;
    sendf("dwarf_flash_buf_reset_response pos=%u", flash_buf.pos);
}
DECL_COMMAND(command_dwarf_flash_buf_reset, "dwarf_flash_buf_reset");

#define BOOTLOADER_CMD_READ_FLASH        0x08

// Read a chunk of flash from a Dwarf in bootloader mode
// READ_FLASH frame: [addr][0x08][offset_4B_BE][len_1B][crc16]
// Response: [addr][status][resp_len][data...][crc16]
void command_dwarf_read_flash(uint32_t *args)
{
    uint8_t addr = args[0];
    uint32_t offset = args[1];
    uint8_t read_len = args[2];

    if (read_len > 128) read_len = 128;  // Cap to safe size for response

    // Build READ_FLASH frame
    uint8_t frame[16];
    frame[0] = addr;
    frame[1] = BOOTLOADER_CMD_READ_FLASH;
    frame[2] = (offset >> 24) & 0xFF;
    frame[3] = (offset >> 16) & 0xFF;
    frame[4] = (offset >> 8) & 0xFF;
    frame[5] = offset & 0xFF;
    frame[6] = read_len;

    uint16_t frame_len = 7;
    uint16_t crc = calc_crc16(frame, frame_len);
    frame[frame_len] = crc & 0xFF;
    frame[frame_len + 1] = (crc >> 8) & 0xFF;
    frame_len += 2;

    uint8_t response[192];
    uint16_t resp_len = 0;

    int ret = send_frame_wait(frame, frame_len, response, &resp_len,
                              sizeof(response), BOOTLOADER_TIMEOUT_MS, 1);

    uint8_t status = 0xFF;
    uint8_t data_len = 0;
    uint8_t *data_ptr = response;

    if (ret == MODBUS_OK && resp_len >= 5) {
        status = response[1];
        data_len = response[2];
        data_ptr = &response[3];
        // Cap data_len to what we actually received (minus header + crc)
        if (data_len > resp_len - 5) data_len = resp_len - 5;
        // Cap to MESSAGE_MAX safe size
        if (data_len > 50) data_len = 50;
    }
    sendf("dwarf_read_flash_response status=%c offset=%u data=%*s",
          status, offset, data_len, data_ptr);
}
DECL_COMMAND(command_dwarf_read_flash,
             "dwarf_read_flash address=%c offset=%u len=%c");

