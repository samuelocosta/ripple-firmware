#include "ota_manager.h"

#include <stdio.h>
#include <string.h>
#include "esp_log.h"
#include "esp_ota_ops.h"
#include "esp_app_format.h"
#include "esp_partition.h"
#include "esp_system.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/timers.h"

static const char *TAG = "OTA_MGR";

static esp_ota_handle_t s_ota_handle = 0;
static const esp_partition_t *s_running_partition = NULL;
static const esp_partition_t *s_update_partition = NULL;
static ota_status_info_t s_status = {
    .state = OTA_STATE_IDLE,
    .total_size = 0,
    .written_size = 0,
    .progress_percent = 0,
    .running_partition = "unknown",
    .target_partition = "unknown",
    .error_msg = {0}
};

static TimerHandle_t s_reboot_timer = NULL;

static void reboot_timer_cb(TimerHandle_t xTimer)
{
    (void)xTimer;
    ESP_LOGI(TAG, "Reiniciando sistema para aplicar nova particao de firmware...");
    esp_restart();
}

esp_err_t ota_manager_init(void)
{
    s_running_partition = esp_ota_get_running_partition();
    if (s_running_partition) {
        s_status.running_partition = s_running_partition->label;
        ESP_LOGI(TAG, "Particao ativa em execucao: '%s' (offset 0x%08" PRIX32 ", size 0x%08" PRIX32 ")",
                 s_running_partition->label, (uint32_t)s_running_partition->address, (uint32_t)s_running_partition->size);
    }

    const esp_app_desc_t *app_desc = esp_app_get_description();
    if (app_desc) {
        ESP_LOGI(TAG, "Firmware: %s | Versao: %s | Compilado: %s %s | IDF: %s",
                 app_desc->project_name, app_desc->version, app_desc->date, app_desc->time, app_desc->idf_ver);
    }

    // Verifica se acabamos de subir via OTA e precisamos validar para cancelar rollback
    esp_ota_img_states_t ota_state;
    if (esp_ota_get_state_partition(s_running_partition, &ota_state) == ESP_OK) {
        if (ota_state == ESP_OTA_IMG_PENDING_VERIFY) {
            ESP_LOGW(TAG, "Nova imagem detectada em modo PENDING_VERIFY! Confirmando saude do firmware...");
            esp_err_t err = esp_ota_mark_app_valid_cancel_rollback();
            if (err == ESP_OK) {
                ESP_LOGI(TAG, "Firmware marcado como VALIDO permanentemente! Rollback cancelado com sucesso.");
            } else {
                ESP_LOGE(TAG, "Falha ao validar app (err=%s). Rollback ocorrera se houver reboot.", esp_err_to_name(err));
            }
        } else if (ota_state == ESP_OTA_IMG_VALID) {
            ESP_LOGI(TAG, "Estado da particao: VALID");
        }
    }

    return ESP_OK;
}

esp_err_t ota_manager_begin(size_t image_size)
{
    if (s_status.state == OTA_STATE_IN_PROGRESS) {
        ESP_LOGW(TAG, "Sessao OTA anterior ainda em andamento. Abortando-a antes de iniciar nova.");
        ota_manager_abort("Reiniciada pelo cliente");
    }

    s_running_partition = esp_ota_get_running_partition();
    s_update_partition = esp_ota_get_next_update_partition(NULL);

    if (!s_update_partition) {
        snprintf(s_status.error_msg, sizeof(s_status.error_msg), "Particao OTA de destino nao encontrada");
        s_status.state = OTA_STATE_ERROR;
        ESP_LOGE(TAG, "%s", s_status.error_msg);
        return ESP_ERR_NOT_FOUND;
    }

    s_status.target_partition = s_update_partition->label;
    s_status.total_size = image_size;
    s_status.written_size = 0;
    s_status.progress_percent = 0;
    s_status.error_msg[0] = '\0';

    ESP_LOGI(TAG, "Iniciando gravacao OTA: destino '%s' (offset 0x%08" PRIX32 ", size 0x%08" PRIX32 ", imagem: %zu bytes)",
             s_update_partition->label, (uint32_t)s_update_partition->address, (uint32_t)s_update_partition->size, image_size);

    esp_err_t err = esp_ota_begin(s_update_partition,
                                  (image_size > 0) ? image_size : OTA_WITH_SEQUENTIAL_WRITES,
                                  &s_ota_handle);
    if (err != ESP_OK) {
        snprintf(s_status.error_msg, sizeof(s_status.error_msg), "esp_ota_begin falhou: %s", esp_err_to_name(err));
        s_status.state = OTA_STATE_ERROR;
        ESP_LOGE(TAG, "%s", s_status.error_msg);
        return err;
    }

    s_status.state = OTA_STATE_IN_PROGRESS;
    ESP_LOGI(TAG, "Sessao OTA iniciada com sucesso. Pronto para receber blocos binarios.");
    return ESP_OK;
}

esp_err_t ota_manager_write(const uint8_t *data, size_t length)
{
    if (s_status.state != OTA_STATE_IN_PROGRESS || s_ota_handle == 0) {
        ESP_LOGE(TAG, "Tentativa de escrita sem sessao OTA ativa");
        return ESP_ERR_INVALID_STATE;
    }

    if (!data || length == 0) {
        return ESP_OK;
    }

    esp_err_t err = esp_ota_write(s_ota_handle, data, length);
    if (err != ESP_OK) {
        snprintf(s_status.error_msg, sizeof(s_status.error_msg), "esp_ota_write erro: %s", esp_err_to_name(err));
        s_status.state = OTA_STATE_ERROR;
        ESP_LOGE(TAG, "%s", s_status.error_msg);
        esp_ota_abort(s_ota_handle);
        s_ota_handle = 0;
        return err;
    }

    s_status.written_size += length;
    if (s_status.total_size > 0) {
        uint8_t new_pct = (uint8_t)((s_status.written_size * 100ULL) / s_status.total_size);
        if (new_pct > 100) new_pct = 100;
        s_status.progress_percent = new_pct;
    }

    return ESP_OK;
}

esp_err_t ota_manager_finish(void)
{
    if (s_status.state != OTA_STATE_IN_PROGRESS || s_ota_handle == 0) {
        return ESP_ERR_INVALID_STATE;
    }

    ESP_LOGI(TAG, "Finalizando escrita OTA (%zu bytes recebidos). Validando hash e cabecalho...", s_status.written_size);
    esp_err_t err = esp_ota_end(s_ota_handle);
    s_ota_handle = 0;

    if (err != ESP_OK) {
        snprintf(s_status.error_msg, sizeof(s_status.error_msg), "esp_ota_end erro: %s", esp_err_to_name(err));
        s_status.state = OTA_STATE_ERROR;
        ESP_LOGE(TAG, "%s", s_status.error_msg);
        return err;
    }

    err = esp_ota_set_boot_partition(s_update_partition);
    if (err != ESP_OK) {
        snprintf(s_status.error_msg, sizeof(s_status.error_msg), "esp_ota_set_boot erro: %s", esp_err_to_name(err));
        s_status.state = OTA_STATE_ERROR;
        ESP_LOGE(TAG, "%s", s_status.error_msg);
        return err;
    }

    s_status.state = OTA_STATE_FINISHED;
    s_status.progress_percent = 100;
    ESP_LOGI(TAG, "Atualizacao OTA concluida com SUCESSO! Nova particao de boot definida para: '%s'",
             s_update_partition->label);

    return ESP_OK;
}

void ota_manager_abort(const char *reason)
{
    if (s_status.state == OTA_STATE_IN_PROGRESS && s_ota_handle != 0) {
        esp_ota_abort(s_ota_handle);
        s_ota_handle = 0;
    }
    s_status.state = OTA_STATE_ERROR;
    snprintf(s_status.error_msg, sizeof(s_status.error_msg), "%s", reason ? reason : "Abortado");
    ESP_LOGW(TAG, "Sessao OTA abortada: %s", s_status.error_msg);
}

void ota_manager_reboot_delayed(uint32_t delay_ms)
{
    if (s_reboot_timer == NULL) {
        s_reboot_timer = xTimerCreate("ota_reboot", pdMS_TO_TICKS(delay_ms), pdFALSE, NULL, reboot_timer_cb);
    } else {
        xTimerChangePeriod(s_reboot_timer, pdMS_TO_TICKS(delay_ms), 0);
    }

    if (s_reboot_timer) {
        ESP_LOGI(TAG, "Reboot agendado para daqui a %" PRIu32 " ms", delay_ms);
        xTimerStart(s_reboot_timer, 0);
    } else {
        vTaskDelay(pdMS_TO_TICKS(delay_ms));
        esp_restart();
    }
}

void ota_manager_get_status(ota_status_info_t *status)
{
    if (status) {
        *status = s_status;
    }
}

esp_err_t ota_manager_get_info_json(char *buf, size_t max_len)
{
    if (!buf || max_len == 0) {
        return ESP_ERR_INVALID_ARG;
    }

    const esp_app_desc_t *desc = esp_app_get_description();
    const esp_partition_t *running = esp_ota_get_running_partition();
    const esp_partition_t *next = esp_ota_get_next_update_partition(NULL);
    const esp_partition_t *boot = esp_ota_get_boot_partition();

    snprintf(buf, max_len,
             "{\"app\":\"%s\",\"ver\":\"%s\",\"build\":\"%s %s\",\"idf\":\"%s\","
             "\"running\":\"%s\",\"boot\":\"%s\",\"next_ota\":\"%s\",\"written\":%zu,\"pct\":%u}\n",
             desc ? desc->project_name : "ripple",
             desc ? desc->version : "unknown",
             desc ? desc->date : "",
             desc ? desc->time : "",
             desc ? desc->idf_ver : "",
             running ? running->label : "none",
             boot ? boot->label : "none",
             next ? next->label : "none",
             s_status.written_size,
             (unsigned int)s_status.progress_percent);

    return ESP_OK;
}

esp_err_t ota_manager_rollback(void)
{
    ESP_LOGW(TAG, "Solicitado rollback manual de firmware!");
    esp_err_t err = esp_ota_mark_app_invalid_rollback_and_reboot();
    return err;
}
