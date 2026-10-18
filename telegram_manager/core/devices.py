"""The devices (login sessions) of one account: list them and end them from the bot."""
from __future__ import annotations

import logging
from typing import Any

from telethon.tl.functions.account import GetAuthorizationsRequest, ResetAuthorizationRequest
from telethon.tl.functions.auth import ResetAuthorizationsRequest

from telegram_manager.log_setup import mask_id

logger = logging.getLogger(__name__)

_ERRORS = {
    "FreshResetAuthorisationForbiddenError": (
        "Telegram does not let a login younger than 24 hours end other devices. Try again later."
    ),
    "HashInvalidError": "That device is already gone. Refresh the list.",
}


def _friendly(exc: Exception) -> Exception:
    text = _ERRORS.get(exc.__class__.__name__)
    return ValueError(text) if text else exc


class DevicesMixin:
    async def list_devices(self, account_id: str, owner_id: int | None = None) -> list[dict[str, Any]]:
        """Every login of the account, the one this bot uses first, then the most recently active."""
        client = self._client_for(account_id, owner_id)
        result = await self._request(client(GetAuthorizationsRequest()), 20)
        devices = [
            {
                "hash": int(item.hash),
                "current": bool(getattr(item, "current", False)),
                "device": str(getattr(item, "device_model", "") or "Unknown device"),
                "platform": str(getattr(item, "platform", "") or ""),
                "system": str(getattr(item, "system_version", "") or ""),
                "app": " ".join(p for p in (getattr(item, "app_name", ""), getattr(item, "app_version", "")) if p),
                "ip": str(getattr(item, "ip", "") or ""),
                "country": str(getattr(item, "country", "") or ""),
                "region": str(getattr(item, "region", "") or ""),
                "active": getattr(item, "date_active", None),
                "created": getattr(item, "date_created", None),
            }
            for item in result.authorizations
        ]
        devices.sort(key=lambda d: (not d["current"], -(d["active"].timestamp() if d["active"] else 0)))
        return devices

    async def end_device(self, account_id: str, owner_id: int | None, device_hash: int) -> None:
        """Log out one other device. The login this bot uses cannot be ended here (use Logout)."""
        client = self._client_for(account_id, owner_id)
        for device in await self.list_devices(account_id, owner_id):
            if device["hash"] == device_hash and device["current"]:
                raise ValueError("That is the login this bot uses. Use “Logout this account” to remove it.")
        try:
            await self._request(client(ResetAuthorizationRequest(hash=device_hash)), 20)
        except Exception as exc:
            raise _friendly(exc) from exc
        logger.info("Ended a device of account %s", mask_id(account_id))

    async def end_other_devices(self, account_id: str, owner_id: int | None = None) -> int:
        """Log out every device except the one this bot uses. Returns how many were ended."""
        client = self._client_for(account_id, owner_id)
        before = sum(1 for d in await self.list_devices(account_id, owner_id) if not d["current"])
        try:
            await self._request(client(ResetAuthorizationsRequest()), 20)
        except Exception as exc:
            raise _friendly(exc) from exc
        logger.info("Ended all other devices of account %s", mask_id(account_id))
        return before
