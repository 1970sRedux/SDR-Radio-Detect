#!/usr/bin/env python3
"""
Live presence + signal-strength monitor for selected bands.

  analog wireless mics / garage remotes / pagers /
  FRS GMRS PMR446 ham FM / ADS-B 1090 /
  GSM 850-900 / LTE below 1.76 GHz

Hops one RTL-SDR through those windows with rtl_power.
Prints a strength bar every visit. ALERT on quiet → active.

  python3 sdr_alert.py --region us --gain 20
  python3 sdr_alert.py --region eu --gain 20 --ppm 40
"""

from __future__ import annotations

import argparse
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path


def have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def bar(spread: float, width: int = 12) -> str:
    n = int(max(0.0, min(width, spread / 2.0)))
    return "█" * n + "░" * (width - n)


def parse_power(text: str) -> tuple[float, float, float] | None:
    """Return floor_dbm, peak_dbm, peak_mhz or None."""
    floors, peaks, peak_hz = [], [], []
    for line in text.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 7:
            continue
        try:
            low = float(parts[2])
            step = float(parts[4])
            dbms = [float(x) for x in parts[6:] if x]
        except ValueError:
            continue
        if not dbms:
            continue
        floors.append(statistics.median(dbms))
        i = max(range(len(dbms)), key=lambda k: dbms[k])
        peaks.append(dbms[i])
        peak_hz.append(low + i * step)
    if not peaks:
        return None
    floor = statistics.median(floors)
    idx = max(range(len(peaks)), key=lambda k: peaks[k])
    return floor, peaks[idx], peak_hz[idx] / 1e6


def targets(region: str) -> list[dict]:
    remotes_us = [
        {"id": "REMOTE315", "title": "garage / gate / fob 315",
         "f": "314.2M:316.2M:8k", "thr": 9, "whip": 24, "priority": 1},
        {"id": "REMOTE433", "title": "garage / gate / fob 433.92",
         "f": "433.3M:434.5M:8k", "thr": 9, "whip": 17, "priority": 1},
    ]
    remotes_eu = [
        {"id": "REMOTE433", "title": "garage / gate / fob 433.92",
         "f": "433.3M:434.5M:8k", "thr": 9, "whip": 17, "priority": 1},
        {"id": "REMOTE868", "title": "garage / gate / SRD 868",
         "f": "868.0M:869.2M:8k", "thr": 9, "whip": 9, "priority": 1},
    ]
    mics_us = [
        {"id": "MIC_VHF", "title": "analog wireless mic VHF",
         "f": "169M:216M:50k", "thr": 12, "whip": 40, "priority": 2},
        {"id": "MIC_900", "title": "analog wireless mic / 900 ISM",
         "f": "902M:928M:50k", "thr": 12, "whip": 8, "priority": 2},
    ]
    mics_eu = [
        {"id": "MIC_VHF", "title": "analog wireless mic VHF",
         "f": "174M:230M:50k", "thr": 12, "whip": 40, "priority": 2},
        {"id": "MIC_863", "title": "analog wireless mic 863-865",
         "f": "863.0M:865.0M:10k", "thr": 12, "whip": 9, "priority": 2},
    ]
    voice_us = [
        {"id": "HAM2M", "title": "ham FM 2 m",
         "f": "144.0M:148.0M:12k", "thr": 10, "whip": 51, "priority": 2},
        {"id": "FRS_GMRS", "title": "FRS / GMRS",
         "f": "462.0M:467.8M:12k", "thr": 10, "whip": 16, "priority": 2},
        {"id": "HAM70", "title": "ham FM 70 cm",
         "f": "440.0M:450.0M:20k", "thr": 10, "whip": 17, "priority": 2},
    ]
    voice_eu = [
        {"id": "HAM2M", "title": "ham FM 2 m",
         "f": "144.0M:146.0M:10k", "thr": 10, "whip": 51, "priority": 2},
        {"id": "PMR446", "title": "PMR446",
         "f": "446.0M:446.2M:6.25k", "thr": 10, "whip": 17, "priority": 2},
        {"id": "HAM70", "title": "ham FM 70 cm",
         "f": "430.0M:440.0M:20k", "thr": 10, "whip": 17, "priority": 2},
    ]
    pagers_us = [
        {"id": "PAGER_VHF", "title": "pagers VHF",
         "f": "152.0M:159.0M:10k", "thr": 10, "whip": 48, "priority": 2},
        {"id": "PAGER_900", "title": "pagers 929-932",
         "f": "929.0M:932.0M:8k", "thr": 10, "whip": 8, "priority": 2},
    ]
    pagers_eu = [
        {"id": "PAGER_VHF", "title": "pagers VHF",
         "f": "153.0M:158.0M:10k", "thr": 10, "whip": 48, "priority": 2},
        {"id": "PAGER_UHF", "title": "pagers UHF",
         "f": "466.0M:470.0M:10k", "thr": 10, "whip": 16, "priority": 2},
    ]
    common_hi = [
        {"id": "ADSB", "title": "ADS-B 1090",
         "f": "1089.0M:1091.0M:8k", "thr": 6, "whip": 7, "priority": 2},
        {"id": "GSM850", "title": "GSM 850 downlink",
         "f": "869M:894M:40k", "thr": 8, "whip": 9, "priority": 3},
        {"id": "GSM900", "title": "GSM 900 downlink",
         "f": "925M:960M:40k", "thr": 8, "whip": 8, "priority": 3},
        {"id": "LTE700", "title": "LTE 700",
         "f": "728M:768M:40k", "thr": 8, "whip": 10, "priority": 3},
        {"id": "LTE_AWS_UL", "title": "LTE AWS uplink 1710-1755",
         "f": "1710M:1755M:50k", "thr": 8, "whip": 4, "priority": 3},
    ]
    if region == "eu":
        return remotes_eu + mics_eu + voice_eu + pagers_eu + common_hi
    return remotes_us + mics_us + voice_us + pagers_us + common_hi


def schedule(items: list[dict]) -> list[dict]:
    """Visit remotes twice per cycle so a button press is less likely to be missed."""
    hot = [t for t in items if t["priority"] == 1]
    rest = [t for t in items if t["priority"] != 1]
    ordered = []
    mid = max(1, len(rest) // 2)
    ordered.extend(hot)
    ordered.extend(rest[:mid])
    ordered.extend(hot)
    ordered.extend(rest[mid:])
    return ordered


def rtl_power_once(tgt: dict, gain: str, ppm: int, device: int | None,
                   dwell: int, integrate: str) -> tuple[float, float, float] | None:
    tmp = Path(tempfile.mkstemp(prefix="sdr_alert_", suffix=".csv")[1])
    cmd = [
        "rtl_power",
        "-f", tgt["f"],
        "-g", str(gain),
        "-i", str(integrate),
        "-e", f"{dwell}s",
        "-1",
        str(tmp),
    ]
    if ppm:
        cmd[1:1] = ["-p", str(ppm)]
    if device is not None:
        cmd += ["-d", str(device)]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=dwell + 45, check=False
        )
        text = tmp.read_text() if tmp.exists() else proc.stdout
    except subprocess.TimeoutExpired:
        return None
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
    if not text:
        return None
    return parse_power(text)


def main() -> int:
    p = argparse.ArgumentParser(description="Presence + RSSI alerts on selected bands")
    p.add_argument("--region", choices=["us", "eu"], default="us")
    p.add_argument("--gain", default="20")
    p.add_argument("--ppm", type=int, default=0)
    p.add_argument("-d", "--device", type=int, default=None)
    p.add_argument("--dwell", type=int, default=8,
                   help="seconds per window (wide windows take longer internally)")
    p.add_argument("--integrate", default="2")
    p.add_argument("--only", default=None,
                   help="comma ids to include, e.g. REMOTE433,ADSB,GSM900")
    p.add_argument("--beep", action="store_true")
    args = p.parse_args()

    if not have("rtl_power"):
        print("rtl_power not found.  sudo apt install rtl-sdr", file=sys.stderr)
        return 1

    items = targets(args.region)
    if args.only:
        want = {x.strip().upper() for x in args.only.split(",") if x.strip()}
        items = [t for t in items if t["id"] in want]
        if not items:
            print("no matching --only ids", file=sys.stderr)
            return 1

    print(f"# sdr_alert  region={args.region}  gain={args.gain}  ppm={args.ppm}")
    print("# whip: remotes 17/24 cm · voice ~16-51 cm · ADS-B/LTE collapse to 5-7 cm")
    print("# ALERT fires on quiet→active. Ctrl+C to stop.")
    print()
    sys.stdout.flush()

    prev: dict[str, str] = {}
    cycle = schedule(items)
    try:
        while True:
            for tgt in cycle:
                now = datetime.now().strftime("%H:%M:%S")
                meas = rtl_power_once(
                    tgt, args.gain, args.ppm, args.device, args.dwell, args.integrate
                )
                if meas is None:
                    print(f"{now}  {tgt['id']:<12}  ----    no data")
                    sys.stdout.flush()
                    continue
                floor, peak, mhz = meas
                spread = peak - floor
                active = spread >= tgt["thr"]
                state = "ACTIVE" if active else "quiet"
                line = (
                    f"{now}  {tgt['id']:<12}  {state:<6}  "
                    f"{peak:6.1f} dBm  {bar(spread)}  "
                    f"{mhz:10.4f} MHz  Δ{spread:4.1f}  {tgt['title']}"
                )
                print(line)
                if active and prev.get(tgt["id"]) != "ACTIVE":
                    alert = (
                        f"{now}  ALERT  {tgt['title']}  "
                        f"{peak:.1f} dBm @ {mhz:.4f} MHz  "
                        f"(floor {floor:.1f}, +{spread:.1f} dB)"
                    )
                    print(alert)
                    if args.beep:
                        sys.stdout.write("\a")
                prev[tgt["id"]] = state
                sys.stdout.flush()
    except KeyboardInterrupt:
        print("\n# stopped")
        return 0


if __name__ == "__main__":
    sys.exit(main())
