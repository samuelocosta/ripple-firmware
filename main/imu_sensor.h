#pragma once

#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    float ax;            // Aceleração X (g)
    float ay;            // Aceleração Y (g)
    float az;            // Aceleração Z (g)
    float gx;            // Giroscópio X (deg/s)
    float gy;            // Giroscópio Y (deg/s)
    float gz;            // Giroscópio Z (deg/s)
    float temperature_c; // Temperatura (BMP280 ou QMI8658C)
    float pressure_hpa;  // Pressão barométrica (hPa, se BMP280 presente)
    bool imu_ok;
    bool bmp_ok;
} ripple_sensor_sample_t;

/**
 * @brief Liga o pino GPIO 21 (TFT_I2C_POWER), inicializa o barramento I2C (SDA=42, SCL=41)
 *        e configura os sensores QMI8658C (IMU) e BMP280.
 */
esp_err_t imu_sensor_init(void);

/**
 * @brief Lê uma amostra instantânea do IMU (QMI8658C) e, opcionalmente, do BMP280.
 * @param out Ponteiro para estrutura de saída.
 * @param read_env Se true, atualiza também a leitura de temperatura/pressão do BMP280.
 */
esp_err_t imu_sensor_read(ripple_sensor_sample_t *out, bool read_env);

/**
 * @brief Retorna se o QMI8658C foi inicializado com sucesso.
 */
bool imu_sensor_is_ready(void);

#ifdef __cplusplus
}
#endif
