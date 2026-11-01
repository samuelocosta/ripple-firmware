"""
Ripple Smart Buoy — Bluetooth LE CLI & OTA Tool (Windows / Cross-platform via Bleak)

Interactive Mode (REPL):
  python tools/ripple_bt_cli.py

Direct Command Mode:
  python tools/ripple_bt_cli.py scan
  python tools/ripple_bt_cli.py connect [address|index]
  python tools/ripple_bt_cli.py dashboard [-r 50] [-w 30]
  python tools/ripple_bt_cli.py battery
  python tools/ripple_bt_cli.py status
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
from collections import deque, namedtuple
import datetime
import hashlib
import json
import math
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from bleak import BleakClient, BleakScanner

try:
    import msvcrt
except ImportError:
    msvcrt = None

try:
    import select
except ImportError:
    select = None

try:
    from tools.ripple_tui import RippleTuiDashboard, classify_packet
except ImportError:
    from ripple_tui import RippleTuiDashboard, classify_packet

# Ensure stdout handles UTF-8 for box-drawing and graph characters
if hasattr(sys.stdout, "reconfigure") and sys.stdout.encoding:
    if sys.stdout.encoding.lower() != "utf-8":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def enable_windows_vt():
    """Enable ANSI virtual terminal processing on Windows console."""
    if sys.platform == "win32":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            hStdOut = kernel32.GetStdHandle(-11)
            mode = ctypes.c_ulong()
            kernel32.GetConsoleMode(hStdOut, ctypes.byref(mode))
            mode.value |= 0x0004  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
            kernel32.SetConsoleMode(hStdOut, mode)
        except Exception:
            pass


# Nordic UART Service (NUS) UUIDs
NUS_SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
NUS_RX_CHAR_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
NUS_TX_CHAR_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"

# Dedicated Ripple BLE OTA Service UUIDs
RIPPLE_OTA_SVC_UUID  = "80140001-3850-424a-b5e0-9e67d266ff00"
RIPPLE_OTA_CTRL_UUID = "80140002-3850-424a-b5e0-9e67d266ff00"
RIPPLE_OTA_DATA_UUID = "80140003-3850-424a-b5e0-9e67d266ff00"

TARGET_NAMES = ("Ripple-Buoy", "RIPPLE: QUEDA NA PISCINA!", "RIPPLE:QUEDA!")

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences to compute visual character length."""
    return ANSI_RE.sub("", text)


def make_border(left: str, styled_title: str, right: str, width: int) -> str:
    """Format a border line like '┌─ TITLE ───┐' with visual width exactly equal to width."""
    prefix = f"{left} {styled_title} " if styled_title else left
    plain_len = len(strip_ansi(prefix)) + len(strip_ansi(right))
    fill = max(0, width - plain_len)
    return prefix + ("─" * fill) + right


def truncate_ansi(text: str, max_len: int) -> str:
    """Truncate text to at most max_len visible characters while preserving ANSI escape sequences."""
    if len(strip_ansi(text)) <= max_len:
        return text
    result = []
    visible_count = 0
    in_ansi = False
    ansi_buf = []

    for char in text:
        if char == "\x1b":
            in_ansi = True
            ansi_buf.append(char)
        elif in_ansi:
            ansi_buf.append(char)
            if char.isalpha():
                in_ansi = False
                result.append("".join(ansi_buf))
                ansi_buf = []
        else:
            if visible_count < max_len:
                result.append(char)
                visible_count += 1
            else:
                break
    result.append("\033[0m")
    return "".join(result)


def _prompt_user(prompt: str) -> str:
    try:
        if sys.stdin and sys.stdin.isatty():
            return input(prompt)
        if sys.stdin:
            line = sys.stdin.readline()
            if not line:
                return "exit"
            return line.strip()
        return "exit"
    except (EOFError, KeyboardInterrupt):
        return "exit"


# =============================================================================
# CONNECTION MANAGER (Persistent Connection & Auto-Discovery)
# =============================================================================

class RippleConnectionManager:
    """Manages discovery, active connections, and session persistence for Ripple buoys."""

    def __init__(self, cache_file: str = ".ripple_target"):
        try:
            script_dir = os.path.dirname(os.path.abspath(__file__))
            self.cache_file = os.path.join(script_dir, cache_file)
        except Exception:
            self.cache_file = os.path.join(os.path.expanduser("~"), cache_file)

        self.client: BleakClient | None = None
        self.connected_device = None
        self.last_scanned = []  # list of (BLEDevice, AdvertisementData)
        self.override_target: str | None = None
        self.cached_address = self._load_cached_target()

    def set_override_target(self, target: str | None):
        """Set explicit MAC address or device name target to bypass discovery."""
        if target and str(target).strip():
            self.override_target = str(target).strip()
        else:
            self.override_target = None

    def _load_cached_target(self) -> str | None:
        try:
            if os.path.exists(self.cache_file):
                with open(self.cache_file, "r", encoding="utf-8") as f:
                    addr = f.read().strip()
                    return addr if addr else None
        except Exception:
            pass
        return None

    def _save_cached_target(self, address: str):
        try:
            with open(self.cache_file, "w", encoding="utf-8") as f:
                f.write(address.strip())
            self.cached_address = address.strip()
        except Exception:
            pass

    def is_connected(self) -> bool:
        return self.client is not None and self.client.is_connected

    def get_connected_address(self) -> str | None:
        if self.is_connected() and self.connected_device:
            return getattr(self.connected_device, "address", str(self.connected_device))
        return self.override_target or self.cached_address

    async def scan(self, timeout: float = 4.0, show: bool = True) -> list:
        if show:
            print(f"[*] Scanning for Ripple buoys via Bluetooth LE ({timeout:.1f}s)...")
        try:
            devices_dict = await BleakScanner.discover(timeout=timeout, return_adv=True)
        except Exception as e:
            if show:
                print(f"[-] Bluetooth scan error: {e}")
            return []

        results = []
        for addr, (dev, adv) in devices_dict.items():
            name = dev.name or adv.local_name or ""
            uuids = [u.lower() for u in (adv.service_uuids or [])]
            if (
                any(t.lower() in name.lower() for t in TARGET_NAMES)
                or NUS_SERVICE_UUID in uuids
                or "ripple" in name.lower()
            ):
                results.append((dev, adv))

        self.last_scanned = results
        if show:
            if not results:
                print("[-] No Ripple buoys detected during scan.")
                print("[-] Ensure the buoy is powered on, within range, and advertising.")
            else:
                print(f"[+] Found {len(results)} Ripple buoy(s) in range:")
                for idx, (dev, adv) in enumerate(results, 1):
                    name = dev.name or adv.local_name or "Ripple-Buoy"
                    is_active = self.is_connected() and self.connected_device and getattr(self.connected_device, "address", "") == dev.address
                    active_marker = " \033[1;32m(CONNECTED)\033[0m" if is_active else ""
                    print(f"  [{idx}] {name} [{dev.address}] (RSSI: {adv.rssi} dBm){active_marker}")
        return results

    async def connect(self, target=None) -> bool:
        """Connect to a buoy by index (1-based from last scan), MAC/UUID address, or device."""
        if not target and self.override_target:
            target = self.override_target

        dev_to_connect = None

        if isinstance(target, int) or (isinstance(target, str) and target.strip().isdigit()):
            idx = int(target) - 1
            if 0 <= idx < len(self.last_scanned):
                dev_to_connect = self.last_scanned[idx][0]
            else:
                print(f"[-] Invalid device index {target}. Run 'scan' first to view available buoys.")
                return False
        elif isinstance(target, str) and target.strip():
            target_str = target.strip()
            for dev, _ in self.last_scanned:
                if dev.address.lower() == target_str.lower() or (dev.name and dev.name.lower() == target_str.lower()):
                    dev_to_connect = dev
                    break
            if not dev_to_connect:
                # Active scan to warm BLE cache on Windows
                try:
                    found = await BleakScanner.find_device_by_address(target_str, timeout=4.0)
                    if found:
                        dev_to_connect = found
                    else:
                        dev_to_connect = target_str
                except Exception:
                    dev_to_connect = target_str
        elif hasattr(target, "address"):
            dev_to_connect = target
        elif self.cached_address:
            dev_to_connect = self.cached_address
        else:
            print("[-] No target specified. Specify with --mac <address> or run 'scan' and 'connect <index|address>'.")
            return False

        addr_str = getattr(dev_to_connect, "address", str(dev_to_connect))
        if self.is_connected() and self.connected_device and getattr(self.connected_device, "address", "").lower() == addr_str.lower():
            print(f"[*] Already connected to [{addr_str}].")
            return True

        await self.disconnect(silent=True)

        target_name = getattr(dev_to_connect, "name", "Ripple-Buoy") or "Ripple-Buoy"
        print(f"[*] Connecting to {target_name} [{addr_str}]...")

        try:
            client = BleakClient(dev_to_connect)
            await client.connect(timeout=10.0)
            if client.is_connected:
                self.client = client
                self.connected_device = dev_to_connect if hasattr(dev_to_connect, "address") else client
                self._save_cached_target(addr_str)
                print(f"[+] Connected successfully to {target_name} [{addr_str}]!")
                return True
            else:
                print(f"[-] Failed to establish connection to [{addr_str}].")
                return False
        except Exception as e:
            print(f"[-] Connection failed: {e}")
            print("[-] Suggestion: Ensure the buoy is nearby and powered on. Run 'scan' and 'connect'.")
            return False

    async def disconnect(self, silent: bool = False):
        if self.client:
            try:
                if self.client.is_connected:
                    await self.client.disconnect()
            except Exception:
                pass
            self.client = None
            self.connected_device = None
            if not silent:
                print("[*] Disconnected from Ripple buoy.")

    async def send_fast(self, cmd_text: str):
        """Send asynchronous GATT write without waiting for a reply."""
        if self.is_connected() and self.client:
            try:
                await self.client.write_gatt_char(
                    NUS_RX_CHAR_UUID, (cmd_text.strip() + "\n").encode("utf-8"), response=False
                )
            except Exception as e:
                pass

    async def ensure_connected(self, prompt_if_multiple: bool = True, target: str | None = None) -> BleakClient | None:
        """Ensure an active connection exists. If not, auto-discovers and connects."""
        active_target = target or self.override_target

        if active_target:
            addr_str = self.get_connected_address() or ""
            if self.is_connected() and addr_str.lower() == str(active_target).strip().lower():
                return self.client
            print(f"[*] Targeting buoy [{active_target}] directly (bypassing auto-discovery)...")
            ok = await self.connect(active_target)
            return self.client if ok else None

        if self.is_connected():
            return self.client

        # Attempt to reconnect to previously cached target address
        if self.cached_address:
            print(f"[*] Connecting to previously selected buoy [{self.cached_address}]...")
            ok = await self.connect(self.cached_address)
            if ok:
                return self.client
            print(f"[-] Previously selected buoy [{self.cached_address}] not reachable. Scanning...")

        # Discover available buoys
        devices = await self.scan(timeout=4.0, show=False)
        if not devices:
            print("[-] Error: No Ripple buoy found in Bluetooth range.")
            print("[-] Suggestion: Power on the buoy and run 'scan' followed by 'connect'.")
            return None

        if len(devices) == 1:
            dev, adv = devices[0]
            name = dev.name or adv.local_name or "Ripple-Buoy"
            print(f"[+] Found single Ripple buoy: {name} [{dev.address}] (RSSI: {adv.rssi} dBm). Auto-connecting...")
            ok = await self.connect(dev)
            return self.client if ok else None

        # Multiple buoys detected
        print("\n[?] Multiple Ripple buoys detected in range:")
        for idx, (dev, adv) in enumerate(devices, 1):
            name = dev.name or adv.local_name or "Ripple-Buoy"
            print(f"  [{idx}] {name} [{dev.address}] (RSSI: {adv.rssi} dBm)")

        is_tty = bool(sys.stdin and sys.stdin.isatty())
        if not is_tty or not prompt_if_multiple:
            print("[-] Non-interactive execution: multiple buoys detected in range.")
            print("[-] Please specify the target buoy using '--mac <address>' or '-m <address>'.")
            print(f"    Example: python tools/ripple_bt_cli.py --mac {devices[0][0].address} battery")
            return None

        choice = await asyncio.to_thread(_prompt_user, f"Select buoy to connect [1-{len(devices)}] (or 'c' to cancel): ")
        choice = choice.strip()
        if choice.lower() in ("c", "cancel", "q", "exit", ""):
            print("[-] Connection cancelled.")
            return None

        if choice.isdigit():
            idx = int(choice)
            if 1 <= idx <= len(devices):
                ok = await self.connect(devices[idx - 1][0])
                return self.client if ok else None

        print("[-] Invalid choice.")
        return None

    def handle_comm_error(self, err: Exception):
        addr = self.get_connected_address() or "unknown"
        print(f"[-] Error: Communication with buoy [{addr}] failed ({err}).")
        print("[-] Suggestion: The buoy may have disconnected or is out of range. Run 'scan' and 'connect'.")
        self.client = None
        self.connected_device = None


# Global connection manager instance
g_conn_mgr = RippleConnectionManager()


# =============================================================================
# BASIC COMMANDS VIA CONNECTION MANAGER
# =============================================================================

async def cmd_scan(timeout: float = 4.0):
    await g_conn_mgr.scan(timeout=timeout, show=True)


async def cmd_connect(target=None):
    if not target:
        devices = await g_conn_mgr.scan(timeout=4.0, show=True)
        if not devices:
            return
        choice = await asyncio.to_thread(_prompt_user, f"Select buoy to connect [1-{len(devices)}] (or 'c' to cancel): ")
        if choice.strip().isdigit():
            await g_conn_mgr.connect(choice.strip())
        return
    await g_conn_mgr.connect(target)


async def cmd_disconnect():
    await g_conn_mgr.disconnect()


async def cmd_send(command_text: str):
    client = await g_conn_mgr.ensure_connected()
    if not client:
        return

    received_event = asyncio.Event()
    handled = False
    cmd_normalized = command_text.strip().lower()

    def on_notify(_, data: bytearray):
        nonlocal handled
        if handled:
            return

        kind, payload = classify_packet(data)
        if kind == "empty":
            return
        if kind == "log":
            # Real-time ESP-IDF log streamed over BLE: print to console without closing command wait
            print(f"[RIPPLE LOG] {payload}")
            return

        # Structured JSON packet:
        # Ignore unprompted periodic telemetry packets if waiting for a non-status command
        if (
            cmd_normalized != "status"
            and not cmd_normalized.startswith("stream")
            and isinstance(payload, dict)
            and "wave" in payload
            and "event" not in payload
            and "status" not in payload
            and "vbat" not in payload
            and "cmds" not in payload
        ):
            return

        handled = True
        msg_out = json.dumps(payload) if isinstance(payload, dict) else payload
        print(f"[RIPPLE -> PC] {msg_out}")
        received_event.set()

    try:
        await client.start_notify(NUS_TX_CHAR_UUID, on_notify)
        await client.write_gatt_char(NUS_RX_CHAR_UUID, (command_text + "\n").encode("utf-8"), response=False)
        try:
            await asyncio.wait_for(received_event.wait(), timeout=4.0)
        except asyncio.TimeoutError:
            if not handled:
                try:
                    raw = await client.read_gatt_char(NUS_TX_CHAR_UUID)
                    raw_str = raw.decode("utf-8", errors="replace").strip()
                    if raw_str:
                        print(f"[RIPPLE Read] {raw_str}")
                except Exception:
                    pass
        await client.stop_notify(NUS_TX_CHAR_UUID)
    except Exception as e:
        g_conn_mgr.handle_comm_error(e)


async def cmd_monitor(duration: float = 30.0):
    client = await g_conn_mgr.ensure_connected()
    if not client:
        return

    def on_notify(_, data: bytearray):
        msg = data.decode("utf-8", errors="replace").strip()
        print(f"[TELEMETRY] {msg}")

    try:
        print(f"[*] Starting telemetry stream for {duration:.0f}s (Ctrl+C to stop)...")
        await client.start_notify(NUS_TX_CHAR_UUID, on_notify)
        await client.write_gatt_char(NUS_RX_CHAR_UUID, b"stream on\n", response=False)
        try:
            await asyncio.sleep(duration)
        except asyncio.CancelledError:
            pass
        finally:
            try:
                await client.write_gatt_char(NUS_RX_CHAR_UUID, b"stream off\n", response=False)
                await client.stop_notify(NUS_TX_CHAR_UUID)
            except Exception:
                pass
    except Exception as e:
        g_conn_mgr.handle_comm_error(e)


async def cmd_battery():
    client = await g_conn_mgr.ensure_connected()
    if not client:
        return

    received_event = asyncio.Event()
    handled = False

    def on_notify(_, data: bytearray):
        nonlocal handled
        if handled:
            return
        msg = data.decode("utf-8", errors="replace").strip()
        try:
            bat = json.loads(msg)
            # Ensure it is battery response with min_v or crit or sensor_ok
            if "vbat" in bat and ("min_v" in bat or "crit" in bat):
                handled = True
                v = bat.get("vbat", 0.0)
                pct = bat.get("pct", 0.0)
                min_v = bat.get("min_v", 3.20)
                low = bat.get("low", False)
                crit = bat.get("crit", False)
                sensor_ok = bat.get("sensor_ok", True)

                filled = int(round(pct / 5.0))
                bar = "#" * filled + "-" * max(0, 20 - filled)
                if crit:
                    status_str = "CRITICAL: Voltage <= Safe Cutoff (Deep Sleep protection active!)"
                elif low:
                    status_str = "WARNING: LOW BATTERY (<= 15%)"
                else:
                    status_str = "NORMAL"

                print("\n========== RIPPLE BATTERY STATUS ==========")
                print(f"  Current Voltage:   {v:.3f} V")
                print(f"  Capacity:          [{bar}] {pct:5.1f}%")
                print(f"  Safe Minimum:      {min_v:.2f} V (0% Safe Cutoff / Deep Sleep)")
                print(f"  Theoretical Max:   4.20 V (100% Charge Reference)")
                print(f"  Alert State:       {status_str}")
                print(f"  Sensor Hardware:   {'OK (ADC1 Ch3)' if sensor_ok else 'ERROR/DISCONNECTED'}")
                print("===========================================\n")
                received_event.set()
        except Exception:
            pass

    try:
        await client.start_notify(NUS_TX_CHAR_UUID, on_notify)
        await client.write_gatt_char(NUS_RX_CHAR_UUID, b"battery\n", response=False)
        try:
            await asyncio.wait_for(received_event.wait(), timeout=4.0)
        except asyncio.TimeoutError:
            if not handled:
                print("[-] Timeout waiting for battery reading from buoy.")
        await client.stop_notify(NUS_TX_CHAR_UUID)
    except Exception as e:
        g_conn_mgr.handle_comm_error(e)


async def cmd_set_bat_min(min_v: float):
    client = await g_conn_mgr.ensure_connected()
    if not client:
        return

    received_event = asyncio.Event()
    handled = False

    def on_notify(_, data: bytearray):
        nonlocal handled
        if handled:
            return
        msg = data.decode("utf-8", errors="replace").strip()
        try:
            resp = json.loads(msg)
            if resp.get("status") == "ok":
                handled = True
                print(f"\n[+] Safe minimum voltage successfully updated to {resp.get('min_v'):.2f} V!")
                print(f"[+] Recalculated battery capacity: {resp.get('pct'):.1f}%\n")
                received_event.set()
            elif "error" in resp:
                handled = True
                print(f"\n[-] Error reported by buoy: {resp.get('error', msg)}\n")
                received_event.set()
        except Exception:
            pass

    try:
        await client.start_notify(NUS_TX_CHAR_UUID, on_notify)
        cmd_str = f"bat_min {min_v:.2f}\n"
        print(f"[*] Sending command: '{cmd_str.strip()}'...")
        await client.write_gatt_char(NUS_RX_CHAR_UUID, cmd_str.encode("utf-8"), response=False)
        try:
            await asyncio.wait_for(received_event.wait(), timeout=4.0)
        except asyncio.TimeoutError:
            if not handled:
                print("[-] Timeout waiting for confirmation from buoy.")
        await client.stop_notify(NUS_TX_CHAR_UUID)
    except Exception as e:
        g_conn_mgr.handle_comm_error(e)


async def cmd_logs(duration: float = 60.0):
    client = await g_conn_mgr.ensure_connected()
    if not client:
        return

    def on_notify(_, data: bytearray):
        msg = data.decode("utf-8", errors="replace")
        sys.stdout.write(msg)
        sys.stdout.flush()

    try:
        print(f"[*] Subscribing to ESP-IDF serial log console for {duration:.0f}s (Ctrl+C to exit)...")
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
    except Exception as e:
        g_conn_mgr.handle_comm_error(e)


async def cmd_ota_info():
    client = await g_conn_mgr.ensure_connected()
    if not client:
        return

    received_event = asyncio.Event()
    handled = False

    def on_notify(_, data: bytearray):
        nonlocal handled
        if handled:
            return
        msg = data.decode("utf-8", errors="replace").strip()
        try:
            info = json.loads(msg)
            # Differentiate from general telemetry stream by checking OTA-specific fields
            if "app" in info or "running" in info or "next_ota" in info:
                handled = True
                print("\n========== FIRMWARE & PARTITION INFO ==========")
                print(f"  Application:       {info.get('app')}")
                print(f"  Version:           {info.get('ver')}")
                print(f"  Build Date:        {info.get('build')}")
                print(f"  ESP-IDF:           {info.get('idf')}")
                print(f"  Active Partition:  {info.get('running')}")
                print(f"  Boot Partition:    {info.get('boot')}")
                print(f"  Next OTA Target:   {info.get('next_ota')}")
                print("================================================\n")
                received_event.set()
        except Exception:
            pass

    try:
        await client.start_notify(NUS_TX_CHAR_UUID, on_notify)
        await client.write_gatt_char(NUS_RX_CHAR_UUID, b"ota info\n", response=False)
        try:
            await asyncio.wait_for(received_event.wait(), timeout=4.0)
        except asyncio.TimeoutError:
            if not handled:
                print("[-] Timeout waiting for OTA info.")
        await client.stop_notify(NUS_TX_CHAR_UUID)
    except Exception as e:
        g_conn_mgr.handle_comm_error(e)


async def cmd_ota(bin_path: str, chunk_size: int = 480):
    if not os.path.isfile(bin_path):
        print(f"[-] Error: Firmware file '{bin_path}' not found!")
        return

    with open(bin_path, "rb") as f:
        firmware_bytes = f.read()

    total_len = len(firmware_bytes)
    sha256 = hashlib.sha256(firmware_bytes).hexdigest()
    print(f"[*] Firmware: '{bin_path}'")
    print(f"[*] Size:     {total_len} bytes ({total_len / 1024:.1f} KB)")
    print(f"[*] SHA256:   {sha256[:16]}...{sha256[-8:]}")

    client = await g_conn_mgr.ensure_connected()
    if not client:
        return

    ready_event = asyncio.Event()
    finish_event = asyncio.Event()
    ota_error = None

    def on_ota_ctrl_notify(_, data: bytearray):
        nonlocal ota_error
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
        except Exception:
            pass

    try:
        print("[*] Initializing high-speed BLE OTA session...")
        await client.start_notify(RIPPLE_OTA_CTRL_UUID, on_ota_ctrl_notify)

        start_cmd = f"start {total_len}".encode("utf-8")
        await client.write_gatt_char(RIPPLE_OTA_CTRL_UUID, start_cmd, response=True)

        try:
            await asyncio.wait_for(ready_event.wait(), timeout=6.0)
        except asyncio.TimeoutError:
            print("[-] Timeout waiting for buoy OTA preparation.")
            return

        if ota_error:
            print(f"[-] Failed to start OTA: {ota_error}")
            return

        print("[+] Buoy ready! Starting packet transmission...")
        start_time = time.time()
        sent_bytes = 0

        for i in range(0, total_len, chunk_size):
            if ota_error:
                break
            chunk = firmware_bytes[i:i + chunk_size]
            await client.write_gatt_char(RIPPLE_OTA_DATA_UUID, chunk, response=False)
            sent_bytes += len(chunk)

            pct = (sent_bytes * 100) / total_len
            elapsed = time.time() - start_time
            speed_kb = (sent_bytes / 1024.0) / elapsed if elapsed > 0 else 0
            bar = ("#" * int(pct // 2.5)).ljust(40, "-")
            sys.stdout.write(f"\r  [{bar}] {pct:5.1f}% | {sent_bytes}/{total_len} B | {speed_kb:5.1f} KB/s")
            sys.stdout.flush()

            if ((i // chunk_size) % 16) == 0:
                await asyncio.sleep(0.008)

        print("")
        if ota_error:
            print("[-] OTA transfer aborted due to error.")
            return

        elapsed_xfer = time.time() - start_time
        print(f"[*] All {total_len} bytes sent in {elapsed_xfer:.1f}s ({total_len / 1024 / elapsed_xfer:.1f} KB/s)!")
        print("[*] Sending FINISH command and awaiting flash validation...")

        await client.write_gatt_char(RIPPLE_OTA_CTRL_UUID, b"finish", response=True)
        try:
            await asyncio.wait_for(finish_event.wait(), timeout=8.0)
            if not ota_error:
                print("\n[SUCCESS] OTA UPDATE COMPLETED SUCCESSFULLY!")
                print("[+] The Ripple buoy has been updated and is rebooting into the new partition.")
                print("[+] Re-run 'connect' in a few seconds once it finishes booting.")
        except asyncio.TimeoutError:
            print("\n[?] Buoy rebooted without confirmation (normal for fast reboots).")

        try:
            await client.stop_notify(RIPPLE_OTA_CTRL_UUID)
        except Exception:
            pass

        # Disconnect cleanly since buoy is rebooting
        await g_conn_mgr.disconnect(silent=True)
    except Exception as e:
        g_conn_mgr.handle_comm_error(e)


# =============================================================================
# RIPPLE TELEMETRY DASHBOARD (TUI - Top / Btop Oscilloscope Style)
# =============================================================================

async def cmd_dashboard(rate: int = 50, target: str | None = None):
    """Launch the native terminal telemetry dashboard (top/btop style)."""
    print("[*] Connecting to Ripple buoy for TUI Dashboard session...")
    client = await g_conn_mgr.ensure_connected(target=target)
    if not client:
        print("[-] Could not connect to Ripple buoy. Dashboard launch aborted.")
        return

    dev_name = getattr(g_conn_mgr.connected_device, "name", "Ripple-Buoy") or "Ripple-Buoy"
    dev_addr = g_conn_mgr.get_connected_address() or target or "Unknown"

    dashboard = RippleTuiDashboard(
        dev_name=dev_name,
        dev_addr=dev_addr,
        send_cmd_fn=g_conn_mgr.send_fast,
    )

    def on_notify(_, data: bytearray):
        dashboard.push_raw_ble_packet(data)

    try:
        await client.start_notify(NUS_TX_CHAR_UUID, on_notify)
        # Request stream at rate (default 50 Hz)
        await g_conn_mgr.send_fast(f"stream {rate}")
        # Run TUI loop until user quits with 'q' or 'Esc'
        await dashboard.run_async()
    finally:
        try:
            await g_conn_mgr.send_fast("stream off")
        except Exception:
            pass
        try:
            await client.stop_notify(NUS_TX_CHAR_UUID)
        except Exception:
            pass
        print(f"[*] Exited Telemetry Dashboard [{dev_name}].")


# =============================================================================
# INTERACTIVE REPL SHELL
# =============================================================================

def print_interactive_help():
    print("""
Available commands in interactive mode (Ripple Shell):
  scan [seconds]      - Scan for nearby Ripple buoys and list them (default: 4s)
  connect [index|mac] - Connect to a specific buoy and maintain active session
  disconnect          - Disconnect from currently connected buoy
  dashboard / dash    - Open native terminal telemetry dashboard (top/btop style, 50 Hz)
  battery             - Show battery voltage, capacity %, and health status
  set-bat-min <volts> - Configure safe cutoff voltage (e.g. set-bat-min 3.20)
  status              - Show full buoy state (IMU, wave detector, alerts, battery)
  sens <value>        - Adjust fall detection sensitivity threshold (e.g. sens 0.22)
  test_alert / alert  - Trigger simulated fall alert (LEDs + emergency BLE pop-up)
  rearm / reset       - Rearm buoy back to safe monitoring state
  calibrate / cal     - Start in-water calm baseline calibration
  sleep               - Put buoy into ultra-low-power Deep Sleep
  log on / log off    - Enable or disable real-time serial log streaming over BLE
  monitor [seconds]   - Stream real-time telemetry at 5 Hz (default: 15s)
  logs [seconds]      - Stream serial console logs over BLE in real time (default: 30s)
  ota-info            - Show boot partitions and firmware details
  ota <file.bin>      - Flash new firmware (.bin) via high-speed BLE OTA
  reboot              - Reboot the buoy
  rollback            - Roll back to previous firmware version
  clear / cls         - Clear the terminal screen
  cmd <command>       - Send raw text command to Nordic UART Service
  help / ?            - Show this list of available commands
  exit / quit         - Exit the Ripple interactive shell
""")


async def interactive_shell():
    global g_async_loop
    g_async_loop = asyncio.get_running_loop()

    print("=================================================================")
    print("  RIPPLE SMART BUOY — Interactive Shell                          ")
    print("  Type 'help' to list commands or 'exit' to quit.                ")
    print("=================================================================\n")

    if g_conn_mgr.override_target and not g_conn_mgr.is_connected():
        print(f"[*] Targeting specified buoy [{g_conn_mgr.override_target}]...")
        await g_conn_mgr.connect(g_conn_mgr.override_target)

    while True:
        target_addr = g_conn_mgr.get_connected_address() if g_conn_mgr.is_connected() else None
        prompt_tag = f"ripple [{target_addr}]> " if target_addr else "ripple [disconnected]> "

        try:
            line = await asyncio.to_thread(_prompt_user, prompt_tag)
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
            print("Disconnecting and exiting Ripple interactive shell. Goodbye!")
            await g_conn_mgr.disconnect(silent=True)
            break
        elif verb in ("clear", "cls"):
            enable_windows_vt()
            sys.stdout.write("\033[2J\033[H")
            sys.stdout.flush()
        elif verb in ("help", "?"):
            print_interactive_help()
        elif verb == "scan":
            timeout = 4.0
            if arg:
                try:
                    timeout = float(arg)
                except ValueError:
                    pass
            await cmd_scan(timeout)
        elif verb == "connect":
            await cmd_connect(arg if arg else None)
        elif verb in ("disconnect", "disc"):
            await cmd_disconnect()
        elif verb in ("dashboard", "scope", "dash", "top", "htop", "btop"):
            rate = 50
            if arg:
                try:
                    rate = int(arg)
                except ValueError:
                    pass
            await cmd_dashboard(rate=rate)
        elif verb == "log":
            if arg.lower() in ("on", "start", "1", "true"):
                await cmd_send("log on")
            elif arg.lower() in ("off", "stop", "0", "false"):
                await cmd_send("log off")
            else:
                print("[-] Usage: log on | log off")
        elif verb in ("battery", "bat"):
            await cmd_battery()
        elif verb in ("set-bat-min", "bat_min", "set_bat_min"):
            if not arg:
                print("[-] Usage: set-bat-min <volts> (e.g. set-bat-min 3.20)")
                continue
            try:
                val = float(arg)
                await cmd_set_bat_min(val)
            except ValueError:
                print("[-] Invalid voltage value. Example: set-bat-min 3.20")
        elif verb == "status":
            await cmd_send("status")
        elif verb in ("sens", "threshold"):
            if not arg:
                print("[-] Usage: sens <value> (e.g. sens 0.22)")
                continue
            await cmd_send(f"sens {arg}")
        elif verb in ("test_alert", "alert"):
            await cmd_send("test_alert")
        elif verb in ("rearm", "reset"):
            await cmd_send("rearm")
        elif verb in ("calibrate", "cal"):
            await cmd_send("calibrate")
        elif verb == "sleep":
            await cmd_send("sleep")
        elif verb == "monitor":
            dur = 15.0
            if arg:
                try:
                    dur = float(arg)
                except ValueError:
                    pass
            await cmd_monitor(dur)
        elif verb == "logs":
            dur = 30.0
            if arg:
                try:
                    dur = float(arg)
                except ValueError:
                    pass
            await cmd_logs(dur)
        elif verb in ("ota-info", "otainfo"):
            await cmd_ota_info()
        elif verb == "ota":
            if not arg:
                print("[-] Usage: ota <firmware_path> (e.g. ota build/ripple.bin)")
                continue
            await cmd_ota(arg)
        elif verb == "reboot":
            await cmd_send("reboot")
        elif verb == "rollback":
            await cmd_send("rollback")
        elif verb == "cmd":
            if not arg:
                print("[-] Usage: cmd <command> (e.g. cmd status)")
                continue
            await cmd_send(arg)
        else:
            await cmd_send(line)


# =============================================================================
# CLI MAIN ENTRY POINT
# =============================================================================

def main():
    common_parser = argparse.ArgumentParser(add_help=False)
    common_parser.add_argument(
        "-m", "--mac", "--target", "--device",
        dest="mac",
        type=str,
        default=None,
        help="Target buoy MAC/BLE address (e.g. 3C:0F:02:DF:EB:82) to connect directly without prompting"
    )

    parser = argparse.ArgumentParser(
        parents=[common_parser],
        description="Ripple Smart Buoy — Bluetooth LE CLI & OTA Tool",
        epilog="Tip: Run without arguments to enter interactive shell mode."
    )
    sub = parser.add_subparsers(dest="action", required=False)

    sub.add_parser("interactive", parents=[common_parser], help="Start interactive command shell")
    sub.add_parser("shell", parents=[common_parser], help="Start interactive command shell")

    for p_name in ("dashboard", "scope", "dash", "top", "htop", "btop"):
        p_dash = sub.add_parser(p_name, parents=[common_parser], help="Native terminal telemetry dashboard with live 50 Hz wave & 2-hour battery graphs (top/btop style)")
        p_dash.add_argument("-r", "--rate", type=int, default=50, help="Telemetry polling/streaming rate in Hz (1 to 50 Hz, default: 50 Hz)")

    p_log = sub.add_parser("log", parents=[common_parser], help="Enable or disable real-time serial log streaming over BLE")
    p_log.add_argument("state", choices=["on", "off"], help="State: 'on' or 'off'")

    sub.add_parser("help", parents=[common_parser], help="Show this help message with all available commands")

    p_scan = sub.add_parser("scan", parents=[common_parser], help="Scan for Ripple BLE advertisements within Bluetooth range")
    p_scan.add_argument("-t", "--timeout", type=float, default=4.0, help="Scan timeout in seconds (default: 4s)")

    p_conn = sub.add_parser("connect", parents=[common_parser], help="Connect to a specific Ripple buoy and save session")
    p_conn.add_argument("target", nargs="?", default=None, help="Device index [1-N] from scan or MAC address")

    sub.add_parser("disconnect", parents=[common_parser], help="Disconnect from active buoy session")
    sub.add_parser("clear", help="Clear terminal screen")
    sub.add_parser("cls", help="Clear terminal screen")

    p_cmd = sub.add_parser("cmd", parents=[common_parser], help="Send remote NUS command (status, battery, bat_min <v>, sleep, sens <v>, test_alert, rearm, calibrate, reboot, rollback)")
    p_cmd.add_argument("command", type=str, help="Command to send (e.g. 'status', 'battery', 'sleep', 'sens 0.25', 'rearm')")

    p_mon = sub.add_parser("monitor", parents=[common_parser], help="Monitor wave telemetry and state in real time via BLE")
    p_mon.add_argument("-d", "--duration", type=float, default=30.0, help="Monitoring duration in seconds (default: 30s)")

    p_logs = sub.add_parser("logs", parents=[common_parser], help="Stream ESP-IDF serial logs in real time via BLE")
    p_logs.add_argument("-d", "--duration", type=float, default=60.0, help="Log capture duration in seconds (default: 60s)")

    sub.add_parser("ota-info", parents=[common_parser], help="Show active partition, boot partition, and firmware status")
    sub.add_parser("battery", parents=[common_parser], help="Check buoy battery voltage, percentage, and protection status via BLE")

    p_set_bat = sub.add_parser("set-bat-min", parents=[common_parser], help="Set safe minimum voltage (0%% charge / deep sleep cutoff) and store to NVS")
    p_set_bat.add_argument("volts", type=float, help="Minimum voltage in Volts (e.g. 3.20)")

    p_ota = sub.add_parser("ota", parents=[common_parser], help="Flash buoy firmware via high-speed secure BLE OTA")
    p_ota.add_argument("file", type=str, help="Path to firmware binary (.bin)")
    p_ota.add_argument("-c", "--chunk", type=int, default=480, help="Transfer chunk size in bytes (default: 480)")

    args = parser.parse_args()

    if getattr(args, "mac", None):
        g_conn_mgr.set_override_target(args.mac)

    if not args.action or args.action in ("interactive", "shell"):
        try:
            asyncio.run(interactive_shell())
        except KeyboardInterrupt:
            print("\nTerminated.")
    elif args.action == "help":
        parser.print_help()
        sys.exit(0)
    elif args.action in ("dashboard", "scope", "dash", "top", "htop", "btop"):
        try:
            asyncio.run(cmd_dashboard(rate=args.rate, target=getattr(args, "mac", None)))
        except KeyboardInterrupt:
            print("\n[*] Dashboard terminated.")
    elif args.action == "log":
        asyncio.run(cmd_send(f"log {args.state}"))
    elif args.action == "scan":
        asyncio.run(cmd_scan(args.timeout))
    elif args.action == "connect":
        target = args.target or getattr(args, "mac", None)
        asyncio.run(cmd_connect(target))
    elif args.action == "disconnect":
        asyncio.run(cmd_disconnect())
    elif args.action in ("clear", "cls"):
        enable_windows_vt()
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()
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
    elif args.action == "scan":
        asyncio.run(cmd_scan(args.timeout))
    elif args.action == "connect":
        target = args.target or getattr(args, "mac", None)
        asyncio.run(cmd_connect(target))
    elif args.action == "disconnect":
        asyncio.run(cmd_disconnect())
    elif args.action in ("clear", "cls"):
        enable_windows_vt()
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()
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
