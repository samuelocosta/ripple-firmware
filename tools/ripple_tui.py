"""
Ripple Smart Buoy — Native High-Speed Terminal Telemetry Dashboard (TUI)
Inspired by btop/htop with sub-pixel Braille oscilloscope graphing,
real-time 50 Hz wave & 2-hour battery tracking, and ESP-IDF log demultiplexing.
"""

import asyncio
from collections import deque, namedtuple
import datetime
import json
import math
import os
import re
import shutil
import sys
import time

try:
    import msvcrt
except ImportError:
    msvcrt = None

try:
    import select
except ImportError:
    select = None

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


# ANSI Color & Formatting Constants
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"

# Standard Foreground Colors
FG_BLACK = "\033[30m"
FG_RED = "\033[31m"
FG_GREEN = "\033[32m"
FG_YELLOW = "\033[33m"
FG_BLUE = "\033[34m"
FG_MAGENTA = "\033[35m"
FG_CYAN = "\033[36m"
FG_WHITE = "\033[37m"

# Bright Foreground Colors
FG_BRIGHT_BLACK = "\033[90m"
FG_BRIGHT_RED = "\033[91m"
FG_BRIGHT_GREEN = "\033[92m"
FG_BRIGHT_YELLOW = "\033[93m"
FG_BRIGHT_BLUE = "\033[94m"
FG_BRIGHT_MAGENTA = "\033[95m"
FG_BRIGHT_CYAN = "\033[96m"
FG_BRIGHT_WHITE = "\033[97m"

# Background Colors
BG_RED = "\033[41m"
BG_GREEN = "\033[42m"
BG_YELLOW = "\033[43m"
BG_BLUE = "\033[44m"
BG_CYAN = "\033[46m"
BG_DARK = "\033[100m"

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def strip_ansi(text: str) -> str:
    """Compute visual string length excluding ANSI escape sequences."""
    return ANSI_RE.sub("", text)


def truncate_ansi(text: str, max_len: int) -> str:
    """Truncate text to at most max_len visible characters while keeping ANSI codes."""
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
    result.append(RESET)
    return "".join(result)


def make_border(left: str, styled_title: str, right: str, width: int) -> str:
    """Format a border line like '┌─ TITLE ───┐' with visual width exactly equal to width."""
    prefix = f"{left} {styled_title} " if styled_title else left
    plain_len = len(strip_ansi(prefix)) + len(strip_ansi(right))
    fill = max(0, width - plain_len)
    return prefix + ("─" * fill) + right


# =============================================================================
# PACKET CLASSIFICATION & DEMULTIPLEXING
# =============================================================================

def classify_packet(raw_bytes: bytes | bytearray | str) -> tuple[str, dict | str]:
    """
    Demultiplex raw BLE notification payloads into either:
      ('json', parsed_dict) for structured telemetry, state reports, and events.
      ('log', line_str) for ESP-IDF serial console logs (I, W, E, D lines).
      ('empty', "") for empty/whitespace packets.
    """
    if isinstance(raw_bytes, (bytes, bytearray)):
        line = raw_bytes.decode("utf-8", errors="replace").strip()
    else:
        line = str(raw_bytes).strip()

    if not line:
        return "empty", ""

    if line.startswith("{") and line.endswith("}"):
        try:
            data = json.loads(line)
            if isinstance(data, dict):
                return "json", data
        except Exception:
            pass

    return "log", line


# =============================================================================
# DATA STRUCTURES & RING BUFFERS
# =============================================================================

TelemetryPoint = namedtuple(
    "TelemetryPoint",
    [
        "ts",         # float: unix timestamp
        "wave",       # float: current wave motion coefficient (DIMENSIONLESS)
        "peak",       # float: session peak wave coefficient (DIMENSIONLESS)
        "thr",        # float: detection threshold (DIMENSIONLESS)
        "da",         # float: delta accel (g)
        "gyro",       # float: gyro tilt (deg/s)
        "vbat",       # float: battery voltage (V)
        "bat_pct",    # float: battery capacity %
        "temp",       # float: temperature (°C)
        "hum",        # float: humidity (% RH)
        "hum_ok",     # bool: whether humidity sensor is present
        "state",      # str: CALIBRATING / MONITORING / FALL_ALERT
        "alerts",     # int: total alerts count
    ],
)


class TelemetryRingBuffer:
    """
    High-capacity telemetry ring buffer:
    - Wave buffer: fixed 60s window @ 50 Hz (~3,000 samples).
    - Battery buffer: fixed 2-hour window (7,200s) @ 50 Hz (~360,000 samples).
    """

    def __init__(self, wave_capacity: int = 10_000, bat_capacity: int = 400_000):
        self.wave_samples = deque(maxlen=wave_capacity)
        # Store (ts, vbat, bat_pct) tuples for battery to minimize memory overhead
        self.bat_samples = deque(maxlen=bat_capacity)
        self.recent_ts = deque(maxlen=150)
        self.latest_point: TelemetryPoint | None = None
        self.peak_da_all = 0.0
        self.peak_gyro_all = 0.0
        self.total_packets = 0

    def append(self, point: TelemetryPoint):
        self.latest_point = point
        self.total_packets += 1
        self.wave_samples.append(point)
        self.bat_samples.append((point.ts, point.vbat, point.bat_pct))
        self.recent_ts.append(point.ts)

        if point.da > self.peak_da_all:
            self.peak_da_all = point.da
        if point.gyro > self.peak_gyro_all:
            self.peak_gyro_all = point.gyro

    def calc_observed_rate(self) -> float:
        """Calculate real-time packets per second (Hz) based on recent arrivals."""
        now = time.time()
        recent = [t for t in self.recent_ts if (now - t) <= 1.5]
        if len(recent) < 2:
            return 0.0
        dt = recent[-1] - recent[0]
        if dt <= 0.001:
            return 0.0
        return (len(recent) - 1) / dt

    def get_wave_window(self, window_sec: float = 60.0) -> list[TelemetryPoint]:
        """Return all wave points within the last window_sec."""
        if not self.wave_samples:
            return []
        cutoff = time.time() - window_sec
        pts = []
        for p in reversed(self.wave_samples):
            if p.ts < cutoff:
                break
            pts.append(p)
        pts.reverse()
        return pts

    def get_battery_window(self, window_sec: float = 7200.0) -> list[tuple[float, float, float]]:
        """Return all battery samples (ts, vbat, bat_pct) within the last window_sec (default 2h)."""
        if not self.bat_samples:
            return []
        cutoff = time.time() - window_sec
        pts = []
        for p in reversed(self.bat_samples):
            if p[0] < cutoff:
                break
            pts.append(p)
        pts.reverse()
        return pts


class LogRingBuffer:
    """Thread-safe ring buffer for ESP-IDF serial console and event logs."""

    def __init__(self, capacity: int = 200):
        self.capacity = capacity
        self.entries = deque(maxlen=capacity)

    def append(self, text: str, tag: str = "LOG"):
        ts_str = datetime.datetime.now().strftime("%H:%M:%S")
        self.entries.append((ts_str, tag, text.strip()))

    def get_tail(self, count: int) -> list[tuple[str, str, str]]:
        if not self.entries:
            return []
        count = max(1, min(count, len(self.entries)))
        return list(self.entries)[-count:]


# =============================================================================
# BRAILLE SUB-PIXEL OSCILLOSCOPE CANVAS (WAVE GRAPH)
# =============================================================================

class BrailleCanvas:
    """
    Sub-pixel terminal plotting using Unicode Braille Patterns (U+2800..U+28FF).
    Each character cell is 2 horizontal by 4 vertical dots.
    """

    DOT_MAP = {
        (0, 0): 0x01,
        (1, 0): 0x02,
        (2, 0): 0x04,
        (3, 0): 0x40,
        (0, 1): 0x08,
        (1, 1): 0x10,
        (2, 1): 0x20,
        (3, 1): 0x80,
    }

    def __init__(self, char_cols: int, char_rows: int):
        self.char_cols = max(10, char_cols)
        self.char_rows = max(3, char_rows)
        self.pixel_w = self.char_cols * 2
        self.pixel_h = self.char_rows * 4
        self.grid = [[0 for _ in range(self.char_cols)] for _ in range(self.char_rows)]
        self.thr_mask = [[0 for _ in range(self.char_cols)] for _ in range(self.char_rows)]

    def set_pixel(self, px: int, py: int):
        if 0 <= px < self.pixel_w and 0 <= py < self.pixel_h:
            c = px // 2
            r = py // 4
            sub_c = px % 2
            sub_r = py % 4
            self.grid[r][c] |= self.DOT_MAP[(sub_r, sub_c)]

    def draw_line(self, x0: int, y0: int, x1: int, y1: int):
        """Bresenham's line algorithm in sub-pixel space."""
        dx = abs(x1 - x0)
        dy = abs(y1 - y0)
        sx = 1 if x0 < x1 else -1
        sy = 1 if y0 < y1 else -1
        err = dx - dy

        while True:
            self.set_pixel(x0, y0)
            if x0 == x1 and y0 == y1:
                break
            e2 = 2 * err
            if e2 > -dy:
                err -= dy
                x0 += sx
            if e2 < dx:
                err += dx
                y0 += sy

    def draw_threshold_line(self, thr_y_pixel: int):
        """Draw a horizontal dashed/dotted threshold line across the canvas."""
        if 0 <= thr_y_pixel < self.pixel_h:
            r = thr_y_pixel // 4
            sub_r = thr_y_pixel % 4
            for c in range(self.char_cols):
                # Dashed pattern: 2 dots on, 2 dots off
                bit0 = self.DOT_MAP[(sub_r, 0)]
                bit1 = self.DOT_MAP[(sub_r, 1)]
                if c % 2 == 0:
                    self.thr_mask[r][c] |= (bit0 | bit1)
                    self.grid[r][c] |= (bit0 | bit1)

    def render_rows(self, thr_color: str = FG_BRIGHT_YELLOW, wave_color: str = FG_BRIGHT_CYAN) -> list[str]:
        """Render all character rows with appropriate ANSI colors."""
        out = []
        for r in range(self.char_rows):
            line_parts = []
            cur_color = ""
            for c in range(self.char_cols):
                val = self.grid[r][c]
                is_thr_only = (val != 0) and (val == self.thr_mask[r][c])
                target_color = thr_color if is_thr_only else wave_color

                if target_color != cur_color:
                    line_parts.append(target_color)
                    cur_color = target_color

                ch = " " if val == 0 else chr(0x2800 + val)
                line_parts.append(ch)

            line_parts.append(RESET)
            out.append("".join(line_parts))
        return out


# =============================================================================
# BATTERY DISCHARGE PROFILE CANVAS (2-HOUR GRAPH)
# =============================================================================

class BatteryCanvas:
    """
    Renders the 2-hour battery voltage discharge profile with cutoff line.
    Uses multi-level block characters (' ', '▂', '▃', '▄', '▅', '▆', '▇', '█')
    or Braille for smooth voltage tracking.
    """

    BLOCKS = (" ", " ", "▂", "▃", "▄", "▅", "▆", "▇", "█")

    def __init__(self, char_cols: int, char_rows: int):
        self.char_cols = max(10, char_cols)
        self.char_rows = max(3, char_rows)

    def render(
        self,
        samples: list[tuple[float, float, float]],
        window_sec: float = 7200.0,
        min_cutoff_v: float = 3.20,
        max_v: float = 4.25,
        min_v: float = 3.00,
    ) -> list[str]:
        """
        Draw 2-hour discharge curve into character grid with safe cutoff reference.
        samples: list of (ts, vbat, bat_pct)
        """
        now = time.time()
        t_start = now - window_sec

        # Downsample into char_cols buckets
        buckets = [[] for _ in range(self.char_cols)]
        for ts, vbat, pct in samples:
            if ts < t_start:
                continue
            frac = (ts - t_start) / window_sec
            c = int(frac * (self.char_cols - 1))
            c = max(0, min(self.char_cols - 1, c))
            buckets[c].append(vbat)

        col_voltages = [None] * self.char_cols
        for c in range(self.char_cols):
            if buckets[c]:
                col_voltages[c] = sum(buckets[c]) / len(buckets[c])

        # Fill grid (char_rows x char_cols)
        # Row 0 is highest voltage (max_v), row (char_rows - 1) is min_v
        grid = [[" " for _ in range(self.char_cols)] for _ in range(self.char_rows)]
        cutoff_row = int(((max_v - min_cutoff_v) / (max_v - min_v)) * (self.char_rows - 1))
        cutoff_row = max(0, min(self.char_rows - 1, cutoff_row))

        # Place cutoff dotted line across
        for c in range(self.char_cols):
            grid[cutoff_row][c] = f"{FG_BRIGHT_RED}┄{RESET}"

        # Plot battery curve
        for c, v in enumerate(col_voltages):
            if v is None:
                continue
            v_norm = (v - min_v) / (max_v - min_v)
            v_norm = max(0.0, min(1.0, v_norm))
            float_row = (1.0 - v_norm) * (self.char_rows - 1)
            row_idx = int(round(float_row))
            row_idx = max(0, min(self.char_rows - 1, row_idx))

            # Color depending on health
            color = FG_BRIGHT_GREEN if v >= 3.60 else (FG_BRIGHT_YELLOW if v >= min_cutoff_v else FG_BRIGHT_RED)
            grid[row_idx][c] = f"{color}█{RESET}"

        return ["".join(row) for row in grid]


# =============================================================================
# RIPPLE TUI DASHBOARD (MAIN CONTROLLER)
# =============================================================================

class RippleTuiDashboard:
    """
    Full-terminal reactive telemetry dashboard (top/btop style).
    - Fixed 1-minute window @ 50 Hz for motion wave dynamics (dimensionless).
    - Fixed 2-hour window @ 50 Hz for battery discharge profile.
    - Live logs & events console without swallowing command responses.
    - Non-blocking keyboard shortcuts.
    """

    def __init__(
        self,
        dev_name: str = "Ripple-Buoy",
        dev_addr: str = "Unknown",
        send_cmd_fn=None,
        min_safe_voltage: float = 3.20,
    ):
        self.dev_name = dev_name
        self.dev_addr = dev_addr
        self.send_cmd_fn = send_cmd_fn
        self.min_safe_voltage = min_safe_voltage

        self.buffer = TelemetryRingBuffer()
        self.logs = LogRingBuffer(capacity=200)

        self.running = False
        self.paused = False
        self.start_time = time.time()
        self.active_rate_hz = 50
        self.log_stream_enabled = False

        enable_windows_vt()

    def push_raw_ble_packet(self, data: bytes | bytearray | str):
        """Demultiplex and route an incoming BLE notification."""
        kind, payload = classify_packet(data)
        if kind == "json":
            self.push_json_packet(payload)
        elif kind == "log":
            self.push_log(payload, tag="ESP-LOG")

    def push_log(self, text: str, tag: str = "LOG"):
        """Add an event or system log message to the live log buffer."""
        self.logs.append(text, tag=tag)

    def push_json_packet(self, d: dict):
        """Process structured telemetry and buoy event JSON messages."""
        ev = d.get("event")
        if ev:
            if ev == "FALL_ALERT":
                self.push_log("EMERGENCY: Sudden motion detected! Possible fall in pool.", tag="ALERT")
            elif ev == "REARMED":
                self.push_log("Buoy rearmed successfully for monitoring.", tag="SYSTEM")
            elif ev == "CALIBRATING_STARTED":
                self.push_log("Calm water baseline calibration started.", tag="CALIB")
            elif ev == "THRESHOLD_UPDATED":
                self.push_log(f"Sensitivity threshold updated to {d.get('thr', 0):.3f}", tag="CONFIG")
            elif ev == "CRITICAL_BATTERY_SHUTDOWN":
                self.push_log("Critical battery cutoff reached! Entering Deep Sleep.", tag="BATTERY")
            else:
                self.push_log(f"Event: {ev}", tag="EVENT")

        if "stream" in d and "hz" in d:
            self.active_rate_hz = int(d.get("hz", 50))
            self.push_log(f"Stream confirmed at {self.active_rate_hz} Hz ({d.get('interval_ms')} ms loop)", tag="STREAM")

        if "log_stream" in d:
            self.log_stream_enabled = bool(d.get("log_stream"))
            st_text = "ENABLED" if self.log_stream_enabled else "DISABLED"
            self.push_log(f"Firmware serial log streaming {st_text}", tag="SYSTEM")

        if "min_v" in d:
            try:
                self.min_safe_voltage = float(d["min_v"])
            except Exception:
                pass

        if "wave" in d:
            point = TelemetryPoint(
                ts=time.time(),
                wave=float(d.get("wave", 0.0)),
                peak=float(d.get("peak", 0.0)),
                thr=float(d.get("thr", 0.22)),
                da=float(d.get("da", 0.0)),
                gyro=float(d.get("gyro", 0.0)),
                vbat=float(d.get("vbat", 3.8)),
                bat_pct=float(d.get("bat_pct", 80.0)),
                temp=float(d.get("temp", 25.0)),
                hum=float(d.get("hum", 50.0)) if d.get("hum") is not None else 50.0,
                hum_ok=bool(d.get("hum_ok", False) or d.get("hum") is not None),
                state=str(d.get("state", "MONITORING")),
                alerts=int(d.get("alerts", 0)),
            )
            self.buffer.append(point)

    async def _send_cmd(self, cmd_text: str):
        """Send command to buoy via provided callback."""
        self.push_log(f"> {cmd_text}", tag="CMD")
        if self.send_cmd_fn:
            try:
                await self.send_cmd_fn(cmd_text)
            except Exception as e:
                self.push_log(f"Command error: {e}", tag="ERROR")

    # =========================================================================
    # FRAME RENDERING (DOUBLE BUFFERED)
    # =========================================================================

    def render_frame(self, term_cols: int, term_rows: int) -> str:
        """Construct the entire TUI screen in memory without flickering."""
        w = max(80, term_cols)
        h = max(24, term_rows)

        lp = self.buffer.latest_point or TelemetryPoint(
            ts=time.time(), wave=0.0, peak=0.0, thr=0.22, da=0.0, gyro=0.0,
            vbat=3.8, bat_pct=80.0, temp=25.0, hum=50.0, hum_ok=False,
            state="MONITORING", alerts=0
        )

        obs_hz = self.buffer.calc_observed_rate()
        uptime_sec = int(time.time() - self.start_time)
        uptime_str = str(datetime.timedelta(seconds=uptime_sec))

        lines = []

        # ---------------------------------------------------------------------
        # 1. HEADER
        # ---------------------------------------------------------------------
        state_color = FG_BRIGHT_GREEN
        if lp.state == "FALL_ALERT":
            state_color = f"{BG_RED}{BOLD}{FG_WHITE}"
        elif lp.state == "CALIBRATING":
            state_color = f"{BG_YELLOW}{BOLD}{FG_BLACK}"

        state_badge = f"{state_color} [ {lp.state} ] {RESET}"
        hdr_title = f"{BOLD}{FG_BRIGHT_CYAN}RIPPLE SMART BUOY{RESET} {DIM}— Telemetry Dashboard [top/btop]{RESET}"
        lines.append(make_border("┌─", f"{hdr_title}  Status: {state_badge}", "─┐", w))

        info_line = (
            f" Target: {BOLD}{self.dev_name}{RESET} [{self.dev_addr}] "
            f"│ Stream: {BOLD}{self.active_rate_hz} Hz{RESET} (Observed: {obs_hz:.1f} Hz) "
            f"│ Samples: {self.buffer.total_packets:,} "
            f"│ Uptime: {uptime_str} "
            + (f"│ {FG_YELLOW}[PAUSED]{RESET}" if self.paused else "")
        )
        lines.append(f"│ {truncate_ansi(info_line, w - 4):<{w - 4}} │")
        lines.append(make_border("├─", f"{BOLD}LIVE SENSORS & METRICS (50 Hz Native){RESET}", "─┤", w))

        # ---------------------------------------------------------------------
        # 2. SENSOR METRICS & KPIS (Dimensionless Wave)
        # ---------------------------------------------------------------------
        margin = lp.thr - lp.wave
        margin_color = FG_BRIGHT_GREEN if margin > 0.05 else (FG_BRIGHT_YELLOW if margin > 0.0 else FG_BRIGHT_RED)
        wave_color = FG_BRIGHT_RED if lp.wave >= lp.thr else FG_BRIGHT_CYAN

        wave_line = (
            f" Wave Motion: {BOLD}{wave_color}{lp.wave:.3f}{RESET} "
            f"[Peak: {BOLD}{lp.peak:.3f}{RESET} │ Thr: {FG_YELLOW}{lp.thr:.3f}{RESET} │ Margin: {margin_color}{margin:+.3f}{RESET}]  "
            f"│ Δa: {BOLD}{lp.da:.3f} g{RESET} (Peak: {self.buffer.peak_da_all:.3f} g)  "
            f"│ Gyro: {BOLD}{lp.gyro:.1f} °/s{RESET} (Peak: {self.buffer.peak_gyro_all:.1f} °/s)"
        )
        lines.append(f"│ {truncate_ansi(wave_line, w - 4):<{w - 4}} │")

        # Battery bar: 15 chars
        bat_blocks = int(max(0.0, min(100.0, lp.bat_pct)) / 100.0 * 15)
        bat_bar = f"{FG_BRIGHT_GREEN}{'█' * bat_blocks}{DIM}{'░' * (15 - bat_blocks)}{RESET}"
        watertight_badge = f"{FG_BRIGHT_GREEN}SEALED / WATERTIGHT{RESET}" if lp.hum < 70.0 else f"{BG_RED}{FG_WHITE}LEAK WARNING{RESET}"

        bat_line = (
            f" Battery:     {BOLD}{lp.vbat:.3f} V{RESET} [{bat_bar}] {BOLD}{lp.bat_pct:.1f}%{RESET} (Cutoff: {self.min_safe_voltage:.2f}V)  "
            f"│ Temp: {BOLD}{lp.temp:.1f} °C{RESET}  "
            f"│ Hum: {BOLD}{lp.hum:.1f}% RH{RESET} [{watertight_badge}]  "
            f"│ Alerts: {BOLD}{lp.alerts}{RESET}"
        )
        lines.append(f"│ {truncate_ansi(bat_line, w - 4):<{w - 4}} │")

        # ---------------------------------------------------------------------
        # 3. DYNAMIC HEIGHT BUDGETING
        # ---------------------------------------------------------------------
        # Fixed overhead: Header (5 lines) + Wave title (1) + Wave axis (1) +
        # Battery title (1) + Battery axis (1) + Logs title (1) + Footer (2) = 12 lines
        remaining_h = max(12, h - 12)
        wave_plot_h = max(4, int(remaining_h * 0.40))
        bat_plot_h = max(3, int(remaining_h * 0.28))
        log_plot_h = max(3, remaining_h - wave_plot_h - bat_plot_h)

        # ---------------------------------------------------------------------
        # 4. WAVE MOTION DYNAMICS GRAPH (Fixed 1-Minute Window @ 50 Hz)
        # ---------------------------------------------------------------------
        lines.append(make_border("├─", f"{BOLD}WAVE MOTION DYNAMICS (Fixed 1-Minute Window [60s] @ 50 Hz){RESET} ── Threshold: {FG_YELLOW}{lp.thr:.3f}{RESET}", "─┤", w))

        label_w = 7
        plot_w_chars = w - label_w - 4

        b_canvas = BrailleCanvas(plot_w_chars, wave_plot_h)
        wave_pts = self.buffer.get_wave_window(window_sec=60.0)

        # Determine Y scale (dimensionless)
        max_y = max(0.35, lp.thr * 1.35)
        for p in wave_pts:
            if p.wave > max_y:
                max_y = p.wave * 1.15
        min_y = 0.0

        # Draw threshold line
        thr_y_norm = (lp.thr - min_y) / max(0.001, (max_y - min_y))
        thr_pixel_y = int((1.0 - max(0.0, min(1.0, thr_y_norm))) * (b_canvas.pixel_h - 1))
        b_canvas.draw_threshold_line(thr_pixel_y)

        # Plot wave samples
        if len(wave_pts) >= 2:
            t_now = time.time()
            t_start = t_now - 60.0
            prev_px = None
            prev_py = None

            for p in wave_pts:
                frac_x = (p.ts - t_start) / 60.0
                px = int(frac_x * (b_canvas.pixel_w - 1))
                px = max(0, min(b_canvas.pixel_w - 1, px))

                frac_y = (p.wave - min_y) / max(0.001, (max_y - min_y))
                py = int((1.0 - max(0.0, min(1.0, frac_y))) * (b_canvas.pixel_h - 1))
                py = max(0, min(b_canvas.pixel_h - 1, py))

                if prev_px is not None:
                    b_canvas.draw_line(prev_px, prev_py, px, py)
                else:
                    b_canvas.set_pixel(px, py)

                prev_px = px
                prev_py = py

        wave_rows = b_canvas.render_rows(thr_color=FG_BRIGHT_YELLOW, wave_color=FG_BRIGHT_CYAN)
        for i, row_str in enumerate(wave_rows):
            # Y axis label
            val_at_row = max_y - (i / max(1, wave_plot_h - 1)) * (max_y - min_y)
            lbl = f"{val_at_row:5.2f} ┤"
            lines.append(f"│ {lbl}{row_str} │")

        # X axis for 60s
        x_axis_ticks = " -60s" + " " * max(1, (plot_w_chars // 4) - 5) + "-45s" + " " * max(1, (plot_w_chars // 4) - 5) + "-30s" + " " * max(1, (plot_w_chars // 4) - 5) + "-15s" + " " * max(1, (plot_w_chars // 4) - 8) + "Now (0s)"
        lines.append(f"│ {' ' * label_w}{x_axis_ticks:<{plot_w_chars}} │")

        # ---------------------------------------------------------------------
        # 5. BATTERY DISCHARGE PROFILE GRAPH (Fixed 2-Hour Window @ 50 Hz)
        # ---------------------------------------------------------------------
        lines.append(make_border("├─", f"{BOLD}BATTERY VOLTAGE & DISCHARGE (Fixed 2-Hour Window [120m] @ 50 Hz){RESET} ── Cutoff: {FG_RED}{self.min_safe_voltage:.2f}V{RESET}", "─┤", w))

        bat_canvas = BatteryCanvas(plot_w_chars, bat_plot_h)
        bat_samples = self.buffer.get_battery_window(window_sec=7200.0)
        bat_rows = bat_canvas.render(
            bat_samples,
            window_sec=7200.0,
            min_cutoff_v=self.min_safe_voltage,
            max_v=4.25,
            min_v=3.00,
        )

        for i, row_str in enumerate(bat_rows):
            v_val = 4.25 - (i / max(1, bat_plot_h - 1)) * (4.25 - 3.00)
            lbl = f"{v_val:5.2f}V┤"
            lines.append(f"│ {lbl}{row_str} │")

        # X axis for 2h
        x_axis_bat = " -120m" + " " * max(1, (plot_w_chars // 4) - 6) + "-90m" + " " * max(1, (plot_w_chars // 4) - 5) + "-60m" + " " * max(1, (plot_w_chars // 4) - 5) + "-30m" + " " * max(1, (plot_w_chars // 4) - 8) + "Now (0m)"
        lines.append(f"│ {' ' * label_w}{x_axis_bat:<{plot_w_chars}} │")

        # ---------------------------------------------------------------------
        # 6. LIVE CONSOLE & BLE LOGS
        # ---------------------------------------------------------------------
        log_title = f"{BOLD}LIVE CONSOLE & FIRMWARE LOGS{RESET} (Last {log_plot_h} events)"
        lines.append(make_border("├─", log_title, "─┤", w))

        tail_logs = self.logs.get_tail(log_plot_h)
        # Pad with empty lines if fewer logs
        while len(tail_logs) < log_plot_h:
            tail_logs.insert(0, ("", "", ""))

        for ts, tag, text in tail_logs:
            if not text:
                lines.append(f"│ {'':<{w - 4}} │")
                continue

            tag_color = FG_BRIGHT_BLACK
            if tag == "ALERT":
                tag_color = f"{BG_RED}{FG_WHITE}{BOLD}"
            elif tag in ("SYSTEM", "STREAM"):
                tag_color = FG_BRIGHT_GREEN
            elif tag == "CMD":
                tag_color = FG_BRIGHT_YELLOW
            elif tag == "ERROR":
                tag_color = FG_BRIGHT_RED
            elif tag == "ESP-LOG":
                tag_color = FG_CYAN

            formatted = f"{DIM}[{ts}]{RESET} {tag_color}[{tag}]{RESET} {text}"
            lines.append(f"│ {truncate_ansi(formatted, w - 4):<{w - 4}} │")

        # ---------------------------------------------------------------------
        # 7. FOOTER & HOTKEYS
        # ---------------------------------------------------------------------
        hotkeys = (
            f"{BOLD}[Q]{RESET} Quit  "
            f"{BOLD}[R]{RESET} Rearm  "
            f"{BOLD}[C]{RESET} Calibrate  "
            f"{BOLD}[T]{RESET} Test Alert  "
            f"{BOLD}[1..5]{RESET} Rate (1/5/10/20/50Hz)  "
            f"{BOLD}[P]{RESET} Pause  "
            f"{BOLD}[L]{RESET} Toggle Log Stream  "
            f"{BOLD}[S]{RESET} Sleep"
        )
        lines.append(make_border("├─", hotkeys, "─┤", w))
        lines.append("└" + ("─" * (w - 2)) + "┘")

        return "\n".join(lines[:h])

    # =========================================================================
    # KEYBOARD INPUT LISTENER (NON-BLOCKING)
    # =========================================================================

    def _poll_key(self) -> str | None:
        """Poll for non-blocking single key press across Windows and POSIX."""
        if msvcrt:
            if msvcrt.kbhit():
                try:
                    ch = msvcrt.getch()
                    if ch in (b"\x00", b"\xe0"):  # Special / Arrow keys
                        msvcrt.getch()
                        return None
                    return ch.decode("utf-8", errors="ignore")
                except Exception:
                    return None
            return None

        if select:
            try:
                r, _, _ = select.select([sys.stdin], [], [], 0.0)
                if r:
                    return sys.stdin.read(1)
            except Exception:
                return None

        return None

    # =========================================================================
    # ASYNC MAIN LOOP
    # =========================================================================

    async def run_async(self):
        """Execute the TUI dashboard loop with alternate screen buffer."""
        self.running = True

        # Activate alternate screen buffer, hide cursor, clear screen
        sys.stdout.write("\033[?1049h\033[?25l\033[2J\033[H")
        sys.stdout.flush()

        try:
            while self.running:
                # 1. Check keyboard
                key = self._poll_key()
                if key:
                    key_lower = key.lower()
                    if key_lower in ("q", "\x1b"):  # q or Esc
                        self.running = False
                        break
                    elif key_lower == "r":
                        await self._send_cmd("rearm")
                    elif key_lower == "c":
                        await self._send_cmd("calibrate")
                    elif key_lower == "t":
                        await self._send_cmd("test_alert")
                    elif key_lower == "p":
                        self.paused = not self.paused
                        st = "paused" if self.paused else "resumed"
                        self.push_log(f"Telemetry rendering {st}.", tag="SYSTEM")
                    elif key_lower == "l":
                        self.log_stream_enabled = not self.log_stream_enabled
                        cmd = "log on" if self.log_stream_enabled else "log off"
                        await self._send_cmd(cmd)
                    elif key_lower == "s":
                        await self._send_cmd("sleep")
                    elif key == "1":
                        await self._send_cmd("stream 1")
                    elif key == "2":
                        await self._send_cmd("stream 5")
                    elif key == "3":
                        await self._send_cmd("stream 10")
                    elif key == "4":
                        await self._send_cmd("stream 20")
                    elif key == "5":
                        await self._send_cmd("stream 50")

                # 2. Render frame if not paused
                cols, rows = shutil.get_terminal_size((100, 30))
                frame = self.render_frame(cols, rows)
                # Cursor home + full frame overwrite
                sys.stdout.write(f"\033[H{frame}")
                sys.stdout.flush()

                # Refresh at 10 Hz (100 ms loop)
                await asyncio.sleep(0.10)

        finally:
            # Cleanly restore cursor and exit alternate screen buffer
            sys.stdout.write("\033[?25h\033[?1049l")
            sys.stdout.flush()
