"""Renumber on refresh, arranging the order of accounts, and the devices (logins) screen."""
from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

from tests.harness import BOB, OWNER, Harness, texts


def buttons(markup):
    return [b for row in markup.inline_keyboard for b in row]


async def names_in_order(h, owner):
    return [(r["number"], r["display_name"]) for r in await h.store.accounts.list(owner)]


class RenumberTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.h = Harness()
        self.rows = [await self.h.add_account(OWNER, n) for n in ("A", "B", "C", "D")]
        for row, number in zip(self.rows, (5, 6, 7, 8)):
            await self.h.store.accounts.update(row["id"], {"number": number})
            self.h.manager.accounts[row["id"]]["number"] = number
            self.h.manager.clients[row["id"]].me = SimpleNamespace(first_name=row["display_name"], last_name=None, username=None, id=1)

    async def test_refresh_turns_5_6_7_8_into_1_2_3_4(self):
        u = await self.h.tap(OWNER, "acc_refresh")
        self.assertIn("Numbers tidied up", texts(u))
        self.assertEqual(await names_in_order(self.h, OWNER), [(1, "A"), (2, "B"), (3, "C"), (4, "D")])
        self.assertEqual(self.h.manager.accounts[self.rows[3]["id"]]["number"], 4)

    async def test_next_new_account_continues_after_the_tidied_numbers(self):
        await self.h.tap(OWNER, "acc_refresh")
        new = await self.h.store.accounts.create(OWNER, {"display_name": "E"})
        self.assertEqual(new["number"], 5)

    async def test_refresh_with_nothing_to_tidy_says_nothing_about_numbers(self):
        await self.h.tap(OWNER, "acc_refresh")
        u = await self.h.tap(OWNER, "acc_refresh")
        self.assertNotIn("Numbers tidied up", texts(u))

    async def test_each_admin_is_numbered_on_their_own(self):
        await self.h.access.add(BOB, OWNER, "Bob")
        b = await self.h.add_account(BOB, "Bob 1")
        await self.h.store.accounts.update(b["id"], {"number": 9})
        self.h.manager.clients[b["id"]].me = SimpleNamespace(first_name="Bob 1", last_name=None, username=None, id=2)
        await self.h.tap(BOB, "acc_refresh")
        self.assertEqual(await names_in_order(self.h, BOB), [(1, "Bob 1")])
        self.assertEqual([n for n, _ in await names_in_order(self.h, OWNER)], [5, 6, 7, 8])  # untouched


class ArrangeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.h = Harness()
        self.a, self.b, self.c = [await self.h.add_account(OWNER, n) for n in ("A", "B", "C")]

    async def test_list_has_an_arrange_button_and_the_screen_lists_in_order(self):
        u = await self.h.tap(OWNER, "acc_list")
        self.assertIn("mv|home", {b.callback_data for b in buttons(u.last()[1])})
        u = await self.h.tap(OWNER, "mv|home")
        text = texts(u)
        self.assertLess(text.index("1. A"), text.index("2. B"))
        data = {b.callback_data for b in buttons(u.last()[1])}
        self.assertIn(f"mv|up|{self.c['id']}|l", data)
        self.assertIn(f"mv|top|{self.c['id']}|l", data)
        self.assertNotIn(f"mv|up|{self.a['id']}|l", data)  # the first cannot go up
        self.assertNotIn(f"mv|down|{self.c['id']}|l", data)  # the last cannot go down

    async def test_move_up_down_and_top(self):
        h = self.h
        await h.tap(OWNER, f"mv|up|{self.c['id']}|l")
        self.assertEqual(await names_in_order(h, OWNER), [(1, "A"), (2, "C"), (3, "B")])
        await h.tap(OWNER, f"mv|down|{self.a['id']}|l")
        self.assertEqual(await names_in_order(h, OWNER), [(1, "C"), (2, "A"), (3, "B")])
        await h.tap(OWNER, f"mv|top|{self.b['id']}|l")
        self.assertEqual(await names_in_order(h, OWNER), [(1, "B"), (2, "C"), (3, "A")])
        self.assertEqual(h.manager.accounts[self.b["id"]]["number"], 1)

    async def test_move_from_the_account_screen_stays_on_that_screen(self):
        data = {b.callback_data for b in buttons((await self.h.tap(OWNER, f"acc|{self.b['id']}")).last()[1])}
        self.assertIn(f"mv|up|{self.b['id']}|a", data)
        u = await self.h.tap(OWNER, f"mv|up|{self.b['id']}|a")
        self.assertIn("1. B", texts(u))
        u = await self.h.tap(OWNER, f"mv|up|{self.b['id']}|a")  # already first
        self.assertIn("cannot move any further", texts(u))

    async def test_cannot_move_someone_elses_account(self):
        await self.h.access.add(BOB, OWNER, "Bob")
        u = await self.h.tap(BOB, f"mv|up|{self.c['id']}|l")
        self.assertIn("Account not found", texts(u))
        self.assertEqual([n for n, _ in await names_in_order(self.h, OWNER)], [1, 2, 3])


def _device(hash_, name, current=False, platform="Android", day=10):
    return {"hash": hash_, "current": current, "device": name, "platform": platform, "system": "14",
            "app": "Telegram 11", "ip": "1.2.3.4", "country": "India", "region": "",
            "active": datetime(2026, 10, day, 8, 30, tzinfo=timezone.utc), "created": None}


class DevicesScreenTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.h = Harness()
        self.acc = await self.h.add_account(OWNER, "Own 1")
        self.devices = [_device(1, "Manager", current=True, platform="Linux"), _device(22, "Pixel 7"), _device(-33, "iPhone 15", platform="iOS")]
        self.ended: list = []

        async def list_devices(account_id, owner_id=None):
            return list(self.devices)

        async def end_device(account_id, owner_id, device_hash):
            self.ended.append(device_hash)
            self.devices = [d for d in self.devices if d["hash"] != device_hash]

        async def end_other(account_id, owner_id=None):
            count = sum(1 for d in self.devices if not d["current"])
            self.devices = [d for d in self.devices if d["current"]]
            return count

        self.h.manager.list_devices = list_devices
        self.h.manager.end_device = end_device
        self.h.manager.end_other_devices = end_other

    async def test_account_screen_opens_devices(self):
        data = {b.callback_data for b in buttons((await self.h.tap(OWNER, f"acc|{self.acc['id']}")).last()[1])}
        self.assertIn(f"dv|list|{self.acc['id']}", data)

    async def test_devices_screen_lists_all_and_offers_end_only_for_others(self):
        u = await self.h.tap(OWNER, f"dv|list|{self.acc['id']}")
        text = texts(u)
        for expected in ("Manager", "Pixel 7", "iPhone 15", "2 other device(s)", "this bot"):
            self.assertIn(expected, text)
        data = [b.callback_data for b in buttons(u.last()[1])]
        self.assertIn(f"dv|end|{self.acc['id']}|22", data)
        self.assertIn(f"dv|end|{self.acc['id']}|-33", data)
        self.assertNotIn(f"dv|end|{self.acc['id']}|1", data)  # the bot's own login
        self.assertIn(f"dv|all|{self.acc['id']}", data)
        self.assertTrue(all(len(d.encode()) <= 64 for d in data))

    async def test_end_one_device(self):
        u = await self.h.tap(OWNER, f"dv|end|{self.acc['id']}|22")
        self.assertEqual(self.ended, [22])
        self.assertIn("logged out", texts(u))
        self.assertNotIn("Pixel 7", texts(u).split("✅")[0])

    async def test_end_all_asks_first_then_ends_the_others(self):
        u = await self.h.tap(OWNER, f"dv|all|{self.acc['id']}")
        self.assertIn("End all other devices?", texts(u))
        self.assertEqual(len(self.devices), 3)
        u = await self.h.tap(OWNER, f"dv|allc|{self.acc['id']}")
        self.assertIn("2 other device(s) logged out", texts(u))
        self.assertEqual([d["hash"] for d in self.devices], [1])

    async def test_offline_account_and_foreign_account(self):
        self.h.manager.clients[self.acc["id"]].connected = False
        self.assertIn("offline", texts(await self.h.tap(OWNER, f"dv|list|{self.acc['id']}")))
        await self.h.access.add(BOB, OWNER, "Bob")
        self.assertIn("Account not found", texts(await self.h.tap(BOB, f"dv|list|{self.acc['id']}")))
        self.assertEqual(self.ended, [])

    async def test_a_failed_end_is_shown_not_raised(self):
        async def broken(account_id, owner_id, device_hash):
            raise ValueError("Telegram says no.")

        self.h.manager.end_device = broken
        self.assertIn("Telegram says no.", texts(await self.h.tap(OWNER, f"dv|end|{self.acc['id']}|22")))


class DevicesCoreTests(unittest.IsolatedAsyncioTestCase):
    """The Telethon side, with distinct request classes so it also works with the stubs."""

    async def asyncSetUp(self):
        self.h = Harness()
        self.acc = await self.h.add_account(OWNER, "Own 1")
        self.sent = []
        auth = lambda h, cur, name: SimpleNamespace(hash=h, current=cur, device_model=name, platform="x", system_version="1",
                                                    app_name="Telegram", app_version="1", ip="9.9.9.9", country="IN", region="",
                                                    date_active=datetime(2026, 10, 1, tzinfo=timezone.utc), date_created=None)
        self.auths = [auth(5, False, "Old phone"), auth(1, True, "Bot")]

        class Get:  # noqa: D401
            pass

        class Reset:
            def __init__(self, hash):
                self.hash = hash

        class ResetAll:
            pass

        self.Get, self.Reset, self.ResetAll = Get, Reset, ResetAll
        patches = [mock.patch("telegram_manager.core.devices.GetAuthorizationsRequest", Get),
                   mock.patch("telegram_manager.core.devices.ResetAuthorizationRequest", Reset),
                   mock.patch("telegram_manager.core.devices.ResetAuthorizationsRequest", ResetAll)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

        async def call(request):
            self.sent.append(request)
            if isinstance(request, Get):
                return SimpleNamespace(authorizations=list(self.auths))
            return True

        self.h.manager.clients[self.acc["id"]].__class__.__call__ = lambda client, request: call(request)

    async def test_list_puts_the_bot_login_first(self):
        devices = await self.h.manager.list_devices(self.acc["id"], OWNER)
        self.assertEqual([(d["hash"], d["current"]) for d in devices], [(1, True), (5, False)])

    async def test_ending_the_bots_own_login_is_refused(self):
        with self.assertRaises(ValueError):
            await self.h.manager.end_device(self.acc["id"], OWNER, 1)
        self.assertFalse(any(isinstance(r, self.Reset) for r in self.sent))

    async def test_end_one_and_end_others(self):
        await self.h.manager.end_device(self.acc["id"], OWNER, 5)
        self.assertEqual([r.hash for r in self.sent if isinstance(r, self.Reset)], [5])
        self.assertEqual(await self.h.manager.end_other_devices(self.acc["id"], OWNER), 1)
        self.assertTrue(any(isinstance(r, self.ResetAll) for r in self.sent))

    async def test_another_admin_cannot_list_devices(self):
        with self.assertRaises(ValueError):
            await self.h.manager.list_devices(self.acc["id"], BOB)


if __name__ == "__main__":
    unittest.main()
