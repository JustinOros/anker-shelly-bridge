# anker-shelly-bridge

Automate a Shelly smart plug from live Anker SOLIX telemetry.

Your SOLIX device reports battery level, solar input, and load over Anker's
cloud MQTT service. This reads that telemetry and switches a Shelly plug on
your local network according to rules you write in a plain text file.

The common use is controlling grid charging: plug the SOLIX into a Shelly, and
let the battery level decide when to draw from the wall.

**The SOLIX device is read-only.** This tool never sends it a command. The
Shelly plug is the only thing it ever switches.

> Unofficial and unaffiliated with Anker or Allterco Robotics. SOLIX and Shelly
> are trademarks of their respective owners. Built on the community
> [anker-solix-api](https://github.com/thomluther/anker-solix-api) library.

---

## Quick start

You need a SOLIX device that connects to WiFi, a Shelly plug on the same
network, and your Anker account login.

```bash
git clone https://github.com/JustinOros/anker-shelly-bridge.git
cd anker-shelly-bridge
./start.sh
```

On Windows, double-click `start.bat`.

That is the whole install. `start.sh` creates its own Python environment,
installs everything, and hands over to a guided setup with eight steps:

1. **Dependencies** — installs whatever is missing
2. **Anker account** — your login, stored locally with owner-only permissions
3. **Find your SOLIX device** — connects and captures every field it reports
4. **Find your Shelly plug** — scans the local network
5. **Check the plug** — finds schedules and timers already on it that would
   conflict, and offers to remove them
6. **Notifications** — optional push to your phone, with a QR code to scan
7. **Rules** — pick a strategy and answer a few questions in plain language
8. **Test, then start** — a live dry run against your hardware that switches
   nothing, then offers to install a background service

Nothing touches your hardware until step 8 asks. Ctrl-C is safe at any point.

Needs Python 3.12+ and git. If they are missing, `start.sh` tells you how to
install them for your platform.

---

## What it does

### Reads your SOLIX device

Connects to Anker's cloud MQTT broker, captures a live telemetry snapshot, and
writes a device profile you can read. A portable power station typically
reports around 44 fields: battery percentage, per-string solar input, AC and DC
output, temperature, port states.

It also computes derived fields, so rules can use `pv_total` instead of adding
`pv_1_power` and `pv_2_power` by hand:

| Field | Meaning |
|---|---|
| `pv_total` | all solar strings added together, in watts |
| `pv_surplus` | solar minus load; positive means the surplus is charging the battery |
| `pv_covers_load` | true when solar alone is carrying everything |
| `usb_total` | combined USB output |
| `soc_headroom` | percentage points above the configured floor |

```bash
solixauto fields <device>              # every usable field name
solixauto status <device> --watch      # live values
```

### Controls your Shelly plug

Local HTTP only, no cloud. Gen1 and Gen2+ including Gen4. Discovery uses mDNS
plus a subnet scan across every private network it can see, so machines with
several interfaces work.

```bash
solixauto switch <plug> on|off|status
```

Every command reads the state back from the device afterwards rather than
trusting the HTTP response.

### Runs your rules

A power profile is a YAML file linking one SOLIX device to one Shelly channel:

```yaml
rules:
  - name: top up from grid when low
    when: battery_soc <= 35
    for: 2m
    then: target.on

  - name: stop charging when full enough
    when: battery_soc >= 85
    for: 2m
    then: target.off
```

`when` is an expression over any field the device reports. `for` is how long
the condition must hold before anything happens, so a passing cloud or a
momentary load spike cannot toggle the relay. `priority` breaks ties when rules
disagree.

Expressions run in a sandbox allowing only comparisons, `and`/`or`/`not`, and
arithmetic. Function calls and attribute access are rejected at parse time, so
a power profile cannot execute code.

Full rule reference with worked examples: [docs/RULES.md](docs/RULES.md).
The same document is written into your data directory during setup.

---

## Safety

Controlling the power supply to a battery has a specific failure mode: turn
charging off, let the battery run flat, and the device drops off the network —
at which point nothing can turn charging back on. The design is built around
preventing that.

### The battery floor

```yaml
safety:
  battery_floor:
    at_or_below: 20
    release_at: 40
    then: target.on
```

Checked before any rule. It **bypasses the rate limits**, **outranks every
rule**, and **latches** once tripped — nothing can turn the plug off again
until the battery reaches `release_at`. Without the latch, a solar rule could
release it at 21% straight back into whatever drained it.

`release_at` must be meaningfully above `at_or_below`; the profile is rejected
otherwise.

### Failing in the safe direction

```yaml
source:
  stale_after: 300s
  on_stale: safe_state
safe_state: on
```

If telemetry stops arriving, rules are never evaluated against stale values.
`hold` leaves the plug alone, `safe_state` drives it to a known state, `stop`
exits. When the plug supplies power to the SOLIX device, `safe_state: on` means
losing sight of it fails toward charging.

Rules are also never evaluated against **partial** telemetry. If a field a rule
needs has not arrived yet, nothing is evaluated at all — including the floor.

### Rate limits

```yaml
limits:
  min_seconds_between_actions: 60
  max_actions_per_hour: 20
```

A hard backstop independent of dwell times. If a rule somehow oscillates, this
caps the damage. The battery floor deliberately ignores it.

### Conflict detection

A Shelly can hold schedules, auto-on/auto-off timers, and webhooks on the
device itself. They run whether or not this tool is running, and they silently
override it.

```bash
solixauto conflicts <plug>          # list them
solixauto conflicts <plug> --fix    # remove them, asking before each change
```

Checked at discovery, at `--test`, and again at engine startup.

One limitation: schedules created as Shelly Cloud **scenes** live in the cloud
rather than on the device and cannot be seen over local HTTP. If behaviour
still looks wrong after `conflicts` is clean, check the app's scenes.

---

## Testing before you trust it

```bash
solixauto run <profile> --test
```

Validates syntax, checks every field name against what the device actually
reports, warns about missing deadbands and short dwell times, connects to both
devices, and prints what each rule would do right now. Switches nothing.

```bash
solixauto run <profile> --test --offline
```

Same checks without connecting, evaluated against values captured at discovery.

```bash
solixauto run <profile> --test --simulate battery_soc=12
```

Force a rule to fire without waiting for real conditions. Prove your
low-battery logic works at 2pm on a sunny day, and see the exact notification
text it would send.

```bash
solixauto run <profile> --dry-run
```

The full engine loop with real telemetry, narrating every cycle, but no switch
command and no notification.

---

## Notifications

```bash
solixauto notify-setup
```

Interactive: pick a channel, it configures and tests it. For ntfy it generates
a random private topic, shows QR codes for the app store and the topic, and
sends a test push.

Supported: **ntfy** (free, no account), **Pushover**, **email**, **Telegram**,
**webhook** (Slack/Discord/custom), and **desktop** (osascript on macOS,
notify-send on Linux, PowerShell on Windows).

Credentials live in `notifications.yaml` with owner-only permissions, never in
power profiles, so profiles stay safe to share.

Battery floor alerts fire **even when notifications are otherwise disabled**,
at elevated priority, bypassing the throttle. Muting routine chatter should not
silence a battery emergency.

The Anker app and the Shelly app cannot receive custom push messages from an
external program, so neither is an option.

---

## Running it unattended

```bash
solixauto service <profile>              # install and start
solixauto service <profile> --status
solixauto service <profile> --uninstall
```

macOS gets a LaunchAgent, Linux a systemd user unit, both starting at login and
restarting on crash. Windows prints the Task Scheduler command.

If the machine sleeps, the automation sleeps with it and the safety floor
cannot protect anything. Use an always-on machine, or disable sleep.

---

## Layout

Code lives where you cloned it. Everything else lives in `~/solix-automation`:

```
~/solix-automation/
├── venv/                     Python environment created by start.sh
├── device-profiles/
│   ├── anker/                what your SOLIX device reports
│   └── shelly/               your plugs and their capabilities
├── power-profiles/           your rules, hand-editable
│   └── README.md             full rule reference with worked examples
├── notifications.yaml        channel credentials, mode 0600
├── state/runtime.json        last known target state
└── logs/automation.log       every action taken
```

Override the root with `SOLIXAUTO_HOME`.

---

## Commands

```
solixauto setup                     guided setup, start here
solixauto doctor                    check this machine is set up correctly

solixauto discover-anker            find and profile SOLIX devices
solixauto discover-shelly           find and profile Shelly devices
solixauto devices                   list saved device profiles
solixauto name <device> "<name>"    name a device, rename its file
solixauto fields <device>           field names usable in rules
solixauto status <device>           live telemetry
solixauto switch <plug> on|off      manual control
solixauto conflicts <plug>          automation set on the plug itself

solixauto new-profile <name>        scaffold a power profile
solixauto profiles                  list power profiles
solixauto validate <profile>        check without connecting
solixauto run <profile> --test      test against real devices, switch nothing
solixauto run <profile>             run it

solixauto notify-setup              configure push notifications
solixauto notify-test               send a test
solixauto notify-qr                 show the ntfy topic QR again

solixauto service <profile>         run in the background at login
```

Profiles resolve by friendly name, serial, ID, MAC, or IP, so all of these
reach the same device:

```bash
solixauto switch "Garage Plug" on
solixauto switch 192.168.1.50 on
solixauto switch garage-plug on
```

---

## Device support

Anything the upstream
[anker-solix-api](https://github.com/thomluther/anker-solix-api) library
supports **and** that holds a cloud connection.

Models pairing with the Anker app over Bluetooth only — press a button on the
unit to make it discoverable — publish nothing to the MQTT broker and cannot be
used. The F2000 / PowerHouse 767 works this way. Discovery detects and skips
them.

Shelly Gen1 and Gen2+ including Gen4, over local HTTP.

Developed against a SOLIX F3000 (A1782) and a Shelly Plug US Gen4. Other
combinations should work; reports welcome.

---

## Known limitations

- Field mappings come from a community reverse-engineering effort, not from
  Anker. Fields ending in `?` or starting with `unknown_` are unconfirmed —
  do not build rules on them.
- **Verify any field against the Anker app before trusting it.** The solar
  fields in particular should be watched in daylight and compared to the app
  before writing solar rules.
- Shelly Cloud scenes and app-set device names are invisible over local HTTP.
- Requires a machine that stays awake.
- Anker rate-limits logins; avoid restarting the service in a tight loop.

---

## Troubleshooting

**A device reports no telemetry.** It is powered off, asleep, off WiFi, or
Bluetooth-only. Confirm it shows online in the Anker app, then retry that
device alone with `--sn <serial>`.

**Shelly discovery finds nothing.** Try `--host 192.168.1.50` or
`--network 192.168.1.0/24`. Give your plugs DHCP reservations; `access.host` is
a fixed address in the profile.

**The automation does nothing.** Check `solixauto service <profile> --status`
and the log. Rules only act after their `for:` dwell has fully elapsed.

**Something switches the plug unexpectedly.** Run `solixauto conflicts <plug>`,
then check the Shelly app for cloud scenes.

**Anything else.** `solixauto doctor` reports platform, dependencies,
credentials, network, and notification status in one place.

---

## Documentation

- [docs/RULES.md](docs/RULES.md) — power profile format, every option, worked
  automation examples
- [docs/TESTING.md](docs/TESTING.md) — staged checklist for validating a new
  setup before trusting it with real hardware
- [examples/](examples/) — a solar failover profile and an annotated
  notifications config

## Contributing

Bug reports welcome, particularly for device models not listed above. Include
the output of `solixauto doctor` and the relevant section of
`logs/automation.log`.

**Never paste `notifications.yaml`, your `.env`, or an ntfy topic** into an
issue. Device profiles contain serial numbers; redact them if that matters to
you.

## License

MIT. See [LICENSE](LICENSE).
