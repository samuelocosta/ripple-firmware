---
name: esp-idf-build
description: Guia e padrão operacional para agentes e desenvolvedores compilarem e gerarem binários do firmware Ripple com ESP-IDF v6.1 no Windows sem erros de sandbox ou PATH.
---

# ESP-IDF v6.1 Build & Compilation Guide (Agent Skill)

Este documento instrui agentes de IA e desenvolvedores sobre como compilar o firmware **Ripple** de forma rápida, reproduzível e sem erros de ambiente ou sandbox no Windows.

---

## 1. Localização do Ambiente ESP-IDF

No ambiente do host, o framework e toolchain estão instalados nos seguintes caminhos padrão:
- **ESP-IDF v6.1:** `C:\esp\v6.1\esp-idf`
- **Script de Ativação de Variáveis:** `C:\esp\v6.1\esp-idf\export.ps1`
- **Ambiente Python:** `C:\Users\Samuel\.espressif\python_env\idf6.1_py3.14_env`
- **Toolchain Xtensa & Ninja:** `C:\Users\Samuel\.espressif\tools`

---

## 2. Comando Canônico de Compilação (Recomendado)

> [!IMPORTANT]
> **Atenção para Agentes (Sandbox Execution):**
> Como o diretório `C:\esp\` e `C:\Users\Samuel\.espressif\` ficam fora da pasta do workspace, **sempre execute com `BypassSandbox: true`**.
> **NÃO tente** invocar `ninja.exe` ou `python.exe` manualmente montando caminhos na mão. Use sempre o comando canônico abaixo:

### No PowerShell / Terminal do Agente:
```powershell
C:\esp\v6.1\esp-idf\export.ps1; idf.py build
```

Este comando:
1. Executa `export.ps1` para injetar o toolchain `xtensa-esp32s3-elf-gcc`, `ninja`, `cmake` e o Python venv no `PATH` da sessão.
2. Invoca o `idf.py build` para reconfigurar CMake (se necessário), compilar os componentes e gerar os arquivos binários em `build/`:
   - `build/ripple.bin` (Aplicação principal com detecção de queda e BLE OTA)
   - `build/bootloader/bootloader.bin` (Bootloader anti-bricking com suporte a rollback)
   - `build/partition_table/partition-table.bin` (Tabela Dual-OTA)

---

## 3. Limpeza ou Reconfiguração

Se novos arquivos de configuração (`Kconfig`, `sdkconfig.defaults`) ou novos componentes I2C forem adicionados:

### Reconfigurar sem limpar build:
```powershell
C:\esp\v6.1\esp-idf\export.ps1; idf.py reconfigure build
```

### Limpeza total (Full Clean):
```powershell
C:\esp\v6.1\esp-idf\export.ps1; idf.py fullclean
```

---

## 4. Métodos de Envio do Binário

### 4.1 Via BLE OTA Sem Fios (Padrão de Produção / Boia na Bateria)
Quando a boia estiver selada ou funcionando na bateria, **NÃO use cabo USB**. Use o script de OTA Bluetooth do projeto:
```bash
python tools/ripple_bt_cli.py ota build/ripple.bin
```
- Transfere em ~5 segundos via BLE (throughput de ~100 KB/s).
- Valida o binário na flash e comuta as partições `ota_0` / `ota_1` com rollback automático anti-bricking.

---

### 4.2 Via Cabo USB (Apenas Primeira Gravação ou Bancada)

> [!CAUTION]
> **Comportamento do Circuito USB-Serial/JTAG (Reset via Watchdog Obrigatório):**
> Na placa **TENSTAR TS-ESP32-S3** (e clones Adafruit Feather ESP32-S3 TFT), o periférico nativo `USB-Serial/JTAG` retém a flag de download no registrador RTC após uma gravação via USB (`idf.py flash` ou `esptool write_flash`).
> O chip fica preso no bootloader ROM:
> ```text
> rst:0x15 (USB_UART_CHIP_RESET), boot:0x0 (DOWNLOAD(USB/UART0)) waiting for download
> ```
> O chaveamento tradicional de linhas RTS/DTR falha em reiniciar o microcontrolador no modo normal.
> Para reiniciar o circuito integrado e fazer o firmware executar sem precisar pressionar o botão físico `RST` da placa, **é necessário disparar um reset via RTC Watchdog**:
> 
> ```powershell
> python -m esptool --chip esp32s3 -p COMx --after watchdog-reset read-mac
> ```
> *(Substitua `COMx` pela porta Serial/JTAG nativa, ex: `COM3`).*
> 
> **Comando Completo de Gravação + Watchdog Reset em Bancada:**
> ```powershell
> C:\esp\v6.1\esp-idf\export.ps1
> idf.py -p COMx flash
> python -m esptool --chip esp32s3 -p COMx --after watchdog-reset read-mac
> ```
> O esptool enviará o comando de reset por watchdog (`Hard resetting with a watchdog...`), limpando o modo download e iniciando a aplicação imediatamente.
