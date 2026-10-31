#pragma once

#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    LED_MODE_CALIBRATING = 0,  // Azul pulsante
    LED_MODE_MONITORING,       // Verde suave (ou roxo se cliente BLE conectado)
    LED_MODE_FALL_ALERT,       // Estrobo Vermelho/Branco + LED Vermelho rápido
} ripple_led_mode_t;

/**
 * @brief Inicializa os pinos de energia (GPIO 34 / GPIO 45 off), LED Vermelho (GPIO 13)
 *        e o LED RGB WS2812 (GPIO 33), iniciando a task de animação visual.
 */
esp_err_t led_indicator_init(void);

/**
 * @brief Define o modo visual atual da boia.
 */
void led_indicator_set_mode(ripple_led_mode_t mode);

/**
 * @brief Informa ao indicador luminoso se há um cliente BLE GATT conectado (ex: notebook).
 */
void led_indicator_set_ble_connected(bool connected);

#ifdef __cplusplus
}
#endif
