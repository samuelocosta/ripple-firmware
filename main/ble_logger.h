#pragma once

#include <stdbool.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief Inicializa o interceptador de logs do ESP-IDF (esp_log_set_vprintf)
 *        para streaming sem fio via BLE Notification.
 */
esp_err_t ble_logger_init(void);

/**
 * @brief Habilita ou desabilita o streaming de logs via BLE.
 */
void ble_logger_set_enabled(bool enabled);

/**
 * @brief Retorna se o streaming de logs via BLE está ativo.
 */
bool ble_logger_is_enabled(void);

#ifdef __cplusplus
}
#endif
