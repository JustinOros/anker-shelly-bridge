from . import paths
from .profiles import list_profiles, write_text

POWER_PROFILE_TEMPLATE = """# Power profile: __NAME__
#
# Links ONE Anker SOLIX device (read-only data source) to ONE Shelly
# switch channel (the actuator). Rules decide when the Shelly turns on
# or off. The Anker device is never commanded.
#
# Validate before running:
#     __INVOCATION__ run __NAME__ --test
#
# ---------------------------------------------------------------------
# QUICK REFERENCE
# ---------------------------------------------------------------------
# when:      an expression over fields from the Anker device profile.
#            Use field names directly. Allowed: < <= > >= == !=
#            and / or / not, + - * /, and parentheses.
#            Run `__INVOCATION__ fields __SOURCE__` to list every name.
#
# for:       how long the condition must hold continuously before acting.
#            Prevents a passing cloud from toggling the relay. 30s 5m 1h.
#
# then:      target.on, target.off, or none.
#
# priority:  when several rules are ready at once, the highest number
#            wins. Default 0.
#
# notify:    on or off per rule. Omit to inherit the profile default.
#            Only matters when notifications.enabled is true below.
#
# ---------------------------------------------------------------------

name: __NAME__
description: >
  Describe what this profile is for.

enabled: true

poll_interval: 10s

source:
  profile: __SOURCE__
  stale_after: 120s
  on_stale: safe_state

target:
  profile: __TARGET__
  channel: __CHANNEL__

safe_state: on

# SAFETY FLOOR - evaluated before any rule below, and it bypasses the rate
# limits. Once tripped it LATCHES: nothing can turn the target off again until
# the battery climbs back to release_at. Set this whenever the target controls
# charging for the source device, so the battery can never be stranded at 0%.
safety:
  battery_floor:
    at_or_below: 25
    release_at: 45
    then: target.on
    for: 30s
    notify: true
    notify_release: true

# Notifications fire when a rule actually switches the target.
# Channels and credentials live in ../notifications.yaml, not here.
#   solixauto notify-test
notifications:
  enabled: false
  channels: []
  title: "{profile}"
  template: >-
    {source_name} battery {battery_soc}%, solar {pv_total}W.
    {target_name} turned {action}.
  throttle: 5m
  on:
    - action

rules:
__RULES__
limits:
  min_seconds_between_actions: 60
  max_actions_per_hour: 20
"""

RULES_SOLAR = """  - name: grid assist when solar drops
    when: pv_total < 150
    for: 90s
    then: target.on

  - name: release grid when solar recovers
    when: pv_total > 300
    for: 2m
    then: target.off

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
"""

RULES_BATTERY = """  - name: charge when battery is low
    when: battery_soc <= 15
    for: 30s
    then: target.on
    notify:
      template: >-
        {source_name} has {battery_soc}% battery remaining.
        {target_name} turned on AC power.

  - name: stop charging when battery is healthy
    when: battery_soc >= 60
    for: 2m
    then: target.off
"""

RULES_MINIMAL = """  - name: turn on
    when: battery_soc <= 20
    for: 60s
    then: target.on

  - name: turn off
    when: battery_soc >= 50
    for: 60s
    then: target.off
"""

RULE_SETS = {
    "solar": RULES_SOLAR,
    "battery": RULES_BATTERY,
    "minimal": RULES_MINIMAL,
}

README_HEADER = """# Power profiles

> Commands below are written as `solixauto`. If that is not on your PATH, run
> them the same way you ran the tool, for example:
>
>     __INVOCATION__ run <profile> --test
"""

README = """# Power profiles

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

**Let solar do the work.** Stop grid charging once the sun is carrying the load
and has spare capacity for the battery. `pv_surplus` is solar minus everything
drawing from the unit, so positive means the surplus is going into the battery.

    - name: solar covers the load, stop grid charging
      when: pv_surplus > 100
      for: 5m
      then: target.off

    - name: solar cannot keep up, fall back to grid
      when: pv_surplus < -100 and battery_soc <= 60
      for: 10m
      then: target.on

The 100W band on either side of zero is the deadband; without it, a load
cycling on and off would flip the relay every few minutes. The SOC condition on
the second rule stops it reaching for the grid on a cloudy afternoon when the
battery is still comfortable.

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

## The safety floor

This is not a rule. It is checked before every rule, it ignores the rate limits,
and once tripped it latches.

    safety:
      battery_floor:
        at_or_below: 25
        release_at: 45
        then: target.on
        for: 30s
        notify: true

Set this whenever the Shelly controls charging for the Anker device itself.
Without it you can deadlock: a rule turns charging off, the battery runs flat,
the device drops off wifi, telemetry goes stale, and nothing ever turns charging
back on.

`release_at` must be meaningfully higher than `at_or_below`. While latched, no
rule can turn the target off, so the battery gets a real recharge instead of
being released at 26% into the same conditions that drained it.

Pair it with a stale policy that fails toward charging:

    source:
      stale_after: 300s
      on_stale: safe_state

    safe_state: on

`--test` warns when no floor is set.

### Floor notifications

The floor notifies on both trip and release, and it does this **even when
`notifications.enabled` is false**. Turning off routine solar chatter should not
silence a battery emergency. All it needs is one enabled channel in
`notifications.yaml`. Push channels get urgent priority, and the throttle is
bypassed.

    safety:
      battery_floor:
        at_or_below: 25
        notify: true
        notify_release: true
        notify_template: >-
          SAFETY: {source_name} battery at {value}. {target_name} turned
          {action} to protect it. Holding until {release}.
        release_template: >-
          {source_name} battery recovered to {value}. Normal rules resumed
          for {target_name}.

Extra fields available in these two templates: `{value}`, `{field}`,
`{threshold}`, `{release}`.

If the floor trips and no channel is configured, the log says so explicitly
rather than failing silently.

## Conflicts with the Shelly's own automation

A Shelly can run schedules, auto-on/auto-off timers, and webhooks entirely on
the device. Those keep running whether or not this engine is running, and they
will fight your rules. A schedule that turns the plug off at 08:00 will undo a
rule that just turned it on, and nothing in the log will explain why.

Check before you rely on anything:

    solixauto conflicts <shelly-profile>

To remove them without leaving the terminal:

    solixauto conflicts <shelly-profile> --fix

That asks before every change and defaults to No. Deletions happen on the
device and cannot be undone from here; you would have to recreate them in the
Shelly app.

Discovery records what it finds, `--test` lists it as notes, and the engine
prints a loud warning at startup if anything is still there.

Remove them in the Shelly app, or accept that the device wins.

One limitation: schedules created as Shelly Cloud **scenes** live in the cloud
rather than on the device, and cannot be seen over local HTTP. If behaviour
still looks wrong after `conflicts` comes back clean, check the app's scenes.

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
"""


def scaffold_readme():
    destination = paths.POWER_PROFILE_DIR / "README.md"
    header = README_HEADER.replace("__INVOCATION__", paths.invocation())
    body = README.split("\n", 1)[1] if README.startswith("# Power profiles") else README
    write_text(destination, header.rstrip() + "\n" + body)
    return destination


def default_source():
    profiles = list_profiles(paths.ANKER_PROFILE_DIR)
    return profiles[0].name if profiles else "REPLACE-WITH-ANKER-PROFILE.yaml"


def default_target():
    profiles = list_profiles(paths.SHELLY_PROFILE_DIR)
    return profiles[0].name if profiles else "REPLACE-WITH-SHELLY-PROFILE.yaml"


def render(name, source=None, target=None, channel=0, template="solar"):
    rules = RULE_SETS.get(template, RULES_SOLAR)
    output = POWER_PROFILE_TEMPLATE
    for token, value in (
        ("__NAME__", name),
        ("__SOURCE__", source or default_source()),
        ("__TARGET__", target or default_target()),
        ("__CHANNEL__", str(channel)),
        ("__RULES__", rules),
        ("__INVOCATION__", paths.invocation()),
    ):
        output = output.replace(token, value)
    return output


def create(name, source=None, target=None, channel=0, template="solar", force=False):
    paths.ensure_dirs()
    destination = paths.POWER_PROFILE_DIR / f"{name}.yaml"
    if destination.exists() and not force:
        raise FileExistsError(destination)
    write_text(destination, render(name, source, target, channel, template))
    scaffold_readme()
    return destination
