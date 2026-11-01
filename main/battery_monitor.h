#pragma once

#include <stdbool.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

#define BATTERY_DEFAULT_MIN_VOLTAGE   3.20f   // Default safe minimum cutoff (0%)
#define BATTERY_THEORETICAL_MAX_VOLT  4.20f   // Theoretical maximum charge (100%)
#define BATTERY_LOW_THRESHOLD_PCT     15.0f   // Low battery trigger threshold (%)
#define BATTERY_LOW_HYSTERESIS_PCT    2.0f    // Clear low battery threshold (17%)
#define BATTERY_CRITICAL_CONSEC_SAMPLES 3     // Consecutive readings required for shutdown
#define BATTERY_DEEP_SLEEP_WAKEUP_SEC 3600ULL // Periodic wakeup interval in deep sleep (1 hour)

typedef enum {
    BATTERY_BACKEND_NONE = 0,
    BATTERY_BACKEND_ADC,        // ESP32-S3 ADC channel with voltage divider
} battery_backend_t;

typedef struct {
    float voltage_v;            // Current measured cell voltage in Volts (e.g. 3.842V)
    float percentage;           // Calculated percentage 0.0% .. 100.0%
    float min_safe_voltage_v;   // Configured safe minimum voltage (0% reference)
    bool  is_low_battery;       // True if percentage <= 15.0%
    bool  is_critical;          // True if percentage <= 0.0% (voltage <= min_safe_voltage_v)
    bool  sensor_ok;            // True if sensor is communicating
    battery_backend_t backend;  // Active sensing hardware backend
} battery_status_t;

/**
 * @brief Initializes battery monitoring subsystem via ESP32-S3 ADC1.
 *        Uses calibrated curve fitting on the onboard resistor divider.
 *        Loads configured minimum safe voltage from NVS.
 */
esp_err_t battery_monitor_init(void);

/**
 * @brief Reads current battery telemetry, updates percentage and low-battery flag.
 */
esp_err_t battery_monitor_read(battery_status_t *out_status);

/**
 * @brief Retrieves the latest cached battery status.
 */
void battery_monitor_get_status(battery_status_t *out_status);

/**
 * @brief Sets a new safe minimum voltage and persists it into NVS.
 * @param min_voltage_v Minimum voltage between 2.80V and 3.90V.
 */
esp_err_t battery_monitor_set_min_voltage(float min_voltage_v);

/**
 * @brief Returns the currently configured safe minimum voltage.
 */
float battery_monitor_get_min_voltage(void);

/**
 * @brief Evaluates whether the battery has reached the critical cutoff level.
 * @param status Battery status structure to evaluate (or NULL to check cached status).
 * @return true if confirmed critical level (<= safe minimum voltage / 0%).
 */
bool battery_monitor_is_critical(const battery_status_t *status);

/**
 * @brief Enters ultra-low power deep sleep to prevent irreversible LiPo damage.
 *        Disables power rails (I2C, NeoPixel, TFT backlight, LEDs), detaches BLE,
 *        and arms wakeup via RTC GPIO0 (BOOT button) and periodic 1-hour RTC timer.
 */
void battery_monitor_enter_deep_sleep(void);

#ifdef __cplusplus
}
#endif
