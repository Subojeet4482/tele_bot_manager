from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time as dtime
from typing import Any


@dataclass
class LoginAttempt:
    owner_id: int
    api_id: int
    api_hash: str
    phone: str
    client: Any
    phone_code_hash: str


@dataclass
class BulkResult:
    """Outcome of an action run on all of one admin's accounts."""

    total: int = 0
    ok: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def failed(self) -> int:
        return len(self.errors)

    @property
    def done(self) -> int:
        return self.ok + self.failed


@dataclass
class DailyJob:
    target: str
    text: str
    delay_seconds: int
    start_time: dtime
    times_per_day: int
    account_ids: list[str] | None = None  # None = every connected account (also ones added later)
    buttons: list[str] = field(default_factory=list)  # inline buttons to press after the message

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "text": self.text,
            "delay_seconds": self.delay_seconds,
            "start_time": self.start_time.strftime("%H:%M"),
            "times_per_day": self.times_per_day,
            "account_ids": None if self.account_ids is None else [str(item) for item in self.account_ids],
            "buttons": list(self.buttons),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DailyJob":
        hour, minute = str(data["start_time"]).split(":")
        return cls(
            target=str(data["target"]),
            text=str(data["text"]),
            delay_seconds=int(data["delay_seconds"]),
            start_time=dtime(int(hour), int(minute)),
            times_per_day=int(data["times_per_day"]),
            account_ids=None if data.get("account_ids") is None else [str(item) for item in data["account_ids"]],
            buttons=[str(item) for item in data.get("buttons") or []],
        )
