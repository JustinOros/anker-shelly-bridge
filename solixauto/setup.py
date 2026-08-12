import asyncio
import os
import subprocess
import sys
from pathlib import Path

from . import notify, paths
from .profiles import list_profiles, load_yaml, save_yaml, slugify, write_text

STRATEGIES = {
    "battery": {
        "label": "Battery only - charge from the wall when low, stop when full",
        "detail": (
            "The safest place to start. Uses only battery percentage, which is "
            "the most reliable field on every model."
        ),
        "needs_solar": False,
    },
    "solar": {
        "label": "Solar aware - stop grid charging once the sun covers the load",
        "detail": (
            "Adds rules using solar input. Only pick this once you have watched "
            "the solar fields in daylight and confirmed they match the Anker app."
        ),
        "needs_solar": True,
    },
    "manual": {
        "label": "Write my own rules later",
        "detail": "Creates the profile with the safety floor only.",
        "needs_solar": False,
    },
}

PROFILE_TEMPLATE = """# Power profile: __NAME__
#
# Created by the setup wizard. Edit freely; rerun the validator after:
#     __INVOCATION__ run __NAME__ --test
#
# Field names come from the device profile. List them with:
#     __INVOCATION__ fields __SOURCE__

name: __NAME__
description: >
  __DESCRIPTION__

enabled: true

poll_interval: 10s

source:
  profile: __SOURCE__
  stale_after: 300s
  on_stale: safe_state

target:
  profile: __TARGET__
  channel: __CHANNEL__

safe_state: __SAFE_STATE__

# Checked before every rule below. Bypasses the rate limits and latches once
# tripped, so the battery cannot be stranded flat.
safety:
  battery_floor:
    at_or_below: __FLOOR__
    release_at: __RELEASE__
    then: target.on
    for: 30s
    notify: true
    notify_release: true

notifications:
  enabled: __NOTIFY__
  channels: __CHANNELS__
  title: "{profile}"
  template: >-
    {source_name} battery {battery_soc}%.
    {target_name} turned {action}.
  throttle: 5m
  on:
    - action
    - stale

rules:
__RULES__
limits:
  min_seconds_between_actions: 60
  max_actions_per_hour: 20
"""

BATTERY_RULES = """  - name: top up from grid when low
    when: battery_soc <= __LOW__
    for: 2m
    then: target.on

  - name: stop charging when full enough
    when: battery_soc >= __HIGH__
    for: 2m
    then: target.off
"""

SOLAR_RULES = """  - name: top up from grid when low
    when: battery_soc <= __LOW__
    for: 2m
    then: target.on

  - name: stop charging when full enough
    when: battery_soc >= __HIGH__
    for: 2m
    then: target.off

  - name: solar is carrying the load, stay off the grid
    when: pv_surplus > 200 and battery_soc > 50
    for: 15m
    then: target.off

  - name: solar cannot keep up, fall back to the grid
    when: pv_surplus < -200 and battery_soc <= 50
    for: 15m
    then: target.on
"""

MANUAL_RULES = """  - name: placeholder, replace me
    when: battery_soc <= 20
    for: 2m
    then: target.on
"""


def header(step, total, title):
    print()
    print("=" * 64)
    print(f"STEP {step} of {total}   {title}")
    print("=" * 64)


def note(text):
    for line in text.strip().splitlines():
        print(f"  {line.strip()}")


def pip_install(packages, editable_repo=None):
    command = [sys.executable, "-m", "pip", "install", "--upgrade"]
    if editable_repo:
        command.append(editable_repo)
    else:
        command.extend(packages)
    print()
    print(f"  running: {' '.join(command)}")
    result = subprocess.run(command)
    return result.returncode == 0


MODULE_TO_PACKAGE = {
    "yaml": "pyyaml",
    "aiohttp": "aiohttp",
    "aiofiles": "aiofiles",
    "cryptography": "cryptography",
    "paho": "paho-mqtt",
    "dotenv": "python-dotenv",
    "qrcode": "qrcode",
    "zeroconf": "zeroconf",
    "ifaddr": "ifaddr",
    "yarl": "yarl",
    "dateutil": "python-dateutil",
    "tzlocal": "tzlocal",
    "Crypto": "pycryptodome",
    "websockets": "websockets",
    "requests": "requests",
}

DEEP_PROBE = "import anker_solix_api.api, anker_solix_api.mqtt_factory"


def probe(statement):
    result = subprocess.run(
        [sys.executable, "-c", statement], capture_output=True, text=True
    )
    if result.returncode == 0:
        return None
    import re

    match = re.search(r"No module named '([^']+)'", result.stderr)
    if match:
        return match.group(1)
    return result.stderr.strip() or "unknown import error"


def check_dependencies(prompt, confirm):
    own = [
        ("yaml", "pyyaml"),
        ("aiohttp", "aiohttp"),
        ("qrcode", "qrcode"),
        ("zeroconf", "zeroconf"),
        ("dotenv", "python-dotenv"),
    ]

    missing = []
    for module, package in own:
        try:
            __import__(module)
        except ImportError:
            missing.append(package)

    if missing:
        print()
        print(f"  Missing: {', '.join(missing)}")
        if confirm("  Install them now?", default=True):
            if not pip_install(missing):
                print("  Install failed. Fix that, then rerun.")
                return False
        else:
            return False
    else:
        print("  Python packages: all present")

    print("  Checking the Anker library and everything it needs...")

    for attempt in range(12):
        problem = probe(DEEP_PROBE)

        if problem is None:
            print("  anker-solix-api: ready")
            return True

        if problem == "anker_solix_api":
            print()
            note(
                """
                anker-solix-api is not installed for this interpreter. It is the
                library that talks to Anker's cloud, and it is not on PyPI.
                """
            )
            if not confirm("  Install it from GitHub now?", default=True):
                print()
                note(
                    """
                    Skipped. Nothing can read your Anker device without it.
                    Install it when ready, then rerun this setup:
                      git clone https://github.com/thomluther/anker-solix-api.git
                      pip install -e anker-solix-api
                    """
                )
                return False
            if not pip_install(
                None, "git+https://github.com/thomluther/anker-solix-api.git"
            ):
                print()
                note(
                    """
                    That install did not work. Install it by hand, then rerun:
                      git clone https://github.com/thomluther/anker-solix-api.git
                      pip install -e anker-solix-api
                    """
                )
                return False
            continue

        if " " in problem:
            print()
            print(f"  The Anker library failed to import: {problem}")
            return False

        root = problem.split(".")[0]
        package = MODULE_TO_PACKAGE.get(root, root)
        print(f"  anker-solix-api needs {package}, installing...")
        if not pip_install([package]):
            print(f"  Could not install {package}.")
            return False

    print("  Dependency resolution did not settle. Install by hand and rerun.")
    return False


def ensure_credentials(prompt, confirm):
    from .credentials import load_credentials

    try:
        user, _, country = load_credentials()
        masked = user[:2] + "***" + user[-8:] if len(user) > 10 else "***"
        print(f"  Found Anker credentials for {masked} (country {country})")
        if not confirm("  Use these?", default=True):
            raise RuntimeError("user chose to re-enter")
        return True
    except Exception:
        pass

    import getpass

    print()
    note(
        """
        Your Anker account email and password are needed to read telemetry.
        They are written only to a local file with owner-only permissions.
        The password is not shown while you type.
        """
    )
    print()

    email = prompt("  Anker account email")
    password = getpass.getpass("  Anker account password: ").strip()
    if not password:
        print("  Password cannot be empty.")
        return False
    country = prompt("  Country code", "US").upper()

    destination = paths.BASE_DIR / ".env"

    def escape(value):
        return value.replace("\\", "\\\\").replace('"', '\\"')

    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(f'ANKERUSER="{escape(email)}"\n')
        handle.write(f'ANKERPASSWORD="{escape(password)}"\n')
        handle.write(f'ANKERCOUNTRY="{escape(country)}"\n')

    paths.secure_file(destination)
    os.environ["SOLIXAUTO_ENV"] = str(destination)

    for key in ("ANKERUSER", "ANKERPASSWORD", "ANKERCOUNTRY"):
        os.environ.pop(key, None)

    print()
    print(f"  Saved to {destination}")

    from .credentials import load_credentials

    try:
        check_user, _, _ = load_credentials()
    except Exception as err:
        print(f"  But it could not be read back: {err}")
        return False

    if check_user != email:
        print("  But reading it back gave a different value. Something is wrong")
        print(f"  with the file at {destination}.")
        return False

    print("  Verified readable.")
    if not paths.permissions_enforced():
        print("  Note: Windows does not enforce file permissions. Keep this")
        print("  directory out of any synced or shared folder.")
    return True


def pick_profile(kind, directory, choose, confirm, label):
    found = [p for p in list_profiles(directory) if p.name != "README.md"]

    if not found:
        return None

    if len(found) == 1:
        data = load_yaml(found[0])
        name = (data.get("identity") or {}).get("name") or found[0].stem
        print(f"  Found one {label}: {name}")
        return found[0]

    options = []
    for path in found:
        data = load_yaml(path)
        identity = data.get("identity") or {}
        display = identity.get("name") or path.stem
        extra = identity.get("model") or identity.get("part_number") or ""
        options.append((str(path), f"{display}  ({extra})"))

    print()
    print(f"  Which {label}?")
    print()
    chosen = choose(options, "  Choose")
    return Path(chosen)


def run_setup(prompt, choose, confirm):
    total = 8

    print()
    print("SOLIXAUTO SETUP")
    print()
    note(
        """
        This walks through everything: dependencies, your Anker account,
        finding your devices, notifications, writing an automation profile,
        testing it, and starting it in the background.

        Nothing switches any hardware until you say so near the end.
        Press Ctrl-C at any point to stop; nothing is left half-done.
        """
    )
    print()
    print(f"  Working directory: {paths.BASE_DIR}")

    paths.ensure_dirs()

    header(1, total, "Dependencies")
    if not check_dependencies(prompt, confirm):
        return False

    header(2, total, "Anker account")
    if not ensure_credentials(prompt, confirm):
        return False

    header(3, total, "Find your Anker device")
    from . import anker

    existing = [p for p in list_profiles(paths.ANKER_PROFILE_DIR)]
    if existing and not confirm(
        f"  {len(existing)} Anker device profile(s) already saved. Search again?",
        default=False,
    ):
        print("  Keeping what is already saved.")
    else:
        note(
            """
            Make sure the device is powered on, awake, and connected to wifi.
            Models that only pair over Bluetooth cannot be used.
            """
        )
        print()
        try:
            asyncio.run(anker.discover(explain_skipped=True))
        except Exception as err:
            print(f"  Discovery failed: {type(err).__name__}: {err}")
            return False

    source = pick_profile(
        "anker", paths.ANKER_PROFILE_DIR, choose, confirm, "Anker device"
    )
    if source is None:
        print("  No Anker device found. Cannot continue.")
        return False

    source_data = load_yaml(source)
    source_fields = set((source_data.get("readable") or {}).keys()) | set(
        (source_data.get("derived") or {}).keys()
    )

    header(4, total, "Find your Shelly plug")
    from . import shelly

    existing = list_profiles(paths.SHELLY_PROFILE_DIR)
    if existing and not confirm(
        f"  {len(existing)} Shelly profile(s) already saved. Search again?",
        default=False,
    ):
        print("  Keeping what is already saved.")
    else:
        note(
            """
            Scanning the local network. This takes up to a minute.
            """
        )
        try:
            asyncio.run(shelly.discover())
        except Exception as err:
            print(f"  Discovery failed: {type(err).__name__}: {err}")
            return False

    target = pick_profile(
        "shelly", paths.SHELLY_PROFILE_DIR, choose, confirm, "Shelly plug"
    )
    if target is None:
        print()
        note(
            """
            No Shelly found. If it is on a different subnet, rerun discovery
            with --network or --host, then start this wizard again.
            """
        )
        return False

    target_data = load_yaml(target)
    channels = target_data.get("channels") or {"0": {}}
    channel = 0
    if len(channels) > 1:
        options = [
            (index, f"channel {index}: {entry.get('name', '')}")
            for index, entry in sorted(channels.items())
        ]
        print()
        print("  Which channel controls the Anker device?")
        print()
        channel = int(choose(options, "  Choose"))

    if not (target_data.get("identity") or {}).get("name"):
        print()
        if confirm("  This plug has no name. Give it one now?", default=True):
            new_name = prompt("  Name")
            target = rename_profile(target, new_name)
            target_data = load_yaml(target)

    header(5, total, "Check the plug for competing automation")
    conflicts = check_conflicts(target, channel, confirm)
    if conflicts is False:
        return False

    header(6, total, "Notifications")
    setup_notifications(confirm)

    header(7, total, "Automation rules")
    profile_path = build_profile(
        source, target, channel, source_fields, prompt, choose, confirm
    )
    if profile_path is None:
        return False

    header(8, total, "Test, then start it")
    return finish(profile_path, confirm)


def rename_profile(path, new_name):
    data = load_yaml(path)
    identity = data.setdefault("identity", {})
    old = identity.get("name") or path.stem
    identity["name"] = new_name

    aliases = list(data.get("aliases") or [])
    for candidate in (new_name, old, path.stem):
        if candidate and candidate not in aliases:
            aliases.append(candidate)
    data["aliases"] = aliases

    destination = path.parent / f"{slugify(new_name).lower()}.yaml"
    save_yaml(path, data)
    if destination != path and not destination.exists():
        path.replace(destination)
        print(f"  Renamed to {destination.name}")
        return destination
    return path


def check_conflicts(target_path, channel, confirm):
    import aiohttp
    from .shelly import (
        ShellyTarget,
        automation_warnings,
        clear_auto_timer,
        delete_schedule,
        set_initial_state,
    )

    device = ShellyTarget(target_path, channel)

    async def go():
        async with aiohttp.ClientSession() as session:
            automation = await device.automation(session)
            warnings = automation_warnings(automation, channel)

            if not warnings:
                print("  Nothing on the plug will fight your rules.")
                return True

            print()
            print(f"  Found {len(warnings)} thing(s) set on the plug itself:")
            for item in warnings:
                print(f"    {item}")
            print()
            note(
                """
                These run on the device whether or not this tool is running,
                and they will override it. Removing them is recommended.
                """
            )
            print()

            if not confirm("  Remove them now?", default=True):
                print("  Left in place. Expect them to interfere.")
                return True

            for job in automation.get("schedules") or []:
                if job.get("enabled") and job.get("id") is not None:
                    ok = await delete_schedule(
                        session, device.host, job["id"], device.generation, device.auth
                    )
                    print(f"    {'removed' if ok else 'FAILED'}: {job['description']}")

            for index, values in (automation.get("timers") or {}).items():
                if str(index) != str(channel):
                    continue
                for key, value in values.items():
                    if key == "initial_state":
                        if str(value).lower() == "off":
                            ok = await set_initial_state(
                                session, device.host, index, "on",
                                device.generation, device.auth,
                            )
                            print(
                                f"    {'power-up set to on' if ok else 'FAILED to set power-up state'}"
                            )
                    else:
                        ok = await clear_auto_timer(
                            session, device.host, index, key,
                            device.generation, device.auth,
                        )
                        print(f"    {'disabled' if ok else 'FAILED to disable'} {key}")

            return True

    try:
        return asyncio.run(go())
    except Exception as err:
        print(f"  Could not check the plug: {type(err).__name__}: {err}")
        return True


def setup_notifications(confirm):
    enabled = notify.enabled_channels()
    if enabled:
        print(f"  Already configured: {', '.join(enabled)}")
        return True

    note(
        """
        Notifications tell you when the automation switches something, and
        when the battery hits its safety floor. Strongly recommended if the
        machine will be running unattended.
        """
    )
    print()
    if not confirm("  Set up push notifications now?", default=True):
        print("  Skipped. You can do this later with: notify-setup")
        return False

    topic = notify.generate_topic()
    url = notify.subscribe_url(topic)

    print()
    note(
        """
        Using ntfy: free, open source, no account needed.
        A random private topic has been generated. Anyone who knows it can
        read your alerts, so do not share it.
        """
    )
    print()
    notify.show_qr(notify.NTFY_IOS_URL, "1. Scan to install the app (iPhone):")
    print()
    print(f"     Android: {notify.NTFY_ANDROID_URL}")
    print()
    try:
        input("  Press Enter once the app is installed...")
    except EOFError:
        pass

    print()
    notify.show_qr(url, "2. Scan to open your topic, or add it by hand:")
    print()
    print(f"     topic:  {topic}")
    print("     server: ntfy.sh")
    print()
    print("  On iPhone the QR opens a browser page. Use the app's + button")
    print("  and paste the topic instead.")
    print()
    try:
        input("  Press Enter once you have subscribed in the app...")
    except EOFError:
        pass

    notify.apply_settings("ntfy", {"enabled": True, "topic": topic})

    print()
    print("  Sending a test...")
    try:
        results = asyncio.run(notify.send_test("ntfy"))
        outcome = results.get("ntfy")
        print(f"  ntfy: {outcome}")
    except Exception as err:
        print(f"  test failed: {err}")

    return True


def existing_profiles():
    found = []
    for path in list_profiles(paths.POWER_PROFILE_DIR):
        if path.name == "README.md":
            continue
        try:
            from .rules import PowerProfile

            profile = PowerProfile(path)
        except Exception:
            continue
        found.append((path, profile))
    return found


def keep_existing(prompt, choose, confirm):
    found = existing_profiles()
    if not found:
        return None

    print()
    print(f"  You already have {len(found)} automation profile(s):")
    print()
    for path, profile in found:
        rules = len(profile.active_rules())
        floor = profile.battery_floor
        detail = f"{rules} rule(s)"
        if floor:
            detail += f", floor at {floor.threshold:g}%"
        print(f"    {path.name}  ({detail})")

    print()
    print("  Writing a new one would replace a file of the same name, losing any")
    print("  thresholds you have tuned.")
    print()

    if len(found) == 1:
        path, profile = found[0]
        if confirm(f"  Keep using {path.name} as it is?", default=True):
            return path
        return None

    options = [(str(path), path.name) for path, _ in found]
    options.append(("__new__", "Write a new profile instead"))
    print("  Which do you want to use?")
    print()
    chosen = choose(options, "  Choose")
    if chosen == "__new__":
        return None
    return Path(chosen)


def build_profile(source, target, channel, fields, prompt, choose, confirm):
    kept = keep_existing(prompt, choose, confirm)
    if kept is not None:
        print()
        print(f"  Keeping {kept.name} unchanged.")
        return kept

    print()
    note(
        """
        What should the plug do? Pick the closest fit; you can edit the file
        afterwards.
        """
    )
    print()

    has_solar = "pv_surplus" in fields
    options = []
    for key, entry in STRATEGIES.items():
        if entry["needs_solar"] and not has_solar:
            continue
        options.append((key, entry["label"]))

    strategy = choose(options, "  Choose")
    print()
    note(STRATEGIES[strategy]["detail"])

    controls_charging = confirm(
        "\n  Does this plug supply power TO the Anker device?", default=True
    )

    print()
    if controls_charging:
        note(
            """
            Then turning the plug ON charges the Anker device. The safety floor
            and stale-telemetry behaviour will both fail toward charging.
            """
        )
    else:
        note(
            """
            Then the plug runs some other load. Failure states will leave it
            off rather than on.
            """
        )

    low = high = None
    if strategy != "manual":
        print()
        low = int(prompt("  Charge when battery drops to (%)", "35"))
        high = int(prompt("  Stop charging at (%)", "85"))
        while high <= low + 10:
            print("  Leave at least 10 points between them, or the plug will")
            print("  switch constantly. Try again.")
            low = int(prompt("  Charge when battery drops to (%)", "35"))
            high = int(prompt("  Stop charging at (%)", "85"))

    print()
    floor = int(prompt("  Emergency floor, never let battery fall below (%)", "20"))
    release = int(prompt("  Hold emergency charging until (%)", str(floor + 20)))
    while release <= floor:
        release = int(prompt(f"  Must be above {floor}. Hold until (%)", str(floor + 20)))

    if strategy == "battery":
        rules = BATTERY_RULES
    elif strategy == "solar":
        rules = SOLAR_RULES
    else:
        rules = MANUAL_RULES

    if low is not None:
        rules = rules.replace("__LOW__", str(low)).replace("__HIGH__", str(high))

    name = prompt("\n  Name for this automation", "battery-charging")
    name = slugify(name).lower()

    channels = notify.enabled_channels()
    content = PROFILE_TEMPLATE
    for token, value in (
        ("__NAME__", name),
        ("__DESCRIPTION__", STRATEGIES[strategy]["label"]),
        ("__SOURCE__", source.name),
        ("__TARGET__", target.name),
        ("__CHANNEL__", str(channel)),
        ("__SAFE_STATE__", "on" if controls_charging else "off"),
        ("__FLOOR__", str(floor)),
        ("__RELEASE__", str(release)),
        ("__NOTIFY__", "true" if channels else "false"),
        ("__CHANNELS__", "[" + ", ".join(channels) + "]"),
        ("__RULES__", rules),
        ("__INVOCATION__", paths.invocation()),
    ):
        content = content.replace(token, value)

    destination = paths.POWER_PROFILE_DIR / f"{name}.yaml"
    if destination.exists():
        print()
        print(f"  WARNING: {destination.name} already exists and will be")
        print("  replaced. Any thresholds you tuned in it will be lost.")
        print()
        if not confirm("  Overwrite it?", default=False):
            print("  Keeping the existing file unchanged.")
            return destination

    write_text(destination, content)
    print()
    print(f"  Wrote {paths.relative(destination)}")
    return destination


def finish(profile_path, confirm):
    from . import service
    from .engine import dry_run_report
    from .rules import PowerProfile, ProfileError

    try:
        profile = PowerProfile(profile_path)
    except ProfileError as err:
        print(f"  The generated profile is invalid: {err}")
        return False

    print()
    print("  Checking it against your devices, without switching anything...")

    try:
        ok = asyncio.run(dry_run_report(profile, cycles=2))
    except Exception as err:
        print(f"  Test failed: {type(err).__name__}: {err}")
        return False

    if not ok:
        print("  Fix the problems above, then rerun.")
        return False

    installed, state = service.status(profile.path.stem)
    if installed:
        print()
        print(f"  WARNING: a service named {profile.path.stem!r} already exists")
        print(f"  and is {state}.")
        print()
        print("  Installing again will replace it and point it at:")
        print(f"    {paths.BASE_DIR}")
        print()
        if not confirm("  Replace the existing service?", default=False):
            print("  Left the existing service alone.")
            return True

    print()
    if not confirm("  Start this automation in the background now?", default=True):
        print()
        print("  Nothing is running. When you are ready:")
        print(f"    {paths.command('run ' + profile.path.stem)}")
        print(f"    {paths.command('service ' + profile.path.stem)}")
        return True

    ok, detail = service.install(profile.path.stem)
    print()
    if not ok:
        print(f"  Could not install the service: {detail}")
        print(f"  Run it manually with: {paths.command('run ' + profile.path.stem)}")
        return False

    running, state = service.status(profile.path.stem)
    print("  DONE. The automation is running.")
    print()
    print(f"    status  {state}")
    print(f"    log     {paths.ENGINE_LOG}")
    print(f"    profile {profile.path}")
    print()
    print("  Useful later:")
    print(f"    {paths.command('service ' + profile.path.stem + ' --status')}")
    print(f"    {paths.command('service ' + profile.path.stem + ' --uninstall')}")
    print(f"    tail -f {paths.ENGINE_LOG}")

    if sys.platform == "darwin":
        print()
        print("  This stops if the Mac sleeps. In System Settings > Energy,")
        print("  prevent automatic sleeping, and set 'Start up when power is")
        print("  connected' to Always.")

    return True
