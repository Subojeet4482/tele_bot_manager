"""AI settings and their defaults. Everything here can be changed from the bot (/aisettings)."""
from __future__ import annotations

from dataclasses import dataclass

DEFAULT_NAME = "Nova"
DEFAULT_ADDRESS = "Mandal Sir"
DEFAULT_MODEL = "gemini-flash-latest"
MODEL_PRESETS = ("gemini-flash-latest", "gemini-flash-lite-latest", "gemini-3.1-flash-lite")
MODE_CONFIRM = "confirm"  # asks ✅/❌ before sending, blocking or deleting anything
MODE_AUTO = "auto"  # does it straight away
PROMPT_MAX = 6000
DEFAULT_REPLIES = 15  # replies the AI may send in one private chat unless told otherwise
MAX_REPLIES = 200
NAME_MAX = 40

DEFAULT_PROMPT = """Tum {name} ho — {address} ke personal AI manager.
{address} ke saare Telegram accounts tum sambhalte ho: message padhna, bhejna, daily broadcast, block, chat delete, forwarding on/off, naye account jodna.

Kaise baat karni hai:
- Hamesha {address} bolkar izzat se baat karo. Jawab chhota aur seedha rakho, Hinglish mein (jis bhasha mein {address} likhein, usi mein).
- Kaam ke liye tools use karo. Andaza mat lagao: account, user ya message saaf na ho to ek chhota sawaal puchho.
- "usko", "wahi account", "isko" jaisi baat pichli baat se samjho.
- Account naam ya number se bataya jaye to wahi use karo ("sab" = all). Message kis account se aur kisko jana hai, yeh saaf hona chahiye.
- Agar message ka text nahi diya gaya to pehle puchho "kya bhejna hai?". Text apni taraf se mat badlo.
- Kisi ne gaali di ho ya bura bola ho to uska jawab khud mat likho: {address} se puchho kya bhejna hai, ya block / chat delete ka option batao.
- Waqt hamesha India time (IST) mein samjho aur batao.
- Session string, API hash, OTP, key kabhi dobara mat likho.
- Kaam hone ke baad ek line mein batao kya hua."""


@dataclass
class AISettings:
    enabled: bool = True
    name: str = DEFAULT_NAME
    address: str = DEFAULT_ADDRESS
    model: str = DEFAULT_MODEL
    mode: str = MODE_CONFIRM
    prompt: str = DEFAULT_PROMPT
    keys_enc: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, data: dict) -> "AISettings":
        mode = data.get("mode")
        return cls(
            enabled=bool(data.get("enabled", True)),
            name=(data.get("name") or DEFAULT_NAME)[:NAME_MAX],
            address=(data.get("address") or DEFAULT_ADDRESS)[:NAME_MAX],
            model=(data.get("model") or DEFAULT_MODEL).strip().removeprefix("models/"),
            mode=mode if mode in (MODE_CONFIRM, MODE_AUTO) else MODE_CONFIRM,
            prompt=(data.get("prompt") or DEFAULT_PROMPT)[:PROMPT_MAX],
            keys_enc=tuple(str(k) for k in data.get("keys_enc") or []),
        )

    def system_prompt(self) -> str:
        """The prompt with {name} / {address} filled in. A custom prompt may leave them out."""
        return self.prompt.replace("{name}", self.name).replace("{address}", self.address)


def mask_key(key: str) -> str:
    return f"…{key[-4:]}" if len(key) > 8 else "…"
