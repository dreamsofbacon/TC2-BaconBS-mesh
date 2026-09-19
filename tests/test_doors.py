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

import os
import sys
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

    def test_a_door_with_no_configured_url_is_not_offered(self):
        """The news-feed door is the operator's own. Unset, it is left off
        the menu rather than offered and then refused."""
        with mock.patch.object(services, '_config_raw', return_value=''):
            self.assertNotIn('rss', services.available_door_ids())
        with mock.patch.object(services, '_config_raw',
                               return_value='https://example.com/feed.xml'):
            self.assertIn('rss', services.available_door_ids())

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


class NewsFeedSettingTests(unittest.TestCase):
    """The one door an operator chooses, set from the web admin."""

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

    def test_a_refused_url_is_stored_as_nothing_so_no_door_is_offered(self):
        """Storing an unusable URL would put a door on the menu that can only
        fail. Storing nothing leaves it off, which is true."""
        with mock.patch.object(services, '_config_raw',
                               return_value=self.web_admin._normalise_feed_url("nonsense")):
            self.assertNotIn('rss', services.available_door_ids())

    def test_the_setting_reaches_the_form_and_the_door(self):
        import tempfile
        import textwrap
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "config.ini")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(textwrap.dedent("""\
                    [gateway]
                    enabled = true
                    rss_url = https://feeds.npr.org/1001/rss.xml
                    """))
            settings = self.web_admin.load_gateway_settings(path)
        self.assertEqual("https://feeds.npr.org/1001/rss.xml", settings["rss_url"])
        with mock.patch.object(services, '_config_raw', return_value=settings["rss_url"]):
            self.assertIn('rss', services.available_door_ids())
            self.assertEqual(settings["rss_url"], services._door_url_template('rss'))


if __name__ == '__main__':
    unittest.main()
