import argparse
import asyncio
import ipaddress
import os
from datetime import datetime
import platform
import shutil
import sys

from . import notify, paths, shelly, templates
from .engine import Engine, Reporter, configure_event_loop, dry_run_report
from .profiles import list_profiles, load_yaml
from .rules import PowerProfile, ProfileError, format_duration, validate


def fail(message):
    print(f"error: {message}", file=sys.stderr)
    sys.exit(1)


def cmd_setup(args):
    from . import setup as setup_module

    def wrapped_confirm(question, default=False):
        return confirm(question, default)

    try:
        ok = setup_module.run_setup(prompt, choose, wrapped_confirm)
    except KeyboardInterrupt:
        print()
        print()
        print("Setup stopped. Nothing was left half-finished.")
        print(f"Run it again any time: {paths.command('setup')}")
        sys.exit(1)

    sys.exit(0 if ok else 1)


def cmd_doctor(args):
    from . import notify as notify_module

    print()
    print("Environment")
    print(f"  platform     {platform.system()} {platform.release()} ({sys.platform})")
    print(f"  python       {platform.python_version()} at {sys.executable}")

    problems = []
    warnings = []

    if sys.version_info < (3, 9):
        problems.append(f"Python {platform.python_version()} is too old, need 3.9+")

    print()
    print("Dependencies")
    for module, package, required in (
        ("yaml", "pyyaml", True),
        ("aiohttp", "aiohttp", True),
        ("anker_solix_api", "anker-solix-api", True),
        ("zeroconf", "zeroconf", False),
        ("dotenv", "python-dotenv", False),
        ("ifaddr", "ifaddr", False),
        ("qrcode", "qrcode", False),
    ):
        try:
            __import__(module)
            print(f"  {package:<18} ok")
        except ImportError:
            label = "MISSING" if required else "optional, not installed"
            print(f"  {package:<18} {label}")
            if required:
                problems.append(f"{package} is not installed")
            else:
                warnings.append(f"{package} is not installed")

    print()
    print("Directories")
    paths.ensure_dirs()
    print(f"  root         {paths.BASE_DIR}")
    if paths.permissions_enforced():
        print("  permissions  POSIX mode bits applied to secret files")
    else:
        print("  permissions  Windows, mode bits not enforced")
        warnings.append(
            "on Windows, notifications.yaml and .env are not protected by file "
            "permissions. Keep this directory out of shared or synced folders."
        )

    print()
    print("Credentials")
    try:
        from .credentials import load_credentials

        user, _, country = load_credentials()
        masked = user[:2] + "***" + user[-6:] if len(user) > 8 else "***"
        print(f"  anker        found ({masked}, country {country})")
    except Exception as err:
        print("  anker        not found")
        problems.append(
            "Anker credentials not found. Set ANKERUSER and ANKERPASSWORD, or "
            f"put a .env file in {paths.BASE_DIR}"
        )

    print()
    print("Network")
    try:
        from .shelly import local_subnets

        subnets = local_subnets()
        if subnets:
            for network in subnets:
                print(f"  subnet       {network}")
        else:
            print("  subnet       could not detect one")
            warnings.append(
                "no local subnet detected. Use discover-shelly --network or --host."
            )
    except Exception as err:
        print(f"  subnet       error: {err}")

    print()
    print("Notifications")
    backend = notify_module.desktop_backend()
    print(f"  desktop      {backend or 'no backend available'}")
    if backend is None and sys.platform.startswith("linux"):
        warnings.append(
            "desktop notifications need libnotify-bin on Linux "
            "(apt install libnotify-bin)"
        )
    enabled = notify_module.enabled_channels()
    print(f"  channels     {', '.join(enabled) if enabled else 'none enabled'}")

    print()
    for warning in warnings:
        print(f"  warning: {warning}")
    for problem in problems:
        print(f"  PROBLEM: {problem}")

    print()
    print("Invocation")
    print(f"  this run       {paths.invocation()}")
    if paths.invocation() != "solixauto":
        print()
        print("  To type just 'solixauto' from anywhere, add an alias:")
        shell_file = "~/.zshrc" if "zsh" in os.environ.get("SHELL", "") else "~/.bashrc"
        script = os.path.abspath(sys.argv[0]) if sys.argv else "solixauto.py"
        print(f"    echo 'alias solixauto=\"{sys.executable} {script}\"' >> {shell_file}")
        print(f"    source {shell_file}")

    print()
    if problems:
        print(f"{len(problems)} problem(s) found.")
        sys.exit(1)
    print("Ready.")


def cmd_init(args):
    base = paths.ensure_dirs()
    templates.scaffold_readme()
    print(f"Initialized {base}")
    for directory in paths.ALL_DIRS[1:]:
        print(f"  {paths.relative(directory)}/")
    print(f"  {paths.relative(paths.POWER_PROFILE_DIR / 'README.md')}")


def cmd_discover_anker(args):
    from . import anker

    written = asyncio.run(
        anker.discover(
            settle=args.settle,
            only_pn=args.pn,
            only_sn=args.sn,
            skip=args.skip,
            include_offline=args.include_offline,
            explain_skipped=args.explain_skipped,
        )
    )
    print()
    print(f"Wrote {len(written)} Anker device profile(s).")


def cmd_discover_shelly(args):
    network = None
    if args.network:
        try:
            network = ipaddress.ip_network(args.network, strict=False)
        except ValueError as err:
            fail(f"invalid network {args.network!r}: {err}")

    written = asyncio.run(
        shelly.discover(
            hosts=args.host,
            network=network,
            use_mdns=not args.no_mdns,
        )
    )
    print()
    print(f"Wrote {len(written)} Shelly device profile(s).")
    if not written:
        print(f"Nothing found. Try: {paths.command('discover-shelly --host 192.168.1.50')}")


def cmd_name(args):
    from .profiles import save_yaml, slugify

    path = paths.resolve_profile(args.device, None)
    if path is None:
        fail(f"no device profile matching {args.device!r}")

    data = load_yaml(path)
    identity = data.setdefault("identity", {})
    old_name = identity.get("name") or ""
    new_name = args.name.strip()

    if not new_name:
        fail("name cannot be empty")

    identity["name"] = new_name

    aliases = list(data.get("aliases") or [])
    for candidate in (new_name, old_name, path.stem):
        if candidate and candidate not in aliases:
            aliases.append(candidate)
    data["aliases"] = aliases

    destination = path.parent / f"{slugify(new_name).lower()}.yaml"

    if destination != path and destination.exists():
        fail(f"{destination.name} already exists. Pick another name.")

    save_yaml(path, data)

    if destination != path:
        path.replace(destination)
        print(f"renamed {path.name} -> {destination.name}")
    print(f"{destination.name} is now named {new_name!r}")
    print(f"resolves as: {', '.join(aliases)}")

    if args.on_device:
        if data.get("kind") != "shelly":
            fail("--on-device only applies to Shelly devices")
        asyncio.run(_push_shelly_name(data, new_name))


async def _push_shelly_name(data, new_name):
    import aiohttp
    from .shelly import auth_from, CONTROL_TIMEOUT

    host = (data.get("access") or {}).get("host")
    generation = int((data.get("identity") or {}).get("generation", 1) or 1)

    if generation < 2:
        fail("writing the name on device is only supported for Gen2+ Shellys")

    url = f"http://{host}/rpc/Sys.SetConfig"
    payload = {"config": {"device": {"name": new_name}}}

    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(
                url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=CONTROL_TIMEOUT),
                auth=auth_from(data),
            ) as response:
                body = await response.text()
                if response.status != 200:
                    fail(f"HTTP {response.status} from {host}: {body}")
        except Exception as err:
            fail(f"could not write name to {host}: {type(err).__name__}: {err}")

    print(f"wrote the name to the device at {host}")


def confirm(question, default=False):
    suffix = "[y/N]" if not default else "[Y/n]"
    answer = _read(f"{question} {suffix}: ").lower()
    if not answer:
        return default
    return answer in ("y", "yes")


def cmd_service(args):
    from . import service

    profile = load_power_profile(args.profile)
    name = profile.path.stem
    kind = service.platform_kind()

    if kind == "unknown":
        fail(f"no service manager known for {sys.platform}")

    if args.show:
        content = service.render(name)
        print()
        print(content)
        return

    if args.status:
        running, detail = service.status(name)
        print()
        print(f"{name}: {detail}")
        target = service.unit_path(name)
        if target:
            print(f"  unit: {target}")
        print(f"  log:  {paths.ENGINE_LOG}")
        sys.exit(0 if running else 1)

    if args.uninstall:
        ok, detail = service.uninstall(name)
        print()
        print(detail if detail else "removed")
        print(f"{name} will no longer start automatically.")
        print("Note: the Shelly stays in whatever state it was last set to.")
        return

    if kind == "windows":
        print()
        print(service.windows_instructions(name))
        return

    target = service.unit_path(name)

    print()
    print(f"This will install a background service for {name}:")
    print()
    print(f"  unit file   {target}")
    print(f"  runs        {sys.executable} {service.script_path()} run {name}")
    print(f"  log         {paths.ENGINE_LOG}")
    print()
    print("It starts at login, restarts if it crashes, and will switch your")
    print("Shelly without anyone watching.")
    print()

    env_file = service.env_file_in_use()
    if env_file:
        print(f"  credentials {env_file}")
    else:
        print("  WARNING: no .env file found. The service will fail to")
        print("  authenticate unless ANKERUSER and ANKERPASSWORD are set")
        print("  in the environment it inherits.")
    print()

    if not args.yes and not confirm("Install and start it now?", default=True):
        print("Not installed. Nothing is running.")
        print(f"Run it in the foreground with: {paths.command('run ' + name)}")
        return

    ok, detail = service.install(name)
    print()
    if not ok:
        print(f"Install failed: {detail}")
        sys.exit(1)

    print("Installed and started.")
    print()
    running, state = service.status(name)
    print(f"  status: {state}")
    print(f"  log:    {paths.ENGINE_LOG}")
    print()
    print(f"Stop and remove it with: {paths.command(f'service {name} --uninstall')}")

    if sys.platform == "darwin":
        print()
        print("If this Mac sleeps, the automation stops with it. For an always-on")
        print("machine set System Settings > Energy Saver to prevent sleep.")


def cmd_monitor(args):
    import webbrowser
    from . import monitor as monitor_module

    try:
        server, sampler = monitor_module.serve(
            port=args.port,
            interval=args.interval,
            host=args.host,
            allow_remote_edit=args.allow_remote_edit,
        )
    except OSError as err:
        fail(
            f"could not start on {args.host}:{args.port}: {err}\n"
            "Another monitor may already be running. Try --port 8766."
        )

    url = f"http://{args.host}:{args.port}"
    print()
    print(f"Power monitor running at {url}")
    print()
    print("  Readings come from a running automation, so start one first if the")
    print("  Anker box says it has no live data:")
    print(f"    {paths.command('service <profile>')}")
    print()
    if monitor_module.Handler.allow_edit:
        print("  Click a device name to rename it.")
    else:
        print("  Renaming is disabled because this is reachable from other")
        print("  machines. Add --allow-remote-edit to permit it.")
    print()
    print("  Press Ctrl-C to stop.")

    if not args.no_open:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
        print("stopped")
    finally:
        sampler.stop()
        server.shutdown()
        server.server_close()


def cmd_conflicts(args):
    from .shelly import (
        ShellyTarget,
        automation_warnings,
        clear_auto_timer,
        delete_schedule,
        delete_webhook,
        set_initial_state,
    )
    import aiohttp

    path = paths.resolve_profile(args.device, "shelly")
    if path is None:
        fail(f"no Shelly device profile matching {args.device!r}")

    target = ShellyTarget(path, args.channel)

    async def go():
        async with aiohttp.ClientSession() as session:
            automation = await target.automation(session)
            if not automation:
                fail(f"could not reach {target.host}")

            print()
            print(f"{target.label} channel {target.channel}")
            print()

            schedules = automation.get("schedules") or []
            print(f"Schedules on the device ({len(schedules)}):")
            if not schedules:
                print("  none")
            for job in schedules:
                mark = "" if job.get("enabled") else "  [disabled]"
                print(f"  {job['description']}{mark}")

            timers = automation.get("timers") or {}
            print()
            print(f"Auto timers and power-up state ({len(timers)}):")
            if not timers:
                print("  none")
            for index, values in sorted(timers.items()):
                for key, value in values.items():
                    print(f"  channel {index}: {key} = {value}")

            hooks = automation.get("webhooks") or []
            print()
            print(f"Webhooks and actions ({len(hooks)}):")
            if not hooks:
                print("  none")
            for hook in hooks:
                label = hook.get("name") or hook.get("event")
                mark = "" if hook.get("enabled") else "  [disabled]"
                print(f"  {label}{mark}")

            warnings = automation_warnings(automation, target.channel)
            print()

            if not warnings:
                print("Nothing on the device will fight your rules.")
                print()
                print("Note: schedules created as Shelly Cloud 'scenes' live in the")
                print("cloud, not on the device, and cannot be seen from here.")
                return

            print(f"{len(warnings)} of these will conflict with your rules:")
            for item in warnings:
                print(f"  {item}")

            if not args.fix:
                print()
                print("Rerun with --fix to remove them from here, or change them")
                print("in the Shelly app.")
                print()
                print("Note: schedules created as Shelly Cloud 'scenes' live in the")
                print("cloud, not on the device, and cannot be seen from here.")
                return

            if target.generation < 2:
                fail("--fix currently supports Gen2 and newer Shellys only")

            print()
            print("=" * 60)
            print("These changes are made on the device and cannot be undone from")
            print("here. You would have to recreate them in the Shelly app.")
            print("=" * 60)

            changed = 0

            for job in schedules:
                if not job.get("enabled") or job.get("id") is None:
                    continue
                print()
                print(f"  {job['description']}")
                if not (args.yes or confirm("  Delete this schedule?")):
                    print("  kept")
                    continue
                if await delete_schedule(
                    session, target.host, job["id"], target.generation, target.auth
                ):
                    print("  deleted")
                    changed += 1
                else:
                    print("  FAILED to delete")

            for index, values in sorted(timers.items()):
                if str(index) != str(target.channel):
                    continue
                for key, value in values.items():
                    print()
                    if key == "initial_state":
                        if str(value).lower() == "on":
                            continue
                        print(f"  channel {index} powers up to '{value}'")
                        print("  Recommended: 'on', so a power cut cannot leave")
                        print("  whatever this plug charges stranded off.")
                        if not (
                            args.yes or confirm("  Set power-up state to ON?")
                        ):
                            print("  kept")
                            continue
                        if await set_initial_state(
                            session, target.host, index, "on",
                            target.generation, target.auth,
                        ):
                            print("  set to on")
                            changed += 1
                        else:
                            print("  FAILED to change")
                    else:
                        print(f"  channel {index} has {key} = {value}")
                        if not (args.yes or confirm(f"  Disable {key}?")):
                            print("  kept")
                            continue
                        if await clear_auto_timer(
                            session, target.host, index, key,
                            target.generation, target.auth,
                        ):
                            print("  disabled")
                            changed += 1
                        else:
                            print("  FAILED to change")

            for hook in hooks:
                if not hook.get("enabled") or hook.get("id") is None:
                    continue
                label = hook.get("name") or hook.get("event")
                print()
                print(f"  webhook: {label}")
                if not (args.yes or confirm("  Delete this webhook?")):
                    print("  kept")
                    continue
                if await delete_webhook(
                    session, target.host, hook["id"], target.generation, target.auth
                ):
                    print("  deleted")
                    changed += 1
                else:
                    print("  FAILED to delete")

            print()
            if not changed:
                print("Nothing changed.")
                return

            print(f"{changed} change(s) applied. Re-checking the device...")
            after = await target.automation(session)
            remaining = automation_warnings(after, target.channel)
            print()
            if remaining:
                print(f"{len(remaining)} conflict(s) remain:")
                for item in remaining:
                    print(f"  {item}")
            else:
                print("Device is clean. Nothing left that will fight your rules.")

            print()
            print("Re-run discover-shelly to refresh the stored profile.")

    asyncio.run(go())


def cmd_status(args):
    from .anker import AnkerSource
    from .rules import derived_values

    path = paths.resolve_profile(args.device, "anker")
    if path is None:
        fail(f"no Anker device profile matching {args.device!r}")

    profile = load_yaml(path)
    identity = profile.get("identity", {})
    label = identity.get("model") or identity.get("part_number") or "device"

    source = AnkerSource(path)

    def show(values, age):
        keys = sorted(values)
        if args.fields:
            wanted = [f.strip().lower() for f in args.fields]
            keys = [k for k in keys if any(w in k.lower() for w in wanted)]
        if not keys:
            print("  (no fields matched)")
            return

        derived_names = set((profile.get("derived") or {}).keys())
        width = max(len(k) for k in keys)
        for key in keys:
            marker = "  <- derived" if key in derived_names else ""
            print(f"  {key.ljust(width)}  {values[key]}{marker}")
        if age is not None:
            print()
            print(f"  last message {age:.0f}s ago")

    async def go():
        print(f"Connecting to {label} {source.serial}...")
        try:
            await source.start(settle=args.settle)
        except RuntimeError as err:
            fail(str(err))

        try:
            while True:
                status = source.read()
                if not status:
                    from .anker import OFFLINE_HINT

                    print("no telemetry decoded yet")
                    print(OFFLINE_HINT)
                else:
                    values = derived_values(profile, status)
                    print()
                    print(datetime.now().strftime("%H:%M:%S"))
                    show(values, source.age_seconds())

                if not args.watch:
                    break
                await asyncio.sleep(args.interval)
        finally:
            await source.stop()

    try:
        asyncio.run(go())
    except KeyboardInterrupt:
        print()
        print("stopped")


def cmd_switch(args):
    from .shelly import ShellyTarget
    import aiohttp

    path = paths.resolve_profile(args.device, "shelly")
    if path is None:
        fail(f"no Shelly device profile matching {args.device!r}")

    target = ShellyTarget(path, args.channel)

    async def go():
        async with aiohttp.ClientSession() as session:
            current = await target.get_state(session)
            if current is None:
                fail(
                    f"could not read {target.host}. Check that it is powered on, "
                    "on the network, and that access.host in its profile is right."
                )

            print(f"{target.label} channel {target.channel} is currently "
                  f"{'ON' if current else 'OFF'}")

            action = args.action.lower()
            if action == "status":
                return

            if action == "toggle":
                desired = not current
            else:
                desired = action == "on"

            if desired == current and action != "toggle":
                print(f"already {'ON' if desired else 'OFF'}, nothing to do")
                return

            print(f"turning {'ON' if desired else 'OFF'}...")
            try:
                await target.set_state(session, desired)
            except Exception as err:
                fail(f"{type(err).__name__}: {err}")

            await asyncio.sleep(1.0)
            confirmed = await target.get_state(session)
            if confirmed is None:
                print("command sent but the state could not be read back")
            elif confirmed == desired:
                print(f"confirmed {'ON' if confirmed else 'OFF'}")
            else:
                print(
                    f"WARNING: commanded {'ON' if desired else 'OFF'} but device "
                    f"reports {'ON' if confirmed else 'OFF'}"
                )

    asyncio.run(go())


def cmd_devices(args):
    paths.ensure_dirs()

    for label, directory in (
        ("Anker", paths.ANKER_PROFILE_DIR),
        ("Shelly", paths.SHELLY_PROFILE_DIR),
    ):
        found = list_profiles(directory)
        print()
        print(f"{label} ({len(found)}):")
        if not found:
            print("  none. Run the matching discover command.")
            continue
        for path in found:
            try:
                data = load_yaml(path)
            except Exception as err:
                print(f"  {path.name}  [unreadable: {err}]")
                continue
            identity = data.get("identity", {})
            friendly = identity.get("name") or "(unnamed)"
            descriptor = " ".join(
                str(part)
                for part in (
                    identity.get("model"),
                    identity.get("serial") or identity.get("id"),
                )
                if part
            )
            extra = ""
            if data.get("kind") == "shelly":
                extra = f"  host {data.get('access', {}).get('host')}"
            elif data.get("kind") == "anker":
                extra = f"  {len(data.get('readable') or {})} fields"
            print(f"  {friendly}")
            print(f"    file {path.name}")
            print(f"    {descriptor}{extra}")
            channels = data.get("channels") or {}
            if len(channels) > 1:
                labels = ", ".join(
                    f"{index}={entry.get('name')}" for index, entry in sorted(channels.items())
                )
                print(f"    channels {labels}")


def cmd_fields(args):
    path = paths.resolve_profile(args.device, "anker")
    if path is None:
        fail(f"no Anker device profile matching {args.device!r}")

    data = load_yaml(path)
    readable = data.get("readable") or {}
    derived = data.get("derived") or {}

    query = (args.filter or "").lower()

    print()
    print(f"{path.name}")
    print()
    print(f"derived ({len(derived)}):")
    for name, spec in sorted(derived.items()):
        if query and query not in name.lower():
            continue
        expression = spec.get("expression") if isinstance(spec, dict) else spec
        description = spec.get("description", "") if isinstance(spec, dict) else ""
        print(f"  {name:<26} = {expression}")
        if description:
            print(f"  {'':<26}   {description}")

    print()
    print(f"readable ({len(readable)}):")
    for name, spec in sorted(readable.items()):
        if query and query not in name.lower():
            continue
        kind = spec.get("type") if isinstance(spec, dict) else "?"
        sample = spec.get("sample") if isinstance(spec, dict) else spec
        print(f"  {name:<26} {kind:<6} sample={sample!r}")

    if args.writable:
        writable = data.get("writable") or {}
        print()
        print(f"writable ({len(writable)}) - reference only, the engine never calls these:")
        for name, spec in sorted(writable.items()):
            params = ", ".join(spec.get("parameters", [])) if isinstance(spec, dict) else ""
            print(f"  {name:<26} ({params})")


def _read(question, secret=False):
    import getpass

    try:
        if secret:
            return getpass.getpass(question).strip()
        return input(question).strip()
    except EOFError:
        print()
        print()
        print("No input available. Run this from an interactive terminal.")
        sys.exit(1)


def prompt(label, default=None, secret=False):
    suffix = f" [{default}]" if default else ""
    while True:
        value = _read(f"{label}{suffix}: ", secret)
        if value:
            return value
        if default is not None:
            return default
        print("  required")


def choose(options, label="Choose"):
    for index, (key, description) in enumerate(options, start=1):
        print(f"  {index}) {description}")
    print()
    while True:
        raw = _read(f"{label} [1-{len(options)}]: ")
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1][0]
        print("  pick a number from the list")


def setup_ntfy(args):
    from . import notify as notify_module

    topic = args.topic or notify_module.generate_topic()
    url = notify_module.subscribe_url(topic)

    print()
    print("ntfy is free, open source, and needs no account.")
    print()

    if not args.no_qr:
        platform_choice = choose(
            [
                ("ios", "iPhone or iPad"),
                ("android", "Android"),
                ("installed", "I already have the ntfy app installed"),
                ("skip", "Skip, I will set it up later"),
            ],
            "Which phone will receive the alerts",
        )
    else:
        platform_choice = "installed"

    if platform_choice == "ios":
        print()
        print("STEP 1 - install the app")
        print()
        notify_module.show_qr(
            notify_module.NTFY_IOS_URL,
            "Point your camera at this to open the App Store:",
            dark_terminal=not args.light_terminal,
        )
        print()
        input("Press Enter once the app is installed...")
    elif platform_choice == "android":
        print()
        print("STEP 1 - install the app")
        print()
        notify_module.show_qr(
            notify_module.NTFY_ANDROID_URL,
            "Point your camera at this to open Google Play:",
            dark_terminal=not args.light_terminal,
        )
        print()
        print(f"  F-Droid instead: {notify_module.NTFY_FDROID_URL}")
        print()
        input("Press Enter once the app is installed...")

    print()
    print("STEP 2 - subscribe to your private topic")
    print()
    print("A random topic has been generated for you. The topic IS the")
    print("credential, which is why it is long. Anyone who knows it can read")
    print("your alerts, so do not share or post it.")
    print()

    notify_module.show_qr(
        url,
        "Scan this to open the topic directly in ntfy:",
        dark_terminal=not args.light_terminal,
    )

    print()
    print("  If the code will not scan, your terminal may use a light")
    print("  background. Rerun with --light-terminal to flip it.")
    print()
    print("  Or add it by hand in the app:")
    print("    tap +, then 'Subscribe to topic'")
    print(f"    topic:  {topic}")
    print("    server: ntfy.sh (leave as default)")
    print()
    print(f"  Docs: {notify_module.NTFY_DOCS_URL}")
    print()
    input("Press Enter once you have subscribed...")

    return "ntfy", {"enabled": True, "topic": topic}


def setup_desktop(args):
    from . import notify as notify_module

    backend = notify_module.desktop_backend()
    print()
    if backend:
        print(f"Desktop notifications will use: {backend}")
    else:
        print("No desktop notification backend was found on this machine.")
        if sys.platform.startswith("linux"):
            print("Install libnotify-bin, then rerun.")
        return None, None
    print("These only appear on this machine, not on your phone.")
    return "desktop", {"enabled": True}


def setup_pushover(args):
    print()
    print("Create an app at https://pushover.net to get an API token.")
    print("Your user key is on the dashboard after you log in.")
    print()
    user_key = prompt("User key")
    api_token = prompt("API token", secret=True)
    return "pushover", {
        "enabled": True,
        "user_key": user_key,
        "api_token": api_token,
    }


def setup_telegram(args):
    print()
    print("Message @BotFather on Telegram to create a bot and get its token.")
    print("Then message your new bot once, and read your chat id from:")
    print("  https://api.telegram.org/bot<TOKEN>/getUpdates")
    print()
    token = prompt("Bot token", secret=True)
    chat_id = prompt("Chat id")
    return "telegram", {"enabled": True, "bot_token": token, "chat_id": chat_id}


def setup_email(args):
    print()
    print("For Gmail you must use an App Password, not your normal password.")
    print()
    host = prompt("SMTP host", "smtp.gmail.com")
    port = prompt("SMTP port", "587")
    username = prompt("Username")
    password = prompt("Password", secret=True)
    sender = prompt("From address", username)
    recipients = prompt("To address(es), comma separated", username)
    return "email", {
        "enabled": True,
        "host": host,
        "port": port,
        "username": username,
        "password": password,
        "sender": sender,
        "recipients": [r.strip() for r in recipients.split(",") if r.strip()],
    }


def setup_webhook(args):
    print()
    print("Works with Slack or Discord incoming webhooks, or any JSON endpoint.")
    print()
    url = prompt("Webhook URL")
    style = choose(
        [
            ("json", "generic JSON"),
            ("slack", "Slack"),
            ("discord", "Discord"),
            ("form", "form encoded"),
        ],
        "Format",
    )
    return "webhook", {"enabled": True, "url": url, "format": style}


SETUP_FLOWS = {
    "ntfy": setup_ntfy,
    "desktop": setup_desktop,
    "pushover": setup_pushover,
    "telegram": setup_telegram,
    "email": setup_email,
    "webhook": setup_webhook,
}


def cmd_notify_setup(args):
    from . import notify as notify_module

    path = notify_module.ensure_config()
    enabled = notify_module.enabled_channels()

    if args.show:
        print(f"Config: {path}")
        print()
        print(f"Enabled: {', '.join(enabled) if enabled else 'none'}")
        return

    print()
    print("Notification setup")
    print(f"  config file: {path}")
    if enabled:
        print(f"  already enabled: {', '.join(enabled)}")
    print()

    channel = args.channel
    if not channel:
        print("Which channel do you want to set up?")
        print()
        channel = choose(
            [
                ("ntfy", "ntfy      - free push to your phone, no account (recommended)"),
                ("desktop", "desktop   - notification on this machine only"),
                ("pushover", "pushover  - paid push, very reliable delivery"),
                ("telegram", "telegram  - free, via a bot"),
                ("email", "email     - SMTP"),
                ("webhook", "webhook   - Slack, Discord, or custom"),
            ]
        )

    flow = SETUP_FLOWS.get(channel)
    if flow is None:
        fail(f"unknown channel {channel!r}")

    name, values = flow(args)
    if name is None:
        fail("setup did not complete")

    missing = notify_module.apply_settings(name, values)
    if missing:
        print()
        print(f"warning: could not write {', '.join(missing)} into the config.")
        print(f"Edit {path} by hand for those.")

    print()
    print(f"Enabled {name} in {paths.relative(path)}")

    if args.no_test:
        print("Skipping the test send.")
        return

    print("Sending a test notification...")
    try:
        results = asyncio.run(notify_module.send_test(name))
    except ValueError as err:
        fail(str(err))

    print()
    outcome = results.get(name, "unknown")
    if outcome == "ok":
        print(f"  {name}: sent")
        print()
        print("Check your device. If nothing arrived, confirm you subscribed to")
        print(f"the exact topic, then run: {paths.command('notify-test')}")
    else:
        print(f"  {name}: {outcome}")
        print()
        print(f"Fix the settings in {path} and run: {paths.command('notify-test')}")
        sys.exit(1)


def cmd_notify_qr(args):
    from . import notify as notify_module

    config = notify_module.load_config()
    settings = config.get("ntfy") or {}
    topic = settings.get("topic")

    if not topic or "CHANGE-ME" in str(topic):
        fail(f"no ntfy topic configured. Run: {paths.command('notify-setup --channel ntfy')}")

    server = settings.get("server") or "https://ntfy.sh"
    url = notify_module.subscribe_url(topic, server)

    print()
    if args.store:
        target = (
            notify_module.NTFY_IOS_URL
            if args.store == "ios"
            else notify_module.NTFY_ANDROID_URL
        )
        notify_module.show_qr(
            target,
            f"Install the ntfy app ({args.store}):",
            dark_terminal=not args.light_terminal,
        )
        print()

    notify_module.show_qr(
        url,
        "Scan to subscribe on another device:",
        dark_terminal=not args.light_terminal,
    )
    print()
    print(f"  topic:  {topic}")
    print(f"  server: {server}")
    if not settings.get("enabled"):
        print()
        print("  note: ntfy is configured but not enabled in notifications.yaml")


def cmd_notify_test(args):
    try:
        results = asyncio.run(notify.send_test(args.channel, args.message))
    except ValueError as err:
        fail(str(err))

    print()
    failures = 0
    for name, outcome in sorted(results.items()):
        print(f"  {name:<10} {outcome}")
        if outcome != "ok":
            failures += 1
    print()
    if failures:
        print(f"{failures} channel(s) failed.")
        sys.exit(1)
    print("Sent. Check your device.")


def cmd_new_profile(args):
    try:
        destination = templates.create(
            args.name,
            source=args.source,
            target=args.target,
            channel=args.channel,
            template=args.template,
            force=args.force,
        )
    except FileExistsError as err:
        fail(f"{err} already exists. Use --force to overwrite.")

    print(f"Created {paths.relative(destination)}")
    print()
    print("Next:")
    print(f"  1. edit it and set source/target if the defaults were placeholders")
    print(f"  2. {paths.command(f'run {args.name} --test')}")


def cmd_profiles(args):
    paths.ensure_dirs()
    found = [
        path
        for path in list_profiles(paths.POWER_PROFILE_DIR)
        if path.name != "README.md"
    ]

    print()
    print(f"Power profiles ({len(found)}):")
    if not found:
        print(f"  none. Run: {paths.command('new-profile <name>')}")
        return

    for path in found:
        try:
            profile = PowerProfile(path)
        except Exception as err:
            print(f"  {path.name}  [invalid: {err}]")
            continue
        problems, _ = validate(profile)
        marker = "OK" if not problems else f"{len(problems)} problem(s)"
        state = "enabled" if profile.enabled else "disabled"
        print(f"  {path.name}")
        print(
            f"    {state}, {len(profile.active_rules())} rule(s), "
            f"poll {format_duration(profile.poll_interval)}, {marker}"
        )


def load_power_profile(reference):
    path = paths.resolve_profile(reference, "power")
    if path is None:
        fail(f"no power profile matching {reference!r}")
    try:
        return PowerProfile(path)
    except ProfileError as err:
        fail(f"{path.name}: {err}")
    except Exception as err:
        fail(f"{path.name}: {type(err).__name__}: {err}")


def cmd_validate(args):
    profile = load_power_profile(args.profile)
    problems, notes = validate(profile)

    print()
    print(f"{profile.path.name}")
    for note in notes:
        print(f"  note:    {note}")
    for problem in problems:
        print(f"  PROBLEM: {problem}")
    if not problems:
        print(f"  valid, {len(profile.active_rules())} active rule(s)")
    sys.exit(1 if problems else 0)


def parse_overrides(items):
    if not items:
        return {}
    overrides = {}
    for item in items:
        if "=" not in item:
            fail(f"--simulate expects FIELD=VALUE, got {item!r}")
        field, _, raw = item.partition("=")
        raw = raw.strip()
        try:
            value = int(raw)
        except ValueError:
            try:
                value = float(raw)
            except ValueError:
                lowered = raw.lower()
                if lowered in ("true", "false"):
                    value = lowered == "true"
                else:
                    value = raw
        overrides[field.strip()] = value
    return overrides


def cmd_run(args):
    profile = load_power_profile(args.profile)

    if args.test:
        overrides = parse_overrides(args.simulate)
        ok = asyncio.run(
            dry_run_report(
                profile,
                cycles=args.cycles,
                offline=args.offline,
                overrides=overrides,
            )
        )
        sys.exit(0 if ok else 1)

    problems, notes = validate(profile)
    for note in notes:
        print(f"note: {note}")
    if problems:
        for problem in problems:
            print(f"PROBLEM: {problem}", file=sys.stderr)
        fail("profile has problems. Run with --test for detail.")

    if not profile.enabled:
        fail(f"{profile.path.name} has enabled: false")

    paths.ensure_dirs()
    reporter = Reporter(log_path=paths.ENGINE_LOG, quiet=args.quiet)
    reporter(f"starting {profile.name}", force=True)

    engine = Engine(profile, dry_run=args.dry_run, reporter=reporter)

    try:
        asyncio.run(engine.run(cycles=args.cycles if args.cycles else None))
    except KeyboardInterrupt:
        reporter("stopped by user", force=True)
    except Exception as err:
        reporter(f"fatal: {type(err).__name__}: {err}", force=True)
        sys.exit(1)
    finally:
        reporter.close()


def build_parser():
    parser = argparse.ArgumentParser(
        prog="solixauto",
        description=(
            "Anker SOLIX telemetry to Shelly switch automation. "
            "Anker devices are read-only data sources; Shelly devices are the "
            "only things ever switched."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    setup_parser = subparsers.add_parser(
        "setup", help="guided end-to-end setup, start here"
    )
    setup_parser.set_defaults(func=cmd_setup)

    init_parser = subparsers.add_parser("init", help="create the directory layout")
    init_parser.set_defaults(func=cmd_init)

    doctor_parser = subparsers.add_parser(
        "doctor", help="check this machine is set up correctly"
    )
    doctor_parser.set_defaults(func=cmd_doctor)

    anker_parser = subparsers.add_parser(
        "discover-anker", help="connect to Anker cloud and save device profiles"
    )
    anker_parser.add_argument("--sn", help="only this serial number")
    anker_parser.add_argument("--pn", help="only this part number, e.g. A1782")
    anker_parser.add_argument(
        "--settle", type=int, default=45, help="seconds to wait for telemetry per device"
    )
    anker_parser.add_argument(
        "--skip",
        action="append",
        metavar="SERIAL",
        help="never try this serial, repeatable. Use for Bluetooth-only devices.",
    )
    anker_parser.add_argument(
        "--include-offline",
        action="store_true",
        help="try devices the cloud reports as not connected",
    )
    anker_parser.add_argument(
        "--explain-skipped",
        action="store_true",
        help="print the full reason when a device is skipped",
    )
    anker_parser.set_defaults(func=cmd_discover_anker)

    shelly_parser = subparsers.add_parser(
        "discover-shelly", help="find Shelly devices on the local network"
    )
    shelly_parser.add_argument(
        "--host", action="append", help="probe this address directly, repeatable"
    )
    shelly_parser.add_argument("--network", help="scan this CIDR, e.g. 192.168.1.0/24")
    shelly_parser.add_argument(
        "--no-mdns", action="store_true", help="skip mDNS discovery"
    )
    shelly_parser.set_defaults(func=cmd_discover_shelly)

    devices_parser = subparsers.add_parser("devices", help="list saved device profiles")
    devices_parser.set_defaults(func=cmd_devices)

    name_parser = subparsers.add_parser(
        "name", help="give a device profile a friendly name and rename its file"
    )
    name_parser.add_argument("device", help="current profile name, serial, MAC or IP")
    name_parser.add_argument("name", help="the friendly name to use")
    name_parser.add_argument(
        "--on-device",
        action="store_true",
        help="also write this name to the Shelly itself over local HTTP",
    )
    name_parser.set_defaults(func=cmd_name)

    status_parser = subparsers.add_parser(
        "status", help="read live telemetry from an Anker device"
    )
    status_parser.add_argument("device", help="Anker profile name or path")
    status_parser.add_argument(
        "--fields", nargs="+", help="only show fields containing these substrings"
    )
    status_parser.add_argument(
        "--watch", action="store_true", help="keep refreshing until Ctrl-C"
    )
    status_parser.add_argument("--interval", type=float, default=5.0)
    status_parser.add_argument("--settle", type=int, default=45)
    status_parser.set_defaults(func=cmd_status)

    monitor_parser = subparsers.add_parser(
        "monitor", help="live dashboard in your browser"
    )
    monitor_parser.add_argument("--port", type=int, default=8765)
    monitor_parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="use 0.0.0.0 to reach it from other devices on your network",
    )
    monitor_parser.add_argument(
        "--interval", type=float, default=5.0, help="seconds between samples"
    )
    monitor_parser.add_argument(
        "--no-open", action="store_true", help="do not open a browser"
    )
    monitor_parser.add_argument(
        "--allow-remote-edit",
        action="store_true",
        help="permit renaming devices even when reachable from other machines",
    )
    monitor_parser.set_defaults(func=cmd_monitor)

    service_parser = subparsers.add_parser(
        "service", help="run a power profile in the background at login"
    )
    service_parser.add_argument("profile")
    service_parser.add_argument(
        "--show", action="store_true", help="print the unit file without installing"
    )
    service_parser.add_argument(
        "--status", action="store_true", help="report whether it is installed/running"
    )
    service_parser.add_argument(
        "--uninstall", action="store_true", help="stop and remove the service"
    )
    service_parser.add_argument(
        "--yes", action="store_true", help="do not ask before installing"
    )
    service_parser.set_defaults(func=cmd_service)

    conflicts_parser = subparsers.add_parser(
        "conflicts", help="show schedules and timers configured on a Shelly itself"
    )
    conflicts_parser.add_argument("device", help="Shelly profile name or path")
    conflicts_parser.add_argument("--channel", type=int, default=None)
    conflicts_parser.add_argument(
        "--fix",
        action="store_true",
        help="offer to remove each conflict, asking before every change",
    )
    conflicts_parser.add_argument(
        "--yes",
        action="store_true",
        help="with --fix, apply every change without asking",
    )
    conflicts_parser.set_defaults(func=cmd_conflicts)

    switch_parser = subparsers.add_parser(
        "switch", help="manually control a Shelly, to verify wiring before automating"
    )
    switch_parser.add_argument("device", help="Shelly profile name or path")
    switch_parser.add_argument(
        "action", choices=["on", "off", "toggle", "status"], nargs="?", default="status"
    )
    switch_parser.add_argument("--channel", type=int, default=None)
    switch_parser.set_defaults(func=cmd_switch)

    fields_parser = subparsers.add_parser(
        "fields", help="list the field names usable in rules"
    )
    fields_parser.add_argument("device", help="Anker profile name or path")
    fields_parser.add_argument("--filter", help="only names containing this text")
    fields_parser.add_argument(
        "--writable", action="store_true", help="also show control methods"
    )
    fields_parser.set_defaults(func=cmd_fields)

    new_parser = subparsers.add_parser("new-profile", help="scaffold a power profile")
    new_parser.add_argument("name")
    new_parser.add_argument("--source", help="Anker profile filename")
    new_parser.add_argument("--target", help="Shelly profile filename")
    new_parser.add_argument("--channel", type=int, default=0)
    new_parser.add_argument(
        "--template",
        choices=sorted(templates.RULE_SETS),
        default="solar",
    )
    new_parser.add_argument("--force", action="store_true")
    new_parser.set_defaults(func=cmd_new_profile)

    notify_setup_parser = subparsers.add_parser(
        "notify-setup", help="interactively set up and test a notification channel"
    )
    notify_setup_parser.add_argument(
        "--channel",
        choices=["ntfy", "desktop", "pushover", "telegram", "email", "webhook"],
        help="skip the menu and set up this channel",
    )
    notify_setup_parser.add_argument(
        "--topic", help="use this ntfy topic instead of generating one"
    )
    notify_setup_parser.add_argument(
        "--no-test", action="store_true", help="do not send a test notification"
    )
    notify_setup_parser.add_argument(
        "--no-qr", action="store_true", help="do not draw QR codes"
    )
    notify_setup_parser.add_argument(
        "--light-terminal",
        action="store_true",
        help="flip QR colours for a light background terminal",
    )
    notify_setup_parser.add_argument(
        "--show", action="store_true", help="just print what is configured"
    )
    notify_setup_parser.set_defaults(func=cmd_notify_setup)

    notify_qr_parser = subparsers.add_parser(
        "notify-qr", help="show the QR code for your ntfy topic again"
    )
    notify_qr_parser.add_argument(
        "--store",
        choices=["ios", "android"],
        help="also show a QR for the app store",
    )
    notify_qr_parser.add_argument(
        "--light-terminal",
        action="store_true",
        help="flip QR colours for a light background terminal",
    )
    notify_qr_parser.set_defaults(func=cmd_notify_qr)

    notify_test_parser = subparsers.add_parser(
        "notify-test", help="send a test notification"
    )
    notify_test_parser.add_argument(
        "--channel", help="only this channel, e.g. ntfy"
    )
    notify_test_parser.add_argument("--message", help="custom test message")
    notify_test_parser.set_defaults(func=cmd_notify_test)

    profiles_parser = subparsers.add_parser("profiles", help="list power profiles")
    profiles_parser.set_defaults(func=cmd_profiles)

    validate_parser = subparsers.add_parser(
        "validate", help="check a power profile without connecting"
    )
    validate_parser.add_argument("profile")
    validate_parser.set_defaults(func=cmd_validate)

    run_parser = subparsers.add_parser("run", help="run a power profile")
    run_parser.add_argument("profile")
    run_parser.add_argument(
        "-t",
        "--test",
        action="store_true",
        help="validate and show what would happen, without switching anything",
    )
    run_parser.add_argument(
        "--offline",
        action="store_true",
        help="with --test, evaluate against saved samples instead of connecting",
    )
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="run the full loop but never send a switch command",
    )
    run_parser.add_argument(
        "--cycles", type=int, default=0, help="stop after N evaluations"
    )
    run_parser.add_argument("--quiet", action="store_true")
    run_parser.add_argument(
        "--simulate",
        nargs="+",
        metavar="FIELD=VALUE",
        help=(
            "with --test, override telemetry values to prove a rule fires "
            "without waiting for real conditions"
        ),
    )
    run_parser.set_defaults(func=cmd_run)

    return parser


def main():
    configure_event_loop()

    parser = build_parser()
    args = parser.parse_args()

    if args.command == "run" and args.test and not args.cycles:
        args.cycles = 3

    if args.command == "run" and getattr(args, "simulate", None) and not args.test:
        fail("--simulate only works together with --test")

    try:
        args.func(args)
    except KeyboardInterrupt:
        print()
        print("stopped")
    except ProfileError as err:
        fail(str(err))


if __name__ == "__main__":
    main()
