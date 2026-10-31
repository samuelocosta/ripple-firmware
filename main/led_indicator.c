#include "led_indicator.h"
#include "board_pins.h"

#include "driver/gpio.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "led_strip.h"

static const char *TAG = "LED_IND";

static led_strip_handle_t s_strip = NULL;
static volatile ripple_led_mode_t s_current_mode = LED_MODE_CALIBRATING;
static volatile bool s_ble_connected = false;
static volatile bool s_low_battery = false;

static void set_rgb(uint8_t r, uint8_t g, uint8_t b)
{
    if (!s_strip) {
        return;
    }
    led_strip_set_pixel(s_strip, 0, r, g, b);
    led_strip_refresh(s_strip);
}

static void led_animation_task(void *arg)
{
    (void)arg;
    uint32_t tick = 0;

    while (true) {
        ripple_led_mode_t mode = s_current_mode;
        bool ble_conn = s_ble_connected;
        bool low_bat = s_low_battery;

        // Emergency fall alert ALWAYS takes absolute priority over everything else
        if (mode == LED_MODE_FALL_ALERT) {
            // Estrobo de alta visibilidade Vermelho / Branco (10 Hz)
            bool flash = (tick % 2) == 0;
            if (flash) {
                set_rgb(255, 0, 0);
                gpio_set_level(BOARD_LED_RED_GPIO, 1);
            } else {
                set_rgb(180, 180, 180);
                gpio_set_level(BOARD_LED_RED_GPIO, 0);
            }
        } else if (low_bat) {
            // Low battery alert (<= 15%): Red LED constantly ON + RGB solid RED
            set_rgb(255, 0, 0);
            gpio_set_level(BOARD_LED_RED_GPIO, 1);
        } else {
            // Normal operating animations
            switch (mode) {
                case LED_MODE_CALIBRATING: {
                    // Azul pulsante rápido (a cada ~100ms alterna intensidade)
                    uint8_t blue = ((tick % 10) < 5) ? 120 : 15;
                    set_rgb(0, 10, blue);
                    gpio_set_level(BOARD_LED_RED_GPIO, (tick % 4 == 0) ? 1 : 0);
                    break;
                }

                case LED_MODE_MONITORING: {
                    // Efeito de respiração suave a cada ~2.5s (50 ticks de 50ms)
                    int phase = (int)(tick % 40);
                    int triangle = (phase < 20) ? phase : (40 - phase); // 0..20
                    uint8_t intensity = (uint8_t)(8 + triangle * 4);    // 8..88

                    if (ble_conn) {
                        // Ciano/Verde-Água quando o notebook está conectado via BLE monitorando
                        set_rgb(0, intensity, intensity / 2);
                    } else {
                        // Verde puro quando monitorando autonomamente
                        set_rgb(0, intensity, 0);
                    }

                    // Heartbeat discreto no LED vermelho a cada 3 segundos
                    gpio_set_level(BOARD_LED_RED_GPIO, (tick % 60 == 0) ? 1 : 0);
                    break;
                }

                case LED_MODE_FALL_ALERT:
                    break;
            }
        }

        tick++;
        vTaskDelay(pdMS_TO_TICKS(50));
    }
}

esp_err_t led_indicator_init(void)
{
    // Garante que o backlight do TFT fique desligado (GPIO 45 = LOW) para economizar bateria,
    // liga o NEOPIXEL_POWER (GPIO 34 = HIGH) e configura o LED Vermelho (GPIO 13).
    gpio_config_t io_conf = {
        .pin_bit_mask = (1ULL << BOARD_LED_RED_GPIO) |
                        (1ULL << BOARD_NEOPIXEL_POWER_GPIO) |
                        (1ULL << BOARD_TFT_BACKLIGHT_GPIO),
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    ESP_ERROR_CHECK(gpio_config(&io_conf));

    gpio_set_level(BOARD_TFT_BACKLIGHT_GPIO, 0);
    gpio_set_level(BOARD_NEOPIXEL_POWER_GPIO, 1);
    gpio_set_level(BOARD_LED_RED_GPIO, 0);

    led_strip_config_t strip_config = {
        .strip_gpio_num = BOARD_NEOPIXEL_GPIO,
        .max_leds = 1,
        .led_model = LED_MODEL_WS2812,
        .color_component_format = LED_STRIP_COLOR_COMPONENT_FMT_GRB,
        .flags = {
            .invert_out = false,
        },
    };

    led_strip_rmt_config_t rmt_config = {
        .clk_src = RMT_CLK_SRC_DEFAULT,
        .resolution_hz = 10 * 1000 * 1000, // 10 MHz
        .mem_block_symbols = 0,
        .flags = {
            .with_dma = false,
        },
    };

    esp_err_t err = led_strip_new_rmt_device(&strip_config, &rmt_config, &s_strip);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Falha ao inicializar WS2812 no GPIO %d: %s",
                 BOARD_NEOPIXEL_GPIO, esp_err_to_name(err));
        return err;
    }

    led_strip_clear(s_strip);

    xTaskCreate(led_animation_task, "led_anim", 3072, NULL, 3, NULL);
    ESP_LOGI(TAG, "LEDs inicializados (Red=GPIO%d, Neopixel=GPIO%d, NeoPwr=GPIO%d)",
             BOARD_LED_RED_GPIO, BOARD_NEOPIXEL_GPIO, BOARD_NEOPIXEL_POWER_GPIO);
    return ESP_OK;
}

void led_indicator_set_mode(ripple_led_mode_t mode)
{
    s_current_mode = mode;
}

void led_indicator_set_ble_connected(bool connected)
{
    s_ble_connected = connected;
}

void led_indicator_set_low_battery(bool is_low)
{
    s_low_battery = is_low;
}
