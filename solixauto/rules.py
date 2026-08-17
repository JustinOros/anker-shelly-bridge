import ast
import difflib
import re
from pathlib import Path

from . import paths
from .profiles import load_yaml

ALLOWED_NODES = (
    ast.Expression,
    ast.BoolOp,
    ast.And,
    ast.Or,
    ast.UnaryOp,
    ast.Not,
    ast.USub,
    ast.UAdd,
    ast.BinOp,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Compare,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.Eq,
    ast.NotEq,
    ast.In,
    ast.NotIn,
    ast.Name,
    ast.Load,
    ast.Constant,
    ast.List,
    ast.Tuple,
)

ACTIONS = {"target.on", "target.off", "none"}

DURATION_PATTERN = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smh]?)\s*$", re.IGNORECASE)

DEFAULT_NOTIFY_TEMPLATE = (
    "{clock} {source_name} battery {battery_soc}%, solar {pv_total}W. "
    "{target_name} turned {action}."
)
DEFAULT_NOTIFY_THROTTLE = 300
NOTIFY_EVENTS = {"action", "stale", "error", "start", "safety"}

DEFAULT_DWELL = 60
DEFAULT_STALE_AFTER = 120
DEFAULT_MIN_GAP = 60
DEFAULT_MAX_PER_HOUR = 20
STALE_POLICIES = {"hold", "safe_state", "stop"}


class ProfileError(Exception):
    pass


def parse_duration(value, field="duration"):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = DURATION_PATTERN.match(str(value))
    if not match:
        raise ProfileError(
            f"{field}: cannot parse {value!r}. Use forms like 30, 90s, 5m, 1h."
        )
    amount = float(match.group(1))
    unit = (match.group(2) or "s").lower()
    return amount * {"s": 1, "m": 60, "h": 3600}[unit]


def format_duration(seconds):
    if seconds is None:
        return "-"

    seconds = round(float(seconds))

    if seconds >= 3600 and seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds >= 60 and seconds % 60 == 0:
        return f"{seconds // 60}m"
    if seconds >= 60:
        minutes, rest = divmod(seconds, 60)
        return f"{minutes}m{rest}s"
    return f"{seconds}s"


def compile_expression(source, field="when"):
    try:
        tree = ast.parse(str(source), mode="eval")
    except SyntaxError as err:
        raise ProfileError(f"{field}: syntax error in {source!r}: {err.msg}") from err

    for node in ast.walk(tree):
        if not isinstance(node, ALLOWED_NODES):
            raise ProfileError(
                f"{field}: {type(node).__name__} is not allowed in {source!r}. "
                "Expressions may only use field names, numbers, comparisons, "
                "and/or/not, and basic arithmetic."
            )

    return compile(tree, "<power-profile>", "eval")


def expression_names(source):
    tree = ast.parse(str(source), mode="eval")
    return {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
    }


def evaluate(code, variables):
    return bool(eval(code, {"__builtins__": {}}, dict(variables)))


class BatteryFloor:
    def __init__(self, raw):
        if not isinstance(raw, dict):
            raise ProfileError("safety.battery_floor must be a mapping")

        self.enabled = bool(raw.get("enabled", True))
        self.field = str(raw.get("field") or "battery_soc")

        if "at_or_below" not in raw:
            raise ProfileError("safety.battery_floor.at_or_below is required")

        try:
            self.threshold = float(raw["at_or_below"])
        except (TypeError, ValueError):
            raise ProfileError(
                "safety.battery_floor.at_or_below must be a number"
            ) from None

        release = raw.get("release_at", self.threshold + 15)
        try:
            self.release = float(release)
        except (TypeError, ValueError):
            raise ProfileError("safety.battery_floor.release_at must be a number") from None

        if self.release <= self.threshold:
            raise ProfileError(
                f"safety.battery_floor.release_at ({self.release}) must be greater "
                f"than at_or_below ({self.threshold}). Without a gap the relay will "
                "chatter at the floor."
            )

        action = str(raw.get("then") or "target.on").strip().lower()
        if action not in ("target.on", "target.off"):
            raise ProfileError(
                "safety.battery_floor.then must be target.on or target.off"
            )
        self.action = action

        self.dwell = parse_duration(raw.get("for", 30), "safety.battery_floor.for")
        self.notify = bool(raw.get("notify", True))
        self.notify_release = bool(raw.get("notify_release", True))
        self.notify_template = raw.get("notify_template") or (
            "SAFETY: {source_name} battery at {value}. {target_name} turned "
            "{action} to protect it. Holding until {release}."
        )
        self.release_template = raw.get("release_template") or (
            "{source_name} battery recovered to {value}. Normal rules resumed "
            "for {target_name}."
        )

    def desired_state(self):
        return self.action == "target.on"

    def describe(self):
        return (
            f"{self.field} <= {self.threshold:g} -> {self.action}, "
            f"releases at {self.release:g}"
        )


class NotificationSettings:
    def __init__(self, raw):
        if not isinstance(raw, dict):
            raise ProfileError("notifications must be a mapping")

        self.enabled = bool(raw.get("enabled", False))

        channels = raw.get("channels")
        if isinstance(channels, str):
            channels = [channels]
        self.channels = list(channels or [])

        self.template = str(raw.get("template") or DEFAULT_NOTIFY_TEMPLATE)
        self.stale_template = str(
            raw.get("stale_template")
            or (
                "{clock} {source_name}: telemetry lost ({reason}). Last seen "
                "battery {battery_soc}% at {last_seen}. {target_name} was "
                "turned {action} as a precaution."
            )
        )
        self.recovered_template = str(
            raw.get("recovered_template")
            or "{clock} {source_name}: telemetry is back after {outage}. Rules resumed."
        )
        self.title = str(raw.get("title") or "{profile}")
        self.throttle = parse_duration(
            raw.get("throttle", DEFAULT_NOTIFY_THROTTLE), "notifications.throttle"
        )
        self.priority = raw.get("priority")

        events = raw.get("on")
        if isinstance(events, str):
            events = [events]
        self.events = set(events or ["action"])
        unknown = self.events - NOTIFY_EVENTS
        if unknown:
            raise ProfileError(
                f"notifications.on has unknown event(s) {sorted(unknown)}. "
                f"Known: {sorted(NOTIFY_EVENTS)}"
            )

    def wants(self, event):
        return self.enabled and event in self.events

    def template_for(self, rule):
        if rule is not None and rule.notify_template:
            return rule.notify_template
        return self.template

    def rule_enabled(self, rule):
        if not self.enabled:
            return False
        if rule is None:
            return True
        if rule.notify is None:
            return True
        return rule.notify


class Rule:
    def __init__(self, raw, index):
        if not isinstance(raw, dict):
            raise ProfileError(f"rules[{index}]: each rule must be a mapping")

        self.name = str(raw.get("name") or f"rule {index + 1}")
        label = f"rules[{index}] ({self.name})"

        if "when" not in raw:
            raise ProfileError(f"{label}: missing 'when'")
        self.when_source = str(raw["when"])
        self.code = compile_expression(self.when_source, f"{label}.when")
        self.names = expression_names(self.when_source)

        action = str(raw.get("then") or "").strip().lower()
        if action not in ACTIONS:
            raise ProfileError(
                f"{label}: 'then' must be one of {sorted(ACTIONS)}, got {action!r}"
            )
        self.action = action

        self.dwell = parse_duration(
            raw.get("for", DEFAULT_DWELL), f"{label}.for"
        )
        self.priority = int(raw.get("priority", 0))
        self.enabled = bool(raw.get("enabled", True))

        notify = raw.get("notify", None)
        self.notify_template = None
        self.notify_priority = None
        self.notify_channels = None

        if notify is None:
            self.notify = None
        elif isinstance(notify, bool):
            self.notify = notify
        elif isinstance(notify, str):
            text = notify.strip().lower()
            if text in ("on", "true", "yes"):
                self.notify = True
            elif text in ("off", "false", "no"):
                self.notify = False
            else:
                raise ProfileError(
                    f"{label}: notify must be on/off or a mapping, got {notify!r}"
                )
        elif isinstance(notify, dict):
            self.notify = bool(notify.get("enabled", True))
            self.notify_template = notify.get("template")
            self.notify_priority = notify.get("priority")
            channels = notify.get("channels")
            if isinstance(channels, str):
                channels = [channels]
            self.notify_channels = channels
        else:
            raise ProfileError(f"{label}: notify must be on/off or a mapping")

    def desired_state(self):
        if self.action == "target.on":
            return True
        if self.action == "target.off":
            return False
        return None

    def evaluate(self, variables):
        return evaluate(self.code, variables)


class PowerProfile:
    def __init__(self, path):
        self.path = Path(path)
        raw = load_yaml(self.path)

        self.name = str(raw.get("name") or self.path.stem)
        self.enabled = bool(raw.get("enabled", True))
        self.description = str(raw.get("description") or "")

        source = raw.get("source")
        if not isinstance(source, dict) or not source.get("profile"):
            raise ProfileError("source.profile is required")
        self.source_reference = str(source["profile"])
        self.stale_after = parse_duration(
            source.get("stale_after", DEFAULT_STALE_AFTER), "source.stale_after"
        )
        self.on_stale = str(source.get("on_stale", "hold")).strip().lower()
        if self.on_stale not in STALE_POLICIES:
            raise ProfileError(
                f"source.on_stale must be one of {sorted(STALE_POLICIES)}, "
                f"got {self.on_stale!r}"
            )

        target = raw.get("target")
        if not isinstance(target, dict) or not target.get("profile"):
            raise ProfileError("target.profile is required")
        self.target_reference = str(target["profile"])
        self.target_channel = target.get("channel")

        safe_state = raw.get("safe_state", "off")
        if isinstance(safe_state, bool):
            self.safe_state = safe_state
        else:
            text = str(safe_state).strip().lower()
            if text not in ("on", "off"):
                raise ProfileError("safe_state must be 'on' or 'off'")
            self.safe_state = text == "on"

        raw_rules = raw.get("rules")
        if not isinstance(raw_rules, list) or not raw_rules:
            raise ProfileError("at least one rule is required")
        self.rules = [Rule(item, index) for index, item in enumerate(raw_rules)]

        monitor = raw.get("monitor_fields") or []
        if isinstance(monitor, str):
            monitor = [monitor]
        if not isinstance(monitor, list):
            raise ProfileError("monitor_fields must be a list of field names")
        self.monitor_fields = [str(entry).strip() for entry in monitor if str(entry).strip()]

        self.notifications = NotificationSettings(raw.get("notifications") or {})

        safety = raw.get("safety") or {}
        if not isinstance(safety, dict):
            raise ProfileError("safety must be a mapping")
        floor = safety.get("battery_floor")
        self.battery_floor = BatteryFloor(floor) if floor else None

        limits = raw.get("limits") or {}
        self.min_gap = parse_duration(
            limits.get("min_seconds_between_actions", DEFAULT_MIN_GAP),
            "limits.min_seconds_between_actions",
        )
        self.max_per_hour = int(limits.get("max_actions_per_hour", DEFAULT_MAX_PER_HOUR))
        if self.max_per_hour < 1:
            raise ProfileError("limits.max_actions_per_hour must be at least 1")

        self.poll_interval = parse_duration(raw.get("poll_interval", 10), "poll_interval")

        self.source_path = paths.resolve_profile(self.source_reference, "anker")
        self.target_path = paths.resolve_profile(self.target_reference, "shelly")

    def active_rules(self):
        return [rule for rule in self.rules if rule.enabled]

    def referenced_names(self):
        names = set()
        for rule in self.active_rules():
            names |= rule.names
        return names


def known_fields(anker_profile):
    readable = set((anker_profile.get("readable") or {}).keys())
    derived = set((anker_profile.get("derived") or {}).keys())
    return readable, derived


def derived_values(anker_profile, status):
    values = dict(status)
    for name, spec in (anker_profile.get("derived") or {}).items():
        expression = spec.get("expression") if isinstance(spec, dict) else spec
        if not expression:
            continue
        try:
            code = compile_expression(expression, f"derived.{name}")
            values[name] = eval(code, {"__builtins__": {}}, dict(values))
        except Exception:
            values[name] = None
    return values


def validate(profile):
    problems = []
    notes = []

    if profile.source_path is None:
        problems.append(
            f"source.profile {profile.source_reference!r} does not resolve to a file "
            f"under {paths.relative(paths.ANKER_PROFILE_DIR)}"
        )
    if profile.target_path is None:
        problems.append(
            f"target.profile {profile.target_reference!r} does not resolve to a file "
            f"under {paths.relative(paths.SHELLY_PROFILE_DIR)}"
        )

    if problems:
        return problems, notes

    anker_profile = load_yaml(profile.source_path)
    shelly_profile = load_yaml(profile.target_path)

    if anker_profile.get("kind") != "anker":
        problems.append(f"{profile.source_path} is not an Anker device profile")
    if shelly_profile.get("kind") != "shelly":
        problems.append(f"{profile.target_path} is not a Shelly device profile")

    readable, derived = known_fields(anker_profile)
    available = readable | derived

    for rule in profile.active_rules():
        unknown = sorted(rule.names - available)
        for name in unknown:
            close = sorted(
                candidate
                for candidate in available
                if name.lower() in candidate.lower()
                or candidate.lower() in name.lower()
            )[:3]
            if not close:
                close = difflib.get_close_matches(name, sorted(available), n=3, cutoff=0.5)
            hint = f" Did you mean: {', '.join(close)}?" if close else ""
            problems.append(f"rule {rule.name!r}: unknown field {name!r}.{hint}")

    channels = shelly_profile.get("channels") or {}
    if profile.target_channel is not None:
        if str(profile.target_channel) not in channels:
            problems.append(
                f"target.channel {profile.target_channel!r} not present on device. "
                f"Available: {sorted(channels)}"
            )
    elif len(channels) > 1:
        notes.append(
            f"target.channel not set and device has {len(channels)} channels; "
            f"channel {sorted(channels)[0]} will be used"
        )

    device_automation = shelly_profile.get("device_automation") or {}
    if device_automation.get("checked"):
        from .shelly import automation_warnings

        for warning in automation_warnings(device_automation, profile.target_channel):
            notes.append(f"target device has its own automation: {warning}")

    for name in profile.monitor_fields:
        if name not in available:
            notes.append(
                f"monitor_fields lists {name!r}, which this device does not report. "
                "It will show as '-' in the log."
            )

    if profile.battery_floor is None:
        notes.append(
            "no safety.battery_floor is set. If this target controls charging for "
            "the source device, add one so the battery cannot be stranded at 0%"
        )
    elif profile.battery_floor.field not in available:
        problems.append(
            f"safety.battery_floor.field {profile.battery_floor.field!r} is not a "
            "field on this device"
        )

    on_rules = [r for r in profile.active_rules() if r.desired_state() is True]
    off_rules = [r for r in profile.active_rules() if r.desired_state() is False]
    if not on_rules:
        notes.append("no rule ever turns the target ON")
    if not off_rules:
        notes.append("no rule ever turns the target OFF")

    for rule in profile.active_rules():
        if rule.dwell is not None and rule.dwell < 15:
            notes.append(
                f"rule {rule.name!r}: dwell of {format_duration(rule.dwell)} is short "
                "and may cause the relay to chatter"
            )

    _check_hysteresis(profile, notes)

    if profile.min_gap and profile.poll_interval and profile.min_gap < profile.poll_interval:
        notes.append(
            "limits.min_seconds_between_actions is shorter than poll_interval"
        )

    if profile.notifications.enabled:
        from . import notify as notify_module

        configured = set(notify_module.enabled_channels())
        wanted = set(profile.notifications.channels)
        if not configured:
            notes.append(
                "notifications are enabled but no channel is turned on in "
                "notifications.yaml"
            )
        elif wanted and not (wanted & configured):
            notes.append(
                f"notifications request channel(s) {sorted(wanted)} but only "
                f"{sorted(configured)} are enabled in notifications.yaml"
            )

        templates_to_check = [profile.notifications.template, profile.notifications.title]
        for rule in profile.active_rules():
            if rule.notify_template:
                templates_to_check.append(rule.notify_template)

        context_names = {
            "profile", "rule", "action", "action_word", "source_name", "source_model",
            "source_serial", "target_name", "target_model", "target_host",
            "target_channel", "condition", "time", "clock", "reason", "event",
        }
        for template in templates_to_check:
            for field in notify_module.template_fields(template):
                if field in context_names or field in available:
                    continue
                problems.append(
                    f"notification template references unknown field {field!r}"
                )

    if profile.auth_warning(shelly_profile):
        notes.append(
            "the Shelly device reports authentication enabled but no credentials "
            "are set in its profile; control calls will fail"
        )

    return problems, notes


def _check_hysteresis(profile, notes):
    thresholds = {}
    for rule in profile.active_rules():
        state = rule.desired_state()
        if state is None:
            continue
        try:
            tree = ast.parse(rule.when_source, mode="eval")
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            if not isinstance(node.left, ast.Name):
                continue
            if len(node.comparators) != 1:
                continue
            comparator = node.comparators[0]
            if not isinstance(comparator, ast.Constant):
                continue
            if not isinstance(comparator.value, (int, float)):
                continue
            thresholds.setdefault(node.left.id, []).append(
                (state, comparator.value, rule.name)
            )

    for field, entries in thresholds.items():
        values = {}
        for state, value, rule_name in entries:
            values.setdefault(value, set()).add(state)
        for value, states in values.items():
            if len(states) > 1:
                notes.append(
                    f"field {field!r} uses the same threshold {value} for both ON and "
                    "OFF; separate them to create a deadband"
                )


def _auth_warning(self, shelly_profile):
    auth = shelly_profile.get("auth") or {}
    if not auth.get("required"):
        return False
    return not (auth.get("username") and auth.get("password"))


PowerProfile.auth_warning = _auth_warning
