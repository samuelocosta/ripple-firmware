#include "imu_sensor.h"
#include "board_pins.h"

#include <string.h>
#include <qmi8658c.h>
#include <bmp280.h>
#include "driver/gpio.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "i2cdev.h"

static const char *TAG = "IMU_SENSOR";

static i2c_dev_t s_qmi_dev = {0};
static bmp280_t s_bmp_dev = {0};
static bool s_imu_ready = false;
static bool s_bmp_ready = false;
static float s_last_temp_c = 25.0f;
static float s_last_press_hpa = 1013.25f;

static bool try_init_qmi8658(uint8_t addr, gpio_num_t sda, gpio_num_t scl)
{
    memset(&s_qmi_dev, 0, sizeof(s_qmi_dev));
    if (qmi8658c_init_desc(&s_qmi_dev, addr, BOARD_I2C_PORT, sda, scl) != ESP_OK) {
        return false;
    }

    qmi8658c_config_t cfg = {
        .mode = QMI8658C_MODE_DUAL,
        .acc_scale = QMI8658C_ACC_SCALE_4G,
        .acc_odr = QMI8658C_ACC_ODR_125,
        .gyro_scale = QMI8658C_GYRO_SCALE_512DPS,
        .gyro_odr = QMI8658C_GYRO_ODR_125,
    };

    if (qmi8658c_setup(&s_qmi_dev, &cfg) != ESP_OK) {
        qmi8658c_free_desc(&s_qmi_dev);
        return false;
    }

    // Faz uma leitura de teste para garantir que o sensor responde no barramento I2C
    qmi8658c_data_t test_data = {0};
    if (qmi8658c_read_data(&s_qmi_dev, &test_data) != ESP_OK) {
        qmi8658c_free_desc(&s_qmi_dev);
        return false;
    }

    ESP_LOGI(TAG, "QMI8658C detectado em 0x%02X (SDA=%d, SCL=%d) | Acc=(%.2f, %.2f, %.2f)",
             addr, sda, scl, test_data.acc.x, test_data.acc.y, test_data.acc.z);
    return true;
}

static bool try_init_bmp280(uint8_t addr, gpio_num_t sda, gpio_num_t scl)
{
    memset(&s_bmp_dev, 0, sizeof(s_bmp_dev));
    if (bmp280_init_desc(&s_bmp_dev, addr, BOARD_I2C_PORT, sda, scl) != ESP_OK) {
        return false;
    }

    bmp280_params_t params;
    bmp280_init_default_params(&params);
    params.mode = BMP280_MODE_NORMAL;

    if (bmp280_init(&s_bmp_dev, &params) != ESP_OK) {
        bmp280_free_desc(&s_bmp_dev);
        return false;
    }

    ESP_LOGI(TAG, "BMP280 detectado em 0x%02X (SDA=%d, SCL=%d)", addr, sda, scl);
    return true;
}

esp_err_t imu_sensor_init(void)
{
    // 1. Liga a alimentação dos sensores I2C (GPIO 21 = HIGH)
    gpio_config_t pwr_conf = {
        .pin_bit_mask = (1ULL << BOARD_TFT_I2C_POWER_GPIO),
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    ESP_ERROR_CHECK(gpio_config(&pwr_conf));
    gpio_set_level(BOARD_TFT_I2C_POWER_GPIO, 1);

    // Aguarda a estabilização do LDO de 3.3V dos sensores
    vTaskDelay(pdMS_TO_TICKS(80));

    // 2. Inicializa subsistema i2cdev
    ESP_ERROR_CHECK(i2cdev_init());

    // 3. Inicializa o QMI8658C nos pinos da TENSTAR ESP32-S3 (SDA=42, SCL=41)
    //    Testa os endereços 0x6B, 0x6A e também 0x77 caso haja inversão
    const uint8_t qmi_addrs[] = {
        BOARD_QMI8658_I2C_ADDR_PRIMARY,
        BOARD_QMI8658_I2C_ADDR_ALT,
    };

    for (size_t i = 0; i < sizeof(qmi_addrs); i++) {
        if (try_init_qmi8658(qmi_addrs[i], BOARD_I2C_SDA_GPIO, BOARD_I2C_SCL_GPIO)) {
            s_imu_ready = true;
            break;
        }
    }

    if (!s_imu_ready) {
        ESP_LOGW(TAG, "QMI8658C nao respondeu em SDA=%d/SCL=%d; tentando scan secundario...",
                 BOARD_I2C_SDA_GPIO, BOARD_I2C_SCL_GPIO);
    }

    // 4. Inicializa o BMP280 (0x77 ou 0x76)
    if (try_init_bmp280(BOARD_BMP280_I2C_ADDR_PRIMARY, BOARD_I2C_SDA_GPIO, BOARD_I2C_SCL_GPIO) ||
        try_init_bmp280(BOARD_BMP280_I2C_ADDR_ALT, BOARD_I2C_SDA_GPIO, BOARD_I2C_SCL_GPIO)) {
        s_bmp_ready = true;
    } else {
        ESP_LOGW(TAG, "BMP280 nao encontrado em 0x77/0x76 (continuando apenas com QMI8658C)");
    }

    return s_imu_ready ? ESP_OK : ESP_ERR_NOT_FOUND;
}

esp_err_t imu_sensor_read(ripple_sensor_sample_t *out, bool read_env)
{
    if (!out) {
        return ESP_ERR_INVALID_ARG;
    }

    memset(out, 0, sizeof(*out));
    out->imu_ok = false;
    out->bmp_ok = s_bmp_ready;
    out->temperature_c = s_last_temp_c;
    out->pressure_hpa = s_last_press_hpa;

    if (s_imu_ready) {
        qmi8658c_data_t raw = {0};
        if (qmi8658c_read_data(&s_qmi_dev, &raw) == ESP_OK) {
            out->ax = raw.acc.x;
            out->ay = raw.acc.y;
            out->az = raw.acc.z;
            out->gx = raw.gyro.x;
            out->gy = raw.gyro.y;
            out->gz = raw.gyro.z;
            if (raw.temperature != 0.0f) {
                s_last_temp_c = raw.temperature;
                out->temperature_c = raw.temperature;
            }
            out->imu_ok = true;
        }
    }

    if (read_env && s_bmp_ready) {
        float temp = 0.0f;
        float press_pa = 0.0f;
        if (bmp280_read_float(&s_bmp_dev, &temp, &press_pa, NULL) == ESP_OK) {
            s_last_temp_c = temp;
            s_last_press_hpa = press_pa / 100.0f;
            out->temperature_c = s_last_temp_c;
            out->pressure_hpa = s_last_press_hpa;
        }
    }

    return out->imu_ok ? ESP_OK : ESP_FAIL;
}

bool imu_sensor_is_ready(void)
{
    return s_imu_ready;
}
