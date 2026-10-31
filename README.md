# Ripple — Smart Pool Fall Detection Buoy

> **Boia inteligente de segurança para piscinas com detecção inercial de quedas, alertas de emergência sem aplicativo via BLE Pop-ups, console sem fio e atualização de firmware Over-The-Air (BLE OTA).**

---

## 1. Visão Geral do Projeto

O **Ripple** é um dispositivo inteligente embarcado projetado para ficar flutuando na piscina e detectar quedas acidentais de crianças, adultos ou animais de estimação na água.

### Premissas de Engenharia
- **100% Selado e Sem Fios:** O hardware fica hermeticamente vedado dentro do corpo da boia com alimentação por bateria LiPo. Não há conectores USB ou botões acessíveis após a montagem.
- **Monitoramento Inteligente de Bateria:** Mede a tensão da célula em tempo real com calibração precisa (suporte ao medidor I2C MAX17048 em `0x36` e fallback analógico ADC1). Permite configurar remotamente a tensão mínima segura com persistência na NVS. Quando a carga atinge $\le 15\%$, aciona alerta visual contínuo (LED vermelho estático e RGB em vermelho sólido), mantendo prioridade absoluta para alertas de emergência caso ocorra uma queda.
- **Proteção da Bateria via Deep Sleep (Corte a 0%):** Se a tensão cair até ou abaixo da tensão mínima segura configurada ($V \le V_{\text{min}}$, carga = 0%), a boia desliga imediatamente todos os periféricos (sensores I2C, NeoPixel, display e LEDs) e entra em modo de **Deep Sleep** para reduzir o consumo a microamperes, protegendo a química da bateria LiPo contra danos irreversíveis. O microcontrolador arma o despertar periódico (timer RTC de 1 hora para verificar se foi recarregada) e despertar pelo botão BOOT (GPIO 0).
- **Detecção Inercial de Ondas (IMU 6-Eixos):** O sensor inercial onboard mede oscilações bruscas de inclinação e aceleração ($\Delta g$ e velocidade angular) provocadas pelo impacto e ondas na água.
- **Alertas Instantâneos Sem Aplicativo:** Ao detectar uma queda, a boia dispara pacotes *Raw BLE Advertisement* multiplexando payloads de **Apple Continuity Proximity Pairing**, **Google Fast Pair** e **Samsung Galaxy**, disparando pop-ups e notificações nativas na tela de smartphones próximos sem exigir aplicativo instalado.
- **Atualização OTA 100% via Bluetooth (BLE OTA):** O firmware pode ser atualizado pelo ar através da conexão BLE em apenas ~5 segundos, com proteção anti-bricking por rollback automático.
- **Console Sem Fio:** Telemetria, comandos de calibração e logs internos do sistema (`ESP_LOG*`) são lidos remotamente pelo notebook via Bluetooth.

---

## 2. Hardware e Mapeamento de Pinos

- **SoC:** Espressif **ESP32-S3FH4R2** (Dual-Core Xtensa LX7 @ 240 MHz, 4 MB Flash embarcada, 2 MB PSRAM).
- **Placa:** TENSTAR ROBOT TS-ESP32-S3 (`ESP32S3-1.14TFT`).

| Periférico / Sinal | Pinos / Endereço | Descrição / Função |
| :--- | :---: | :--- |
| **`TFT_I2C_POWER`** | **GPIO 21** | Chave FET que alimenta o barramento I2C onboard e display |
| **`NEOPIXEL_POWER`** | **GPIO 34** | Chave que alimenta o LED RGB WS2812 |
| **`I2C_SDA`** | **GPIO 42** | Linha de dados I2C (400 kHz) |
| **`I2C_SCL`** | **GPIO 41** | Linha de clock I2C |
| **`QMI8658C`** | **`0x6B` (I2C)** | IMU 6-Eixos: Acelerômetro ($\pm 4g$) + Giroscópio ($\pm 512^\circ/\text{s}$) |
| **`BMP280`** | **`0x77` (I2C)** | Sensor de temperatura interna e pressão barométrica |
| **`MAX17048`** | **`0x36` (I2C)** | Medidor de bateria Fuel Gauge (com fallback para ADC1 Ch3 / GPIO 4) |
| **`WS2812 RGB`** | **GPIO 33** | LED endereçável de feedback de status (Vermelho sólido em bateria <= 15%) |
| **`LED Vermelho`** | **GPIO 13** | Indicador visual de alerta (Aceso estático em bateria <= 15%) |

---

## 3. Arquitetura de Firmware e Partições

- **Framework:** ESP-IDF v6.1 (FreeRTOS)
- **Pilha Bluetooth:** Apache NimBLE configurado para modo periférico e broadcaster simultâneos.
- **Esquema de Partições Dual-OTA:**
  - `otadata` (8 KB): Ponteiro de partição ativa e controle de rollback.
  - `ota_0` (1.90 MB): Slot de aplicação A.
  - `ota_1` (1.90 MB): Slot de aplicação B.
  - *Rollback Anti-Bricking:* Se um novo firmware falhar na inicialização ou entrar em bootloop, o bootloader reverte automaticamente para a partição estável anterior.

---

## 4. Ferramenta CLI Python (`tools/ripple_bt_cli.py`)

A boia expõe serviços BLE GATT que permitem controle, depuração, monitoramento e atualização de firmware através do script Python multiplataforma [`tools/ripple_bt_cli.py`](tools/ripple_bt_cli.py).

### 4.1 Instalação e Modos de Uso

Requer Python 3.8+ e a biblioteca [Bleak](https://github.com/hbldh/bleak):
```bash
pip install bleak
```

A ferramenta opera em **dois modos flexíveis**:

1. **Modo Interativo (Interpretador de Comandos / Shell Interativo):**
   Basta executar o script sem argumentos. Ele inicializa uma sessão interativa no próprio terminal com prompt `ripple> ` e aguarda seus comandos continuamente, mantendo a janela aberta e sem abrir abas extras:
   ```bash
   python tools/ripple_bt_cli.py
   ```
   *No prompt interativo:*
   ```text
   ripple> battery
   ripple> status
   ripple> sens 0.25
   ripple> test_alert
   ripple> rearm
   ripple> help
   ripple> exit
   ```

2. **Modo Comando Direto (One-Shot para automação / scripts):**
   Passe o comando diretamente na linha de comando para execução única imediata:
   ```bash
   python tools/ripple_bt_cli.py battery
   python tools/ripple_bt_cli.py ota build/ripple.bin
   ```

---

### 4.2 Comandos Disponíveis

#### 0. Ajuda Completa (`help`)
Exibe a lista de todos os subcomandos e opções disponíveis:
```bash
python tools/ripple_bt_cli.py help
```

---

#### 1. Escanear a Boia (`scan`)
Procura anúncios BLE emitidos pela boia e exibe endereço MAC, RSSI (potência de sinal) e Manufacturer Data:
```bash
python tools/ripple_bt_cli.py scan
```
*Opções:* `-t, --timeout <segundos>` (padrão: 6.0s)

---

#### 2. Atualização de Firmware Sem Fios (`ota`)
Transfere um novo binário de firmware diretamente para a partição OTA inativa da boia via BLE, valida a imagem, agenda o reinício e comuta a partição:
```bash
python tools/ripple_bt_cli.py ota build/ripple.bin
```
- **Taxa de transferência:** ~100 KB/s (firmware de 540 KB transferido em ~5 segundos).
- Apresenta barra de progresso interativa com porcentagem, bytes enviados e taxa de transferência.
- *Opções:* `-c, --chunk <bytes>` (tamanho do bloco, padrão: 480 bytes).

---

#### 3. Informações de Firmware e Partição (`ota-info`)
Consulta a partição atualmente em execução (`ota_0` ou `ota_1`), a partição definida para o próximo boot e a partição que receberá o próximo update:
```bash
python tools/ripple_bt_cli.py ota-info
```
*Exemplo de saída:*
```text
========== INFORMACOES DE FIRMWARE E PARTICAO ==========
  Aplicacao:         ripple
  Versao:            71cef49-dirty
  ESP-IDF:           v6.1
  Particao Ativa:    ota_1
  Particao de Boot:  ota_1
  Proximo Destino:   ota_0
========================================================
```

---

#### 4. Consulta de Nível e Tensão da Bateria (`battery`)
Lê a tensão real da bateria, porcentagem estimada, tensão mínima segura e status de alerta:
```bash
python tools/ripple_bt_cli.py battery
```
*Exemplo de saída:*
```text
========== STATUS DA BATERIA (RIPPLE) ==========
  Tensao Atual:      4.168 V
  Capacidade:        [###################-] 96.8%
  Tensao Minima:     3.20 V (0% Referencia Segura)
  Tensao Maxima:     4.20 V (100% Referencia Teorica)
  Estado de Alerta:  NORMAL
  Status do Sensor:  OK
================================================
```

---

#### 5. Configurar Tensão Mínima Segura da Bateria (`set-bat-min`)
Define o limite inferior de operação da bateria (referência de 0% de carga) e salva na memória permanente **NVS** da boia:
```bash
python tools/ripple_bt_cli.py set-bat-min 3.30
```
*Resposta:*
```text
[+] Tensao minima segura atualizada com sucesso para 3.30 V!
[+] Nova porcentagem recalculada: 96.4%
```

---

#### 6. Console de Logs do Sistema em Tempo Real (`logs`)
Subscreve o stream sem fio de logs do ESP-IDF (`ESP_LOGI`, `ESP_LOGW`, `ESP_LOGE`), permitindo depurar a boia na piscina exatamente como se estivesse conectada ao cabo USB:
```bash
python tools/ripple_bt_cli.py logs
```
*Opções:* `-d, --duration <segundos>` (padrão: 60s, pressione `Ctrl+C` para encerrar)

---

#### 7. Monitoramento de Telemetria Contínua (`monitor`)
Ativa o stream contínuo de energia das ondas, inclinação, temperatura e bateria a 5 Hz:
```bash
python tools/ripple_bt_cli.py monitor
```
*Opções:* `-d, --duration <segundos>` (padrão: 30s)

---

#### 8. Envio de Comandos e Calibração (`cmd`)
Envia comandos individuais via Nordic UART Service (NUS) e aguarda resposta JSON:

- **Consultar estado atual, sensores e bateria:**
  ```bash
  python tools/ripple_bt_cli.py cmd status
  ```
  *Resposta:* `{"state":"MONITORING","wave":0.000,"peak":0.000,"thr":0.22,"da":0.000,"gyro":0.0,"alerts":0,"temp":25.0,"vbat":4.168,"bat_pct":96.8,"bat_low":false}`

- **Consultar telemetria direta da bateria:**
  ```bash
  python tools/ripple_bt_cli.py cmd battery
  ```

- **Ajustar tensão mínima segura da bateria:**
  ```bash
  python tools/ripple_bt_cli.py cmd "bat_min 3.30"
  ```

- **Ajustar limiar de sensibilidade de queda:**
  ```bash
  python tools/ripple_bt_cli.py cmd "sens 0.18"
  ```

- **Disparar alerta de teste (simulação):**
  ```bash
  python tools/ripple_bt_cli.py cmd test_alert
  ```

- **Rearmar a boia após alerta:**
  ```bash
  python tools/ripple_bt_cli.py cmd rearm
  ```

- **Iniciar auto-calibração do nível de água:**
  ```bash
  python tools/ripple_bt_cli.py cmd calibrate
  ```

- **Reiniciar a boia remotamente:**
  ```bash
  python tools/ripple_bt_cli.py cmd reboot
  ```

- **Forçar rollback de firmware (voltar para a versão anterior):**
  ```bash
  python tools/ripple_bt_cli.py cmd rollback
  ```

---

- **Forçar entrada em Deep Sleep remotamente (economia extrema):**
  ```bash
  python tools/ripple_bt_cli.py cmd sleep
  ```

---

## 5. Compilação e Gravação

> [!TIP]
> Consulte o guia operacional completo para agentes e desenvolvedores em [`SKILL_ESP_IDF_BUILD.md`](SKILL_ESP_IDF_BUILD.md).

### Compilar o projeto (PowerShell no Windows):
```powershell
C:\esp\v6.1\esp-idf\export.ps1; idf.py build
```

### Gravação Sem Fios Over-The-Air (Recomendado — Boia na Bateria):
```bash
python tools/ripple_bt_cli.py ota build/ripple.bin
```

### Gravação Inicial via USB (apenas se a flash estiver virgem):

> [!CAUTION]
> **Atenção ao Reset USB-Serial/JTAG:**
> Na placa TENSTAR TS-ESP32-S3, após gravar via USB o chip permanece no modo bootloader ROM (`waiting for download`).
> Para reiniciar o circuito e rodar a aplicação sem pressionar o botão físico `RST`, execute o reset via watchdog:
> ```powershell
> C:\esp\v6.1\esp-idf\export.ps1
> idf.py -p COMx flash
> python -m esptool --chip esp32s3 -p COMx --after watchdog-reset read-mac
> ```
