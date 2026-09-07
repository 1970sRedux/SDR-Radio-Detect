#!/usr/bin/env python3
"""
sdr_detect — passive RF presence monitor for a single RTL-SDR (R820T2).

What this does
  - Names common 315/433/868/915 MHz sensors and remotes via rtl_433
  - Builds a baseline of *your* devices so new IDs become leads
  - Sweeps other bands with rtl_power for energy only (continuous vs bursty)
  - Health-checks FM / weather radio so you know the stick works
  - Optional ADS-B smoke test

What this does not do
  - Decode phone, Wi-Fi, Bluetooth, or pager *content*
  - Detect 2.4/5.8 GHz cameras or typical consumer drones
  - Direction-find or identify a person
  - Tell a Stingray from a normal cell site

Requires: rtl-sdr (rtl_power, rtl_test), rtl-433. Optional: dump1090-mutability.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_STATE = SCRIPT_DIR / "state"
DEFAULT_KNOWN = SCRIPT_DIR / "known_devices.json"

# Quarter-wave reminders for a stock telescopic whip (cm, connector to tip).
WHIP_CM = {
    "FM": "70-85",
    "NOAA_WX": "46",
    "ISM_315": "24",
    "ISM_433": "17",
    "ISM_868": "9",
    "ISM_915": "8",
    "FRS_GMRS": "16",
    "PMR446": "17",
    "PAGERS": "8-20 (band dependent)",
    "ANALOG_VIDEO_12": "6-7 (collapse the whip)",
    "ADSB": "7 (collapse the whip)",
    "GPS_L1": "5 (collapse the whip)",
    "GSM": "8-9",
    "LTE": "4-10 (band dependent)",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def run(cmd: list[str], timeout: int | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


# ---------------------------------------------------------------------------
# Band plans — energy surveys (rtl_power). rtl_433 handles ISM identification.
# ---------------------------------------------------------------------------

def band_plan(region: str) -> list[dict[str, Any]]:
    """rtl_power -f strings. Keep spans modest; the stick only sees ~2.4 MHz at once."""
    common = [
        {
            "id": "FM",
            "title": "Broadcast FM (health / overload)",
            "rtl_power": "88M:108M:100k",
            "kind": "health",
            "expect": "strong continuous multiplexes — not a threat",
            "whip": WHIP_CM["FM"],
        },
        {
            "id": "NOAA_WX",
            "title": "NOAA / weather radio (health)",
            "rtl_power": "162.400M:162.550M:5k",
            "kind": "health",
            "expect": "one or two continuous NFM carriers if you are in range",
            "whip": WHIP_CM["NOAA_WX"],
        },
        {
            "id": "ANALOG_VIDEO_12",
            "title": "Analog video window 1.18-1.30 GHz",
            "rtl_power": "1180M:1300M:100k",
            "kind": "wide_continuous",
            "expect": "quiet unless a nearby analog VTX is on; look for a fat plateau",
            "whip": WHIP_CM["ANALOG_VIDEO_12"],
        },
        {
            "id": "ADSB",
            "title": "ADS-B 1090 MHz (radio test, not drones)",
            "rtl_power": "1089M:1091M:10k",
            "kind": "pulse_energy",
            "expect": "raised floor / chirps if aircraft are visible",
            "whip": WHIP_CM["ADSB"],
        },
        {
            "id": "GPS_L1",
            "title": "GPS L1 jam watch 1575.42 MHz",
            "rtl_power": "1574.0M:1576.8M:10k",
            "kind": "jam_watch",
            "expect": "no sharp carrier if healthy; a loud dirty spike is interference",
            "whip": WHIP_CM["GPS_L1"],
        },
    ]

    us = [
        {
            "id": "ISM_315",
            "title": "US ISM 315 MHz (fobs, alarms, some TPMS)",
            "rtl_power": "314.0M:316.5M:10k",
            "kind": "burst_expected",
            "expect": "quiet until a button or sensor fires",
            "whip": WHIP_CM["ISM_315"],
        },
        {
            "id": "ISM_433",
            "title": "433.92 MHz (also common in the US)",
            "rtl_power": "433.0M:434.8M:10k",
            "kind": "burst_expected",
            "expect": "weather sensors / some remotes; periodic speckles",
            "whip": WHIP_CM["ISM_433"],
        },
        {
            "id": "ISM_915",
            "title": "US 902-928 MHz ISM",
            "rtl_power": "902M:928M:50k",
            "kind": "mixed",
            "expect": "meters, some cordless, occasional video",
            "whip": WHIP_CM["ISM_915"],
        },
        {
            "id": "FRS_GMRS",
            "title": "FRS/GMRS handhelds",
            "rtl_power": "462.0M:467.8M:25k",
            "kind": "voice_energy",
            "expect": "empty unless someone is talking nearby",
            "whip": WHIP_CM["FRS_GMRS"],
        },
        {
            "id": "PAGERS_US",
            "title": "US paging cluster 929-932 MHz (energy only)",
            "rtl_power": "929M:932M:10k",
            "kind": "energy_only",
            "expect": "bursts if a paging transmitter is local — no content decode",
            "whip": WHIP_CM["PAGERS"],
        },
        {
            "id": "GSM850_DL",
            "title": "GSM/LTE occupancy ~850 downlink (not identity)",
            "rtl_power": "869M:894M:50k",
            "kind": "occupancy",
            "expect": "channel slabs if a site is nearby",
            "whip": WHIP_CM["GSM"],
        },
        {
            "id": "GSM900_DL",
            "title": "GSM 900 downlink occupancy",
            "rtl_power": "925M:960M:50k",
            "kind": "occupancy",
            "expect": "channel slabs if used in your area",
            "whip": WHIP_CM["GSM"],
        },
    ]

    eu = [
        {
            "id": "ISM_433",
            "title": "433.92 MHz ISM (sensors, remotes, fobs)",
            "rtl_power": "433.0M:434.8M:10k",
            "kind": "burst_expected",
            "expect": "the highest-value detection band on this stick",
            "whip": WHIP_CM["ISM_433"],
        },
        {
            "id": "ISM_868",
            "title": "868 MHz SRD / LoRa / some alarms",
            "rtl_power": "863M:870M:25k",
            "kind": "mixed",
            "expect": "short frames; LoRa looks like a chirp not an OOK speckle",
            "whip": WHIP_CM["ISM_868"],
        },
        {
            "id": "PMR446",
            "title": "PMR446 handhelds",
            "rtl_power": "446.0M:446.2M:6.25k",
            "kind": "voice_energy",
            "expect": "empty unless someone is talking nearby",
            "whip": WHIP_CM["PMR446"],
        },
        {
            "id": "GSM900_DL",
            "title": "GSM 900 downlink occupancy (not identity)",
            "rtl_power": "925M:960M:50k",
            "kind": "occupancy",
            "expect": "channel slabs from a normal cell site",
            "whip": WHIP_CM["GSM"],
        },
    ]

    extra = common
    return (us if region == "us" else eu) + extra


# ---------------------------------------------------------------------------
# Device identity (rtl_433 JSON)
# ---------------------------------------------------------------------------

def device_key(msg: dict[str, Any]) -> str:
    model = str(msg.get("model") or msg.get("protocol") or "unknown")
    ident = msg.get("id", msg.get("sid", msg.get("device", "")))
    channel = msg.get("channel", "")
    return f"{model}|{ident}|{channel}"


def load_known(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "updated": None, "devices": {}}
    with path.open() as f:
        data = json.load(f)
    data.setdefault("devices", {})
    return data


def save_known(path: Path, data: dict[str, Any]) -> None:
    data["updated"] = utc_now()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
    tmp.replace(path)


def summarize_433_message(msg: dict[str, Any]) -> dict[str, Any]:
    return {
        "time": msg.get("time") or utc_now(),
        "model": msg.get("model"),
        "id": msg.get("id", msg.get("sid")),
        "channel": msg.get("channel"),
        "freq_mhz": msg.get("freq") or msg.get("frequency"),
        "mod": msg.get("mod"),
        "rssi": msg.get("rssi"),
        "snr": msg.get("snr"),
        "key": device_key(msg),
        "raw_keys": sorted(k for k in msg.keys() if k not in {"mic", "codes"}),
    }


def rtl_433_cmd(args: argparse.Namespace, duration_s: int | None) -> list[str]:
    cmd = [
        "rtl_433",
        "-F", "json",
        "-M", "time:iso",
        "-M", "level",
        "-g", str(args.gain),
    ]
    if args.ppm:
        cmd += ["-p", str(args.ppm)]
    if args.device is not None:
        cmd += ["-d", str(args.device)]
    # Frequency hops rtl_433 already knows. Pin if the user asked.
    if args.freq:
        cmd += ["-f", args.freq]
    if duration_s:
        cmd += ["-T", str(duration_s)]
    return cmd


def collect_rtl_433(args: argparse.Namespace, duration_s: int) -> list[dict[str, Any]]:
    if not have("rtl_433"):
        raise SystemExit(
            "rtl_433 not found. On Debian: sudo apt install rtl-433"
        )
    cmd = rtl_433_cmd(args, duration_s)
    print(f"[*] {' '.join(cmd)}", file=sys.stderr)
    proc = run(cmd, timeout=duration_s + 30)
    if proc.returncode not in (0, 124) and proc.stderr and "No supported devices" in proc.stderr:
        raise SystemExit(proc.stderr.strip())
    events = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        events.append(summarize_433_message(msg))
    if proc.stderr:
        # rtl_433 prints tuner info to stderr; keep the last few lines.
        tail = [ln for ln in proc.stderr.splitlines() if ln.strip()][-8:]
        for ln in tail:
            print(f"[rtl_433] {ln}", file=sys.stderr)
    return events


def watch_rtl_433(args: argparse.Namespace, known_path: Path) -> None:
    if not have("rtl_433"):
        raise SystemExit("rtl_433 not found. On Debian: sudo apt install rtl-433")
    known = load_known(known_path)
    cmd = rtl_433_cmd(args, duration_s=None)
    print(f"[*] watch: {' '.join(cmd)}", file=sys.stderr)
    print("[*] Ctrl+C to stop. Unknown device IDs print as LEAD.", file=sys.stderr)
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line.startswith("{"):
                if args.verbose and line:
                    print(f"[rtl_433] {line}", file=sys.stderr)
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            ev = summarize_433_message(msg)
            entry = known["devices"].get(ev["key"])
            if entry:
                entry["last_seen"] = ev["time"]
                entry["seen_count"] = int(entry.get("seen_count", 0)) + 1
                tag = entry.get("label") or "known"
                print(f"KNOWN  {ev['time']}  {ev['key']}  rssi={ev.get('rssi')}  ({tag})")
            else:
                print(
                    f"LEAD   {ev['time']}  {ev['key']}  "
                    f"freq={ev.get('freq_mhz')} rssi={ev.get('rssi')}  "
                    f"— not in {known_path.name}"
                )
                if args.auto_learn:
                    known["devices"][ev["key"]] = {
                        "first_seen": ev["time"],
                        "last_seen": ev["time"],
                        "seen_count": 1,
                        "label": "",
                        "sample": ev,
                    }
                    save_known(known_path, known)
    except KeyboardInterrupt:
        print("\n[*] stopped", file=sys.stderr)
    finally:
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        save_known(known_path, known)


# ---------------------------------------------------------------------------
# Energy surveys (rtl_power)
# ---------------------------------------------------------------------------

def parse_rtl_power(text: str) -> list[dict[str, Any]]:
    """Parse rtl_power CSV rows into per-row peak/median dBm."""
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 7:
            continue
        try:
            low = float(parts[2])
            high = float(parts[3])
            step = float(parts[4])
            dbms = [float(x) for x in parts[6:] if x]
        except ValueError:
            continue
        if not dbms:
            continue
        peak_i = max(range(len(dbms)), key=lambda i: dbms[i])
        rows.append(
            {
                "low_hz": low,
                "high_hz": high,
                "step_hz": step,
                "n": len(dbms),
                "median_dbm": statistics.median(dbms),
                "mean_dbm": statistics.fmean(dbms),
                "peak_dbm": dbms[peak_i],
                "peak_hz": low + peak_i * step,
                "p90_dbm": statistics.quantiles(dbms, n=10)[8] if len(dbms) >= 10 else max(dbms),
                "spread_db": max(dbms) - statistics.median(dbms),
            }
        )
    return rows


def classify_energy(band: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {
            "status": "no_data",
            "detail": "rtl_power produced no rows (device busy, bad rate, or timeout)",
        }
    med = statistics.median(r["median_dbm"] for r in rows)
    peak = max(r["peak_dbm"] for r in rows)
    peak_row = max(rows, key=lambda r: r["peak_dbm"])
    spread = peak - med
    kind = band["kind"]

    # Heuristics are coarse on purpose: 8-bit + unknown antenna + unknown site noise.
    if kind == "health":
        if peak > med + 15:
            status, detail = "ok_signals", "strong carriers present — radio path looks alive"
        elif peak > med + 8:
            status, detail = "weak_signals", "something is there; antenna/placement may be limiting"
        else:
            status, detail = "quiet", "no strong broadcast — wrong region, bad antenna, or dead stick"
    elif kind == "jam_watch":
        # Healthy GPS is spread under the noise. A spike is the interesting case.
        if spread >= 12:
            status, detail = "lead_interference", (
                f"sharp energy {spread:.1f} dB above the local floor at "
                f"{peak_row['peak_hz']/1e6:.3f} MHz — check for a nearby interferer, "
                f"not a satellite"
            )
        else:
            status, detail = "no_spike", (
                "no dominant carrier in the L1 window (normal). "
                "This whip is a poor GNSS sensor; only loud jammers would show."
            )
    elif kind == "wide_continuous":
        if spread >= 12 and peak_row["spread_db"] >= 8:
            status, detail = "lead_wide_energy", (
                f"elevated plateau/peak at {peak_row['peak_hz']/1e6:.3f} MHz "
                f"(+{spread:.1f} dB). Collapse the whip and inspect in GQRX — "
                f"analog video is a smear, Wi-Fi is not in this band."
            )
        else:
            status, detail = "quiet", "no fat persistent smear in this 120 MHz window"
    elif kind in ("burst_expected", "mixed", "voice_energy", "energy_only", "pulse_energy"):
        if spread >= 10:
            status, detail = "energy_present", (
                f"peak {peak:.1f} dBm at {peak_row['peak_hz']/1e6:.3f} MHz "
                f"(+{spread:.1f} dB vs floor). For ISM, use inventory/watch to name it."
            )
        else:
            status, detail = "quiet", "no stand-out carrier during this short dwell"
    elif kind == "occupancy":
        if spread >= 8:
            status, detail = "occupied", (
                "channel-like energy present — a cell site or similar. "
                "This is occupancy, not a device fingerprint."
            )
        else:
            status, detail = "quiet", "no obvious downlink slab in this snapshot"
    else:
        status, detail = "measured", f"peak {peak:.1f} dBm, floor ~{med:.1f} dBm"

    return {
        "status": status,
        "detail": detail,
        "floor_dbm": round(med, 1),
        "peak_dbm": round(peak, 1),
        "peak_mhz": round(peak_row["peak_hz"] / 1e6, 4),
        "spread_db": round(spread, 1),
        "rows": len(rows),
    }


def survey_band(args: argparse.Namespace, band: dict[str, Any], out_dir: Path) -> dict[str, Any]:
    if not have("rtl_power"):
        raise SystemExit("rtl_power not found. On Debian: sudo apt install rtl-sdr")
    csv_path = out_dir / f"{band['id']}.csv"
    cmd = [
        "rtl_power",
        "-f", band["rtl_power"],
        "-g", str(args.gain),
        "-i", str(args.integrate),
        "-e", f"{args.dwell}s",
        "-1",
        str(csv_path),
    ]
    if args.ppm:
        cmd[1:1] = ["-p", str(args.ppm)]
    if args.device is not None:
        cmd += ["-d", str(args.device)]
    print(f"[*] {band['id']}: {' '.join(cmd)}", file=sys.stderr)
    print(f"    whip ≈ {band['whip']} cm   ({band['expect']})", file=sys.stderr)
    proc = run(cmd, timeout=args.dwell + 60)
    text = csv_path.read_text() if csv_path.exists() else proc.stdout
    if proc.returncode != 0 and not text:
        return {
            "id": band["id"],
            "title": band["title"],
            "status": "error",
            "detail": (proc.stderr or proc.stdout or "rtl_power failed").strip()[:400],
            "whip_cm": band["whip"],
        }
    rows = parse_rtl_power(text)
    result = classify_energy(band, rows)
    result.update(
        {
            "id": band["id"],
            "title": band["title"],
            "kind": band["kind"],
            "expect": band["expect"],
            "whip_cm": band["whip"],
            "csv": str(csv_path),
        }
    )
    return result


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

def print_inventory(events: list[dict[str, Any]], known: dict[str, Any]) -> None:
    by_key: dict[str, list[dict[str, Any]]] = {}
    for ev in events:
        by_key.setdefault(ev["key"], []).append(ev)
    print()
    print(f"ISM inventory  ({len(events)} frames, {len(by_key)} distinct keys)")
    print("-" * 72)
    if not by_key:
        print("  (none)  — no rtl_433 decodes. Check gain, whip length, and that")
        print("            something in the house actually transmits on 315/433/868/915.")
        return
    for key, group in sorted(by_key.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        last = group[-1]
        meta = known["devices"].get(key, {})
        label = meta.get("label") or ""
        flag = "KNOWN" if key in known["devices"] else "NEW "
        print(
            f"  {flag}  n={len(group):3d}  {key}"
            + (f"  [{label}]" if label else "")
        )
        print(
            f"         last {last.get('time')}  freq={last.get('freq_mhz')}  "
            f"rssi={last.get('rssi')}"
        )


def print_survey(results: list[dict[str, Any]]) -> None:
    print()
    print("Energy survey")
    print("-" * 72)
    for r in results:
        print(f"  [{r.get('status','?'):18}]  {r['id']}: {r['title']}")
        print(f"      whip ≈ {r.get('whip_cm')} cm")
        if "peak_mhz" in r:
            print(
                f"      floor={r.get('floor_dbm')} dBm  "
                f"peak={r.get('peak_dbm')} dBm @ {r.get('peak_mhz')} MHz  "
                f"Δ={r.get('spread_db')} dB"
            )
        print(f"      {r.get('detail')}")


def write_report(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
    print(f"\n[*] wrote {path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Passive RTL-SDR detector: ISM inventory + band energy survey",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "mode",
        choices=["check", "inventory", "learn", "watch", "survey", "health", "full"],
        help=(
            "check=tools/dongle; inventory=rtl_433 snapshot; learn=add snapshot to baseline; "
            "watch=live unknown-ID alerts; survey=rtl_power bands; health=FM+WX only; "
            "full=inventory+survey"
        ),
    )
    p.add_argument("--region", choices=["us", "eu"], default="us",
                   help="ISM / handheld / GSM band plan")
    p.add_argument("--seconds", type=int, default=180,
                   help="rtl_433 listen time for inventory/learn/full")
    p.add_argument("--gain", default="20",
                   help="tuner gain in dB, or 'auto'. Start low in cities.")
    p.add_argument("--ppm", type=int, default=0, help="frequency correction")
    p.add_argument("-d", "--device", type=int, default=None, help="rtl device index")
    p.add_argument("-f", "--freq", default=None,
                   help="pin rtl_433 to one frequency, e.g. 433.92M")
    p.add_argument("--dwell", type=int, default=20,
                   help="seconds rtl_power spends on each survey band")
    p.add_argument("--integrate", default="5",
                   help="rtl_power integration interval")
    p.add_argument("--known", type=Path, default=DEFAULT_KNOWN,
                   help="baseline JSON of your devices")
    p.add_argument("--state", type=Path, default=DEFAULT_STATE,
                   help="directory for csv/json outputs")
    p.add_argument("--auto-learn", action="store_true",
                   help="watch mode: append every new key to the baseline")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def cmd_check() -> int:
    print("Tooling")
    for name in ("rtl_test", "rtl_power", "rtl_433", "dump1090-mutability", "dump1090"):
        print(f"  {name:22} {'yes' if have(name) else 'NO'}")
    if have("rtl_test"):
        print("\nrtl_test (3s)…")
        proc = run(["rtl_test", "-t"], timeout=8)
        # rtl_test -t exits after a tuner probe on many builds; show output either way.
        out = (proc.stdout or "") + (proc.stderr or "")
        print(out[-1500:] if out else "(no output)")
    else:
        print("\nInstall tools:  sudo apt install rtl-sdr rtl-433")
    print(
        "\nAntenna reminder: stock telescopic is a monopole. Set length to ~¼-wave,\n"
        "use a USB extension, keep it off the laptop. Collapse to ~7 cm for 1.2 GHz / ADS-B / GPS."
    )
    return 0


def main() -> int:
    args = build_parser().parse_args()
    args.state.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    if args.mode == "check":
        return cmd_check()

    if args.mode == "watch":
        watch_rtl_433(args, args.known)
        return 0

    payload: dict[str, Any] = {
        "when": utc_now(),
        "mode": args.mode,
        "region": args.region,
        "gain": args.gain,
        "ppm": args.ppm,
        "disclaimer": (
            "Passive energy/identity of unlicensed ISM and public beacons only. "
            "Not a TSCM sweep. Does not cover Wi-Fi/BLE/2.4/5.8 GHz."
        ),
    }

    if args.mode in ("inventory", "learn", "full"):
        events = collect_rtl_433(args, args.seconds)
        known = load_known(args.known)
        payload["ism_frames"] = events
        payload["ism_keys"] = sorted({e["key"] for e in events})
        print_inventory(events, known)
        if args.mode == "learn":
            added = 0
            for ev in events:
                if ev["key"] not in known["devices"]:
                    known["devices"][ev["key"]] = {
                        "first_seen": ev["time"],
                        "last_seen": ev["time"],
                        "seen_count": 1,
                        "label": "",
                        "sample": ev,
                    }
                    added += 1
                else:
                    rec = known["devices"][ev["key"]]
                    rec["last_seen"] = ev["time"]
                    rec["seen_count"] = int(rec.get("seen_count", 0)) + 1
            save_known(args.known, known)
            print(f"\n[*] baseline {args.known}  new keys added: {added}  total: {len(known['devices'])}")
            print("    Edit that JSON and set a 'label' on each device you recognize.")

    if args.mode in ("survey", "health", "full"):
        bands = band_plan(args.region)
        if args.mode == "health":
            bands = [b for b in bands if b["kind"] == "health"]
        results = []
        for band in bands:
            results.append(survey_band(args, band, args.state / stamp))
            time.sleep(0.3)
        payload["survey"] = results
        print_survey(results)

    report = args.state / f"report_{args.mode}_{stamp}.json"
    write_report(report, payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
