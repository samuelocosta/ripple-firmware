#include "battery_monitor.h"
#include "board_pins.h"

#include <stdio.h>
#include <string.h>
#include <math.h>

#include "esp_log.h"
#include "esp_err.h"
#include "esp_sleep.h"
#include "driver/gpio.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs_flash.h"
#include "nvs.h"
#include "esp_adc/adc_oneshot.h"
#include "esp_adc/adc_cali.h"
#include "esp_adc/adc_cali_scheme.h"

static const char *TAG = "BATTERY_MON";

#define NVS_NAMESPACE_BATTERY   "ripple_bat"
#define NVS_KEY_MIN_VOLTAGE     "vbat_min"

static battery_status_t s_status = {
    .voltage_v = 0.0f,
    .percentage = 0.0f,
    .min_safe_voltage_v = BATTERY_DEFAULT_MIN_VOLTAGE,
    .is_low_battery = false,
    .is_critical = false,
    .sensor_ok = false,
    .backend = BATTERY_BACKEND_NONE,
};
static uint8_t s_critical_samples = 0;

/* ADC handles */
static adc_oneshot_unit_handle_t s_adc_handle = NULL;
static adc_cali_handle_t s_adc_cali_handle = NULL;
static bool s_adc_calibrated = false;

static esp_err_t load_nvs_min_voltage(void)
{
    nvs_handle_t nvs_h;
    esp_err_t err = nvs_open(NVS_NAMESPACE_BATTERY, NVS_READONLY, &nvs_h);
    if (err == ESP_OK) {
        uint32_t raw_val = 0;
        err = nvs_get_u32(nvs_h, NVS_KEY_MIN_VOLTAGE, &raw_val);
        if (err == ESP_OK) {
            float val = 0.0f;
            memcpy(&val, &raw_val, sizeof(float));
            if (val >= 2.80f && val <= 3.90f) {
                s_status.min_safe_voltage_v = val;
                ESP_LOGI(TAG, "Loaded safe min voltage from NVS: %.2fV", val);
            }
        }
        nvs_close(nvs_h);
    }
    return ESP_OK;
}

static esp_err_t save_nvs_min_voltage(float val)
{
    nvs_handle_t nvs_h;
    esp_err_t err = nvs_open(NVS_NAMESPACE_BATTERY, NVS_READWRITE, &nvs_h);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Failed to open NVS for writing: %s", esp_err_to_name(err));
        return err;
    }

    uint32_t raw_val = 0;
    memcpy(&raw_val, &val, sizeof(float));
    err = nvs_set_u32(nvs_h, NVS_KEY_MIN_VOLTAGE, raw_val);
    if (err == ESP_OK) {
        err = nvs_commit(nvs_h);
    }
    nvs_close(nvs_h);
    return err;
}

static bool init_adc(void)
{
    adc_oneshot_unit_init_cfg_t init_config = {
        .unit_id = ADC_UNIT_1,
        .clk_src = 0,
        .ulp_mode = ADC_ULP_MODE_DISABLE,
    };

    if (adc_oneshot_new_unit(&init_config, &s_adc_handle) != ESP_OK) {
        ESP_LOGE(TAG, "Failed to initialize ADC1 unit");
        return false;
    }

    adc_oneshot_chan_cfg_t config = {
        .bitwidth = ADC_BITWIDTH_DEFAULT,
        .atten = ADC_ATTEN_DB_12, // Measure up to ~3.1V safely
    };

    if (adc_oneshot_config_channel(s_adc_handle, BOARD_BATTERY_ADC_CHANNEL, &config) != ESP_OK) {
        ESP_LOGE(TAG, "Failed to configure ADC channel %d", BOARD_BATTERY_ADC_CHANNEL);
        return false;
    }

    // Attempt eFuse calibration
    adc_cali_curve_fitting_config_t cali_config = {
        .unit_id = ADC_UNIT_1,
        .chan = BOARD_BATTERY_ADC_CHANNEL,
        .atten = ADC_ATTEN_DB_12,
        .bitwidth = ADC_BITWIDTH_DEFAULT,
    };

    if (adc_cali_create_scheme_curve_fitting(&cali_config, &s_adc_cali_handle) == ESP_OK) {
        s_adc_calibrated = true;
        ESP_LOGI(TAG, "ADC calibration (curve fitting) enabled for battery channel");
    } else {
        s_adc_calibrated = false;
        ESP_LOGW(TAG, "ADC calibration not available; raw conversion will be used");
    }

    ESP_LOGI(TAG, "ADC1 initialized on Channel %d (Divider Ratio: %.1f)",
             BOARD_BATTERY_ADC_CHANNEL, BOARD_BATTERY_DIVIDER_RATIO);
    return true;
}

static float calculate_percentage(float voltage, float min_v)
{
    if (voltage >= BATTERY_THEORETICAL_MAX_VOLT) {
        return 100.0f;
    }
    if (voltage <= min_v) {
        return 0.0f;
    }
    float pct = ((voltage - min_v) / (BATTERY_THEORETICAL_MAX_VOLT - min_v)) * 100.0f;
    if (pct < 0.0f) pct = 0.0f;
    if (pct > 100.0f) pct = 100.0f;
    return pct;
}

esp_err_t battery_monitor_init(void)
{
    load_nvs_min_voltage();

    if (init_adc()) {
        s_status.backend = BATTERY_BACKEND_ADC;
        s_status.sensor_ok = true;
    } else {
        s_status.backend = BATTERY_BACKEND_NONE;
        s_status.sensor_ok = false;
        ESP_LOGE(TAG, "Failed to initialize ADC battery monitor!");
        return ESP_FAIL;
    }

    // Initial read
    battery_monitor_read(NULL);
    return ESP_OK;
}

esp_err_t battery_monitor_read(battery_status_t *out_status)
{
    float voltage = 0.0f;
    bool success = false;

    if (s_status.backend == BATTERY_BACKEND_ADC && s_adc_handle != NULL) {
        int raw_adc = 0;
        esp_err_t err = adc_oneshot_read(s_adc_handle, BOARD_BATTERY_ADC_CHANNEL, &raw_adc);
        if (err == ESP_OK) {
            int pin_mv = 0;
            if (s_adc_calibrated && s_adc_cali_handle) {
                adc_cali_raw_to_voltage(s_adc_cali_handle, raw_adc, &pin_mv);
            } else {
                pin_mv = (raw_adc * 3100) / 4095;
            }
            voltage = ((float)pin_mv / 1000.0f) * BOARD_BATTERY_DIVIDER_RATIO;
            success = true;
        } else {
            ESP_LOGW(TAG, "Failed to read ADC channel: %s", esp_err_to_name(err));
        }
    }

    if (success) {
        // Simple IIR filter for stable readings
        if (s_status.voltage_v <= 0.1f) {
            s_status.voltage_v = voltage;
        } else {
            s_status.voltage_v = (s_status.voltage_v * 0.8f) + (voltage * 0.2f);
        }

        s_status.percentage = calculate_percentage(s_status.voltage_v, s_status.min_safe_voltage_v);

        // Low-battery evaluation with hysteresis
        if (!s_status.is_low_battery && (s_status.percentage <= BATTERY_LOW_THRESHOLD_PCT)) {
            s_status.is_low_battery = true;
            ESP_LOGW(TAG, "Low battery condition detected! (%.2fV, %.1f%%)",
                     s_status.voltage_v, s_status.percentage);
        } else if (s_status.is_low_battery &&
                   (s_status.percentage > (BATTERY_LOW_THRESHOLD_PCT + BATTERY_LOW_HYSTERESIS_PCT))) {
            s_status.is_low_battery = false;
            ESP_LOGI(TAG, "Low battery condition cleared. (%.2fV, %.1f%%)",
                     s_status.voltage_v, s_status.percentage);
        }

        // Critical-battery evaluation (voltage <= min_safe_voltage, or 0%) with debounce
        if (s_status.voltage_v > 0.5f && (s_status.voltage_v <= s_status.min_safe_voltage_v)) {
            if (s_critical_samples < BATTERY_CRITICAL_CONSEC_SAMPLES) {
                s_critical_samples++;
            }
            if (s_critical_samples >= BATTERY_CRITICAL_CONSEC_SAMPLES) {
                s_status.is_critical = true;
            }
        } else {
            s_critical_samples = 0;
            s_status.is_critical = false;
        }

        s_status.sensor_ok = true;
    } else {
        s_status.sensor_ok = false;
    }

    if (out_status) {
        *out_status = s_status;
    }

    return success ? ESP_OK : ESP_FAIL;
}

void battery_monitor_get_status(battery_status_t *out_status)
{
    if (out_status) {
        *out_status = s_status;
    }
}

bool battery_monitor_is_critical(const battery_status_t *status)
{
    if (status) {
        return status->is_critical;
    }
    return s_status.is_critical;
}

void battery_monitor_enter_deep_sleep(void)
{
    ESP_LOGE(TAG, "=========================================================");
    ESP_LOGE(TAG, "  CRITICAL BATTERY REACHED: %.3fV <= Safe Min %.2fV (0%%)  ",
             s_status.voltage_v, s_status.min_safe_voltage_v);
    ESP_LOGE(TAG, "  ENTERING DEEP SLEEP TO PROTECT BATTERY FROM DESTRUCTION   ");
    ESP_LOGE(TAG, "=========================================================");

    // 1. Turn off Red LED
    gpio_set_direction((gpio_num_t)BOARD_LED_RED_GPIO, GPIO_MODE_OUTPUT);
    gpio_set_level((gpio_num_t)BOARD_LED_RED_GPIO, 0);

    // 2. Cut power to NeoPixel (GPIO 34 = LOW)
    gpio_set_direction((gpio_num_t)BOARD_NEOPIXEL_POWER_GPIO, GPIO_MODE_OUTPUT);
    gpio_set_level((gpio_num_t)BOARD_NEOPIXEL_POWER_GPIO, 0);

    // 3. Turn off TFT backlight (GPIO 45 = LOW)
    gpio_set_direction((gpio_num_t)BOARD_TFT_BACKLIGHT_GPIO, GPIO_MODE_OUTPUT);
    gpio_set_level((gpio_num_t)BOARD_TFT_BACKLIGHT_GPIO, 0);

    // 4. Cut power to I2C sensors and TFT display (GPIO 21 = LOW)
    gpio_set_direction((gpio_num_t)BOARD_TFT_I2C_POWER_GPIO, GPIO_MODE_OUTPUT);
    gpio_set_level((gpio_num_t)BOARD_TFT_I2C_POWER_GPIO, 0);

    // 5. Configure wakeup sources:
    //    a) BOOT button (GPIO 0, active LOW on Feather)
    esp_sleep_enable_ext1_wakeup_io(1ULL << BOARD_BOOT_BUTTON_GPIO, ESP_EXT1_WAKEUP_ANY_LOW);

    //    b) Periodic RTC timer wakeup (1 hour) to check if battery was recharged
    esp_sleep_enable_timer_wakeup(BATTERY_DEEP_SLEEP_WAKEUP_SEC * 1000000ULL);

    ESP_LOGI(TAG, "Wakeup armed: BOOT Button (GPIO %d) or RTC Timer (%llu sec). Good night!",
             BOARD_BOOT_BUTTON_GPIO, (unsigned long long)BATTERY_DEEP_SLEEP_WAKEUP_SEC);

    fflush(stdout);
    vTaskDelay(pdMS_TO_TICKS(100));

    // 6. Enter Deep Sleep
    esp_deep_sleep_start();
}

esp_err_t battery_monitor_set_min_voltage(float min_voltage_v)
{
    if (min_voltage_v < 2.80f || min_voltage_v > 3.90f) {
        return ESP_ERR_INVALID_ARG;
    }

    s_status.min_safe_voltage_v = min_voltage_v;
    s_status.percentage = calculate_percentage(s_status.voltage_v, s_status.min_safe_voltage_v);

    if (s_status.percentage <= BATTERY_LOW_THRESHOLD_PCT) {
        s_status.is_low_battery = true;
    } else if (s_status.percentage > (BATTERY_LOW_THRESHOLD_PCT + BATTERY_LOW_HYSTERESIS_PCT)) {
        s_status.is_low_battery = false;
    }

    s_critical_samples = 0;
    s_status.is_critical = (s_status.voltage_v > 0.5f && (s_status.voltage_v <= s_status.min_safe_voltage_v));

    ESP_LOGI(TAG, "Safe minimum voltage updated to %.2fV (Recalculated: %.1f%%)",
             min_voltage_v, s_status.percentage);

    return save_nvs_min_voltage(min_voltage_v);
}

float battery_monitor_get_min_voltage(void)
{
    return s_status.min_safe_voltage_v;
}
