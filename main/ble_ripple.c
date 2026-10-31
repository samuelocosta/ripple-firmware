#include "ble_ripple.h"
#include "led_indicator.h"
#include "ota_manager.h"

#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include "esp_log.h"
#include "nvs_flash.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "nimble/nimble_port.h"
#include "nimble/nimble_port_freertos.h"
#include "host/ble_hs.h"
#include "host/util/util.h"
#include "services/gap/ble_svc_gap.h"
#include "services/gatt/ble_svc_gatt.h"
#include "esp_bt.h"

static const char *TAG = "BLE_RIPPLE";

static uint8_t s_own_addr_type;
static uint16_t s_conn_handle = BLE_HS_CONN_HANDLE_NONE;
static uint16_t s_tx_attr_handle = 0;
static uint16_t s_ota_ctrl_attr_handle = 0;
static volatile bool s_is_connected = false;
static volatile bool s_alert_active = false;
static volatile bool s_stream_enabled = false;
static uint8_t s_adv_slot = 0;
static char s_last_tx_buf[192] = "{\"state\":\"BOOT\"}";
static char s_last_ota_ctrl_buf[128] = "{\"status\":\"idle\"}";
static ripple_cmd_handler_t s_cmd_cb = NULL;
static size_t s_last_notified_pct = 0;

/* --- UUIDs do Nordic UART Service (NUS) ---
 * Service: 6E400001-B5A3-F393-E0A9-E50E24DCCA9E
 * RX Char: 6E400002-B5A3-F393-E0A9-E50E24DCCA9E (Write / Write No Resp)
 * TX Char: 6E400003-B5A3-F393-E0A9-E50E24DCCA9E (Notify / Read)
 */
static const ble_uuid128_t kNusSvcUuid =
    BLE_UUID128_INIT(0x9e, 0xca, 0xdc, 0x24, 0x0e, 0xe5, 0xa9, 0xe0,
                     0x93, 0xf3, 0xa3, 0xb5, 0x01, 0x00, 0x40, 0x6e);

static const ble_uuid128_t kNusRxUuid =
    BLE_UUID128_INIT(0x9e, 0xca, 0xdc, 0x24, 0x0e, 0xe5, 0xa9, 0xe0,
                     0x93, 0xf3, 0xa3, 0xb5, 0x02, 0x00, 0x40, 0x6e);

static const ble_uuid128_t kNusTxUuid =
    BLE_UUID128_INIT(0x9e, 0xca, 0xdc, 0x24, 0x0e, 0xe5, 0xa9, 0xe0,
                     0x93, 0xf3, 0xa3, 0xb5, 0x03, 0x00, 0x40, 0x6e);

/* --- UUIDs do Serviço Dedicado de OTA Ripple ---
 * Service: 80140001-3850-424A-B5E0-9E67D266FF00
 * Control: 80140002-3850-424A-B5E0-9E67D266FF00 (Write / Notify / Read)
 * Data:    80140003-3850-424A-B5E0-9E67D266FF00 (Write / Write No Resp)
 */
static const ble_uuid128_t kOtaSvcUuid =
    BLE_UUID128_INIT(0x00, 0xff, 0x66, 0xd2, 0x67, 0x9e, 0xe0, 0xb5,
                     0x4a, 0x42, 0x50, 0x38, 0x01, 0x00, 0x14, 0x80);

static const ble_uuid128_t kOtaCtrlUuid =
    BLE_UUID128_INIT(0x00, 0xff, 0x66, 0xd2, 0x67, 0x9e, 0xe0, 0xb5,
                     0x4a, 0x42, 0x50, 0x38, 0x02, 0x00, 0x14, 0x80);

static const ble_uuid128_t kOtaDataUuid =
    BLE_UUID128_INIT(0x00, 0xff, 0x66, 0xd2, 0x67, 0x9e, 0xe0, 0xb5,
                     0x4a, 0x42, 0x50, 0x38, 0x03, 0x00, 0x14, 0x80);

/* --- Payloads Raw BLE Advertisement para Pop-up Nativo Sem App --- */

// Slot 0: Apple Continuity Proximity Pairing (Company ID 0x004C, Type 0x07)
static const uint8_t kAppleProximityAdv[] = {
    0x1E, 0xFF, 0x4C, 0x00, 0x07, 0x19, 0x07, 0x0E,
    0x20, 0x75, 0xAA, 0x30, 0x01, 0x00, 0x00, 0x45,
    0x12, 0x12, 0x12, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00
};

// Slot 1: Google Fast Pair Service (UUID 0xFE2C)
static const uint8_t kGoogleFastPairAdv[] = {
    0x02, 0x01, 0x06,
    0x03, 0x03, 0x2C, 0xFE,
    0x06, 0x16, 0x2C, 0xFE, 0xCD, 0x82, 0x56,
    0x02, 0x0A, 0x09
};

// Slot 2: Samsung Galaxy Buds Proximity Pairing (Company ID 0x0075)
static const uint8_t kSamsungGalaxyAdv[] = {
    0x13, 0xFF, 0x75, 0x00, 0x01, 0x00, 0x02, 0x00,
    0x01, 0x01, 0xFF, 0x00, 0x00, 0x41, 0x6E, 0x64,
    0x72, 0x6F, 0x69, 0x64
};

static void start_advertising_slot(uint8_t slot);

static int gatt_nus_access_cb(uint16_t conn_handle, uint16_t attr_handle,
                              struct ble_gatt_access_ctxt *ctxt, void *arg)
{
    (void)conn_handle;
    (void)attr_handle;
    (void)arg;

    if (ctxt->op == BLE_GATT_ACCESS_OP_WRITE_CHR) {
        uint16_t om_len = OS_MBUF_PKTLEN(ctxt->om);
        if (om_len == 0 || om_len >= 120) {
            return BLE_ATT_ERR_INVALID_ATTR_VALUE_LEN;
        }

        char cmd_buf[128] = {0};
        uint16_t copied = 0;
        ble_hs_mbuf_to_flat(ctxt->om, cmd_buf, sizeof(cmd_buf) - 1, &copied);
        cmd_buf[copied] = '\0';

        while (copied > 0 && (cmd_buf[copied - 1] == '\r' || cmd_buf[copied - 1] == '\n' || cmd_buf[copied - 1] == ' ')) {
            cmd_buf[--copied] = '\0';
        }

        ESP_LOGI(TAG, "Comando NUS recebido: '%s'", cmd_buf);
        if (s_cmd_cb && copied > 0) {
            s_cmd_cb(cmd_buf);
        }
        return 0;
    }

    if (ctxt->op == BLE_GATT_ACCESS_OP_READ_CHR) {
        int rc = os_mbuf_append(ctxt->om, s_last_tx_buf, strlen(s_last_tx_buf));
        return (rc == 0) ? 0 : BLE_ATT_ERR_INSUFFICIENT_RES;
    }

    return BLE_ATT_ERR_UNLIKELY;
}

static int gatt_ota_ctrl_access_cb(uint16_t conn_handle, uint16_t attr_handle,
                                   struct ble_gatt_access_ctxt *ctxt, void *arg)
{
    (void)conn_handle;
    (void)attr_handle;
    (void)arg;

    if (ctxt->op == BLE_GATT_ACCESS_OP_WRITE_CHR) {
        uint16_t om_len = OS_MBUF_PKTLEN(ctxt->om);
        if (om_len == 0 || om_len >= 128) {
            return BLE_ATT_ERR_INVALID_ATTR_VALUE_LEN;
        }

        char cmd_buf[128] = {0};
        uint16_t copied = 0;
        ble_hs_mbuf_to_flat(ctxt->om, cmd_buf, sizeof(cmd_buf) - 1, &copied);
        cmd_buf[copied] = '\0';

        while (copied > 0 && (cmd_buf[copied - 1] == '\r' || cmd_buf[copied - 1] == '\n' || cmd_buf[copied - 1] == ' ')) {
            cmd_buf[--copied] = '\0';
        }

        ESP_LOGI(TAG, "Comando OTA Ctrl recebido: '%s'", cmd_buf);

        // Trata comandos de controle OTA: "start <size>", "finish", "abort", "info"
        if (strncasecmp(cmd_buf, "start", 5) == 0) {
            size_t img_size = 0;
            // Tenta ler formato "start <size>"
            const char *p = cmd_buf + 5;
            while (*p == ' ' || *p == ':') p++;
            if (*p != '\0') {
                img_size = (size_t)strtoul(p, NULL, 10);
            }

            s_last_notified_pct = 0;
            esp_err_t err = ota_manager_begin(img_size);
            char resp[128];
            if (err == ESP_OK) {
                snprintf(resp, sizeof(resp), "{\"status\":\"ready\",\"chunk_size\":512}\n");
            } else {
                snprintf(resp, sizeof(resp), "{\"status\":\"error\",\"msg\":\"%s\"}\n", esp_err_to_name(err));
            }
            ble_ripple_notify_ota_ctrl(resp);
            return 0;
        } else if (strcasecmp(cmd_buf, "finish") == 0) {
            esp_err_t err = ota_manager_finish();
            char resp[128];
            if (err == ESP_OK) {
                snprintf(resp, sizeof(resp), "{\"status\":\"success\",\"msg\":\"rebooting_in_1s\"}\n");
                ble_ripple_notify_ota_ctrl(resp);
                ota_manager_reboot_delayed(1000);
            } else {
                snprintf(resp, sizeof(resp), "{\"status\":\"error\",\"msg\":\"%s\"}\n", esp_err_to_name(err));
                ble_ripple_notify_ota_ctrl(resp);
            }
            return 0;
        } else if (strcasecmp(cmd_buf, "abort") == 0) {
            ota_manager_abort("Cancelado pelo cliente");
            ble_ripple_notify_ota_ctrl("{\"status\":\"aborted\"}\n");
            return 0;
        } else if (strcasecmp(cmd_buf, "info") == 0 || strcasecmp(cmd_buf, "status") == 0) {
            char info_buf[192];
            ota_manager_get_info_json(info_buf, sizeof(info_buf));
            ble_ripple_notify_ota_ctrl(info_buf);
            return 0;
        }

        ble_ripple_notify_ota_ctrl("{\"status\":\"error\",\"msg\":\"unknown_ota_cmd\"}\n");
        return 0;
    }

    if (ctxt->op == BLE_GATT_ACCESS_OP_READ_CHR) {
        char info_buf[192];
        ota_manager_get_info_json(info_buf, sizeof(info_buf));
        int rc = os_mbuf_append(ctxt->om, info_buf, strlen(info_buf));
        return (rc == 0) ? 0 : BLE_ATT_ERR_INSUFFICIENT_RES;
    }

    return BLE_ATT_ERR_UNLIKELY;
}

static int gatt_ota_data_access_cb(uint16_t conn_handle, uint16_t attr_handle,
                                   struct ble_gatt_access_ctxt *ctxt, void *arg)
{
    (void)conn_handle;
    (void)attr_handle;
    (void)arg;

    if (ctxt->op == BLE_GATT_ACCESS_OP_WRITE_CHR) {
        uint16_t om_len = OS_MBUF_PKTLEN(ctxt->om);
        if (om_len == 0) {
            return 0;
        }

        // Buffer dinâmico na pilha para receber o bloco binário do MTU negociado
        uint8_t chunk_buf[512];
        uint16_t to_copy = (om_len > sizeof(chunk_buf)) ? sizeof(chunk_buf) : om_len;
        uint16_t copied = 0;
        ble_hs_mbuf_to_flat(ctxt->om, chunk_buf, to_copy, &copied);

        esp_err_t err = ota_manager_write(chunk_buf, copied);
        if (err != ESP_OK) {
            char err_resp[128];
            snprintf(err_resp, sizeof(err_resp), "{\"event\":\"error\",\"msg\":\"write_failed\"}\n");
            ble_ripple_notify_ota_ctrl(err_resp);
            return BLE_ATT_ERR_UNLIKELY;
        }

        // Notifica progresso a cada 5% para economizar largura de banda BLE
        ota_status_info_t st;
        ota_manager_get_status(&st);
        if (st.progress_percent >= s_last_notified_pct + 5 || st.progress_percent == 100) {
            s_last_notified_pct = st.progress_percent;
            char prog_msg[96];
            snprintf(prog_msg, sizeof(prog_msg),
                     "{\"event\":\"progress\",\"written\":%zu,\"pct\":%u}\n",
                     st.written_size, (unsigned int)st.progress_percent);
            ble_ripple_notify_ota_ctrl(prog_msg);
        }

        return 0;
    }

    return BLE_ATT_ERR_UNLIKELY;
}

static const struct ble_gatt_svc_def s_gatt_svcs[] = {
    // 1. Nordic UART Service (NUS): Telemetria, Logs e Comandos
    {
        .type = BLE_GATT_SVC_TYPE_PRIMARY,
        .uuid = &kNusSvcUuid.u,
        .characteristics = (struct ble_gatt_chr_def[]) {
            {
                // RX Characteristic (Write / Write No Response)
                .uuid = &kNusRxUuid.u,
                .access_cb = gatt_nus_access_cb,
                .flags = BLE_GATT_CHR_F_WRITE | BLE_GATT_CHR_F_WRITE_NO_RSP,
            },
            {
                // TX Characteristic (Notify / Read)
                .uuid = &kNusTxUuid.u,
                .access_cb = gatt_nus_access_cb,
                .val_handle = &s_tx_attr_handle,
                .flags = BLE_GATT_CHR_F_NOTIFY | BLE_GATT_CHR_F_READ,
            },
            { 0 }
        },
    },
    // 2. Ripple Dedicated BLE OTA Service: Atualização Segura de Firmware
    {
        .type = BLE_GATT_SVC_TYPE_PRIMARY,
        .uuid = &kOtaSvcUuid.u,
        .characteristics = (struct ble_gatt_chr_def[]) {
            {
                // OTA Control Characteristic (Write / Notify / Read)
                .uuid = &kOtaCtrlUuid.u,
                .access_cb = gatt_ota_ctrl_access_cb,
                .val_handle = &s_ota_ctrl_attr_handle,
                .flags = BLE_GATT_CHR_F_WRITE | BLE_GATT_CHR_F_NOTIFY | BLE_GATT_CHR_F_READ,
            },
            {
                // OTA Data Characteristic (Write / Write No Response para velocidade máxima)
                .uuid = &kOtaDataUuid.u,
                .access_cb = gatt_ota_data_access_cb,
                .flags = BLE_GATT_CHR_F_WRITE | BLE_GATT_CHR_F_WRITE_NO_RSP,
            },
            { 0 }
        },
    },
    { 0 }
};

static int ble_gap_event_cb(struct ble_gap_event *event, void *arg)
{
    (void)arg;

    switch (event->type) {
        case BLE_GAP_EVENT_CONNECT:
            if (event->connect.status == 0) {
                s_conn_handle = event->connect.conn_handle;
                s_is_connected = true;
                led_indicator_set_ble_connected(true);
                ESP_LOGI(TAG, "Cliente BLE conectado (handle=%d)", s_conn_handle);
            } else {
                s_is_connected = false;
                led_indicator_set_ble_connected(false);
                start_advertising_slot(0);
            }
            break;

        case BLE_GAP_EVENT_DISCONNECT:
            ESP_LOGI(TAG, "Cliente BLE desconectado (reason=%d)", event->disconnect.reason);
            // Se havia uma atualização OTA em curso, aborta com segurança
            ota_status_info_t st;
            ota_manager_get_status(&st);
            if (st.state == OTA_STATE_IN_PROGRESS) {
                ota_manager_abort("Desconexao BLE durante OTA");
            }
            s_conn_handle = BLE_HS_CONN_HANDLE_NONE;
            s_is_connected = false;
            s_stream_enabled = false;
            led_indicator_set_ble_connected(false);
            start_advertising_slot(0);
            break;

        case BLE_GAP_EVENT_ADV_COMPLETE:
            start_advertising_slot(s_adv_slot);
            break;

        case BLE_GAP_EVENT_MTU:
            ESP_LOGI(TAG, "MTU BLE atualizado para %d bytes", event->mtu.value);
            break;

        default:
            break;
    }

    return 0;
}

static void start_advertising_slot(uint8_t slot)
{
    if (!ble_hs_synced()) {
        return;
    }

    if (ble_gap_adv_active()) {
        ble_gap_adv_stop();
    }

    struct ble_gap_adv_params adv_params = {0};
    adv_params.disc_mode = BLE_GAP_DISC_MODE_GEN;

    if (!s_alert_active) {
        // Modo Normal (MONITORING): Anuncia "Ripple-Buoy" + UUID NUS no Scan Response
        adv_params.conn_mode = s_is_connected ? BLE_GAP_CONN_MODE_NON : BLE_GAP_CONN_MODE_UND;
        adv_params.itvl_min = 0x00A0; // 100 ms
        adv_params.itvl_max = 0x0140; // 200 ms

        struct ble_hs_adv_fields fields = {0};
        fields.flags = BLE_HS_ADV_F_DISC_GEN | BLE_HS_ADV_F_BREDR_UNSUP;
        const char *name = "Ripple-Buoy";
        fields.name = (uint8_t *)name;
        fields.name_len = strlen(name);
        fields.name_is_complete = 1;

        uint8_t mfg_ok[] = {0xFF, 0xFF, 'R', 'P', 'L', 0x00};
        fields.mfg_data = mfg_ok;
        fields.mfg_data_len = sizeof(mfg_ok);
        ble_gap_adv_set_fields(&fields);

        struct ble_hs_adv_fields rsp_fields = {0};
        rsp_fields.uuids128 = (ble_uuid128_t *)&kNusSvcUuid;
        rsp_fields.num_uuids128 = 1;
        rsp_fields.uuids128_is_complete = 1;
        ble_gap_adv_rsp_set_fields(&rsp_fields);

        ble_gap_adv_start(s_own_addr_type, NULL, BLE_HS_FOREVER, &adv_params, ble_gap_event_cb, NULL);
        return;
    }

    // Modo Emergência (FALL_ALERT): Ciclo rápido de pop-ups
    adv_params.itvl_min = 0x0030; // 30 ms
    adv_params.itvl_max = 0x0050; // 50 ms

    if (slot == 0) {
        adv_params.conn_mode = BLE_GAP_CONN_MODE_NON;
        ble_gap_adv_set_data(kGoogleFastPairAdv, sizeof(kGoogleFastPairAdv));
        ble_gap_adv_start(s_own_addr_type, NULL, BLE_HS_FOREVER, &adv_params, ble_gap_event_cb, NULL);
    } else if (slot == 1) {
        adv_params.conn_mode = BLE_GAP_CONN_MODE_NON;
        ble_gap_adv_set_data(kSamsungGalaxyAdv, sizeof(kSamsungGalaxyAdv));
        ble_gap_adv_start(s_own_addr_type, NULL, BLE_HS_FOREVER, &adv_params, ble_gap_event_cb, NULL);
    } else if (slot == 2) {
        adv_params.conn_mode = BLE_GAP_CONN_MODE_NON;
        ble_gap_adv_set_data(kAppleProximityAdv, sizeof(kAppleProximityAdv));
        ble_gap_adv_start(s_own_addr_type, NULL, BLE_HS_FOREVER, &adv_params, ble_gap_event_cb, NULL);
    } else {
        adv_params.conn_mode = s_is_connected ? BLE_GAP_CONN_MODE_NON : BLE_GAP_CONN_MODE_UND;

        struct ble_hs_adv_fields fields = {0};
        fields.flags = BLE_HS_ADV_F_DISC_GEN | BLE_HS_ADV_F_BREDR_UNSUP;
        const char *alert_name = "RIPPLE: QUEDA NA PISCINA!";
        fields.name = (uint8_t *)alert_name;
        fields.name_len = strlen(alert_name);
        fields.name_is_complete = 1;
        ble_gap_adv_set_fields(&fields);

        struct ble_hs_adv_fields rsp_fields = {0};
        rsp_fields.uuids128 = (ble_uuid128_t *)&kNusSvcUuid;
        rsp_fields.num_uuids128 = 1;
        rsp_fields.uuids128_is_complete = 1;
        ble_gap_adv_rsp_set_fields(&rsp_fields);

        ble_gap_adv_start(s_own_addr_type, NULL, BLE_HS_FOREVER, &adv_params, ble_gap_event_cb, NULL);
    }
}

static void ble_adv_rotator_task(void *arg)
{
    (void)arg;
    bool prev_alert = false;

    while (true) {
        bool alert = s_alert_active;
        if (alert) {
            s_adv_slot = (s_adv_slot + 1) % 4;
            start_advertising_slot(s_adv_slot);
            prev_alert = true;
            vTaskDelay(pdMS_TO_TICKS(1200));
        } else {
            if (prev_alert) {
                s_adv_slot = 0;
                start_advertising_slot(0);
                prev_alert = false;
            }
            vTaskDelay(pdMS_TO_TICKS(200));
        }
    }
}

static void ble_on_sync(void)
{
    int rc = ble_hs_id_infer_auto(0, &s_own_addr_type);
    if (rc != 0) {
        ESP_LOGE(TAG, "Erro ao inferir endereco BLE: %d", rc);
        return;
    }

    esp_ble_tx_power_set(ESP_BLE_PWR_TYPE_ADV, ESP_PWR_LVL_P9);
    esp_ble_tx_power_set(ESP_BLE_PWR_TYPE_DEFAULT, ESP_PWR_LVL_P9);

    uint8_t addr_val[6] = {0};
    ble_hs_id_copy_addr(s_own_addr_type, addr_val, NULL);
    ESP_LOGI(TAG, "NimBLE sincronizado! MAC BLE: %02X:%02X:%02X:%02X:%02X:%02X",
             addr_val[5], addr_val[4], addr_val[3], addr_val[2], addr_val[1], addr_val[0]);

    start_advertising_slot(0);
}

static void ble_host_task(void *param)
{
    (void)param;
    ESP_LOGI(TAG, "NimBLE Host Task iniciada");
    nimble_port_run();
    nimble_port_freertos_deinit();
}

esp_err_t ble_ripple_init(ripple_cmd_handler_t cmd_cb)
{
    s_cmd_cb = cmd_cb;

    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    ESP_ERROR_CHECK(nimble_port_init());

    ble_hs_cfg.sync_cb = ble_on_sync;

    ble_svc_gap_init();
    ble_svc_gatt_init();

    int rc = ble_gatts_count_cfg(s_gatt_svcs);
    if (rc != 0) {
        return ESP_FAIL;
    }
    rc = ble_gatts_add_svcs(s_gatt_svcs);
    if (rc != 0) {
        return ESP_FAIL;
    }

    ble_svc_gap_device_name_set("Ripple-Buoy");

    nimble_port_freertos_init(ble_host_task);
    xTaskCreate(ble_adv_rotator_task, "ble_adv_rot", 3072, NULL, 4, NULL);

    return ESP_OK;
}

void ble_ripple_set_alert_mode(bool alert_active)
{
    s_alert_active = alert_active;
}

esp_err_t ble_ripple_notify_text(const char *text)
{
    if (!text) {
        return ESP_ERR_INVALID_ARG;
    }

    snprintf(s_last_tx_buf, sizeof(s_last_tx_buf), "%s", text);

    if (!s_is_connected || s_conn_handle == BLE_HS_CONN_HANDLE_NONE) {
        return ESP_ERR_INVALID_STATE;
    }

    struct os_mbuf *om = ble_hs_mbuf_from_flat(text, strlen(text));
    if (!om) {
        return ESP_ERR_NO_MEM;
    }

    int rc = ble_gatts_notify_custom(s_conn_handle, s_tx_attr_handle, om);
    return (rc == 0) ? ESP_OK : ESP_FAIL;
}

esp_err_t ble_ripple_notify_ota_ctrl(const char *text)
{
    if (!text) {
        return ESP_ERR_INVALID_ARG;
    }

    snprintf(s_last_ota_ctrl_buf, sizeof(s_last_ota_ctrl_buf), "%s", text);

    if (!s_is_connected || s_conn_handle == BLE_HS_CONN_HANDLE_NONE || s_ota_ctrl_attr_handle == 0) {
        return ESP_ERR_INVALID_STATE;
    }

    struct os_mbuf *om = ble_hs_mbuf_from_flat(text, strlen(text));
    if (!om) {
        return ESP_ERR_NO_MEM;
    }

    int rc = ble_gatts_notify_custom(s_conn_handle, s_ota_ctrl_attr_handle, om);
    return (rc == 0) ? ESP_OK : ESP_FAIL;
}

bool ble_ripple_is_connected(void)
{
    return s_is_connected;
}

bool ble_ripple_is_streaming(void)
{
    return s_stream_enabled;
}

void ble_ripple_set_streaming(bool enable)
{
    s_stream_enabled = enable;
}
