#include "wave_detector.h"

#include <math.h>
#include <string.h>
#include "esp_log.h"

static const char *TAG = "WAVE_DET";

#define CALIBRATION_DURATION_MS     3000U   // 3s de calibração inicial na água
#define MIN_ALERT_DURATION_MS       15000U  // 15s mínimos de disparo BLE Pop-up
#define CALM_WATER_REARM_MS         3000U   // 3s de água calma para auto-rearme
#define WINDOW_SIZE_SAMPLES         25      // Janela de 0.5s @ 50Hz
#define HITS_REQUIRED_FOR_ALERT     3       // Mínimo de amostras acima do limiar na janela

static ripple_detector_status_t s_status = {0};
static uint32_t s_state_enter_ms = 0;
static uint32_t s_calm_since_ms = 0;
static float s_calib_sum_g = 0.0f;
static uint32_t s_calib_samples = 0;

static uint8_t s_hit_ring[WINDOW_SIZE_SAMPLES] = {0};
static size_t s_hit_idx = 0;
static uint8_t s_hit_count = 0;

static void clear_hit_window(void)
{
    memset(s_hit_ring, 0, sizeof(s_hit_ring));
    s_hit_idx = 0;
    s_hit_count = 0;
}

void wave_detector_init(float default_threshold)
{
    memset(&s_status, 0, sizeof(s_status));
    s_status.state = RIPPLE_STATE_CALIBRATING;
    s_status.accel_baseline_g = 1.0f;
    s_status.threshold = (default_threshold > 0.02f) ? default_threshold : 0.22f;
    s_state_enter_ms = 0;
    s_calm_since_ms = 0;
    s_calib_sum_g = 0.0f;
    s_calib_samples = 0;
    clear_hit_window();
}

const char *wave_detector_state_str(ripple_state_t state)
{
    switch (state) {
        case RIPPLE_STATE_CALIBRATING: return "CALIBRATING";
        case RIPPLE_STATE_MONITORING:  return "MONITORING";
        case RIPPLE_STATE_FALL_ALERT:  return "FALL_ALERT";
        default:                       return "UNKNOWN";
    }
}

bool wave_detector_update(const ripple_sensor_sample_t *sample, uint32_t now_ms)
{
    if (!sample) {
        return false;
    }

    if (s_state_enter_ms == 0) {
        s_state_enter_ms = now_ms;
    }

    s_status.temperature_c = sample->temperature_c;
    s_status.humidity_pct = sample->humidity_pct;
    s_status.humidity_ok = sample->humidity_ok;

    if (!sample->imu_ok) {
        return false;
    }

    // 1. Calcula módulo da aceleração |a| e inclinação angular do giroscópio
    const float ax = sample->ax;
    const float ay = sample->ay;
    const float az = sample->az;
    const float acc_mag = sqrtf(ax * ax + ay * ay + az * az);

    const float gx = sample->gx;
    const float gy = sample->gy;
    const float gyro_tilt = sqrtf(gx * gx + gy * gy);

    s_status.accel_mag_g = acc_mag;
    s_status.gyro_tilt_dps = gyro_tilt;

    // 2. Estado CALIBRATING: mede o repouso inicial da boia por 3 segundos
    if (s_status.state == RIPPLE_STATE_CALIBRATING) {
        s_calib_sum_g += acc_mag;
        s_calib_samples++;

        if ((now_ms - s_state_enter_ms) >= CALIBRATION_DURATION_MS && s_calib_samples > 0) {
            s_status.accel_baseline_g = s_calib_sum_g / (float)s_calib_samples;
            s_status.state = RIPPLE_STATE_MONITORING;
            s_state_enter_ms = now_ms;
            s_status.peak_wave_energy = 0.0f;
            clear_hit_window();
            ESP_LOGI(TAG, "Calibracao concluida! Baseline=%.3fg | Threshold=%.2f | Estado -> MONITORING",
                     s_status.accel_baseline_g, s_status.threshold);
        }
        return false;
    }

    // 3. Atualiza baseline lenta (média móvel exponencial) e calcula energia da onda
    s_status.accel_baseline_g = (0.98f * s_status.accel_baseline_g) + (0.02f * acc_mag);
    const float delta_a = fabsf(acc_mag - s_status.accel_baseline_g);
    const float wave_energy = delta_a + (gyro_tilt / 140.0f);

    s_status.delta_accel_g = delta_a;
    s_status.wave_energy = wave_energy;
    if (wave_energy > s_status.peak_wave_energy) {
        s_status.peak_wave_energy = wave_energy;
    } else {
        // Decaimento suave do pico para visualização na telemetria
        s_status.peak_wave_energy *= 0.995f;
    }

    // 4. Atualiza janela circular de cruzamentos de limiar
    uint8_t is_hit = (wave_energy >= s_status.threshold) ? 1 : 0;
    s_hit_count -= s_hit_ring[s_hit_idx];
    s_hit_ring[s_hit_idx] = is_hit;
    s_hit_count += is_hit;
    s_hit_idx = (s_hit_idx + 1) % WINDOW_SIZE_SAMPLES;

    // 5. Máquina de estados
    if (s_status.state == RIPPLE_STATE_MONITORING) {
        if (s_hit_count >= HITS_REQUIRED_FOR_ALERT) {
            s_status.state = RIPPLE_STATE_FALL_ALERT;
            s_status.alert_count++;
            s_state_enter_ms = now_ms;
            s_calm_since_ms = 0;
            ESP_LOGW(TAG, "🚨 QUEDA / ONDA DETECTADA! Energia=%.3f (Limiar=%.2f) | Alertas=%lu",
                     wave_energy, s_status.threshold, (unsigned long)s_status.alert_count);
            return true;
        }
    } else if (s_status.state == RIPPLE_STATE_FALL_ALERT) {
        // Auto-rearme: espera no mínimo 15s de alerta E 3s seguidos de água calma
        if ((now_ms - s_state_enter_ms) >= MIN_ALERT_DURATION_MS) {
            const float calm_limit = s_status.threshold * 0.65f;
            if (wave_energy < calm_limit) {
                if (s_calm_since_ms == 0) {
                    s_calm_since_ms = now_ms;
                } else if ((now_ms - s_calm_since_ms) >= CALM_WATER_REARM_MS) {
                    s_status.state = RIPPLE_STATE_MONITORING;
                    s_state_enter_ms = now_ms;
                    s_calm_since_ms = 0;
                    clear_hit_window();
                    ESP_LOGI(TAG, "Agua estabilizada. Auto-rearme concluido -> MONITORING");
                }
            } else {
                s_calm_since_ms = 0; // Continua agitada, reseta timer de água calma
            }
        }
    }

    return false;
}

void wave_detector_get_status(ripple_detector_status_t *out)
{
    if (out) {
        *out = s_status;
    }
}

void wave_detector_set_threshold(float new_threshold)
{
    if (new_threshold < 0.03f) {
        new_threshold = 0.03f;
    } else if (new_threshold > 3.5f) {
        new_threshold = 3.5f;
    }
    s_status.threshold = new_threshold;
    clear_hit_window();
    ESP_LOGI(TAG, "Novo limiar de sensibilidade configurado: %.3f", s_status.threshold);
}

void wave_detector_trigger_test_alert(uint32_t now_ms)
{
    s_status.state = RIPPLE_STATE_FALL_ALERT;
    s_status.alert_count++;
    s_state_enter_ms = now_ms;
    s_calm_since_ms = 0;
    ESP_LOGW(TAG, "🚨 ALERTA DE TESTE DISPARADO VIA BLUETOOTH! (Alertas=%lu)",
             (unsigned long)s_status.alert_count);
}

void wave_detector_rearm(uint32_t now_ms)
{
    s_status.state = RIPPLE_STATE_MONITORING;
    s_state_enter_ms = now_ms;
    s_calm_since_ms = 0;
    s_status.peak_wave_energy = 0.0f;
    clear_hit_window();
    ESP_LOGI(TAG, "Boia rearmada manualmente -> MONITORING");
}

void wave_detector_start_calibration(uint32_t now_ms)
{
    s_status.state = RIPPLE_STATE_CALIBRATING;
    s_state_enter_ms = now_ms;
    s_calib_sum_g = 0.0f;
    s_calib_samples = 0;
    clear_hit_window();
    ESP_LOGI(TAG, "Recalibracao iniciada por 3 segundos -> CALIBRATING");
}
