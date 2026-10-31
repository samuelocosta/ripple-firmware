#pragma once

#include <stdbool.h>
#include <stdint.h>
#include "imu_sensor.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    RIPPLE_STATE_CALIBRATING = 0,
    RIPPLE_STATE_MONITORING,
    RIPPLE_STATE_FALL_ALERT,
} ripple_state_t;

typedef struct {
    ripple_state_t state;
    float accel_mag_g;       // |a| atual (g)
    float accel_baseline_g;  // Linha de base da gravidade filtrada (g)
    float delta_accel_g;     // | |a| - baseline | (g)
    float gyro_tilt_dps;     // sqrt(gx^2 + gy^2) (deg/s)
    float wave_energy;       // Índice combinado de energia da onda
    float peak_wave_energy;  // Pico recente de energia da onda
    float threshold;         // Limiar atual de disparo (ajustável via BLE)
    uint32_t alert_count;    // Total de alertas disparados desde o boot
    float temperature_c;
} ripple_detector_status_t;

/**
 * @brief Inicializa o detector de ondas/quedas com limiar padrão.
 */
void wave_detector_init(float default_threshold);

/**
 * @brief Processa uma nova amostra do IMU (esperado a ~50 Hz, dt_ms = 20 ms).
 * @return true se acabou de ocorrer uma transição para RIPPLE_STATE_FALL_ALERT.
 */
bool wave_detector_update(const ripple_sensor_sample_t *sample, uint32_t now_ms);

/**
 * @brief Obtém o status e métricas atuais do detector.
 */
void wave_detector_get_status(ripple_detector_status_t *out);

/**
 * @brief Altera o limiar (threshold) de sensibilidade em tempo real.
 */
void wave_detector_set_threshold(float new_threshold);

/**
 * @brief Força o estado de alerta (ex: comando "test_alert" via Bluetooth).
 */
void wave_detector_trigger_test_alert(uint32_t now_ms);

/**
 * @brief Rearma imediatamente para o estado MONITORING (ex: comando "rearm").
 */
void wave_detector_rearm(uint32_t now_ms);

/**
 * @brief Reinicia a calibração de 3 segundos (ex: comando "calibrate").
 */
void wave_detector_start_calibration(uint32_t now_ms);

/**
 * @brief Retorna string legível do estado atual.
 */
const char *wave_detector_state_str(ripple_state_t state);

#ifdef __cplusplus
}
#endif
