import asyncio
import json
import secrets
import shutil
import smtplib
import sys
import string
import subprocess
import time
from email.message import EmailMessage
from pathlib import Path

import aiohttp

from . import paths
from .profiles import load_yaml, write_text

CONFIG_PATH = paths.BASE_DIR / "notifications.yaml"
SEND_TIMEOUT = 15

CONFIG_TEMPLATE = """# Notification channels for solixauto.
#
# Secrets live here, NOT in your power profiles, so profiles stay safe to
# share or commit. This file is created with owner-only permissions.
#
# Enable a channel by setting enabled: true and filling in its settings.
# Test with:
#     solixauto notify-test
#     solixauto notify-test --channel ntfy
#
# ---------------------------------------------------------------------
# ntfy - recommended. Free, no account needed.
#   1. install the ntfy app on your phone
#   2. subscribe to a topic name that nobody else would guess
#   3. put that topic below
# Anyone who knows the topic name can read your alerts, so make it long.
# ---------------------------------------------------------------------
ntfy:
  enabled: false
  server: https://ntfy.sh
  topic: solix-CHANGE-ME-to-something-random
  priority: default
  token: ""

# ---------------------------------------------------------------------
# Pushover - $5 one time per platform. Very reliable delivery.
# Get both keys from https://pushover.net
# ---------------------------------------------------------------------
pushover:
  enabled: false
  user_key: ""
  api_token: ""
  priority: 0
  sound: ""

# ---------------------------------------------------------------------
# Email over SMTP.
# For Gmail you must use an App Password, not your normal password.
# ---------------------------------------------------------------------
email:
  enabled: false
  host: smtp.gmail.com
  port: 587
  use_tls: true
  username: ""
  password: ""
  sender: ""
  recipients: []

# ---------------------------------------------------------------------
# Telegram - free. Create a bot with @BotFather, then message it once and
# read your chat id from https://api.telegram.org/bot<TOKEN>/getUpdates
# ---------------------------------------------------------------------
telegram:
  enabled: false
  bot_token: ""
  chat_id: ""

# ---------------------------------------------------------------------
# Generic webhook. Works with Slack and Discord incoming webhooks.
# format: slack | discord | json | form
# ---------------------------------------------------------------------
webhook:
  enabled: false
  url: ""
  format: json
  method: POST

# ---------------------------------------------------------------------
# Desktop notification on the machine running the engine.
# Useful while testing. Does not reach your phone.
#
#   macOS    uses osascript, built in
#   Linux    uses notify-send, from libnotify-bin
#   Windows  uses PowerShell, built in
#
# sound is macOS only and ignored elsewhere.
# ---------------------------------------------------------------------
desktop:
  enabled: false
  sound: Submarine
"""


class SafeDict(dict):
    def __missing__(self, key):
        return "?"


class TemplateFormatter(string.Formatter):
    def get_value(self, key, args, kwargs):
        if isinstance(key, str):
            return kwargs.get(key, "?")
        return "?"

    def format_field(self, value, format_spec):
        try:
            return super().format_field(value, format_spec)
        except (TypeError, ValueError):
            return str(value)


FORMATTER = TemplateFormatter()


def render(template, context):
    try:
        return FORMATTER.vformat(str(template), (), SafeDict(context))
    except Exception:
        return str(template)


def template_fields(template):
    found = set()
    try:
        for _, field, _, _ in string.Formatter().parse(str(template)):
            if field:
                found.add(field.split(".")[0].split("[")[0])
    except ValueError:
        pass
    return found


def ensure_config():
    paths.ensure_dirs()
    if not CONFIG_PATH.exists():
        write_text(CONFIG_PATH, CONFIG_TEMPLATE)
        paths.secure_file(CONFIG_PATH)
    return CONFIG_PATH


def set_value(text, channel, key, value):
    lines = text.splitlines()
    output = []
    inside = False
    replaced = False

    if isinstance(value, bool):
        rendered = "true" if value else "false"
    elif isinstance(value, list):
        rendered = "[" + ", ".join(str(v) for v in value) + "]"
    elif value == "":
        rendered = '""'
    else:
        rendered = str(value)

    for line in lines:
        stripped = line.strip()

        if not line.startswith((" ", "\t")) and stripped.endswith(":"):
            if inside and not replaced:
                pass
            inside = stripped[:-1] == channel

        if inside and not replaced:
            without_indent = line.lstrip()
            if without_indent.startswith(f"{key}:"):
                indent = line[: len(line) - len(without_indent)]
                output.append(f"{indent}{key}: {rendered}")
                replaced = True
                continue

        output.append(line)

    return "\n".join(output) + "\n", replaced


def apply_settings(channel, values):
    ensure_config()
    text = CONFIG_PATH.read_text(encoding="utf-8")

    missing = []
    for key, value in values.items():
        text, replaced = set_value(text, channel, key, value)
        if not replaced:
            missing.append(key)

    CONFIG_PATH.write_text(text, encoding="utf-8")
    paths.secure_file(CONFIG_PATH)
    return missing


def generate_topic(prefix="solix"):
    return f"{prefix}-{secrets.token_hex(10)}"


NTFY_IOS_URL = "https://apps.apple.com/us/app/ntfy/id1625396347"
NTFY_ANDROID_URL = "https://play.google.com/store/apps/details?id=io.heckel.ntfy"
NTFY_FDROID_URL = "https://f-droid.org/en/packages/io.heckel.ntfy/"
NTFY_DOCS_URL = "https://docs.ntfy.sh/subscribe/phone/"


def subscribe_url(topic, server="https://ntfy.sh"):
    return f"{server.rstrip('/')}/{topic}"


def _can_encode(sample):
    encoding = getattr(sys.stdout, "encoding", None) or ""
    if not encoding:
        return False
    try:
        sample.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


def render_qr(data, border=2, dark_terminal=True):
    try:
        import qrcode
    except ImportError:
        return None, "qrcode package not installed"

    try:
        code = qrcode.QRCode(border=border)
        code.add_data(data)
        code.make(fit=True)
        matrix = code.get_matrix()
    except Exception as err:
        return None, f"{type(err).__name__}: {err}"

    if not _can_encode("\u2588\u2580\u2584"):
        return None, "this console cannot render block characters"

    def ink(value):
        return (not value) if dark_terminal else value

    lines = []
    for index in range(0, len(matrix), 2):
        top = matrix[index]
        bottom = matrix[index + 1] if index + 1 < len(matrix) else [False] * len(top)
        row = []
        for upper, lower in zip(top, bottom):
            upper_on = ink(upper)
            lower_on = ink(lower)
            if upper_on and lower_on:
                row.append("\u2588")
            elif upper_on:
                row.append("\u2580")
            elif lower_on:
                row.append("\u2584")
            else:
                row.append(" ")
        lines.append("".join(row))

    if dark_terminal:
        width = len(lines[0]) if lines else 0
        pad = "\u2588" * width
        lines = [pad] + lines + [pad]

    return "\n".join(lines), None


def show_qr(data, label=None, indent="  ", dark_terminal=True):
    art, problem = render_qr(data, dark_terminal=dark_terminal)

    if label:
        print(f"{indent}{label}")
        print()

    if art is None:
        print(f"{indent}[QR unavailable: {problem}]")
        print(f"{indent}Open this link on your phone instead:")
        print(f"{indent}{data}")
        return False

    try:
        for line in art.splitlines():
            print(f"{indent}{line}")
    except UnicodeEncodeError:
        print(f"{indent}[QR unavailable: console encoding]")
        print(f"{indent}{data}")
        return False

    print()
    print(f"{indent}{data}")
    return True


def load_config():
    if not CONFIG_PATH.exists():
        return {}
    try:
        return load_yaml(CONFIG_PATH)
    except Exception:
        return {}


def enabled_channels(config=None):
    config = config if config is not None else load_config()
    return sorted(
        name
        for name, settings in config.items()
        if isinstance(settings, dict) and settings.get("enabled")
    )


async def _send_ntfy(session, settings, title, body, priority):
    server = str(settings.get("server") or "https://ntfy.sh").rstrip("/")
    topic = settings.get("topic")
    if not topic:
        raise ValueError("ntfy.topic is not set")

    headers = {"Title": title}
    level = priority or settings.get("priority")
    if level:
        headers["Priority"] = str(level)
    token = settings.get("token")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    async with session.post(
        f"{server}/{topic}",
        data=body.encode("utf-8"),
        headers=headers,
        timeout=aiohttp.ClientTimeout(total=SEND_TIMEOUT),
    ) as response:
        if response.status >= 300:
            raise RuntimeError(f"HTTP {response.status}")


async def _send_pushover(session, settings, title, body, priority):
    user_key = settings.get("user_key")
    api_token = settings.get("api_token")
    if not user_key or not api_token:
        raise ValueError("pushover.user_key and pushover.api_token are required")

    payload = {
        "token": api_token,
        "user": user_key,
        "title": title,
        "message": body,
        "priority": int(priority if priority is not None else settings.get("priority", 0)),
    }
    if settings.get("sound"):
        payload["sound"] = settings["sound"]
    if payload["priority"] == 2:
        payload["retry"] = 60
        payload["expire"] = 3600

    async with session.post(
        "https://api.pushover.net/1/messages.json",
        data=payload,
        timeout=aiohttp.ClientTimeout(total=SEND_TIMEOUT),
    ) as response:
        if response.status >= 300:
            raise RuntimeError(f"HTTP {response.status}: {await response.text()}")


async def _send_telegram(session, settings, title, body, priority):
    token = settings.get("bot_token")
    chat_id = settings.get("chat_id")
    if not token or not chat_id:
        raise ValueError("telegram.bot_token and telegram.chat_id are required")

    async with session.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": str(chat_id), "text": f"{title}\n{body}"},
        timeout=aiohttp.ClientTimeout(total=SEND_TIMEOUT),
    ) as response:
        if response.status >= 300:
            raise RuntimeError(f"HTTP {response.status}: {await response.text()}")


async def _send_webhook(session, settings, title, body, priority):
    url = settings.get("url")
    if not url:
        raise ValueError("webhook.url is not set")

    style = str(settings.get("format") or "json").lower()
    method = str(settings.get("method") or "POST").upper()

    kwargs = {"timeout": aiohttp.ClientTimeout(total=SEND_TIMEOUT)}
    if style == "slack":
        kwargs["json"] = {"text": f"*{title}*\n{body}"}
    elif style == "discord":
        kwargs["json"] = {"content": f"**{title}**\n{body}"}
    elif style == "form":
        kwargs["data"] = {"title": title, "message": body}
    else:
        kwargs["json"] = {"title": title, "message": body, "priority": priority}

    async with session.request(method, url, **kwargs) as response:
        if response.status >= 300:
            raise RuntimeError(f"HTTP {response.status}")


def _send_email_blocking(settings, title, body):
    recipients = settings.get("recipients") or []
    if isinstance(recipients, str):
        recipients = [recipients]
    sender = settings.get("sender") or settings.get("username")

    if not recipients or not sender:
        raise ValueError("email.sender and email.recipients are required")

    message = EmailMessage()
    message["Subject"] = title
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message.set_content(body)

    host = settings.get("host") or "localhost"
    port = int(settings.get("port") or 587)

    if int(port) == 465:
        server = smtplib.SMTP_SSL(host, port, timeout=SEND_TIMEOUT)
    else:
        server = smtplib.SMTP(host, port, timeout=SEND_TIMEOUT)

    try:
        if int(port) != 465 and settings.get("use_tls", True):
            server.starttls()
        if settings.get("username") and settings.get("password"):
            server.login(settings["username"], settings["password"])
        server.send_message(message)
    finally:
        try:
            server.quit()
        except Exception:
            pass


async def _send_email(session, settings, title, body, priority):
    await asyncio.get_running_loop().run_in_executor(
        None, _send_email_blocking, settings, title, body
    )


def desktop_backend():
    if sys.platform == "darwin":
        return "osascript" if shutil.which("osascript") else None
    if sys.platform.startswith("win"):
        for candidate in ("powershell", "pwsh"):
            if shutil.which(candidate):
                return candidate
        return None
    for candidate in ("notify-send", "kdialog", "zenity"):
        if shutil.which(candidate):
            return candidate
    return None


def _desktop_macos(settings, title, body):
    escaped_body = body.replace("\\", "\\\\").replace('"', '\\"')
    escaped_title = title.replace("\\", "\\\\").replace('"', '\\"')
    script = f'display notification "{escaped_body}" with title "{escaped_title}"'
    if settings.get("sound"):
        sound = str(settings["sound"]).replace('"', "")
        script += f' sound name "{sound}"'
    return ["osascript", "-e", script]


def _desktop_windows(executable, title, body):
    safe_title = title.replace("'", "''")
    safe_body = body.replace("'", "''")
    script = (
        "[reflection.assembly]::LoadWithPartialName('System.Windows.Forms') | Out-Null; "
        "[reflection.assembly]::LoadWithPartialName('System.Drawing') | Out-Null; "
        "$n = New-Object System.Windows.Forms.NotifyIcon; "
        "$n.Icon = [System.Drawing.SystemIcons]::Information; "
        "$n.Visible = $true; "
        f"$n.ShowBalloonTip(10000, '{safe_title}', '{safe_body}', "
        "[System.Windows.Forms.ToolTipIcon]::Info); "
        "Start-Sleep -Seconds 6; "
        "$n.Dispose()"
    )
    return [
        executable,
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        script,
    ]


def _desktop_linux(backend, title, body):
    if backend == "notify-send":
        return ["notify-send", title, body]
    if backend == "kdialog":
        return ["kdialog", "--title", title, "--passivepopup", body, "10"]
    return ["zenity", "--notification", "--text", f"{title}\n{body}"]


def _send_desktop_blocking(settings, title, body):
    backend = desktop_backend()

    if backend is None:
        if sys.platform.startswith("linux"):
            raise RuntimeError(
                "no desktop notifier found. Install libnotify-bin "
                "(apt install libnotify-bin) or use a push channel instead."
            )
        raise RuntimeError(f"no desktop notifier available on {sys.platform}")

    if backend == "osascript":
        command = _desktop_macos(settings, title, body)
    elif backend in ("powershell", "pwsh"):
        command = _desktop_windows(backend, title, body)
    else:
        command = _desktop_linux(backend, title, body)

    result = subprocess.run(
        command, capture_output=True, text=True, timeout=SEND_TIMEOUT
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"{backend} failed")


async def _send_desktop(session, settings, title, body, priority):
    await asyncio.get_running_loop().run_in_executor(
        None, _send_desktop_blocking, settings, title, body
    )


SENDERS = {
    "ntfy": _send_ntfy,
    "pushover": _send_pushover,
    "telegram": _send_telegram,
    "webhook": _send_webhook,
    "email": _send_email,
    "desktop": _send_desktop,
    "macos": _send_desktop,
}


class Notifier:
    def __init__(self, settings, reporter=None, dry_run=False):
        self.settings = settings
        self.reporter = reporter or (lambda message: None)
        self.dry_run = dry_run
        self.config = load_config()
        self._last_sent = {}
        self._last_message = {}

    def available(self):
        configured = enabled_channels(self.config)
        wanted = self.settings.channels
        if not wanted:
            return configured
        return [name for name in wanted if name in configured]

    def missing(self):
        configured = set(enabled_channels(self.config))
        return [name for name in (self.settings.channels or []) if name not in configured]

    def throttled(self, key, message, now):
        window = self.settings.throttle or 0
        last_at = self._last_sent.get(key)

        if self._last_message.get(key) == message and last_at and window:
            if now - last_at < window:
                return f"identical message within {int(window)}s"

        if last_at and window and now - last_at < window:
            return f"throttled, {int(window - (now - last_at))}s remaining"

        return None

    async def send(self, title, body, priority=None, key="default", force=False):
        channels = self.available()
        if not channels:
            return False

        now = time.monotonic()
        if not force:
            blocked = self.throttled(key, body, now)
            if blocked:
                self.reporter(f"notification suppressed: {blocked}")
                return False

        if self.dry_run:
            self.reporter(f"DRY RUN would notify via {', '.join(channels)}: {body}")
            self._last_sent[key] = now
            self._last_message[key] = body
            return True

        sent = 0
        async with aiohttp.ClientSession() as session:
            for name in channels:
                sender = SENDERS.get(name)
                if sender is None:
                    continue
                try:
                    await sender(session, self.config.get(name) or {}, title, body, priority)
                    sent += 1
                except Exception as err:
                    self.reporter(
                        f"notification via {name} failed: {type(err).__name__}: {err}"
                    )

        if sent:
            self._last_sent[key] = now
            self._last_message[key] = body
            self.reporter(f"notified via {sent} channel(s)")

        return sent > 0


async def send_test(channel=None, message=None):
    ensure_config()
    config = load_config()
    available = enabled_channels(config)

    if channel:
        if channel not in SENDERS:
            raise ValueError(f"unknown channel {channel!r}. Known: {sorted(SENDERS)}")
        if channel not in available:
            raise ValueError(
                f"channel {channel!r} is not enabled in {paths.relative(CONFIG_PATH)}"
            )
        available = [channel]

    if not available:
        raise ValueError(
            f"no channels enabled. Edit {paths.relative(CONFIG_PATH)} and set "
            "enabled: true on at least one."
        )

    title = "solixauto test"
    body = message or "Test notification. If you can read this, the channel works."

    results = {}
    async with aiohttp.ClientSession() as session:
        for name in available:
            try:
                await SENDERS[name](session, config.get(name) or {}, title, body, None)
                results[name] = "ok"
            except Exception as err:
                results[name] = f"{type(err).__name__}: {err}"

    return results
