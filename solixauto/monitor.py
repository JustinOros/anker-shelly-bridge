import json
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import paths
from .profiles import list_profiles, load_yaml

HISTORY_SIZE = 2880
POLL_TIMEOUT = 3


class Sampler:
    def __init__(self, interval=5.0, history=HISTORY_SIZE):
        self.interval = interval
        self.history = deque(maxlen=history)
        self.snapshot = {"anker": [], "shelly": [], "updated": None, "engine": False}
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
                    "name": identity.get("name") or identity.get("model") or path.stem,
                    "model": identity.get("model") or identity.get("part_number") or "",
                    "serial": serial,
                    "live": bool(live and live.get("fresh")),
                    "updated": (live or {}).get("updated"),
                    "age": (live or {}).get("age_seconds"),
                    "profile": (live or {}).get("profile"),
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

    def _shelly_devices(self):
        devices = []
        for path in list_profiles(paths.SHELLY_PROFILE_DIR):
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

    def _sample(self):
        anker = self._anker_devices()
        shelly = self._shelly_devices()

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
    display: flex;
    height: 16px;
    border: 1px solid var(--ink);
    background:
      repeating-linear-gradient(90deg, transparent 0 5px, var(--etch) 5px 6px);
    overflow: hidden;
  }

  .bus i { display: block; height: 100%; transition: width .5s ease; }
  .bus .in-solar { background: var(--solar); }
  .bus .in-grid { background: var(--grid); }
  .bus .out-load { background: var(--load); }
  .bus .gap { background: transparent; flex: 1 1 auto; }

  .bus-key {
    display: flex;
    gap: 18px;
    margin-top: 7px;
    font-family: ui-monospace, "SF Mono", Menlo, monospace;
    font-size: 10px;
    color: var(--ink-soft);
    flex-wrap: wrap;
  }

  .bus-key em { font-style: normal; }
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
    </div>
  </header>

  <section>
    <h2>Anker SOLIX</h2>
    <div id="anker"></div>
  </section>

  <section>
    <h2>Shelly switches</h2>
    <div id="shelly"></div>
  </section>

  <section>
    <div class="chart-head">
      <h2 style="margin:0">Recent history</h2>
      <div class="legend">
        <span><i class="swatch sw-solar"></i>solar W</span>
        <span><i class="swatch sw-grid"></i>grid in W</span>
        <span><i class="swatch sw-load"></i>load W</span>
        <span><i class="swatch sw-batt"></i>battery %</span>
      </div>
    </div>
    <canvas id="chart"></canvas>
  </section>

</div>

<script>
const css = name => getComputedStyle(document.documentElement)
  .getPropertyValue(name).trim();

const fmt = (v, unit) => v === null || v === undefined
  ? '<span>--</span>'
  : Math.round(v) + '<span>' + unit + '</span>';

function busBar(pv, ac, out) {
  const scale = Math.max(pv || 0, ac || 0, out || 0, 100);
  const w = v => ((v || 0) / scale * 100).toFixed(1) + '%';
  return '<div class="bus">'
    + '<i class="in-solar" style="width:' + w(pv) + '"></i>'
    + '<i class="in-grid" style="width:' + w(ac) + '"></i>'
    + '<i class="gap"></i>'
    + '<i class="out-load" style="width:' + w(out) + '"></i>'
    + '</div>'
    + '<div class="bus-key">'
    + '<em><i class="swatch sw-solar"></i>in from sun</em>'
    + '<em><i class="swatch sw-grid"></i>in from grid</em>'
    + '<em><i class="swatch sw-load"></i>out to load</em>'
    + '</div>';
}

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
        + '<span class="device-name">' + d.name + '</span>'
        + '<span class="device-meta">' + d.model + '</span></div>'
        + '<p class="empty">No live readings. Live data comes from a running '
        + 'automation.<br>Start one with <b>solixauto service &lt;profile&gt;</b>.</p></div>';
    }
    return '<div class="device">'
      + '<div class="device-head">'
      + '<span class="device-name">' + d.name + '</span>'
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
        return '<tr><td>' + d.name + '</td>'
          + '<td class="mono">' + (d.host || '--') + ' ch' + d.channel + '</td>'
          + '<td class="mono">' + (d.watts === null || d.watts === undefined ? '--' : Math.round(d.watts) + ' W') + '</td>'
          + '<td><span class="pill ' + cls + '">' + text + '</span></td></tr>';
      }).join('')
    + '</tbody></table>';
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
    const d = new Date(tt * 1000);
    const label = String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
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

function applyTheme(mode) {
  document.documentElement.setAttribute('data-theme', mode);
  document.getElementById('lightBtn').setAttribute('aria-pressed', mode === 'light');
  document.getElementById('darkBtn').setAttribute('aria-pressed', mode === 'dark');
  try { localStorage.setItem('solixauto-theme', mode); } catch (e) {}
  if (latest) drawChart(latest.history, latest.anker);
}

const stored = (() => {
  try { return localStorage.getItem('solixauto-theme'); } catch (e) { return null; }
})();

applyTheme(stored || (window.matchMedia
  && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'));

document.getElementById('lightBtn').addEventListener('click', () => applyTheme('light'));
document.getElementById('darkBtn').addEventListener('click', () => applyTheme('dark'));

let failures = 0;

async function refresh() {
  try {
    const response = await fetch('/api/data', { cache: 'no-store' });
    const data = await response.json();
    latest = data;
    failures = 0;
    document.getElementById('stamp').textContent = data.updated || '--:--:--';
    document.getElementById('dot').className = data.engine ? 'dot' : 'dot stale';
    renderAnker(data.anker);
    renderShelly(data.shelly);
    drawChart(data.history, data.anker);
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
window.addEventListener('resize', () => refresh());
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    sampler = None

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
            self._send(
                200, json.dumps(Handler.sampler.payload()), "application/json"
            )
            return
        if self.path in ("/", "/index.html"):
            self._send(200, PAGE, "text/html; charset=utf-8")
            return
        self._send(404, "not found", "text/plain")


def serve(port=8765, interval=5.0, host="127.0.0.1"):
    paths.ensure_dirs()

    sampler = Sampler(interval=interval)
    sampler.start()

    Handler.sampler = sampler
    server = ThreadingHTTPServer((host, port), Handler)
    return server, sampler
