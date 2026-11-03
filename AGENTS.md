# Diretrizes para Agentes de IA — Ripple Firmware

Este documento define os padrões operacionais, protocolos de execução e arquitetura técnica para agentes de IA que atuam no repositório **Ripple Firmware**.

---

## 1. Protocolo de Descoberta de Ambiente (Host & Toolchain)

Para garantir portabilidade entre diferentes computadores, sistemas operacionais e desenvolvedores, o agente deve seguir o fluxo de inicialização abaixo antes de executar comandos de compilação ou gravação:

```
[Início] ──► Existe AGENTS.local.md?
                ├── SIM ──► Lê caminhos e versão de AGENTS.local.md e prossegue
                └── NÃO ──► [Fluxo First-Run]:
                              1. Sonda o ambiente local (PATH, $env:IDF_PATH, diretórios padrão)
                              2. Se múltiplas versões do ESP-IDF existirem, pergunta ao usuário
                              3. Gera AGENTS.local.md com os caminhos da máquina
                              4. Prossegue com a execução
```

### 1.1. Verificação do Arquivo Local
O agente **sempre deve verificar primeiro** se o arquivo [`AGENTS.local.md`](AGENTS.local.md) existe na raiz do repositório:
- **Se existir:** O agente deve ler as variáveis e caminhos definidos nele (ex.: script de exportação, versão do IDF e porta serial de bancada) e utilizá-los na sessão.
- **Se NÃO existir (*First Run*):** Significa que este é o primeiro contato do agente com esta máquina/ambiente.

### 1.2. Fluxo de Primeira Execução (*First Run*)
1. **Verificar se o ESP-IDF já está no PATH:**
   - Teste no terminal se `idf.py` responde diretamente (`idf.py --version`).
2. **Localizar instalações disponíveis no sistema:**
   - Inspecione a variável de ambiente do sistema `$env:IDF_PATH` (PowerShell) ou `$IDF_PATH` (Bash).
   - Verifique diretórios de instalação padrão comuns do sistema operacional (ex.: `C:\esp\` no Windows ou `~/esp/` no Linux/macOS).
3. **Resolução de Ambiguidade:**
   - **Caso exista apenas uma versão compatível:** O agente pode adotá-la e confirmar seu uso.
   - **Caso existam múltiplas versões instaladas no host:** O agente **NÃO DEVE** assumir ou adivinhar a versão. Deve perguntar explicitamente ao usuário através de pergunta de múltipla escolha qual versão ele deseja utilizar para compilar o firmware Ripple.
4. **Criação do `AGENTS.local.md`:**
   - Após a definição dos caminhos e versão pelo usuário, o agente deve **criar automaticamente** o arquivo `AGENTS.local.md` na raiz do projeto contendo as escolhas para que futuras execuções sejam instantâneas.
   - *Nota:* O arquivo `AGENTS.local.md` é explicitamente ignorado pelo `.gitignore` e nunca deve ser rastreado no controle de versão.

### 1.3. Template para `AGENTS.local.md`
Quando for necessário gerar o arquivo local, utilize o seguinte modelo:

```markdown
# Configurações Locais do Agente (Máquina Atual)

> Arquivo gerado para armazenar parâmetros do host local. Ignorado no Git.

## Ambiente ESP-IDF
- **Versão Selecionada:** <VERSAO_SELECIONADA>
- **Script de Ativação:** `<CAMINHO_PARA_EXPORT_SCRIPT>`
- **Porta Serial Preferencial:** `<PORTA_SERIAL_OPCIONAL>`

## Comando Canônico de Build no Terminal Desta Máquina
\`\`\`powershell
& "<CAMINHO_PARA_EXPORT_SCRIPT>"; idf.py build
\`\`\`
```

---

## 2. Regras de Execução e Sandbox

> [!IMPORTANT]
> **Acesso ao Host (Sandbox):**
> Ferramentas de compilação do ESP-IDF (compiladores Xtensa, CMake, Ninja, Python virtualenvs) ficam instaladas em diretórios do sistema fora do workspace do projeto.
> Ao executar comandos de compilação (`idf.py`), export de ambiente ou scripts de gravação serial via terminal de agente com sandbox isolado, utilize **permissão de bypass de sandbox (`BypassSandbox: true`)** para que o processo possa acessar os executáveis do sistema operacional.

---

## 3. Comandos Canônicos do Ciclo de Vida do Firmware

Todos os comandos de build devem ser executados a partir da raiz do repositório após o carregamento do ambiente ESP-IDF:

### 3.1. Compilação Padrão
```bash
idf.py build
```
Gera os artefatos em `build/`:
- `build/ripple.bin`: Binário da aplicação principal.
- `build/bootloader/bootloader.bin`: Bootloader do ESP-IDF.
- `build/partition_table/partition-table.bin`: Tabela de partições dual-OTA.

### 3.2. Reconfiguração CMake
Quando houver modificações em `CMakeLists.txt`, `Kconfig` ou `sdkconfig.defaults`:
```bash
idf.py reconfigure build
```

### 3.3. Limpeza Completa (*Full Clean*)
```bash
idf.py fullclean
```

---

## 4. Métodos de Gravação e Atualização (*Flashing*)

### 4.1. Via BLE OTA Sem Fios (Produção / Boia Fechada na Bateria)
Para dispositivos já montados, selados ou operando em bateria, **não conecte cabos**. Utilize a ferramenta BLE CLI do projeto:
```bash
python tools/ripple_bt_cli.py ota build/ripple.bin
```
- Transmissão em alta velocidade (~100 KB/s, cerca de 5 segundos).
- Comutação automática entre slots `ota_0` e `ota_1` com mecanismo anti-bricking de rollback.

### 4.2. Via USB com Workaround de RTC Watchdog (Bancada / Placa Virgem)

> [!CAUTION]
> **Comportamento do Circuito USB-Serial/JTAG Nativo do ESP32-S3:**
> Em placas com conexão USB nativa ao SoC (como TENSTAR TS-ESP32-S3 / clones Adafruit ESP32-S3 TFT), o periférico nativo `USB-Serial/JTAG` retém a flag de download no registrador RTC após uma gravação USB (`idf.py flash` ou `esptool write_flash`), mantendo o chip em modo bootloader ROM (`rst:0x15 (USB_UART_CHIP_RESET), boot:0x0 (DOWNLOAD...)`).
> 
> O chaveamento tradicional por RTS/DTR falha em reiniciar para o modo de execução normal sem intervenção manual no botão físico `RST`.
> 
> **Procedimento de Gravação de Bancada sem Botão Físico:**
> Para disparar a execução imediata da aplicação via software, execute a gravação seguida de um reset forçado por RTC Watchdog via `esptool`:
> ```bash
> idf.py -p <PORTA_SERIAL> flash
> python -m esptool --chip esp32s3 -p <PORTA_SERIAL> --after watchdog-reset read-mac
> ```
> O comando `--after watchdog-reset` limpa o registrador RTC de download e inicializa o firmware instantaneamente.

---

## 5. Diretrizes de Arquitetura e Código

Ao implementar ou modificar componentes do firmware em `main/`:

1. **FreeRTOS & Concorrência:**
   - Não bloqueie tarefas com esperas ativas. Utilize filas (`QueueHandle_t`), semáforos ou notificações de tarefa.
   - Respeite o Watchdog de Tarefas (TWDT) alimentando-o adequadamente em loops longos.
2. **Pilha Bluetooth (NimBLE):**
   - Eventos de BLE GAP e GATT rodam na task do NimBLE. Não execute operações demoradas (como I2C ou leituras de flash bloqueantes) diretamente dentro de callbacks de eventos NimBLE.
3. **Partições e Armazenamento NVS:**
   - Configurações persistentes (como calibração de bateria e sensibilidade de queda) devem utilizar o namespace NVS designado (`ripple_cfg`).
   - Sempre chame `nvs_commit()` após alterações de parâmetros.
4. **Barramento I2C e Sensores:**
   - Garanta a inicialização correta dos pinos de chaveamento de energia (`TFT_I2C_POWER` no GPIO 21) antes de sondar dispositivos I2C (`QMI8658C` no `0x6B`, `BMP280` no `0x77`).
