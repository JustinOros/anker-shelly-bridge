import os
import stat
import sys
from pathlib import Path

_INVOCATION = None

BASE_DIR = Path(os.environ.get("SOLIXAUTO_HOME", Path.home() / "solix-automation"))

DEVICE_PROFILE_DIR = BASE_DIR / "device-profiles"
ANKER_PROFILE_DIR = DEVICE_PROFILE_DIR / "anker"
SHELLY_PROFILE_DIR = DEVICE_PROFILE_DIR / "shelly"
POWER_PROFILE_DIR = BASE_DIR / "power-profiles"
STATE_DIR = BASE_DIR / "state"
LOG_DIR = BASE_DIR / "logs"
TELEMETRY_DIR = STATE_DIR / "telemetry"

RUNTIME_STATE = STATE_DIR / "runtime.json"
ENGINE_LOG = LOG_DIR / "automation.log"

ALL_DIRS = [
    BASE_DIR,
    DEVICE_PROFILE_DIR,
    ANKER_PROFILE_DIR,
    SHELLY_PROFILE_DIR,
    POWER_PROFILE_DIR,
    STATE_DIR,
    LOG_DIR,
    TELEMETRY_DIR,
]


def ensure_dirs():
    for directory in ALL_DIRS:
        directory.mkdir(parents=True, exist_ok=True)
    gitignore = BASE_DIR / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(
            "device-profiles/\nstate/\nlogs/\n*.env\n.env\n",
            encoding="utf-8",
        )
    return BASE_DIR


def invocation():
    global _INVOCATION
    if _INVOCATION is not None:
        return _INVOCATION

    script = sys.argv[0] if sys.argv else ""
    stem = Path(script).name if script else ""

    if stem in ("solixauto", "solixauto.exe"):
        _INVOCATION = "solixauto"
        return _INVOCATION

    if not script:
        _INVOCATION = "solixauto"
        return _INVOCATION

    interpreter = sys.executable or "python"
    display = script

    try:
        here = Path(script).resolve()
        if here.parent == Path.cwd():
            display = here.name
    except (OSError, ValueError):
        pass

    _INVOCATION = f"{interpreter} {display}"
    return _INVOCATION


def command(rest=""):
    base = invocation()
    return f"{base} {rest}".rstrip()


def secure_file(path):
    path = Path(path)
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        return True
    except (OSError, NotImplementedError):
        return False


def permissions_enforced():
    return not sys.platform.startswith("win")


def relative(path):
    path = Path(path)
    try:
        return str(path.relative_to(BASE_DIR))
    except ValueError:
        return str(path)


def resolve_profile(reference, kind):
    reference = str(reference).strip()
    candidate = Path(reference).expanduser()

    if candidate.is_absolute() and candidate.exists():
        return candidate

    roots = [BASE_DIR, DEVICE_PROFILE_DIR]
    if kind == "anker":
        roots.insert(0, ANKER_PROFILE_DIR)
    elif kind == "shelly":
        roots.insert(0, SHELLY_PROFILE_DIR)
    elif kind == "power":
        roots.insert(0, POWER_PROFILE_DIR)

    names = [reference]
    if not reference.endswith((".yaml", ".yml")):
        names.append(reference + ".yaml")
        names.append(reference + ".yml")

    for root in roots:
        for name in names:
            probe = root / name
            if probe.exists():
                return probe

    return resolve_by_alias(reference, kind)


def _slug(value):
    text = str(value)
    for suffix in (".yaml", ".yml"):
        if text.lower().endswith(suffix):
            text = text[: -len(suffix)]
            break
    return "".join(
        char.lower() if char.isalnum() else "-" for char in text
    ).strip("-")


def resolve_by_alias(reference, kind):
    import yaml

    directories = []
    if kind == "anker":
        directories.append(ANKER_PROFILE_DIR)
    elif kind == "shelly":
        directories.append(SHELLY_PROFILE_DIR)
    elif kind == "power":
        directories.append(POWER_PROFILE_DIR)
    else:
        directories.extend([ANKER_PROFILE_DIR, SHELLY_PROFILE_DIR, POWER_PROFILE_DIR])

    wanted = _slug(reference)
    if not wanted:
        return None

    for directory in directories:
        if not directory.exists():
            continue
        for candidate in sorted(directory.iterdir()):
            if candidate.suffix not in (".yaml", ".yml"):
                continue
            try:
                with candidate.open("r", encoding="utf-8") as handle:
                    data = yaml.safe_load(handle)
            except Exception:
                continue
            if not isinstance(data, dict):
                continue

            haystack = list(data.get("aliases") or [])
            identity = data.get("identity") or {}
            for key in ("name", "id", "mac", "serial", "part_number"):
                if identity.get(key):
                    haystack.append(identity[key])
            access = data.get("access") or {}
            if access.get("host"):
                haystack.append(access["host"])
            if data.get("name"):
                haystack.append(data["name"])

            for entry in haystack:
                if _slug(entry) == wanted:
                    return candidate

    return None
