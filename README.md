# Ripple — Smart Pool Fall Detection Buoy

> **Boia inteligente de segurança para piscinas com detecção inercial de quedas, alertas de emergência sem aplicativo via BLE Pop-ups, console sem fio e atualização de firmware Over-The-Air (BLE OTA).**

---

## 1. Visão Geral do Projeto

O **Ripple** é um dispositivo inteligente embarcado projetado para ficar flutuando na piscina e detectar quedas acidentais de crianças, adultos ou animais de estimação na água.

### Premissas de Engenharia
- **100% Selado e Sem Fios:** O hardware fica hermeticamente vedado dentro do corpo da boia com alimentação por bateria LiPo. Não há conectores USB ou botões acessíveis após a montagem.
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
| **`WS2812 RGB`** | **GPIO 33** | LED endereçável de feedback de status |
| **`LED Vermelho`** | **GPIO 13** | Indicador visual de alerta |

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

### 4.1 Instalação de Dependências
Requer Python 3.8+ e a biblioteca [Bleak](https://github.com/hbldh/bleak):
```bash
pip install bleak
```

---

### 4.2 Comandos Disponíveis

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
  Versao:            9df0765-dirty
  ESP-IDF:           v6.1
  Particao Ativa:    ota_1
  Particao de Boot:  ota_1
  Proximo Destino:   ota_0
========================================================
```

---

#### 4. Console de Logs do Sistema em Tempo Real (`logs`)
Subscreve o stream sem fio de logs do ESP-IDF (`ESP_LOGI`, `ESP_LOGW`, `ESP_LOGE`), permitindo depurar a boia na piscina exatamente como se estivesse conectada ao cabo USB:
```bash
python tools/ripple_bt_cli.py logs
```
*Opções:* `-d, --duration <segundos>` (padrão: 60s, pressione `Ctrl+C` para encerrar)

---

#### 5. Monitoramento de Telemetria Contínua (`monitor`)
Ativa o stream contínuo de energia das ondas, inclinação e temperatura a 5 Hz:
```bash
python tools/ripple_bt_cli.py monitor
```
*Opções:* `-d, --duration <segundos>` (padrão: 30s)

---

#### 6. Envio de Comandos e Calibração (`cmd`)
Envia comandos individuais via Nordic UART Service (NUS) e aguarda resposta JSON:

- **Consultar estado atual e sensores:**
  ```bash
  python tools/ripple_bt_cli.py cmd status
  ```
  *Resposta:* `{"state":"MONITORING","wave":0.083,"peak":0.084,"thr":0.22,"da":0.000,"gyro":11.6,"alerts":0,"temp":37.2}`

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

## 5. Compilação e Gravação Inicial

> **Nota:** A gravação via cabo USB é necessária apenas na **primeira inicialização** para gravar a tabela de partição `partitions.csv` e o bootloader na flash. A partir disso, todas as gravações futuras são feitas 100% via OTA Bluetooth.

### Compilar o projeto:
```bash
idf.py build
```

### Gravar via USB (ex: porta COM3):
```bash
idf.py -p COM3 flash
```

### Atualizações subsequentes (sem cabos):
```bash
python tools/ripple_bt_cli.py ota build/ripple.bin
```
