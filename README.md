# Data Acquisition System

Standalone thermocouple data acquisition for **Raspberry Pi 4 + MCC 134 HAT**.

## Features
- **4-Channel Type K** thermocouple reading via MCC 134.
- **Dynamic channel detection** — only active channels are plotted and logged.
- **SPST switch on GPIO 23** — toggle data acquisition ON/OFF.
- **30-minute CSV rotation** — files saved as `DDMMYYYY_HH_MM.csv`.
- **Live web dashboard** on `http://localhost:5000`.

## Quick Start

```bash
cd /home/pi/Desktop/data_acquisition
bash start.sh
```

Then open `http://localhost:5000` in a browser.

## Auto-Start on Boot

```bash
sudo cp data_acquisition.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable data_acquisition.service
sudo systemctl start data_acquisition.service
```

Notes:
- The systemd service launches `start.sh`, which runs `app.py` from the project directory and prints a brief banner.
- Stop/restart: `sudo systemctl stop|restart data_acquisition.service`

Check status:
```bash
sudo systemctl status data_acquisition.service
```

## CSV Logs

Saved to `/home/pi/project_temp/`. Naming: `DDMMYYYY_HH_MM.csv`.

Only columns for **active channels** are written. Example:
```
timestamp,ch0(K),ch2(K)
2026-02-21 16:30:01,85.23,42.10
```

## Wiring

| Component | Pin |
|---|---|
| MCC 134 HAT | Stacked on GPIO header |
| SPST Switch | GPIO 23 (BCM) |
| Switch GND | Any GND pin |
| Switch VCC | 3.3V |
