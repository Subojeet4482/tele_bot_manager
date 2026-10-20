"""What the AI can do. Every tool wraps something the bot already does for its admin.

Reading and looking things up runs straight away. Sending, blocking and deleting stop and ask
the admin to tap ✅ first (confirm mode), or run at once in auto mode. An admin's tools only ever
see that admin's own accounts."""
from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass, field
from datetime import time as dtime
from typing import Any, Awaitable, Callable

from telegram_manager.ai.config import DEFAULT_REPLIES, MAX_REPLIES, MODE_CONFIRM
from telegram_manager.constants import BULK_DELAY_MAX, MAX_TIMES_PER_DAY, OPEN_CHAT_MIN_DELAY
from telegram_manager.core.models import DailyJob
from telegram_manager.errors import describe_error
from telegram_manager.parsing import (
    looks_like_selector,
    parse_selector,
    parse_target,
    parse_time,
)
from telegram_manager.store.settings import FORWARD_KEYS

logger = logging.getLogger(__name__)

PENDING_KEY = "ai_pending"
PENDING_LIMIT = 20
ALL_WORDS = {"all", "sab", "sabhi", "saare", "sare", "everyone", "everything"}
FORWARD_KINDS = {"user": FORWARD_KEYS[0], "bot": FORWARD_KEYS[1], "channel": FORWARD_KEYS[2]}


@dataclass
class Outcome:
    """What the tools want shown to the admin besides the AI's own words."""

    pending: list[tuple[str, str]] = field(default_factory=list)  # (token, what needs approval)
    choices: list[tuple[str, str]] = field(default_factory=list)  # (button label, callback data)
    wizard: str | None = None  # a step-by-step flow to start (adding an account)


@dataclass
class ToolContext:
    deps: Any
    update: Any
    context: Any
    owner_id: int
    mode: str
    outcome: Outcome = field(default_factory=Outcome)

    @property
    def manager(self):
        return self.deps.manager

    @property
    def store(self):
        return self.deps.store


@dataclass
class Prepared:
    summary: str
    data: dict[str, Any]
    direct: bool = False  # run at once even in confirm mode (nothing is sent, or the admin picks by button)


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    prepare: Callable[[ToolContext, dict], Awaitable[Prepared]]
    run: Callable[[ToolContext, dict], Awaitable[dict]]
    risky: bool = False
    always_confirm: bool = False  # asks first even in auto mode (cannot be undone)

    def declaration(self) -> dict:
        return {"name": self.name, "description": self.description, "parameters": self.parameters}


# --- helpers -----------------------------------------------------------------------------
def _obj(properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": properties, "required": required or []}


_STR = {"type": "string"}
_INT = {"type": "integer"}
_ACCOUNTS = {
    "type": "array",
    "items": _STR,
    "description": "Account numbers, names or @usernames, or [\"all\"] for every account. Example: [\"1\",\"3\"] or [\"Rahul\"].",
}


def label(account: dict) -> str:
    return f"{account.get('number')}. {account.get('display_name') or 'Unnamed'}"


def labels(accounts: list[dict], limit: int = 6) -> str:
    shown = ", ".join(label(a) for a in accounts[:limit])
    return shown + (f" +{len(accounts) - limit} more" if len(accounts) > limit else "")


async def resolve_accounts(ctx: ToolContext, spec: Any, *, empty_means_all: bool = False) -> list[dict]:
    rows = [a for a in await ctx.store.accounts.list(ctx.owner_id) if a.get("enabled", True)]
    if not rows:
        raise ValueError("No account is connected yet.")
    items = spec if isinstance(spec, list) else ([spec] if spec else [])
    items = [str(item).strip() for item in items if str(item).strip()]
    if not items:
        if empty_means_all:
            return rows
        raise ValueError("Which account? Ask the admin for the account name or number.")
    if any(item.lower() in ALL_WORDS for item in items):
        return rows
    chosen: list[dict] = []
    for item in items:
        if looks_like_selector(item):
            numbers = parse_selector(item, [r["number"] for r in rows if r.get("number")])
            found = [r for r in rows if r.get("number") in numbers]
        else:
            row = await ctx.store.accounts.find_by_alias(ctx.owner_id, item)
            if row is None:
                raise ValueError(f"No account is called '{item}'. Call list_accounts to see them.")
            found = [row]
        chosen += [r for r in found if r not in chosen]
    return chosen


async def resolve_one(ctx: ToolContext, spec: Any) -> dict:
    accounts = await resolve_accounts(ctx, spec)
    if len(accounts) != 1:
        raise ValueError("Pick exactly one account for this.")
    return accounts[0]


def _target(value: Any) -> str:
    return parse_target(str(value or ""))


def _text(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("The message text is empty. Ask the admin what to send.")
    if len(text) > 4000:
        raise ValueError("That message is too long (max 4000 characters).")
    return text


def _direct(summary: str, **data) -> Prepared:
    return Prepared(summary, data, direct=True)


def _ids(accounts: list[dict]) -> list[str]:
    return [str(a["id"]) for a in accounts]


# --- reading / looking up (run immediately) -------------------------------------------------
async def _prep_none(ctx, args) -> Prepared:
    return _direct("", **args)


async def _list_accounts(ctx, data) -> dict:
    rows = await ctx.store.accounts.list(ctx.owner_id)
    return {"accounts": [
        {"number": a.get("number"), "name": a.get("display_name"), "username": a.get("username") or "",
         "online": ctx.manager.is_online(str(a["id"]))}
        for a in rows
    ]}


async def _prep_read_chat(ctx, args) -> Prepared:
    account = await resolve_one(ctx, args.get("account"))
    return _direct("", account=str(account["id"]), chat=_target(args.get("chat")), limit=int(args.get("limit") or 10))


async def _read_chat(ctx, data) -> dict:
    return {"messages": await ctx.manager.read_chat(ctx.owner_id, data["account"], data["chat"], data["limit"])}


async def _prep_unread(ctx, args) -> Prepared:
    accounts = await resolve_accounts(ctx, args.get("accounts"), empty_means_all=True)
    return _direct("", accounts=_ids(accounts)[:12])


async def _unread(ctx, data) -> dict:
    out, skipped = [], 0
    for account_id in data["accounts"]:
        row = await ctx.store.accounts.get(account_id, owner_id=ctx.owner_id)
        if row is None or not ctx.manager.is_online(account_id):
            skipped += 1
            continue
        try:
            chats = await ctx.manager.unread_chats(ctx.owner_id, account_id)
        except Exception as exc:
            out.append({"account": label(row), "error": describe_error(exc)})
            continue
        if chats:
            out.append({"account": label(row), "chats": chats})
    return {"accounts_with_unread": out, "offline_skipped": skipped}


async def _prep_find(ctx, args) -> Prepared:
    account = await resolve_one(ctx, args.get("account"))
    return _direct("", account=str(account["id"]), query=str(args.get("query") or ""))


async def _find(ctx, data) -> dict:
    return {"chats": await ctx.manager.find_chats(ctx.owner_id, data["account"], data["query"])}


async def _prep_whois(ctx, args) -> Prepared:
    return _direct("", target=_target(args.get("target")))


async def _whois(ctx, data) -> dict:
    return await ctx.manager.lookup_user(ctx.owner_id, data["target"])


# --- sending and the risky ones -----------------------------------------------------------------
async def _prep_send(ctx, args) -> Prepared:
    accounts = await resolve_accounts(ctx, args.get("accounts"))
    target, text = _target(args.get("target")), _text(args.get("text"))
    delay = max(OPEN_CHAT_MIN_DELAY, min(int(args.get("delay_seconds") or 5), BULK_DELAY_MAX))
    preview = text if len(text) <= 80 else text[:77] + "…"
    return Prepared(
        f"📨 Send to {target} from {len(accounts)} account(s) ({labels(accounts)}), {delay}s apart:\n“{preview}”",
        {"accounts": _ids(accounts), "target": target, "text": text, "delay": delay},
    )


async def _send(ctx, data) -> dict:
    await ctx.deps.bulk.launch(
        ctx.update, ctx.context, ctx.owner_id, "multi", data["target"], data["text"], data["delay"],
        account_ids=data["accounts"],
    )
    return {"status": "started", "message": None}  # the bulk runner shows its own live progress


async def _prep_block(ctx, args) -> Prepared:
    accounts = await resolve_accounts(ctx, args.get("accounts"))
    target = _target(args.get("target"))
    return Prepared(f"🚫 Block {target} on {len(accounts)} account(s): {labels(accounts)}", {"accounts": _ids(accounts), "target": target})


async def _block(ctx, data) -> dict:
    result = await ctx.manager.block_selected(ctx.owner_id, data["accounts"], data["target"])
    return _bulk_message(f"🚫 {data['target']} blocked", result)


async def _prep_delchat(ctx, args) -> Prepared:
    accounts = await resolve_accounts(ctx, args.get("accounts"))
    target = _target(args.get("target"))
    return Prepared(
        f"🗑 Delete the chat with {target} on {len(accounts)} account(s): {labels(accounts)} (their side stays)",
        {"accounts": _ids(accounts), "target": target},
    )


async def _delchat(ctx, data) -> dict:
    result = await ctx.manager.delete_chat_selected(ctx.owner_id, data["accounts"], data["target"])
    return _bulk_message(f"🗑 Chat with {data['target']} deleted", result)


async def _prep_unsend(ctx, args) -> Prepared:
    minutes = int(args.get("minutes") or 0)
    if not 1 <= minutes <= 24 * 60:
        raise ValueError("Say how many minutes back (1 to 1440).")
    accounts = await resolve_accounts(ctx, args.get("accounts"), empty_means_all=True)
    count = ctx.manager.sent_count(ctx.owner_id, minutes, _ids(accounts))
    if not count:
        raise ValueError(f"The bot sent nothing in the last {minutes} minute(s) from those accounts.")
    return Prepared(
        f"↩️ Delete {count} message(s) sent in the last {minutes} min from {len(accounts)} account(s)",
        {"accounts": _ids(accounts), "minutes": minutes},
    )


async def _unsend(ctx, data) -> dict:
    result, deleted = await ctx.manager.unsend_recent(ctx.owner_id, data["minutes"], data["accounts"])
    return _bulk_message(f"↩️ {deleted} message(s) deleted", result)


def _bulk_message(done: str, result) -> dict:
    lines = [f"✅ {done} on {result.ok} of {result.total} account(s)."]
    if result.errors:
        lines += ["⚠️ Problems:"] + result.errors[:10]
    return {"status": "done" if not result.errors else "partly_done", "message": "\n".join(lines)}


# --- settings and broadcasts ---------------------------------------------------------------------
async def _prep_forwarding(ctx, args) -> Prepared:
    kind = str(args.get("kind") or "").lower()
    if kind not in FORWARD_KINDS:
        raise ValueError("kind must be user, bot or channel.")
    enabled = bool(args.get("enabled"))
    accounts = await resolve_accounts(ctx, args["account"]) if args.get("account") else None
    return _direct("", kind=kind, enabled=enabled, accounts=_ids(accounts) if accounts else None)


async def _forwarding(ctx, data) -> dict:
    key = FORWARD_KINDS[data["kind"]]
    state = "ON" if data["enabled"] else "OFF"
    if data["accounts"] is None:
        await ctx.store.settings.set_forwarding(ctx.owner_id, key, data["enabled"])
        return {"status": "done", "message": f"📥 {data['kind']} message forwarding is now {state} for all accounts."}
    for account_id in data["accounts"]:
        await ctx.manager.set_forward_override(account_id, key, data["enabled"])
    return {"status": "done", "message": f"📥 {data['kind']} message forwarding is now {state} on {len(data['accounts'])} account(s)."}


def _job_line(slot: int, job: DailyJob) -> dict:
    return {
        "slot": slot, "to": job.target, "text": job.text[:60], "time": job.start_time.strftime("%H:%M"),
        "per_day": job.times_per_day, "accounts": "all" if job.account_ids is None else len(job.account_ids),
    }


async def _list_broadcasts(ctx, data) -> dict:
    return {"broadcasts": [_job_line(slot, job) for slot, job in ctx.manager.daily_jobs_for(ctx.owner_id)]}


async def _prep_create_broadcast(ctx, args) -> Prepared:
    target, text = _target(args.get("target")), _text(args.get("text"))
    clock = parse_time(str(args.get("time") or ""))
    spec = args.get("accounts")
    everyone = [a for a in await ctx.store.accounts.list(ctx.owner_id) if a.get("enabled", True)]
    accounts = await resolve_accounts(ctx, spec, empty_means_all=True)
    delay = max(OPEN_CHAT_MIN_DELAY, min(int(args.get("delay_seconds") or 5), BULK_DELAY_MAX))
    per_day = max(1, min(int(args.get("times_per_day") or 1), MAX_TIMES_PER_DAY))
    low = max(0, int(args.get("shift_min_seconds") or 0))
    high = max(low, int(args.get("shift_max_seconds") or 0))
    shift = "same time every day" if not high else (f"+{low}s later every day" if low == high else f"random +{low}-{high}s later every day")
    covers_all = len(accounts) == len(everyone)
    return Prepared(
        f"🕒 Daily broadcast to {target} at {clock.strftime('%I:%M %p').lstrip('0')} IST, {per_day}×/day, "
        f"{'all accounts' if covers_all else labels(accounts)}, {shift}:\n“{text[:80]}”",
        {"target": target, "text": text, "time": clock.strftime("%H:%M:%S"), "delay": delay, "per_day": per_day,
         "accounts": None if covers_all else _ids(accounts), "low": low, "high": high},
    )


async def _create_broadcast(ctx, data) -> dict:
    hour, minute, second = (int(x) for x in data["time"].split(":"))
    job = DailyJob(
        data["target"], data["text"], data["delay"], dtime(hour, minute, second), data["per_day"],
        account_ids=data["accounts"], drift_seconds=data["low"], drift_max_seconds=data["high"],
    )
    slot = await ctx.manager.start_daily_job(ctx.owner_id, job)
    return {"status": "done", "slot": slot, "message": f"🕒 Daily broadcast #{slot} is set.\n\n{ctx.manager.daily_job_status(ctx.owner_id, slot)}"}


async def _prep_delete_broadcast(ctx, args) -> Prepared:
    jobs = ctx.manager.daily_jobs_for(ctx.owner_id)
    if not jobs:
        raise ValueError("There is no daily broadcast to delete.")
    slot = args.get("slot")
    if slot in (None, "", 0):
        return _direct("", choose=True)
    slot = int(slot)
    if not any(s == slot for s, _ in jobs):
        raise ValueError(f"There is no broadcast #{slot}. Call list_broadcasts.")
    job = next(j for s, j in jobs if s == slot)
    return Prepared(f"🗑 Delete daily broadcast #{slot} (to {job.target}, “{job.text[:40]}”)", {"slot": slot})


async def _delete_broadcast(ctx, data) -> dict:
    if data.get("choose"):
        for slot, job in ctx.manager.daily_jobs_for(ctx.owner_id):
            ctx.outcome.choices.append((f"🗑 #{slot} → {job.target}: {job.text[:24]}", f"aib|del|{slot}"))
        return {"status": "asked_the_admin_to_pick_one_with_buttons"}
    await ctx.manager.stop_daily_job(ctx.owner_id, data["slot"])
    return {"status": "done", "message": f"🗑 Daily broadcast #{data['slot']} deleted."}


async def _prep_add_account(ctx, args) -> Prepared:
    method = "session" if str(args.get("method") or "").lower().startswith("session") else "phone"
    return _direct("", method=method)


async def _add_account(ctx, data) -> dict:
    ctx.outcome.wizard = "cx_session" if data["method"] == "session" else "cx_otp"
    return {"status": "login_steps_will_start_in_the_chat"}



# --- chatting by itself ------------------------------------------------------------------------------
async def _prep_start_chat(ctx, args) -> Prepared:
    account = await resolve_one(ctx, args.get("account"))
    target = _target(args.get("target"))
    goal = str(args.get("goal") or "").strip()
    if len(goal) < 3:
        raise ValueError("What should the chat be about? Ask the admin for the goal.")
    opener = str(args.get("first_message") or "").strip()[:4000]
    autonomy = "auto" if str(args.get("autonomy") or "").lower() == "auto" else "draft"
    limit = max(1, min(int(args.get("max_replies") or DEFAULT_REPLIES), MAX_REPLIES))
    how = "replies by itself" if autonomy == "auto" else "writes drafts, you approve each"
    return Prepared(
        f"💬 AI chat with {target} from {label(account)}: {how}, up to {limit} replies.\nGoal: {goal[:200]}"
        + (f"\nFirst message: “{opener[:100]}”" if opener else "\n(waits for them to write first)"),
        {"account": str(account["id"]), "target": target, "goal": goal, "opener": opener, "autonomy": autonomy, "limit": limit},
    )


async def _start_chat(ctx, data) -> dict:
    client = ctx.manager._client_for(data["account"], ctx.owner_id)
    entity = await ctx.manager._resolve_any_entity(client, data["target"])
    if getattr(entity, "first_name", None) is None:
        raise ValueError("The AI can only chat privately with a person, not in a group or channel.")
    peer_id = int(entity.id)
    session = await ctx.deps.chats.start(
        ctx.owner_id, data["account"], peer_id, data["target"], data["goal"], data["autonomy"], data["limit"], data["opener"]
    )
    if data["opener"]:
        await ctx.manager.send_to_any(data["account"], data["target"], data["opener"], ctx.owner_id)
    return {"status": "chat_started", "id": session["id"],
            "message": f"💬 AI chat shuru: {data['target']} (reply limit {session['replies_left']}). Band karne ke liye /aichats."}


async def _prep_end_chat(ctx, args) -> Prepared:
    return _direct("", target=str(args.get("target") or ""))


async def _end_chat(ctx, data) -> dict:
    ended = await ctx.deps.chats.end(ctx.owner_id, label=data["target"] or None)
    return {"status": "done", "ended": [s["label"] for s in ended],
            "message": f"⛔ {len(ended)} AI chat band." if ended else "Koi chalti hui AI chat nahi mili."}


async def _prep_resume_chat(ctx, args) -> Prepared:
    return _direct("", target=str(args.get("target") or ""))


async def _resume_chat(ctx, data) -> dict:
    resumed = await ctx.deps.chats.resume(ctx.owner_id, data["target"])
    return {"status": "done", "resumed": [s["label"] for s in resumed]} if resumed else {"error": "No paused chat matches that."}


async def _list_chats(ctx, data) -> dict:
    out = []
    for s in await ctx.deps.chats.sessions(ctx.owner_id):
        if s["state"] == "ended":
            continue
        row = await ctx.store.accounts.get(s["account_id"], owner_id=ctx.owner_id) or {}
        out.append({"with": s["label"], "account": row.get("number"), "state": s["state"], "autonomy": s["autonomy"],
                    "replies_left": s["replies_left"], "goal": s["goal"][:100]})
    return {"chats": out}


# --- more control ----------------------------------------------------------------------------------------
async def _prep_join(ctx, args) -> Prepared:
    accounts = await resolve_accounts(ctx, args.get("accounts"))
    link = str(args.get("link") or "").strip()
    if not link:
        raise ValueError("Which group/channel? Ask for the invite link or @username.")
    return Prepared(f"➕ Join {link} with {len(accounts)} account(s): {labels(accounts)}", {"accounts": _ids(accounts), "link": link})


async def _join(ctx, data) -> dict:
    return _bulk_message("➕ Joined", await ctx.manager.join_selected(ctx.owner_id, data["accounts"], data["link"]))


async def _prep_send_later(ctx, args) -> Prepared:
    prepared = await _prep_send(ctx, args)
    minutes = int(args.get("minutes") or 0)
    if not 1 <= minutes <= 24 * 60:
        raise ValueError("Say after how many minutes (1 to 1440).")
    prepared.data["minutes"] = minutes
    prepared.summary = prepared.summary.replace("📨 Send", f"⏰ In {minutes} min, send", 1)
    return prepared


async def _send_later(ctx, data) -> dict:
    await ctx.deps.bulk.launch(
        ctx.update, ctx.context, ctx.owner_id, "multi", data["target"], data["text"], data["delay"],
        account_ids=data["accounts"], start_in=data["minutes"] * 60,
    )
    return {"status": "scheduled", "message": None}


async def _prep_press(ctx, args) -> Prepared:
    accounts = await resolve_accounts(ctx, args.get("accounts"))
    target = _target(args.get("target"))
    names = [str(b).strip() for b in (args.get("buttons") or []) if str(b).strip()]
    if not names:
        raise ValueError("Which button(s) to press? Ask for the button text.")
    return Prepared(f"🔘 Press {', '.join(names)} in {target} on {len(accounts)} account(s): {labels(accounts)}",
                    {"accounts": _ids(accounts), "target": target, "buttons": names})


async def _press(ctx, data) -> dict:
    await ctx.deps.bulk.launch(ctx.update, ctx.context, ctx.owner_id, "click", data["target"], None, OPEN_CHAT_MIN_DELAY,
                               account_ids=data["accounts"], buttons=data["buttons"])
    return {"status": "started", "message": None}


async def _prep_profile(ctx, args) -> Prepared:
    accounts = await resolve_accounts(ctx, args.get("accounts"))
    fields = {k: str(args[k]).strip() for k in ("first_name", "last_name", "about") if args.get(k) is not None}
    if not fields:
        raise ValueError("What to change: first_name, last_name or about?")
    if any(len(fields.get(k, "")) > 64 for k in ("first_name", "last_name")) or len(fields.get("about", "")) > 70:
        raise ValueError("Names can be 64 characters, the bio 70.")
    return Prepared(f"🪪 Change profile ({', '.join(fields)}) on {len(accounts)} account(s): {labels(accounts)}",
                    {"accounts": _ids(accounts), **fields})


async def _profile(ctx, data) -> dict:
    fields = {k: data[k] for k in ("first_name", "last_name", "about") if k in data}
    done, errors = 0, []
    for account_id in data["accounts"]:
        try:
            await ctx.manager.update_profile(account_id, ctx.owner_id, **fields)
            done += 1
        except Exception as exc:
            errors.append(describe_error(exc))
    return {"status": "done" if not errors else "partly_done",
            "message": f"🪪 Profile changed on {done} of {len(data['accounts'])} account(s)." + ("\n⚠️ " + "; ".join(errors[:5]) if errors else "")}


async def _prep_logout(ctx, args) -> Prepared:
    accounts = await resolve_accounts(ctx, args.get("accounts"))
    return Prepared(f"⚠️ LOG OUT and remove {len(accounts)} account(s): {labels(accounts)}", {"accounts": _ids(accounts)})


async def _logout(ctx, data) -> dict:
    done = 0
    for account_id in data["accounts"]:
        if await ctx.manager.remove_account(account_id, ctx.owner_id):
            done += 1
    return {"status": "done", "message": f"🚪 {done} account(s) logged out and removed."}


# --- the registry -------------------------------------------------------------------------------------
TOOLS: dict[str, Tool] = {t.name: t for t in [
    Tool("list_accounts", "List the admin's connected accounts (number, name, @username, online).", _obj({}), _prep_none, _list_accounts),
    Tool("read_chat", "Read the latest messages of one chat on one account.",
         _obj({"account": _STR, "chat": {"type": "string", "description": "@username or numeric chat id"}, "limit": _INT}, ["account", "chat"]),
         _prep_read_chat, _read_chat),
    Tool("unread_chats", "Show chats with unread messages. Leave accounts empty for all accounts.",
         _obj({"accounts": _ACCOUNTS}), _prep_unread, _unread),
    Tool("find_chat", "Search an account's chats by name or @username to get the chat id.",
         _obj({"account": _STR, "query": _STR}, ["account", "query"]), _prep_find, _find),
    Tool("whois", "Look up the id, name and type of a @username.", _obj({"target": _STR}, ["target"]), _prep_whois, _whois),
    Tool("send_message", "Send a text message to a user/chat from one or more accounts (opens the chat if needed).",
         _obj({"accounts": _ACCOUNTS, "target": {"type": "string", "description": "@username, chat id or +phone"}, "text": _STR,
               "delay_seconds": {"type": "integer", "description": "pause between accounts, min 3"}}, ["accounts", "target", "text"]),
         _prep_send, _send, risky=True),
    Tool("block_user", "Block a user on the named accounts.", _obj({"accounts": _ACCOUNTS, "target": _STR}, ["accounts", "target"]),
         _prep_block, _block, risky=True),
    Tool("delete_chat", "Delete a chat on the named accounts (only their side). For a group or channel this leaves it.", _obj({"accounts": _ACCOUNTS, "target": _STR}, ["accounts", "target"]),
         _prep_delchat, _delchat, risky=True),
    Tool("unsend_recent", "Delete the messages this bot sent in the last N minutes (for everybody). Leave accounts empty for all.",
         _obj({"minutes": _INT, "accounts": _ACCOUNTS}, ["minutes"]), _prep_unsend, _unsend, risky=True),
    Tool("set_forwarding", "Switch forwarding of incoming messages on or off, for all accounts or one.",
         _obj({"kind": {"type": "string", "description": "user, bot or channel"}, "enabled": {"type": "boolean"}, "account": _STR},
              ["kind", "enabled"]), _prep_forwarding, _forwarding),
    Tool("list_broadcasts", "List the daily broadcasts.", _obj({}), _prep_none, _list_broadcasts),
    Tool("create_daily_broadcast",
         "Schedule a message that goes out every day at a time (India time, 24h like 18:30; for every 6 hours use times_per_day=4). The daily shift moves it later every day, fixed or random between min and max seconds.",
         _obj({"target": _STR, "text": _STR, "time": {"type": "string", "description": "HH:MM India time"}, "accounts": _ACCOUNTS,
               "delay_seconds": _INT, "times_per_day": _INT, "shift_min_seconds": _INT, "shift_max_seconds": _INT}, ["target", "text", "time"]),
         _prep_create_broadcast, _create_broadcast, risky=True),
    Tool("delete_broadcast", "Delete a daily broadcast. Without a slot the admin is shown buttons to choose.",
         _obj({"slot": _INT}), _prep_delete_broadcast, _delete_broadcast, risky=True),
    Tool("start_chat",
         "Start an AI-run private chat with ONE person from ONE account. From then on the AI answers whatever that person writes back. "
         "autonomy=auto replies by itself; autonomy=draft (default) shows each reply to the admin first. first_message is optional.",
         _obj({"account": _STR, "target": _STR, "goal": {"type": "string", "description": "what the chat is about / what to achieve"},
               "first_message": _STR, "autonomy": {"type": "string", "description": "auto or draft"}, "max_replies": _INT},
              ["account", "target", "goal"]), _prep_start_chat, _start_chat, risky=True),
    Tool("end_chat", "Stop AI chats: with one person (target) or all when target is empty.", _obj({"target": _STR}), _prep_end_chat, _end_chat),
    Tool("resume_chat", "Continue an AI chat that paused to ask the admin.", _obj({"target": _STR}, ["target"]), _prep_resume_chat, _resume_chat),
    Tool("list_chats", "List the AI's running private chats.", _obj({}), _prep_none, _list_chats),
    Tool("join_chat", "Join a group or channel (invite link or public @username) with the named accounts.",
         _obj({"accounts": _ACCOUNTS, "link": _STR}, ["accounts", "link"]), _prep_join, _join, risky=True),
    Tool("send_later", "Send a message after N minutes (1 to 1440) from the named accounts.",
         _obj({"accounts": _ACCOUNTS, "target": _STR, "text": _STR, "minutes": _INT, "delay_seconds": _INT}, ["accounts", "target", "text", "minutes"]),
         _prep_send_later, _send_later, risky=True),
    Tool("press_buttons", "Press inline buttons (by their text) under a bot's message in a chat, on the named accounts.",
         _obj({"accounts": _ACCOUNTS, "target": _STR, "buttons": {"type": "array", "items": _STR}}, ["accounts", "target", "buttons"]),
         _prep_press, _press, risky=True),
    Tool("set_profile", "Change the first name, last name or bio of the named accounts.",
         _obj({"accounts": _ACCOUNTS, "first_name": _STR, "last_name": _STR, "about": _STR}, ["accounts"]), _prep_profile, _profile, risky=True),
    Tool("logout_account", "Log an account out and remove it from the bot. Always asks the admin first.",
         _obj({"accounts": _ACCOUNTS}, ["accounts"]), _prep_logout, _logout, risky=True, always_confirm=True),
    Tool("add_account", "Start adding a new Telegram account (login steps run in the chat). method: phone or session.",
         _obj({"method": _STR}), _prep_add_account, _add_account),
]}


def declarations() -> list[dict]:
    return [tool.declaration() for tool in TOOLS.values()]


async def _execute(tool: Tool, ctx: ToolContext, data: dict) -> dict:
    try:
        return await tool.run(ctx, data)
    except Exception as exc:
        logger.warning("AI tool %s failed: %s", tool.name, describe_error(exc))
        return {"error": describe_error(exc)}


async def call(ctx: ToolContext, name: str, args: dict | None) -> dict:
    """Run one tool the model asked for (or park it for approval). Always returns a dict for the model."""
    tool = TOOLS.get(name)
    if tool is None:
        return {"error": f"There is no tool called {name}."}
    try:
        prepared = await tool.prepare(ctx, dict(args or {}))
    except (ValueError, TypeError) as exc:
        return {"error": str(exc)}
    except Exception as exc:
        return {"error": describe_error(exc)}
    if tool.risky and not prepared.direct and (ctx.mode == MODE_CONFIRM or tool.always_confirm):
        pending = ctx.context.user_data.setdefault(PENDING_KEY, {})
        while len(pending) >= PENDING_LIMIT:
            pending.pop(next(iter(pending)))
        token = secrets.token_hex(4)
        pending[token] = (name, prepared.data, prepared.summary)
        ctx.outcome.pending.append((token, prepared.summary))
        return {"status": "waiting_for_the_admin_to_approve",
                "note": "Approval buttons were shown. Do not say it is done; say you are waiting for approval."}
    return await _execute(tool, ctx, prepared.data)


async def execute_pending(ctx: ToolContext, token: str) -> dict | None:
    """The admin tapped ✅. None if the request is gone (expired or already used)."""
    entry = ctx.context.user_data.get(PENDING_KEY, {}).pop(token, None)
    if entry is None:
        return None
    name, data, _summary = entry
    return await _execute(TOOLS[name], ctx, data)


def pending_summary(ctx: ToolContext, token: str) -> str | None:
    entry = ctx.context.user_data.get(PENDING_KEY, {}).get(token)
    return entry[2] if entry else None
