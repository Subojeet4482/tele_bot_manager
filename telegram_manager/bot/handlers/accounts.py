"""Connect, list and log out accounts."""
from __future__ import annotations

from datetime import datetime

from telegram_manager.bot import keyboards, ui
from telegram_manager.bot.handlers.base import HandlerBase
from telegram_manager.errors import describe_error


class AccountHandlers(HandlerBase):
    forwarding = None  # the ForwardingHandlers (owns the one-account screen); set in registry.wire()

    # --- connect --------------------------------------------------------------------------
    async def connect_menu(self, update, context) -> None:
        if not await self.entry(update):
            return
        await ui.show(update, "🔗 How do you want to connect the account?", keyboards.connect_keyboard())

    async def connect_start(self, update, context) -> None:
        if not await self.entry(update):
            return
        method = self.arg(update)
        await self.engine.start(update, context, "cx_session" if method == "session" else "cx_otp")

    # --- profile ---------------------------------------------------------------------------
    async def profile_button(self, update, context) -> None:
        """Account screen buttons pf|name|<id>, pf|photo|<id>, pf|bio|<id>."""
        if not await self.entry(update):
            return
        what, _, account_id = self.arg(update).partition("|")
        if what not in ("name", "photo", "bio"):
            return
        account = await self.own_account(update, account_id)
        if account is None:
            return
        if not self.manager.is_online(str(account["id"])):
            await ui.show(
                update, "🔴 This account is offline right now, so its profile cannot be changed.", keyboards.back(f"acc|{account_id}")
            )
            return
        await self.engine.start(update, context, f"pf_{what}", {"account": str(account["id"])})

    # --- list ------------------------------------------------------------------------------
    async def list_screen(self, update, context, footer: str = "") -> None:
        accounts = await self.store.accounts.list(self.uid(update))  # always in number order
        if not accounts:
            await ui.show(update, "You have no connected accounts yet.", keyboards.no_accounts())
            return
        lines = ["📋 Your accounts", ""]
        for account in accounts:
            online = self.manager.is_online(str(account["id"]))
            label = self.manager.label_for_account(account)
            phone = f" · {account['phone_masked']}" if account.get("phone_masked") else ""
            lines.append(f"{account.get('number')}. {label}{phone} {'🟢' if online else '🔴 offline'}")
        lines += ["", "Tap an account for full control: number, username, name, photo, bio, forwarding, session export, give it away or log it out."]
        if footer:
            lines += ["", footer]
        await ui.show(update, "\n".join(lines), keyboards.accounts_list(accounts, self.manager.is_online))

    async def refresh(self, update, context) -> None:
        """🔄 Refresh button and /refresh: read every account's name from Telegram again, then show the list."""
        if not await self.entry(update):
            return
        if await self.accounts_or_notice(update) is None:
            return
        if update.callback_query is not None:
            await ui.show(update, "🔄 Refreshing the account names…")
        try:
            changed, failed, offline = await self.manager.refresh_accounts(self.uid(update))
        except Exception as exc:
            await ui.show(update, f"❌ Could not refresh: {describe_error(exc)}", keyboards.back("acc_list"))
            return
        try:
            renumbered = await self.manager.renumber_accounts(self.uid(update))
        except Exception as exc:  # the names are already saved; only the numbering failed
            renumbered = False
            failed_numbers = describe_error(exc)
        else:
            failed_numbers = ""
        stamp = datetime.now(self.manager.tz).strftime("%H:%M:%S")
        notes = [f"🔄 Refreshed at {stamp}: {changed} name(s) changed."]
        if renumbered:
            notes.append("🔢 Numbers tidied up: your accounts are now 1, 2, 3 … in the same order.")
        if failed_numbers:
            notes.append(f"⚠️ Could not tidy the numbers: {failed_numbers}")
        if offline:
            notes.append(f"🔴 {offline} account(s) offline, so their names were not read.")
        if failed:
            notes.append(f"⚠️ {failed} account(s) did not answer.")
        await self.list_screen(update, context, "\n".join(notes))

    # --- arrange: mv|home, mv|<up|down|top>|<id>|<a|l> --------------------------------------------
    async def arrange_screen(self, update, footer: str = "") -> None:
        accounts = await self.store.accounts.list(self.uid(update))
        if not accounts:
            await ui.show(update, "You have no connected accounts yet.", keyboards.no_accounts())
            return
        lines = ["↕️ Arrange your accounts", ""]
        for account in accounts:
            lines.append(f"{account.get('number')}. {self.manager.label_for_account(account)}")
        lines += ["", "⬆️ ⬇️ move one place · 🔝 make it number 1. Tap a name to open the account."]
        if footer:
            lines += ["", footer]
        await ui.show(update, "\n".join(lines), keyboards.arrange_list(accounts))

    async def move_button(self, update, context) -> None:
        if not await self.entry(update):
            return
        parts = (update.callback_query.data or "").split("|")
        if parts == ["mv", "home"]:
            await self.arrange_screen(update)
            return
        if len(parts) != 4 or parts[1] not in ("up", "down", "top") or parts[3] not in ("a", "l"):
            return
        account = await self.own_account(update, parts[2])
        if account is None:
            return
        try:
            moved = await self.manager.move_account(self.uid(update), str(account["id"]), parts[1])
        except Exception as exc:
            await ui.show(update, f"❌ Could not move it: {describe_error(exc)}", keyboards.back("mv|home"))
            return
        if parts[3] == "l":
            await self.arrange_screen(update)
            return
        refreshed = await self.store.accounts.get(str(account["id"]), owner_id=self.uid(update))
        await self.forwarding._screen(update, refreshed or account, "" if moved else "ℹ️ It cannot move any further that way.")

    async def list_accounts(self, update, context) -> None:
        if await self.entry(update):
            await self.list_screen(update, context)

    # --- logout ----------------------------------------------------------------------------------
    async def logout_menu(self, update, context) -> None:
        if not await self.entry(update):
            return
        accounts = await self.accounts_or_notice(update)
        if accounts is None:
            return
        await ui.show(
            update,
            "🚪 Which account do you want to log out?",
            keyboards.accounts_picker(accounts, "lo", self.manager.is_online, "home", "🚪"),
        )

    async def logout_select(self, update, context) -> None:
        if not await self.entry(update):
            return
        account = await self.own_account(update, self.arg(update), "logout")
        if account is None:
            return
        await ui.show(
            update,
            f"🚪 Log out {self.manager.label_for_account(account)}?\n\n"
            "The session is ended on Telegram's side and deleted here. You can connect it again later.",
            keyboards.logout_confirm(str(account["id"])),
        )

    async def logout_confirm(self, update, context) -> None:
        if not await self.entry(update):
            return
        account = await self.own_account(update, self.arg(update), "logout")
        if account is None:
            return
        try:
            revoked = await self.manager.remove_account(str(account["id"]), self.uid(update))
        except Exception as exc:
            await ui.show(update, f"❌ Could not log it out: {describe_error(exc)}", keyboards.back("logout"))
            return
        if revoked is None:
            text = "⏳ This account is already being logged out."
        elif revoked:
            text = f"✅ {self.manager.label_for_account(account)} was logged out."
        else:
            text = (
                f"✅ {self.manager.label_for_account(account)} was removed here.\n"
                "Telegram did not confirm the logout: end the session in Telegram → Settings → Devices."
            )
        await ui.show(update, text, keyboards.back("home"))
