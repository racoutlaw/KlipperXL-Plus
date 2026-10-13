// KlipperXL+ - non-blocking RS485 engine for the Prusa XL XLBuddy (STM32F407)
//
// The XLBuddy talks to the five Dwarf toolheads and the modular bed over ONE
// shared half-duplex RS485 bus (USART3).  This module is the only custom code
// on the XLBuddy in KlipperXL+.  It carries:
//   - Klipper frames to/from the Dwarfs (they have no USB of their own), and
//   - raw frames to the modular bed (MODBUS) and to the Prusa bootloaders.
//
// Klipper rule: a command handler never blocks.  Commands only queue a job
// and return.  TX and RX run by DMA, a Klipper timer watches gaps and
// timeouts, and a task sends the reply to the host.  Every fact used here is
// in FACTS.md (sections 2-4) with its source.
//
// Copyright (C) 2026  Richard Crook
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include <string.h> // memcpy
#include "autoconf.h" // CONFIG_CLOCK_FREQ
#include "board/armcm_boot.h" // armcm_enable_irq
#include "board/gpio.h" // gpio_out_setup
#include "board/internal.h" // gpio_peripheral, enable_pclock
#include "board/irq.h" // irq_disable
#include "board/misc.h" // timer_read_time
#include "command.h" // DECL_COMMAND
#include "sched.h" // sched_add_timer

void usb_bulk_in_task(void); // src/generic/usb_cdc.c - flush replies to the host

/****************************************************************
 * Hardware (FACTS.md section 2)
 ****************************************************************/

#define RS485_USART       USART3
#define RS485_IRQn        USART3_IRQn
#define RS485_TX_PIN      GPIO('D', 8)    // AF7
#define RS485_RX_PIN      GPIO('D', 9)    // AF7, pull-up
#define RS485_DE_PIN      GPIO('G', 1)    // PFB RS485FlowControlPuppies, high = transmit
#define RS485_BAUD        230400

// DMA1: USART3_RX = Stream1 / Channel4, USART3_TX = Stream3 / Channel4
#define RX_STREAM         DMA1_Stream1
#define TX_STREAM         DMA1_Stream3
#define DMA_CHANNEL4      (4 << DMA_SxCR_CHSEL_Pos)
#define RX_CLEAR_FLAGS    (DMA_LIFCR_CTCIF1 | DMA_LIFCR_CHTIF1 | DMA_LIFCR_CTEIF1 \
                           | DMA_LIFCR_CDMEIF1 | DMA_LIFCR_CFEIF1)
#define TX_CLEAR_FLAGS    (DMA_LIFCR_CTCIF3 | DMA_LIFCR_CHTIF3 | DMA_LIFCR_CTEIF3 \
                           | DMA_LIFCR_CDMEIF3 | DMA_LIFCR_CFEIF3)

/****************************************************************
 * Timing (FACTS.md sections 2-3)
 ****************************************************************/

#define POLL_US           100     // timer period while a job is active
#define QUIET_US          200     // bus silent this long before we transmit
#define QUIET_MAX_US      3000    // ...but never wait longer than this for it
#define FRAME_GAP_US      3000    // silence before every frame (Dwarf RTOR is 2.17 ms)
#define TX_MAX_US         20000   // a 260 byte frame takes 11.3 ms at 230400
#define RX_BYTE_GAP_US    500     // reply complete after this much silence
#define BRIDGE_FIRST_US   3000    // a Dwarf answers within 3 ms or not at all
#define BRIDGE_CAP_US     15000   // hard cap on one bridge reply
#define FRAME_FIRST_US    100000  // bed / bootloader: first byte within 100 ms
#define BOOT_FIRST_US     500000  // bootloader: WRITE/FINALIZE_FLASH, Prusa
                                  // READ_TIMEOUT_FINALIZE_FLASH = 500 ms
                                  // (include/puppies/BootloaderProtocol.hpp:271)
#define FRAME_CAP_US      600000  // hard cap on one raw-frame reply

/****************************************************************
 * Jobs
 ****************************************************************/

#define TX_MAX            320     // address/len header + 247 B flash block + CRC
#define RX_MAX            300     // bridge replies are capped at 192 by the Dwarf
#define BRIDGE_RX_MAX     192
#define STAGE_MAX         TX_MAX
#define QUEUE_LEN         4
#define NUM_DWARFS        5
#define CHUNK             54      // reply bytes per message (Klipper 64 B limit)

enum { JOB_BRIDGE = 1, JOB_MODBUS = 2, JOB_BOOT = 3 };

// serial_bridge_done status (protocol 2, FACTS.md section 3)
enum { DONE_OK = 0, DONE_BAD_DWARF = 1, DONE_REFUSED = 2, DONE_TX_FAIL = 5 };
// rs485_frame_done status
enum { FRAME_OK = 0, FRAME_TIMEOUT = 1, FRAME_CRC = 2, FRAME_REFUSED = 3,
       FRAME_TX_FAIL = 5 };

struct job {
    uint8_t kind, dwarf;
    uint16_t tx_len;
    uint8_t tx[TX_MAX];
};

enum { ST_IDLE, ST_QUIET, ST_TX, ST_RX, ST_DONE };

static struct {
    struct job queue[QUEUE_LEN];
    uint8_t q_head, q_count;              // jobs waiting (head = next to run)
    struct job *active;
    volatile uint8_t state;
    uint8_t tx_failed;
    uint32_t phase_start, last_rx_change, last_frame_end;
    uint16_t rx_len, last_rx_count, rx_sent;
    uint32_t first_byte_us, gap_us, cap_us;
    uint16_t rx_max;
    uint8_t rx[RX_MAX];
    struct timer timer;
    struct gpio_out de;
    // host staging buffers: bridge per Dwarf, raw frames shared
    uint8_t bridge_stage[NUM_DWARFS][64];
    uint8_t bridge_stage_len[NUM_DWARFS];
    uint8_t frame_stage[STAGE_MAX];
    uint16_t frame_stage_len;
    // diagnostics (rs485_stats, readable after a shutdown)
    struct {
        uint32_t jobs, tx_fail, rx_empty, rx_full, rx_bytes, refused, giveup;
        uint32_t ore, fe, ne, dma_rx_err, dma_tx_err;
    } st;
} rs;

static struct task_wake rs485_wake;

/****************************************************************
 * CRC16 (MODBUS, init 0xFFFF) - used to check bed / bootloader replies
 ****************************************************************/

static uint16_t
crc16(const uint8_t *buf, uint16_t len)
{
    uint16_t crc = 0xFFFF;
    while (len--) {
        crc ^= *buf++;
        for (int i = 0; i < 8; i++)
            crc = (crc & 1) ? (crc >> 1) ^ 0xA001 : crc >> 1;
    }
    return crc;
}

/****************************************************************
 * Hardware helpers
 ****************************************************************/

static void
rx_dma_stop(void)
{
    RX_STREAM->CR &= ~DMA_SxCR_EN;
    while (RX_STREAM->CR & DMA_SxCR_EN)
        ;
    RS485_USART->CR3 &= ~USART_CR3_DMAR;
    DMA1->LIFCR = RX_CLEAR_FLAGS;
}

static void
tx_dma_stop(void)
{
    TX_STREAM->CR &= ~DMA_SxCR_EN;
    while (TX_STREAM->CR & DMA_SxCR_EN)
        ;
    RS485_USART->CR3 &= ~USART_CR3_DMAT;
    DMA1->LIFCR = TX_CLEAR_FLAGS;
}

// Back to listening: driver off, receiver on, any stale byte dropped.
static void
bus_listen(void)
{
    gpio_out_write(rs.de, 0);
    RS485_USART->CR1 |= USART_CR1_RE;
    (void)RS485_USART->SR;
    (void)RS485_USART->DR;
}

static void
rx_dma_start(uint16_t max)
{
    rx_dma_stop();
    (void)RS485_USART->SR;
    (void)RS485_USART->DR;
    RX_STREAM->PAR = (uint32_t)&RS485_USART->DR;
    RX_STREAM->M0AR = (uint32_t)rs.rx;
    RX_STREAM->NDTR = max;
    RX_STREAM->CR = DMA_CHANNEL4 | DMA_SxCR_MINC;   // periph -> memory
    RS485_USART->CR3 |= USART_CR3_DMAR;
    RX_STREAM->CR |= DMA_SxCR_EN;
}

/****************************************************************
 * Job state machine (timer + USART interrupt)
 ****************************************************************/

static uint_fast8_t rs485_event(struct timer *t);

static void
timer_start(uint32_t delay_us)
{
    rs.timer.func = rs485_event;
    rs.timer.waketime = timer_read_time() + timer_from_us(delay_us);
    sched_add_timer(&rs.timer);
}

static void
job_finish(void)
{
    uint32_t sr = RS485_USART->SR, lisr = DMA1->LISR;
    if (sr & USART_SR_ORE)
        rs.st.ore++;
    if (sr & USART_SR_FE)
        rs.st.fe++;
    if (sr & USART_SR_NE)
        rs.st.ne++;
    if (lisr & (DMA_LISR_TEIF1 | DMA_LISR_DMEIF1))
        rs.st.dma_rx_err++;
    if (lisr & (DMA_LISR_TEIF3 | DMA_LISR_DMEIF3))
        rs.st.dma_tx_err++;
    rx_dma_stop();
    tx_dma_stop();
    RS485_USART->CR1 &= ~USART_CR1_TCIE;
    bus_listen();
    rs.last_frame_end = timer_read_time();
    rs.state = ST_DONE;
    sched_wake_task(&rs485_wake);
}

// Start transmitting the active job (bus is quiet and the frame gap is over)
static void
job_transmit(void)
{
    struct job *j = rs.active;
    rs.state = ST_TX;
    rs.phase_start = timer_read_time();
    // Receiver off while we drive the bus: no echo of our own bytes
    // (same order as Prusa HAL_RS485.cpp:196-198 / 249-252).
    RS485_USART->CR1 &= ~USART_CR1_RE;
    gpio_out_write(rs.de, 1);
    tx_dma_stop();
    RS485_USART->SR &= ~USART_SR_TC;
    TX_STREAM->PAR = (uint32_t)&RS485_USART->DR;
    TX_STREAM->M0AR = (uint32_t)j->tx;
    TX_STREAM->NDTR = j->tx_len;
    TX_STREAM->CR = DMA_CHANNEL4 | DMA_SxCR_MINC | DMA_SxCR_DIR_0; // mem -> periph
    RS485_USART->CR3 |= USART_CR3_DMAT;
    TX_STREAM->CR |= DMA_SxCR_EN;
    RS485_USART->CR1 |= USART_CR1_TCIE;   // USART3_IRQHandler ends the TX phase
}

// Last bit has left the shift register: release the bus and capture the reply
void
USART3_IRQHandler(void)
{
    if ((RS485_USART->SR & USART_SR_TC) && (RS485_USART->CR1 & USART_CR1_TCIE)
        && rs.state == ST_TX && !TX_STREAM->NDTR) {
        RS485_USART->CR1 &= ~USART_CR1_TCIE;
        tx_dma_stop();
        bus_listen();
        rx_dma_start(rs.rx_max);
        rs.last_rx_count = 0;
        rs.phase_start = rs.last_rx_change = timer_read_time();
        rs.state = ST_RX;
    }
}

static uint_fast8_t
rs485_event(struct timer *t)
{
    uint32_t now = timer_read_time();
    switch (rs.state) {
    case ST_QUIET: {
        // Any byte on the bus restarts the quiet window
        if (RS485_USART->SR & USART_SR_RXNE) {
            (void)RS485_USART->DR;
            rs.last_rx_change = now;
        }
        int quiet = timer_is_before(rs.last_rx_change + timer_from_us(QUIET_US)
                                    , now);
        // Frame gap as an UNSIGNED interval: last_frame_end can be any age
        // (the bus may idle for minutes), and timer_is_before() - a signed
        // 32-bit compare - is only right within +-12.8 s at 168 MHz.  After
        // 12.8-25.6 s of silence (mod 25.6 s) it read "gap not over" and the
        // first job waited until the compare flipped back: up to 12.8 s,
        // measured 10/10 (idle 18 s -> 7.57 s, idle 14 s -> 11.57 s; the
        // installer's first scan after a power-up missed T0/T1).  Unsigned
        // modular difference is exact up to 25.6 s; past that it can only
        // make us wait at most one extra gap (3 ms).
        int gap = (uint32_t)(now - rs.last_frame_end)
                  >= timer_from_us(FRAME_GAP_US);
        int give_up = timer_is_before(rs.phase_start
                                      + timer_from_us(QUIET_MAX_US), now);
        if ((quiet || give_up) && gap) {
            if (!quiet)
                rs.st.giveup++;
            job_transmit();
        }
        break;
    }
    case ST_TX:
        if (timer_is_before(rs.phase_start + timer_from_us(TX_MAX_US), now)) {
            rs.tx_failed = 1;            // TX wedged: report, never hang
            rs.rx_len = 0;
            job_finish();
            return SF_DONE;
        }
        break;
    case ST_RX: {
        uint16_t count = rs.rx_max - RX_STREAM->NDTR;
        if (count != rs.last_rx_count) {
            rs.last_rx_count = count;
            rs.last_rx_change = now;
            if (count - rs.rx_sent >= CHUNK)
                sched_wake_task(&rs485_wake);   // stream it while capturing
        }
        uint32_t since_start = now - rs.phase_start;
        int done = count >= rs.rx_max
            || (count && timer_is_before(rs.last_rx_change
                                         + timer_from_us(rs.gap_us), now))
            || (!count && since_start > timer_from_us(rs.first_byte_us))
            || since_start > timer_from_us(rs.cap_us);
        if (done) {
            rs.rx_len = count;
            job_finish();
            return SF_DONE;
        }
        break;
    }
    default:
        return SF_DONE;
    }
    rs.timer.waketime = now + timer_from_us(POLL_US);
    return SF_RESCHEDULE;
}

// Start the next queued job if the engine is free (irqs must be disabled)
static void
job_start_next(void)
{
    if (rs.state != ST_IDLE || !rs.q_count)
        return;
    rs.active = &rs.queue[rs.q_head];
    struct job *j = rs.active;
    if (j->kind == JOB_BRIDGE) {
        rs.first_byte_us = BRIDGE_FIRST_US;
        rs.cap_us = BRIDGE_CAP_US;
        rs.rx_max = BRIDGE_RX_MAX;
    } else {
        rs.first_byte_us = j->kind == JOB_BOOT ? BOOT_FIRST_US : FRAME_FIRST_US;
        rs.cap_us = FRAME_CAP_US;
        rs.rx_max = RX_MAX;
    }
    rs.gap_us = RX_BYTE_GAP_US;
    rs.tx_failed = 0;
    rs.rx_len = rs.rx_sent = 0;
    rs.state = ST_QUIET;
    rs.phase_start = rs.last_rx_change = timer_read_time();
    timer_start(POLL_US);
}

// Queue a job; returns 0 if the queue is full.  Called from command context.
static int
job_queue(uint8_t kind, uint8_t dwarf, const uint8_t *pre, uint16_t pre_len
          , const uint8_t *a, uint16_t a_len, const uint8_t *b, uint16_t b_len)
{
    if (pre_len + a_len + b_len > TX_MAX)
        return 0;
    irqstatus_t flag = irq_save();
    if (rs.q_count >= QUEUE_LEN) {
        rs.st.refused++;
        irq_restore(flag);
        return 0;
    }
    struct job *j = &rs.queue[(rs.q_head + rs.q_count) % QUEUE_LEN];
    j->kind = kind;
    j->dwarf = dwarf;
    memcpy(j->tx, pre, pre_len);
    memcpy(j->tx + pre_len, a, a_len);
    memcpy(j->tx + pre_len + a_len, b, b_len);
    j->tx_len = pre_len + a_len + b_len;
    rs.q_count++;
    job_start_next();
    irq_restore(flag);
    return 1;
}

/****************************************************************
 * Replies to the host (task context - sendf is not allowed in irqs)
 ****************************************************************/

// Klipper's USB transmit buffer is 192 B and a message that does not fit
// is dropped (usb_cdc.c:34,72), so a reply is streamed one chunk per task
// run while it is still arriving (one chunk per 2.3 ms at 230400, USB
// drains 64 B per 1 ms frame) - the same pacing fw4 used.  The done
// message goes out on a later run than the last chunk.
static void
send_chunk(struct job *j, const uint8_t *data, uint8_t n)
{
    if (j->kind == JOB_BRIDGE)
        sendf("serial_bridge_read dwarf=%c data=%*s", j->dwarf, n, data);
    else
        sendf("rs485_frame_read data=%*s", n, data);
    usb_bulk_in_task();
}

static void
send_done(struct job *j, uint16_t len)
{
    if (j->kind == JOB_BRIDGE) {
        uint8_t status = rs.tx_failed ? DONE_TX_FAIL : DONE_OK;
        sendf("serial_bridge_done dwarf=%c status=%c total=%c"
              , j->dwarf, status, (uint8_t)len);
    } else {
        uint8_t status = rs.tx_failed ? FRAME_TX_FAIL : FRAME_OK;
        if (!rs.tx_failed && !len)
            status = FRAME_TIMEOUT;
        else if (!rs.tx_failed && (len < 4 || crc16(rs.rx, len - 2)
                 != (rs.rx[len - 2] | (rs.rx[len - 1] << 8))))
            status = FRAME_CRC;
        sendf("rs485_frame_done status=%c total=%u", status, len);
    }
    usb_bulk_in_task();
}

void
rs485_task(void)
{
    if (!sched_check_wake(&rs485_wake))
        return;
    irq_disable();
    uint8_t state = rs.state;
    uint16_t avail = state == ST_DONE ? rs.rx_len : rs.last_rx_count;
    irq_enable();
    if (state != ST_RX && state != ST_DONE)
        return;
    struct job *j = rs.active;
    uint16_t pending = avail - rs.rx_sent;
    if (pending >= CHUNK || (state == ST_DONE && pending)) {
        uint8_t n = pending > CHUNK ? CHUNK : pending;
        send_chunk(j, rs.rx + rs.rx_sent, n);
        rs.rx_sent += n;
        if (state == ST_DONE)
            sched_wake_task(&rs485_wake);   // rest / done on the next run
        return;
    }
    if (state != ST_DONE)
        return;
    send_done(j, avail);
    rs.st.jobs++;
    rs.st.rx_bytes += avail;
    if (rs.tx_failed)
        rs.st.tx_fail++;
    else if (!avail)
        rs.st.rx_empty++;
    if (avail >= rs.rx_max)
        rs.st.rx_full++;
    irq_disable();
    rs.q_head = (rs.q_head + 1) % QUEUE_LEN;
    rs.q_count--;
    rs.active = NULL;
    rs.state = ST_IDLE;
    job_start_next();
    irq_enable();
}
DECL_TASK(rs485_task);

/****************************************************************
 * Host commands
 ****************************************************************/

DECL_CONSTANT("SERIAL_BRIDGE_PROTOCOL", 2);
DECL_CONSTANT("RS485_FRAME_PROTOCOL", 1);

// Bridge: stage the first part of a Klipper block for a Dwarf (<= 48 B)
void
command_serial_bridge_append(uint32_t *args)
{
    uint8_t dwarf = args[0], len = args[1];
    uint8_t *data = command_decode_ptr(args[2]);
    if (dwarf < 1 || dwarf > NUM_DWARFS)
        return;
    uint8_t i = dwarf - 1;
    if (rs.bridge_stage_len[i] + len > sizeof(rs.bridge_stage[i])) {
        rs.bridge_stage_len[i] = 0;          // overflow: host re-sends whole
        return;
    }
    memcpy(rs.bridge_stage[i] + rs.bridge_stage_len[i], data, len);
    rs.bridge_stage_len[i] += len;
}
DECL_COMMAND(command_serial_bridge_append,
             "serial_bridge_append dwarf=%c data=%*s");

// Bridge: send [0x80|dwarf] + staged + data to a Dwarf; reply is streamed as
// serial_bridge_read chunks and ended by serial_bridge_done.
void
command_serial_bridge_write(uint32_t *args)
{
    uint8_t dwarf = args[0], len = args[1];
    uint8_t *data = command_decode_ptr(args[2]);
    if (dwarf < 1 || dwarf > NUM_DWARFS) {
        sendf("serial_bridge_done dwarf=%c status=%c total=%c"
              , dwarf, DONE_BAD_DWARF, 0);
        return;
    }
    uint8_t i = dwarf - 1, addr = 0x80 | dwarf;
    int ok = job_queue(JOB_BRIDGE, dwarf, &addr, 1, rs.bridge_stage[i]
                       , rs.bridge_stage_len[i], data, len);
    rs.bridge_stage_len[i] = 0;
    if (!ok)
        sendf("serial_bridge_done dwarf=%c status=%c total=%c"
              , dwarf, DONE_REFUSED, 0);
}
DECL_COMMAND(command_serial_bridge_write,
             "serial_bridge_write dwarf=%c data=%*s");

// Raw frames (bed MODBUS, Prusa bootloader): stage long frames in pieces
void
command_rs485_frame_load(uint32_t *args)
{
    uint8_t len = args[0];
    uint8_t *data = command_decode_ptr(args[1]);
    if (rs.frame_stage_len + len > STAGE_MAX) {
        rs.frame_stage_len = 0;
        return;
    }
    memcpy(rs.frame_stage + rs.frame_stage_len, data, len);
    rs.frame_stage_len += len;
}
DECL_COMMAND(command_rs485_frame_load, "rs485_frame_load data=%*s");

// Send staged + data as one frame (the host appends the CRC); boot=1 allows
// the longer bootloader reply time.  Reply: rs485_frame_read chunks, then
// rs485_frame_done (status checks the reply CRC).
void
command_rs485_frame_send(uint32_t *args)
{
    uint8_t boot = args[0], len = args[1];
    uint8_t *data = command_decode_ptr(args[2]);
    int ok = job_queue(boot ? JOB_BOOT : JOB_MODBUS, 0, NULL, 0
                       , rs.frame_stage, rs.frame_stage_len, data, len);
    rs.frame_stage_len = 0;
    if (!ok)
        sendf("rs485_frame_done status=%c total=%u", FRAME_REFUSED, 0);
}
DECL_COMMAND(command_rs485_frame_send, "rs485_frame_send boot=%c data=%*s");

// Diagnostics: two messages (Klipper's 64 B message limit).  Allowed in
// shutdown so a failure can be read after Klipper has stopped.
void
command_rs485_stats(uint32_t *args)
{
    sendf("rs485_stats_a jobs=%u tx_fail=%u rx_empty=%u rx_full=%u"
          " rx_bytes=%u refused=%u giveup=%u", rs.st.jobs, rs.st.tx_fail
          , rs.st.rx_empty, rs.st.rx_full, rs.st.rx_bytes, rs.st.refused
          , rs.st.giveup);
    sendf("rs485_stats_b ore=%u fe=%u ne=%u dmarx=%u dmatx=%u state=%c q=%c"
          " sr=%u cr1=%u cr3=%u de=%c ndtr=%u", rs.st.ore, rs.st.fe, rs.st.ne
          , rs.st.dma_rx_err, rs.st.dma_tx_err, rs.state, rs.q_count
          , RS485_USART->SR, RS485_USART->CR1, RS485_USART->CR3
          , (uint8_t)!!(GPIOG->ODR & (1 << 1)), RX_STREAM->NDTR);
}
DECL_COMMAND_FLAGS(command_rs485_stats, HF_IN_SHUTDOWN, "rs485_stats");

/****************************************************************
 * Init / shutdown
 ****************************************************************/

void
rs485_init(void)
{
    enable_pclock((uint32_t)RS485_USART);
    // Not enable_pclock(): its AHB1 bit is (base - AHB1PERIPH_BASE) / 0x400
    // (src/stm32/stm32f4.c:29-31), right for GPIOx but bit 24 for DMA1,
    // whose enable is DMA1EN = bit 21 (RM0090 AHB1ENR).
    RCC->AHB1ENR |= RCC_AHB1ENR_DMA1EN;
    (void)RCC->AHB1ENR;
    gpio_peripheral(RS485_TX_PIN, GPIO_FUNCTION(7), 0);
    gpio_peripheral(RS485_RX_PIN, GPIO_FUNCTION(7), 1);
    rs.de = gpio_out_setup(RS485_DE_PIN, 0);
    uint32_t pclk = get_pclock_frequency((uint32_t)RS485_USART);
    RS485_USART->BRR = (pclk + RS485_BAUD / 2) / RS485_BAUD;
    RS485_USART->CR3 = 0;
    RS485_USART->CR1 = USART_CR1_UE | USART_CR1_TE | USART_CR1_RE;
    // Same priority as SysTick (armcm_timer.c:106): the TC irq and the
    // job timer never preempt each other mid state change.
    armcm_enable_irq(USART3_IRQHandler, RS485_IRQn, 2);
    rs.last_frame_end = timer_read_time();
    rs.state = ST_IDLE;
}
DECL_INIT(rs485_init);

void
rs485_shutdown(void)
{
    sched_del_timer(&rs.timer);
    RS485_USART->CR1 &= ~USART_CR1_TCIE;
    rx_dma_stop();
    tx_dma_stop();
    bus_listen();
    rs.q_count = 0;
    rs.active = NULL;
    rs.state = ST_IDLE;
    memset(rs.bridge_stage_len, 0, sizeof(rs.bridge_stage_len));
    rs.frame_stage_len = 0;
}
DECL_SHUTDOWN(rs485_shutdown);
