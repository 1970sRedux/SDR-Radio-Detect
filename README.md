# SDR-Radio-Detect

This is a theoretical (not fully tested) sdr radio detection app using python.

It's for a Linux OS, tested in Debian.

Passive monitor for a cheap R820T2 RTL-SDR and a stock telescopic antenna. 
Name ISM sensors, baseline *your* devices, flag new IDs; scan other bands...


## Install (Debian)

```bash
sudo apt install rtl-sdr rtl-433
# sudo apt install dump1090-mutability
```

Plug the dongle in, then:

```bash
rtl_test          # should print Rafael Micro R820T2 or something similar
python3 sdr_detect.py check
```

If `No supported devices found`, fix udev / `plugdev` first. Only one program can own the stick (close GQRX or anything else using your radio).

## Modes

| Command | What it does |
|---|---|
| `check` | Are `rtl_433` / `rtl_power` installed? Can the dongle be seen? |
| `inventory` | Listen with `rtl_433`, print every decoded 315/433/868/915 device |
| `learn` | Same listen, **write new IDs** into `known_devices.json` (this is your house inventory) |
| `watch` | Live stream: `KNOWN` vs `LEAD` for IDs not in the baseline |
| `health` | Short `rtl_power` on FM + weather radio — proves the radio path works |
| `survey` | Energy sweep of the rest of the list (900 ISM, 1.2 GHz video window, GPS L1, GSM occupancy, ADS-B, handhelds, pagers-as-energy) |
| `full` | `inventory` + `survey` |

Examples:

```bash
# US band plan, 3 minute sensor census
python3 sdr_detect.py inventory --region us --seconds 180 --gain 20

# After you recognize those IDs, freeze them as "yours"
python3 sdr_detect.py learn --region us --seconds 600 --gain 20

# Then leave this running while you walk the house / press fobs
python3 sdr_detect.py watch --region us --gain 20

# Energy-only look at the non-ISM rows (collapse whip for 1.2 GHz / 1090 / GPS)
python3 sdr_detect.py survey --region eu --dwell 15 --gain 20

python3 sdr_detect.py full --region us --seconds 120 --dwell 15
```

`--region eu` switches the plan to 433 + 868 + PMR446 + GSM900 instead of 315 + 915 + FRS/GMRS.

`--ppm 50` if you already measured a crystal offset. `--gain 12` in a dense FM/GSM area.


## Whip length (stock telescopic)

Set length *before* the matching mode. Measure connector to tip.

| Band | Length |
|---|---|
| 433.92 MHz inventory/watch | ~17 cm |
| 315 MHz | ~24 cm |
| 868 / 915 | ~8–9 cm |
| FM / health | ~75 cm |
| 1.2 GHz analog video, ADS-B, GPS L1 | collapse to ~6–7 cm |
| Put the dongle on a USB extension, not on the laptop | — |

## Baseline file

`learn` and `watch --auto-learn` write `known_devices.json`. Edit labels by hand:

```json
"Acurite-609TXC|12345|": {
  "label": "kitchen weather sensor",
  "first_seen": "...",
  "last_seen": "...",
  "seen_count": 4
}
```

A `LEAD` after that is “this ID was not in the house inventory.” It is not automatically a bug. Press *your* doorbell and garage remote during `learn` so those keys are owned.

## What each survey status means

- `ok_signals` / `weak_signals` on FM or weather radio — the stick works
- `energy_present` on ISM — something radiated during the dwell; use `watch` to name it
- `lead_wide_energy` on 1.18–1.30 GHz — look in GQRX for a fat persistent smear (analog video class), not speckles
- `lead_interference` on GPS L1 — a *spike*, not satellites. This whip will miss quiet jammers
- `occupied` on GSM/LTE slices — a cell site exists. Not a Stingray detector
- `quiet` — nothing stood out in that short snapshot (normal for bursty remotes)

Pager and handheld rows are **energy only**. The script will not decode POCSAG/FLEX content.

## Sample Command Run Order

# 1. Whip ~17 cm (433) or ~24 cm (315). Census whatever is already transmitting.
python3 sdr_detect.py inventory --region us --seconds 180 --gain 20

# 2. Same listen, freeze those IDs as "yours". Press your own fob/doorbell during this.
python3 sdr_detect.py learn --region us --seconds 600 --gain 20

# 3. Live: KNOWN vs LEAD
python3 sdr_detect.py watch --region us --gain 20

# 4. Collapse whip to ~7 cm first. Energy-only look at the rest of the list.
python3 sdr_detect.py survey --region us --dwell 15 --gain 20

# Health-only (FM + weather radio) if you just want to know the stick works:
python3 sdr_detect.py health --region us

## Alert Commands

#sdr alert command hops targets and alert only when signal goes from quiet to active
python3 sdr_alert.py --region us --gain 20

#subset more in depth
python3 sdr_alert.py --region us --only REMOTE315,REMOTE433,ADSB,GSM900,FRS_GMRS --beep
