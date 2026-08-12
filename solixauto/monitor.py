import json
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import paths
from .profiles import list_profiles, load_yaml, save_yaml, slugify

HISTORY_SIZE = 2880
POLL_TIMEOUT = 3


class Sampler:
    def __init__(self, interval=5.0, history=HISTORY_SIZE):
        self.interval = interval
        self.history = deque(maxlen=history)
        self.snapshot = {
            "anker": [],
            "shelly": [],
            "events": [],
            "multi": False,
            "updated": None,
            "engine": False,
        }
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def _loop(self):
        while not self.stop_event.is_set():
            try:
                self._sample()
            except Exception:
                pass
            self.stop_event.wait(self.interval)

    def _read_live(self, serial):
        path = paths.STATE_DIR / f"live-{serial}.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if time.time() - float(data.get("epoch", 0)) > 120:
            data["fresh"] = False
        else:
            data["fresh"] = True
        return data

    def rename(self, kind, path_name, new_name, on_device=False):
        directory = (
            paths.ANKER_PROFILE_DIR if kind == "anker" else paths.SHELLY_PROFILE_DIR
        )
        path = directory / path_name

        if path.parent.resolve() != directory.resolve() or not path.exists():
            raise ValueError("unknown device")

        new_name = str(new_name).strip()
        if not new_name:
            raise ValueError("name cannot be empty")
        if len(new_name) > 60:
            raise ValueError("name is too long")

        profile = load_yaml(path)
        identity = profile.setdefault("identity", {})
        old_name = identity.get("name") or path.stem
        identity["name"] = new_name

        aliases = list(profile.get("aliases") or [])
        for candidate in (new_name, old_name, path.stem):
            if candidate and candidate not in aliases:
                aliases.append(candidate)
        profile["aliases"] = aliases

        destination = directory / f"{slugify(new_name).lower()}.yaml"
        if destination != path and destination.exists():
            raise ValueError(f"{destination.name} already exists")

        save_yaml(path, profile)
        if destination != path:
            path.replace(destination)

        wrote_device = False
        if on_device and kind == "shelly":
            wrote_device = self._write_shelly_name(profile, new_name)

        self._sample()
        return {"file": destination.name, "name": new_name, "on_device": wrote_device}

    def _write_shelly_name(self, profile, new_name):
        access = profile.get("access") or {}
        identity = profile.get("identity") or {}
        host = access.get("host")
        generation = int(identity.get("generation", 1) or 1)

        if not host or generation < 2:
            return False

        payload = json.dumps({"config": {"device": {"name": new_name}}}).encode()
        request = urllib.request.Request(
            f"http://{host}/rpc/Sys.SetConfig",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=POLL_TIMEOUT) as response:
                return response.status == 200
        except Exception:
            return False

    def _anker_devices(self):
        devices = []
        for path in list_profiles(paths.ANKER_PROFILE_DIR):
            try:
                profile = load_yaml(path)
            except Exception:
                continue
            identity = profile.get("identity", {})
            serial = identity.get("serial")
            live = self._read_live(serial) if serial else None
            values = (live or {}).get("values", {})

            devices.append(
                {
                    "file": path.name,
                    "name": identity.get("name") or identity.get("model") or path.stem,
                    "model": identity.get("model") or identity.get("part_number") or "",
                    "serial": serial,
                    "live": bool(live and live.get("fresh")),
                    "updated": (live or {}).get("updated"),
                    "age": (live or {}).get("age_seconds"),
                    "profile": (live or {}).get("profile"),
                    "thresholds": (live or {}).get("thresholds") or [],
                    "floor_latched": (live or {}).get("floor_latched", False),
                    "battery_soc": values.get("battery_soc"),
                    "output_watts": values.get("output_power_total"),
                    "ac_in_watts": values.get("ac_input_power"),
                    "pv_watts": values.get("pv_total"),
                    "pv_surplus": values.get("pv_surplus"),
                    "temperature": values.get("temperature"),
                }
            )
        return devices

    def _poll_shelly(self, host, generation, channel):
        path = "/rpc/Shelly.GetStatus" if generation >= 2 else "/status"
        try:
            with urllib.request.urlopen(
                f"http://{host}{path}", timeout=POLL_TIMEOUT
            ) as response:
                data = json.loads(response.read().decode("utf-8"))
        except Exception:
            return None, None

        if generation >= 2:
            entry = data.get(f"switch:{channel}") or {}
            return entry.get("output"), entry.get("apower")

        relays = data.get("relays") or []
        meters = data.get("meters") or []
        state = relays[channel].get("ison") if channel < len(relays) else None
        power = meters[channel].get("power") if channel < len(meters) else None
        return state, power

    def _utilized_shelly_files(self):
        utilized = set()
        for path in list_profiles(paths.POWER_PROFILE_DIR):
            try:
                power_profile = load_yaml(path)
            except Exception:
                continue
            target = (power_profile.get("target") or {}).get("profile")
            if not target:
                continue
            resolved = paths.resolve_profile(target, "shelly")
            if resolved is not None:
                utilized.add(resolved.name)
        return utilized

    def _shelly_devices(self):
        devices = []
        utilized = self._utilized_shelly_files()
        for path in list_profiles(paths.SHELLY_PROFILE_DIR):
            if path.name not in utilized:
                continue
            try:
                profile = load_yaml(path)
            except Exception:
                continue
            identity = profile.get("identity", {})
            access = profile.get("access", {})
            host = access.get("host")
            generation = int(identity.get("generation", 1) or 1)
            channels = profile.get("channels") or {"0": {}}
            channel = int(sorted(channels)[0])

            state, power = self._poll_shelly(host, generation, channel) if host else (None, None)

            devices.append(
                {
                    "file": path.name,
                    "name": identity.get("name") or path.stem,
                    "model": identity.get("model") or "",
                    "host": host,
                    "channel": channel,
                    "state": state,
                    "watts": power,
                    "reachable": state is not None,
                }
            )
        return devices

    def _events(self):
        events = []
        if not paths.STATE_DIR.exists():
            return events

        for path in sorted(paths.STATE_DIR.glob("events-*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(data, list):
                events.extend(entry for entry in data if isinstance(entry, dict))

        events.sort(key=lambda entry: entry.get("epoch", 0), reverse=True)
        return events[:60]

    def _sample(self):
        anker = self._anker_devices()
        shelly = self._shelly_devices()
        events = self._events()

        point = {"t": time.time()}
        for device in anker:
            if not device["live"]:
                continue
            serial = device["serial"]
            point[f"{serial}:soc"] = device["battery_soc"]
            point[f"{serial}:out"] = device["output_watts"]
            point[f"{serial}:ac"] = device["ac_in_watts"]
            point[f"{serial}:pv"] = device["pv_watts"]

        with self.lock:
            if len(point) > 1:
                self.history.append(point)
            self.snapshot = {
                "anker": anker,
                "shelly": shelly,
                "events": events,
                "multi": len(anker) > 1 or len(shelly) > 1,
                "updated": time.strftime("%H:%M:%S"),
                "engine": any(d["live"] for d in anker),
                "interval": self.interval,
            }

    def payload(self):
        with self.lock:
            return {
                **self.snapshot,
                "history": list(self.history),
            }


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Power monitor</title>
<style>
  :root {
    --panel: #dedbd4;
    --panel-raised: #e9e7e1;
    --ink: #1a1a18;
    --ink-soft: #6b675e;
    --rule: #bcb8ae;
    --solar: #d8830f;
    --grid: #2f6f7e;
    --load: #4a4741;
    --charge: #4c7a34;
    --alert: #a83a2a;
    --etch: rgba(0, 0, 0, 0.05);
  }

  [data-theme="dark"] {
    --panel: #0a1220;
    --panel-raised: #121e33;
    --ink: #e9eff8;
    --ink-soft: #7f95b4;
    --rule: #22354f;
    --solar: #ff8f3c;
    --grid: #54a6de;
    --load: #8ba2c1;
    --charge: #3fbf9a;
    --alert: #ff6f5e;
    --etch: rgba(120, 170, 235, 0.09);
  }

  [data-theme="dark"] section {
    box-shadow: inset 0 1px 0 rgba(120, 170, 235, 0.06);
  }

  [data-theme="dark"] .bus {
    border-color: var(--rule);
  }

  [data-theme="dark"] h1 {
    color: var(--solar);
  }

  html { background: var(--panel); }

  * { box-sizing: border-box; }

  body {
    margin: 0;
    transition: background-color .25s ease, color .25s ease;
    padding: 28px 20px 60px;
    background: var(--panel);
    color: var(--ink);
    font-family: "Helvetica Neue", Helvetica, Arial, sans-serif;
    font-size: 14px;
    -webkit-font-smoothing: antialiased;
  }

  .wrap { max-width: 1040px; margin: 0 auto; }

  header {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 16px;
    padding-bottom: 14px;
    border-bottom: 2px solid var(--ink);
    margin-bottom: 26px;
    flex-wrap: wrap;
  }

  h1 {
    margin: 0;
    font-size: 15px;
    font-weight: 700;
    letter-spacing: 0.16em;
    text-transform: uppercase;
  }

  .stamp {
    font-family: ui-monospace, "SF Mono", Menlo, monospace;
    font-size: 12px;
    color: var(--ink-soft);
    letter-spacing: 0.04em;
  }

  .stamp b { color: var(--ink); font-weight: 600; }

  .dot {
    display: inline-block;
    width: 7px; height: 7px;
    border-radius: 50%;
    background: var(--charge);
    margin-right: 7px;
    vertical-align: middle;
  }
  .dot.stale { background: var(--alert); }

  section {
    background: var(--panel-raised);
    border: 1px solid var(--rule);
    padding: 20px 22px 22px;
    margin-bottom: 22px;
  }

  h2 {
    margin: 0 0 18px;
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.2em;
    text-transform: uppercase;
    color: var(--ink-soft);
  }

  .panel-toggle {
    display: flex;
    align-items: center;
    gap: 8px;
    width: 100%;
    margin: 0 0 18px;
    padding: 0;
    background: none;
    border: 0;
    font: inherit;
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.2em;
    text-transform: uppercase;
    color: var(--ink-soft);
    cursor: pointer;
    text-align: left;
  }

  .panel-toggle:hover { color: var(--ink); }
  .panel-toggle:focus-visible { outline: 2px solid var(--solar); outline-offset: 3px; }

  .panel-toggle .caret {
    font-size: 9px;
    line-height: 1;
    transition: transform .18s ease;
    flex: 0 0 auto;
  }

  .panel-toggle[aria-expanded="false"] .caret { transform: rotate(-90deg); }
  .panel-toggle[aria-expanded="false"] { margin-bottom: 0; }

  .panel-toggle .rule-line {
    flex: 1 1 auto;
    height: 1px;
    background: transparent;
    transition: background-color .18s ease;
  }

  .panel-toggle[aria-expanded="false"] .rule-line { background: var(--rule); }

  .panel-toggle .count {
    font-size: 10px;
    letter-spacing: 0.08em;
    opacity: 0;
    transition: opacity .18s ease;
    flex: 0 0 auto;
  }

  .panel-toggle[aria-expanded="false"] .count { opacity: 1; }

  section[data-collapsed="true"] .panel-body { display: none; }

  @media (prefers-reduced-motion: reduce) {
    .panel-toggle .caret,
    .panel-toggle .rule-line,
    .panel-toggle .count { transition: none; }
  }

  .device { padding: 16px 0; border-top: 1px solid var(--rule); }
  .device:first-of-type { border-top: none; padding-top: 0; }

  .device-head {
    display: flex;
    align-items: baseline;
    gap: 10px;
    margin-bottom: 12px;
    flex-wrap: wrap;
  }

  .device-name { font-size: 17px; font-weight: 600; letter-spacing: -0.01em; }

  .editable {
    cursor: text;
    border-bottom: 1px dashed transparent;
  }

  .editable:hover, .editable:focus-visible {
    border-bottom-color: var(--ink-soft);
    outline: none;
  }

  .editable::after {
    content: " \270E";
    font-size: 0.7em;
    color: var(--ink-soft);
    opacity: 0;
    transition: opacity .15s ease;
  }

  .editable:hover::after, .editable:focus-visible::after { opacity: 1; }

  .name-input {
    font: inherit;
    color: var(--ink);
    background: var(--panel);
    border: 1px solid var(--solar);
    padding: 1px 5px;
    width: 22ch;
    max-width: 100%;
  }

  .name-input:focus { outline: none; }

  .edit-hint {
    font-family: ui-monospace, "SF Mono", Menlo, monospace;
    font-size: 10px;
    color: var(--ink-soft);
    margin-left: 8px;
  }

  .edit-error {
    font-family: ui-monospace, "SF Mono", Menlo, monospace;
    font-size: 11px;
    color: var(--alert);
    margin-top: 6px;
  }

  .device-meta {
    font-family: ui-monospace, "SF Mono", Menlo, monospace;
    font-size: 11px;
    color: var(--ink-soft);
  }

  .readouts {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(108px, 1fr));
    gap: 14px 20px;
    margin-bottom: 16px;
  }

  .readout .label {
    font-size: 9px;
    letter-spacing: 0.15em;
    text-transform: uppercase;
    color: var(--ink-soft);
    margin-bottom: 3px;
  }

  .readout .value {
    font-family: ui-monospace, "SF Mono", Menlo, monospace;
    font-size: 25px;
    font-weight: 500;
    font-variant-numeric: tabular-nums;
    line-height: 1.05;
  }

  .readout .value span { font-size: 13px; color: var(--ink-soft); margin-left: 2px; }
  .readout.solar .value { color: var(--solar); }
  .readout.grid .value { color: var(--grid); }
  .readout.load .value { color: var(--load); }

  .bus {
    display: grid;
    grid-template-columns: 96px 1fr 62px;
    gap: 6px 12px;
    align-items: center;
  }

  .bus-label {
    font-family: ui-monospace, "SF Mono", Menlo, monospace;
    font-size: 10px;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--ink-soft);
    text-align: right;
  }

  .bus-track {
    height: 13px;
    background:
      repeating-linear-gradient(90deg, transparent 0 5px, var(--etch) 5px 6px);
    border: 1px solid var(--rule);
  }

  .bus-track i {
    display: block;
    height: 100%;
    transition: width .5s ease;
    min-width: 0;
  }

  .bus-track .in-solar { background: var(--solar); }
  .bus-track .in-grid { background: var(--grid); }
  .bus-track .out-load { background: var(--load); }

  .bus-value {
    font-family: ui-monospace, "SF Mono", Menlo, monospace;
    font-size: 12px;
    font-variant-numeric: tabular-nums;
    color: var(--ink);
  }

  .bus-scale {
    grid-column: 2 / 4;
    font-family: ui-monospace, "SF Mono", Menlo, monospace;
    font-size: 10px;
    color: var(--ink-soft);
    margin-top: 2px;
  }

  .battery {
    display: grid;
    grid-template-columns: 96px 1fr 62px;
    grid-template-rows: auto auto auto;
    gap: 0 12px;
    align-items: center;
    margin-top: 14px;
    padding-top: 14px;
    border-top: 1px solid var(--rule);
  }

  .battery-track {
    position: relative;
    height: 15px;
    background:
      repeating-linear-gradient(90deg, transparent 0 5px, var(--etch) 5px 6px);
    border: 1px solid var(--rule);
  }

  .battery-fill {
    display: block;
    height: 100%;
    background: var(--charge);
    transition: width .5s ease, background-color .3s ease;
  }

  .battery-fill.low { background: var(--solar); }
  .battery-fill.critical { background: var(--alert); }

  .battery-mark {
    position: absolute;
    top: -3px;
    bottom: -3px;
    width: 1px;
    background: var(--ink-soft);
    opacity: 0.55;
  }

  .battery-mark.floor { background: var(--ink-soft); opacity: 0.55; width: 1px; }

  .battery-words {
    grid-column: 2;
    grid-row: 1;
    position: relative;
    height: 15px;
  }

  .battery-values {
    grid-column: 2;
    grid-row: 3;
    position: relative;
    height: 15px;
  }

  .battery > .bus-label { grid-column: 1; grid-row: 2; }
  .battery > .battery-track { grid-column: 2; grid-row: 2; }
  .battery > .bus-value { grid-column: 3; grid-row: 2; }

  .battery-tag {
    position: absolute;
    font-family: ui-monospace, "SF Mono", Menlo, monospace;
    font-size: 9px;
    letter-spacing: 0.04em;
    color: var(--ink-soft);
    white-space: nowrap;
    transform: translateX(-50%);
  }

  .battery-words .battery-tag { top: 0; }
  .battery-values .battery-tag { top: 6px; }

  .battery-tag.floor { color: var(--ink-soft); }
  .battery-tag { cursor: help; }
  .battery-tag:hover { color: var(--ink); }

  .battery-leader {
    position: absolute;
    border-left: 1px solid var(--rule);
    border-bottom: 1px solid var(--rule);
    height: 5px;
  }

  .battery-words .battery-leader {
    bottom: 0;
    border-bottom: none;
    border-top: 1px solid var(--rule);
  }

  .battery-values .battery-leader { top: 0; }

  .battery-leader.flat { border-top: none; border-bottom: none; }
  .swatch { display: inline-block; width: 9px; height: 9px; margin-right: 5px; }
  .sw-solar { background: var(--solar); }
  .sw-grid { background: var(--grid); }
  .sw-load { background: var(--load); }
  .sw-batt { background: var(--charge); }

  .theme {
    display: inline-flex;
    border: 1px solid var(--rule);
    margin-left: 14px;
    vertical-align: middle;
  }

  .theme button {
    font-family: ui-monospace, "SF Mono", Menlo, monospace;
    font-size: 10px;
    letter-spacing: 0.12em;
    padding: 4px 9px;
    border: 0;
    background: transparent;
    color: var(--ink-soft);
    cursor: pointer;
  }

  .theme button[aria-pressed="true"] {
    background: var(--ink);
    color: var(--panel-raised);
  }

  [data-theme="dark"] .theme button[aria-pressed="true"] {
    background: var(--solar);
    color: var(--panel);
  }

  .theme button:focus-visible { outline: 2px solid var(--solar); outline-offset: 1px; }

  table { width: 100%; border-collapse: collapse; }

  th {
    text-align: left;
    font-size: 9px;
    letter-spacing: 0.15em;
    text-transform: uppercase;
    color: var(--ink-soft);
    font-weight: 600;
    padding-bottom: 8px;
    border-bottom: 1px solid var(--rule);
  }

  td {
    padding: 12px 0;
    border-bottom: 1px solid var(--rule);
    font-size: 15px;
  }

  tr:last-child td { border-bottom: none; }

  td.mono {
    font-family: ui-monospace, "SF Mono", Menlo, monospace;
    font-variant-numeric: tabular-nums;
    font-size: 13px;
    color: var(--ink-soft);
  }

  .pill {
    display: inline-block;
    font-family: ui-monospace, "SF Mono", Menlo, monospace;
    font-size: 11px;
    letter-spacing: 0.1em;
    padding: 3px 10px;
    border: 1px solid currentColor;
  }
  .pill.on { color: var(--charge); }
  .pill.off { color: var(--ink-soft); }
  .pill.unknown { color: var(--alert); }

  .chart-head {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    gap: 14px;
    margin-bottom: 14px;
    flex-wrap: wrap;
  }

  .legend {
    display: flex;
    gap: 16px;
    font-family: ui-monospace, "SF Mono", Menlo, monospace;
    font-size: 10px;
    letter-spacing: 0.06em;
    color: var(--ink-soft);
    flex-wrap: wrap;
  }

  canvas { width: 100%; height: 300px; display: block; }

  .event-scroll {
    max-height: 194px;
    overflow-y: auto;
    scrollbar-width: thin;
    scrollbar-color: var(--rule) transparent;
  }

  .event-scroll::-webkit-scrollbar { width: 9px; }
  .event-scroll::-webkit-scrollbar-track { background: transparent; }
  .event-scroll::-webkit-scrollbar-thumb {
    background: var(--rule);
    border: 2px solid var(--panel-raised);
  }
  .event-scroll::-webkit-scrollbar-thumb:hover { background: var(--ink-soft); }

  .event {
    display: flex;
    align-items: baseline;
    gap: 12px;
    padding: 9px 12px 9px 0;
    border-top: 1px solid var(--rule);
    white-space: nowrap;
  }

  .event:first-child { border-top: none; }

  .event-time {
    font-family: ui-monospace, "SF Mono", Menlo, monospace;
    font-size: 12px;
    color: var(--ink-soft);
    font-variant-numeric: tabular-nums;
    flex: 0 0 auto;
  }

  .event-state {
    font-family: ui-monospace, "SF Mono", Menlo, monospace;
    font-size: 10px;
    letter-spacing: 0.08em;
    text-align: center;
    padding: 1px 0;
    border: 1px solid currentColor;
    flex: 0 0 38px;
  }

  .event-state.on { color: var(--charge); }
  .event-state.off { color: var(--ink-soft); }

  .event-target {
    font-size: 13px;
    font-weight: 600;
    flex: 0 0 auto;
    max-width: 20ch;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .event-why {
    font-size: 13px;
    color: var(--ink);
    flex: 1 1 auto;
    overflow: hidden;
    text-overflow: ellipsis;
    min-width: 0;
  }

  .event-cause {
    display: inline-block;
    font-family: ui-monospace, "SF Mono", Menlo, monospace;
    font-size: 9px;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    padding: 1px 6px;
    margin-right: 7px;
    border: 1px solid var(--rule);
    color: var(--ink-soft);
    vertical-align: 1px;
  }

  .event-cause.floor { color: var(--alert); border-color: currentColor; }
  .event-cause.stale { color: var(--solar); border-color: currentColor; }

  .event-detail {
    font-family: ui-monospace, "SF Mono", Menlo, monospace;
    font-size: 11px;
    color: var(--ink-soft);
    flex: 0 0 auto;
  }

  .event-day {
    font-family: ui-monospace, "SF Mono", Menlo, monospace;
    font-size: 10px;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--ink-soft);
    padding: 14px 0 6px;
    border-top: 1px solid var(--rule);
  }

  .event-day:first-child { border-top: none; padding-top: 0; }

  .empty {
    font-family: ui-monospace, "SF Mono", Menlo, monospace;
    font-size: 12px;
    color: var(--ink-soft);
    line-height: 1.7;
  }

  .empty b { color: var(--ink); font-weight: 600; }

  @media (prefers-reduced-motion: reduce) {
    .bus i { transition: none; }
  }
</style>
</head>
<body>
<div class="wrap">

  <header>
    <h1>Power monitor</h1>
    <div class="stamp"><span class="dot" id="dot"></span>updated <b id="stamp">--:--:--</b>
      <span class="theme" role="group" aria-label="Colour scheme">
        <button type="button" id="lightBtn" aria-pressed="false">LIGHT</button>
        <button type="button" id="darkBtn" aria-pressed="false">DARK</button>
      </span>
      <span class="theme" role="group" aria-label="Clock format">
        <button type="button" id="clock12Btn" aria-pressed="false">12H</button>
        <button type="button" id="clock24Btn" aria-pressed="false">24H</button>
      </span>
    </div>
  </header>

  <section>
    <button type="button" class="panel-toggle" data-panel="anker" aria-expanded="true">
      <span class="caret">▼</span>
      <span>Anker devices</span>
      <span class="rule-line"></span>
      <span class="count" id="count-anker"></span>
    </button>
    <div class="panel-body"><div id="anker"></div></div>
  </section>

  <section>
    <button type="button" class="panel-toggle" data-panel="shelly" aria-expanded="true">
      <span class="caret">▼</span>
      <span>Shelly devices</span>
      <span class="rule-line"></span>
      <span class="count" id="count-shelly"></span>
    </button>
    <div class="panel-body"><div id="shelly"></div></div>
  </section>

  <section>
    <button type="button" class="panel-toggle" data-panel="actions" aria-expanded="true">
      <span class="caret">▼</span>
      <span>Actions</span>
      <span class="rule-line"></span>
      <span class="count" id="count-actions"></span>
    </button>
    <div class="panel-body">
      <div class="event-scroll"><div id="events"></div></div>
    </div>
  </section>

  <section>
    <button type="button" class="panel-toggle" data-panel="history" aria-expanded="true">
      <span class="caret">▼</span>
      <span>History</span>
      <span class="rule-line"></span>
      <span class="count" id="count-history"></span>
    </button>
    <div class="panel-body">
    <div class="chart-head">
      <div class="legend">
        <span><i class="swatch sw-solar"></i>solar</span>
        <span><i class="swatch sw-grid"></i>grid</span>
        <span><i class="swatch sw-load"></i>load</span>
        <span><i class="swatch sw-batt"></i>battery</span>
      </div>
    </div>
    <canvas id="chart"></canvas>
    </div>
  </section>

</div>

<script>
const css = name => getComputedStyle(document.documentElement)
  .getPropertyValue(name).trim();

const fmt = (v, unit) => v === null || v === undefined
  ? '<span>--</span>'
  : Math.round(v) + '<span>' + unit + '</span>';

function busBar(pv, ac, out) {
  const peak = Math.max(pv || 0, ac || 0, out || 0);
  const scale = Math.max(Math.ceil(peak / 100) * 100, 100);
  const w = v => ((v || 0) / scale * 100).toFixed(1) + '%';

  const row = (cls, label, value) =>
    '<div class="bus-label">' + label + '</div>'
    + '<div class="bus-track"><i class="' + cls + '" style="width:'
    + w(value) + '"></i></div>'
    + '<div class="bus-value">' + Math.round(value || 0) + ' W</div>';

  return '<div class="bus">'
    + row('in-solar', 'solar (in)', pv)
    + row('in-grid', 'grid (in)', ac)
    + row('out-load', 'load (out)', out)
    + '<div class="bus-scale">scale 0 to ' + scale + ' W</div>'
    + '</div>';
}

function tagTitle(t) {
  const parts = [];
  if (t.label) parts.push(t.label);
  if (t.condition) parts.push(t.condition);
  if (t.explain) parts.push(t.explain);
  return parts.join('\\n');
}

function placeTags(points, words, px) {
  const charPx = 5.4;
  let cursor = -Infinity;

  return points.map((t, index) => {
    const word = words[index];
    const halfPx = (word.length * charPx) / 2;
    const wantPx = (t.at / 100) * px;
    let leftPx = Math.max(wantPx, cursor + halfPx + 6);
    leftPx = Math.min(leftPx, px - halfPx);
    cursor = leftPx + halfPx;

    const shifted = Math.abs(leftPx - wantPx) > 1.5;
    const leaderLeft = Math.min(wantPx, leftPx);
    const leaderWidth = Math.abs(leftPx - wantPx);

    return '<span class="battery-leader' + (shifted ? '' : ' flat')
      + '" style="left:' + leaderLeft + 'px;width:'
      + (shifted ? leaderWidth : 0) + 'px"></span>'
      + '<span class="battery-tag ' + (t.kind === 'floor' ? 'floor' : '')
      + '" style="left:' + leftPx + 'px" title="' + esc(tagTitle(t)) + '">'
      + esc(word) + '</span>';
  }).join('');
}

function batteryBar(soc, thresholds) {
  if (soc === null || soc === undefined) return '';

  const RANK = { floor: 0, on: 1, off: 2, release: 3, rule: 4 };

  const byValue = {};
  (thresholds || [])
    .filter(t => typeof t.at === 'number' && t.at >= 0 && t.at <= 100)
    .forEach(t => {
      const key = Math.round(t.at);
      const existing = byValue[key];
      if (!existing || (RANK[t.kind] ?? 9) < (RANK[existing.kind] ?? 9)) {
        byValue[key] = t;
      }
    });

  const points = Object.values(byValue).sort((a, b) => a.at - b.at);

  const marks = points.map(t =>
    '<span class="battery-mark ' + (t.kind === 'floor' ? 'floor' : '')
    + '" style="left:' + t.at + '%" title="'
    + esc(tagTitle(t)) + '"></span>').join('');




  const floor = points.find(t => t.kind === 'floor');
  const onPoint = points.find(t => t.kind === 'on');
  let cls = '';
  if (floor && soc <= floor.at) cls = 'critical';
  else if (onPoint && soc <= onPoint.at) cls = 'low';

  const payload = encodeURIComponent(JSON.stringify(points));

  return '<div class="battery" data-points="' + payload + '">'
    + '<div class="battery-words"></div>'
    + '<div class="bus-label">battery</div>'
    + '<div class="battery-track">'
    + '<i class="battery-fill ' + cls + '" style="width:' + soc + '%"></i>'
    + marks
    + '</div>'
    + '<div class="bus-value">' + Math.round(soc) + ' %</div>'
    + '<div class="battery-values"></div>'
    + '</div>';
}

function layoutBatteryTags() {
  document.querySelectorAll('.battery').forEach(box => {
    let points;
    try {
      points = JSON.parse(decodeURIComponent(box.dataset.points || '[]'));
    } catch (e) { return; }
    if (!points.length) return;

    const track = box.querySelector('.battery-track');
    const px = track ? track.clientWidth : 0;
    if (!px) return;

    const words = box.querySelector('.battery-words');
    const values = box.querySelector('.battery-values');
    if (words) {
      words.innerHTML = placeTags(
        points, points.map(t => t.kind === 'floor' ? 'floor' : 'rule'), px);
    }
    if (values) {
      values.innerHTML = placeTags(
        points, points.map(t => Math.round(t.at) + '%'), px);
    }
  });
}

let canEdit = false;

function esc(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  })[c]);
}

function nameCell(d, kind, cls) {
  const classes = (cls ? cls + ' ' : '') + (canEdit ? 'editable' : '');
  const attrs = canEdit
    ? ' tabindex="0" role="button" title="Click to rename"'
      + ' data-kind="' + kind + '" data-file="' + esc(d.file) + '"'
    : '';
  return '<span class="' + classes.trim() + '"' + attrs + '>' + esc(d.name) + '</span>';
}

function startEdit(span) {
  if (span.dataset.editing) return;
  span.dataset.editing = '1';

  const kind = span.dataset.kind;
  const file = span.dataset.file;
  const current = span.textContent;
  const parent = span.parentNode;

  const input = document.createElement('input');
  input.className = 'name-input';
  input.value = current;
  input.maxLength = 60;

  const hint = document.createElement('span');
  hint.className = 'edit-hint';
  hint.textContent = 'enter to save, esc to cancel';

  const wrap = document.createElement('span');
  wrap.appendChild(input);
  wrap.appendChild(hint);

  let device = null;
  if (kind === 'shelly') {
    const label = document.createElement('label');
    label.className = 'edit-hint';
    device = document.createElement('input');
    device.type = 'checkbox';
    label.appendChild(device);
    label.appendChild(document.createTextNode(' also write to the plug'));
    wrap.appendChild(label);
  }

  parent.replaceChild(wrap, span);
  input.focus();
  input.select();

  let done = false;

  const restore = () => {
    if (done) return;
    done = true;
    paused = false;
    refresh();
  };

  const save = async () => {
    if (done) return;
    const value = input.value.trim();
    if (!value || value === current) { restore(); return; }
    done = true;
    input.disabled = true;
    hint.textContent = 'saving...';
    try {
      const response = await fetch('/api/rename', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          kind: kind, file: file, name: value,
          on_device: device ? device.checked : false
        })
      });
      const result = await response.json();
      if (!response.ok) {
        const problem = document.createElement('div');
        problem.className = 'edit-error';
        problem.textContent = result.error || 'rename failed';
        wrap.appendChild(problem);
        hint.textContent = '';
        setTimeout(() => { paused = false; refresh(); }, 4000);
        return;
      }
    } catch (err) {
      hint.textContent = 'could not reach the monitor';
      setTimeout(() => { paused = false; refresh(); }, 4000);
      return;
    }
    paused = false;
    refresh();
  };

  input.addEventListener('keydown', event => {
    if (event.key === 'Enter') { event.preventDefault(); save(); }
    if (event.key === 'Escape') { event.preventDefault(); restore(); }
  });
  input.addEventListener('blur', () => setTimeout(save, 120));

  paused = true;
}

document.addEventListener('click', event => {
  const span = event.target.closest('.editable');
  if (span) startEdit(span);
});

document.addEventListener('keydown', event => {
  if (event.key !== 'Enter') return;
  const span = event.target.closest && event.target.closest('.editable');
  if (span) { event.preventDefault(); startEdit(span); }
});

function renderAnker(devices) {
  const box = document.getElementById('anker');
  if (!devices.length) {
    box.innerHTML = '<p class="empty">No Anker devices saved yet.<br>'
      + 'Run <b>solixauto discover-anker</b> to add one.</p>';
    return;
  }
  box.innerHTML = devices.map(d => {
    if (!d.live) {
      return '<div class="device"><div class="device-head">'
        + nameCell(d, 'anker', 'device-name')
        + '<span class="device-meta">' + d.model + '</span></div>'
        + '<p class="empty">No live readings. Live data comes from a running '
        + 'automation.<br>Start one with <b>solixauto service &lt;profile&gt;</b>.</p></div>';
    }
    return '<div class="device">'
      + '<div class="device-head">'
      + nameCell(d, 'anker', 'device-name')
      + '<span class="device-meta">' + d.model + ' &middot; ' + (d.profile || '') + '</span>'
      + (d.floor_latched ? '<span class="pill unknown">FLOOR LATCHED</span>' : '')
      + '</div>'
      + '<div class="readouts">'
      + '<div class="readout"><div class="label">Battery</div><div class="value">' + fmt(d.battery_soc, '%') + '</div></div>'
      + '<div class="readout solar"><div class="label">Solar in</div><div class="value">' + fmt(d.pv_watts, 'W') + '</div></div>'
      + '<div class="readout grid"><div class="label">Grid in</div><div class="value">' + fmt(d.ac_in_watts, 'W') + '</div></div>'
      + '<div class="readout load"><div class="label">Load out</div><div class="value">' + fmt(d.output_watts, 'W') + '</div></div>'
      + '<div class="readout"><div class="label">Surplus</div><div class="value">' + fmt(d.pv_surplus, 'W') + '</div></div>'
      + '</div>'
      + busBar(d.pv_watts, d.ac_in_watts, d.output_watts)
      + batteryBar(d.battery_soc, d.thresholds)
      + '</div>';
  }).join('');
}

function renderShelly(devices) {
  const box = document.getElementById('shelly');
  if (!devices.length) {
    box.innerHTML = '<p class="empty">No Shelly devices saved yet.<br>'
      + 'Run <b>solixauto discover-shelly</b> to add one.</p>';
    return;
  }
  box.innerHTML = '<table><thead><tr>'
    + '<th>Name</th><th>Address</th><th>Drawing</th><th>Switch</th>'
    + '</tr></thead><tbody>'
    + devices.map(d => {
        const cls = d.state === true ? 'on' : (d.state === false ? 'off' : 'unknown');
        const text = d.state === true ? 'ON' : (d.state === false ? 'OFF' : 'NO REPLY');
        return '<tr><td>' + nameCell(d, 'shelly', '') + '</td>'
          + '<td class="mono">' + (d.host || '--') + ' ch' + d.channel + '</td>'
          + '<td class="mono">' + (d.watts === null || d.watts === undefined ? '--' : Math.round(d.watts) + ' W') + '</td>'
          + '<td><span class="pill ' + cls + '">' + text + '</span></td></tr>';
      }).join('')
    + '</tbody></table>';
}

const CAUSE_LABEL = {
  rule: 'rule',
  floor: 'safety floor',
  stale: 'telemetry lost',
  recovered: 'recovered',
  manual: 'manual'
};

function dayLabel(epoch) {
  const d = new Date(epoch * 1000);
  const today = new Date();
  const yesterday = new Date(today.getTime() - 86400000);
  const same = (a, b) => a.toDateString() === b.toDateString();
  if (same(d, today)) return 'Today';
  if (same(d, yesterday)) return 'Yesterday';
  return d.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' });
}

let use12Hour = false;

function clockLabel(epoch) {
  const d = new Date(epoch * 1000);
  const minutes = String(d.getMinutes()).padStart(2, '0');
  if (!use12Hour) {
    return String(d.getHours()).padStart(2, '0') + ':' + minutes;
  }
  const hours24 = d.getHours();
  const period = hours24 < 12 ? 'am' : 'pm';
  const hours12 = hours24 % 12 || 12;
  return hours12 + ':' + minutes + period;
}

function eventDetail(e) {
  const v = e.values || {};
  const bits = [];
  if (v.battery_soc !== undefined) bits.push('battery ' + v.battery_soc + '%');
  if (v.pv_total !== undefined) bits.push('solar ' + v.pv_total + 'W');
  if (v.output_power_total !== undefined) bits.push('load ' + v.output_power_total + 'W');
  if (v.ac_input_power) bits.push('grid ' + v.ac_input_power + 'W');
  if (!bits.length) return '';
  const who = e.source ? esc(e.source) + ': ' : '';
  return who + bits.join(', ');
}

function renderEvents(events, multi) {
  const box = document.getElementById('events');

  if (!events || !events.length) {
    box.innerHTML = '<p class="empty">Every change to a plug will appear here '
      + 'with the reason for it.</p>';
    return;
  }

  let html = '';
  let lastDay = null;

  events.forEach(e => {
    const day = dayLabel(e.epoch);
    if (day !== lastDay) {
      html += '<div class="event-day">' + day + '</div>';
      lastDay = day;
    }

    const cause = e.cause || 'rule';
    const label = CAUSE_LABEL[cause] || cause;
    const why = e.rule ? esc(e.rule) : esc(e.reason || 'changed');
    const detail = eventDetail(e);
    const tip = [e.condition, detail].filter(Boolean).join('  |  ');

    html += '<div class="event"' + (tip ? ' title="' + esc(tip) + '"' : '') + '>'
      + '<div class="event-time">' + clockLabel(e.epoch) + '</div>'
      + '<div class="event-state ' + (e.state ? 'on' : 'off') + '">'
      + (e.state ? 'ON' : 'OFF') + '</div>'
      + '<div class="event-target">' + esc(e.target || 'switch') + '</div>'
      + '<div class="event-why">'
      + '<span class="event-cause ' + cause + '">' + label + '</span>'
      + why
      + '</div>'
      + (detail ? '<div class="event-detail">' + detail + '</div>' : '')
      + '</div>';
  });

  box.innerHTML = html;
}

function drawChart(history, devices) {
  const canvas = document.getElementById('chart');
  const ratio = window.devicePixelRatio || 1;
  const width = canvas.clientWidth;
  const height = 300;
  canvas.width = width * ratio;
  canvas.height = height * ratio;
  const ctx = canvas.getContext('2d');
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, width, height);

  const live = devices.find(d => d.live);
  const pad = { l: 46, r: 40, t: 12, b: 26 };
  const plotW = width - pad.l - pad.r;
  const plotH = height - pad.t - pad.b;

  ctx.font = '10px ui-monospace, Menlo, monospace';
  ctx.strokeStyle = css('--rule');
  ctx.fillStyle = css('--ink-soft');

  if (!live || history.length < 2) {
    ctx.fillText('Waiting for readings. The chart fills in as samples arrive.', pad.l, height / 2);
    return;
  }

  const sn = live.serial;
  const series = [
    { key: sn + ':pv', color: css('--solar'), axis: 'w' },
    { key: sn + ':ac', color: css('--grid'), axis: 'w' },
    { key: sn + ':out', color: css('--load'), axis: 'w' },
    { key: sn + ':soc', color: css('--charge'), axis: 'p' }
  ];

  let maxW = 100;
  history.forEach(p => series.forEach(s => {
    if (s.axis === 'w' && typeof p[s.key] === 'number') maxW = Math.max(maxW, p[s.key]);
  }));
  maxW = Math.ceil(maxW / 100) * 100;

  const t0 = history[0].t;
  const t1 = history[history.length - 1].t;
  const span = Math.max(t1 - t0, 1);

  const x = t => pad.l + (t - t0) / span * plotW;
  const yW = v => pad.t + plotH - (v / maxW) * plotH;
  const yP = v => pad.t + plotH - (v / 100) * plotH;

  ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const yy = pad.t + plotH * i / 4;
    ctx.beginPath();
    ctx.moveTo(pad.l, yy);
    ctx.lineTo(pad.l + plotW, yy);
    ctx.stroke();
    ctx.textAlign = 'right';
    ctx.fillText(Math.round(maxW * (1 - i / 4)) + 'W', pad.l - 8, yy + 3);
    ctx.textAlign = 'left';
    ctx.fillText(Math.round(100 * (1 - i / 4)) + '%', pad.l + plotW + 8, yy + 3);
  }

  ctx.textAlign = 'center';
  [0, 0.5, 1].forEach(f => {
    const tt = t0 + span * f;
    const label = clockLabel(tt);
    ctx.fillText(label, pad.l + plotW * f, height - 8);
  });

  series.forEach(s => {
    ctx.strokeStyle = s.color;
    ctx.lineWidth = s.axis === 'p' ? 1.6 : 1.2;
    if (s.axis === 'p') ctx.setLineDash([4, 3]); else ctx.setLineDash([]);
    ctx.beginPath();
    let started = false;
    history.forEach(p => {
      const v = p[s.key];
      if (typeof v !== 'number') return;
      const px = x(p.t);
      const py = s.axis === 'w' ? yW(v) : yP(v);
      if (!started) { ctx.moveTo(px, py); started = true; }
      else ctx.lineTo(px, py);
    });
    ctx.stroke();
  });
  ctx.setLineDash([]);
}

let latest = null;
let paused = false;

const PANEL_KEY = 'solixauto-collapsed';

function loadCollapsed() {
  try {
    const raw = localStorage.getItem(PANEL_KEY);
    return raw ? JSON.parse(raw) : {};
  } catch (e) { return {}; }
}

let collapsed = loadCollapsed();

function applyPanel(key, isCollapsed) {
  const button = document.querySelector('.panel-toggle[data-panel="' + key + '"]');
  if (!button) return;
  const section = button.closest('section');
  button.setAttribute('aria-expanded', isCollapsed ? 'false' : 'true');
  section.setAttribute('data-collapsed', isCollapsed ? 'true' : 'false');
}

function togglePanel(key) {
  collapsed[key] = !collapsed[key];
  try { localStorage.setItem(PANEL_KEY, JSON.stringify(collapsed)); } catch (e) {}
  applyPanel(key, collapsed[key]);
  if (!collapsed[key] && key === 'history' && latest) {
    drawChart(latest.history, latest.anker);
  }
}

function setCount(key, text) {
  const node = document.getElementById('count-' + key);
  if (node) node.textContent = text;
}

document.querySelectorAll('.panel-toggle').forEach(button => {
  const key = button.dataset.panel;
  applyPanel(key, !!collapsed[key]);
  button.addEventListener('click', () => togglePanel(key));
});

function applyTheme(mode) {
  document.documentElement.setAttribute('data-theme', mode);
  document.getElementById('lightBtn').setAttribute('aria-pressed', mode === 'light');
  document.getElementById('darkBtn').setAttribute('aria-pressed', mode === 'dark');
  try { localStorage.setItem('solixauto-theme', mode); } catch (e) {}
  if (latest) drawChart(latest.history, latest.anker);
}

function applyClockFormat(is12Hour) {
  use12Hour = is12Hour;
  document.getElementById('clock12Btn').setAttribute('aria-pressed', is12Hour);
  document.getElementById('clock24Btn').setAttribute('aria-pressed', !is12Hour);
  try { localStorage.setItem('solixauto-clock12', is12Hour ? '1' : '0'); } catch (e) {}
  if (latest) {
    renderEvents(latest.events, latest.multi);
    if (!collapsed.history) drawChart(latest.history, latest.anker);
  }
}

const stored = (() => {
  try { return localStorage.getItem('solixauto-theme'); } catch (e) { return null; }
})();

applyTheme(stored || (window.matchMedia
  && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'));

const storedClock12 = (() => {
  try { return localStorage.getItem('solixauto-clock12'); } catch (e) { return null; }
})();

applyClockFormat(storedClock12 === '1');

document.getElementById('lightBtn').addEventListener('click', () => applyTheme('light'));
document.getElementById('darkBtn').addEventListener('click', () => applyTheme('dark'));
document.getElementById('clock12Btn').addEventListener('click', () => applyClockFormat(true));
document.getElementById('clock24Btn').addEventListener('click', () => applyClockFormat(false));

let failures = 0;

async function refresh() {
  if (paused) return;
  try {
    const response = await fetch('/api/data', { cache: 'no-store' });
    const data = await response.json();
    latest = data;
    canEdit = !!data.can_edit;
    failures = 0;
    document.getElementById('stamp').textContent = data.updated || '--:--:--';
    document.getElementById('dot').className = data.engine ? 'dot' : 'dot stale';
    renderAnker(data.anker);
    layoutBatteryTags();
    renderShelly(data.shelly);
    renderEvents(data.events, data.multi);

    const live = data.anker.filter(d => d.live);
    const battery = live.length && live[0].battery_soc !== null
      ? live[0].battery_soc + '%' : '';
    setCount('anker', data.anker.length
      ? data.anker.length + (battery ? ', ' + battery : '') : 'none');
    const on = data.shelly.filter(d => d.state === true).length;
    setCount('shelly', data.shelly.length
      ? data.shelly.length + ', ' + on + ' on' : 'none');
    setCount('actions', (data.events || []).length
      ? (data.events || []).length + ' logged' : 'none yet');
    setCount('history', (data.history || []).length + ' samples');

    if (!collapsed.history) drawChart(data.history, data.anker);
  } catch (err) {
    failures++;
    document.getElementById('dot').className = 'dot stale';
    if (failures > 2) {
      document.getElementById('stamp').textContent = 'monitor not responding';
    }
  }
}

refresh();
setInterval(refresh, 5000);
window.addEventListener('resize', () => {
  layoutBatteryTags();
  refresh();
});
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    sampler = None
    allow_edit = True

    def log_message(self, *args):
        pass

    def _send(self, code, body, content_type):
        payload = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path.startswith("/api/data"):
            payload = Handler.sampler.payload()
            payload["can_edit"] = Handler.allow_edit
            self._send(200, json.dumps(payload), "application/json")
            return
        if self.path in ("/", "/index.html"):
            self._send(200, PAGE, "text/html; charset=utf-8")
            return
        self._send(404, "not found", "text/plain")

    def do_POST(self):
        if self.path != "/api/rename":
            self._send(404, json.dumps({"error": "not found"}), "application/json")
            return

        if not Handler.allow_edit:
            self._send(
                403,
                json.dumps(
                    {
                        "error": "Editing is disabled because this monitor is "
                        "reachable from other machines. Restart it without "
                        "--host, or add --allow-remote-edit."
                    }
                ),
                "application/json",
            )
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > 4096:
                raise ValueError("request too large")
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            self._send(400, json.dumps({"error": "bad request"}), "application/json")
            return

        try:
            result = Handler.sampler.rename(
                body.get("kind"),
                body.get("file"),
                body.get("name"),
                bool(body.get("on_device")),
            )
        except ValueError as err:
            self._send(400, json.dumps({"error": str(err)}), "application/json")
            return
        except Exception as err:
            self._send(
                500,
                json.dumps({"error": f"{type(err).__name__}: {err}"}),
                "application/json",
            )
            return

        self._send(200, json.dumps(result), "application/json")


def serve(port=8765, interval=5.0, host="127.0.0.1", allow_remote_edit=False):
    paths.ensure_dirs()

    sampler = Sampler(interval=interval)
    sampler.start()

    local_only = host in ("127.0.0.1", "localhost", "::1")

    Handler.sampler = sampler
    Handler.allow_edit = local_only or allow_remote_edit

    server = ThreadingHTTPServer((host, port), Handler)
    return server, sampler
