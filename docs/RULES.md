# Power profiles

A power profile is a plain YAML file linking one Anker SOLIX device to one
Shelly switch channel. You can edit it in any text editor.

The Anker device is **read-only**. The engine reads its telemetry and never
sends it a command. The only thing that gets switched is the Shelly.

## Layout

    device-profiles/anker/     generated, one file per Anker device
    device-profiles/shelly/    generated, one file per Shelly device
    power-profiles/            yours, hand-edited
    state/runtime.json         last known target state per profile
    logs/automation.log        action history

## Workflow

    solixauto discover-anker
    solixauto discover-shelly
    solixauto new-profile solar-failover --template solar
    solixauto fields A1782-<serial>
    solixauto run solar-failover --test
    solixauto run solar-failover

## Anatomy of a rule

    - name: grid assist when solar drops
      when: pv_total < 150
      for: 90s
      then: target.on
      priority: 0

`when` is an expression over any field listed in the Anker device profile,
under `readable` or `derived`. `solixauto fields <device>` prints them with
current sample values.

`for` is the dwell time. The condition must stay true for this long before
anything happens. Without it, a cloud passing over your array would cycle the
relay repeatedly.

`then` is `target.on`, `target.off`, or `none`.

`priority` breaks ties. If two rules are ready at the same moment and disagree,
the higher number wins. A low-battery override should outrank normal solar
logic.

## Use a deadband

This is the single most important thing to get right:

    # WRONG - will chatter around 200W
    - when: pv_total < 200
      then: target.on
    - when: pv_total > 200
      then: target.off

    # RIGHT - 150W of deadband between the two
    - when: pv_total < 150
      then: target.on
    - when: pv_total > 300
      then: target.off

`--test` warns when it detects the same threshold used for both directions.

## Useful automations

**Solar failover.** Solar covers the load most of the day; pull from the wall
only when production drops.

    - name: grid assist when solar drops
      when: pv_total < 150
      for: 90s
      then: target.on

    - name: release grid when solar recovers
      when: pv_total > 300
      for: 2m
      then: target.off

**Low-battery charge.** No solar involved.

    - name: charge when battery is low
      when: battery_soc <= 15
      for: 30s
      then: target.on

    - name: stop charging when battery is healthy
      when: battery_soc >= 60
      for: 2m
      then: target.off

**Overnight top-up.** Combine conditions.

    - name: cheap overnight charging
      when: battery_soc < 80 and pv_total < 20
      for: 5m
      then: target.on

**Load shedding.** Cut a non-essential circuit when the battery is draining.

    - name: shed load
      when: battery_soc < 30 and ac_input_power == 0
      for: 2m
      then: target.off

**Thermal guard.** Higher priority so it outranks everything else.

    - name: stop charging when hot
      when: temperature >= 45
      for: 60s
      then: target.off
      priority: 200

## Safety settings

`stale_after` and `on_stale` control what happens when telemetry stops
arriving. Rules are never evaluated against stale data.

- `hold` keeps the relay wherever it is. Default and safest.
- `safe_state` drives the relay to the `safe_state:` value.
- `stop` exits the engine.

`limits` is a hard backstop independent of dwell times:

    limits:
      min_seconds_between_actions: 60
      max_actions_per_hour: 20

If a rule somehow oscillates, this caps the damage.

## Notifications

Turn them on in the profile:

    notifications:
      enabled: true
      channels: [ntfy]
      title: "{profile}"
      template: >-
        {source_name} battery {battery_soc}%, solar {pv_total}W.
        {target_name} turned {action}.
      throttle: 5m
      on:
        - action

Then per rule, `notify: on` or `notify: off` to include or exclude it. Omit it
to inherit. A rule can also carry its own wording:

    - name: emergency charge on low battery
      when: battery_soc <= 15
      for: 30s
      then: target.on
      priority: 100
      notify:
        template: >-
          {source_name} has {battery_soc}% battery remaining.
          {target_name} turned on AC power.
        priority: high

### Template fields

Any field from the device profile works, plus:

    {profile}         power profile name
    {rule}            rule that fired
    {condition}       the rule's when expression
    {action}          ON or OFF
    {action_word}     on or off
    {source_name}     Anker device name, falling back to model
    {source_model}    e.g. SOLIX F3000
    {source_serial}
    {target_name}     Shelly device name, falling back to model
    {target_model}
    {target_host}
    {time}

`--test` checks every field name in your templates against the device profile,
so a typo is caught before it ships a message reading `battery ?%`.

### Channels

Credentials live in `../notifications.yaml`, which is created with owner-only
permissions. Power profiles stay free of secrets.

- **ntfy** - recommended. Free, no account. Install the app, pick an
  unguessable topic name, subscribe. Anyone who knows the topic can read your
  alerts, so make it long.
- **pushover** - $5 once per platform. Priority 2 alerts repeat until you
  acknowledge them.
- **email** - SMTP. Gmail requires an App Password.
- **telegram** - free bot.
- **webhook** - Slack, Discord, or generic JSON.
- **desktop** - local notification on the machine running the engine.
  Uses osascript on macOS, notify-send on Linux, PowerShell on Windows.
  Run `solixauto doctor` to see which backend was detected.

The Anker app and the Shelly app cannot receive custom push messages from an
external program, so neither is an option here.

Test a channel:

    solixauto notify-test
    solixauto notify-test --channel ntfy

### Throttling

`throttle` is per rule. An identical repeated message inside the window is
dropped. This is separate from the switching rate limits, so a stuck condition
cannot flood your phone even if the relay is behaving.

### Other events

    on:
      - action
      - stale

`stale` fires once when telemetry stops arriving and is worth enabling if you
depend on the automation.

## Testing

    solixauto run <profile> --test

Validates syntax, checks that every field you reference actually exists on the
device, warns about missing deadbands and short dwell times, connects to both
devices, and prints what each rule would do right now. Nothing is switched.

    solixauto run <profile> --test --offline

Same checks without connecting. Rules are evaluated against the sample values
captured during discovery.
