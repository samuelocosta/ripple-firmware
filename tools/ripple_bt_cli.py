"""
Ripple Smart Buoy — Bluetooth LE CLI & OTA Tool (Windows / Cross-platform via Bleak)

Interactive Mode (REPL):
  python tools/ripple_bt_cli.py

Direct Command Mode:
  python tools/ripple_bt_cli.py battery
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

# Nordic UART Service (NUS) UUIDs
NUS_SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
NUS_RX_CHAR_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
NUS_TX_CHAR_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"

# Dedicated Ripple BLE OTA Service UUIDs
RIPPLE_OTA_SVC_UUID  = "80140001-3850-424a-b5e0-9e67d266ff00"
RIPPLE_OTA_CTRL_UUID = "80140002-3850-424a-b5e0-9e67d266ff00"
RIPPLE_OTA_DATA_UUID = "80140003-3850-424a-b5e0-9e67d266ff00"

TARGET_NAMES = ("Ripple-Buoy", "RIPPLE: QUEDA NA PISCINA!", "RIPPLE:QUEDA!")


async def find_ripple_device(timeout: float = 6.0):
    print(f"[*] Searching for Ripple buoy via BLE ({timeout:.0f}s)...")
    devices = await BleakScanner.discover(timeout=timeout, return_adv=True)

    for addr, (dev, adv) in devices.items():
        name = dev.name or adv.local_name or ""
        uuids = [u.lower() for u in (adv.service_uuids or [])]
        if any(t.lower() in name.lower() for t in TARGET_NAMES) or NUS_SERVICE_UUID in uuids:
            print(f"[+] Buoy found: {name or 'Ripple'} [{dev.address}] (RSSI: {adv.rssi} dBm)")
            return dev

    print("[-] No Ripple buoy found. Make sure the board is powered on.")
    return None


async def cmd_scan(timeout: float = 6.0):
    print(f"[*] Scanning BLE advertisements for {timeout:.0f} seconds...")
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
            print(f"  -> Address: {dev.address} | Name: '{name}' | RSSI: {adv.rssi} dBm")
            if mfg:
                print(f"     Manufacturer Data: { {hex(k): v.hex() for k, v in mfg.items()} }")
            if svc_data:
                print(f"     Service Data:      { {k: v.hex() for k, v in svc_data.items()} }")

    if not found_any:
        print("[-] No Ripple buoy advertisements detected during scan.")


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
        print(f"[*] Connected to {dev.address}. Sending command: '{command_text}'")
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
        print(f"[TELEMETRY] {msg}")

    async with BleakClient(dev) as client:
        print(f"[*] Connected! Starting telemetry stream for {duration:.0f}s (Ctrl+C to stop)...")
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
        print(f"[*] Connected to {dev.address}! Subscribing to ESP-IDF log console (Ctrl+C to exit)...")
        await client.start_notify(NUS_TX_CHAR_UUID, on_notify)
        await client.write_gatt_char(NUS_RX_CHAR_UUID, b"log on\n", response=False)
        try:
            await asyncio.sleep(duration)
        except asyncio.CancelledError:
            pass
        finally:
            print("\n[*] Stopping log stream...")
            try:
                await client.write_gatt_char(NUS_RX_CHAR_UUID, b"log off\n", response=False)
                await client.stop_notify(NUS_TX_CHAR_UUID)
            except Exception:
                pass


async def cmd_battery():
    dev = await find_ripple_device()
    if not dev:
        sys.exit(1)

    received_event = asyncio.Event()

    def on_notify(_, data: bytearray):
        msg = data.decode("utf-8", errors="replace").strip()
        try:
            bat = json.loads(msg)
            v = bat.get("vbat", 0.0)
            pct = bat.get("pct", 0.0)
            min_v = bat.get("min_v", 3.2)
            low = bat.get("low", False)
            crit = bat.get("crit", False)
            sensor_ok = bat.get("sensor_ok", True)

            # Capacity progress bar
            filled = int(round(pct / 5.0))
            bar = "#" * filled + "-" * (20 - filled)
            if crit:
                status_str = "CRITICAL: Voltage <= Safe Minimum (Deep Sleep shutdown imminent!)"
            elif low:
                status_str = "WARNING: LOW BATTERY (<= 15%)"
            else:
                status_str = "NORMAL"

            print("\n========== RIPPLE BATTERY STATUS ==========")
            print(f"  Current Voltage:   {v:.3f} V")
            print(f"  Capacity:          [{bar}] {pct:5.1f}%")
            print(f"  Safe Minimum:      {min_v:.2f} V (0% Safe Reference / Deep Sleep)")
            print(f"  Theoretical Max:   4.20 V (100% Theoretical Reference)")
            print(f"  Alert State:       {status_str}")
            print(f"  Sensor Status:     {'OK' if sensor_ok else 'ERROR/DISCONNECTED'}")
            print("===========================================\n")
        except Exception:
            print(f"[RIPPLE -> PC] {msg}")
        received_event.set()

    async with BleakClient(dev) as client:
        await client.start_notify(NUS_TX_CHAR_UUID, on_notify)
        await client.write_gatt_char(NUS_RX_CHAR_UUID, b"battery\n", response=False)
        try:
            await asyncio.wait_for(received_event.wait(), timeout=4.0)
        except asyncio.TimeoutError:
            print("[-] Timeout waiting for battery reading from buoy.")
        await client.stop_notify(NUS_TX_CHAR_UUID)


async def cmd_set_bat_min(min_v: float):
    dev = await find_ripple_device()
    if not dev:
        sys.exit(1)

    received_event = asyncio.Event()

    def on_notify(_, data: bytearray):
        msg = data.decode("utf-8", errors="replace").strip()
        try:
            resp = json.loads(msg)
            if resp.get("status") == "ok":
                print(f"\n[+] Safe minimum voltage successfully updated to {resp.get('min_v'):.2f} V!")
                print(f"[+] Recalculated battery capacity: {resp.get('pct'):.1f}%\n")
            else:
                print(f"\n[-] Error reported by buoy: {resp.get('error', msg)}\n")
        except Exception:
            print(f"[RIPPLE] {msg}")
        received_event.set()

    async with BleakClient(dev) as client:
        await client.start_notify(NUS_TX_CHAR_UUID, on_notify)
        cmd_str = f"bat_min {min_v:.2f}\n"
        print(f"[*] Sending command: '{cmd_str.strip()}'...")
        await client.write_gatt_char(NUS_RX_CHAR_UUID, cmd_str.encode("utf-8"), response=False)
        try:
            await asyncio.wait_for(received_event.wait(), timeout=4.0)
        except asyncio.TimeoutError:
            print("[-] Timeout waiting for confirmation from buoy.")
        await client.stop_notify(NUS_TX_CHAR_UUID)


async def cmd_ota_info():
    dev = await find_ripple_device()
    if not dev:
        sys.exit(1)

    received_event = asyncio.Event()

    def on_notify(_, data: bytearray):
        msg = data.decode("utf-8", errors="replace").strip()
        try:
            info = json.loads(msg)
            print("\n========== FIRMWARE & PARTITION INFO ==========")
            print(f"  Application:       {info.get('app')}")
            print(f"  Version:           {info.get('ver')}")
            print(f"  Build Date:        {info.get('build')}")
            print(f"  ESP-IDF:           {info.get('idf')}")
            print(f"  Active Partition:  {info.get('running')}")
            print(f"  Boot Partition:    {info.get('boot')}")
            print(f"  Next OTA Target:   {info.get('next_ota')}")
            print("================================================\n")
        except Exception:
            print(f"[RIPPLE] {msg}")
        received_event.set()

    async with BleakClient(dev) as client:
        await client.start_notify(NUS_TX_CHAR_UUID, on_notify)
        await client.write_gatt_char(NUS_RX_CHAR_UUID, b"ota info\n", response=False)
        try:
            await asyncio.wait_for(received_event.wait(), timeout=4.0)
        except asyncio.TimeoutError:
            print("[-] Timeout waiting for OTA info.")
        await client.stop_notify(NUS_TX_CHAR_UUID)


async def cmd_ota(bin_path: str, chunk_size: int = 480):
    if not os.path.isfile(bin_path):
        print(f"[-] Error: Firmware file '{bin_path}' not found!")
        sys.exit(1)

    with open(bin_path, "rb") as f:
        firmware_bytes = f.read()

    total_len = len(firmware_bytes)
    sha256 = hashlib.sha256(firmware_bytes).hexdigest()
    print(f"[*] Firmware: '{bin_path}'")
    print(f"[*] Size:     {total_len} bytes ({total_len / 1024:.1f} KB)")
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
                print(f"\n[-] Error reported by buoy: {ota_error}")
                ready_event.set()
                finish_event.set()
            elif ev == "progress":
                pass
        except Exception:
            pass

    async with BleakClient(dev) as client:
        print(f"[*] Connected to {dev.address}! Initializing BLE OTA session...")
        await client.start_notify(RIPPLE_OTA_CTRL_UUID, on_ota_ctrl_notify)

        # 1. Send START command with total binary size
        start_cmd = f"start {total_len}".encode("utf-8")
        await client.write_gatt_char(RIPPLE_OTA_CTRL_UUID, start_cmd, response=True)

        try:
            await asyncio.wait_for(ready_event.wait(), timeout=6.0)
        except asyncio.TimeoutError:
            print("[-] Timeout waiting for buoy OTA preparation.")
            sys.exit(1)

        if ota_error:
            print(f"[-] Failed to start OTA: {ota_error}")
            sys.exit(1)

        print("[+] Buoy ready! Starting packet transmission...")
        start_time = time.time()
        sent_bytes = 0
        total_chunks = (total_len + chunk_size - 1) // chunk_size

        for i in range(0, total_len, chunk_size):
            if ota_error:
                break
            chunk = firmware_bytes[i:i + chunk_size]
            await client.write_gatt_char(RIPPLE_OTA_DATA_UUID, chunk, response=False)
            sent_bytes += len(chunk)

            # Interactive progress bar
            pct = (sent_bytes * 100) / total_len
            elapsed = time.time() - start_time
            speed_kb = (sent_bytes / 1024.0) / elapsed if elapsed > 0 else 0
            bar = ("#" * int(pct // 2.5)).ljust(40, "-")
            sys.stdout.write(f"\r  [{bar}] {pct:5.1f}% | {sent_bytes}/{total_len} B | {speed_kb:5.1f} KB/s")
            sys.stdout.flush()

            # Small pacing every 16 packets to prevent overwhelming NimBLE mbuf queue
            if ((i // chunk_size) % 16) == 0:
                await asyncio.sleep(0.008)

        print("")
        if ota_error:
            print("[-] OTA transfer aborted due to error.")
            sys.exit(1)

        elapsed_xfer = time.time() - start_time
        print(f"[*] All {total_len} bytes sent in {elapsed_xfer:.1f}s ({total_len / 1024 / elapsed_xfer:.1f} KB/s)!")
        print("[*] Sending FINISH command and awaiting flash validation...")

        await client.write_gatt_char(RIPPLE_OTA_CTRL_UUID, b"finish", response=True)
        try:
            await asyncio.wait_for(finish_event.wait(), timeout=8.0)
            if not ota_error:
                print("\n[SUCCESS] OTA UPDATE COMPLETED SUCCESSFULLY!")
                print("[+] The Ripple buoy has been updated and is rebooting into the new partition.")
                print("[+] It will resume advertising 'Ripple-Buoy' via BLE shortly.")
        except asyncio.TimeoutError:
            print("\n[?] Buoy rebooted without confirmation (normal for fast reboots).")

        try:
            await client.stop_notify(RIPPLE_OTA_CTRL_UUID)
        except Exception:
            pass


def print_interactive_help():
    print("""
Available commands in interactive mode (Ripple Shell):
  battery             - Show battery voltage, percentage, and status
  set-bat-min <volts> - Configure safe minimum voltage (e.g. set-bat-min 3.20)
  status              - Show full buoy status (IMU, waves, alerts, battery)
  sens <value>        - Adjust fall detection sensitivity threshold (e.g. sens 0.25)
  test_alert / alert  - Trigger simulated fall alert (LEDs + BLE pop-up)
  rearm / reset       - Rearm buoy back to green monitoring state
  calibrate / cal     - Start 3-second in-water wave baseline calibration
  sleep               - Put buoy into ultra-low-power Deep Sleep
  scan [seconds]      - Scan for BLE advertisements (default: 4s)
  monitor [seconds]   - Stream real-time telemetry at 5 Hz (default: 15s)
  logs [seconds]      - Stream serial console logs over BLE in real time (default: 30s)
  ota-info            - Show boot partitions and firmware details
  ota <file.bin>      - Flash new firmware (.bin) via high-speed BLE OTA
  reboot              - Reboot the buoy
  rollback            - Roll back to previous firmware version
  cmd <command>       - Send raw text command to Nordic UART Service
  help / ?            - Show this list of available commands
  exit / quit         - Exit the Ripple interactive shell
""")


def _prompt_user(prompt: str) -> str:
    try:
        if sys.stdin and sys.stdin.isatty():
            return input(prompt)
        # Pipe / redirected input mode
        if sys.stdin:
            line = sys.stdin.readline()
            if not line:
                return "exit"
            return line.strip()
        return "exit"
    except (EOFError, KeyboardInterrupt):
        return "exit"


async def interactive_shell():
    print("=================================================================")
    print("  RIPPLE SMART BUOY — Interactive Shell                          ")
    print("  Type 'help' to list commands or 'exit' to quit.                ")
    print("=================================================================\n")

    while True:
        try:
            line = await asyncio.to_thread(_prompt_user, "ripple> ")
        except (EOFError, KeyboardInterrupt):
            print("\nExiting interactive shell.")
            break

        line = line.strip()
        if not line:
            continue

        parts = line.split(maxsplit=1)
        verb = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if verb in ("exit", "quit", "q"):
            print("Exiting Ripple interactive shell. Goodbye!")
            break
        elif verb in ("help", "?"):
            print_interactive_help()
        elif verb in ("battery", "bat"):
            try:
                await cmd_battery()
            except Exception as e:
                print(f"[-] Error reading battery: {e}")
        elif verb in ("set-bat-min", "bat_min", "set_bat_min"):
            if not arg:
                print("[-] Usage: set-bat-min <volts> (e.g. set-bat-min 3.20)")
                continue
            try:
                val = float(arg)
                await cmd_set_bat_min(val)
            except ValueError:
                print("[-] Invalid voltage value. Example: set-bat-min 3.20")
            except Exception as e:
                print(f"[-] Error: {e}")
        elif verb == "status":
            try:
                await cmd_send("status")
            except Exception as e:
                print(f"[-] Error: {e}")
        elif verb in ("sens", "threshold"):
            if not arg:
                print("[-] Usage: sens <value> (e.g. sens 0.22)")
                continue
            try:
                await cmd_send(f"sens {arg}")
            except Exception as e:
                print(f"[-] Error: {e}")
        elif verb in ("test_alert", "alert"):
            try:
                await cmd_send("test_alert")
            except Exception as e:
                print(f"[-] Error: {e}")
        elif verb in ("rearm", "reset"):
            try:
                await cmd_send("rearm")
            except Exception as e:
                print(f"[-] Error: {e}")
        elif verb in ("calibrate", "cal"):
            try:
                await cmd_send("calibrate")
            except Exception as e:
                print(f"[-] Error: {e}")
        elif verb == "sleep":
            try:
                await cmd_send("sleep")
            except Exception as e:
                print(f"[-] Error: {e}")
        elif verb == "scan":
            timeout = 4.0
            if arg:
                try:
                    timeout = float(arg)
                except ValueError:
                    pass
            try:
                await cmd_scan(timeout)
            except Exception as e:
                print(f"[-] Error scanning: {e}")
        elif verb == "monitor":
            dur = 15.0
            if arg:
                try:
                    dur = float(arg)
                except ValueError:
                    pass
            try:
                await cmd_monitor(dur)
            except Exception as e:
                print(f"[-] Error: {e}")
        elif verb == "logs":
            dur = 30.0
            if arg:
                try:
                    dur = float(arg)
                except ValueError:
                    pass
            try:
                await cmd_logs(dur)
            except Exception as e:
                print(f"[-] Error: {e}")
        elif verb in ("ota-info", "otainfo"):
            try:
                await cmd_ota_info()
            except Exception as e:
                print(f"[-] Error: {e}")
        elif verb == "ota":
            if not arg:
                print("[-] Usage: ota <firmware_path> (e.g. ota build/ripple.bin)")
                continue
            try:
                await cmd_ota(arg)
            except Exception as e:
                print(f"[-] OTA update error: {e}")
        elif verb == "reboot":
            try:
                await cmd_send("reboot")
            except Exception as e:
                print(f"[-] Error: {e}")
        elif verb == "rollback":
            try:
                await cmd_send("rollback")
            except Exception as e:
                print(f"[-] Error: {e}")
        elif verb == "cmd":
            if not arg:
                print("[-] Usage: cmd <command> (e.g. cmd status)")
                continue
            try:
                await cmd_send(arg)
            except Exception as e:
                print(f"[-] Error: {e}")
        else:
            try:
                await cmd_send(line)
            except Exception as e:
                print(f"[-] Error: {e}")


def main():
    # Without arguments: enter interactive shell mode
    if len(sys.argv) == 1:
        try:
            asyncio.run(interactive_shell())
        except KeyboardInterrupt:
            print("\nTerminated.")
        return

    parser = argparse.ArgumentParser(
        description="Ripple Smart Buoy — Bluetooth LE CLI & OTA Tool",
        epilog="Tip: Run without arguments to enter interactive shell mode."
    )
    sub = parser.add_subparsers(dest="action", required=False)

    # Subcommands: interactive / shell
    sub.add_parser("interactive", help="Start interactive command shell")
    sub.add_parser("shell", help="Start interactive command shell")

    # Subcommand: help
    sub.add_parser("help", help="Show this help message with all available commands")

    # Subcommand: scan
    p_scan = sub.add_parser("scan", help="Scan for Ripple BLE advertisements within Bluetooth range")
    p_scan.add_argument("-t", "--timeout", type=float, default=6.0, help="Scan timeout in seconds (default: 6s)")

    # Subcommand: cmd
    p_cmd = sub.add_parser("cmd", help="Send remote NUS command (status, battery, bat_min <v>, sleep, sens <v>, test_alert, rearm, calibrate, reboot, rollback)")
    p_cmd.add_argument("command", type=str, help="Command to send (e.g. 'status', 'battery', 'sleep', 'sens 0.25', 'rearm')")

    # Subcommand: monitor
    p_mon = sub.add_parser("monitor", help="Monitor wave telemetry and state in real time via BLE")
    p_mon.add_argument("-d", "--duration", type=float, default=30.0, help="Monitoring duration in seconds (default: 30s)")

    # Subcommand: logs
    p_logs = sub.add_parser("logs", help="Stream ESP-IDF serial logs in real time via BLE")
    p_logs.add_argument("-d", "--duration", type=float, default=60.0, help="Log capture duration in seconds (default: 60s)")

    # Subcommand: ota-info
    sub.add_parser("ota-info", help="Show active partition, boot partition, and firmware status")

    # Subcommand: battery
    sub.add_parser("battery", help="Check buoy battery voltage, percentage, and protection status via BLE")

    # Subcommand: set-bat-min
    p_set_bat = sub.add_parser("set-bat-min", help="Set safe minimum voltage (0%% charge / deep sleep cutoff) and store to NVS")
    p_set_bat.add_argument("volts", type=float, help="Minimum voltage in Volts (e.g. 3.20)")

    # Subcommand: ota
    p_ota = sub.add_parser("ota", help="Flash buoy firmware via high-speed secure BLE OTA")
    p_ota.add_argument("file", type=str, help="Path to firmware binary (.bin)")
    p_ota.add_argument("-c", "--chunk", type=int, default=480, help="Transfer chunk size in bytes (default: 480)")

    args = parser.parse_args()

    if args.action in ("interactive", "shell"):
        try:
            asyncio.run(interactive_shell())
        except KeyboardInterrupt:
            print("\nTerminated.")
    elif args.action == "help" or not args.action:
        parser.print_help()
        sys.exit(0)
    elif args.action == "scan":
        asyncio.run(cmd_scan(args.timeout))
    elif args.action == "cmd":
        asyncio.run(cmd_send(args.command))
    elif args.action == "monitor":
        try:
            asyncio.run(cmd_monitor(args.duration))
        except KeyboardInterrupt:
            print("\n[*] Monitoring stopped.")
    elif args.action == "logs":
        try:
            asyncio.run(cmd_logs(args.duration))
        except KeyboardInterrupt:
            print("\n[*] Log streaming stopped.")
    elif args.action == "ota-info":
        asyncio.run(cmd_ota_info())
    elif args.action == "battery":
        asyncio.run(cmd_battery())
    elif args.action == "set-bat-min":
        asyncio.run(cmd_set_bat_min(args.volts))
    elif args.action == "ota":
        try:
            asyncio.run(cmd_ota(args.file, args.chunk))
        except KeyboardInterrupt:
            print("\n[*] OTA update cancelled by user.")


if __name__ == "__main__":
    main()
