"""Pure text parsing for commands and wizard answers (no Telegram imports)."""
from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from datetime import datetime
from datetime import time as dtime

from telegram_manager.constants import (
    AUTOCLICK_MAX_BUTTONS,
    BULK_DELAY_MAX,
    BULK_DELAY_MIN,
    MAX_TIMES_PER_DAY,
    WHEN_MAX_MINUTES,
)

_USERNAME = re.compile(r"^@?[A-Za-z][A-Za-z0-9_]{4,31}$")
_CHAT_ID = re.compile(r"^-?\d{5,20}$")
_PHONE = re.compile(r"^\+\d{7,15}$")
_TAIL_CONFIRM = re.compile(r"(?:^|\s)(y|yes|n|no)\s*$", re.I)
_TAIL_DELAY = re.compile(r"(?:^|\s)(\d{1,4})(?:s|sec|secs)\s*$", re.I)
_TAIL_BUTTON = re.compile(r"(?:^|\s)(?:\[([^\[\]]{1,40})\]|\(([^()]{1,40})\))\s*$")
_ANY_BUTTON = re.compile(r"\[([^\[\]]{1,40})\]|\(([^()]{1,40})\)")


def mask_phone(phone: str) -> str:
    """+919876543210 -> +91••••••3210: enough to tell accounts apart without showing the number."""
    digits = re.sub(r"\D", "", phone or "")
    if not digits:
        return ""
    if len(digits) < 8:
        return "•" * (len(digits) - 2) + digits[-2:]
    return f"+{digits[:2]}{'•' * (len(digits) - 6)}{digits[-4:]}"


def looks_like_telethon_session(value: str) -> bool:
    """Rough shape check before spending a Telegram request on it: a Telethon StringSession is
    base64 and reasonably long. This cannot prove the session is valid or even that it is a
    Telethon session (only Telegram's own answer can) — it only catches obvious paste mistakes
    (an API hash, a phone number, an empty string) so the auto-detect step can fail fast."""
    value = (value or "").strip()
    if len(value) < 50 or any(ch.isspace() for ch in value):
        return False
    try:
        base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except Exception:
        return False
    return True


def parse_target(text: str) -> str:
    """@username, numeric chat id or +phone. Returns it normalised."""
    value = (text or "").strip()
    if _USERNAME.match(value):
        return "@" + value.lstrip("@")
    if _CHAT_ID.match(value) or _PHONE.match(value):
        return value
    raise ValueError("Send a @username (5-32 letters, digits or _), a chat id, or a +phone number.")


def parse_int(text: str, label: str) -> int:
    value = (text or "").strip()
    if not value.isdigit() or int(value) <= 0:
        raise ValueError(f"{label} must be a positive number.")
    return int(value)


def parse_delay(text: str, minimum: int = BULK_DELAY_MIN, maximum: int = BULK_DELAY_MAX) -> int:
    match = re.fullmatch(r"\s*(\d{1,4})\s*(?:s|sec|secs|seconds?)?\s*", text or "", re.I)
    if not match:
        raise ValueError("Send the delay as a whole number of seconds, for example 3.")
    value = int(match.group(1))
    if not minimum <= value <= maximum:
        raise ValueError(f"Delay must be between {minimum} and {maximum} seconds.")
    return value


def parse_minutes(text: str, maximum: int = WHEN_MAX_MINUTES) -> int:
    """\"now\", \"0\", \"15\", \"15m\", \"2h\" -> minutes to wait before sending (0 = now)."""
    value = (text or "").strip().lower()
    if value in ("now", "0"):
        return 0
    match = re.fullmatch(r"(\d{1,4})\s*(m|min|mins|minutes?|h|hr|hrs|hours?)?", value)
    if not match:
        raise ValueError("Send how long to wait, like 10 (minutes), 30m or 2h. Send 0 for now.")
    minutes = int(match.group(1)) * (60 if (match.group(2) or "m").startswith("h") else 1)
    if minutes > maximum:
        raise ValueError(f"The longest wait is {maximum // 60} hours.")
    return minutes


def parse_time(text: str) -> dtime:
    cleaned = (text or "").strip().replace(" ", "").upper()
    for fmt in ("%I:%M%p", "%I%p", "%H:%M"):
        try:
            return datetime.strptime(cleaned, fmt).time()
        except ValueError:
            continue
    raise ValueError("Use a time like 10:30am or 22:15.")


def parse_count(text: str, maximum: int = MAX_TIMES_PER_DAY) -> int:
    value = (text or "").strip()
    if not value.isdigit() or not 1 <= int(value) <= maximum:
        raise ValueError(f"Send a number between 1 and {maximum}.")
    return int(value)


def parse_button_names(raw: str) -> list[str]:
    """Button names to click: "[follow] (join)", "follow, join" or one per line -> ["follow", "join"]."""
    text = (raw or "").strip()
    found = [(a or b).strip() for a, b in _ANY_BUTTON.findall(text)]
    names = found or [part.strip() for part in re.split(r"[,\n]+", text)]
    names = [name for name in names if name]
    if not names:
        raise ValueError("Send at least one button name, like follow or join.")
    if len(names) > AUTOCLICK_MAX_BUTTONS:
        raise ValueError(f"At most {AUTOCLICK_MAX_BUTTONS} button names at a time.")
    if any(len(name) > 40 for name in names):
        raise ValueError("A button name can be at most 40 characters.")
    return names


def split_trailing_buttons(raw: str) -> tuple[str, list[str]]:
    """"hello [follow] (join)" -> ("hello", ["follow", "join"]): button names written at the end."""
    remainder = (raw or "").rstrip()
    buttons: list[str] = []
    while True:
        match = _TAIL_BUTTON.search(remainder)
        if not match:
            break
        buttons.insert(0, (match.group(1) or match.group(2)).strip())
        remainder = remainder[: match.start()]
    if len(buttons) > AUTOCLICK_MAX_BUTTONS:
        raise ValueError(f"At most {AUTOCLICK_MAX_BUTTONS} button names at a time.")
    return remainder.strip(), buttons


@dataclass
class BulkArgs:
    target: str | None = None
    text: str | None = None
    delay: int | None = None
    confirmed: bool | None = None
    buttons: list[str] = field(default_factory=list)


def parse_bulk_args(kind: str, raw: str) -> BulkArgs:
    """Parse what follows /all, /alll, /multi, /click or /allblock.

    Shape:  <target> [message] [[button] (button) ...] [3sec] [Y|N]     (block has no message)
    Trailing pieces can come in any order: Y/N answers the confirmation, "3sec" is the delay
    between accounts, and each trailing [name] or (name) is the name of an inline button to
    click after the message (the bot's reply is searched for a button containing that name).
    Everything is optional and missing pieces are asked step by step.
    """
    rest = (raw or "").strip()
    args = BulkArgs()
    if not rest:
        return args
    parts = rest.split(None, 1)
    args.target = parse_target(parts[0])
    remainder = parts[1] if len(parts) > 1 else ""
    while True:
        confirm = _TAIL_CONFIRM.search(remainder) if args.confirmed is None else None
        if confirm:
            args.confirmed = confirm.group(1).lower().startswith("y")
            remainder = remainder[: confirm.start()]
            continue
        delay = _TAIL_DELAY.search(remainder) if args.delay is None else None
        if delay:
            args.delay = int(delay.group(1))
            remainder = remainder[: delay.start()]
            continue
        button = _TAIL_BUTTON.search(remainder)
        if button:
            args.buttons.insert(0, (button.group(1) or button.group(2)).strip())
            remainder = remainder[: button.start()]
            continue
        break
    if len(args.buttons) > AUTOCLICK_MAX_BUTTONS:
        raise ValueError(f"At most {AUTOCLICK_MAX_BUTTONS} button names at a time.")
    message = remainder.strip()
    if kind == "block":
        if message or args.buttons:
            raise ValueError("/allblock only takes a user, an optional delay like 3sec, and Y/N.")
    elif kind == "click":
        if message:
            raise ValueError("/click only takes a user and button names like [follow] (join), then an optional delay and Y/N.")
    elif message:
        args.text = message
    return args
