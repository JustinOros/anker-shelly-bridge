import os
from pathlib import Path

from . import paths


def load_credentials():
    candidates = []
    explicit = os.environ.get("SOLIXAUTO_ENV")
    if explicit:
        candidates.append(Path(explicit).expanduser())
    candidates.append(Path.cwd() / ".env")
    candidates.append(paths.BASE_DIR / ".env")
    candidates.append(Path.home() / "anker-solix-mqtt" / "anker-solix-api" / ".env")
    candidates.append(Path.home() / "anker-solix-api" / ".env")

    for candidate in candidates:
        if candidate.exists():
            _load_env_file(candidate)
            break

    user = os.environ.get("ANKERUSER")
    password = os.environ.get("ANKERPASSWORD")
    country = os.environ.get("ANKERCOUNTRY", "US")

    if not user or not password:
        raise RuntimeError(
            "ANKERUSER / ANKERPASSWORD not found. Set them in the environment, "
            "or place a .env file in the current directory, in "
            f"{paths.BASE_DIR}, or point SOLIXAUTO_ENV at one."
        )
    return user, password, country


def _load_env_file(path):
    try:
        from dotenv import load_dotenv

        load_dotenv(path)
        return
    except ImportError:
        pass

    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)
