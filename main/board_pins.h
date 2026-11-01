#pragma once

#include "driver/gpio.h"
#include "hal/i2c_types.h"

/**
 * @file board_pins.h
 * @brief Mapeamento completo de hardware da placa TENSTAR ROBOT TS-ESP32-S3 (ESP32S3-1.14TFT)
 *        SoC: ESP32-S3FH4R2 (4MB Flash, 2MB PSRAM)
 */

/* --- Alimentação dos Periféricos Onboard --- */
#define BOARD_TFT_I2C_POWER_GPIO    GPIO_NUM_21  // HIGH = Liga 3.3V para sensores I2C e Display TFT
#define BOARD_NEOPIXEL_POWER_GPIO   GPIO_NUM_34  // HIGH = Liga alimentação do LED RGB WS2812

/* --- Barramento I2C Onboard (QMI8658C + BMP280 + Conector QWIIC SH1.0-4P) --- */
#define BOARD_I2C_PORT              I2C_NUM_0
#define BOARD_I2C_SDA_GPIO          GPIO_NUM_42
#define BOARD_I2C_SCL_GPIO          GPIO_NUM_41
#define BOARD_I2C_FREQ_HZ           400000

/* --- Endereços I2C e Pinos dos Sensores --- */
#define BOARD_QMI8658_I2C_ADDR_PRIMARY    0x6B
#define BOARD_QMI8658_I2C_ADDR_ALT        0x6A
#define BOARD_QMI8658_INT1_GPIO           GPIO_NUM_3

#define BOARD_BMP280_I2C_ADDR_PRIMARY     0x77
#define BOARD_BMP280_I2C_ADDR_ALT         0x76

/* --- Indicadores Luminosos (LEDs) --- */
#define BOARD_LED_RED_GPIO          GPIO_NUM_13  // LED Vermelho simples (D13)
#define BOARD_NEOPIXEL_GPIO         GPIO_NUM_33  // LED RGB Endereçável WS2812 (1 pixel)

/* --- Monitoramento de Bateria (Divisor Resistivo Onboard) --- */
#define BOARD_BATTERY_ADC_CHANNEL   ADC_CHANNEL_3 // GPIO 4 (A4) analógico
#define BOARD_BATTERY_DIVIDER_RATIO 2.0f         // Razão do divisor de tensão (100k/100k = x2.0)

/* --- Botão Onboard (Apenas referência / bancada) --- */
#define BOARD_BOOT_BUTTON_GPIO      GPIO_NUM_0   // Ativo em LOW

/* --- Display ST7789 1.14" 240x135 (Referência / Desligado na boia) --- */
#define BOARD_TFT_CS_GPIO           GPIO_NUM_7
#define BOARD_TFT_DC_GPIO           GPIO_NUM_39
#define BOARD_TFT_RESET_GPIO        GPIO_NUM_40
#define BOARD_TFT_BACKLIGHT_GPIO    GPIO_NUM_45
#define BOARD_TFT_MOSI_GPIO         GPIO_NUM_35
#define BOARD_TFT_SCLK_GPIO         GPIO_NUM_36
#define BOARD_TFT_MISO_GPIO         GPIO_NUM_37
