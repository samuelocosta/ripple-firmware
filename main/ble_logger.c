#include "ble_logger.h"
#include "ble_ripple.h"

#include <stdio.h>
#include <stdarg.h>
#include <string.h>
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/portmacro.h"

static vprintf_like_t s_default_vprintf = NULL;
static volatile bool s_ble_logging_enabled = false;
static portMUX_TYPE s_log_spinlock = portMUX_INITIALIZER_UNLOCKED;
static volatile bool s_in_log = false;

static int ble_logger_vprintf(const char *fmt, va_list args)
{
    va_list args_copy;
    va_copy(args_copy, args);

    int ret = 0;
    if (s_default_vprintf) {
        ret = s_default_vprintf(fmt, args);
    }

    if (!s_ble_logging_enabled || !ble_ripple_is_connected()) {
        va_end(args_copy);
        return ret;
    }

    // Evita recursão infinita caso ble_ripple_notify_text gere logs internos
    portENTER_CRITICAL(&s_log_spinlock);
    if (s_in_log) {
        portEXIT_CRITICAL(&s_log_spinlock);
        va_end(args_copy);
        return ret;
    }
    s_in_log = true;
    portEXIT_CRITICAL(&s_log_spinlock);

    char log_buf[256];
    int len = vsnprintf(log_buf, sizeof(log_buf), fmt, args_copy);
    va_end(args_copy);

    if (len > 0 && log_buf[0] != '\0') {
        // Envia notificação BLE sem bloquear o caller
        ble_ripple_notify_text(log_buf);
    }

    portENTER_CRITICAL(&s_log_spinlock);
    s_in_log = false;
    portEXIT_CRITICAL(&s_log_spinlock);

    return ret;
}

esp_err_t ble_logger_init(void)
{
    s_default_vprintf = esp_log_set_vprintf(ble_logger_vprintf);
    return ESP_OK;
}

void ble_logger_set_enabled(bool enabled)
{
    s_ble_logging_enabled = enabled;
}

bool ble_logger_is_enabled(void)
{
    return s_ble_logging_enabled;
}
