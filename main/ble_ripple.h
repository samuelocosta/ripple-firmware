#pragma once

#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"
#include "wave_detector.h"

#ifdef __cplusplus
extern "C" {
#endif

// UUIDs dos Serviços BLE
#define RIPPLE_NUS_SVC_UUID_STR       "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
#define RIPPLE_NUS_RX_CHAR_UUID_STR   "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
#define RIPPLE_NUS_TX_CHAR_UUID_STR   "6e400003-b5a3-f393-e0a9-e50e24dcca9e"

#define RIPPLE_OTA_SVC_UUID_STR       "80140001-3850-424a-b5e0-9e67d266ff00"
#define RIPPLE_OTA_CTRL_CHAR_UUID_STR "80140002-3850-424a-b5e0-9e67d266ff00"
#define RIPPLE_OTA_DATA_CHAR_UUID_STR "80140003-3850-424a-b5e0-9e67d266ff00"

/**
 * @brief Callback chamado quando um comando de texto chega via BLE GATT (ou USB Serial).
 */
typedef void (*ripple_cmd_handler_t)(const char *cmd_str);

/**
 * @brief Inicializa a pilha NimBLE, o servidor GATT (NUS + OTA Service)
 *        e o gerenciador de pacotes BLE Advertisement.
 */
esp_err_t ble_ripple_init(ripple_cmd_handler_t cmd_cb);

/**
 * @brief Atualiza o modo de anúncio BLE conforme o estado da boia
 *        (MONITORING = Beacon normal; FALL_ALERT = Multiplexação Apple/FastPair/Emergency).
 */
void ble_ripple_set_alert_mode(bool alert_active);

/**
 * @brief Envia uma string/JSON via BLE GATT Notification (NUS TX Characteristic) para o cliente conectado.
 */
esp_err_t ble_ripple_notify_text(const char *text);

/**
 * @brief Envia uma resposta/evento JSON via BLE GATT Notification (OTA Control Characteristic).
 */
esp_err_t ble_ripple_notify_ota_ctrl(const char *text);

/**
 * @brief Retorna se há algum cliente BLE conectado no momento.
 */
bool ble_ripple_is_connected(void);

/**
 * @brief Retorna se o modo de stream contínuo de telemetria BLE está habilitado.
 */
bool ble_ripple_is_streaming(void);

/**
 * @brief Liga/desliga o stream contínuo de telemetria BLE.
 */
void ble_ripple_set_streaming(bool enable);

#ifdef __cplusplus
}
#endif
