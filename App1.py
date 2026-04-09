#!/usr/bin/env python3
"""
Data Acquisition System
=======================
Standalone thermocouple data acquisition for Raspberry Pi 4 + MCC 134.
- Reads 4 Type K channels, dynamically detects active ones.
- Logs to CSV every 30 minutes with DDMMYYYY_HH_MM naming.
- Serves a live web dashboard on port 5000.
- Triggered by SPST toggle switch on GPIO 23.
"""

import os
import sys
import csv
import time
import json
import threading
from datetime import datetime
from pathlib import Path
import subprocess

from flask import Flask, render_template, jsonify, send_from_directory

# ---------------------------------------------------------------------------
# MCC 134 Setup (with fallback for development without hardware)
# ---------------------------------------------------------------------------
try:
    from daqhats import mcc134, HatError, OptionFlags
    MCC134_AVAILABLE = True
except ImportError:
    MCC134_AVAILABLE = False
    print("[WARN] daqhats library not found. Running in SIMULATION mode.")

# TcType constants (defined manually for compatibility)
class TcType:
    TYPE_J = 0
    TYPE_K = 1
    TYPE_T = 2
    TYPE_E = 3
    TYPE_R = 4
    TYPE_S = 5
    TYPE_B = 6
    TYPE_N = 7
    DISABLED = 255

# MCC 134 open thermocouple sentinel value
OPEN_TC_VALUE = -9999.0
OPEN_TC_THRESHOLD = -8000.0  # Anything below this is considered "no probe"

# ---------------------------------------------------------------------------
# GPIO Setup (with fallback)
# ---------------------------------------------------------------------------
GPIO_SWITCH_PIN = 23

try:
    import RPi.GPIO as GPIO
    GPIO_AVAILABLE = True
except ImportError:
    try:
        import lgpio
        GPIO_AVAILABLE = True
    except ImportError:
        GPIO_AVAILABLE = False
        print("[WARN] No GPIO library found. Switch always treated as ON.")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
TEST_MODE = False          # Set to True to bypass GPIO switch (always ON)
INVERT_SWITCH = True       # Set to True if switch logic is inverted (LOW = ON)
NUM_CHANNELS = 4
POLL_INTERVAL = 1.0        # Read sensors every 1 second
CSV_ROTATE_MINUTES = 30    # Rotate CSV every 30 minutes
LOGS_DIR = os.path.join(Path.home(), "project_temp")

# ---------------------------------------------------------------------------
# Flask App
# ---------------------------------------------------------------------------
app = Flask(__name__)

# ---------------------------------------------------------------------------
# Global State
# ---------------------------------------------------------------------------
state = {
    "switch_on": False,
    "acquiring": False,
    "channels": {
        0: {"temp": 0.0, "active": False},
        1: {"temp": 0.0, "active": False},
        2: {"temp": 0.0, "active": False},
        3: {"temp": 0.0, "active": False},
    },
    "active_channels": [],
    "current_csv": "",
    "start_time": None,
    "last_read_time": None,
    "history": {0: [], 1: [], 2: [], 3: []},
    "time_labels": [],
    "cpu_temp": None,
    "cpu_history": [],
}

MAX_HISTORY = 300  # Keep last 5 minutes at 1Hz

state_lock = threading.Lock()

# ---------------------------------------------------------------------------
# CSV Management
# ---------------------------------------------------------------------------
current_csv_file = None
current_csv_writer = None
current_csv_handle = None
csv_start_time = None


def get_csv_filename():
    """Generate CSV filename: DDMMYYYY_HH_MM.csv"""
    now = datetime.now()
    # Round down to nearest 30-min block
    minute_block = (now.minute // CSV_ROTATE_MINUTES) * CSV_ROTATE_MINUTES
    return now.strftime(f"%d%m%Y_%H_{minute_block:02d}.csv")


def should_rotate_csv():
    """Check if we need a new CSV file."""
    global csv_start_time
    if csv_start_time is None:
        return True
    elapsed = (datetime.now() - csv_start_time).total_seconds()
    return elapsed >= CSV_ROTATE_MINUTES * 60


def open_new_csv(active_channels):
    """Open a new CSV file with headers for active channels."""
    global current_csv_file, current_csv_writer, current_csv_handle, csv_start_time

    # Close previous file
    if current_csv_handle:
        current_csv_handle.close()

    os.makedirs(LOGS_DIR, exist_ok=True)
    filename = get_csv_filename()
    filepath = os.path.join(LOGS_DIR, filename)

    current_csv_handle = open(filepath, "a", newline="")
    current_csv_writer = csv.writer(current_csv_handle)

    # Write header if file is new/empty. Always include CPU column.
    if os.path.getsize(filepath) == 0:
        header = ["timestamp"] + [f"ch{ch}(K)" for ch in active_channels] + ["cpu(C)"]
        current_csv_writer.writerow(header)
        current_csv_handle.flush()

    current_csv_file = filename
    csv_start_time = datetime.now()

    with state_lock:
        state["current_csv"] = filename

    print(f"[CSV] Opened: {filename} | Channels: {active_channels}")


def write_csv_row(active_channels, temps, cpu_temp):
    """Write a single row to the current CSV. Always append CPU temp as final column."""
    global current_csv_writer, current_csv_handle
    if current_csv_writer is None:
        return
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    row = [timestamp] + [f"{temps[ch]:.2f}" for ch in active_channels] + [f"{cpu_temp:.2f}" if cpu_temp is not None else ""]
    current_csv_writer.writerow(row)
    current_csv_handle.flush()


# ---------------------------------------------------------------------------
# GPIO Switch
# ---------------------------------------------------------------------------
gpio_handle = None


def setup_gpio():
    """Initialize GPIO for the SPDT switch on pin 26."""
    global gpio_handle
    if not GPIO_AVAILABLE:
        return

    try:
        # Try lgpio first (Pi 5 / newer Pi OS)
        import lgpio
        gpio_handle = lgpio.gpiochip_open(0)
        lgpio.gpio_claim_input(gpio_handle, GPIO_SWITCH_PIN, lgpio.SET_PULL_UP)
        print(f"[GPIO] lgpio: Pin {GPIO_SWITCH_PIN} configured as input (pull-up).")
    except Exception:
        try:
            # Fall back to RPi.GPIO
            import RPi.GPIO as GPIO
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(GPIO_SWITCH_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
            print(f"[GPIO] RPi.GPIO: Pin {GPIO_SWITCH_PIN} configured as input (pull-up).")
        except Exception as e:
            print(f"[GPIO] Setup failed: {e}")


def read_switch():
    """Read the SPDT switch state. Returns True if ON."""
    if TEST_MODE:
        return True  # Force ON in test mode
    
    if not GPIO_AVAILABLE:
        return True  # Default ON if no GPIO

    gpio_state = False
    try:
        import lgpio
        gpio_state = lgpio.gpio_read(gpio_handle, GPIO_SWITCH_PIN) == 1
    except Exception:
        try:
            import RPi.GPIO as GPIO
            gpio_state = GPIO.input(GPIO_SWITCH_PIN) == GPIO.HIGH
        except Exception:
            return True  # Fallback

    # Invert logic if switch is wired active-low
    return (not gpio_state) if INVERT_SWITCH else gpio_state


# ---------------------------------------------------------------------------
# MCC 134 Reader
# ---------------------------------------------------------------------------
hat = None


def setup_mcc134():
    """Initialize MCC 134 HAT and configure all channels as Type K."""
    global hat
    if not MCC134_AVAILABLE:
        return False

    try:
        hat = mcc134(0)
        for ch in range(NUM_CHANNELS):
            hat.tc_type_write(ch, TcType.TYPE_K)
        print(f"[MCC134] Initialized. All {NUM_CHANNELS} channels set to Type K.")
        return True
    except Exception as e:
        print(f"[MCC134] Init error: {e}")
        return False


def read_temperatures():
    """Read all 4 channels. Returns dict {ch: temp} and list of active channels."""
    temps = {}
    active = []

    if MCC134_AVAILABLE and hat:
        for ch in range(NUM_CHANNELS):
            try:
                t = hat.t_in_read(ch)
                temps[ch] = t
                if t > OPEN_TC_THRESHOLD:
                    active.append(ch)
            except Exception:
                temps[ch] = OPEN_TC_VALUE
    else:
        # Simulation mode: pretend Ch0 and Ch2 are active
        import random
        for ch in range(NUM_CHANNELS):
            if ch in [0, 2]:
                temps[ch] = 25.0 + random.uniform(-2, 5) + ch * 10
                active.append(ch)
            else:
                temps[ch] = OPEN_TC_VALUE

    return temps, active


# ---------------------------------------------------------------------------
# Acquisition Loop
# ---------------------------------------------------------------------------
def acquisition_loop():
    """Main loop: check switch, read sensors, log data."""
    setup_gpio()
    mcc_ok = setup_mcc134()

    if not mcc_ok and MCC134_AVAILABLE:
        print("[FATAL] Could not initialize MCC 134. Exiting acquisition.")
        return

    print("[ACQ] Acquisition thread started. Waiting for switch...")

    prev_active = []

    while True:
        try:
            switch_on = read_switch()

            with state_lock:
                state["switch_on"] = switch_on

            if not switch_on:
                with state_lock:
                    state["acquiring"] = False
                time.sleep(0.5)
                continue

            # Switch is ON — acquire data
            with state_lock:
                if not state["acquiring"]:
                    state["acquiring"] = True
                    state["start_time"] = datetime.now().isoformat()
                    print("[ACQ] Switch ON. Acquisition started.")

            temps, active = read_temperatures()
            cpu_temp = get_cpu_temperature_c()
            now_label = datetime.now().strftime("%H:%M:%S")

            # Check if we need to rotate CSV or if active channels changed
            if should_rotate_csv() or active != prev_active:
                # Always open a CSV (header will include CPU column).
                open_new_csv(active)
                prev_active = active[:]

            # Write to CSV
            # Always write a row (CPU column will be appended). When no thermocouples
            # are active, the row will contain only timestamp and CPU value.
            write_csv_row(active, temps, cpu_temp)

            # Update global state
            with state_lock:
                state["active_channels"] = active
                state["last_read_time"] = now_label

                for ch in range(NUM_CHANNELS):
                    state["channels"][ch]["temp"] = temps[ch]
                    state["channels"][ch]["active"] = ch in active

                # Maintain history
                state["time_labels"].append(now_label)
                if len(state["time_labels"]) > MAX_HISTORY:
                    state["time_labels"].pop(0)

                for ch in range(NUM_CHANNELS):
                    if ch in active:
                        state["history"][ch].append(temps[ch])
                    else:
                        state["history"][ch].append(None)
                    if len(state["history"][ch]) > MAX_HISTORY:
                        state["history"][ch].pop(0)
                # Update CPU history and current value
                state["cpu_temp"] = cpu_temp
                state["cpu_history"].append(cpu_temp if cpu_temp is not None else None)
                if len(state["cpu_history"]) > MAX_HISTORY:
                    state["cpu_history"].pop(0)

            time.sleep(POLL_INTERVAL)

        except Exception as e:
            print(f"[ACQ] Error: {e}")
            time.sleep(2)


# ---------------------------------------------------------------------------
# Flask Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/live")
def api_live():
    with state_lock:
        data = {
            "switch_on": state["switch_on"],
            "acquiring": state["acquiring"],
            "active_channels": state["active_channels"],
            "current_csv": state["current_csv"],
            "start_time": state["start_time"],
            "last_read_time": state["last_read_time"],
            "channels": {},
            "history": {},
            "time_labels": state["time_labels"][-60:],  # Last 60 points
        }
        for ch in range(NUM_CHANNELS):
            data["channels"][ch] = {
                "temp": state["channels"][ch]["temp"],
                "active": state["channels"][ch]["active"],
            }
        for ch in state["active_channels"]:
            data["history"][ch] = state["history"][ch][-60:]
        # CPU temperature info
        data["cpu_temp"] = state.get("cpu_temp")
        data["cpu_history"] = state.get("cpu_history", [])[-60:]
    return jsonify(data)


@app.route("/api/logs")
def api_logs():
    os.makedirs(LOGS_DIR, exist_ok=True)
    files = sorted(
        [f for f in os.listdir(LOGS_DIR) if f.endswith(".csv")],
        reverse=True
    )
    return jsonify({"files": files})


@app.route("/api/export/<filename>")
def api_export(filename):
    return send_from_directory(LOGS_DIR, filename, as_attachment=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    os.makedirs(LOGS_DIR, exist_ok=True)
    print("=" * 50)
    print("  Data Acquisition System")
    print(f"  MCC 134: {'Available' if MCC134_AVAILABLE else 'SIMULATION'}")
    print(f"  GPIO: {'Available' if GPIO_AVAILABLE else 'SIMULATION (always ON)'}")
    if TEST_MODE:
        print("  Mode: TEST MODE (Switch bypassed - Always ON)")
    if INVERT_SWITCH:
        print("  Switch Logic: INVERTED (LOW = ON)")
    print(f"  Logs: {LOGS_DIR}")
    print("=" * 50)

    # Start acquisition in background thread
    acq_thread = threading.Thread(target=acquisition_loop, daemon=True)
    acq_thread.start()

    # Start Flask
    app.run(host="0.0.0.0", port=5000, debug=False)
