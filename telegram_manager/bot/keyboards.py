"""Every inline keyboard in one place. Callback data prefixes:

home / menu            main screens
connect, cx|otp|session  connecting an account
acc_list, acc_refresh, acc|<id>   accounts list, refresh names, one account's screen
logout, lo|<id>, loc|<id>   logout picker / confirm / do it
fw|...                 forwarding switches
bulk|<kind>, msg|start, msg_acc|<id>, pf|name|photo|bio|<id>   start a wizard from a button
sched|home|new|off, adm|home|add|remove, logs|link
wz|<wizard>|<action>|<arg>   inside step-by-step flows (see bot/wizard)
"""
from __future__ import annotations

from telegram import InlineKeyboardButton as Btn
from telegram import InlineKeyboardMarkup as Markup

BULK_TITLES = {
    "all": "📨 Message all accounts",
    "alll": "📤 Message all (opens chat)",
    "block": "🚫 Block on all accounts",
    "multi": "👥 Message chosen accounts",
    "click": "🖱 Click buttons",
}


def _markup(rows: list[list[Btn]]) -> Markup:
    return Markup(rows)


def back(to: str = "home", label: str = "⬅️ Back") -> Markup:
    return _markup([[Btn(label, callback_data=to)]])


def start_keyboard(is_owner: bool) -> Markup:
    rows = [
        [Btn("🔗 Connect account", callback_data="connect"), Btn("🚪 Logout account", callback_data="logout")],
        [Btn("📋 My accounts", callback_data="acc_list")],
    ]
    if is_owner:
        rows.append([Btn("👑 Manage admins", callback_data="adm|home")])
    rows.append([Btn("⚙️ Menu", callback_data="menu")])
    return _markup(rows)


def menu_keyboard(is_owner: bool) -> Markup:
    rows = [
        [Btn(BULK_TITLES["all"], callback_data="bulk|all"), Btn(BULK_TITLES["alll"], callback_data="bulk|alll")],
        [Btn(BULK_TITLES["multi"], callback_data="bulk|multi"), Btn(BULK_TITLES["click"], callback_data="bulk|click")],
        [
            Btn(BULK_TITLES["block"], callback_data="bulk|block"),
            Btn("💬 Message from one account", callback_data="msg|start"),
        ],
        [Btn("📥 Forwarding", callback_data="fw|home"), Btn("🕒 Daily broadcast", callback_data="sched|home")],
        [Btn("📋 My accounts", callback_data="acc_list"), Btn("🔄 Refresh names", callback_data="acc_refresh")],
    ]
    if is_owner:
        rows.append([Btn("📜 Live logs", callback_data="logs|link")])
    rows.append([Btn("⬅️ Back", callback_data="home")])
    return _markup(rows)


def connect_keyboard() -> Markup:
    return _markup([
        [Btn("📱 Phone number + code", callback_data="cx|otp")],
        [Btn("🔑 Session string", callback_data="cx|session")],
        [Btn("⬅️ Back", callback_data="home")],
    ])


def after_connect() -> Markup:
    return _markup([[Btn("📋 My accounts", callback_data="acc_list"), Btn("⚙️ Menu", callback_data="menu")]])


def accounts_picker(accounts: list[dict], prefix: str, online, back_to: str, icon: str = "📱") -> Markup:
    """One button per account; `prefix|<id>` is sent back. online(id) -> bool."""
    rows = []
    for account in accounts:
        dot = "🟢" if online(str(account["id"])) else "🔴"
        name = str(account.get("display_name") or "Unnamed")[:28]
        rows.append([Btn(f"{icon} {account.get('number')}. {name} {dot}", callback_data=f"{prefix}|{account['id']}")])
    rows.append([Btn("⬅️ Back", callback_data=back_to)])
    return _markup(rows)


def accounts_list(accounts: list[dict], online) -> Markup:
    rows = [
        [Btn(f"📱 {a.get('number')}. {str(a.get('display_name') or 'Unnamed')[:28]} "
             f"{'🟢' if online(str(a['id'])) else '🔴'}", callback_data=f"acc|{a['id']}")]
        for a in accounts
    ]
    rows.append([Btn("🔄 Refresh", callback_data="acc_refresh"), Btn("🔗 Connect account", callback_data="connect")])
    rows.append([Btn("⬅️ Back", callback_data="home")])
    return _markup(rows)


def account_screen(account_id: str, labels: dict[str, str], back_to: str = "acc_list") -> Markup:
    """labels: {"u": "...", "b": "...", "c": "..."} button texts for the three forwarding switches."""
    return _markup([
        [Btn(labels["u"], callback_data=f"fw|set|{account_id}|u")],
        [Btn(labels["b"], callback_data=f"fw|set|{account_id}|b")],
        [Btn(labels["c"], callback_data=f"fw|set|{account_id}|c")],
        [Btn("💬 Message from this account", callback_data=f"msg_acc|{account_id}")],
        [
            Btn("✏️ Name", callback_data=f"pf|name|{account_id}"),
            Btn("🖼 Photo", callback_data=f"pf|photo|{account_id}"),
            Btn("📝 Bio", callback_data=f"pf|bio|{account_id}"),
        ],
        [Btn("🚪 Logout this account", callback_data=f"lo|{account_id}")],
        [Btn("⬅️ Back", callback_data=back_to)],
    ])


def logout_confirm(account_id: str) -> Markup:
    return _markup([
        [Btn("✅ Yes, log it out", callback_data=f"loc|{account_id}")],
        [Btn("⬅️ Back", callback_data="logout")],
    ])


def forwarding_home(settings: dict[str, bool]) -> Markup:
    def label(title: str, on: bool) -> str:
        return f"{title}: {'🟢 ON' if on else '🔴 OFF'}"

    return _markup([
        [Btn(label("👤 User messages", settings["forward_user_messages"]), callback_data="fw|g|u")],
        [Btn(label("🤖 Bot messages", settings["forward_bot_messages"]), callback_data="fw|g|b")],
        [Btn(label("📢 Channel & group messages", settings["forward_channel_messages"]), callback_data="fw|g|c")],
        [Btn("📱 Per-account settings", callback_data="fw|accs")],
        [Btn("⬅️ Back", callback_data="menu")],
    ])


def schedule_home(has_job: bool) -> Markup:
    rows = [[Btn("🔄 Replace schedule" if has_job else "➕ New schedule", callback_data="sched|new")]]
    if has_job:
        rows.append([Btn("🗑 Turn off", callback_data="sched|off")])
    rows.append([Btn("⬅️ Back", callback_data="menu")])
    return _markup(rows)


def admins_home(can_remove: bool) -> Markup:
    rows = [[Btn("➕ Add admin", callback_data="adm|add")]]
    if can_remove:
        rows.append([Btn("➖ Remove admin", callback_data="adm|remove")])
    rows.append([Btn("⬅️ Back", callback_data="home")])
    return _markup(rows)


def after_action(again_label: str, again_data: str) -> Markup:
    return _markup([[Btn(again_label, callback_data=again_data), Btn("⚙️ Menu", callback_data="menu")]])


def stop_bulk() -> Markup:
    return _markup([[Btn("🛑 Stop", callback_data="stop_bulk")]])


def no_accounts() -> Markup:
    return _markup([[Btn("🔗 Connect account", callback_data="connect")], [Btn("⬅️ Back", callback_data="home")]])
