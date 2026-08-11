import os
import subprocess
import sys
from pathlib import Path

from . import paths

LAUNCHD_DIR = Path.home() / "Library" / "LaunchAgents"
SYSTEMD_DIR = Path.home() / ".config" / "systemd" / "user"


def platform_kind():
    if sys.platform == "darwin":
        return "launchd"
    if sys.platform.startswith("linux"):
        return "systemd"
    if sys.platform.startswith("win"):
        return "windows"
    return "unknown"


def service_label(profile_name):
    return f"com.solixauto.{profile_name}"


def script_path():
    if sys.argv and sys.argv[0]:
        return str(Path(sys.argv[0]).resolve())
    return "solixauto.py"


def env_file_in_use():
    explicit = os.environ.get("SOLIXAUTO_ENV")
    if explicit and Path(explicit).expanduser().exists():
        return str(Path(explicit).expanduser())

    candidates = [
        Path.cwd() / ".env",
        paths.BASE_DIR / ".env",
        Path.home() / "anker-solix-mqtt" / "anker-solix-api" / ".env",
        Path.home() / "anker-solix-api" / ".env",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


def environment():
    values = {}
    env_file = env_file_in_use()
    if env_file:
        values["SOLIXAUTO_ENV"] = env_file
    if os.environ.get("SOLIXAUTO_HOME"):
        values["SOLIXAUTO_HOME"] = os.environ["SOLIXAUTO_HOME"]
    return values


def launchd_plist(profile_name):
    label = service_label(profile_name)
    script = script_path()
    log_dir = paths.LOG_DIR

    env_entries = ""
    for key, value in environment().items():
        env_entries += f"        <key>{key}</key>\n        <string>{value}</string>\n"

    env_block = ""
    if env_entries:
        env_block = (
            "    <key>EnvironmentVariables</key>\n"
            "    <dict>\n"
            f"{env_entries}"
            "    </dict>\n"
        )

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key>
    <string>{label}</string>

    <key>ProgramArguments</key>
    <array>
      <string>{sys.executable}</string>
      <string>{script}</string>
      <string>run</string>
      <string>{profile_name}</string>
      <string>--quiet</string>
    </array>

    <key>WorkingDirectory</key>
    <string>{Path(script).parent}</string>

{env_block}    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <true/>

    <key>ThrottleInterval</key>
    <integer>60</integer>

    <key>StandardOutPath</key>
    <string>{log_dir / f"{profile_name}.out.log"}</string>

    <key>StandardErrorPath</key>
    <string>{log_dir / f"{profile_name}.err.log"}</string>
  </dict>
</plist>
"""


def systemd_unit(profile_name):
    script = script_path()
    env_lines = "".join(
        f"Environment={key}={value}\n" for key, value in environment().items()
    )

    return f"""[Unit]
Description=solixauto {profile_name}
After=network-online.target

[Service]
Type=simple
ExecStart={sys.executable} {script} run {profile_name} --quiet
WorkingDirectory={Path(script).parent}
{env_lines}Restart=always
RestartSec=60

[Install]
WantedBy=default.target
"""


def windows_instructions(profile_name):
    script = script_path()
    label = f"solixauto-{profile_name}"
    return f"""Windows does not have a per-user service manager as simple as the
others. Use Task Scheduler:

  schtasks /create /tn "{label}" /sc onlogon /rl highest ^
    /tr "\\"{sys.executable}\\" \\"{script}\\" run {profile_name} --quiet"

To remove it:

  schtasks /delete /tn "{label}" /f

Task Scheduler will not restart the task if it crashes. For that, set
the task's "If the task fails, restart every" option in the GUI, under
task properties, Settings.
"""


def unit_path(profile_name):
    kind = platform_kind()
    if kind == "launchd":
        return LAUNCHD_DIR / f"{service_label(profile_name)}.plist"
    if kind == "systemd":
        return SYSTEMD_DIR / f"solixauto-{profile_name}.service"
    return None


def render(profile_name):
    kind = platform_kind()
    if kind == "launchd":
        return launchd_plist(profile_name)
    if kind == "systemd":
        return systemd_unit(profile_name)
    if kind == "windows":
        return windows_instructions(profile_name)
    return None


def run_command(command):
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    except Exception as err:
        return False, f"{type(err).__name__}: {err}"
    output = (result.stdout + result.stderr).strip()
    return result.returncode == 0, output


def install(profile_name):
    kind = platform_kind()
    destination = unit_path(profile_name)

    if destination is None:
        return False, "no service manager available on this platform"

    destination.parent.mkdir(parents=True, exist_ok=True)
    paths.LOG_DIR.mkdir(parents=True, exist_ok=True)
    destination.write_text(render(profile_name), encoding="utf-8")

    if kind == "launchd":
        label = service_label(profile_name)
        run_command(["launchctl", "unload", str(destination)])
        ok, output = run_command(["launchctl", "load", "-w", str(destination)])
        return ok, output or f"loaded {label}"

    ok, output = run_command(["systemctl", "--user", "daemon-reload"])
    if not ok:
        return False, output
    ok, output = run_command(
        ["systemctl", "--user", "enable", "--now", destination.name]
    )
    return ok, output or f"enabled {destination.name}"


def uninstall(profile_name):
    kind = platform_kind()
    destination = unit_path(profile_name)

    if destination is None:
        return False, "no service manager available on this platform"

    if kind == "launchd":
        ok, output = run_command(["launchctl", "unload", "-w", str(destination)])
    else:
        run_command(["systemctl", "--user", "disable", "--now", destination.name])
        ok, output = run_command(["systemctl", "--user", "daemon-reload"])

    if destination.exists():
        destination.unlink()

    return True, output or "removed"


def status(profile_name):
    kind = platform_kind()
    destination = unit_path(profile_name)

    if destination is None or not destination.exists():
        return False, "not installed"

    if kind == "launchd":
        ok, output = run_command(["launchctl", "list"])
        label = service_label(profile_name)
        for line in output.splitlines():
            if label in line:
                fields = line.split()
                pid = fields[0]
                if pid != "-":
                    return True, f"running, pid {pid}"
                return True, f"loaded but not running (last exit {fields[1]})"
        return False, "installed but not loaded"

    ok, output = run_command(
        ["systemctl", "--user", "is-active", destination.name]
    )
    return ok, output or "unknown"
