/*
 * CC2530 standalone flasher — embeds the Kadsol CC2591 firmware in the
 * binary and flashes it to a connected CC2530 on boot. No PC, no Python.
 *
 * PlatformIO project — build with `pio run`, upload with `pio run -t upload`,
 * monitor with `pio device monitor`.
 *
 * Wiring (ESP32-C3 default; configurable via build_flags in platformio.ini):
 *   ESP32 GPIO3  → CC2530 DD   (Debug Data)
 *   ESP32 GPIO4  → CC2530 DC   (Debug Clock)
 *   ESP32 GPIO5  → CC2530 RESET
 *   ESP32 3V3    → CC2530 VCC  (3.3 V — never 5 V)
 *   ESP32 GND    → CC2530 GND
 *
 * The CC2530 MUST be powered exclusively by the ESP32 during flashing.
 * Disconnect any USB-TTL bridge (CH340E etc.) first.
 *
 * Low-level CC2530 debug protocol functions are taken from RedBearLab's
 * CCLoader (MIT license), unmodified except for the pin numbers.
 *   https://github.com/RedBearLab/CCLoader
 *
 * Embedded firmware: Kadsol CC2530ZNP_2591-with-SBL.hex (256 KB, full
 * flash including SBL bootloader), generated from the upstream archive
 *   https://github.com/patience4711/read-APSystems-YC600-QS1-DS3/blob/main/cc25xx_firmware.zip
 */

#include <Arduino.h>
#include "firmware_data.h"

/* === USER CONFIG (defaults overridable in platformio.ini) ================== */
#ifndef CC2530_PIN_DD
#define CC2530_PIN_DD 3
#endif
#ifndef CC2530_PIN_DC
#define CC2530_PIN_DC 4
#endif
#ifndef CC2530_PIN_RST
#define CC2530_PIN_RST 5
#endif
#ifndef CC2530_PIN_LED
#define CC2530_PIN_LED 8
#endif
#ifndef CC2530_LED_ACTIVE_LOW
#define CC2530_LED_ACTIVE_LOW 1
#endif
#ifndef CC2530_LED_WS2812
#define CC2530_LED_WS2812 0
#endif

static const int DD = CC2530_PIN_DD;
static const int DC = CC2530_PIN_DC;
static const int RESET_PIN = CC2530_PIN_RST;
static const int LED = CC2530_PIN_LED;
static const bool LED_ACTIVE_LOW = (CC2530_LED_ACTIVE_LOW != 0);

#if CC2530_LED_WS2812
#include <Adafruit_NeoPixel.h>
static Adafruit_NeoPixel led_strip(1, CC2530_PIN_LED, NEO_GRB + NEO_KHZ800);
#endif

/* ============================================================================ */
/* --------- CCLoader low-level CC2530 debug protocol (unchanged) ----------- */
/* ============================================================================ */

/* CC2530 DUP memory layout */
#define ADDR_BUF0                   0x0000
#define ADDR_DMA_DESC_0             0x0200
#define ADDR_DMA_DESC_1             (ADDR_DMA_DESC_0 + 8)
#define CH_DBG_TO_BUF0              0x01
#define CH_BUF0_TO_FLASH            0x02
#define CMD_CHIP_ERASE              0x10
#define CMD_WR_CONFIG               0x19
#define CMD_RD_CONFIG               0x24
#define CMD_READ_STATUS             0x30
#define CMD_RESUME                  0x4C
#define CMD_DEBUG_INSTR_1B          (0x54|1)
#define CMD_DEBUG_INSTR_2B          (0x54|2)
#define CMD_DEBUG_INSTR_3B          (0x54|3)
#define CMD_BURST_WRITE             0x80
#define CMD_GET_CHIP_ID             0x68

#define STATUS_CHIP_ERASE_BUSY_BM   0x80

#define DUP_DBGDATA                 0x6260
#define DUP_FCTL                    0x6270
#define DUP_FADDRL                  0x6271
#define DUP_FADDRH                  0x6272
#define DUP_FWDATA                  0x6273
#define DUP_CLKCONSTA               0x709E
#define DUP_CLKCONCMD               0x70C6
#define DUP_MEMCTR                  0x70C7
#define DUP_DMA1CFGL                0x70D2
#define DUP_DMA1CFGH                0x70D3
#define DUP_DMA0CFGL                0x70D4
#define DUP_DMA0CFGH                0x70D5
#define DUP_DMAARM                  0x70D6

#define LOBYTE(w)  ((unsigned char)(w))
#define HIBYTE(w)  ((unsigned char)(((unsigned short)(w) >> 8) & 0xFF))

static const unsigned char dma_desc_0[8] = {
    HIBYTE(DUP_DBGDATA), LOBYTE(DUP_DBGDATA),
    HIBYTE(ADDR_BUF0),   LOBYTE(ADDR_BUF0),
    0, 0,
    31,
    0x11
};
static const unsigned char dma_desc_1[8] = {
    HIBYTE(ADDR_BUF0),    LOBYTE(ADDR_BUF0),
    HIBYTE(DUP_FWDATA),   LOBYTE(DUP_FWDATA),
    0, 0,
    18,
    0x42
};

static void write_debug_byte(unsigned char data) {
    for (unsigned char i = 0; i < 8; i++) {
        digitalWrite(DC, HIGH);
        digitalWrite(DD, (data & 0x80) ? HIGH : LOW);
        data <<= 1;
        digitalWrite(DC, LOW);
    }
}

static unsigned char read_debug_byte(void) {
    unsigned char data = 0;
    for (unsigned char i = 0; i < 8; i++) {
        digitalWrite(DC, HIGH);
        data <<= 1;
        if (digitalRead(DD) == HIGH) data |= 0x01;
        digitalWrite(DC, LOW);
    }
    return data;
}

static unsigned char wait_dup_ready(void) {
    unsigned int count = 0;
    while ((digitalRead(DD) == HIGH) && count < 16) {
        read_debug_byte();
        count++;
    }
    return (count == 16) ? 0 : 1;
}

static unsigned char debug_command(unsigned char cmd,
                                   const unsigned char *cmd_bytes,
                                   unsigned short num_cmd_bytes) {
    pinMode(DD, OUTPUT);
    write_debug_byte(cmd);
    for (unsigned short i = 0; i < num_cmd_bytes; i++) write_debug_byte(cmd_bytes[i]);
    pinMode(DD, INPUT);
    digitalWrite(DD, HIGH);
    wait_dup_ready();
    unsigned char output = read_debug_byte();
    pinMode(DD, OUTPUT);
    return output;
}

static void debug_init(void) {
    digitalWrite(DD, LOW);
    digitalWrite(DC, LOW);
    digitalWrite(RESET_PIN, LOW);
    delay(10);
    digitalWrite(DC, HIGH); delay(10);
    digitalWrite(DC, LOW);  delay(10);
    digitalWrite(DC, HIGH); delay(10);
    digitalWrite(DC, LOW);  delay(10);
    digitalWrite(RESET_PIN, HIGH); delay(10);
}

static unsigned char read_chip_id(void) {
    unsigned char id = 0;
    pinMode(DD, OUTPUT);
    delay(1);
    write_debug_byte(CMD_GET_CHIP_ID);
    pinMode(DD, INPUT);
    digitalWrite(DD, HIGH);
    delay(1);
    if (wait_dup_ready() == 1) {
        id = read_debug_byte();
        read_debug_byte();
    }
    pinMode(DD, OUTPUT);
    return id;
}

static void burst_write_block(const unsigned char *src, unsigned short n) {
    pinMode(DD, OUTPUT);
    write_debug_byte(CMD_BURST_WRITE | HIBYTE(n));
    write_debug_byte(LOBYTE(n));
    for (unsigned short i = 0; i < n; i++) write_debug_byte(src[i]);
    pinMode(DD, INPUT);
    digitalWrite(DD, HIGH);
    wait_dup_ready();
    read_debug_byte();
    pinMode(DD, OUTPUT);
}

static void chip_erase(void) {
    volatile unsigned char status;
    debug_command(CMD_CHIP_ERASE, 0, 0);
    do {
        status = debug_command(CMD_READ_STATUS, 0, 0);
    } while (status & STATUS_CHIP_ERASE_BUSY_BM);
}

static void write_xdata_memory_block(unsigned short address,
                                     const unsigned char *values,
                                     unsigned short num_bytes) {
    unsigned char instr[3];
    instr[0] = 0x90; instr[1] = HIBYTE(address); instr[2] = LOBYTE(address);
    debug_command(CMD_DEBUG_INSTR_3B, instr, 3);
    for (unsigned short i = 0; i < num_bytes; i++) {
        instr[0] = 0x74; instr[1] = values[i];
        debug_command(CMD_DEBUG_INSTR_2B, instr, 2);
        instr[0] = 0xF0;
        debug_command(CMD_DEBUG_INSTR_1B, instr, 1);
        instr[0] = 0xA3;
        debug_command(CMD_DEBUG_INSTR_1B, instr, 1);
    }
}

static void write_xdata_memory(unsigned short address, unsigned char value) {
    unsigned char instr[3];
    instr[0] = 0x90; instr[1] = HIBYTE(address); instr[2] = LOBYTE(address);
    debug_command(CMD_DEBUG_INSTR_3B, instr, 3);
    instr[0] = 0x74; instr[1] = value;
    debug_command(CMD_DEBUG_INSTR_2B, instr, 2);
    instr[0] = 0xF0;
    debug_command(CMD_DEBUG_INSTR_1B, instr, 1);
}

static unsigned char read_xdata_memory(unsigned short address) {
    unsigned char instr[3];
    instr[0] = 0x90; instr[1] = HIBYTE(address); instr[2] = LOBYTE(address);
    debug_command(CMD_DEBUG_INSTR_3B, instr, 3);
    instr[0] = 0xE0;
    return debug_command(CMD_DEBUG_INSTR_1B, instr, 1);
}

static void read_flash_memory_block(unsigned char bank,
                                    unsigned short flash_addr,
                                    unsigned short num_bytes,
                                    unsigned char *values) {
    unsigned char instr[3];
    unsigned short xdata_addr = (0x8000 + flash_addr);
    write_xdata_memory(DUP_MEMCTR, bank);
    instr[0] = 0x90; instr[1] = HIBYTE(xdata_addr); instr[2] = LOBYTE(xdata_addr);
    debug_command(CMD_DEBUG_INSTR_3B, instr, 3);
    for (unsigned short i = 0; i < num_bytes; i++) {
        instr[0] = 0xE0;
        values[i] = debug_command(CMD_DEBUG_INSTR_1B, instr, 1);
        instr[0] = 0xA3;
        debug_command(CMD_DEBUG_INSTR_1B, instr, 1);
    }
}

static void write_flash_memory_block(const unsigned char *src,
                                     unsigned long start_addr_words,
                                     unsigned short num_bytes) {
    write_xdata_memory_block(ADDR_DMA_DESC_0, dma_desc_0, 8);
    write_xdata_memory_block(ADDR_DMA_DESC_1, dma_desc_1, 8);
    unsigned char len[2] = {HIBYTE(num_bytes), LOBYTE(num_bytes)};
    write_xdata_memory_block((ADDR_DMA_DESC_0 + 4), len, 2);
    write_xdata_memory_block((ADDR_DMA_DESC_1 + 4), len, 2);
    write_xdata_memory(DUP_DMA0CFGH, HIBYTE(ADDR_DMA_DESC_0));
    write_xdata_memory(DUP_DMA0CFGL, LOBYTE(ADDR_DMA_DESC_0));
    write_xdata_memory(DUP_DMA1CFGH, HIBYTE(ADDR_DMA_DESC_1));
    write_xdata_memory(DUP_DMA1CFGL, LOBYTE(ADDR_DMA_DESC_1));
    write_xdata_memory(DUP_FADDRH, HIBYTE(start_addr_words));
    write_xdata_memory(DUP_FADDRL, LOBYTE(start_addr_words));
    write_xdata_memory(DUP_DMAARM, CH_DBG_TO_BUF0);
    burst_write_block(src, num_bytes);
    write_xdata_memory(DUP_DMAARM, CH_BUF0_TO_FLASH);
    write_xdata_memory(DUP_FCTL, 0x0A);
    while (read_xdata_memory(DUP_FCTL) & 0x80);
}

static void RunDUP(void) {
    digitalWrite(DD, LOW);
    digitalWrite(DC, LOW);
    digitalWrite(RESET_PIN, LOW);
    delay(10);
    digitalWrite(RESET_PIN, HIGH);
    delay(10);
}

#if CC2530_LED_WS2812
static void led_init(void) {
    led_strip.begin();
    led_strip.setBrightness(40);
    led_strip.setPixelColor(0, 0);
    led_strip.show();
}
/* led_on/led_off keep the "neutral activity blink" semantics → blue pulses */
static void led_on(void)  { led_strip.setPixelColor(0, led_strip.Color(0, 0, 255)); led_strip.show(); }
static void led_off(void) { led_strip.setPixelColor(0, 0); led_strip.show(); }
static void led_success_solid(void) {
    led_strip.setPixelColor(0, led_strip.Color(0, 255, 0));   /* solid green */
    led_strip.show();
}
static void led_fail_pulse(unsigned int ms) {     /* one red blink */
    led_strip.setPixelColor(0, led_strip.Color(255, 0, 0));
    led_strip.show();
    delay(ms);
    led_strip.setPixelColor(0, 0);
    led_strip.show();
    delay(ms);
}
static void led_blink_forever(unsigned int ms) {
    for (;;) led_fail_pulse(ms);
}
#else
static void led_init(void) {
    pinMode(LED, OUTPUT);
    digitalWrite(LED, LED_ACTIVE_LOW ? HIGH : LOW);
}
static void led_on(void)  { digitalWrite(LED, LED_ACTIVE_LOW ? LOW  : HIGH); }
static void led_off(void) { digitalWrite(LED, LED_ACTIVE_LOW ? HIGH : LOW ); }
static void led_success_solid(void) { led_on(); }
static void led_fail_pulse(unsigned int ms) {
    led_on(); delay(ms); led_off(); delay(ms);
}
static void led_blink_forever(unsigned int ms) {
    for (;;) led_fail_pulse(ms);
}
#endif

static void ProgrammerInit(void) {
    pinMode(DD, OUTPUT);
    pinMode(DC, OUTPUT);
    pinMode(RESET_PIN, OUTPUT);
    digitalWrite(DD, LOW);
    digitalWrite(DC, LOW);
    digitalWrite(RESET_PIN, HIGH);
}

/* Release the CC2530: put every pin in high-impedance so the chip can run
 * normally while this ESP32 stays permanently wired to it (armed mode). */
static void pins_release(void) {
    pinMode(DD, INPUT);
    pinMode(DC, INPUT);
    pinMode(RESET_PIN, INPUT);
}

/* ============================================================================ */
/* ----------------------- Standalone orchestration ------------------------- */
/* ============================================================================ */

static bool perform_flash(void) {
    Serial.println(F("=== CC2530 standalone flasher ==="));
    Serial.printf("Embedded firmware: %u bytes\n", (unsigned)KADSOL_FIRMWARE_BYTES);

    /* 1. Reset DUP into debug mode + read chip ID */
    debug_init();
    unsigned char chip_id = read_chip_id();
    Serial.printf("Chip ID: 0x%02X\n", chip_id);
    if (chip_id == 0) {
        Serial.println(F("ERROR: no CC2530 detected. Check wiring + 3.3 V on VCC + GND common."));
        return false;
    }

    /* 2. Chip erase (re-init around it as CCLoader does) */
    Serial.println(F("Erasing chip..."));
    RunDUP();
    debug_init();
    chip_erase();
    RunDUP();
    debug_init();
    Serial.println(F("  erase done."));

    /* 3. Switch DUP to external crystal (recommended for stable programming) */
    write_xdata_memory(DUP_CLKCONCMD, 0x80);
    unsigned long t0 = millis();
    while (read_xdata_memory(DUP_CLKCONSTA) != 0x80) {
        if (millis() - t0 > 2000) {
            Serial.println(F("WARNING: XOSC didn't stabilise in 2 s, continuing anyway"));
            break;
        }
    }

    /* 4. Enable DMA (disable DMA_PAUSE) */
    unsigned char debug_config = 0x22;
    debug_command(CMD_WR_CONFIG, &debug_config, 1);

    /* 5. Program flash in 512-byte chunks */
    Serial.println(F("Writing firmware..."));
    static unsigned char buf[512];
    unsigned long addr_words = 0;
    unsigned long progress_step = KADSOL_FIRMWARE_BYTES / 16;
    if (progress_step < 512) progress_step = 512;
    for (size_t off = 0; off < KADSOL_FIRMWARE_BYTES; off += 512) {
        size_t remaining = KADSOL_FIRMWARE_BYTES - off;
        size_t chunk = (remaining < 512) ? remaining : 512;
        for (size_t i = 0; i < 512; i++) {
            buf[i] = (i < chunk) ? pgm_read_byte(&KADSOL_FIRMWARE[off + i]) : 0xFF;
        }
        write_flash_memory_block(buf, addr_words, 512);
        addr_words += 128;
        if ((off % progress_step) < 512) {
            Serial.printf("  wrote %u / %u bytes (%u%%)\n",
                          (unsigned)(off + chunk),
                          (unsigned)KADSOL_FIRMWARE_BYTES,
                          (unsigned)((off + chunk) * 100UL / KADSOL_FIRMWARE_BYTES));
            led_on(); delay(20); led_off();
        }
    }
    Serial.println(F("  write done."));

    /* 6. Verify */
    Serial.println(F("Verifying..."));
    static unsigned char read_buf[512];
    unsigned long verify_addr_words = 0;
    for (size_t off = 0; off < KADSOL_FIRMWARE_BYTES; off += 512) {
        unsigned char bank = verify_addr_words / (512 * 16);
        unsigned int offset = (verify_addr_words % (512 * 16)) * 4;
        read_flash_memory_block(bank, offset, 512, read_buf);
        for (size_t i = 0; i < 512; i++) {
            unsigned char expected;
            if ((off + i) < KADSOL_FIRMWARE_BYTES) {
                expected = pgm_read_byte(&KADSOL_FIRMWARE[off + i]);
            } else {
                expected = 0xFF;
            }
            if (read_buf[i] != expected) {
                Serial.printf("VERIFY MISMATCH at 0x%05X: expected 0x%02X got 0x%02X\n",
                              (unsigned)(off + i), expected, read_buf[i]);
                return false;
            }
        }
        verify_addr_words += 128;
        if ((off % progress_step) < 512) {
            Serial.printf("  verified %u / %u bytes\n",
                          (unsigned)off, (unsigned)KADSOL_FIRMWARE_BYTES);
        }
    }
    Serial.println(F("  verify done."));

    /* 7. Boot the freshly-flashed CC2530 (release from debug mode) */
    RunDUP();
    return true;
}

/* === Armed mode ============================================================
 * The ESP32 stays permanently wired to the CC2530 (DD/DC/RST/GND — but NOT
 * 3V3: the CC2530 keeps being powered by its CH340E bridge). At boot all
 * pins go high-impedance so the CC2530 runs normally. Flashing only starts
 * when the literal command "FLASH" is received on Serial — so an accidental
 * ESP32 reboot can never wipe the CC2530.
 * ========================================================================= */

static void print_banner(void) {
    Serial.println();
    Serial.println(F("================================================="));
    Serial.println(F(" CC2530 standalone flasher (Kadsol CC2591)"));
    Serial.println(F("           --- ARMED MODE ---"));
    Serial.println(F("================================================="));
    Serial.printf(" Pinout: DD=GPIO%d  DC=GPIO%d  RST=GPIO%d  LED=GPIO%d\n",
                  DD, DC, RESET_PIN, LED);
    Serial.println(F(" Pins are in high-impedance: the CC2530 runs normally."));
    Serial.println(F(" To flash it with the embedded Kadsol firmware, type:"));
    Serial.println(F("     FLASH"));
    Serial.println(F(" (followed by Enter) on this serial console."));
    Serial.println(F(" Before flashing make sure:"));
    Serial.println(F("   - the CC2530 is powered (CH340E bridge or 3.3 V)"));
    Serial.println(F("   - nothing else is talking to the CC2530 (stop HA)"));
    Serial.println(F("================================================="));
    Serial.println(F("cc2530-flasher> waiting for FLASH command..."));
}

void setup(void) {
    Serial.begin(115200);
    delay(3000);                       /* let serial settle */
    led_init();
    pins_release();                    /* high-Z — do NOT disturb the CC2530 */
    print_banner();
}

void loop(void) {
    static String cmd_buf;
    static unsigned long last_banner = 0;

    /* Re-print a short prompt every 60 s so a late-attached console knows
     * what this device is. */
    if (millis() - last_banner > 60000UL) {
        Serial.println(F("cc2530-flasher> armed. Type FLASH to (re)flash the CC2530."));
        last_banner = millis();
    }

    while (Serial.available()) {
        char c = (char)Serial.read();
        if (c == '\r') continue;
        if (c != '\n') {
            cmd_buf += c;
            if (cmd_buf.length() > 32) cmd_buf = "";   /* junk guard */
            continue;
        }

        /* full line received */
        String line = cmd_buf;
        cmd_buf = "";
        line.trim();

        if (line.equalsIgnoreCase("FLASH")) {
            Serial.println(F("Got FLASH command — taking control of the CC2530 pins."));
            ProgrammerInit();
            bool ok = perform_flash();
            if (ok) {
                Serial.println();
                Serial.println(F("================================================="));
                Serial.println(F("  FLASH SUCCESS — solid green LED"));
                Serial.println(F("  Pins released: the CC2530 is rebooting on the"));
                Serial.println(F("  fresh firmware. You can resume normal use"));
                Serial.println(F("  (CH340E + HA) without touching the wiring."));
                Serial.println(F("================================================="));
                pins_release();        /* hand the chip back immediately */
                led_success_solid();
            } else {
                Serial.println();
                Serial.println(F("================================================="));
                Serial.println(F("  FLASH FAILED — fast-blinking red LED"));
                Serial.println(F("  Check wiring/power/GND and type FLASH again"));
                Serial.println(F("  (or power-cycle the ESP32)."));
                Serial.println(F("================================================="));
                pins_release();
                /* blink red for 30 s, then return to armed mode */
                for (int i = 0; i < 150; i++) led_fail_pulse(100);
                print_banner();
            }
        } else if (line.equalsIgnoreCase("STATUS")) {
            Serial.printf("cc2530-flasher> armed, firmware embedded: %u bytes, pins HI-Z\n",
                          (unsigned)KADSOL_FIRMWARE_BYTES);
        } else if (line.length() > 0) {
            Serial.printf("cc2530-flasher> unknown command '%s' (try FLASH or STATUS)\n",
                          line.c_str());
        }
    }
    delay(10);
}
