"""Doors: curated text services a mesh user can reach through the gateway.

A "door", in the BBS sense, is a named service on the menu rather than an
address to type. Each entry here pairs a vetted URL with a parser that turns
the response into the two or three lines a person actually asked for.

This replaces Web Fetch, which handed back the first 800 bytes of whatever a
URL served. On any real site that is ``<!DOCTYPE html>`` and a stylesheet
link: the user paid for a LoRa packet of markup and learned nothing. The
knowledge that makes a fetch useful -- which URL, which field, which units --
had nowhere to live, so every request carried it or went without. It lives
here now, written once per service.

Two consequences worth stating, because they are the point:

* Doors do not consult ``[gateway] allowed_hosts``. A door's host was vetted
  when the door was written, which is the only moment anyone is in a position
  to judge it. ``allowed_hosts`` defaulting to empty is what left Web Fetch
  refusing every URL on every node that ever shipped.
* Every door is a plain GET that changes nothing upstream. Nothing here takes
  a method, a body, or a credential from the user.

Parsers are pure ``(body: str) -> str``. They never touch the network, which
is what lets the tests run every one of them against a captured fixture.
"""

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Optional, Tuple

from utils import _config_int, _config_raw

# Some services answer differently -- or refuse -- without a User-Agent that
# names a contact. NWS documents this as a requirement, and callook and
# Wikipedia both prefer it.
USER_AGENT = "BaconBBS-mesh (off-grid mesh radio BBS; https://github.com/dreamsofbacon/TC2-BaconBS-mesh)"

# The cap on what a door READS, which is not the cap on what it sends. A
# forecast document is tens of kilobytes and yields one line; capping the
# read at the reply size would truncate the JSON and break the parser
# instead of shortening the answer.
DOOR_MAX_FETCH_BYTES = 262144


class DoorError(Exception):
    """A failure with something to say to the person who opened the door."""


# ── Parsers ──────────────────────────────────────────────────────────────────
#
# Each returns finished text. Raise DoorError for anything a user can act on
# ("no station by that name"); let everything else raise and be reported as a
# service fault, because a parser crashing means the upstream shape changed
# and that is not the user's problem to solve.

def _loads(body: str):
    try:
        return json.loads(body)
    except ValueError:
        raise DoorError("the service sent back something unreadable")


def _one_line(body: str) -> str:
    """wttr.in's ``?format=`` output: already a single line, by design."""
    text = " ".join(body.split())
    if not text:
        raise DoorError("no data for that place")
    # wttr.in reports an unknown place as a 200 with its own prose.
    if "Unknown location" in body or text.startswith("ERROR"):
        raise DoorError("no place by that name")
    return text


def _parse_wx3(body: str) -> str:
    doc = _loads(body)
    days = doc.get('weather') or []
    if not days:
        raise DoorError("no forecast for that place")
    where = ''
    area = (doc.get('nearest_area') or [{}])[0]
    for key in ('areaName', 'region'):
        vals = area.get(key) or []
        if vals and vals[0].get('value'):
            where = vals[0]['value']
            break
    lines = [f"{where} 3-day:"] if where else ["3-day:"]
    for day in days[:3]:
        desc = ((day.get('hourly') or [{}])[4].get('weatherDesc') or [{}])[0].get('value', '')
        lines.append(f"{day.get('date','?')} {day.get('mintempF','?')}-"
                     f"{day.get('maxtempF','?')}F {desc}".strip())
    return "\n".join(lines)


def _parse_alerts(body: str) -> str:
    doc = _loads(body)
    features = doc.get('features') or []
    if not features:
        return "No active NWS alerts."
    lines = []
    for feature in features[:4]:
        props = feature.get('properties') or {}
        event = props.get('event', 'Alert')
        area = props.get('areaDesc', '')
        # areaDesc lists every county by name and runs to hundreds of
        # characters; the first two say enough to know whether it is you.
        parts = [p.strip() for p in area.split(';') if p.strip()]
        where = ", ".join(parts[:2]) + ("…" if len(parts) > 2 else "")
        lines.append(f"{event}: {where}" if where else event)
    return "\n".join(lines)


def _parse_quake(body: str) -> str:
    doc = _loads(body)
    features = doc.get('features') or []
    if not features:
        return "No quakes M2.5+ in the last day."
    lines = []
    for feature in features[:4]:
        props = feature.get('properties') or {}
        when = props.get('time')
        ago = ''
        if isinstance(when, (int, float)):
            hours = max(0, (time.time() - when / 1000.0) / 3600.0)
            ago = f" {hours:.0f}h ago" if hours >= 1 else " <1h ago"
        lines.append(f"M{props.get('mag','?')} {props.get('place','')}{ago}")
    return "\n".join(lines)


def _parse_tide(body: str) -> str:
    doc = _loads(body)
    if doc.get('error'):
        raise DoorError(str(doc['error'].get('message', 'no such station'))[:120])
    rows = doc.get('predictions') or []
    if not rows:
        raise DoorError("no predictions for that station")
    lines = []
    for row in rows[:4]:
        kind = 'High' if row.get('type') == 'H' else 'Low'
        lines.append(f"{kind} {row.get('t','')} {row.get('v','')}ft")
    return "\n".join(lines)


def _parse_solar(body: str) -> str:
    """hamqsl's solar XML: the numbers an operator checks before calling CQ."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        raise DoorError("the solar service sent back something unreadable")
    data = root.find('solardata')
    if data is None:
        raise DoorError("the solar service sent no data")

    def field(tag, default='?'):
        node = data.find(tag)
        return (node.text or default).strip() if node is not None else default

    head = (f"SFI {field('solarflux')} SN {field('sunspots')} "
            f"A {field('aindex')} K {field('kindex')}")
    storm = field('geomagfield', '')
    if storm and storm != '?':
        head += f" ({storm.lower()})"
    bands = []
    for band in data.findall('.//band'):
        if band.get('time') == 'day' and band.text:
            bands.append(f"{band.get('name')} {band.text.strip()}")
    if bands:
        head += "\nDay: " + ", ".join(bands[:4])
    return head


def _parse_kp(body: str) -> str:
    """SWPC's planetary K index, in either shape it is served in.

    The products endpoint returns objects ({"time_tag", "Kp", ...}); several
    of its siblings return a header row followed by plain arrays. Reading
    both costs four lines and saves this door breaking the day SWPC swaps
    one for the other -- which is how it shipped broken the first time.
    """
    rows = _loads(body)
    if not isinstance(rows, list) or not rows:
        raise DoorError("the space weather service sent no data")
    readings = []
    for row in rows:
        if isinstance(row, dict):
            when, value = row.get('time_tag'), row.get('Kp', row.get('kp_index'))
        elif isinstance(row, list) and len(row) >= 2:
            when, value = row[0], row[1]
        else:
            continue
        try:
            readings.append((str(when), float(value)))
        except (TypeError, ValueError):
            continue  # the header row of the array form lands here
    recent = readings[-3:]
    if not recent:
        raise DoorError("the space weather service sent no data")
    when, kp = recent[-1]
    # The number is meaningless to most people; the sentence is not.
    verdict = (" quiet" if kp < 4 else " active" if kp < 5
               else " storm — HF degraded, aurora possible")
    trail = ", ".join(f"{value:g}" for _, value in recent)
    return f"Kp {kp:g}{verdict}\n{when[:16].replace('T', ' ')} UTC (last 3: {trail})"


def _parse_forecast_text(body: str) -> str:
    """SWPC's 3-day forecast: plain text, and the rationale is the useful part."""
    text = body.replace('\r', '')
    match = re.search(r'NOAA Kp index breakdown.*?\n\n(.*?)\n\n', text, re.S)
    lines = []
    head = re.search(r'^([A-C]\.\s*NOAA Geomagnetic Activity Observation.*)$', text, re.M)
    rationale = re.search(r'Rationale:\s*(.+?)(?:\n\n|\Z)', text, re.S)
    if rationale:
        lines.append(" ".join(rationale.group(1).split()))
    if not lines and match:
        lines.append(" ".join(match.group(1).split()))
    if not lines:
        # Better a slice of the real document than an invented error.
        lines.append(" ".join(text.split())[:300])
    return "\n".join(lines)


def _parse_iss(body: str) -> str:
    doc = _loads(body)
    lat, lon = doc.get('latitude'), doc.get('longitude')
    if lat is None or lon is None:
        raise DoorError("the satellite service sent no position")
    return (f"ISS {float(lat):.1f}, {float(lon):.1f} — "
            f"{float(doc.get('altitude', 0)):.0f}km up, "
            f"{float(doc.get('velocity', 0)):.0f}km/h")


def _parse_wiki(body: str) -> str:
    doc = _loads(body)
    if doc.get('type', '').endswith('not_found'):
        raise DoorError("no article by that name")
    extract = (doc.get('extract') or '').strip()
    if not extract:
        raise DoorError("that article has no summary")
    title = (doc.get('title') or '').strip()
    return f"{title}: {extract}" if title else extract


def _parse_definition(body: str) -> str:
    doc = _loads(body)
    if isinstance(doc, dict):
        raise DoorError("no word by that spelling")
    entry = (doc or [{}])[0]
    word = entry.get('word', '')
    lines = []
    for meaning in (entry.get('meanings') or [])[:2]:
        part = meaning.get('partOfSpeech', '')
        for definition in (meaning.get('definitions') or [])[:1]:
            text = (definition.get('definition') or '').strip()
            if text:
                lines.append(f"({part}) {text}" if part else text)
    if not lines:
        raise DoorError("no definition found")
    return f"{word}: " + " ".join(lines)


def _parse_callsign(body: str) -> str:
    doc = _loads(body)
    if doc.get('status') != 'VALID':
        raise DoorError("no US licence by that callsign")
    current = doc.get('current') or {}
    name = (doc.get('name') or '').title()
    location = doc.get('location') or {}
    trustee = current.get('operClass', '')
    bits = [f"{current.get('callsign', '')}: {name}".strip(': ')]
    if trustee:
        bits.append(trustee.title())
    if location.get('gridsquare'):
        bits.append(location['gridsquare'])
    address = doc.get('address') or {}
    if address.get('line2'):
        bits.append(address['line2'])
    expiry = (doc.get('otherInfo') or {}).get('expiryDate')
    if expiry:
        bits.append(f"exp {expiry}")
    return " — ".join(bits)


def _parse_rates(body: str) -> str:
    doc = _loads(body)
    rates = doc.get('rates') or {}
    if not rates:
        raise DoorError("no rates available")
    base = doc.get('base_code', 'USD')
    wanted = [c for c in ('EUR', 'GBP', 'CAD', 'MXN', 'JPY') if c in rates][:5]
    shown = ", ".join(f"{c} {rates[c]:.2f}" for c in wanted)
    return f"1 {base} = {shown}"


def _parse_time(body: str) -> str:
    doc = _loads(body)
    when = doc.get('dateTime') or doc.get('datetime')
    if not when:
        raise DoorError("no time for that zone")
    zone = doc.get('timeZone') or doc.get('timezone') or ''
    return f"{zone}: {str(when)[:16].replace('T', ' ')}"


def _parse_rss_titles(body: str) -> str:
    """Titles only. A feed's bodies are the overhead we are here to avoid."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        raise DoorError("that feed sent back something unreadable")
    titles = []
    for item in root.iter():
        tag = item.tag.rsplit('}', 1)[-1]
        if tag in ('item', 'entry'):
            for child in item:
                if child.tag.rsplit('}', 1)[-1] == 'title' and (child.text or '').strip():
                    titles.append(" ".join(child.text.split()))
                    break
        if len(titles) >= 5:
            break
    if not titles:
        raise DoorError("that feed has no items")
    return "\n".join(f"- {t}" for t in titles)


# ── The registry ─────────────────────────────────────────────────────────────
#
# 'arg' is the prompt shown when a door needs one; a door without it runs
# straight off the menu. 'cache' is seconds -- a dozen people asking the same
# node for the same forecast should be one call upstream, not a dozen.

DOORS: dict = {
    # Weather and safety. The strongest case for a gateway on an off-grid
    # mesh: a warning nobody else can hear is the reason the radio is on.
    'wx': {
        'name': 'Weather now',
        'group': 'Weather & Safety',
        'arg': 'ZIP, city, or airport code',
        'url': 'https://wttr.in/{arg}?format=%l:+%C,+%t+(feels+%f),+wind+%w,+hum+%h',
        'parse': _one_line,
        'cache': 600,
    },
    'wx3': {
        'name': '3-day forecast',
        'group': 'Weather & Safety',
        'arg': 'ZIP, city, or airport code',
        'url': 'https://wttr.in/{arg}?format=j1',
        'parse': _parse_wx3,
        'cache': 1800,
    },
    'alerts': {
        'name': 'NWS alerts',
        'group': 'Weather & Safety',
        'arg': 'two-letter state, e.g. TN',
        'url': 'https://api.weather.gov/alerts/active?area={arg}',
        'parse': _parse_alerts,
        'cache': 300,
    },
    'quake': {
        'name': 'Recent quakes',
        'group': 'Weather & Safety',
        'url': ('https://earthquake.usgs.gov/fdsnws/event/1/query?format=geojson'
                '&minmagnitude=2.5&orderby=time&limit=5'),
        'parse': _parse_quake,
        'cache': 600,
    },
    'sun': {
        'name': 'Sunrise & sunset',
        'group': 'Weather & Safety',
        'arg': 'ZIP, city, or airport code',
        'url': 'https://wttr.in/{arg}?format=%l:+dawn+%D,+sunrise+%S,+sunset+%s,+dusk+%d',
        'parse': _one_line,
        'cache': 3600,
    },
    'tide': {
        'name': 'Tides',
        'group': 'Weather & Safety',
        'arg': 'NOAA station number, e.g. 8518750',
        'url': ('https://api.tidesandcurrents.noaa.gov/api/prod/datagetter'
                '?product=predictions&datum=MLLW&interval=hilo&units=english'
                '&time_zone=lst_ldt&format=json&date=today&station={arg}'),
        'parse': _parse_tide,
        'cache': 3600,
    },

    # Propagation and sky. On a mesh radio BBS this is not trivia: it is
    # whether the HF rig is worth switching on this afternoon.
    'solar': {
        'name': 'Solar & band conditions',
        'group': 'Propagation & Sky',
        'url': 'https://www.hamqsl.com/solarxml.php',
        'parse': _parse_solar,
        'cache': 900,
    },
    'kp': {
        'name': 'Geomagnetic Kp',
        'group': 'Propagation & Sky',
        'url': 'https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json',
        'parse': _parse_kp,
        'cache': 900,
    },
    'aurora': {
        'name': 'Space weather outlook',
        'group': 'Propagation & Sky',
        'url': 'https://services.swpc.noaa.gov/text/3-day-forecast.txt',
        'parse': _parse_forecast_text,
        'cache': 3600,
    },
    'iss': {
        'name': 'ISS position',
        'group': 'Propagation & Sky',
        'url': 'https://api.wheretheiss.at/v1/satellites/25544',
        'parse': _parse_iss,
        'cache': 30,
    },

    # Reference. Wikipedia summaries over LoRa is the one that turns a radio
    # into something you carry for a reason.
    'wiki': {
        'name': 'Wikipedia',
        'group': 'Reference',
        'arg': 'article title',
        'url': 'https://en.wikipedia.org/api/rest_v1/page/summary/{arg}',
        'parse': _parse_wiki,
        'cache': 86400,
    },
    'def': {
        'name': 'Dictionary',
        'group': 'Reference',
        'arg': 'word',
        'url': 'https://api.dictionaryapi.dev/api/v2/entries/en/{arg}',
        'parse': _parse_definition,
        'cache': 86400,
    },
    'call': {
        'name': 'Callsign lookup',
        'group': 'Reference',
        'arg': 'US callsign',
        'url': 'https://callook.info/{arg}/json',
        'parse': _parse_callsign,
        'cache': 86400,
    },
    'fx': {
        'name': 'Exchange rates',
        'group': 'Reference',
        'url': 'https://open.er-api.com/v6/latest/USD',
        'parse': _parse_rates,
        'cache': 3600,
    },
    'time': {
        'name': 'Time by zone',
        'group': 'Reference',
        'arg': 'zone, e.g. America/New_York',
        'url': 'https://timeapi.io/api/Time/current/zone?timeZone={arg}',
        'parse': _parse_time,
        'cache': 30,
    },

    # News. Titles only -- the bodies are exactly the overhead doors exist
    # to stop paying for.
    'hn': {
        'name': 'Hacker News',
        'group': 'News',
        'category': 'Tech',
        'url': 'https://hnrss.org/frontpage',
        'parse': _parse_rss_titles,
        'cache': 900,
    },
}

# Groups in the order the menu should show them, derived rather than
# repeated, so adding a door in a new group cannot leave it off the menu.
GROUP_ORDER = []
for _door in DOORS.values():
    if _door['group'] not in GROUP_ORDER:
        GROUP_ORDER.append(_door['group'])


# ── Feeds as doors ───────────────────────────────────────────────────────────
#
# A feed is a door whose URL an operator chose, so it cannot live in the
# static registry above. Feeds are fleet-wide and owned by the node that
# added them (db_operations.fleet_feeds); each becomes a door under News,
# filed by the category its author typed.

FEED_DOOR_PREFIX = 'feed:'
FEED_GROUP = 'News'


def _feed_doors() -> dict:
    """Every feed this node will show, as door entries keyed by door id."""
    try:
        from db_operations import list_feeds
        feeds = list_feeds(include_deleted=False, include_blocked=False)
    except Exception:
        return {}
    doors = {}
    for feed in feeds:
        doors[FEED_DOOR_PREFIX + str(feed['feed_id'])] = {
            'name': feed['name'],
            'group': FEED_GROUP,
            'category': feed['category'],
            'url': feed['url'],
            'parse': _parse_rss_titles,
            'cache': 900,
        }
    return doors


def all_doors() -> dict:
    """The static registry plus this node's feeds.

    Feed ids are namespaced, so a feed called 'wx' cannot shadow the
    weather door.
    """
    doors = dict(DOORS)
    doors.update(_feed_doors())
    return doors


# Where a door lands when its author filed it nowhere. Without this bucket
# an uncategorised door in a group that HAS categories is unreachable: the
# category screen is the only way in, and it lists nothing that would hold
# it. That is how the built-in Hacker News door first disappeared the moment
# a second feed was added.
UNFILED_CATEGORY = 'Other'


def _category_of(entry: dict) -> str:
    return (entry.get('category') or '').strip()


def categories_in_group(group: str) -> list:
    """Categories inside one group, in menu order, or [] for a group that
    does not need a second level.

    A group earns sub-menus only once its doors are filed into more than
    one category. Weather has none and stays two keypresses deep; News
    grows a level the moment a second category exists, and not before --
    an extra screen costs a packet and a keypress on a radio.
    """
    doors = all_doors()
    named = []
    unfiled = False
    for door_id in available_door_ids():
        entry = doors.get(door_id) or {}
        if entry.get('group') != group:
            continue
        name = _category_of(entry)
        if not name:
            unfiled = True
        elif name not in named:
            named.append(name)
    if len(named) + (1 if unfiled else 0) < 2:
        return []
    # Alphabetical, with the catch-all last. Discovery order would put a
    # built-in door's category above the operator's own for no reason a
    # reader could see, and would shuffle as feeds come and go.
    return sorted(named, key=str.casefold) + ([UNFILED_CATEGORY] if unfiled else [])


def doors_in_category(group: str, category: str) -> list:
    """Door ids inside one category of one group.

    The catch-all bucket collects the doors with no category of their own,
    so nothing in a sub-grouped menu is left with no way to reach it.
    """
    doors = all_doors()
    wanted = (category or '').strip()
    catch_all = wanted == UNFILED_CATEGORY
    result = []
    for door_id in available_door_ids():
        entry = doors.get(door_id) or {}
        if entry.get('group') != group:
            continue
        name = _category_of(entry)
        if (catch_all and not name) or (name and name == wanted):
            result.append(door_id)
    return result


def door_ids() -> list:
    """Every door id, grouped in GROUP_ORDER -- the menu's order."""
    doors = all_doors()
    return [door_id for group in GROUP_ORDER
            for door_id, door in doors.items() if door['group'] == group]


def available_door_ids() -> list:
    """Doors that can actually run here. A door with no URL configured is
    left off the menu rather than offered and then refused."""
    return [door_id for door_id in door_ids() if _door_url_template(door_id)]


def _door_url_template(door_id: str) -> str:
    entry = all_doors().get(str(door_id or '')) or {}
    if entry.get('url'):
        return entry['url']
    section_option = entry.get('config_url')
    if section_option:
        return (_config_raw(*section_option) or '').strip()
    return ''


def door_hosts() -> set:
    """Hosts the registry can reach, for the audit that asks what this node
    talks to. Derived from the templates, so it cannot drift from them."""
    hosts = set()
    for door_id in all_doors():
        template = _door_url_template(door_id)
        if template:
            host = urllib.parse.urlparse(template).hostname
            if host:
                hosts.add(host.lower())
    return hosts


# ── Fetch ────────────────────────────────────────────────────────────────────

_cache_lock = threading.Lock()
_cache: dict = {}

MAX_ARG_LENGTH = 80


def _reset_cache() -> None:
    """Test hook."""
    with _cache_lock:
        _cache.clear()


def _clean_arg(arg: str) -> str:
    text = " ".join((arg or "").split())[:MAX_ARG_LENGTH]
    # Control characters cannot appear in a URL and are how a header would be
    # smuggled into one.
    return "".join(ch for ch in text if ch.isprintable())


def _build_url(door_id: str, arg: str) -> str:
    template = _door_url_template(door_id)
    if not template:
        raise DoorError("that service is not set up on this node")
    if '{arg}' not in template:
        return template
    if not arg:
        raise DoorError("that service needs something to look up")
    quoted = urllib.parse.quote(arg, safe='')
    return template.replace('{arg}', quoted)


def _fetch(url: str, timeout: int) -> str:
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"}, method='GET')
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read(DOOR_MAX_FETCH_BYTES)
    return raw.decode('utf-8', errors='replace')


def run_door(door_id: str, arg: str = '', timeout: Optional[int] = None,
             max_bytes: Optional[int] = None) -> Tuple[str, str]:
    """Open a door and return ``(status, text)`` the way the gateway does.

    Blocking; the gateway already runs this on a worker thread.
    """
    door = all_doors().get(str(door_id or ''))
    if not door:
        return "ERR", f"unknown service '{door_id}'"
    arg = _clean_arg(arg)
    timeout = timeout or _config_int('gateway', 'request_timeout', 20)
    max_bytes = max_bytes or _config_int('gateway', 'max_response_bytes', 800)

    key = (door_id, arg)
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and hit[0] > now:
            return hit[1], hit[2]

    try:
        url = _build_url(door_id, arg)
    except DoorError as exc:
        return "ERR", str(exc)

    try:
        body = _fetch(url, timeout)
    except urllib.error.HTTPError as exc:
        # 404, and 400 from a door that takes an argument, are the user's
        # answer rather than a fault: the article, station or state does not
        # exist. NWS answers a bad state code with 400, and "not answering
        # right now" sends someone away to try again at a URL that will
        # never work. Everything else is the service's problem, said plainly.
        if exc.code == 404 or (exc.code == 400 and door.get('arg')):
            for_what = f" for '{arg}'" if arg else ""
            return "ERR", f"{door['name']}: nothing found{for_what}."
        return "ERR", f"{door['name']} is not answering right now ({exc.code})."
    except Exception as exc:
        logging.warning("door %s failed: %s", door_id, exc)
        return "ERR", f"{door['name']} could not be reached."

    try:
        text = door['parse'](body)
    except DoorError as exc:
        return "ERR", f"{door['name']}: {exc}"
    except Exception as exc:
        # The upstream shape changed. That is ours to fix, so log it loudly
        # and tell the user something true rather than a stack trace.
        logging.warning("door %s could not read the reply: %s", door_id, exc)
        return "ERR", f"{door['name']} sent something this node could not read."

    text = str(text).strip()
    if len(text.encode('utf-8')) > max_bytes:
        # The ellipsis counts. max_bytes is a transport budget, and cutting
        # to it and then appending three more bytes overruns the thing it
        # was measured against.
        ellipsis = "…"
        room = max(0, max_bytes - len(ellipsis.encode('utf-8')))
        text = text.encode('utf-8')[:room].decode('utf-8', errors='ignore') + ellipsis
    cache_seconds = door.get('cache', 0)
    if cache_seconds:
        with _cache_lock:
            _cache[key] = (now + cache_seconds, "200", text)
    return "200", text
