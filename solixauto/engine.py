import asyncio
import json
import sys
import time
from collections import deque
from datetime import datetime

import aiohttp

from . import paths
from .profiles import load_yaml
from .notify import Notifier, render
from .rules import derived_values, format_duration, validate
from .shelly import ShellyTarget


async def interruptible_sleep(seconds):
    remaining = float(seconds or 0)
    while remaining > 0:
        chunk = min(0.5, remaining)
        await asyncio.sleep(chunk)
        remaining -= chunk


def configure_event_loop():
    if sys.platform.startswith("win"):
        policy = getattr(asyncio, "WindowsSelectorEventLoopPolicy", None)
        if policy is not None:
            asyncio.set_event_loop_policy(policy())


def stamp():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class Reporter:
    def __init__(self, log_path=None, quiet=False):
        self.quiet = quiet
        self.handle = None
        if log_path:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            self.handle = open(log_path, "a", encoding="utf-8")

    def __call__(self, message, force=False):
        line = f"[{stamp()}] {message}"
        if not self.quiet or force:
            print(line, flush=True)
        if self.handle:
            self.handle.write(line + "\n")
            self.handle.flush()

    def close(self):
        if self.handle:
            self.handle.close()
            self.handle = None


class RuleState:
    def __init__(self, rule):
        self.rule = rule
        self.satisfied_since = None
        self.last_value = None
        self.error = None

    def update(self, variables, now):
        try:
            satisfied = self.rule.evaluate(variables)
            self.error = None
        except Exception as err:
            self.error = f"{type(err).__name__}: {err}"
            satisfied = False

        self.last_value = satisfied

        if satisfied:
            if self.satisfied_since is None:
                self.satisfied_since = now
        else:
            self.satisfied_since = None

        return satisfied

    def held_for(self, now):
        if self.satisfied_since is None:
            return 0.0
        return now - self.satisfied_since

    def ripe(self, now):
        if not self.last_value:
            return False
        dwell = self.rule.dwell or 0
        return self.held_for(now) >= dwell


class Engine:
    def __init__(self, profile, dry_run=False, reporter=None):
        self.profile = profile
        self.dry_run = dry_run
        self.report = reporter or Reporter()

        from .anker import AnkerSource

        self.anker_profile = load_yaml(profile.source_path)
        self.source = AnkerSource(profile.source_path)
        self.target = ShellyTarget(profile.target_path, profile.target_channel)

        self.notifier = Notifier(
            profile.notifications, reporter=self.report, dry_run=dry_run
        )

        self.states = [RuleState(rule) for rule in profile.active_rules()]
        self.recent_actions = deque()
        self.last_action_at = 0.0
        self.last_commanded = None
        self.stale_reported = False
        self.floor_latched = False
        self.floor_since = None
        self.last_heartbeat = 0.0
        self.heartbeat_every = 300
        self.incomplete_reported = False
        self.disconnected_since = None
        self.stale_action_state = None
        self.pre_stale_state = None

    def evaluate(self, variables, now):
        for state in self.states:
            state.update(variables, now)

        ripe = [state for state in self.states if state.ripe(now)]
        if not ripe:
            return None, []

        ripe.sort(key=lambda s: (-s.rule.priority, self.states.index(s)))
        return ripe[0], ripe

    def rate_limited(self, now):
        while self.recent_actions and now - self.recent_actions[0] > 3600:
            self.recent_actions.popleft()

        if self.last_action_at and (now - self.last_action_at) < (self.profile.min_gap or 0):
            remaining = (self.profile.min_gap or 0) - (now - self.last_action_at)
            return f"min gap, {format_duration(remaining)} remaining"

        if len(self.recent_actions) >= self.profile.max_per_hour:
            return f"hourly cap of {self.profile.max_per_hour} actions reached"

        return None

    async def apply(
        self, session, desired, reason, now, rule=None, variables=None, force=False
    ):
        if self.last_commanded is desired:
            return False

        blocked = None if force else self.rate_limited(now)
        if blocked:
            self.report(f"suppressed {self._word(desired)} ({reason}): {blocked}")
            return False

        if self.dry_run:
            self.report(f"DRY RUN would turn {self._word(desired)} - {reason}")
            self.last_commanded = desired
            return True

        try:
            await self.target.set_state(session, desired)
        except Exception as err:
            self.report(f"FAILED to turn {self._word(desired)}: {type(err).__name__}: {err}")
            return False

        self.last_commanded = desired
        self.last_action_at = now
        self.recent_actions.append(now)
        self.report(f"turned {self._word(desired)} - {reason}")
        self.save_state(desired, reason)
        await self.notify(desired, rule, variables, event="action")
        return True

    @staticmethod
    def _word(desired):
        return "ON" if desired else "OFF"

    def context(self, desired, rule, variables, event="action"):
        source_identity = self.anker_profile.get("identity", {})
        target_identity = self.target.profile.get("identity", {})

        context = dict(variables or {})
        context.update(
            {
                "profile": self.profile.name,
                "event": event,
                "rule": rule.name if rule else "",
                "condition": rule.when_source if rule else "",
                "action": self._word(desired) if desired is not None else "",
                "action_word": ("on" if desired else "off") if desired is not None else "",
                "source_name": (
                    source_identity.get("name")
                    or source_identity.get("model")
                    or source_identity.get("serial")
                ),
                "source_model": source_identity.get("model", ""),
                "source_serial": source_identity.get("serial", ""),
                "target_name": (
                    target_identity.get("name")
                    or target_identity.get("model")
                    or self.target.host
                ),
                "target_model": target_identity.get("model", ""),
                "target_host": self.target.host,
                "target_channel": self.target.channel,
                "time": stamp(),
            }
        )
        return context

    async def notify(self, desired, rule, variables, event="action"):
        settings = self.profile.notifications
        if not settings.wants(event):
            return
        if event == "action" and not settings.rule_enabled(rule):
            return

        context = self.context(desired, rule, variables, event)
        body = render(settings.template_for(rule), context)
        title = render(settings.title, context)
        priority = (rule.notify_priority if rule else None) or settings.priority
        key = rule.name if rule else event

        await self.notifier.send(title, body, priority=priority, key=key)

    def required_fields(self):
        names = set(self.profile.referenced_names())
        floor = self.profile.battery_floor
        if floor is not None and floor.enabled:
            names.add(floor.field)
        return names

    def summarize(self, variables, now):
        names = sorted(self.profile.referenced_names())
        floor = self.profile.battery_floor
        if floor and floor.field not in names:
            names.insert(0, floor.field)

        parts_readings = [
            f"{name}={variables.get(name)}" for name in names if name in variables
        ]

        for name in self.profile.monitor_fields:
            if name in names:
                continue
            value = variables.get(name, None)
            parts_readings.append(f"{name}={'-' if value is None else value}")

        readings = " ".join(parts_readings)

        parts = []
        for state in self.states:
            if state.error:
                parts.append(f"{state.rule.name}=ERROR")
                continue
            if not state.last_value:
                continue
            dwell = state.rule.dwell or 0
            held = state.held_for(now)
            if state.ripe(now):
                parts.append(f"{state.rule.name}=READY")
            else:
                parts.append(
                    f"{state.rule.name}={format_duration(held)}/{format_duration(dwell)}"
                )

        floor = self.profile.battery_floor
        if self.floor_latched:
            status = f"FLOOR LATCHED until {floor.field} >= {floor.release:g}"
        elif floor is not None and self.floor_since is not None:
            held = now - self.floor_since
            status = (
                f"FLOOR ARMING {format_duration(held)}/"
                f"{format_duration(floor.dwell)}"
            )
        elif parts:
            status = "; ".join(parts)
        else:
            status = "no rule matches"

        target = "?" if self.last_commanded is None else self._word(self.last_commanded)
        return f"{readings} | target={target} | {status}"

    def heartbeat(self, variables, now, force=False):
        due = force or self.dry_run or (now - self.last_heartbeat) >= self.heartbeat_every
        if not due:
            return
        self.last_heartbeat = now
        self.report(self.summarize(variables, now))

    async def undo_stale_action(self, session, now):
        if self.stale_action_state is None:
            return

        previous = self.pre_stale_state
        applied = self.stale_action_state
        self.stale_action_state = None
        self.pre_stale_state = None

        if previous is None or previous == applied:
            return

        if self.last_commanded != applied:
            return

        self.report(
            f"undoing the precautionary {self._word(applied)}, restoring "
            f"{self._word(previous)} so the rules decide from here",
            force=True,
        )
        await self.apply(
            session,
            previous,
            "restored after telemetry recovered",
            now,
            force=True,
        )

    async def check_floor(self, session, variables, now):
        floor = self.profile.battery_floor
        if floor is None or not floor.enabled:
            return False

        value = variables.get(floor.field)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            if self.floor_latched:
                self.report(
                    f"battery floor: {floor.field} is unreadable, holding the latch",
                    force=True,
                )
                return True
            return False

        if self.floor_latched:
            if value >= floor.release:
                self.floor_latched = False
                self.floor_since = None
                self.report(
                    f"battery floor released, {floor.field} back to {value:g}",
                    force=True,
                )
                if floor.notify_release:
                    await self.notify_floor(variables, value, released=True)
                return False

            await self.apply(
                session,
                floor.desired_state(),
                f"battery floor holding, {floor.field}={value:g}",
                now,
                variables=variables,
                force=True,
            )
            return True

        if value > floor.threshold:
            self.floor_since = None
            return False

        if self.floor_since is None:
            self.floor_since = now

        if (now - self.floor_since) < (floor.dwell or 0):
            return True

        self.floor_latched = True
        self.report(
            f"BATTERY FLOOR TRIPPED: {floor.field}={value:g} at or below "
            f"{floor.threshold:g}",
            force=True,
        )

        await self.apply(
            session,
            floor.desired_state(),
            f"battery floor, {floor.field}={value:g}",
            now,
            variables=variables,
            force=True,
        )

        if floor.notify:
            await self.notify_floor(variables, value)

        return True

    async def notify_floor(self, variables, value, released=False):
        floor = self.profile.battery_floor
        if floor is None:
            return

        if not self.notifier.available():
            if not released:
                self.report(
                    "battery floor tripped but no notification channel is enabled",
                    force=True,
                )
            return

        settings = self.profile.notifications
        desired = None if released else floor.desired_state()
        context = self.context(desired, None, variables, "safety")
        context["value"] = f"{value:g}"
        context["field"] = floor.field
        context["threshold"] = f"{floor.threshold:g}"
        context["release"] = f"{floor.release:g}"
        context["reason"] = f"{floor.field} at {value:g}"

        template = floor.release_template if released else floor.notify_template
        body = render(template, context)
        title = render(settings.title or "{profile}", context)

        await self.notifier.send(
            title,
            body,
            priority="urgent" if not released else None,
            key="battery_floor_release" if released else "battery_floor",
            force=True,
        )

    def save_state(self, desired, reason):
        record = {
            "profile": self.profile.name,
            "updated": stamp(),
            "target_state": bool(desired),
            "reason": reason,
            "actions_last_hour": len(self.recent_actions),
        }
        try:
            paths.STATE_DIR.mkdir(parents=True, exist_ok=True)
            existing = {}
            if paths.RUNTIME_STATE.exists():
                existing = json.loads(paths.RUNTIME_STATE.read_text(encoding="utf-8"))
            existing[self.profile.name] = record
            paths.RUNTIME_STATE.write_text(
                json.dumps(existing, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

    async def tick(self, session):
        now = time.monotonic()
        status = self.source.read()
        age = self.source.age_seconds()

        if not status:
            self.report("no telemetry yet")
            return

        if not self.source.connected():
            if self.disconnected_since is None:
                self.disconnected_since = now
                self.report(
                    "MQTT session disconnected, waiting "
                    f"{format_duration(self.profile.stale_after)} to see if it "
                    "recovers before acting",
                    force=True,
                )

            waited = now - self.disconnected_since
            if waited < (self.profile.stale_after or 0):
                return

            if not self.stale_reported:
                self.report(
                    f"MQTT session still down after {format_duration(waited)}, "
                    f"applying on_stale={self.profile.on_stale}",
                    force=True,
                )
                self.stale_reported = True
                await self.notify(
                    None, None, {"reason": "mqtt disconnected"}, event="stale"
                )

            if self.profile.on_stale == "stop":
                raise RuntimeError("stopping: MQTT session disconnected")
            if self.profile.on_stale == "safe_state":
                if self.stale_action_state is None:
                    self.pre_stale_state = self.last_commanded
                    self.stale_action_state = self.profile.safe_state
                await self.apply(
                    session, self.profile.safe_state, "mqtt disconnected safe state", now
                )
            return

        if self.disconnected_since is not None:
            self.report(
                "MQTT session recovered after "
                f"{format_duration(now - self.disconnected_since)}",
                force=True,
            )
            self.disconnected_since = None
            await self.undo_stale_action(session, now)

        if age is not None and self.profile.stale_after and age > self.profile.stale_after:
            if not self.stale_reported:
                self.report(
                    f"telemetry stale ({format_duration(age)} old), "
                    f"policy={self.profile.on_stale}",
                    force=True,
                )
                self.stale_reported = True

                await self.notify(None, None, {"reason": "telemetry stale"}, event="stale")

            if self.profile.on_stale == "stop":
                raise RuntimeError("stopping: telemetry went stale")
            if self.profile.on_stale == "safe_state":
                if self.stale_action_state is None:
                    self.pre_stale_state = self.last_commanded
                    self.stale_action_state = self.profile.safe_state
                await self.apply(
                    session, self.profile.safe_state, "stale telemetry safe state", now
                )
            return

        if self.stale_reported:
            self.report("telemetry recovered", force=True)
            self.stale_reported = False
            await self.undo_stale_action(session, now)

        variables = derived_values(self.anker_profile, status)

        missing = sorted(
            name
            for name in self.required_fields()
            if name not in variables or variables[name] is None
        )
        if missing:
            if not self.incomplete_reported:
                self.report(
                    f"waiting for telemetry field(s) {missing}. Not evaluating any "
                    "rule or the safety floor until they arrive.",
                    force=True,
                )
                self.incomplete_reported = True
            return

        if self.incomplete_reported:
            self.report("telemetry complete, resuming evaluation", force=True)
            self.incomplete_reported = False

        if await self.check_floor(session, variables, now):
            self.heartbeat(variables, now)
            return

        winner, ripe = self.evaluate(variables, now)

        for state in self.states:
            if state.error:
                self.report(f"rule {state.rule.name!r} error: {state.error}")

        self.heartbeat(variables, now)

        if winner is None:
            return

        desired = winner.rule.desired_state()
        if desired is None:
            return

        reason = f"{winner.rule.name} [{winner.rule.when_source}]"
        if len(ripe) > 1:
            reason += f" (priority over {len(ripe) - 1} other)"

        await self.apply(session, desired, reason, now, rule=winner.rule, variables=variables)

    async def run(self, cycles=None):
        await self.source.start(required=self.required_fields())
        self.report(f"source: {self.source.label} {self.source.serial}", force=True)
        if self.profile.battery_floor:
            self.report(
                f"battery floor: {self.profile.battery_floor.describe()}", force=True
            )
        else:
            self.report("battery floor: NONE SET", force=True)

        if self.profile.monitor_fields:
            self.report(
                "also logging (not used by any rule): "
                + ", ".join(self.profile.monitor_fields),
                force=True,
            )
        self.report(f"target: {self.target.label} channel {self.target.channel}", force=True)

        async with aiohttp.ClientSession() as session:
            current = await self.target.get_state(session)
            if current is None:
                self.report("warning: could not read the Shelly current state", force=True)
            else:
                self.last_commanded = bool(current)
                self.report(f"target currently {self._word(current)}", force=True)

            try:
                conflicts = await self.target.conflicts(session)
            except Exception:
                conflicts = []

            if conflicts:
                self.report("=" * 60, force=True)
                self.report(
                    f"{len(conflicts)} CONFLICTING AUTOMATION(S) ON THE SHELLY ITSELF",
                    force=True,
                )
                for item in conflicts:
                    self.report(f"  {item}", force=True)
                self.report(
                    "These run on the device and will fight these rules. "
                    "Remove them in the Shelly app before relying on this.",
                    force=True,
                )
                self.report("=" * 60, force=True)

            if self.dry_run:
                self.report(
                    "DRY RUN: evaluating normally, but no switch command will be "
                    "sent and no notification will fire",
                    force=True,
                )

            count = 0
            try:
                while cycles is None or count < cycles:
                    await self.tick(session)
                    count += 1
                    if cycles is not None and count >= cycles:
                        self.report(
                            f"completed {count} cycle(s), stopping as requested",
                            force=True,
                        )
                        break
                    await interruptible_sleep(self.profile.poll_interval)
            finally:
                await self.source.stop()
                await asyncio.sleep(0.25)

    async def close(self):
        await self.source.stop()


async def dry_run_report(profile, cycles=3, offline=False, overrides=None):
    problems, notes = validate(profile)

    print()
    print(f"Power profile: {profile.name}")
    print(f"  file    {paths.relative(profile.path)}")
    print(f"  source  {profile.source_reference}")
    print(f"  target  {profile.target_reference}")
    print(f"  enabled {profile.enabled}")
    print()

    for note in notes:
        print(f"  note:    {note}")
    for problem in problems:
        print(f"  PROBLEM: {problem}")

    if problems:
        print()
        print(f"{len(problems)} problem(s) found. Fix these before running.")
        return False

    print(f"  syntax OK, {len(profile.active_rules())} active rule(s)")

    settings = profile.notifications
    if settings.enabled:
        from .notify import Notifier

        probe = Notifier(settings)
        channels = probe.available()
        missing = probe.missing()
        print(
            f"  notifications on via {', '.join(channels) if channels else 'NO CHANNEL'}"
            f", throttle {format_duration(settings.throttle)}"
        )
        if missing:
            print(f"  requested but not enabled: {', '.join(missing)}")
        if not channels:
            print("  nothing will be delivered until a channel is enabled")
    else:
        print("  notifications off")

    if profile.battery_floor:
        floor = profile.battery_floor
        print(f"  safety floor: {floor.describe()}, dwell {format_duration(floor.dwell)}")
    else:
        print("  safety floor: NONE SET")

    if overrides:
        print()
        print("Simulated overrides:")
        for key, value in sorted(overrides.items()):
            print(f"    {key} = {value!r}")

    if offline:
        anker_profile = load_yaml(profile.source_path)
        samples = {
            key: spec.get("sample")
            for key, spec in (anker_profile.get("readable") or {}).items()
        }
        variables = derived_values(anker_profile, samples)
        if overrides:
            variables.update(overrides)
        print()
        print("Offline evaluation against the sample values in the device profile:")
        referenced = sorted(profile.referenced_names())
        overridden = overrides or {}
        if referenced:
            readings = ", ".join(
                f"{name}={variables.get(name)!r}"
                + (" (simulated)" if name in overridden else "")
                for name in referenced
            )
            print(f"    values: {readings}")

        floor_wins = _print_floor_verdict(profile, variables)

        print()
        if floor_wins:
            print("    Rules below are shown for reference, but the floor would")
            print("    take precedence while it is latched:")
        _print_rule_table(profile, variables, overrides=overrides, skip_values=True)
        print()
        print("Sample values are a snapshot from discovery, not live data.")
        return True

    print()
    print(f"Connecting for a live dry run ({cycles} cycle(s), no commands sent)...")

    engine = Engine(profile, dry_run=True)
    await engine.source.start(required=engine.required_fields())

    try:
        async with aiohttp.ClientSession() as session:
            reachable = await engine.target.reachable(session)
            current = await engine.target.get_state(session)
            print()
            print(f"  Shelly reachable: {reachable}")
            if current is not None:
                print(f"  Shelly currently: {'ON' if current else 'OFF'}")
            if not reachable:
                print(
                    "  the target did not respond; check access.host in its profile"
                )

            for index in range(cycles):
                if index:
                    await interruptible_sleep(profile.poll_interval)

                status = engine.source.read()
                if not status:
                    print()
                    print(f"  cycle {index + 1}: no telemetry decoded yet")
                    continue

                variables = derived_values(engine.anker_profile, status)
                if overrides:
                    variables.update(overrides)

                absent = sorted(
                    name
                    for name in engine.required_fields()
                    if name not in variables or variables[name] is None
                )
                if absent:
                    print()
                    print(f"  cycle {index + 1}: waiting for field(s) {absent}")
                    print("    nothing is evaluated until they arrive")
                    continue

                now = time.monotonic()
                for state in engine.states:
                    state.update(variables, now)

                print()
                print(f"  cycle {index + 1}  (telemetry age "
                      f"{format_duration(engine.source.age_seconds())})")
                floor_wins = _print_floor_verdict(profile, variables)
                print()
                if floor_wins:
                    print("    Rules below are shown for reference, but the floor")
                    print("    would take precedence while it is latched:")
                _print_rule_table(
                    profile, variables, engine, now, overrides=overrides
                )
    finally:
        await engine.source.stop()

    print()
    print("Dry run complete. No commands were sent.")
    return True


def _preview_notification(profile, rule, variables, desired):
    from .notify import render

    settings = profile.notifications
    if not settings.wants("action") or not settings.rule_enabled(rule):
        return None

    source_identity = load_yaml(profile.source_path).get("identity", {})
    target_profile = load_yaml(profile.target_path)
    target_identity = target_profile.get("identity", {})

    context = dict(variables)
    context.update(
        {
            "profile": profile.name,
            "rule": rule.name,
            "condition": rule.when_source,
            "action": "ON" if desired else "OFF",
            "action_word": "on" if desired else "off",
            "source_name": (
                source_identity.get("name")
                or source_identity.get("model")
                or source_identity.get("serial")
            ),
            "source_model": source_identity.get("model", ""),
            "source_serial": source_identity.get("serial", ""),
            "target_name": (
                target_identity.get("name")
                or target_identity.get("model")
                or target_profile.get("access", {}).get("host")
            ),
            "target_model": target_identity.get("model", ""),
            "target_host": target_profile.get("access", {}).get("host", ""),
            "target_channel": profile.target_channel or 0,
            "time": stamp(),
            "event": "action",
        }
    )
    return render(settings.template_for(rule), context)


def _print_floor_verdict(profile, variables):
    floor = profile.battery_floor
    if floor is None or not floor.enabled:
        return False

    value = variables.get(floor.field)

    print()
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        print(f"    SAFETY FLOOR: {floor.field} is not readable, cannot evaluate")
        return False

    if value <= floor.threshold:
        print(
            f"    SAFETY FLOOR TRIPS: {floor.field}={value:g} is at or below "
            f"{floor.threshold:g}"
        )
        print(
            f"      after {format_duration(floor.dwell)} it would {floor.action} "
            f"and LATCH until {floor.field} reaches {floor.release:g}"
        )
        print("      it bypasses the rate limits and outranks every rule below")
        print("      while latched, no rule can turn the target off")
        return True

    print(
        f"    safety floor idle: {floor.field}={value:g} is above "
        f"{floor.threshold:g}"
    )
    return False


def _print_rule_table(
    profile, variables, engine=None, now=None, overrides=None, skip_values=False
):
    referenced = sorted(profile.referenced_names())
    overrides = overrides or {}
    if referenced and not skip_values:
        readings = ", ".join(
            f"{name}={variables.get(name)!r}"
            + (" (simulated)" if name in overrides else "")
            for name in referenced
        )
        print(f"    values: {readings}")

    states = engine.states if engine else None

    for index, rule in enumerate(profile.active_rules()):
        if states:
            state = states[index]
            satisfied = state.last_value
            held = state.held_for(now) if now else 0
            ripe = state.ripe(now) if now else False
            if state.error:
                verdict = f"ERROR {state.error}"
            elif not satisfied:
                verdict = "false"
            elif ripe:
                verdict = f"TRUE and ripe -> would {rule.action}"
            else:
                remaining = (rule.dwell or 0) - held
                verdict = f"true, waiting {format_duration(remaining)} of dwell"
        else:
            try:
                satisfied = rule.evaluate(variables)
                verdict = (
                    f"true -> would {rule.action} after {format_duration(rule.dwell)}"
                    if satisfied
                    else "false"
                )
            except Exception as err:
                verdict = f"ERROR {type(err).__name__}: {err}"

        print(f"    [{rule.priority:>3}] {rule.name}")
        print(f"          when {rule.when_source}")
        print(f"          {verdict}")

        desired = rule.desired_state()
        if desired is not None:
            if engine:
                message = _render_live_notification(engine, rule, variables, desired)
            else:
                message = _preview_notification(profile, rule, variables, desired)
            if message:
                print(f"          notify: {message}")


def _render_live_notification(engine, rule, variables, desired):
    from .notify import render

    settings = engine.profile.notifications
    if not settings.wants("action") or not settings.rule_enabled(rule):
        return None
    context = engine.context(desired, rule, variables, "action")
    return render(settings.template_for(rule), context)
