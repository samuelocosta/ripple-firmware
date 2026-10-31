#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    OTA_STATE_IDLE = 0,
    OTA_STATE_IN_PROGRESS,
    OTA_STATE_FINISHED,
    OTA_STATE_ERROR
} ota_state_t;

typedef struct {
    ota_state_t state;
    size_t total_size;
    size_t written_size;
    uint8_t progress_percent;
    const char *running_partition;
    const char *target_partition;
    char error_msg[64];
} ota_status_info_t;

/**
 * @brief Inicializa o subsistema de OTA, verifica rollback e confirma partição atual como válida.
 */
esp_err_t ota_manager_init(void);

/**
 * @brief Inicia uma nova sessão de atualização OTA via BLE.
 * @param image_size Tamanho total do binário em bytes (0 se desconhecido).
 */
esp_err_t ota_manager_begin(size_t image_size);

/**
 * @brief Grava um bloco sequencial de bytes na partição OTA de destino.
 */
esp_err_t ota_manager_write(const uint8_t *data, size_t length);

/**
 * @brief Finaliza a escrita, valida o cabeçalho/checksum da imagem e define a partição de boot.
 */
esp_err_t ota_manager_finish(void);

/**
 * @brief Cancela uma sessão OTA em andamento e limpa recursos.
 */
void ota_manager_abort(const char *reason);

/**
 * @brief Agenda a reinicialização da boia após delay especificado em ms.
 */
void ota_manager_reboot_delayed(uint32_t delay_ms);

/**
 * @brief Obtém o estado atual da atualização OTA.
 */
void ota_manager_get_status(ota_status_info_t *status);

/**
 * @brief Preenche buffer com JSON contendo informações de partições e versão.
 */
esp_err_t ota_manager_get_info_json(char *buf, size_t max_len);

/**
 * @brief Força marcação manual de rollback para voltar à partição anterior.
 */
esp_err_t ota_manager_rollback(void);

#ifdef __cplusplus
}
#endif
