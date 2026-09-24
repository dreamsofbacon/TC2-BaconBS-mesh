"""Every game screen fits one packet, in both themes, at their worst.

The game is played over LoRa, where a screen that spills into a second
packet doubles the airtime of every turn. Two changes in a row pushed on
this without anyone noticing: six goods and five boroughs instead of four
and four, and then an icon on every line. The Candy Wars buy screen went
two bytes over, and the main screen three, at the sizes a long run reaches.

So the budget is measured here at the worst the game can produce -- the
longest place name, six figures of cash, five of debt, and a market event
on the busiest screen -- rather than at whatever a fresh game happens to
look like.
"""
import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db_operations

# One Meshtastic packet of text. MeshCore is tighter still, but 200 is the
# line the rest of the BBS holds itself to.
PACKET = 200

MENUS = ("main", "buy", "sell", "move", "gear", "bag", "loan")


class ScreenBudgetTests(unittest.TestCase):
    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        self.addCleanup(self._close)

    def _close(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def _worst(self, pg13):
        import dopewars as game
        import dopewars_menu as menu
        import dopewars_theme as theme

        t = theme.theme(pg13)
        worst_size, worst_screen = 0, ""
        for place in game.PLACES:
            for cash in (0, 999999):
                for event in ("", "deal:mushrooms", "bust:cocaine"):
                    state = game.new_game(1)
                    state.update(place=place, cash=cash, debt=99999, event=event)
                    for name in MENUS:
                        screen = menu.render(state, {"menu": name}, t)
                        size = len(screen.encode("utf-8"))
                        if size > worst_size:
                            worst_size, worst_screen = size, screen
        return worst_size, worst_screen

    def test_candy_wars_fits(self):
        size, screen = self._worst(False)
        self.assertLessEqual(size, PACKET, f"{size} bytes:\n{screen}")

    def test_dope_wars_fits(self):
        size, screen = self._worst(True)
        self.assertLessEqual(size, PACKET, f"{size} bytes:\n{screen}")

    def test_the_encounter_screen_fits(self):
        """Not in the loop above: it renders from a different phase."""
        import dopewars as game
        import dopewars_menu as menu
        import dopewars_theme as theme
        for pg13 in (False, True):
            t = theme.theme(pg13)
            state = game.new_game(1)
            state.update(phase="police", enemy_hp=40, hp=7, cash=999999,
                         debt=99999)
            with self.subTest(theme=t["title"]):
                screen = menu.render(state, {"menu": "main"}, t)
                self.assertLessEqual(len(screen.encode("utf-8")), PACKET, screen)


class IconsReachThePlayerTests(unittest.TestCase):
    """The engine has its own emoji, and the menu never prints engine text --
    that is what keeps a police siren out of a game about a hall monitor. So
    the icons come through the theme, one set per theme, or they are simply
    never seen."""

    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        self.addCleanup(self._close)

    def _close(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def _buy_screen(self, pg13):
        import dopewars as game
        import dopewars_menu as menu
        import dopewars_theme as theme
        return menu.render(game.new_game(1), {"menu": "buy"}, theme.theme(pg13))

    def test_every_good_carries_an_icon_in_both_themes(self):
        import dopewars as game
        import dopewars_theme as theme
        for pg13 in (False, True):
            icons = theme.theme(pg13)["icons"]
            for item in game.GOODS:
                with self.subTest(theme=pg13, item=item):
                    self.assertTrue(icons.get(item), f"{item} has no icon")

    def test_dope_wars_uses_the_engine_icons(self):
        """His icons, unchanged -- the theme borrows them rather than
        inventing a second set that could drift."""
        import dopewars as game
        import dopewars_theme as theme
        self.assertEqual(dict(game.GOOD_ICONS), theme.theme(True)["icons"])

    def test_the_icons_are_actually_rendered(self):
        import dopewars_theme as theme
        for pg13 in (False, True):
            screen = self._buy_screen(pg13)
            with self.subTest(theme=pg13):
                for icon in theme.theme(pg13)["icons"].values():
                    self.assertIn(icon, screen)

    def test_candy_wars_shows_none_of_the_engine_emoji(self):
        """The police light in particular."""
        import dopewars as game
        import dopewars_menu as menu
        import dopewars_theme as theme
        candy = theme.theme(False)
        state = game.new_game(1)
        state.update(phase="police", enemy_hp=40, hp=9)
        screens = [menu.render(state, {"menu": "main"}, candy)]
        state = game.new_game(1)
        state["event"] = "bust:cocaine"
        screens.append(menu.render(state, {"menu": "main"}, candy))
        for screen in screens:
            with self.subTest(screen=screen[:40]):
                self.assertNotIn("\N{POLICE CARS REVOLVING LIGHT}", screen)
                for icon in game.GOOD_ICONS.values():
                    self.assertNotIn(icon, screen)


class TravelKeyTests(unittest.TestCase):
    """[3] says Travel, and T travels. The label promising a shortcut that
    did nothing is the reason this exists."""

    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        self.addCleanup(self._close)

    def _close(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def _main(self, pg13=False):
        import dopewars as game
        import dopewars_menu as menu
        import dopewars_theme as theme
        return menu.render(game.new_game(1), {"menu": "main"}, theme.theme(pg13))

    def test_the_button_says_travel(self):
        for pg13 in (False, True):
            with self.subTest(pg13=pg13):
                self.assertIn("[3]Travel", self._main(pg13))
                self.assertNotIn("[3]Go", self._main(pg13))

    def test_t_opens_the_travel_screen(self):
        import dopewars as game
        import dopewars_menu as menu
        import dopewars_theme as theme
        state = game.new_game(1)
        turn = menu._Turn(1, "caller", theme.theme(False), state)
        for word in ("t", "travel", "T"):
            with self.subTest(word=word):
                nav = menu._step(turn, word.lower(), {"menu": "main"})
                self.assertEqual("move", nav["menu"])

    def test_three_still_works(self):
        import dopewars as game
        import dopewars_menu as menu
        import dopewars_theme as theme
        state = game.new_game(1)
        turn = menu._Turn(1, "caller", theme.theme(False), state)
        self.assertEqual("move", menu._step(turn, "3", {"menu": "main"})["menu"])



class MainScreenFitsOnePacketTests(unittest.TestCase):
    """The main screen is the one a player stares at all run, so it is the
    one that must not cost two packets of airtime. "Travel" is four bytes
    more than "Go", which is close enough to the ceiling to pin down."""

    def _worst_screens(self):
        import dopewars as game
        import dopewars_menu as menu
        import dopewars_theme as theme
        for pg13 in (False, True):
            t = theme.theme(pg13)
            for day_one in (True, False):
                state = game.new_game(1)
                if not day_one:
                    # Past the opening screen the [8] line is gone, but the
                    # numbers are at their longest.
                    state.update(moves=5, days=365, loan_due=365,
                                 cash=999999, debt=99999)
                for place in game.PLACES:
                    state["place"] = place
                    for kind in ("deal", "bust"):
                        for item in t["goods"]:
                            state["event"] = f"{kind}:{item}"
                            yield (pg13, day_one, place, state["event"],
                                   menu.render(state, {"menu": "main"}, t))

    def test_every_main_screen_fits(self):
        import dopewars_menu as menu
        for pg13, day_one, place, event, screen in self._worst_screens():
            size = len(screen.encode("utf-8"))
            with self.subTest(pg13=pg13, day_one=day_one, place=place,
                              event=event, size=size):
                self.assertLessEqual(size, menu.MAX_SCREEN_BYTES)

    def test_the_title_survives(self):
        """The fit rule is a safety net, not a routine cost: no screen a
        player can actually reach should be paying for it today."""
        import dopewars_theme as theme
        for pg13, day_one, place, event, screen in self._worst_screens():
            with self.subTest(pg13=pg13, day_one=day_one, place=place,
                              event=event):
                self.assertTrue(screen.startswith(theme.theme(pg13)["title"]))

    def test_a_screen_that_would_spill_drops_the_title_first(self):
        """And when something later does push it over, the game's name is
        what goes -- not a line the player needs."""
        import dopewars as game
        import dopewars_menu as menu
        import dopewars_theme as theme
        t = dict(theme.theme(False))
        t["places"] = dict(t["places"])
        t["places"]["bronx"] = "X" * 120
        state = game.new_game(1)
        state["place"] = "bronx"
        screen = menu.render(state, {"menu": "main"}, t)
        self.assertNotIn(t["title"], screen)
        self.assertIn("[3]Travel", screen)


if __name__ == "__main__":
    unittest.main()
