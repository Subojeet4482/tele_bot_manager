"""Tiny stand-ins for python-telegram-bot / Telethon, used only when the real libraries are not installed.

They let the tests import and drive the bot code (wizards, handlers, manager) without network access.
With the real libraries installed nothing is replaced."""
from __future__ import annotations

import sys
import types


def _module(name: str, **attrs) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    sys.modules[name] = module
    return module


class _Btn:
    def __init__(self, text, callback_data=None, **kwargs):
        self.text = text
        self.callback_data = callback_data


class _Markup:
    def __init__(self, inline_keyboard):
        self.inline_keyboard = inline_keyboard


class _Any:
    def __init__(self, *args, **kwargs):
        self.args, self.kwargs = args, kwargs


def install() -> None:
    try:
        import telegram  # noqa: F401
        import telethon  # noqa: F401

        return
    except ImportError:
        pass

    class BadRequest(Exception):
        pass

    class NetworkError(Exception):
        pass

    class Conflict(Exception):
        pass

    _module("telegram", Update=_Any, InlineKeyboardButton=_Btn, InlineKeyboardMarkup=_Markup, BotCommand=_Any,
            LinkPreviewOptions=_Any, Bot=_Any)
    _module("telegram.error", BadRequest=BadRequest, NetworkError=NetworkError, Conflict=Conflict)
    _module("telegram.ext", Application=_Any, ApplicationBuilder=_Any, CallbackQueryHandler=_Any,
            CommandHandler=_Any, MessageHandler=_Any, MessageReactionHandler=_Any, TypeHandler=_Any,
            filters=types.SimpleNamespace(TEXT=1, COMMAND=2, PHOTO=3, Document=types.SimpleNamespace(IMAGE=4)))

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

    class _NewMessage:
        Event = object

        def __init__(self, *args, **kwargs):
            pass

    error_names = (
        "AuthKeyUnregisteredError AuthKeyDuplicatedError SessionRevokedError SessionExpiredError "
        "UserDeactivatedError UserDeactivatedBanError PasswordHashInvalidError PhoneCodeExpiredError "
        "PhoneCodeInvalidError SessionPasswordNeededError"
    ).split()
    errors = _module("telethon.errors", **{name: type(name, (Exception,), {}) for name in error_names})
    _module("telethon", TelegramClient=_Client, errors=errors, events=types.SimpleNamespace(NewMessage=_NewMessage))
    _module("telethon.sessions", StringSession=lambda value="": value)
    _module("telethon.tl")
    _module("telethon.tl.functions")
    _module("telethon.tl.functions.contacts", BlockRequest=_Any)
    _module("telethon.tl.functions.messages", SendReactionRequest=_Any, ImportChatInviteRequest=_Any)
    _module("telethon.tl.functions.channels", JoinChannelRequest=_Any)
    _module("telethon.tl.functions.account", UpdateProfileRequest=_Any)
    _module("telethon.tl.functions.photos", UploadProfilePhotoRequest=_Any)
    _module("telethon.tl.types", ReactionEmoji=_Any, Channel=type("Channel", (), {}))


install()
