"""Doors: the curated services that replaced Web Fetch.

Two things are being protected here.

The first is the registry itself. A door is data plus a parser, and the ways
it can be written wrongly are all silent: a template with a ``{arg}`` nobody
prompts for, a prompt for an argument the URL never uses, a host that quietly
became http. Those are checked across every door at once, so a new entry
cannot ship broken.

The second is each parser's reading of a real reply. The fixtures are trimmed
captures of what the live services actually sent on 2026-09-18 -- shapes, not
inventions. No test here touches the network: ``_fetch`` is the seam.
"""

import io
import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import services


class RegistryTests(unittest.TestCase):
    """What every door must be, whoever adds the next one."""

    def test_every_door_is_complete(self):
        for door_id, door in services.DOORS.items():
            with self.subTest(door=door_id):
                self.assertTrue(door.get('name'))
                self.assertTrue(door.get('group'))
                self.assertTrue(callable(door.get('parse')))
                self.assertTrue(door.get('url') or door.get('config_url'))

    def test_a_prompt_and_a_placeholder_always_agree(self):
        """A door that asks for a word and then never puts it in the URL
        answers the same thing to everyone; one that needs a word and never
        asks fails on every call. Both are invisible without this."""
        for door_id, door in services.DOORS.items():
            template = door.get('url') or ''
            if not template:
                continue  # operator-configured, checked when it is set
            with self.subTest(door=door_id):
                self.assertEqual(bool(door.get('arg')), '{arg}' in template)

    def test_every_bundled_door_is_https(self):
        for door_id, door in services.DOORS.items():
            if door.get('url'):
                with self.subTest(door=door_id):
                    self.assertTrue(door['url'].startswith('https://'))

    def test_group_order_covers_every_group(self):
        self.assertEqual(set(services.GROUP_ORDER),
                         {d['group'] for d in services.DOORS.values()})

    def test_door_ids_are_listed_grouped(self):
        """The menu shows one group at a time, so the ids have to arrive
        grouped or a group's doors end up split across two screens."""
        seen = []
        for door_id in services.door_ids():
            group = services.DOORS[door_id]['group']
            if group not in seen:
                seen.append(group)
            self.assertEqual(seen[-1], group, f"{door_id} breaks its group run")

    def test_hosts_are_derived_from_the_templates(self):
        hosts = services.door_hosts()
        self.assertIn('wttr.in', hosts)
        self.assertIn('en.wikipedia.org', hosts)
        self.assertNotIn('', hosts)


class ParserTests(unittest.TestCase):
    """Each parser against the shape its service really sends."""

    def test_wttr_one_liner_passes_through(self):
        body = "37402: Sunny, +89°F (feels +91°F), wind ↓3mph, hum 47%\n"
        self.assertEqual(services._one_line(body),
                         "37402: Sunny, +89°F (feels +91°F), wind ↓3mph, hum 47%")

    def test_wttr_says_so_when_the_place_is_unknown(self):
        with self.assertRaises(services.DoorError):
            services._one_line("Unknown location; please try ~37402")

    def test_three_day_forecast_names_the_place_and_three_days(self):
        body = ('{"nearest_area":[{"areaName":[{"value":"Chattanooga"}]}],'
                '"weather":[{"date":"2026-09-18","mintempF":"72","maxtempF":"94",'
                '"hourly":[{},{},{},{},{"weatherDesc":[{"value":"Sunny"}]}]},'
                '{"date":"2026-09-19","mintempF":"70","maxtempF":"95",'
                '"hourly":[{},{},{},{},{"weatherDesc":[{"value":"Clear"}]}]},'
                '{"date":"2026-09-20","mintempF":"69","maxtempF":"91",'
                '"hourly":[{},{},{},{},{"weatherDesc":[{"value":"Rain"}]}]}]}')
        text = services._parse_wx3(body)
        self.assertIn("Chattanooga 3-day:", text)
        self.assertIn("2026-09-18 72-94F Sunny", text)
        self.assertEqual(len(text.splitlines()), 4)

    def test_alerts_name_the_event_and_trim_the_county_list(self):
        body = ('{"features":[{"properties":{"event":"Heat Advisory",'
                '"areaDesc":"Clay; Greene; Sumner; Wilson; Davidson"}}]}')
        text = services._parse_alerts(body)
        self.assertIn("Heat Advisory: Clay, Greene", text)
        self.assertNotIn("Davidson", text)

    def test_no_alerts_is_an_answer_not_an_error(self):
        self.assertEqual(services._parse_alerts('{"features":[]}'),
                         "No active NWS alerts.")

    def test_quakes_are_reported_with_how_long_ago(self):
        import time
        body = ('{"features":[{"properties":{"mag":5.2,"place":"near Sola",'
                '"time":%d}}]}' % int((time.time() - 7200) * 1000))
        text = services._parse_quake(body)
        self.assertIn("M5.2 near Sola", text)
        self.assertIn("2h ago", text)

    def test_no_quakes_is_an_answer_not_an_error(self):
        self.assertIn("No quakes", services._parse_quake('{"features":[]}'))

    def test_tides_read_as_high_and_low(self):
        body = ('{"predictions":[{"t":"2026-09-18 02:04","v":"3.924","type":"H"},'
                '{"t":"2026-09-18 07:47","v":"1.596","type":"L"}]}')
        text = services._parse_tide(body)
        self.assertIn("High 2026-09-18 02:04 3.924ft", text)
        self.assertIn("Low 2026-09-18 07:47 1.596ft", text)

    def test_an_unknown_tide_station_says_so(self):
        with self.assertRaises(services.DoorError):
            services._parse_tide('{"error":{"message":"No station found"}}')

    def test_solar_reports_the_indices_and_the_bands(self):
        body = ('<solar><solardata><solarflux>97</solarflux><sunspots>0</sunspots>'
                '<aindex>9</aindex><kindex>0</kindex>'
                '<geomagfield>INACTIVE</geomagfield><calculatedconditions>'
                '<band name="80m-40m" time="day">Good</band>'
                '<band name="80m-40m" time="night">Fair</band>'
                '</calculatedconditions></solardata></solar>')
        text = services._parse_solar(body)
        self.assertIn("SFI 97 SN 0 A 9 K 0 (inactive)", text)
        self.assertIn("80m-40m Good", text)
        self.assertNotIn("Fair", text)  # night conditions are a different question

    def test_kp_reads_the_object_shape(self):
        body = ('[{"time_tag":"2026-09-18T06:00:00","Kp":2.00},'
                '{"time_tag":"2026-09-18T09:00:00","Kp":1.67},'
                '{"time_tag":"2026-09-18T12:00:00","Kp":0.67}]')
        text = services._parse_kp(body)
        self.assertIn("Kp 0.67 quiet", text)
        self.assertIn("2026-09-18 12:00 UTC", text)
        self.assertIn("last 3: 2, 1.67, 0.67", text)

    def test_kp_reads_the_array_shape_with_its_header_row(self):
        body = ('[["time_tag","Kp","a_running"],'
                '["2026-09-18T09:00:00","4.33","20"],'
                '["2026-09-18T12:00:00","5.67","40"]]')
        text = services._parse_kp(body)
        self.assertIn("Kp 5.67", text)
        self.assertIn("storm", text)

    def test_kp_translates_the_number_into_a_sentence(self):
        """"Kp 6" means nothing to most people; "storm" does."""
        def kp_at(value):
            return services._parse_kp('[{"time_tag":"2026-09-18T12:00:00","Kp":%s}]' % value)
        self.assertIn("quiet", kp_at(1))
        self.assertIn("active", kp_at(4.33))
        self.assertIn("storm", kp_at(6))

    def test_the_space_weather_outlook_returns_its_rationale(self):
        body = ("Product: 3-Day Forecast\r\n\r\nA. NOAA Geomagnetic Activity\r\n\r\n"
                "Rationale: Isolated unsettled periods are expected on 18 Sep\r\n"
                "due to CH HSS influences.\r\n\r\n")
        text = services._parse_forecast_text(body)
        self.assertIn("Isolated unsettled periods", text)
        self.assertIn("CH HSS influences.", text)
        self.assertNotIn("\r", text)

    def test_the_iss_position_is_rounded_to_something_sayable(self):
        body = ('{"latitude":-21.63274,"longitude":147.2481,'
                '"altitude":423.117,"velocity":27571.4}')
        self.assertEqual(services._parse_iss(body),
                         "ISS -21.6, 147.2 — 423km up, 27571km/h")

    def test_a_wiki_summary_leads_with_its_title(self):
        body = '{"title":"Meshtastic","extract":"Meshtastic is a LoRa-based mesh."}'
        self.assertEqual(services._parse_wiki(body),
                         "Meshtastic: Meshtastic is a LoRa-based mesh.")

    def test_a_missing_article_says_so(self):
        with self.assertRaises(services.DoorError):
            services._parse_wiki('{"type":"https://mediawiki.org/wiki/HyperSwitch/errors/not_found"}')

    def test_a_definition_carries_its_part_of_speech(self):
        body = ('[{"word":"radio","meanings":[{"partOfSpeech":"noun",'
                '"definitions":[{"definition":"The technology."},'
                '{"definition":"Ignored, only the first is sent."}]}]}]')
        text = services._parse_definition(body)
        self.assertIn("radio: (noun) The technology.", text)
        self.assertNotIn("Ignored", text)

    def test_an_unknown_word_says_so(self):
        # dictionaryapi answers a miss with an object, not a list.
        with self.assertRaises(services.DoorError):
            services._parse_definition('{"title":"No Definitions Found"}')

    def test_a_callsign_returns_the_licence_facts(self):
        body = ('{"status":"VALID","name":"ARRL HQ OPERATORS CLUB",'
                '"current":{"callsign":"W1AW","operClass":"CLUB"},'
                '"location":{"gridsquare":"FN31pr"},'
                '"address":{"line2":"NEWINGTON, CT 06111"},'
                '"otherInfo":{"expiryDate":"02/26/2031"}}')
        text = services._parse_callsign(body)
        self.assertIn("W1AW", text)
        self.assertIn("Arrl Hq Operators Club", text)
        self.assertIn("FN31pr", text)
        self.assertIn("exp 02/26/2031", text)

    def test_an_invalid_callsign_says_so(self):
        with self.assertRaises(services.DoorError):
            services._parse_callsign('{"status":"INVALID"}')

    def test_rates_are_one_line(self):
        body = '{"base_code":"USD","rates":{"EUR":0.8712,"GBP":0.7533,"ZZZ":1}}'
        text = services._parse_rates(body)
        self.assertEqual("1 USD = EUR 0.87, GBP 0.75", text)

    def test_the_time_drops_the_seconds(self):
        body = '{"timeZone":"America/New_York","dateTime":"2026-09-18T12:42:07.123"}'
        self.assertEqual(services._parse_time(body),
                         "America/New_York: 2026-09-18 12:42")

    def test_a_feed_returns_titles_and_nothing_else(self):
        body = ('<rss><channel>'
                '<item><title>First story</title><description>A whole article</description></item>'
                '<item><title>Second story</title></item>'
                '</channel></rss>')
        text = services._parse_rss_titles(body)
        self.assertEqual("- First story\n- Second story", text)
        self.assertNotIn("whole article", text)

    def test_an_atom_feed_reads_the_same_way(self):
        body = ('<feed xmlns="http://www.w3.org/2005/Atom">'
                '<title>The feed itself, not an item</title>'
                '<entry><title>An entry</title></entry></feed>')
        self.assertEqual(services._parse_rss_titles(body), "- An entry")

    def test_unreadable_json_is_a_door_error_not_a_crash(self):
        with self.assertRaises(services.DoorError):
            services._loads("<html>502 Bad Gateway</html>")


class RunDoorTests(unittest.TestCase):
    """The wrapper: arguments in, one short answer or one plain refusal out."""

    def setUp(self):
        services._reset_cache()
        self.addCleanup(services._reset_cache)

    def _run(self, door_id, arg='', body='', **kwargs):
        with mock.patch.object(services, '_fetch', return_value=body) as fetch:
            result = services.run_door(door_id, arg, **kwargs)
        self.fetched = fetch.call_args[0][0] if fetch.call_args else None
        return result

    def test_an_unknown_door_is_refused_without_a_call(self):
        status, text = services.run_door('nosuchdoor')
        self.assertEqual("ERR", status)
        self.assertIn("nosuchdoor", text)

    def test_the_argument_is_url_quoted(self):
        self._run('wiki', 'Ada Lovelace', body='{"title":"A","extract":"B"}')
        self.assertIn("Ada%20Lovelace", self.fetched)

    def test_a_slash_in_the_argument_cannot_change_the_path(self):
        """quote(safe='') -- otherwise 'x/../../admin' walks the URL."""
        self._run('wiki', '../../etc/passwd', body='{"title":"A","extract":"B"}')
        self.assertNotIn("/../", self.fetched)
        self.assertIn("%2F", self.fetched)

    def test_control_characters_are_stripped_from_the_argument(self):
        self._run('wiki', "Ada\r\nX-Evil: 1", body='{"title":"A","extract":"B"}')
        self.assertNotIn("%0D", self.fetched.upper())

    def test_a_very_long_argument_is_cut_to_size(self):
        self._run('wiki', 'A' * 500, body='{"title":"A","extract":"B"}')
        self.assertLessEqual(self.fetched.count('A'), services.MAX_ARG_LENGTH + 10)

    def test_a_door_that_needs_an_argument_says_so_when_it_has_none(self):
        status, text = services.run_door('wiki', '')
        self.assertEqual("ERR", status)
        self.assertIn("look up", text)

    def test_an_answer_is_cut_to_the_reply_budget(self):
        status, text = self._run('wiki', 'X', max_bytes=40,
                                 body='{"title":"T","extract":"%s"}' % ('long ' * 50))
        self.assertEqual("200", status)
        self.assertLessEqual(len(text.encode('utf-8')), 40)
        self.assertTrue(text.endswith("…"))

    def test_a_cached_answer_does_not_call_out_again(self):
        with mock.patch.object(services, '_fetch',
                               return_value='{"title":"T","extract":"E"}') as fetch:
            first = services.run_door('wiki', 'Meshtastic')
            second = services.run_door('wiki', 'Meshtastic')
        self.assertEqual(first, second)
        self.assertEqual(1, fetch.call_count)

    def test_a_different_argument_is_a_different_answer(self):
        with mock.patch.object(services, '_fetch',
                               return_value='{"title":"T","extract":"E"}') as fetch:
            services.run_door('wiki', 'One')
            services.run_door('wiki', 'Two')
        self.assertEqual(2, fetch.call_count)

    def test_a_404_reads_as_nothing_found(self):
        import urllib.error
        error = urllib.error.HTTPError('u', 404, 'Not Found', {}, None)
        with mock.patch.object(services, '_fetch', side_effect=error):
            status, text = services.run_door('wiki', 'Zzqq')
        self.assertEqual("ERR", status)
        self.assertIn("nothing found for 'Zzqq'", text)

    def test_a_400_from_a_door_with_an_argument_reads_the_same_way(self):
        """NWS answers a bad state code with 400. "Not answering right now"
        sent people back to retry a URL that would never work."""
        import urllib.error
        error = urllib.error.HTTPError('u', 400, 'Bad Request', {}, None)
        with mock.patch.object(services, '_fetch', side_effect=error):
            status, text = services.run_door('alerts', 'ZZ')
        self.assertIn("nothing found for 'ZZ'", text)

    def test_a_400_from_a_door_with_no_argument_is_the_services_fault(self):
        import urllib.error
        error = urllib.error.HTTPError('u', 400, 'Bad Request', {}, None)
        with mock.patch.object(services, '_fetch', side_effect=error):
            status, text = services.run_door('quake')
        self.assertIn("not answering right now (400)", text)

    def test_an_unreachable_service_never_leaks_a_traceback(self):
        with mock.patch.object(services, '_fetch', side_effect=OSError("timed out")):
            status, text = services.run_door('quake')
        self.assertEqual("ERR", status)
        self.assertNotIn("Traceback", text)
        self.assertNotIn("timed out", text)
        self.assertIn("could not be reached", text)

    def test_a_parser_saying_no_reaches_the_user_in_its_own_words(self):
        status, text = self._run('tide', '999',
                                 body='{"error":{"message":"No station found"}}')
        self.assertEqual("ERR", status)
        self.assertIn("No station found", text)

    def test_a_shape_change_upstream_is_reported_as_ours_not_theirs(self):
        """A parser crashing means the service changed. The user is told
        something true; the detail goes to the log, where it can be fixed."""
        with mock.patch.dict(services.DOORS['quake'],
                             {'parse': lambda body: 1 / 0}):
            status, text = self._run('quake', body='{}')
        self.assertEqual("ERR", status)
        self.assertIn("could not read", text)

    def test_a_failure_is_never_cached(self):
        """Otherwise one blip silences a door for the whole cache window."""
        import urllib.error
        error = urllib.error.HTTPError('u', 503, 'Down', {}, None)
        with mock.patch.object(services, '_fetch', side_effect=error):
            services.run_door('quake')
        with mock.patch.object(services, '_fetch',
                               return_value='{"features":[]}') as fetch:
            status, _ = services.run_door('quake')
        self.assertEqual("200", status)
        self.assertEqual(1, fetch.call_count)

    def test_the_request_names_this_bbs(self):
        """NWS requires a User-Agent and answers 403 without one."""
        captured = {}

        class _Resp:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

            def read(self_inner, _n):
                return b'{"features":[]}'

        def _urlopen(req, timeout=None):
            captured['ua'] = req.get_header('User-agent')
            return _Resp()

        with mock.patch.object(services.urllib.request, 'urlopen', _urlopen):
            services.run_door('quake')
        self.assertIn("BaconBBS", captured['ua'])


class GatewayDispatchTests(unittest.TestCase):
    """The gateway hands a door request to the registry, and only to a peer
    that understands one."""

    def test_kind_d_opens_a_door(self):
        import gateway
        replies = []
        with mock.patch.object(services, 'run_door',
                               return_value=("200", "sunny")) as run:
            gateway.handle_apireq('r1', '!abc', 'd', "wx\x1f37402", None,
                                  lambda status, body: replies.append((status, body)))
            for thread in __import__('threading').enumerate():
                if thread.name == 'apigw-r1':
                    thread.join(5)
        run.assert_called_once_with('wx', '37402')
        self.assertEqual([("200", "sunny")], replies)

    def test_a_door_is_never_sent_to_a_gateway_that_predates_doors(self):
        """An old peer advertises 'apigw', would accept the request, and
        would answer "unknown relay target" a minute later over the radio."""
        import types
        import utils
        interface = types.SimpleNamespace(bbs_nodes=['!old'])
        with mock.patch.object(utils, 'peers_all_support',
                               side_effect=lambda peers, cap: cap == 'apigw'):
            self.assertIsNone(utils.select_gateway_peer(interface, 'door'))
            self.assertEqual('!old', utils.select_gateway_peer(interface))


class FeedUrlTests(unittest.TestCase):
    """The URL an operator types, made into something fetchable."""

    def setUp(self):
        import web_admin
        self.web_admin = web_admin

    def test_a_feed_pasted_without_a_scheme_still_works(self):
        """People paste a feed the way they read it. A URL with no scheme is
        not fetched, it is refused, and on a radio that reads as the door
        being broken."""
        self.assertEqual("https://feeds.npr.org/1001/rss.xml",
                         self.web_admin._normalise_feed_url("feeds.npr.org/1001/rss.xml"))

    def test_surrounding_whitespace_is_forgiven(self):
        self.assertEqual("https://a.example/f.xml",
                         self.web_admin._normalise_feed_url("  https://a.example/f.xml  "))

    def test_both_web_schemes_are_kept_as_typed(self):
        for url in ("https://a.example/f", "http://a.example/f"):
            self.assertEqual(url, self.web_admin._normalise_feed_url(url))

    def test_a_scheme_that_is_not_the_web_is_refused(self):
        """Left alone, prefixing https:// to "javascript:alert(1)" turns a
        scheme into a hostname and stores something fetchable-looking."""
        for url in ("javascript:alert(1)", "file:///etc/passwd", "ftp://a.example/f"):
            with self.subTest(url=url):
                self.assertEqual("", self.web_admin._normalise_feed_url(url))

    def test_something_that_is_not_a_url_is_refused(self):
        for raw in ("", "   ", "not a url", "localhost/feed", "https:// spaced.example/f"):
            with self.subTest(raw=raw):
                self.assertEqual("", self.web_admin._normalise_feed_url(raw))

    def test_a_feed_is_read_before_it_is_accepted(self):
        """A feed that does not parse is refused in the browser, with the
        first headline as proof when it does."""
        good = ('<rss><channel><item><title>A headline</title></item>'
                '</channel></rss>')
        with mock.patch.object(services, '_fetch', return_value=good):
            ok, detail = self.web_admin._check_feed_url("https://a.example/f")
        self.assertTrue(ok)
        self.assertEqual("A headline", detail)

        with mock.patch.object(services, '_fetch', return_value="<html>nope</html>"):
            ok, detail = self.web_admin._check_feed_url("https://a.example/f")
        self.assertFalse(ok)
        self.assertIn("not a readable", detail)

        with mock.patch.object(services, '_fetch', side_effect=OSError("boom")):
            ok, detail = self.web_admin._check_feed_url("https://a.example/f")
        self.assertFalse(ok)
        self.assertIn("could not fetch", detail)


class FeedOwnershipTests(unittest.TestCase):
    """Who may change a feed, and what everyone else can do instead."""

    ME = '!aaaa1111'
    THEM = '!bbbb2222'

    def setUp(self):
        import tempfile
        folder = tempfile.mkdtemp()
        patcher = mock.patch.dict(os.environ,
                                  {'BBS_DB_PATH': os.path.join(folder, 'feeds.db')})
        patcher.start()
        self.addCleanup(patcher.stop)
        import db_operations
        db_operations.close_db_connection() if hasattr(
            db_operations, 'close_db_connection') else None
        self.db = db_operations
        self.db.initialize_database()
        self.addCleanup(self._drop_connection)

    def _drop_connection(self):
        try:
            self.db.get_db_connection().close()
        except Exception:
            pass

    def test_a_feed_can_be_edited_by_the_node_that_added_it(self):
        feed_id = self.db.save_feed('NPR', 'https://a.example/f', 'World', self.ME)
        self.assertTrue(feed_id)
        self.assertEqual(feed_id,
                         self.db.save_feed('NPR News', 'https://a.example/f2',
                                           'World', self.ME, feed_id))
        self.assertEqual('NPR News', self.db.get_feed(feed_id)['name'])

    def test_another_node_cannot_edit_or_retire_it(self):
        """The ownership rule, enforced in the data layer so the web form
        and the sync path cannot disagree about it."""
        feed_id = self.db.save_feed('NPR', 'https://a.example/f', 'World', self.ME)
        self.assertEqual('', self.db.save_feed('Hijacked', 'https://evil.example/f',
                                               'World', self.THEM, feed_id))
        self.assertFalse(self.db.delete_feed(feed_id, self.THEM))
        self.assertEqual('NPR', self.db.get_feed(feed_id)['name'])

    def test_what_another_node_can_do_is_hide_it(self):
        feed_id = self.db.save_feed('Theirs', 'https://a.example/f', 'World', self.THEM)
        self.assertIn(feed_id, [f['feed_id'] for f in
                                self.db.list_feeds(include_blocked=False)])
        self.assertTrue(self.db.set_feed_blocked(feed_id, True))
        self.assertNotIn(feed_id, [f['feed_id'] for f in
                                   self.db.list_feeds(include_blocked=False)])
        # ...and the feed itself is untouched, so the fleet still has it.
        self.assertEqual('Theirs', self.db.get_feed(feed_id)['name'])
        self.assertTrue(self.db.set_feed_blocked(feed_id, False))
        self.assertIn(feed_id, [f['feed_id'] for f in
                                self.db.list_feeds(include_blocked=False)])

    def test_retiring_leaves_a_tombstone_so_the_delete_can_travel(self):
        """Without the row, the next peer to sync hands the feed back."""
        feed_id = self.db.save_feed('NPR', 'https://a.example/f', 'World', self.ME)
        self.assertTrue(self.db.delete_feed(feed_id, self.ME))
        self.assertNotIn(feed_id, [f['feed_id'] for f in self.db.list_feeds()])
        self.assertIn(feed_id, [f['feed_id'] for f in self.db.get_feeds_for_sync()])

    def test_a_peer_may_only_change_the_feeds_it_authored(self):
        feed_id = self.db.save_feed('Mine', 'https://a.example/f', 'World', self.ME)
        later = '2099-01-01T00:00:00.000000+00:00'
        self.assertFalse(self.db.apply_synced_feed(
            feed_id, 'Stolen', 'https://evil.example/f', 'World',
            self.THEM, later, False, self.THEM))
        self.assertEqual('Mine', self.db.get_feed(feed_id)['name'])

    def test_the_author_may_change_it_from_anywhere(self):
        feed_id = self.db.save_feed('Mine', 'https://a.example/f', 'World', self.ME)
        later = '2099-01-01T00:00:00.000000+00:00'
        self.assertTrue(self.db.apply_synced_feed(
            feed_id, 'Renamed', 'https://a.example/f', 'World',
            self.ME, later, False, self.THEM))
        self.assertEqual('Renamed', self.db.get_feed(feed_id)['name'])

    def test_an_older_stamp_cannot_undo_a_newer_edit(self):
        """A node that was offline through an edit must not resurrect what
        it still remembers."""
        feed_id = self.db.save_feed('Current', 'https://a.example/f', 'World', self.ME)
        self.assertFalse(self.db.apply_synced_feed(
            feed_id, 'Stale', 'https://a.example/old', 'World',
            self.ME, '2000-01-01T00:00:00.000000+00:00', False, self.ME))
        self.assertEqual('Current', self.db.get_feed(feed_id)['name'])

    def test_a_retirement_travels_as_an_update(self):
        feed_id = self.db.save_feed('Theirs', 'https://a.example/f', 'World', self.THEM)
        later = '2099-01-01T00:00:00.000000+00:00'
        self.assertTrue(self.db.apply_synced_feed(
            feed_id, 'Theirs', 'https://a.example/f', 'World',
            self.THEM, later, True, self.THEM))
        self.assertNotIn(feed_id, [f['feed_id'] for f in self.db.list_feeds()])

    def test_a_feed_with_no_author_is_refused(self):
        """It could never be edited or retired again."""
        self.assertEqual('', self.db.save_feed('Orphan', 'https://a.example/f',
                                               'World', ''))

    def test_names_and_categories_are_trimmed_to_fit_a_radio_menu(self):
        feed_id = self.db.save_feed('N' * 200, 'https://a.example/f',
                                    'C' * 200, self.ME)
        feed = self.db.get_feed(feed_id)
        self.assertEqual(self.db.FEED_NAME_MAX_LENGTH, len(feed['name']))
        self.assertEqual(self.db.FEED_CATEGORY_MAX_LENGTH, len(feed['category']))

    def test_a_newline_cannot_be_smuggled_into_a_menu(self):
        feed_id = self.db.save_feed('Real\n[9] Fake', 'https://a.example/f',
                                    'World', self.ME)
        self.assertNotIn('\n', self.db.get_feed(feed_id)['name'])


class FeedMenuTests(unittest.TestCase):
    """Sub-grouping: a level that appears only once it is earned."""

    def _doors(self, entries):
        """Patch in a set of feed doors without touching the database."""
        return mock.patch.object(services, '_feed_doors', return_value=entries)

    def _feed(self, name, category):
        return {'name': name, 'group': 'News', 'category': category,
                'url': 'https://a.example/f', 'parse': services._parse_rss_titles,
                'cache': 900}

    def test_one_category_is_not_worth_a_screen(self):
        """Hacker News ships filed under Tech, so a single feed filed the
        same way leaves News a flat list."""
        with self._doors({'feed:1': self._feed('NPR', 'Tech')}):
            self.assertEqual([], services.categories_in_group('News'))

    def test_a_second_category_earns_the_screen(self):
        with self._doors({'feed:1': self._feed('NPR', 'World')}):
            self.assertEqual(['Tech', 'World'], services.categories_in_group('News'))

    def test_categories_are_alphabetical_with_the_catch_all_last(self):
        with self._doors({'feed:1': self._feed('NPR', 'World'),
                          'feed:2': self._feed('ARRL', 'Ham radio'),
                          'feed:3': self._feed('Local paper', '')}):
            self.assertEqual(['Ham radio', 'Tech', 'World', 'Other'],
                             services.categories_in_group('News'))

    def test_an_unfiled_door_is_still_reachable(self):
        """The bug this bucket exists for: with categories in play, the
        category screen is the only way in, so a door filed nowhere would
        have no way to be reached at all."""
        with self._doors({'feed:1': self._feed('NPR', 'World'),
                          'feed:2': self._feed('Local paper', '')}):
            reachable = []
            for category in services.categories_in_group('News'):
                reachable += services.doors_in_category('News', category)
            self.assertIn('feed:2', reachable)
            self.assertIn('hn', reachable)

    def test_a_group_with_no_categories_keeps_two_levels(self):
        self.assertEqual([], services.categories_in_group('Weather & Safety'))

    def test_a_feed_cannot_shadow_a_bundled_door(self):
        """Feed ids are namespaced, so a feed called wx does not replace
        the weather."""
        with self._doors({'feed:wx': self._feed('wx', 'World')}):
            self.assertEqual('Weather now', services.all_doors()['wx']['name'])

    def test_a_feed_door_runs_as_a_feed(self):
        body = '<rss><channel><item><title>Headline</title></item></channel></rss>'
        with self._doors({'feed:1': self._feed('NPR', 'World')}), \
                mock.patch.object(services, '_fetch', return_value=body):
            services._reset_cache()
            status, text = services.run_door('feed:1')
        self.assertEqual("200", status)
        self.assertEqual("- Headline", text)


class FeedWireTests(unittest.TestCase):
    """The frame that carries a feed between nodes."""

    def _frame_for(self, name, url, category):
        import utils
        feed = {'feed_id': 'abc123', 'name': name, 'url': url,
                'category': category, 'author_node_id': '!aaaa1111',
                'updated_at': '2026-09-19T20:00:00.000000+00:00', 'deleted': False}
        sent = []
        with mock.patch.object(utils, '_send_one_sync',
                               side_effect=lambda msg, *a, **k: sent.append(msg)), \
                mock.patch('db_operations.peer_supports', return_value=True):
            utils.send_feed_to_bbs_nodes(feed, ['!peer'], object())
        return sent[0]

    def test_a_pipe_in_a_name_cannot_split_the_frame(self):
        """Name, URL and category are operator free text, and '|' is the
        field separator."""
        import utils
        frame = self._frame_for('NPR | News', 'https://a.example/f', 'World & Co')
        parts = frame.split('|', 7)
        self.assertEqual(8, len(parts))
        self.assertEqual('NPR | News', utils.decode_text(parts[2]))
        self.assertEqual('World & Co', utils.decode_text(parts[4]))

    def test_a_realistic_feed_fits_a_lora_packet(self):
        """The fleet meets over MQTT, where 32KB is nothing, but the same
        frame goes out over the radio links too."""
        frame = self._frame_for('NPR News', 'https://feeds.npr.org/1001/rss.xml',
                                'World')
        self.assertLessEqual(len(frame.encode('utf-8')), 220)

    def test_an_oversized_feed_is_skipped_rather_than_truncated(self):
        """Base64 costs a third on top, so a long name and a long URL can
        outgrow a LoRa packet. There is no reassembly for this frame, so
        sending it anyway means a truncated frame dropped as malformed at
        the far end -- silently, and only on the radio links."""
        import utils
        feed = {'feed_id': 'abc123', 'name': 'N' * 40,
                'url': 'https://a.example/' + 'u' * 120, 'category': 'C' * 24,
                'author_node_id': '!aaaa1111',
                'updated_at': '2026-09-19T20:00:00.000000+00:00', 'deleted': False}
        interface = types.SimpleNamespace(max_text_bytes=220,
                                          protocol_name='meshtastic')
        with mock.patch('db_operations.peer_supports', return_value=True), \
                mock.patch.object(utils, '_send_one_sync') as send:
            self.assertEqual(0, utils.send_feed_to_bbs_nodes(feed, ['!peer'], interface))
        send.assert_not_called()

    def test_a_retirement_is_marked_in_the_frame(self):
        import utils
        feed = {'feed_id': 'abc123', 'name': 'Gone', 'url': 'https://a.example/f',
                'category': '', 'author_node_id': '!aaaa1111',
                'updated_at': '2026-09-19T20:00:00.000000+00:00', 'deleted': True}
        sent = []
        with mock.patch.object(utils, '_send_one_sync',
                               side_effect=lambda msg, *a, **k: sent.append(msg)), \
                mock.patch('db_operations.peer_supports', return_value=True):
            utils.send_feed_to_bbs_nodes(feed, ['!peer'], object())
        self.assertTrue(sent[0].endswith('|1'))

    def test_a_peer_without_the_capability_is_not_sent_one(self):
        import utils
        feed = {'feed_id': 'abc', 'name': 'N', 'url': 'https://a.example/f',
                'category': '', 'author_node_id': '!a',
                'updated_at': '2026-09-19T20:00:00Z', 'deleted': False}
        with mock.patch('db_operations.peer_supports', return_value=False), \
                mock.patch.object(utils, '_send_one_sync') as send:
            self.assertEqual(0, utils.send_feed_to_bbs_nodes(feed, ['!old'], object()))
        send.assert_not_called()

    def test_the_capability_is_advertised(self):
        import utils
        self.assertIn('feed', utils.WIRE_CAPABILITIES)

    def test_the_frame_is_classified_as_sync_traffic(self):
        """The trap BBSID fell into: a frame can be sent, received, and
        handled, and still be dropped before dispatch because its prefix
        is missing from the allow-list that decides what is sync traffic.
        Both halves have to exist, so both are checked."""
        source = io.open('message_processing.py', encoding='utf-8').read()
        self.assertIn('message.startswith("FEED|")', source)
        # The allow-list, not the handler that happens to mention the same
        # prefix: anchor on a run of entries only the list contains.
        start = source.index('"SCORESYNC|", "ROLE|", "BBSID|"')
        self.assertIn('"FEED|"', source[start:start + 200])


class DoorFollowUpTests(unittest.TestCase):
    """What a user sees after a door answers.

    It used to be nothing: the answer arrived, the state was cleared, and
    the user was at the main menu with no sign of it. Over a radio that is
    a DM followed by silence.
    """

    def setUp(self):
        import command_handlers
        self.ch = command_handlers
        self.sent = []
        self.iface = types.SimpleNamespace(bbs_nodes=[], nodes={},
                                           protocol_name='meshtastic',
                                           max_text_bytes=220)
        patcher = mock.patch.object(
            self.ch, 'send_message',
            side_effect=lambda text, *a, **k: self.sent.append(text) or True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.ch.update_user_state, 77, None)

    def _answer(self, body, return_state=None):
        self.ch.deliver_door_reply(body, 77, self.iface, return_state)
        return self.sent[-1]

    def test_the_answer_says_what_to_do_next(self):
        text = self._answer("37402: Sunny, +74F")
        self.assertIn("37402: Sunny, +74F", text)
        self.assertIn("[0]", text)

    def test_it_is_one_message_not_two(self):
        """Two DMs two seconds apart race each other's relay traffic on a
        multi-hop mesh and the second one loses -- the reason Ask Nomad
        bundles its invitation too."""
        self._answer("an answer")
        self.assertEqual(1, len(self.sent))

    def test_the_invitation_comes_out_of_the_reply_budget(self):
        """Appending to a reply that already landed on the cap is how one
        packet quietly becomes two."""
        text = self._answer("x" * 400)
        self.assertLessEqual(len(text.encode('utf-8')), self.iface.max_text_bytes)
        self.assertIn("[0]", text)
        self.assertIn("\u2026", text)

    def test_a_short_answer_is_not_trimmed(self):
        text = self._answer("brief")
        self.assertTrue(text.startswith("brief"))
        self.assertNotIn("\u2026", text)

    def test_an_empty_answer_still_leaves_a_way_out(self):
        self.assertIn("[0]", self._answer(""))

    def test_the_user_lands_back_on_the_list_they_picked_from(self):
        """Not the main menu: "[0] main menu" would be a lie there, since 0
        at the main menu disconnects."""
        state = {'command': 'APIGW', 'step': 2, 'group': 'Weather & Safety'}
        self._answer("sunny", state)
        self.assertEqual(state, self.ch.get_user_state(77))

    def test_a_number_then_picks_another_door_in_that_list(self):
        sent = []
        with mock.patch.object(self.ch, '_apigw_submit',
                               side_effect=lambda *a, **k: sent.append(a)):
            self.ch.update_user_state(77, {'command': 'APIGW', 'step': 2,
                                           'group': 'Propagation & Sky'})
            self.ch.handle_apigw_steps(77, '1', self.iface)
        self.assertTrue(sent, "a number after an answer did nothing")
        self.assertEqual('d', sent[-1][2])

    def test_zero_goes_back_up_rather_than_disconnecting(self):
        self.ch.update_user_state(77, {'command': 'APIGW', 'step': 2,
                                       'group': 'Weather & Safety'})
        with mock.patch.object(self.ch, '_apigw_authorized', return_value=True):
            self.ch.handle_apigw_steps(77, '0', self.iface)
        self.assertIn("Services", self.sent[-1])

    def test_a_raw_http_reply_still_gets_no_follow_up(self):
        """'h' is the old one-shot fetch, and nothing on the menu makes one."""
        import message_processing as mp
        source = io.open('message_processing.py', encoding='utf-8').read()
        start = source.index("def _deliver_api_response")
        body = source[start:start + 1400]
        self.assertIn("kind in ('r', 'd')", body)


class FeedAdminRouteTests(unittest.TestCase):
    """The web-admin side of feeds, through the real routes.

    Every check here exists because the same rule has to hold in two
    places: the data layer refuses what it should, and the form must not
    hand it an answer that satisfies the check by accident.
    """

    def setUp(self):
        import tempfile
        folder = tempfile.mkdtemp()
        patcher = mock.patch.dict(os.environ,
                                  {'BBS_DB_PATH': os.path.join(folder, 'feeds.db')})
        patcher.start()
        self.addCleanup(patcher.stop)
        import db_operations
        import web_admin
        self.db = db_operations
        self.web_admin = web_admin
        self.db.initialize_database()
        self.addCleanup(self._drop_connection)

        # Persisted for real rather than patched: owned_locally lives in
        # db_operations and reads its own module's function, so patching the
        # name web_admin imported proves nothing about the path in use.
        self.local_id = 'mqtt:net:me'
        self.db.persist_local_identities({self.local_id}, set())

        self.app = web_admin.create_app()
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session['logged_in'] = True

    def _drop_connection(self):
        try:
            self.db.get_db_connection().close()
        except Exception:
            pass

    def _post(self, **form):
        form.setdefault('settings_section', 'feeds')
        form['csrf_token'] = self.client.get(
            '/api/csrf-token').get_json()['csrf_token']
        return self.client.post('/settings', data=form,
                                follow_redirects=True).get_data(as_text=True)

    def test_a_feed_you_own_can_be_edited_in_place(self):
        """Without this the only way to fix a typo was retire and re-add,
        which mints a new id and leaves a tombstone behind."""
        feed_id = self.db.save_feed('Old Name', 'https://a.example/f', 'World',
                                    self.local_id)
        with mock.patch.object(self.web_admin, '_check_feed_url',
                               return_value=(True, 'a headline')):
            self._post(feed_action='save', feed_id=feed_id, feed_name='New Name',
                       feed_category='World', feed_url='https://a.example/f')
        feed = self.db.get_feed(feed_id)
        self.assertEqual('New Name', feed['name'])
        self.assertEqual(feed_id, feed['feed_id'], "editing minted a new id")
        self.assertEqual(0, feed['deleted'], "editing left a tombstone")

    def test_another_nodes_feed_cannot_be_edited_through_the_form(self):
        """The regression this pins: keeping a feed's author across an edit
        is right, but handing save_feed that author unconditionally answers
        its ownership check with the very thing it was meant to test -- and
        every node could then rewrite every feed in the fleet."""
        feed_id = self.db.save_feed('Theirs', 'https://a.example/f', 'World',
                                    'mqtt:net:someone-else')
        with mock.patch.object(self.web_admin, '_check_feed_url',
                               return_value=(True, 'a headline')):
            page = self._post(feed_action='save', feed_id=feed_id,
                              feed_name='Hijacked', feed_category='World',
                              feed_url='https://evil.example/f')
        self.assertEqual('Theirs', self.db.get_feed(feed_id)['name'])
        self.assertIn('belongs to another node', page)

    def test_a_feed_can_be_rechecked_on_demand(self):
        """A feed is checked when added and then trusted forever, so one
        that dies keeps failing on the radio with nothing here saying so."""
        feed_id = self.db.save_feed('Mine', 'https://a.example/f', 'World',
                                    self.local_id)
        with mock.patch.object(self.web_admin, '_check_feed_url',
                               return_value=(True, 'a fresh headline')):
            page = self._post(feed_action='recheck', feed_id=feed_id)
        self.assertIn('a fresh headline', page)

        with mock.patch.object(self.web_admin, '_check_feed_url',
                               return_value=(False, 'could not fetch it')):
            page = self._post(feed_action='recheck', feed_id=feed_id)
        self.assertIn('is not working', page)

    def test_a_peers_feed_can_still_be_rechecked(self):
        """Checking is reading, not owning."""
        feed_id = self.db.save_feed('Theirs', 'https://a.example/f', 'World',
                                    'mqtt:net:someone-else')
        with mock.patch.object(self.web_admin, '_check_feed_url',
                               return_value=(True, 'a headline')):
            page = self._post(feed_action='recheck', feed_id=feed_id)
        self.assertIn('a headline', page)

    def test_the_author_id_is_one_the_fleet_will_recognise(self):
        """A node can answer to several link ids. Taking the first
        alphabetically stamped feeds with an id from a network the fleet
        does not sync on, so peers saw feeds signed by a node they had
        never heard of."""
        with mock.patch.object(self.web_admin, 'get_persisted_local_link_ids',
                               return_value=['mqtt:aaa:bbs-main',
                                             'mqtt:fleet:Burlington']), \
                mock.patch.object(self.web_admin, 'load_sync_peers',
                                  return_value=[{'node_id': 'mqtt:fleet:VT2'},
                                                {'node_id': 'mqtt:fleet:Chatt'}]):
            self.assertEqual('mqtt:fleet:Burlington',
                             self.web_admin.preferred_author_node_id('config.ini'))

    def test_one_link_id_needs_no_choosing(self):
        with mock.patch.object(self.web_admin, 'get_persisted_local_link_ids',
                               return_value=['mqtt:aaa:only']):
            self.assertEqual('mqtt:aaa:only',
                             self.web_admin.preferred_author_node_id('config.ini'))

    def test_no_link_id_yet_means_no_author(self):
        with mock.patch.object(self.web_admin, 'get_persisted_local_link_ids',
                               return_value=[]):
            self.assertEqual('', self.web_admin.preferred_author_node_id('config.ini'))

    def test_no_matching_network_falls_back_to_the_first(self):
        with mock.patch.object(self.web_admin, 'get_persisted_local_link_ids',
                               return_value=['mqtt:aaa:one', 'mqtt:bbb:two']), \
                mock.patch.object(self.web_admin, 'load_sync_peers',
                                  return_value=[{'node_id': 'mqtt:zzz:elsewhere'}]):
            self.assertEqual('mqtt:aaa:one',
                             self.web_admin.preferred_author_node_id('config.ini'))


class DurableAuthorshipTests(unittest.TestCase):
    """Ownership has to outlive a link going down.

    local_node_identities is rebuilt from the links that are actually up and
    rewritten wholesale, so a broker down at startup takes its id out of the
    set -- and every feed published under it stops looking like this node's
    own. On an intermittent broker that means intermittently losing Edit and
    Retire on your own feeds.
    """

    MAIN = 'mqtt:netone:bbs-main'
    OTHER = 'mqtt:nettwo:Burlington'

    def setUp(self):
        import tempfile
        folder = tempfile.mkdtemp()
        patcher = mock.patch.dict(os.environ,
                                  {'BBS_DB_PATH': os.path.join(folder, 'feeds.db')})
        patcher.start()
        self.addCleanup(patcher.stop)
        import db_operations
        import web_admin
        self.db = db_operations
        self.web_admin = web_admin
        self.db.initialize_database()
        self.addCleanup(self._drop_connection)
        self.db.persist_local_identities({self.MAIN, self.OTHER}, set())

    def _drop_connection(self):
        try:
            self.db.get_db_connection().close()
        except Exception:
            pass

    def _owned(self):
        return {f['name']: f['owned']
                for f in self.web_admin.load_feed_settings()['feeds']}

    def _publish(self, name, author):
        """What the web admin does: save, then claim the id."""
        feed_id = self.db.save_feed(name, 'https://a.example/f', 'World', author)
        self.db.remember_authorship_id(author)
        return feed_id

    def test_publishing_records_the_id_it_published_under(self):
        self._publish('Mine', self.OTHER)
        self.assertIn(self.OTHER, self.db.get_authorship_ids())

    def test_saving_a_feed_does_not_by_itself_claim_its_author(self):
        """save_feed takes whatever author it is given -- including a
        peer's, when one is seeded -- so claiming an id has to be the
        caller's decision."""
        self.db.save_feed('Theirs', 'https://a.example/f', 'World',
                          'mqtt:nettwo:someone-else')
        self.assertNotIn('mqtt:nettwo:someone-else', self.db.get_authorship_ids())

    def test_a_feed_stays_yours_when_its_link_goes_down(self):
        feed_id = self._publish('Mine', self.OTHER)
        self.assertTrue(self._owned()['Mine'])

        # That broker is down when the node next publishes its identities.
        self.db.persist_local_identities({self.MAIN}, set())
        self.assertNotIn(self.OTHER, self.db.get_persisted_local_link_ids())

        self.assertTrue(self._owned()['Mine'], "a link outage disowned our own feed")
        self.assertTrue(self.db.delete_feed(feed_id, self.OTHER),
                        "could not retire our own feed while its link was down")

    def test_another_nodes_feed_is_still_not_ours(self):
        """The wide set must not become a set that owns everything."""
        self.db.save_feed('Theirs', 'https://a.example/f', 'World',
                          'mqtt:nettwo:someone-else')
        self.assertFalse(self._owned()['Theirs'])
        self.assertFalse(self.db.owned_locally('mqtt:nettwo:someone-else'))

    def test_owned_locally_refuses_an_empty_author(self):
        self.assertFalse(self.db.owned_locally(''))
        self.assertFalse(self.db.owned_locally(None))

    def test_a_live_link_id_is_owned_before_anything_is_published(self):
        self.assertTrue(self.db.owned_locally(self.MAIN))

    def test_the_add_form_still_gates_on_a_live_link(self):
        """Ownership is wide; the Add form is not. A node with no link id at
        all cannot own a feed whatever it once published under."""
        self._publish('Mine', self.OTHER)
        conn = self.db.get_db_connection()
        conn.execute("DELETE FROM local_node_identities")
        conn.commit()
        self.assertEqual([], self.web_admin.load_feed_settings()['local_ids'])
        # ...and the feed it already published is still its own.
        self.assertTrue(self._owned()['Mine'])


class NodeLabelTests(unittest.TestCase):
    """A node id as something a person reads, without losing the id."""

    def setUp(self):
        import web_admin
        self.web_admin = web_admin

    def test_an_mqtt_id_reads_as_its_node_name(self):
        self.assertEqual('Chattanooga',
                         self.web_admin.node_display_name('mqtt:baconbbsvt:Chattanooga'))

    def test_a_radio_id_is_left_alone(self):
        self.assertEqual('!04058ac8', self.web_admin.node_display_name('!04058ac8'))

    def test_nothing_at_all_still_says_something(self):
        self.assertEqual('unknown', self.web_admin.node_display_name(''))
        self.assertEqual('unknown', self.web_admin.node_display_name(None))

    def test_an_unfamiliar_shape_is_shown_as_it_is(self):
        """Better the raw id than a confident guess at the wrong part."""
        self.assertEqual('ssh:abc', self.web_admin.node_display_name('ssh:abc'))


if __name__ == '__main__':
    unittest.main()
