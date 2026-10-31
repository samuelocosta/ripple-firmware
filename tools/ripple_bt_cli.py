#!/usr/bin/env python3
"""
Ripple Smart Buoy — Bluetooth LE CLI & OTA Tool (Windows / Cross-platform via Bleak)

Exemplos de uso:
  python tools/ripple_bt_cli.py scan
  python tools/ripple_bt_cli.py cmd status
  python tools/ripple_bt_cli.py cmd "sens 0.18"
  python tools/ripple_bt_cli.py cmd test_alert
  python tools/ripple_bt_cli.py cmd rearm
  python tools/ripple_bt_cli.py monitor
  python tools/ripple_bt_cli.py logs
  python tools/ripple_bt_cli.py ota build/ripple.bin
  python tools/ripple_bt_cli.py ota-info
"""

import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
from bleak import BleakClient, BleakScanner

# UUIDs do Nordic UART Service (NUS)
NUS_SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
NUS_RX_CHAR_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
NUS_TX_CHAR_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"

# UUIDs do Serviço Dedicado Ripple BLE OTA
RIPPLE_OTA_SVC_UUID  = "80140001-3850-424a-b5e0-9e67d266ff00"
RIPPLE_OTA_CTRL_UUID = "80140002-3850-424a-b5e0-9e67d266ff00"
RIPPLE_OTA_DATA_UUID = "80140003-3850-424a-b5e0-9e67d266ff00"

TARGET_NAMES = ("Ripple-Buoy", "RIPPLE: QUEDA NA PISCINA!", "RIPPLE:QUEDA!")


async def find_ripple_device(timeout: float = 6.0):
    print(f"[*] Procurando boia Ripple via BLE ({timeout:.0f}s)...")
    devices = await BleakScanner.discover(timeout=timeout, return_adv=True)

    for addr, (dev, adv) in devices.items():
        name = dev.name or adv.local_name or ""
        uuids = [u.lower() for u in (adv.service_uuids or [])]
        if any(t.lower() in name.lower() for t in TARGET_NAMES) or NUS_SERVICE_UUID in uuids:
            print(f"[+] Boia encontrada: {name or 'Ripple'} [{dev.address}] (RSSI: {adv.rssi} dBm)")
            return dev

    print("[-] Nenhuma boia Ripple encontrada. Verifique se a placa esta ligada.")
    return None


async def cmd_scan(timeout: float = 6.0):
    print(f"[*] Escaneando anuncios BLE por {timeout:.0f} segundos...")
    devices = await BleakScanner.discover(timeout=timeout, return_adv=True)
    found_any = False

    for addr, (dev, adv) in devices.items():
        name = dev.name or adv.local_name or ""
        uuids = [u.lower() for u in (adv.service_uuids or [])]
        mfg = adv.manufacturer_data
        svc_data = adv.service_data

        is_ripple = (
            any(t.lower() in name.lower() for t in TARGET_NAMES)
            or NUS_SERVICE_UUID in uuids
            or "0000fe2c-0000-1000-8000-00805f9b34fb" in svc_data
        )
        if is_ripple:
            found_any = True
            print(f"  -> Endereco: {dev.address} | Nome: '{name}' | RSSI: {adv.rssi} dBm")
            if mfg:
                print(f"     Manufacturer Data: { {hex(k): v.hex() for k, v in mfg.items()} }")
            if svc_data:
                print(f"     Service Data:      { {k: v.hex() for k, v in svc_data.items()} }")

    if not found_any:
        print("[-] Nenhum pacote da boia Ripple capturado no scan.")


async def cmd_send(command_text: str):
    dev = await find_ripple_device()
    if not dev:
        sys.exit(1)

    received_event = asyncio.Event()

    def on_notify(_, data: bytearray):
        msg = data.decode("utf-8", errors="replace").strip()
        print(f"[RIPPLE -> PC] {msg}")
        received_event.set()

    async with BleakClient(dev) as client:
        print(f"[*] Conectado a {dev.address}. Enviando comando: '{command_text}'")
        await client.start_notify(NUS_TX_CHAR_UUID, on_notify)
        await client.write_gatt_char(NUS_RX_CHAR_UUID, (command_text + "\n").encode("utf-8"), response=False)

        try:
            await asyncio.wait_for(received_event.wait(), timeout=4.0)
        except asyncio.TimeoutError:
            raw = await client.read_gatt_char(NUS_TX_CHAR_UUID)
            print(f"[RIPPLE Read] {raw.decode('utf-8', errors='replace').strip()}")

        await client.stop_notify(NUS_TX_CHAR_UUID)


async def cmd_monitor(duration: float = 30.0):
    dev = await find_ripple_device()
    if not dev:
        sys.exit(1)

    def on_notify(_, data: bytearray):
        msg = data.decode("utf-8", errors="replace").strip()
        print(f"[TELEMETRIA] {msg}")

    async with BleakClient(dev) as client:
        print(f"[*] Conectado! Iniciando stream de telemetria por {duration:.0f}s (Ctrl+C para sair)...")
        await client.start_notify(NUS_TX_CHAR_UUID, on_notify)
        await client.write_gatt_char(NUS_RX_CHAR_UUID, b"stream on\n", response=False)
        try:
            await asyncio.sleep(duration)
        except asyncio.CancelledError:
            pass
        finally:
            await client.write_gatt_char(NUS_RX_CHAR_UUID, b"stream off\n", response=False)
            await client.stop_notify(NUS_TX_CHAR_UUID)


async def cmd_logs(duration: float = 60.0):
    dev = await find_ripple_device()
    if not dev:
        sys.exit(1)

    def on_notify(_, data: bytearray):
        msg = data.decode("utf-8", errors="replace")
        sys.stdout.write(msg)
        sys.stdout.flush()

    async with BleakClient(dev) as client:
        print(f"[*] Conectado a {dev.address}! Assinando console de logs do ESP-IDF (Ctrl+C para sair)...")
        await client.start_notify(NUS_TX_CHAR_UUID, on_notify)
        await client.write_gatt_char(NUS_RX_CHAR_UUID, b"log on\n", response=False)
        try:
            await asyncio.sleep(duration)
        except asyncio.CancelledError:
            pass
        finally:
            print("\n[*] Desligando stream de logs...")
            try:
                await client.write_gatt_char(NUS_RX_CHAR_UUID, b"log off\n", response=False)
                await client.stop_notify(NUS_TX_CHAR_UUID)
            except Exception:
                pass


async def cmd_ota_info():
    dev = await find_ripple_device()
    if not dev:
        sys.exit(1)

    received_event = asyncio.Event()

    def on_notify(_, data: bytearray):
        msg = data.decode("utf-8", errors="replace").strip()
        try:
            info = json.loads(msg)
            print("\n========== INFORMACOES DE FIRMWARE E PARTICAO ==========")
            print(f"  Aplicacao:         {info.get('app')}")
            print(f"  Versao:            {info.get('ver')}")
            print(f"  Data de Build:     {info.get('build')}")
            print(f"  ESP-IDF:           {info.get('idf')}")
            print(f"  Particao Ativa:    {info.get('running')}")
            print(f"  Particao de Boot:  {info.get('boot')}")
            print(f"  Proximo Destino:   {info.get('next_ota')}")
            print("========================================================\n")
        except Exception:
            print(f"[RIPPLE] {msg}")
        received_event.set()

    async with BleakClient(dev) as client:
        await client.start_notify(NUS_TX_CHAR_UUID, on_notify)
        await client.write_gatt_char(NUS_RX_CHAR_UUID, b"ota info\n", response=False)
        try:
            await asyncio.wait_for(received_event.wait(), timeout=4.0)
        except asyncio.TimeoutError:
            print("[-] Timeout aguardando informacoes OTA.")
        await client.stop_notify(NUS_TX_CHAR_UUID)


async def cmd_ota(bin_path: str, chunk_size: int = 480):
    if not os.path.isfile(bin_path):
        print(f"[-] Erro: Arquivo de firmware '{bin_path}' nao encontrado!")
        sys.exit(1)

    with open(bin_path, "rb") as f:
        firmware_bytes = f.read()

    total_len = len(firmware_bytes)
    sha256 = hashlib.sha256(firmware_bytes).hexdigest()
    print(f"[*] Firmware: '{bin_path}'")
    print(f"[*] Tamanho:  {total_len} bytes ({total_len / 1024:.1f} KB)")
    print(f"[*] SHA256:   {sha256[:16]}...{sha256[-8:]}")

    dev = await find_ripple_device()
    if not dev:
        sys.exit(1)

    ready_event = asyncio.Event()
    finish_event = asyncio.Event()
    ota_error = None
    target_part = "ota_x"

    def on_ota_ctrl_notify(_, data: bytearray):
        nonlocal ota_error, target_part
        msg = data.decode("utf-8", errors="replace").strip()
        try:
            resp = json.loads(msg)
            st = resp.get("status")
            ev = resp.get("event")
            if st == "ready":
                ready_event.set()
            elif st == "success":
                finish_event.set()
            elif st == "error" or ev == "error":
                ota_error = resp.get("msg", "unknown_error")
                print(f"\n[-] Erro reportado pela boia: {ota_error}")
                ready_event.set()
                finish_event.set()
            elif ev == "progress":
                pass
        except Exception:
            pass

    async with BleakClient(dev) as client:
        print(f"[*] Conectado a {dev.address}! Inicializando sessao BLE OTA...")
        await client.start_notify(RIPPLE_OTA_CTRL_UUID, on_ota_ctrl_notify)

        # 1. Envia comando START com tamanho total
        start_cmd = f"start {total_len}".encode("utf-8")
        await client.write_gatt_char(RIPPLE_OTA_CTRL_UUID, start_cmd, response=True)

        try:
            await asyncio.wait_for(ready_event.wait(), timeout=6.0)
        except asyncio.TimeoutError:
            print("[-] Timeout aguardando preparacao da boia para OTA.")
            sys.exit(1)

        if ota_error:
            print(f"[-] Falha ao iniciar OTA: {ota_error}")
            sys.exit(1)

        print("[+] Boia pronta! Iniciando transmissao de pacotes...")
        start_time = time.time()
        sent_bytes = 0
        total_chunks = (total_len + chunk_size - 1) // chunk_size

        for i in range(0, total_len, chunk_size):
            if ota_error:
                break
            chunk = firmware_bytes[i:i + chunk_size]
            await client.write_gatt_char(RIPPLE_OTA_DATA_UUID, chunk, response=False)
            sent_bytes += len(chunk)

            # Barra de progresso interativa
            pct = (sent_bytes * 100) / total_len
            elapsed = time.time() - start_time
            speed_kb = (sent_bytes / 1024.0) / elapsed if elapsed > 0 else 0
            bar = ("#" * int(pct // 2.5)).ljust(40, "-")
            sys.stdout.write(f"\r  [{bar}] {pct:5.1f}% | {sent_bytes}/{total_len} B | {speed_kb:5.1f} KB/s")
            sys.stdout.flush()

            # Pequeno pacing a cada 16 pacotes para não afogar a fila de mbuf do NimBLE
            if ((i // chunk_size) % 16) == 0:
                await asyncio.sleep(0.008)

        print("")
        if ota_error:
            print("[-] Processo de transmissao interrompido devido a erro.")
            sys.exit(1)

        elapsed_xfer = time.time() - start_time
        print(f"[*] Todos os {total_len} bytes enviados em {elapsed_xfer:.1f}s ({total_len / 1024 / elapsed_xfer:.1f} KB/s)!")
        print("[*] Enviando comando FINISH e aguardando validacao da imagem na flash...")

        await client.write_gatt_char(RIPPLE_OTA_CTRL_UUID, b"finish", response=True)
        try:
            await asyncio.wait_for(finish_event.wait(), timeout=8.0)
            if not ota_error:
                print("\n[SUCCESS] ATUALIZACAO OTA CONCLUIDA COM SUCESSO!")
                print("[+] A boia Ripple foi atualizada e esta reiniciando na nova particao.")
                print("[+] Em instantes ela voltara a anunciar 'Ripple-Buoy' via BLE.")
        except asyncio.TimeoutError:
            print("\n[?] Boia reiniciou sem enviar confirmacao (normal para reboots rapidos).")

        try:
            await client.stop_notify(RIPPLE_OTA_CTRL_UUID)
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser(description="CLI & BLE OTA Tool para Boia Inteligente Ripple")
    sub = parser.add_subparsers(dest="action", required=True)

    p_scan = sub.add_parser("scan", help="Escaneia anuncios BLE da boia Ripple")
    p_scan.add_argument("-t", "--timeout", type=float, default=6.0, help="Tempo de scan (s)")

    p_cmd = sub.add_parser("cmd", help="Envia comando NUS (status, sens <val>, test_alert, rearm, calibrate, reboot, rollback)")
    p_cmd.add_argument("command", type=str, help="Comando a enviar")

    p_mon = sub.add_parser("monitor", help="Monitora telemetria de ondas em tempo real via BLE")
    p_mon.add_argument("-d", "--duration", type=float, default=30.0, help="Duracao do monitoramento (s)")

    p_logs = sub.add_parser("logs", help="Exibe os logs do sistema ESP-IDF em tempo real via BLE")
    p_logs.add_argument("-d", "--duration", type=float, default=60.0, help="Duracao da escuta de logs (s)")

    p_info = sub.add_parser("ota-info", help="Exibe particao ativa, boot partition e status de firmware")

    p_ota = sub.add_parser("ota", help="Atualiza o firmware da boia via BLE OTA seguro")
    p_ota.add_argument("file", type=str, help="Caminho para o arquivo binario (.bin)")
    p_ota.add_argument("-c", "--chunk", type=int, default=480, help="Tamanho do bloco de envio (bytes, padrao 480)")

    args = parser.parse_args()

    if args.action == "scan":
        asyncio.run(cmd_scan(args.timeout))
    elif args.action == "cmd":
        asyncio.run(cmd_send(args.command))
    elif args.action == "monitor":
        try:
            asyncio.run(cmd_monitor(args.duration))
        except KeyboardInterrupt:
            print("\n[*] Monitoramento encerrado.")
    elif args.action == "logs":
        try:
            asyncio.run(cmd_logs(args.duration))
        except KeyboardInterrupt:
            print("\n[*] Console de logs encerrado.")
    elif args.action == "ota-info":
        asyncio.run(cmd_ota_info())
    elif args.action == "ota":
        try:
            asyncio.run(cmd_ota(args.file, args.chunk))
        except KeyboardInterrupt:
            print("\n[*] Atualizacao OTA cancelada pelo usuario.")


if __name__ == "__main__":
    main()
