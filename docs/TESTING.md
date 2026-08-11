# Testing before you publish

Work down this list. Each stage only depends on the ones above it, so a failure
tells you exactly which layer is broken. Nothing switches real hardware until
stage 5.

## What is already verified

Rule parsing, the expression sandbox, dwell timing, priority resolution, rate
limiting, profile validation, template rendering, and notification throttling
all have test coverage.

## What has never run against real hardware

Every Anker library call, every Shelly HTTP call, every notification channel,
and the engine loop end to end. Those are what these stages exercise.

---

## Stage 1 - environment

    python solixauto.py doctor

Expect: no PROBLEM lines. Warnings about optional packages are fine.

If `anker-solix-api` shows MISSING, you are running the wrong interpreter. Use
the venv that already works with mqtt_monitor.

---

## Stage 2 - Anker discovery

    python solixauto.py discover-anker

Expect: one profile per owned device, each reporting a field count.

    python solixauto.py devices
    python solixauto.py fields A1782-<serial>

Check specifically:

- `pv_total` appears under derived, and equals `pv_1_power + pv_2_power`
- `battery_soc` is present and matches the Anker app right now
- the field count is roughly what mqtt_monitor showed you

**If a device reports no telemetry:** it is powered off, asleep, off WiFi, or
Bluetooth-only.

Some models pair with the Anker app over Bluetooth only. You press a button on
the unit to make it discoverable and it holds no persistent cloud connection.
The F2000 / PowerHouse 767 works this way. Those devices publish nothing to
Anker's MQTT broker and the broker refuses the subscription with
`Unspecified error(128)`. They cannot be automation sources here — local
Bluetooth access would need a different project (SolixBLE).

Discovery skips devices the cloud reports as disconnected before subscribing,
so they cost no time. To keep one out permanently:

    python solixauto.py discover-anker --skip <serial>

For a device that is genuinely cloud connected but reported otherwise, force it
with `--include-offline`.

Otherwise, confirm it shows as online in the Anker app, then rerun for that
device alone with `--sn <serial>`.

Discovery never overwrites a good profile with an empty one, so re-running with
a device switched off is harmless.

**Most likely failure:** an `AttributeError` or `TypeError` from
`update_sites`, `get_bind_devices`, `startMqttSession`, or the device factory.
I derived those calls from the library's C1000X example rather than running
them. If one breaks, send me the traceback and the exact line.

**Also check:** verify `pv_total` against the Anker app using LIVE data, not the
samples in the profile. `fields` shows the snapshot captured at discovery time;
use `status` for a live read:

    python solixauto.py status <anker-profile> --fields pv battery --watch

Open the Anker app side by side and compare the combined solar watts. If they
disagree, the field mapping is wrong and every solar rule you write will be
wrong with it. This is community-reverse-engineered, not documented by Anker.

Worth watching for a few minutes across a change in conditions, so you can see
`pv_total` track the app rather than matching once by coincidence.

---

## Stage 3 - Shelly discovery

    python solixauto.py discover-shelly

Expect: one profile per Shelly, with the right host and channel count.

If mDNS finds nothing, fall back:

    python solixauto.py discover-shelly --host 192.168.1.50
    python solixauto.py discover-shelly --network 192.168.1.0/24

Give every Shelly a DHCP reservation before going further. If a plug changes IP,
automation silently stops working.

---

## Stage 4 - manual switch

This is the first stage that moves a relay. **Plug the Shelly into a lamp, not
into anything that matters.**

    python solixauto.py switch <shelly-profile> status
    python solixauto.py switch <shelly-profile> on
    python solixauto.py switch <shelly-profile> off

Expect: state reads back correctly, and the command is confirmed by re-reading
the device rather than trusting the HTTP response.

If this fails, control is broken and no rule will work. Check auth in the
profile if the device has a password set.

---

## Stage 4b - check for competing automation

    python solixauto.py conflicts <shelly-profile>

A Shelly can hold schedules, auto-off timers, and webhooks on the device itself.
They run independently of this tool and will silently override it. Remove them
in the Shelly app before going further, or you will spend a long time debugging
rules that were working correctly.

Shelly Cloud scenes are not visible locally, so also check the app's scenes.

## Stage 5 - notifications

    python solixauto.py notify-setup
    # enable one channel, then
    python solixauto.py notify-test

Expect: `ok` per channel and a message on your phone.

ntfy tip: use a long random topic name. Anyone who knows it can read your
alerts.

---

## Stage 6 - profile validation

    python solixauto.py new-profile solar-failover --template solar
    python solixauto.py run solar-failover --test --offline

Expect: syntax OK, no PROBLEM lines, and a rendered notification per rule with
your real device names in it.

---

## Stage 7 - forced rule firing

Prove the logic without waiting for the sun to set:

    python solixauto.py run solar-failover --test --simulate battery_soc=12 pv_total=0

Expect: the low-battery rule reports TRUE, the release rule reports false, and
the notification text reads the way you want it to read. Nothing is switched.

Try a few combinations. This is where you catch a rule that is inverted or a
threshold that is off by a decimal place.

---

## Stage 8 - live dry run

    python solixauto.py run solar-failover --test

Now it connects to both devices with real telemetry. Expect: Shelly reachable,
real values, dwell countdowns.

Then run the real loop with switching still disabled:

    python solixauto.py run solar-failover --dry-run

Leave it for an hour. Expect log lines saying what it *would* do. Confirm the
decisions match what you would have made by hand.

---

## Stage 9 - live, on a lamp

    python solixauto.py run solar-failover

Still on the lamp. Watch for a full day, ideally one with variable cloud, which
is what exposes missing deadbands.

Check `logs/automation.log` for:

- action counts that look sane, not dozens per hour
- no `suppressed` lines hitting the hourly cap, which means a rule is
  oscillating
- notifications arriving when you expect and not otherwise

---

## Stage 10 - real load

Only after stage 9 has been clean for a full day. Move the Shelly to the real
circuit and keep watching the log for another day.

---

## Stage 11 - run it unattended

    python solixauto.py service <profile>
    python solixauto.py service <profile> --status

Installs a LaunchAgent on macOS or a systemd user unit on Linux, starting at
login and restarting on crash. Check the log after an hour, and again after a
reboot, before trusting it.

Removing the service leaves the Shelly in whatever state it was last set to.
Check the plug before you walk away.

## Before publishing

- [ ] `device-profiles/` and `state/` are gitignored (the generated `.gitignore`
      covers this, but verify — profiles contain your serial numbers)
- [ ] `notifications.yaml` is not committed
- [ ] no `.env` in the repo
- [ ] `git log -p | grep -i -E "password|token|@gmail|user_key"` comes back empty
- [ ] README states the project is unofficial and unaffiliated with Anker or
      Allterco
- [ ] no Anker or Shelly logos in the repo
- [ ] the example profiles reference placeholder serials, not yours
