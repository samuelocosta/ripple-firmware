#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "esp_log.h"
#include "esp_timer.h"
#include "esp_sleep.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "board_pins.h"
#include "led_indicator.h"
#include "battery_monitor.h"
#include "imu_sensor.h"
#include "wave_detector.h"
#include "ble_ripple.h"
#include "ota_manager.h"
#include "ble_logger.h"

static const char *TAG = "RIPPLE_MAIN";

static uint32_t s_stream_interval_loops = 10; // Padrão: 10 loops de 20ms = 200ms = 5 Hz (1 a 50 loops)

static uint32_t now_ms(void)
{
    return (uint32_t)(esp_timer_get_time() / 1000ULL);
}

static void send_status_report(const char *prefix_event)
{
    ripple_detector_status_t st = {0};
    wave_detector_get_status(&st);

    battery_status_t bat = {0};
    battery_monitor_get_status(&bat);

    char msg[280];
    if (prefix_event) {
        snprintf(msg, sizeof(msg),
                 "{\"event\":\"%s\",\"state\":\"%s\",\"wave\":%.3f,\"peak\":%.3f,\"thr\":%.2f,\"alerts\":%lu,\"temp\":%.1f,\"hum\":%.1f,\"hum_ok\":%s,"
                 "\"vbat\":%.3f,\"bat_pct\":%.1f,\"bat_low\":%s,\"bat_crit\":%s}\n",
                 prefix_event,
                 wave_detector_state_str(st.state),
                 st.wave_energy,
                 st.peak_wave_energy,
                 st.threshold,
                 (unsigned long)st.alert_count,
                 st.temperature_c,
                 st.humidity_pct,
                 st.humidity_ok ? "true" : "false",
                 bat.voltage_v,
                 bat.percentage,
                 bat.is_low_battery ? "true" : "false",
                 bat.is_critical ? "true" : "false");
    } else {
        snprintf(msg, sizeof(msg),
                 "{\"state\":\"%s\",\"wave\":%.3f,\"peak\":%.3f,\"thr\":%.2f,\"da\":%.3f,\"gyro\":%.1f,\"alerts\":%lu,\"temp\":%.1f,\"hum\":%.1f,\"hum_ok\":%s,"
                 "\"vbat\":%.3f,\"bat_pct\":%.1f,\"bat_low\":%s,\"bat_crit\":%s}\n",
                 wave_detector_state_str(st.state),
                 st.wave_energy,
                 st.peak_wave_energy,
                 st.threshold,
                 st.delta_accel_g,
                 st.gyro_tilt_dps,
                 (unsigned long)st.alert_count,
                 st.temperature_c,
                 st.humidity_pct,
                 st.humidity_ok ? "true" : "false",
                 bat.voltage_v,
                 bat.percentage,
                 bat.is_low_battery ? "true" : "false",
                 bat.is_critical ? "true" : "false");
    }

    printf("%s", msg);
    ble_ripple_notify_text(msg);
}

static void sync_outputs_with_state(ripple_state_t state)
{
    switch (state) {
        case RIPPLE_STATE_CALIBRATING:
            led_indicator_set_mode(LED_MODE_CALIBRATING);
            ble_ripple_set_alert_mode(false);
            break;
        case RIPPLE_STATE_MONITORING:
            led_indicator_set_mode(LED_MODE_MONITORING);
            ble_ripple_set_alert_mode(false);
            break;
        case RIPPLE_STATE_FALL_ALERT:
            led_indicator_set_mode(LED_MODE_FALL_ALERT);
            ble_ripple_set_alert_mode(true);
            break;
    }
}

static void handle_remote_command(const char *cmd)
{
    if (!cmd || cmd[0] == '\0') {
        return;
    }

    const uint32_t t_ms = now_ms();

    if (strcasecmp(cmd, "status") == 0) {
        send_status_report(NULL);
    } else if (strncasecmp(cmd, "sens ", 5) == 0) {
        float val = strtof(cmd + 5, NULL);
        if (val > 0.0f) {
            wave_detector_set_threshold(val);
            send_status_report("THRESHOLD_UPDATED");
        } else {
            ble_ripple_notify_text("{\"error\":\"invalid_sens_value\"}\n");
        }
    } else if (strcasecmp(cmd, "test_alert") == 0 || strcasecmp(cmd, "alert") == 0) {
        wave_detector_trigger_test_alert(t_ms);
        sync_outputs_with_state(RIPPLE_STATE_FALL_ALERT);
        send_status_report("FALL_ALERT_TEST");
    } else if (strcasecmp(cmd, "rearm") == 0 || strcasecmp(cmd, "reset") == 0) {
        wave_detector_rearm(t_ms);
        sync_outputs_with_state(RIPPLE_STATE_MONITORING);
        send_status_report("REARMED");
    } else if (strcasecmp(cmd, "calibrate") == 0 || strcasecmp(cmd, "cal") == 0) {
        wave_detector_start_calibration(t_ms);
        sync_outputs_with_state(RIPPLE_STATE_CALIBRATING);
        send_status_report("CALIBRATING_STARTED");
    } else if (strncasecmp(cmd, "stream", 6) == 0) {
        const char *arg = cmd + 6;
        while (*arg == ' ') arg++;
        if (strcasecmp(arg, "off") == 0) {
            ble_ripple_set_streaming(false);
            ble_ripple_notify_text("{\"stream\":false}\n");
        } else {
            uint32_t target_hz = 0;
            if (strncasecmp(arg, "on", 2) == 0) {
                arg += 2;
                while (*arg == ' ') arg++;
            }
            if (*arg != '\0') {
                target_hz = (uint32_t)strtoul(arg, NULL, 10);
            }
            if (target_hz == 0) {
                if (s_stream_interval_loops == 0) {
                    s_stream_interval_loops = 10;
                }
            } else {
                if (target_hz < 1) target_hz = 1;
                if (target_hz > 50) target_hz = 50; // Limite físico máximo: loop IMU de 50 Hz
                s_stream_interval_loops = 50 / target_hz;
                if (s_stream_interval_loops < 1) s_stream_interval_loops = 1;
            }
            ble_ripple_set_streaming(true);
            uint32_t eff_hz = 50 / s_stream_interval_loops;
            char resp[80];
            snprintf(resp, sizeof(resp), "{\"stream\":true,\"hz\":%lu,\"interval_ms\":%lu}\n",
                     (unsigned long)eff_hz, (unsigned long)(s_stream_interval_loops * 20));
            ble_ripple_notify_text(resp);
        }
    } else if (strcasecmp(cmd, "log on") == 0) {
        ble_logger_set_enabled(true);
        ble_ripple_notify_text("{\"log_stream\":true}\n");
    } else if (strcasecmp(cmd, "log off") == 0) {
        ble_logger_set_enabled(false);
        ble_ripple_notify_text("{\"log_stream\":false}\n");
    } else if (strcasecmp(cmd, "ota info") == 0 || strcasecmp(cmd, "ota_info") == 0) {
        char info_buf[256];
        ota_manager_get_info_json(info_buf, sizeof(info_buf));
        ble_ripple_notify_text(info_buf);
    } else if (strcasecmp(cmd, "reboot") == 0) {
        ble_ripple_notify_text("{\"event\":\"rebooting_in_500ms\"}\n");
        ota_manager_reboot_delayed(500);
    } else if (strcasecmp(cmd, "rollback") == 0) {
        ble_ripple_notify_text("{\"event\":\"initiating_rollback\"}\n");
        ota_manager_rollback();
    } else if (strcasecmp(cmd, "battery") == 0 || strcasecmp(cmd, "bat") == 0) {
        battery_status_t bat = {0};
        battery_monitor_get_status(&bat);
        char buf[176];
        snprintf(buf, sizeof(buf),
                 "{\"vbat\":%.3f,\"pct\":%.1f,\"min_v\":%.2f,\"low\":%s,\"crit\":%s,\"sensor_ok\":%s}\n",
                 bat.voltage_v, bat.percentage, bat.min_safe_voltage_v,
                 bat.is_low_battery ? "true" : "false",
                 bat.is_critical ? "true" : "false",
                 bat.sensor_ok ? "true" : "false");
        printf("%s", buf);
        ble_ripple_notify_text(buf);
    } else if (strcasecmp(cmd, "sleep") == 0) {
        ESP_LOGI(TAG, "Remote 'sleep' command received. Entering deep sleep...");
        ble_ripple_notify_text("{\"event\":\"entering_deep_sleep\"}\n");
        vTaskDelay(pdMS_TO_TICKS(150));
        battery_monitor_enter_deep_sleep();
    } else if (strncasecmp(cmd, "bat_min ", 8) == 0 || strncasecmp(cmd, "set_bat_min ", 12) == 0) {
        const char *p = (cmd[0] == 's' || cmd[0] == 'S') ? (cmd + 12) : (cmd + 8);
        float v = strtof(p, NULL);
        if (v >= 2.80f && v <= 3.90f) {
            battery_monitor_set_min_voltage(v);
            battery_status_t bat = {0};
            battery_monitor_get_status(&bat);
            char buf[128];
            snprintf(buf, sizeof(buf), "{\"status\":\"ok\",\"min_v\":%.2f,\"pct\":%.1f}\n",
                     bat.min_safe_voltage_v, bat.percentage);
            printf("%s", buf);
            ble_ripple_notify_text(buf);
        } else {
            ble_ripple_notify_text("{\"error\":\"invalid_min_voltage\",\"hint\":\"range 2.80 to 3.90V\"}\n");
        }
    } else if (strcasecmp(cmd, "help") == 0) {
        ble_ripple_notify_text("{\"cmds\":[\"status\",\"battery\",\"bat_min <val>\",\"sleep\",\"sens <val>\",\"test_alert\",\"rearm\",\"calibrate\",\"stream on/off\",\"log on/off\",\"ota info\",\"reboot\",\"rollback\"]}\n");
    } else {
        ble_ripple_notify_text("{\"error\":\"unknown_cmd\",\"hint\":\"send help\"}\n");
    }
}

void app_main(void)
{
    ESP_LOGI(TAG, "================================================");
    ESP_LOGI(TAG, "  RIPPLE - Smart Pool Fall Detection Buoy v1.1  ");
    ESP_LOGI(TAG, "  Board: TENSTAR TS-ESP32-S3 (ESP32S3-1.14TFT)  ");
    ESP_LOGI(TAG, "  Wireless Console & Secure BLE OTA Enabled     ");
    ESP_LOGI(TAG, "================================================");

    // 1. Inicializa subsistema de OTA e valida rollback seguro
    ESP_ERROR_CHECK(ota_manager_init());

    // Check wakeup cause (Deep Sleep or normal power-on)
    uint32_t wakeup_causes = esp_sleep_get_wakeup_causes();
    if (wakeup_causes != 0) {
        ESP_LOGW(TAG, "Buoy woke up from DEEP SLEEP! Wakeup bitmap: 0x%08" PRIx32, wakeup_causes);
    }

    // 2. Inicializa o interceptor de logs sem fio via BLE
    ESP_ERROR_CHECK(ble_logger_init());

    // 3. Inicializa LEDs (WS2812 GPIO33 + Power GPIO34 + Red LED GPIO13)
    ESP_ERROR_CHECK(led_indicator_init());
    led_indicator_set_mode(LED_MODE_CALIBRATING);

    // 4. Inicializa barramento I2C (Power GPIO21 + SDA=42 / SCL=41) e sensores QMI8658C + BMP280
    esp_err_t imu_err = imu_sensor_init();
    if (imu_err != ESP_OK) {
        ESP_LOGW(TAG, "Aviso: QMI8658C nao respondeu no boot (%s). Modo simulacao/BLE segue ativo.",
                 esp_err_to_name(imu_err));
    }

    // 5. Initialize battery monitoring subsystem (ADC1 voltage divider)
    esp_err_t bat_err = battery_monitor_init();
    if (bat_err != ESP_OK) {
        ESP_LOGW(TAG, "Battery monitor initialized with warning: %s", esp_err_to_name(bat_err));
    }

    // 6. Inicializa algoritmo detector de quedas/oscilação (limiar inicial = 0.22)
    wave_detector_init(0.22f);

    // 7. Inicializa Bluetooth Dual-Mode (Pop-up FastPair/Apple + GATT NUS + OTA Service)
    ESP_ERROR_CHECK(ble_ripple_init(handle_remote_command));

    // 7. Loop principal de amostragem a 50 Hz (20 ms)
    uint32_t loop_count = 0;
    ripple_state_t last_state = RIPPLE_STATE_CALIBRATING;

    while (true) {
        const uint32_t t_ms = now_ms();
        const bool read_slow_sensors = (loop_count % 50) == 0; // Lê temperatura a 1 Hz

        ripple_sensor_sample_t sample = {0};
        imu_sensor_read(&sample, read_slow_sensors);

        // Amostragem de tensão de bateria a 50 Hz com filtro IIR 80/20 contra ruído
        battery_status_t bat_sample = {0};
        battery_monitor_read(&bat_sample);
        led_indicator_set_low_battery(bat_sample.is_low_battery);

        // Deep Sleep protection: enter deep sleep on critical voltage (<= min_safe_v / 0%)
        if (battery_monitor_is_critical(&bat_sample)) {
            ESP_LOGE(TAG, "CRITICAL BATTERY LEVEL (%.3fV <= %.2fV). Entering Deep Sleep...",
                     bat_sample.voltage_v, bat_sample.min_safe_voltage_v);
            send_status_report("CRITICAL_BATTERY_SHUTDOWN");
            ble_ripple_notify_text("{\"event\":\"CRITICAL_BATTERY_SHUTDOWN\",\"msg\":\"Entering deep sleep\"}\n");
            vTaskDelay(pdMS_TO_TICKS(200));
            battery_monitor_enter_deep_sleep();
        }

        // Caso o IMU esteja indisponível, simula 1.0g para permitir concluir a calibração e testar via BLE
        if (!sample.imu_ok) {
            sample.imu_ok = true;
            sample.ax = 0.0f;
            sample.ay = 0.0f;
            sample.az = 1.0f;
        }

        bool triggered = wave_detector_update(&sample, t_ms);

        ripple_detector_status_t st = {0};
        wave_detector_get_status(&st);

        // Atualiza LEDs e modo de anúncio BLE quando o estado muda
        if (st.state != last_state) {
            sync_outputs_with_state(st.state);
            last_state = st.state;

            if (triggered || st.state == RIPPLE_STATE_FALL_ALERT) {
                send_status_report("FALL_ALERT");
            } else if (st.state == RIPPLE_STATE_MONITORING) {
                send_status_report("STATE_MONITORING");
            }
        }

        // Se o stream BLE estiver ativo, envia telemetria na taxa configurada (1 Hz a 50 Hz)
        if (ble_ripple_is_connected() && ble_ripple_is_streaming() && (loop_count % s_stream_interval_loops == 0)) {
            send_status_report(NULL);
        }

        // Log no console serial USB / BLE a cada 2 segundos (100 loops)
        if (loop_count % 100 == 0) {
            battery_status_t bat_log = {0};
            battery_monitor_get_status(&bat_log);
            ESP_LOGI(TAG, "[%s] Wave=%.3f (Peak=%.3f / Thr=%.2f) | Acc=(%.2f,%.2f,%.2f)g | Temp=%.1fC | Bat=%.2fV (%.0f%%) | Alerts=%lu",
                     wave_detector_state_str(st.state),
                     st.wave_energy, st.peak_wave_energy, st.threshold,
                     sample.ax, sample.ay, sample.az,
                     st.temperature_c, bat_log.voltage_v, bat_log.percentage,
                     (unsigned long)st.alert_count);
        }

        loop_count++;
        vTaskDelay(pdMS_TO_TICKS(20));
    }
}
