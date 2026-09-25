"""The bank: what it protects, what it does not, and where it is.

A vault that a police stop could still empty would be decoration, and one
that survived the loan deadline would let you borrow ten thousand, bank it
and walk away. Both are pinned here.
"""
import json
import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dopewars as game
import dopewars_menu as menu
import dopewars_theme as theme
import db_operations


def act(state, text):
    after, _reply, _leave = game.command(state, text)
    return after


class BankRulesTests(unittest.TestCase):
    def _at_the_bank(self, **overrides):
        state = game.new_game(19)
        state["place"] = game.BANK_PLACE
        state.update(overrides)
        return state

    def test_it_is_in_one_town(self):
        """Reachable everywhere it would be free insurance -- deposit before
        every trip, withdraw on arrival -- so it is somewhere you go."""
        away = game.new_game(19)
        away["place"] = next(p for p in game.PLACES if p != game.BANK_PLACE)
        self.assertEqual(away, act(away, "bank deposit 100"))
        here = self._at_the_bank()
        self.assertEqual(100, act(here, "bank deposit 100")["bank"])

    def test_a_deposit_moves_cash_and_nothing_else(self):
        state = self._at_the_bank(cash=2400)
        after = act(state, "bank deposit 1000")
        self.assertEqual((1400, 1000), (after["cash"], after["bank"]))
        self.assertEqual(state["debt"], after["debt"])
        self.assertEqual(state["inventory"], after["inventory"])

    def test_a_withdrawal_brings_it_back(self):
        state = self._at_the_bank(cash=400, bank=1000)
        after = act(state, "bank withdraw 600")
        self.assertEqual((1000, 400), (after["cash"], after["bank"]))

    def test_you_cannot_bank_what_you_are_not_carrying(self):
        state = self._at_the_bank(cash=50)
        self.assertEqual(state, act(state, "bank deposit 51"))

    def test_you_cannot_withdraw_what_is_not_there(self):
        state = self._at_the_bank(bank=50)
        self.assertEqual(state, act(state, "bank withdraw 51"))

    # --- the whole point ---------------------------------------------------

    def test_a_police_fine_cannot_reach_it(self):
        """The request, in one test: a surrender takes a quarter of what you
        are carrying and none of what you banked."""
        state = self._at_the_bank(cash=4000, bank=6000)
        state.update(phase="police", enemy_hp=45)
        after = act(state, "surrender")
        self.assertEqual(3000, after["cash"])      # a quarter of 4000 gone
        self.assertEqual(6000, after["bank"])

    def test_being_defeated_cannot_reach_it(self):
        state = self._at_the_bank(cash=4000, bank=6000, hp=1)
        state.update(phase="police", enemy_hp=45)
        while state["phase"] == "police" and state["hp"] > 0:
            state = act(state, "fight")
        if state["outcome"] == "Defeated":
            self.assertEqual(0, state["cash"])
            self.assertEqual(6000, state["bank"])

    def test_the_loan_deadline_does_reach_it(self):
        """Otherwise: borrow the limit, bank it, miss the deadline on
        purpose, keep the money."""
        state = self._at_the_bank(cash=500, bank=9000, debt=10000,
                                  loan_due=5, day=5)
        after = act(state, "travel " + next(p for p in game.PLACES
                                            if p != game.BANK_PLACE))
        self.assertEqual("Bankrupt", after["outcome"])
        self.assertEqual(0, after["cash"])
        self.assertEqual(0, after["bank"])

    def test_the_score_counts_it(self):
        state = self._at_the_bank(cash=1000, bank=5000, debt=2000)
        self.assertEqual(4000, game.score(state))

    def test_finishing_counts_it_towards_completing(self):
        """Cash alone would read as bankrupt here; the bank is what makes
        the run a completed one."""
        state = self._at_the_bank(cash=100, bank=5000, debt=2000)
        state["inventory"] = {k: 0 for k in game.GOODS}
        self.assertEqual("Completed", act(state, "finish")["outcome"])

    def test_a_save_without_one_gains_an_empty_account(self):
        state = game.new_game(19)
        del state["bank"]
        state["version"] = 4
        state["cash"] = 777
        migrated = game.validate(game.migrate(json.loads(json.dumps(state))))
        self.assertEqual(game.VERSION, migrated["version"])
        self.assertEqual(777, migrated["cash"])
        self.assertEqual(0, migrated["bank"])


class BankScreenTests(unittest.TestCase):
    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        self.addCleanup(self._close)

    def _close(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def _money(self, pg13=False, **overrides):
        state = game.new_game(19)
        state.update(overrides)
        return menu.render(state, {"menu": "loan"}, theme.theme(pg13))

    def test_the_keys_appear_only_where_the_bank_is(self):
        here = self._money(place=game.BANK_PLACE)
        self.assertIn("[3]Deposit", here)
        self.assertIn("[4]Withdraw", here)
        away = self._money(place=next(p for p in game.PLACES
                                      if p != game.BANK_PLACE))
        self.assertNotIn("[3]Deposit", away)

    def test_being_elsewhere_says_where_to_go(self):
        for pg13 in (False, True):
            t = theme.theme(pg13)
            away = self._money(pg13, place=next(p for p in game.PLACES
                                                if p != game.BANK_PLACE))
            with self.subTest(theme=t["title"]):
                self.assertIn(t["places"][game.BANK_PLACE], away)

    def test_pressing_deposit_elsewhere_is_refused(self):
        state = game.new_game(19)
        state["place"] = next(p for p in game.PLACES if p != game.BANK_PLACE)
        turn = menu._Turn(1, "caller", theme.theme(False), state)
        with self.assertRaises(menu._Stop):
            menu._step(turn, "3", {"menu": "loan"})

    def test_a_balance_shows_on_the_screen_you_see_every_turn(self):
        """Money you cannot see is money you forget, and this decides the
        final score."""
        for pg13 in (False, True):
            state = game.new_game(19)
            state["bank"] = 500
            screen = menu.render(state, {"menu": "main"}, theme.theme(pg13))
            with self.subTest(theme=pg13):
                self.assertIn("saved $500", screen)

    def test_an_empty_account_costs_the_status_line_nothing(self):
        state = game.new_game(19)
        screen = menu.render(state, {"menu": "main"}, theme.theme(False))
        self.assertNotIn("saved", screen)

    def test_every_money_screen_fits_one_packet(self):
        for pg13 in (False, True):
            for place in game.PLACES:
                for cash, bank, debt in ((0, 0, 0), (999999, 999999, 10000),
                                         (2400, 500, 1200)):
                    for nav in ({"menu": "loan"},
                                {"menu": "loan_amt", "op": "deposit"},
                                {"menu": "loan_amt", "op": "withdraw"},
                                {"menu": "loan_amt", "op": "borrow"},
                                {"menu": "loan_amt", "op": "repay"}):
                        state = game.new_game(19)
                        state.update(place=place, cash=cash, bank=bank,
                                     debt=debt, loan_due=394 if debt else 0)
                        screen = menu.render(state, nav, theme.theme(pg13))
                        size = len(screen.encode("utf-8"))
                        with self.subTest(place=place, nav=nav["menu"],
                                          op=nav.get("op"), size=size):
                            self.assertLessEqual(size, menu.MAX_SCREEN_BYTES,
                                                 screen)

    def test_the_main_screen_still_fits_with_a_balance_on_it(self):
        worst, worst_screen = 0, ""
        for pg13 in (False, True):
            t = theme.theme(pg13)
            for bank in (0, 500, 999999):
                for place in game.PLACES:
                    for kind in ("deal", "bust"):
                        for item in game.GOODS:
                            state = game.new_game(1)
                            state.update(place=place, bank=bank,
                                         event=f"{kind}:{item}")
                            screen = menu.render(state, {"menu": "main"}, t)
                            size = len(screen.encode("utf-8"))
                            if size > worst:
                                worst, worst_screen = size, screen
        self.assertLessEqual(worst, menu.MAX_SCREEN_BYTES,
                             f"{worst} bytes:\n{worst_screen}")


if __name__ == "__main__":
    unittest.main()
