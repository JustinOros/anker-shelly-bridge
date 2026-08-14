import json
import statistics
import time
from pathlib import Path

from . import paths
from .profiles import load_yaml
from .rules import PowerProfile, ProfileError

MIN_HOURS_REQUIRED = 24
MIN_SAMPLES_REQUIRED = 200

TOP_UP_PERCENTILE = 15
STOP_PERCENTILE = 85
SURPLUS_RELEASE_PERCENTILE = 60
SURPLUS_FALLBACK_PERCENTILE = 20
MIN_CHARGING_SURPLUS = 150

FLOOR_MARGIN = 10
MIN_BAND_WIDTH = 15

PLATEAU_FRACTION = 0.15
PLATEAU_BAND = 2


class TuneError(Exception):
    pass


def telemetry_path(serial):
    return paths.TELEMETRY_DIR / f"{serial}.jsonl"


def load_telemetry(serial, since_epoch=None):
    path = telemetry_path(serial)
    if not path.exists():
        return []

    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except Exception:
            continue
        t = record.get("t")
        if not isinstance(t, (int, float)):
            continue
        if since_epoch is not None and t < since_epoch:
            continue
        values = record.get("v")
        if isinstance(values, dict):
            records.append((t, values))

    records.sort(key=lambda pair: pair[0])
    return records


def series(records, field):
    return [
        values[field]
        for _, values in records
        if field in values and isinstance(values[field], (int, float))
    ]


def percentile(values, pct):
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (pct / 100) * (len(ordered) - 1)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    frac = rank - low
    return ordered[low] + (ordered[high] - ordered[low]) * frac


def round_to(value, step):
    return round(value / step) * step


def detect_ceiling_plateau(values, band=PLATEAU_BAND, min_fraction=PLATEAU_FRACTION):
    if not values:
        return None, values

    ceiling = max(values)
    at_ceiling = [v for v in values if v >= ceiling - band]
    fraction = len(at_ceiling) / len(values)

    if fraction < min_fraction:
        return None, values

    below = [v for v in values if v < ceiling - band]
    if len(below) < MIN_SAMPLES_REQUIRED // 4:
        return None, values

    return {"ceiling": ceiling, "fraction": fraction}, below


def detect_floor_plateau(values, band=PLATEAU_BAND, min_fraction=PLATEAU_FRACTION):
    if not values:
        return None, values

    floor_value = min(values)
    at_floor = [v for v in values if v <= floor_value + band]
    fraction = len(at_floor) / len(values)

    if fraction < min_fraction:
        return None, values

    above = [v for v in values if v > floor_value + band]
    if len(above) < MIN_SAMPLES_REQUIRED // 4:
        return None, values

    return {"floor": floor_value, "fraction": fraction}, above


def describe_span(seconds):
    hours = seconds / 3600
    if hours >= 48:
        return f"{hours / 24:.1f} days"
    if hours >= 1:
        return f"{hours:.1f} hours"
    return f"{seconds / 60:.0f} minutes"


class Analysis:
    def __init__(self, profile_path, since_hours=None):
        try:
            self.profile = PowerProfile(profile_path)
        except ProfileError as err:
            raise TuneError(f"{profile_path}: {err}") from None

        if self.profile.source_path is None:
            raise TuneError(
                f"source.profile {self.profile.source_reference!r} does not "
                "resolve to a saved Anker device profile"
            )

        anker = load_yaml(self.profile.source_path)
        self.serial = (anker.get("identity") or {}).get("serial")
        if not self.serial:
            raise TuneError(
                f"{self.profile.source_path} has no identity.serial"
            )

        self.since_hours = since_hours
        since_epoch = None
        if since_hours is not None:
            since_epoch = time.time() - (since_hours * 3600)

        self.records = load_telemetry(self.serial, since_epoch)

        if len(self.records) < MIN_SAMPLES_REQUIRED:
            raise TuneError(
                f"only {len(self.records)} telemetry sample(s) recorded for "
                f"this device. Need at least {MIN_SAMPLES_REQUIRED} to propose "
                "anything sensible. Let the automation run longer, then try "
                "again."
            )

        span_seconds = self.records[-1][0] - self.records[0][0]
        span_hours = span_seconds / 3600

        if span_hours < MIN_HOURS_REQUIRED:
            raise TuneError(
                f"recorded telemetry only spans {describe_span(span_seconds)}. "
                f"Need at least {MIN_HOURS_REQUIRED}h of history to propose "
                "rules that reflect real usage, not a snapshot. Let the "
                "automation run longer, then try again."
            )

        self.span_hours = span_hours
        self.span_seconds = span_seconds

        self.battery = series(self.records, "battery_soc")
        self.surplus = series(self.records, "pv_surplus")
        self.pv_total = series(self.records, "pv_total")
        self.load = series(self.records, "output_power_total")

    def daylight_surplus(self):
        return [
            values.get("pv_surplus")
            for _, values in self.records
            if values.get("pv_total", 0) and values.get("pv_total", 0) > 0
            and isinstance(values.get("pv_surplus"), (int, float))
        ]

    def floor_bounds(self):
        floor = self.profile.battery_floor
        if floor is None:
            return None, None
        return floor.threshold, floor.release

    def propose(self):
        if not self.battery:
            raise TuneError(
                "no battery_soc readings found in the recorded telemetry"
            )

        floor_at, floor_release = self.floor_bounds()
        floor_at = floor_at if floor_at is not None else 0
        floor_release = floor_release if floor_release is not None else floor_at

        low_bound = max(floor_release, floor_at + FLOOR_MARGIN)

        ceiling_info, battery_for_high = detect_ceiling_plateau(self.battery)
        floor_info, battery_for_low = detect_floor_plateau(self.battery)

        top_up = percentile(battery_for_low, TOP_UP_PERCENTILE)
        stop_at = percentile(battery_for_high, STOP_PERCENTILE)

        top_up = round_to(top_up, 5)
        stop_at = round_to(stop_at, 5)

        top_up = max(top_up, low_bound)
        if stop_at - top_up < MIN_BAND_WIDTH:
            stop_at = top_up + MIN_BAND_WIDTH
        stop_at = min(stop_at, 95)
        if stop_at <= top_up:
            top_up = max(low_bound, stop_at - MIN_BAND_WIDTH)

        battery_low = percentile(battery_for_low, 10)
        battery_high = percentile(battery_for_high, 90)

        proposal = {
            "top_up_at": top_up,
            "stop_at": stop_at,
            "observed_low": round(battery_low, 1) if battery_low is not None else None,
            "observed_high": round(battery_high, 1) if battery_high is not None else None,
            "sample_count": len(self.records),
            "span_hours": round(self.span_hours, 1),
            "solar": None,
            "ceiling_plateau": ceiling_info,
            "floor_plateau": floor_info,
        }

        positive_surplus = [v for v in self.daylight_surplus() if v > 0]
        if len(positive_surplus) >= 30:
            release_surplus = percentile(positive_surplus, SURPLUS_RELEASE_PERCENTILE)
            fallback_surplus = percentile(positive_surplus, SURPLUS_FALLBACK_PERCENTILE)

            release_surplus = round_to(release_surplus, 25)
            fallback_surplus = round_to(fallback_surplus, 25)

            floor_applied = release_surplus < MIN_CHARGING_SURPLUS
            if floor_applied:
                release_surplus = MIN_CHARGING_SURPLUS

            if fallback_surplus >= release_surplus:
                fallback_surplus = max(0, release_surplus - 50)

            proposal["solar"] = {
                "release_surplus": release_surplus,
                "fallback_surplus": fallback_surplus,
                "release_battery": max(top_up, round_to(battery_high or top_up, 5) - 10)
                if battery_high
                else top_up,
                "fallback_battery": top_up,
                "sample_count": len(positive_surplus),
                "floor_applied": floor_applied,
            }

        return proposal

    def describe(self, proposal):
        lines = []
        lines.append(
            f"Based on {proposal['sample_count']} sample(s) over "
            f"{describe_span(self.span_seconds)}."
        )
        lines.append(
            f"Battery ranged roughly {proposal['observed_low']:g}% to "
            f"{proposal['observed_high']:g}% during that time."
        )

        if proposal.get("ceiling_plateau"):
            plateau = proposal["ceiling_plateau"]
            lines.append(
                f"  {plateau['fraction']*100:.0f}% of samples sat at or near "
                f"{plateau['ceiling']:g}% (battery topped out and held there). "
                f"That plateau was excluded when figuring out the charging "
                f"thresholds below, so they reflect real charging behavior "
                f"rather than time spent sitting full."
            )
        if proposal.get("floor_plateau"):
            plateau = proposal["floor_plateau"]
            lines.append(
                f"  {plateau['fraction']*100:.0f}% of samples sat at or near "
                f"{plateau['floor']:g}% (battery bottomed out and held there). "
                f"That plateau was excluded the same way."
            )

        lines.append("")
        lines.append(
            f"top up from grid when low: battery <= {proposal['top_up_at']:g}%"
        )
        lines.append(
            f"  the battery was at or below this level about "
            f"{TOP_UP_PERCENTILE}% of the time recorded"
        )
        lines.append(
            f"stop charging when full enough: battery >= {proposal['stop_at']:g}%"
        )
        lines.append(
            f"  the battery reached this level or higher about "
            f"{100 - STOP_PERCENTILE}% of the time recorded"
        )

        if proposal["solar"]:
            solar = proposal["solar"]
            lines.append("")
            lines.append(
                f"solar is carrying it, stay off the grid: "
                f"surplus > {solar['release_surplus']:g}W and "
                f"battery > {solar['release_battery']:g}%"
            )
            if solar.get("floor_applied"):
                lines.append(
                    f"  raised to {MIN_CHARGING_SURPLUS:g}W, the minimum surplus "
                    f"observed to actually charge the battery on this system. "
                    f"The raw statistical threshold from "
                    f"{solar['sample_count']} daylight sample(s) was lower."
                )
            else:
                lines.append(
                    f"  based on {solar['sample_count']} daylight sample(s); solar "
                    f"surplus exceeded this level in the upper "
                    f"{100 - SURPLUS_RELEASE_PERCENTILE}% of observed daylight readings"
                )
            lines.append(
                f"solar cannot keep up, fall back to the grid: "
                f"surplus < {solar['fallback_surplus']:g}W and "
                f"battery <= {solar['fallback_battery']:g}%"
            )
            lines.append(
                f"  surplus stayed below this level in the lower "
                f"{SURPLUS_FALLBACK_PERCENTILE}% of observed daylight readings"
            )
        else:
            lines.append("")
            lines.append(
                "not enough daylight solar data to propose solar rules yet "
                "(need at least 30 daylight samples with pv_surplus recorded)"
            )

        return "\n".join(lines)

    def render_rules_yaml(self, proposal, dwell_simple="2m", dwell_solar="15m"):
        lines = []
        lines.append("rules:")
        lines.append("  - name: top up from grid when low")
        lines.append(f"    when: battery_soc <= {proposal['top_up_at']:g}")
        lines.append(f"    for: {dwell_simple}")
        lines.append("    then: target.on")
        lines.append("")
        lines.append("  - name: stop charging when full enough")
        lines.append(f"    when: battery_soc >= {proposal['stop_at']:g}")
        lines.append(f"    for: {dwell_simple}")
        lines.append("    then: target.off")

        if proposal["solar"]:
            solar = proposal["solar"]
            lines.append("")
            lines.append("  - name: solar is carrying it, stay off the grid")
            lines.append(
                f"    when: pv_surplus > {solar['release_surplus']:g} and "
                f"battery_soc > {solar['release_battery']:g}"
            )
            lines.append(f"    for: {dwell_solar}")
            lines.append("    then: target.off")
            lines.append("")
            lines.append("  - name: solar cannot keep up, fall back to the grid")
            lines.append(
                f"    when: pv_surplus < {solar['fallback_surplus']:g} and "
                f"battery_soc <= {solar['fallback_battery']:g}"
            )
            lines.append(f"    for: {dwell_solar}")
            lines.append("    then: target.on")

        return "\n".join(lines) + "\n"


def apply_rules(profile_path, rules_yaml):
    text = Path(profile_path).read_text(encoding="utf-8")

    start = text.find("\nrules:")
    if start == -1:
        raise TuneError(f"could not find a rules: block in {profile_path}")
    start += 1

    end = text.find("\nlimits:", start)
    if end == -1:
        end = len(text)
    else:
        end += 1

    new_text = text[:start] + rules_yaml.rstrip("\n") + "\n\n" + text[end:]
    Path(profile_path).write_text(new_text, encoding="utf-8")
