from datetime import datetime, timezone
from pathlib import Path

import yaml


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_yaml(path):
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"profile not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if data is None:
        raise ValueError(f"profile is empty: {path}")
    if not isinstance(data, dict):
        raise ValueError(f"profile must be a mapping at top level: {path}")
    return data


def save_yaml(path, data, header=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_dump(data, sort_keys=False, default_flow_style=False, width=100)
    with path.open("w", encoding="utf-8") as handle:
        if header:
            for line in header.strip().splitlines():
                handle.write(f"# {line}\n" if line.strip() else "#\n")
            handle.write("\n")
        handle.write(body)
    return path


def write_text(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def list_profiles(directory):
    directory = Path(directory)
    if not directory.exists():
        return []
    return sorted(
        [p for p in directory.iterdir() if p.suffix in (".yaml", ".yml")],
        key=lambda p: p.name,
    )


def slugify(value):
    cleaned = []
    for char in str(value):
        if char.isalnum() or char in "-_":
            cleaned.append(char)
        elif char in " .:/\\":
            cleaned.append("-")
    slug = "".join(cleaned).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug or "device"


def classify(value):
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if value is None:
        return "null"
    return "str"
