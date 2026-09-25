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

MENUS = ("main", "market", "move", "gear", "bag", "loan")


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

    def _every_market_page(self, pg13):
        """The market pages, joined. Ten goods do not share one packet,
        so no single screen carries every icon any more."""
        import dopewars as game
        import dopewars_menu as menu
        import dopewars_theme as theme
        t = theme.theme(pg13)
        state = game.new_game(1)
        for item in game.GOODS:
            state["market"][item]["stock"] = 9
        pages, nav, seen = [], {"menu": "market", "start": 0}, set()
        while nav["start"] not in seen:
            seen.add(nav["start"])
            pages.append(menu.render(state, nav, t))
            nav = {"menu": "market",
                   "start": menu._market_page(state, nav, t)[1]}
        return "\n".join(pages)

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
            screens = self._every_market_page(pg13)
            with self.subTest(theme=pg13):
                for icon in theme.theme(pg13)["icons"].values():
                    self.assertIn(icon, screens)

    def test_a_space_follows_every_icon(self):
        """Several of these are drawn double-width and a couple carry a
        variation selector; butted against the name, the glyph lands on
        top of its first letter. Reported from a terminal as the
        snowflake sitting on the C of Coke."""
        import dopewars_theme as theme
        for pg13 in (False, True):
            screens = self._every_market_page(pg13)
            for item, icon in theme.theme(pg13)["icons"].items():
                with self.subTest(theme=pg13, item=item):
                    self.assertIn(icon + " ", screens)
                    self.assertNotIn(
                        icon + theme.theme(pg13)["goods"][item], screens)

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
                self.assertIn("[2]Travel", self._main(pg13))
                self.assertNotIn("]Go", self._main(pg13))

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

    def test_the_number_still_works(self):
        import dopewars as game
        import dopewars_menu as menu
        import dopewars_theme as theme
        state = game.new_game(1)
        turn = menu._Turn(1, "caller", theme.theme(False), state)
        self.assertEqual("move", menu._step(turn, "2", {"menu": "main"})["menu"])

    def test_t_travels_from_the_market_too(self):
        """Travelling lands on the market, so without this the way on
        would be 0 then T, every day of the run."""
        import dopewars as game
        import dopewars_menu as menu
        import dopewars_theme as theme
        state = game.new_game(1)
        turn = menu._Turn(1, "caller", theme.theme(False), state)
        nav = menu._step(turn, "t", {"menu": "market", "start": 0})
        self.assertEqual("move", nav["menu"])



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
        self.assertIn("[2]Travel", screen)



class MarketScreenTests(unittest.TestCase):
    """Buy and Sell were two lists of the same goods, each showing one
    number and neither saying whose. They are one Market screen now.

    The number after the slash is the shelf, "+n" is the bag, and the
    footer says so. What this class holds down is that a player's own
    circumstances -- broke, full, holding something the town does not
    stock -- never make the market look empty, which is what the two old
    screens did and what was reported from the field.
    """

    def _theme(self, pg13=False):
        import dopewars_theme as theme
        return theme.theme(pg13)

    def _market(self, state, pg13=False, note="", start=0):
        import dopewars_menu as menu
        return menu.render(state, {"menu": "market", "start": start},
                           self._theme(pg13), note=note)

    def _pages(self, state, pg13=False):
        """Every page of the market, joined -- ten goods do not share one."""
        import dopewars_menu as menu
        t = self._theme(pg13)
        out, nav, seen = [], {"menu": "market", "start": 0}, set()
        while nav["start"] not in seen:
            seen.add(nav["start"])
            out.append(menu.render(state, nav, t))
            nav = {"menu": "market", "start": menu._market_page(state, nav, t)[1]}
        return "\n".join(out)

    def _stocked_run(self):
        import dopewars as game
        state = game.new_game(12345)
        stocked = [i for i in game.GOODS if state["market"][i]["stock"]]
        self.assertTrue(stocked, "seed 12345 is expected to stock something")
        return state, stocked

    # --- the reported bug --------------------------------------------------

    def test_a_full_bag_still_shows_what_the_market_has(self):
        import dopewars as game
        state, stocked = self._stocked_run()
        state["inventory"] = {k: 0 for k in game.GOODS}
        state["inventory"]["weed"] = state["capacity"]
        screens = self._pages(state)
        for item in stocked:
            with self.subTest(item=item):
                self.assertIn(f"/{state['market'][item]['stock']}", screens)

    def test_an_empty_wallet_still_shows_what_the_market_has(self):
        state, stocked = self._stocked_run()
        state["cash"] = 0
        screens = self._pages(state)
        for item in stocked:
            with self.subTest(item=item):
                self.assertIn(f"/{state['market'][item]['stock']}", screens)

    def test_the_shelf_number_is_the_shelf_not_the_player(self):
        """The whole defect in one assertion: cash changed, the market did
        not, so not one number on the screen may move."""
        state, _stocked = self._stocked_run()
        state["cash"] = 20
        broke = self._pages(state)
        state["cash"] = 999999
        flush = self._pages(state)
        self.assertEqual(broke.replace("$20", "$X"),
                         flush.replace("$999999", "$X"))

    # --- what the rows say -------------------------------------------------

    def test_what_you_carry_is_marked(self):
        state, _stocked = self._stocked_run()
        state["inventory"]["hash"] = 7
        self.assertIn("+7", self._pages(state))

    def test_a_good_you_hold_is_listed_even_where_it_is_not_sold(self):
        """Otherwise there is no way to reach it to sell it."""
        import dopewars as game
        state, _stocked = self._stocked_run()
        for item in game.GOODS:
            state["market"][item]["stock"] = 0
        state["inventory"]["heroin"] = 4
        screens = self._pages(state)
        t = self._theme()
        self.assertIn(t["goods"]["heroin"], screens)
        self.assertIn("/out +4", screens)

    def test_a_good_that_is_neither_stocked_nor_held_is_left_out(self):
        """Ten rows do not fit; the ones worth a keypress do."""
        import dopewars as game
        state, _stocked = self._stocked_run()
        for item in game.GOODS:
            state["market"][item]["stock"] = 0
            state["inventory"][item] = 0
        state["market"]["weed"]["stock"] = 5
        screens = self._pages(state)
        t = self._theme()
        self.assertIn(t["goods"]["weed"], screens)
        self.assertNotIn(t["goods"]["cocaine"], screens)

    def test_the_slash_and_the_plus_are_labelled(self):
        import dopewars_menu as menu
        state, _stocked = self._stocked_run()
        screen = self._market(state)
        self.assertTrue(screen.endswith(menu._FOOTER)
                        or screen.endswith(menu._FOOTER_MORE), screen)
        self.assertIn("stock", menu._FOOTER)
        self.assertIn("bag", menu._FOOTER)

    def test_the_header_counts_the_bag(self):
        state, _stocked = self._stocked_run()
        state["inventory"]["hash"] = 7
        self.assertIn(f"bag 7/{state['capacity']}", self._market(state))

    def test_an_empty_town_with_an_empty_bag_says_so(self):
        import dopewars as game
        state, _stocked = self._stocked_run()
        for item in game.GOODS:
            state["market"][item]["stock"] = 0
            state["inventory"][item] = 0
        self.assertIn("Nothing on the shelf", self._market(state))

    # --- paging ------------------------------------------------------------

    def test_every_good_is_reachable_across_the_pages(self):
        import dopewars as game
        state, _stocked = self._stocked_run()
        for item in game.GOODS:
            state["market"][item]["stock"] = 9
        screens = self._pages(state)
        t = self._theme()
        for item in game.GOODS:
            with self.subTest(item=item):
                self.assertIn(f"{t['goods'][item]} $", screens)

    def test_a_page_with_more_behind_it_offers_more(self):
        import dopewars as game
        import dopewars_menu as menu
        state, _stocked = self._stocked_run()
        for item in game.GOODS:
            state["market"][item]["stock"] = 9
        self.assertIn("[M]ore", self._market(state))
        # And paging comes back round rather than dead-ending.
        nav, seen = {"menu": "market", "start": 0}, []
        for _ in range(6):
            seen.append(nav["start"])
            nav = {"menu": "market",
                   "start": menu._market_page(state, nav, self._theme())[1]}
            if nav["start"] == 0:
                break
        self.assertEqual(0, nav["start"], "paging never returns to the first page")

    def test_the_numbers_are_the_catalogue_not_the_page(self):
        """[4] is the same good in every town and on every page, including a
        page it is not printed on -- so a player can type it either way."""
        import dopewars as game
        import dopewars_menu as menu
        state, _stocked = self._stocked_run()
        for item in game.GOODS:
            state["market"][item]["stock"] = 9
        t = self._theme()
        turn = menu._Turn(1, "caller", t, state)
        for index, item in enumerate(game.GOODS, start=1):
            with self.subTest(item=item):
                nav = menu._step(turn, str(index),
                                 {"menu": "market", "start": 0})
                self.assertEqual(item, nav["item"])

    # --- the item screen ---------------------------------------------------

    def test_the_item_screen_offers_both_sides(self):
        import dopewars_menu as menu
        state, stocked = self._stocked_run()
        screen = menu.render(state, {"menu": "item", "item": stocked[0]},
                             self._theme())
        self.assertIn("[1]Buy", screen)
        self.assertIn("[2]Sell", screen)
        self.assertIn("Shelf", screen)
        self.assertIn("bag", screen)

    def test_buying_something_you_cannot_says_which_limit(self):
        import dopewars as game
        import dopewars_menu as menu
        state, stocked = self._stocked_run()
        t = self._theme()
        turn = menu._Turn(1, "caller", t, state)
        item = stocked[0]

        state["cash"] = 0
        with self.assertRaises(menu._Stop) as broke:
            menu._step(turn, "1", {"menu": "item", "item": item})
        self.assertIn("money", str(broke.exception))

        state["cash"] = 999999
        state["inventory"] = {k: 0 for k in game.GOODS}
        state["inventory"]["weed"] = state["capacity"]
        with self.assertRaises(menu._Stop) as full:
            menu._step(turn, "1", {"menu": "item", "item": item})
        self.assertIn("full", str(full.exception))

        state["inventory"] = {k: 0 for k in game.GOODS}
        state["market"][item]["stock"] = 0
        with self.assertRaises(menu._Stop) as gone:
            menu._step(turn, "1", {"menu": "item", "item": item})
        self.assertIn("sold out", str(gone.exception))

    def test_selling_what_you_do_not_have_is_refused(self):
        import dopewars_menu as menu
        state, stocked = self._stocked_run()
        turn = menu._Turn(1, "caller", self._theme(), state)
        state["inventory"][stocked[0]] = 0
        with self.assertRaises(menu._Stop):
            menu._step(turn, "2", {"menu": "item", "item": stocked[0]})

    # --- the budget --------------------------------------------------------

    def _reachable_notes(self, t):
        import dopewars as game
        return [""] + ["Pick 1-10.", "Your bag is full.", "Not enough money.",
                       "That didn't work."] + [
            f"{t['goods'][i]} is sold out." for i in game.GOODS] + [
            f"You have no {t['goods'][i]}." for i in game.GOODS]

    def test_every_market_page_fits_one_packet(self):
        """Ten goods, both themes, an empty/part/full bag, every refusal
        that can sit above, and every page of each."""
        import dopewars as game
        import dopewars_menu as menu
        import dopewars_theme as theme
        worst, worst_screen = 0, ""
        for seed in range(200):
            market = game.new_game(seed)["market"]
            for carried in (0, 4, "one kind"):
                for cash in (0, 2400, 999999):
                    state = game.new_game(seed)
                    state["market"], state["cash"] = market, cash
                    if carried == "one kind":
                        state["inventory"] = {k: 0 for k in game.GOODS}
                        state["inventory"]["weed"] = state["capacity"]
                    else:
                        for item in game.GOODS:
                            state["inventory"][item] = carried
                    for pg13 in (False, True):
                        t = theme.theme(pg13)
                        nav, seen = {"menu": "market", "start": 0}, set()
                        while nav["start"] not in seen:
                            seen.add(nav["start"])
                            for note in self._reachable_notes(t):
                                screen = menu.render(state, nav, t, note=note)
                                size = len(screen.encode("utf-8"))
                                if size > worst:
                                    worst, worst_screen = size, screen
                            nav = {"menu": "market",
                                   "start": menu._market_page(state, nav, t)[1]}
        self.assertLessEqual(worst, menu.MAX_SCREEN_BYTES,
                             f"{worst} bytes:\n{worst_screen}")

    def test_a_crowded_page_keeps_the_way_out_and_the_way_on(self):
        """When a note plus a full page would spill, the legend goes -- but
        [0]Back and [M]ore are the only ways off the screen."""
        import dopewars as game
        import dopewars_menu as menu
        state, _stocked = self._stocked_run()
        state["cash"] = 999999
        for item in game.GOODS:
            state["market"][item].update(price=6246, stock=40)
            state["inventory"][item] = 4
        screen = self._market(state, note="Rock candy is sold out.")
        self.assertLessEqual(len(screen.encode("utf-8")), menu.MAX_SCREEN_BYTES)
        self.assertIn("[0]Back", screen)
        self.assertIn("[M]ore", screen)


if __name__ == "__main__":
    unittest.main()
