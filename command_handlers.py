import configparser
import logging
import os
import random
import re
import threading
import time

from meshtastic import BROADCAST_NUM

from db_operations import (
    add_bulletin, add_mail, delete_mail,
    get_bulletin_content, get_bulletins,
    get_mail, get_mail_content,
    add_channel, get_channels, get_sender_id_by_mail_id,
    get_channel_categories, get_channels_by_name, get_channel_by_id,
    add_channel_comment, get_channel_comments,
    auto_upsert_user_profile, get_user_profile, update_user_bio,
    upsert_game_score, get_game_scoreboard, get_user_game_scores, get_hall_of_fame,
    create_account, get_account_id_for_node, get_linked_node_ids,
    get_linked_nodes_detail, link_node_to_account, unlink_node,
    get_account_alias, set_account_alias, create_link_code, redeem_link_code,
    record_link_attempt, link_rate_limit_ok, account_authorized,
    queue_delayed_link_code,
    get_mail_relay_directory, get_mail_relay_preference, set_mail_relay_for_node,
    get_public_chatter_filters,
    get_public_chatter_history,
)
from utils import (
    get_node_id_from_num, get_node_info,
    get_node_short_name, resolve_display_name, get_user_state, get_zork_save_sync_notice, send_message,
    update_user_state,
    select_gateway_peer, send_api_request, register_api_request,
    home_network, _config_int, send_mail_relay_preference_to_bbs_nodes,
    clear_user_state, request_session_end,
)
from zork_port import (
    GAMES,
    has_zork_save,
    has_zork_session,
    parse_game_score,
    resume_zork_session,
    send_zork_command,
    start_zork_session,
    stop_zork_session,
)
import trivia_port

# Ordered list of playable games (matches GAMES keys in zork_port)
GAME_LIST = list(GAMES.items())  # [(game_id, {name, ...}), ...]

# Read the configuration for menu options
config = configparser.ConfigParser()
config.read('config.ini')


def _parse_menu_items(value: str) -> list[str]:
    return [item.strip().upper() for item in value.split(',') if item.strip()]


def _urgent_board_allow_lists(interface) -> list:
    """All configured urgent-board allow-lists: this interface's own live,
    already-refreshed allowed_nodes, PLUS every [allow_list*] section read
    fresh from config.ini -- [allow_list]/[allow_list2] for up to two
    radios, and [allow_list_mqttN] for each configured MQTT bridge link
    (see config_init.discover_mqtt_link_names; N is open-ended, not capped
    at 2). Reading every section directly here (rather than only consulting
    `interface.allowed_nodes`, which is just ONE link's list) is what lets
    account_authorized() correctly authorize a linked sibling node on a
    DIFFERENT radio or MQTT link, without this handler needing to know
    anything about RadioLink/multi-link internals. config.ini is re-read
    fresh (not cached) so allow-list edits made via the web GUI take effect
    without a restart, matching how interface.allowed_nodes is already
    live-refreshed."""
    lists = [list(getattr(interface, 'allowed_nodes', []) or [])]
    try:
        config.read('config.ini')
        for section in config.sections():
            if section.startswith('allow_list'):
                raw = config.get(section, 'allowed_nodes', fallback='')
                lists.append([n.strip() for n in raw.split(',') if n.strip()])
    except Exception:
        pass
    return lists


main_menu_items = _parse_menu_items(config.get('menu', 'main_menu_items', fallback='Q,B,U,P,N,A,S,X'))
bbs_menu_items = _parse_menu_items(config.get('menu', 'bbs_menu_items', fallback='M,B,C,J,X'))
utilities_menu_items = _parse_menu_items(config.get('menu', 'utilities_menu_items', fallback='S,F,W,G,X'))
# The G/H/Z repairs that used to run here now live in menu_layout, so the
# rendered menu and the digits accepted for it are decided by one function
# rather than by a list mutated at import time and a separate fixed table.


def get_bulletin_boards() -> list[str]:
    env_value = os.getenv('BBS_BULLETIN_BOARDS', '').strip()
    if env_value:
        boards = [item.strip() for item in env_value.split(',') if item.strip()]
        if boards:
            return boards

    config.read('config.ini')
    configured = config.get('boards', 'bulletin_boards', fallback='General,Info,News,Urgent')
    boards = [item.strip() for item in configured.split(',') if item.strip()]
    if boards:
        return boards
    return ['General', 'Info', 'News', 'Urgent']


# Newline for menu strings, kept as a name so patch tooling never has to
# embed a raw escape sequence in these multi-line menu definitions.
LINE_BREAK = chr(10)


UTILITIES_MENU_TITLE = "🛠️Utilities Menu🛠️"
BBS_MENU_TITLE = "📰BBS Menu📰"

# Menu labels WITHOUT their numbers. The number an entry gets is decided at
# render time by menu_layout, so trimming an entry out of config.ini
# renumbers what is left instead of leaving a hole. A node whose config
# predates Profile and Ask Nomad used to render "[1][2][3][6][7]", and gaps
# read as breakage to anyone who finds the BBS cold.
#
# These are still the single source of truth for BOTH the rendered text and
# the digits message_processing accepts, because both go through menu_layout.
# They used to be separate tables, which is how "[5] Ask Nomad" once
# rendered while typing 5 did nothing.
UTILITIES_MENU_LABELS = {
    'S': "Stats",
    'F': "Fortune",
    'W': "Wall of Shame",
    'G': "Games",
    'H': "Public Chatter",
    'X': "Back",
}

BBS_MENU_LABELS = {
    'M': "Mail",
    'B': "Bulletins",
    'C': "Channel Dir",
    'J': "JS8CALL",
    'X': "Back",
}

MAIN_MENU_LABELS = {
    'Q': "Quick Commands",
    'B': "BBS",
    'U': "Utilities",
    'P': "Profile",
    'N': "Ask Nomad",
    'A': "Web Fetch",
    'S': "Linked Devices",
    'X': "Exit",
}

MENU_LABELS = {
    'main': MAIN_MENU_LABELS,
    'bbs': BBS_MENU_LABELS,
    'utilities': UTILITIES_MENU_LABELS,
}

# Entries added after the first config.ini files were written. An explicit
# item list would otherwise never show them -- exactly how the API Gateway
# stayed invisible under Utilities.
MENU_REQUIRED = {
    'main': ('A', 'S'),
    'bbs': (),
    'utilities': ('G', 'H'),
}


def menu_kind(menu_name) -> str:
    """Which menu a display title refers to.

    The main menu's title carries a live mail count, so it is matched last
    as the default rather than by equality.
    """
    if menu_name == UTILITIES_MENU_TITLE:
        return 'utilities'
    if menu_name == BBS_MENU_TITLE:
        return 'bbs'
    return 'main'


def menu_layout(items, menu_name) -> list:
    """The ordered letters a menu actually shows.

    One list, two consumers: build_menu renders it and menu_number_alias
    derives the digits from it. They used to read different tables --
    rendering filtered the configured list while the digits came from a
    fixed map -- so a trimmed config produced a menu whose numbers skipped
    values that still worked if you guessed them.
    """
    kind = menu_kind(menu_name)
    labels = MENU_LABELS[kind]
    layout = [item.strip().upper() for item in items if item and item.strip()]

    # Legacy config spelling for Games.
    if kind == 'utilities' and 'Z' in layout and 'G' not in layout:
        layout[layout.index('Z')] = 'G'

    for required in MENU_REQUIRED[kind]:
        if required not in layout:
            if 'X' in layout:
                layout.insert(layout.index('X'), required)
            else:
                layout.append(required)

    if kind == 'bbs' and not _js8call_configured():
        layout = [item for item in layout if item != 'J']

    # Drop anything with no label: an unknown letter left in a config would
    # otherwise claim a number and render a blank line.
    ordered = []
    for item in layout:
        if item in labels and item not in ordered:
            ordered.append(item)
    # Exit/Back is always [0], so it goes last wherever the config put it.
    return ([item for item in ordered if item != 'X']
            + (['X'] if 'X' in ordered else []))


def build_menu(items, menu_name):
    labels = MENU_LABELS[menu_kind(menu_name)]
    menu_str = f"{menu_name}\n"
    number = 0
    for item in menu_layout(items, menu_name):
        if item == 'X':
            menu_str += f"[0] {labels['X']}\n"
            continue
        number += 1
        menu_str += f"[{number}] {labels[item]}\n"
    return menu_str


def menu_number_alias(items, menu_name, layout=None) -> dict:
    """Digit -> lowercase letter for one rendered menu.

    Counted off the same layout build_menu renders, so a digit cannot mean
    anything other than the line the user is looking at. Callers that have
    already built the layout pass it in: menu_layout consults config.ini for
    the BBS menu, and that would otherwise be re-read on every keystroke.
    """
    alias = {}
    number = 0
    for item in (menu_layout(items, menu_name) if layout is None else layout):
        if item == 'X':
            alias['0'] = 'x'
            continue
        number += 1
        alias[str(number)] = item.lower()
    return alias


def _js8call_configured() -> bool:
    current = configparser.ConfigParser()
    current.read(os.getenv('BBS_CONFIG_PATH', 'config.ini'))
    return bool(current.get('js8call', 'db_file', fallback='').strip())

def handle_help_command(sender_id, interface, menu_name=None, notice=None):
    if menu_name:
        update_user_state(sender_id, {'command': 'MENU', 'menu': menu_name, 'step': 1})
        if menu_name == 'bbs':
            response = build_menu(bbs_menu_items, "📰BBS Menu📰")
        elif menu_name == 'utilities':
            response = build_menu(utilities_menu_items, "🛠️Utilities Menu🛠️")
        else:
            response = build_menu(main_menu_items, "💾Bacon BBS💾")
    else:
        update_user_state(sender_id, {'command': 'MAIN_MENU', 'step': 1})  # Reset to main menu state
        mail = get_mail(get_node_id_from_num(sender_id, interface))
        response = build_menu(main_menu_items, f"💾Bacon BBS💾 (✉️:{len(mail)})")
    if notice:
        response = f"{notice}{LINE_BREAK}{response}"
    send_message(response, sender_id, interface)


def menu_items_for(kind):
    """The configured item list and display title for one menu.

    Shared so message_processing derives its digits from exactly the list
    handle_help_command just rendered, rather than from a parallel table.
    """
    if kind == 'bbs':
        return bbs_menu_items, BBS_MENU_TITLE
    if kind == 'utilities':
        return utilities_menu_items, UTILITIES_MENU_TITLE
    return main_menu_items, "💾Bacon BBS💾"


def send_board_action_menu(sender_id, interface, board_name, boards, notice=None):
    """Show one board's Read/Post menu and park the user on it.

    Shared with the invalid-key path so a wrong number redraws this menu
    instead of bouncing the reader out to the main menu, which used to lose
    which board they were in without saying anything.
    """
    bulletins = get_bulletins(board_name)
    response = f"{board_name} has {len(bulletins)} messages.\n[1]Read [2]Post [0]Back"
    if notice:
        response = f"{notice}{LINE_BREAK}{response}"
    send_message(response, sender_id, interface)
    update_user_state(sender_id, {'command': 'BULLETIN_ACTION', 'step': 2,
                                  'board': board_name, 'boards': boards})


def handle_exit_command(sender_id, interface):
    """Leave the BBS from the top level.

    On a radio there is no connection to close, so this clears the menu
    state and says goodbye; the next message starts fresh. Over SSH there is
    a real session, and request_session_end lets that front end hang up
    without any command handler needing to know which transport it is on.
    """
    clear_user_state(sender_id)
    send_message("73! Send any message to start again.", sender_id, interface)
    request_session_end(interface)


def _incomplete_notice(content_complete, expected_length, actual_content) -> str:
    if bool(content_complete):
        return ""
    have_length = len(str(actual_content or ""))
    target_length = max(have_length, int(expected_length or have_length))
    return f"\n\n[This message may be incomplete. Synced {have_length}/{target_length} chars so far.]"

def get_node_name(node_id, interface):
    node_info = interface.nodes.get(node_id)
    if node_info:
        return node_info['user']['longName']
    return f"Node {node_id}"


def handle_mail_command(sender_id, interface, notice=None):
    response = "✉️Mail Menu✉️\nWhat would you like to do with mail?\n[1]Read [2]Send [3]Relay Directory [0]Back"
    if notice:
        response = f"{notice}{LINE_BREAK}{response}"
    send_message(response, sender_id, interface)
    update_user_state(sender_id, {'command': 'MAIL', 'step': 1})


_MAIL_DIRECTORY_PAGE_SIZE = 6


def _mail_directory_page(entries: list[dict], page: int, selecting: bool) -> str:
    page_count = max(1, (len(entries) + _MAIL_DIRECTORY_PAGE_SIZE - 1) // _MAIL_DIRECTORY_PAGE_SIZE)
    page = max(0, min(page, page_count - 1))
    start = page * _MAIL_DIRECTORY_PAGE_SIZE
    visible = entries[start:start + _MAIL_DIRECTORY_PAGE_SIZE]
    heading = "Select a relay user:" if selecting else "Relay Directory"
    lines = [f"{heading} (page {page + 1}/{page_count})"]
    for index, entry in enumerate(visible, start=1):
        protocols = "/".join(entry['protocols'])
        lines.append(f"[{index}] {entry['display_name']} ({protocols})")
    controls = []
    if page > 0:
        controls.append("[P]revious")
    if page + 1 < page_count:
        controls.append("[N]ext")
    if selecting:
        controls.append("[A]ddress")
    controls.append("[0] Back")
    lines.append(" ".join(controls))
    return "\n".join(lines)


def handle_active_users_command(sender_id, interface):
    entries = get_mail_relay_directory()
    if not entries:
        send_message("No users have opted into offline mail relay.", sender_id, interface)
        handle_mail_command(sender_id, interface)
        return
    send_message(_mail_directory_page(entries, 0, selecting=False), sender_id, interface)
    update_user_state(sender_id, {
        'command': 'MAIL', 'step': 10, 'directory': entries, 'directory_page': 0,
    })


# Public chatter over the radio.
#
# Three things shaped this. Typing "168" on a phone keypad to mean a week is
# worse than pressing one key, so the window is picked from the same six
# presets the web feed offers. One message per reply made reading a busy
# channel a chore of pressing N, so a reply carries as many entries as the
# airtime budget allows. And there was no way to narrow by source at all,
# which on a node bridged to several others is most of what you want.
#
# Both filter dimensions live in one numbered list rather than a menu per
# dimension: one screen and one interaction is simpler than two, and it is
# the combination -- this channel, heard by that node -- that is worth
# selecting.
CHATTER_WINDOWS = ((1, '1h'), (3, '3h'), (6, '6h'),
                   (24, '24h'), (72, '3d'), (168, '7d'))

# How many entries one reply may carry, and the byte ceiling that actually
# decides it. send_message splits at the transport's limit and paces each
# chunk, so an unbounded batch would hold the radio for a long time and bury
# the reader. Whichever runs out first wins, and at least one entry always
# goes out so a reply is never empty.
CHATTER_BATCH = 6
CHATTER_BATCH_BYTES = 700


def _chatter_windows_text() -> str:
    keys = [f"[{n}]{label}" for n, (_, label) in enumerate(CHATTER_WINDOWS, 1)]
    return LINE_BREAK.join([
        "Public Chatter",
        "Show the last:",
        ' '.join(keys[:3]),
        ' '.join(keys[3:]),
        "[0] Back",
    ])


def handle_public_chatter_command(sender_id, interface):
    send_message(_chatter_windows_text(), sender_id, interface)
    update_user_state(sender_id, {
        'command': 'PUBLIC_CHATTER', 'step': 1,
        'channels': [], 'nodes': [],
    })


# "meshcore" and "meshtastic" both truncate to "me", which would make the
# two networks indistinguishable in exactly the place the distinction
# matters -- channel 2 on one is unrelated to channel 2 on the other.
_NETWORK_TAGS = {'meshcore': 'MC', 'meshtastic': 'MT', 'mqtt': 'MQ'}


def _network_tag(network: str) -> str:
    text = str(network or '').strip().casefold()
    return _NETWORK_TAGS.get(text, (text[:2] or '??').upper())


def _short_node(node_id: str) -> str:
    """A capture id is up to 64 hex characters; a line has about 30."""
    text = str(node_id or '')
    return text if len(text) <= 14 else f"{text[:8]}..{text[-4:]}"


def _chatter_filter_options(state: dict) -> list:
    """Both dimensions as one numbered list.

    Reuses get_public_chatter_filters, which already reports exactly what is
    present in the window -- offering a channel with nothing in it would be
    a filter that can only ever empty the screen.
    """
    filters = get_public_chatter_filters(int(state.get('hours', 24)))
    options = []
    for channel in filters.get('channels', []):
        network = str(channel.get('network') or '')
        index = int(channel.get('channel_index') or 0)
        name = str(channel.get('channel_name') or '') or f"Channel {index}"
        options.append({
            'kind': 'channel',
            'value': f"{network}/{index}",
            'label': f"{_network_tag(network)}/{name}"[:22],
        })
    for node_id in filters.get('capture_nodes', []):
        options.append({
            'kind': 'node',
            'value': str(node_id),
            'label': _short_node(node_id),
        })
    return options


def _chatter_filter_text(state: dict) -> str:
    options = state.get('filter_options') or []
    if not options:
        return LINE_BREAK.join([
            "Nothing heard in this window to filter.",
            "[T]ime [0] Back",
        ])
    chosen = set(state.get('channels') or []) | set(state.get('nodes') or [])
    lines = ["Filter  (* = shown)"]
    kind = None
    for number, option in enumerate(options, 1):
        if option['kind'] != kind:
            kind = option['kind']
            lines.append("Channels:" if kind == 'channel' else "Heard by:")
        mark = '*' if option['value'] in chosen else ' '
        lines.append(f"[{number}]{mark}{option['label']}")
    # Nothing selected means no constraint, the same as the web feed: the
    # first press narrows to one thing rather than needing the rest turned
    # off first.
    lines.append("Pick numbers to toggle.")
    lines.append("[A]ll [D]one")
    return LINE_BREAK.join(lines)


def _send_chatter_filter(sender_id, interface, state: dict) -> None:
    state['filter_options'] = _chatter_filter_options(state)
    state['step'] = 3
    send_message(_chatter_filter_text(state), sender_id, interface)
    update_user_state(sender_id, state)


def _chatter_entry_lines(entry: dict) -> str:
    when = str(entry.get('message_timestamp') or '').replace('T', ' ')[11:16]
    sender = str(entry.get('sender_long_name') or entry.get('sender_name')
                 or entry.get('sender_node_id') or '?')
    channel = (str(entry.get('channel_name') or '')
               or f"Ch{entry.get('channel_index', 0)}")
    head = f"{when} {sender} {_network_tag(entry.get('network'))}/{channel}"
    hops = entry.get('hops')
    if hops is not None:
        head += " direct" if hops == 0 else f" {hops}h"
    return f"{head}{LINE_BREAK}{entry.get('content') or ''}"


def _chatter_controls(state: dict, has_more: bool) -> str:
    controls = ['[M]ore'] if has_more else []
    if state.get('channels') or state.get('nodes'):
        controls.append('[F]ilter*')
    else:
        controls.append('[F]ilter')
    controls.extend(['[T]ime', '[0] Back'])
    return ' '.join(controls)


def _send_chatter_batch(sender_id, interface, state: dict, *, older: bool) -> None:
    result = get_public_chatter_history(
        hours=int(state['hours']),
        limit=CHATTER_BATCH,
        channel_keys=list(state.get('channels') or []),
        capture_node_ids=list(state.get('nodes') or []),
        before_time=state.get('before_time', '') if older else '',
        before_id=int(state.get('before_id', 0)) if older else 0,
    )
    entries = result.get('entries', [])
    state['step'] = 2

    if not entries:
        state['has_more'] = False
        send_message(
            LINE_BREAK.join([
                "No older chatter." if older else "Nothing heard in this window.",
                "[T]ime [0] Back",
            ]),
            sender_id, interface)
        update_user_state(sender_id, state)
        return

    # Fill to the byte ceiling rather than the entry count, so one very long
    # message does not turn a batch into a broadcast. The cursor follows what
    # was actually sent, so nothing is skipped on the next More.
    header = f"Public Chatter {state['hours']}h"
    included = []
    used = len(header.encode('utf-8'))
    for entry in entries:
        block = _chatter_entry_lines(entry)
        cost = len(block.encode('utf-8')) + 2
        if included and used + cost > CHATTER_BATCH_BYTES:
            break
        included.append((entry, block))
        used += cost

    last = included[-1][0]
    state['before_time'] = last['message_timestamp']
    state['before_id'] = last['id']
    # More is offered when the query had another page, or when the byte
    # ceiling stopped this one short of what was already fetched.
    state['has_more'] = bool(result.get('has_more')) or len(included) < len(entries)

    body = [header]
    body.extend(block for _, block in included)
    body.append(_chatter_controls(state, state['has_more']))
    send_message(LINE_BREAK.join(body), sender_id, interface)
    update_user_state(sender_id, state)


def _toggle_chatter_filter(state: dict, number: int) -> bool:
    options = state.get('filter_options') or []
    if not 1 <= number <= len(options):
        return False
    option = options[number - 1]
    key = 'channels' if option['kind'] == 'channel' else 'nodes'
    chosen = list(state.get(key) or [])
    if option['value'] in chosen:
        chosen.remove(option['value'])
    else:
        chosen.append(option['value'])
    state[key] = chosen
    return True


def handle_public_chatter_steps(sender_id, message, interface, state):
    choice = str(message or '').strip().lower()
    step = int(state.get('step', 1))

    if choice in ('0', 'x', 'exit'):
        handle_help_command(sender_id, interface, 'utilities')
        return
    if choice in ('t', 'time'):
        state.update({'step': 1, 'before_time': '', 'before_id': 0})
        send_message(_chatter_windows_text(), sender_id, interface)
        update_user_state(sender_id, state)
        return

    if step == 1:
        if not choice.isdigit() or not 1 <= int(choice) <= len(CHATTER_WINDOWS):
            send_message(
                f"Reply 1-{len(CHATTER_WINDOWS)} to pick a window, or 0 to exit.",
                sender_id, interface)
            return
        state['hours'] = CHATTER_WINDOWS[int(choice) - 1][0]
        state['before_time'] = ''
        state['before_id'] = 0
        _send_chatter_batch(sender_id, interface, state, older=False)
        return

    if step == 3:
        if choice in ('d', 'done'):
            state['before_time'] = ''
            state['before_id'] = 0
            _send_chatter_batch(sender_id, interface, state, older=False)
            return
        if choice in ('a', 'all'):
            state['channels'] = []
            state['nodes'] = []
            send_message(_chatter_filter_text(state), sender_id, interface)
            update_user_state(sender_id, state)
            return
        # Several numbers at once, so a combination is one reply rather than
        # one round trip per choice -- which over LoRa is the difference
        # between a filter and a chore.
        numbers = [part for part in re.split(r'[\s,]+', choice) if part.isdigit()]
        if numbers and all(_toggle_chatter_filter(state, int(n)) for n in numbers):
            send_message(_chatter_filter_text(state), sender_id, interface)
            update_user_state(sender_id, state)
            return
        send_message(
            "Reply with option numbers to toggle, A for all, or D when done.",
            sender_id, interface)
        return

    if choice in ('f', 'filter'):
        _send_chatter_filter(sender_id, interface, state)
        return
    if choice in ('m', 'more', 'n', 'next') and state.get('has_more'):
        _send_chatter_batch(sender_id, interface, state, older=True)
        return
    send_message(
        LINE_BREAK.join(["Reply:", _chatter_controls(state, bool(state.get('has_more')))]),
        sender_id, interface)


def _start_mail_recipient_selection(sender_id, interface) -> None:
    entries = get_mail_relay_directory(get_node_id_from_num(sender_id, interface))
    if not entries:
        send_message("No relay users are listed. [A] Enter an address [0] Cancel", sender_id, interface)
        update_user_state(sender_id, {'command': 'MAIL', 'step': 9, 'directory': [], 'directory_page': 0})
        return
    send_message(_mail_directory_page(entries, 0, selecting=True), sender_id, interface)
    update_user_state(sender_id, {
        'command': 'MAIL', 'step': 9, 'directory': entries, 'directory_page': 0,
    })


def _resolve_mail_relay_recipient(recipient: str, sender_node_id=None):
    query = str(recipient or '').strip().casefold()
    entries = get_mail_relay_directory(sender_node_id)
    node_matches = [
        entry for entry in entries
        if query in {node_id.casefold() for node_id in entry.get('node_ids', [])}
    ]
    if len(node_matches) == 1:
        return node_matches[0]
    alias_matches = [
        entry for entry in entries
        if entry.get('alias') and entry['alias'].casefold() == query
    ]
    if len(alias_matches) == 1:
        return alias_matches[0]
    short_matches = [
        entry for entry in entries
        if query in {name.casefold() for name in entry.get('short_names', [])}
    ]
    if len(short_matches) == 1:
        return short_matches[0]
    return None



def handle_bulletin_command(sender_id, interface):
    boards = get_bulletin_boards()
    board_options = "\n".join([f"[{index}] {board}" for index, board in enumerate(boards, start=1)])
    response = (
        f"📰Bulletin Menu📰\nWhich board would you like to enter?\n{board_options}"
        "\nReply with board number, name, or first letter.\n[0] Back"
    )
    send_message(response, sender_id, interface)
    update_user_state(sender_id, {'command': 'BULLETIN_MENU', 'step': 1, 'boards': boards})


def handle_stats_command(sender_id, interface):
    response = "📊Stats Menu📊\nWhat stats would you like to view?\n[1]Nodes [2]Hardware [3]Roles [0]Back"
    send_message(response, sender_id, interface)
    update_user_state(sender_id, {'command': 'STATS', 'step': 1})


def handle_fortune_command(sender_id, interface):
    try:
        with open('fortunes.txt', 'r') as file:
            fortunes = file.readlines()
        if not fortunes:
            send_message("No fortunes available.", sender_id, interface)
        else:
            fortune = random.choice(fortunes).strip()
            decorated_fortune = f"🔮 {fortune} 🔮"
            send_message(decorated_fortune, sender_id, interface)
    except Exception as e:
        send_message(f"Error generating fortune: {e}", sender_id, interface)
    handle_help_command(sender_id, interface, 'utilities')


def handle_games_command(sender_id, interface):
    menu = "🎮 Games 🎮\n"
    for i, (game_id, info) in enumerate(GAME_LIST, start=1):
        menu += f"[{i}] {info['name']}\n"
    menu += "[S]cores [H]all of Fame [0]Back"
    sync_notice = get_zork_save_sync_notice()
    if sync_notice:
        menu += f"\n\n{sync_notice}"
    send_message(menu, sender_id, interface)
    update_user_state(sender_id, {'command': 'GAMES_MENU', 'step': 1})


def handle_games_steps(sender_id, message, interface):
    choice = message.strip()
    if choice.lower() in ('x', '0', 'exit'):
        handle_help_command(sender_id, interface, 'utilities')
        return

    if choice.lower() == 's':
        handle_scoreboard_command(sender_id, interface)
        return

    if choice.lower() == 'h':
        handle_hall_of_fame_command(sender_id, interface)
        return

    try:
        idx = int(choice) - 1
        if idx < 0:
            raise ValueError
        game_id, info = GAME_LIST[idx]
    except (ValueError, IndexError):
        send_message(
            f"Invalid choice. Enter 1-{len(GAME_LIST)}, S for scores, or 0.",
            sender_id, interface
        )
        return

    _launch_game(sender_id, interface, game_id, info['name'])


def _launch_game(sender_id, interface, game_id, game_name):
    if game_id == trivia_port.GAME_ID:
        send_message(trivia_port.start(sender_id), sender_id, interface)
        update_user_state(sender_id, {'command': 'TRIVIA', 'step': 1, 'game_id': game_id})
        return
    sync_notice = get_zork_save_sync_notice()
    if has_zork_session(sender_id, game_id):
        intro = resume_zork_session(sender_id, game_id)
        send_message(intro, sender_id, interface)
        if sync_notice:
            send_message(sync_notice, sender_id, interface)
        send_message(f"{game_name} resumed. Send X to exit.", sender_id, interface)
    elif has_zork_save(sender_id, game_id):
        intro = start_zork_session(sender_id, game_id)
        send_message(intro, sender_id, interface)
        if sync_notice:
            send_message(sync_notice, sender_id, interface)
        send_message(f"Saved game restored. Send X to exit.", sender_id, interface)
    else:
        intro = start_zork_session(sender_id, game_id)
        send_message(intro, sender_id, interface)
        if sync_notice:
            send_message(sync_notice, sender_id, interface)
        send_message(
            f"{game_name} started. Send commands (LOOK, NORTH, TAKE LAMP). Send X to exit.",
            sender_id, interface
        )
    update_user_state(sender_id, {'command': 'ZORK', 'step': 1, 'game_id': game_id})


def handle_hall_of_fame_command(sender_id, interface):
    rows = get_hall_of_fame()
    if not rows:
        send_message("🏛 Hall of Fame\nNo scores recorded yet. Start playing!", sender_id, interface)
        handle_games_command(sender_id, interface)
        return
    by_game = {r[0]: r for r in rows}
    lines = ["🏛 Hall of Fame 🏛"]
    for game_id, info in GAME_LIST:
        if game_id in by_game:
            _, short_name, score, max_score, moves = by_game[game_id]
            ms = f"/{max_score}" if max_score else ""
            lines.append(f"{info['name']}: {short_name} {score}{ms} {moves}mv")
        else:
            lines.append(f"{info['name']}: —")
    send_message("\n".join(lines), sender_id, interface)
    handle_games_command(sender_id, interface)


def handle_zork_command(sender_id, interface):
    """Legacy entry point – redirects to the games menu."""
    handle_games_command(sender_id, interface)


# ── API gateway (apigw) — user-facing ────────────────────────────────────────

def _apigw_authorized(sender_id, interface) -> bool:
    """Whether this node may use a gateway. Honours the gateway-specific
    [gateway] allowed_nodes lock-down when set, otherwise falls back to the
    general [allow_list] (empty = open)."""
    import gateway
    node_id = get_node_id_from_num(sender_id, interface)
    return gateway.is_requester_authorized(node_id, getattr(interface, 'allowed_nodes', None))


def handle_apigw_command(sender_id, interface):
    if not _apigw_authorized(sender_id, interface):
        send_message("API gateway: your node is not on the allow-list.", sender_id, interface)
        handle_help_command(sender_id, interface)
        return
    send_message("Enter the URL to fetch (must be an allowed host), or 0 to cancel:", sender_id, interface)
    update_user_state(sender_id, {'command': 'APIGW', 'step': 2, 'mode': 'http'})


_APIGW_UNIT_SEP = "\x1f"

# "Asked … reply will arrive shortly" is only worth a packet when the answer
# is genuinely slow (e.g. the model is cold-loading). A warm endpoint answers
# in a second or two, and sending the ack first put the answer on the air
# while the ack -- a reliable multi-hop DM -- was still being relayed; the
# two collided and the answer was lost, every time, until the model was
# evicted again. So the ack is armed on a timer and cancelled if the answer
# wins the race.
APIGW_SLOW_ACK_SECONDS = 8.0
# If the slow ack DID go out, hold the answer until the ack's relay/ack
# traffic has cleared the mesh (a 2-hop ack cycle was observed at ~17s).
APIGW_ACK_CLEAR_SECONDS = 15.0


def _apigw_submit(sender_id, interface, kind, payload, label):
    """Dispatch a composed request: fulfill locally if this node is a gateway,
    otherwise forward to a gateway peer. Response returns asynchronously.

    kind == 'r' (AI relay, e.g. Project Nomad) gets a post-reply follow-up
    prompt offering another question or a trip back to the main menu --
    kind == 'h' (HTTP GET) does not, matching the original one-shot flow."""
    import gateway
    import uuid as _uuid
    node_id = get_node_id_from_num(sender_id, interface)
    rid = _uuid.uuid4().hex[:6]

    if gateway.is_gateway_enabled():
        # The reply comes back on a worker thread, up to the gateway's
        # request timeout later. Hold the LINK, not this interface object:
        # if the radio reconnects in that window the link gets a brand-new
        # interface and the one captured here is a closed port, so every
        # send on it fails -- silently, from the user's side.
        try:
            import server as _server
            link = _server.link_for_interface(interface)
        except Exception:
            link = None

        ack_lock = threading.Lock()
        ack_state = {'sent_at': None, 'answered': False}

        def _send_slow_ack():
            with ack_lock:
                if ack_state['answered']:
                    return
                ack_state['sent_at'] = time.monotonic()
            live = getattr(link, 'interface', None) or interface
            send_message(f"Asked {label}… reply will arrive shortly.", sender_id, live)

        ack_timer = threading.Timer(APIGW_SLOW_ACK_SECONDS, _send_slow_ack)
        ack_timer.daemon = True

        # Local fast path: no mesh round-trip; DM the result straight back.
        def _reply(status, body):
            ack_timer.cancel()
            with ack_lock:
                ack_state['answered'] = True
                acked_at = ack_state['sent_at']
            if acked_at is not None:
                wait = APIGW_ACK_CLEAR_SECONDS - (time.monotonic() - acked_at)
                if wait > 0:
                    time.sleep(wait)  # on the gateway worker thread
            live = getattr(link, 'interface', None) or interface
            prefix = "" if str(status) in ("200", "OK") else f"[{status}] "
            text = f"{prefix}{body}"
            # AI replies carry the follow-up invitation in the SAME message
            # -- see deliver_ask_nomad_reply for why a second packet here
            # loses races against the first one's relay traffic.
            ok = (deliver_ask_nomad_reply(text, sender_id, live) if kind == 'r'
                  else send_message(text, sender_id, live))
            if not ok:
                logging.warning(
                    f"apigw rid={rid}: {label} answered but the reply could not "
                    f"be delivered to {node_id}")
        ack_timer.start()
        gateway.handle_apireq(rid, node_id, kind, payload,
                              getattr(interface, 'allowed_nodes', None), _reply)
        update_user_state(sender_id, None)
        return

    peer = select_gateway_peer(interface)
    if not peer:
        send_message("No internet gateway is reachable on the mesh right now.", sender_id, interface)
        update_user_state(sender_id, None)
        return
    register_api_request(rid, sender_id, gateway_node_id=peer, kind=kind)
    if not send_api_request(rid, node_id, kind, payload, peer, interface):
        from utils import pop_api_request
        pop_api_request(rid)
        send_message("Request too long for one packet — please shorten it.", sender_id, interface)
        update_user_state(sender_id, None)
        return
    send_message(f"Sent to gateway {peer} via {label}… waiting for reply.", sender_id, interface)
    update_user_state(sender_id, None)  # response/timeout delivered asynchronously -- see
    # message_processing._deliver_api_response, which shows the same
    # follow-up prompt for kind='r' once the mesh reply actually lands.


def handle_apigw_steps(sender_id, message, interface):
    choice = message.strip()
    state = get_user_state(sender_id) or {}
    step = state.get('step', 1)
    if choice.lower() in ('x', '0', 'exit'):
        handle_help_command(sender_id, interface)
        return

    if step == 1:
        if choice == '1':
            update_user_state(sender_id, {'command': 'APIGW', 'step': 2, 'mode': 'ai'})
            send_message("Type your question for Project Nomad:", sender_id, interface)
        elif choice == '2':
            update_user_state(sender_id, {'command': 'APIGW', 'step': 2, 'mode': 'http'})
            send_message("Enter the URL to GET (must be an allowed host):", sender_id, interface)
        else:
            send_message("Send 1, 2, or 0 to exit.", sender_id, interface)
        return

    # step 2 — the composed input
    mode = state.get('mode', 'ai')
    if not choice:
        send_message("Empty input — cancelled.", sender_id, interface)
        handle_help_command(sender_id, interface)
        return
    if mode == 'ai':
        _apigw_submit(sender_id, interface, 'r', f"ai{_APIGW_UNIT_SEP}{choice}", "Project Nomad")
    else:
        _apigw_submit(sender_id, interface, 'h', f"GET{_APIGW_UNIT_SEP}{choice}{_APIGW_UNIT_SEP}", "HTTP")


# ── Ask Nomad: homescreen shortcut + post-reply follow-up ──────────────────
#
# Skips Utilities > API Gateway > [1] Ask Project Nomad for the common case
# of just wanting to ask a question, and lets the user immediately ask a
# follow-up (or return to the main menu) once a reply arrives, instead of
# re-navigating the whole menu tree for every question.

def handle_ask_nomad_command(sender_id, interface):
    """Main-menu shortcut ('N'): jumps straight to the question prompt."""
    if not _apigw_authorized(sender_id, interface):
        send_message("API gateway: your node is not on the allow-list.", sender_id, interface)
        handle_help_command(sender_id, interface)
        return
    send_message("Type your question for Project Nomad:", sender_id, interface)
    update_user_state(sender_id, {'command': 'ASK_NOMAD', 'step': 1})


ASK_NOMAD_FOLLOWUP = "Reply with another question, or [0] for the main menu."


def deliver_ask_nomad_reply(body, sender_id, interface) -> bool:
    """Send a Project Nomad answer WITH the follow-up invitation attached.

    One radio message, not two, and that matters more than it looks. A DM to
    a multi-hop node takes several seconds to arrive and be acked, but
    send_message paces at two seconds -- so a burst of three (the "asked
    shortly" ack, the answer, the invitation) puts packets two and three on
    the air while the first is still being relayed, and they collide with
    its own relay traffic. Radio-level logs showed exactly that: all three
    accepted by the radio, only the first ever acked by the destination.

    Menu traffic never hit this because a human takes longer than the mesh
    does between selections. This reply is the only burst the BBS emits.
    """
    text = str(body or "").rstrip()
    combined = (text + LINE_BREAK + ASK_NOMAD_FOLLOWUP) if text else ASK_NOMAD_FOLLOWUP
    delivered = send_message(combined, sender_id, interface)
    update_user_state(sender_id, {'command': 'ASK_NOMAD', 'step': 1})
    return delivered


def _prompt_ask_nomad_followup(sender_id, interface) -> None:
    """Invitation on its own, for paths with no answer text to attach it to.

    Reusing 'ASK_NOMAD' state here (same as the homescreen shortcut) means
    the very next message is either treated as a new question or, for
    [0]/x/exit, sent back to the MAIN menu specifically -- not Utilities,
    which the shared handle_apigw_steps() always does regardless of entry
    point."""
    send_message(ASK_NOMAD_FOLLOWUP, sender_id, interface)
    update_user_state(sender_id, {'command': 'ASK_NOMAD', 'step': 1})


def handle_ask_nomad_steps(sender_id, message, interface):
    choice = message.strip()
    if choice.lower() in ('0', 'x', 'exit'):
        handle_help_command(sender_id, interface)  # back to the main menu
        return
    if not choice:
        send_message("Empty question — cancelled.", sender_id, interface)
        handle_help_command(sender_id, interface)
        return
    _apigw_submit(sender_id, interface, 'r', f"ai{_APIGW_UNIT_SEP}{choice}", "Project Nomad")


def handle_scoreboard_command(sender_id, interface):
    menu = "🏆 Scoreboard 🏆\n"
    for i, (game_id, info) in enumerate(GAME_LIST, start=1):
        menu += f"[{i}] {info['name']}\n"
    menu += "[0] Back"
    send_message(menu, sender_id, interface)
    update_user_state(sender_id, {'command': 'SCOREBOARD', 'step': 1})


def handle_scoreboard_steps(sender_id, message, interface):
    choice = message.strip()
    if choice in ('0', 'x', 'back'):
        handle_games_command(sender_id, interface)
        return
    try:
        idx = int(choice) - 1
        if idx < 0:
            raise ValueError
        game_id, info = GAME_LIST[idx]
    except (ValueError, IndexError):
        send_message(f"Enter 1-{len(GAME_LIST)} or 0 to go back.", sender_id, interface)
        return
    scores = get_game_scoreboard(game_id, limit=5)
    if not scores:
        send_message(f"No scores yet for {info['name']}. Be first!", sender_id, interface)
    else:
        lines = [f"🏆 {info['name']}"]
        for rank, (short_name, score, max_score, moves) in enumerate(scores, 1):
            ms = f"/{max_score}" if max_score else ""
            lines.append(f"{rank}. {short_name} {score}{ms} {moves}mv")
        send_message("\n".join(lines), sender_id, interface)
    update_user_state(sender_id, {'command': 'SCOREBOARD', 'step': 1})


_SETTINGS_MENU_TEXT = (
    "⚙️ Settings" + LINE_BREAK
    + "[1] Linked Devices" + LINE_BREAK
    + "[0] Back"
)


def handle_settings_command(sender_id, interface):
    """Open Linked Devices directly from its discoverable main-menu entry."""
    handle_account_command(sender_id, interface, return_to='main')


def handle_settings_steps(sender_id, message, interface, sender_node_id):
    choice = message.strip().lower()
    if choice in ('0', 'x', 'back', 'exit'):
        handle_help_command(sender_id, interface)
        return
    if choice == '1':
        handle_account_command(sender_id, interface, return_to='settings')
        return
    send_message(_SETTINGS_MENU_TEXT, sender_id, interface)


def handle_profile_command(sender_id, interface, notice=None):
    profile = get_user_profile(sender_id)
    if not profile:
        send_message("No profile yet - send any command to create one!", sender_id, interface)
        return
    _, short_name, long_name, first_seen, last_seen, msg_count, bio = profile
    first_date = first_seen[:10] if first_seen else "?"
    scores = get_user_game_scores(sender_id)
    lines = [f"👤 {short_name}", f"Since:{first_date} Msgs:{msg_count}"]
    if scores:
        parts = []
        for game_id, score, max_score in scores[:3]:
            gname = GAMES.get(game_id, {}).get('name', game_id)[:8]
            ms = f"/{max_score}" if max_score else ""
            parts.append(f"{gname}:{score}{ms}")
        lines.append("Scores: " + " ".join(parts))
    if bio:
        lines.append(f"Bio: {bio}")
    node_id = get_node_id_from_num(sender_id, interface)
    relay_status = "On" if get_mail_relay_preference(node_id) else "Off"
    lines.append(f"[1]Edit Bio [3]Offline Relay:{relay_status} [0]Back")
    if notice:
        lines.insert(0, notice)
    send_message("\n".join(lines), sender_id, interface)
    update_user_state(sender_id, {'command': 'PROFILE', 'step': 1})


def handle_profile_steps(sender_id, message, interface, sender_node_id=None):
    state = get_user_state(sender_id) or {}
    choice = message.strip()
    if state.get('step') == 2:
        if choice.lower() in ('0', 'x', 'cancel'):
            handle_profile_command(sender_id, interface)
            return
        bio = choice[:100]
        update_user_bio(sender_id, bio)
        send_message("Bio updated!", sender_id, interface)
        handle_profile_command(sender_id, interface)
        return
    if state.get('step') == 3:
        if choice.lower() not in ('y', 'yes'):
            send_message("Relay setting unchanged.", sender_id, interface)
            handle_profile_command(sender_id, interface)
            return
        if not sender_node_id:
            send_message("Couldn't verify your device identity.", sender_id, interface)
            handle_profile_command(sender_id, interface)
            return
        records = set_mail_relay_for_node(
            sender_node_id, bool(state.get('relay_enabled')), home_network(sender_node_id)
        )
        for node_id, enabled, updated_at in records:
            send_mail_relay_preference_to_bbs_nodes(
                node_id, enabled, updated_at, interface.bbs_nodes, interface
            )
        status = "enabled" if state.get('relay_enabled') else "disabled"
        send_message(f"Offline mail relay {status} for all linked devices.", sender_id, interface)
        handle_profile_command(sender_id, interface)
        return
    if choice.lower() in ('0', 'x', 'back', 'exit'):
        handle_help_command(sender_id, interface)
        return
    if choice.lower() in ('e', '1'):
        send_message("Enter your bio (max 100 chars), or 0 to cancel:", sender_id, interface)
        update_user_state(sender_id, {'command': 'PROFILE', 'step': 2})
        return
    if choice.lower() in ('d', '2'):
        handle_account_command(sender_id, interface)
        return
    if choice == '3':
        if not sender_node_id:
            send_message("Couldn't verify your device identity.", sender_id, interface)
            return
        enabled = not get_mail_relay_preference(sender_node_id)
        action = "Enable" if enabled else "Disable"
        send_message(f"{action} offline mail relay for all linked devices? [Y/N]", sender_id, interface)
        update_user_state(sender_id, {'command': 'PROFILE', 'step': 3, 'relay_enabled': enabled})
        return
    # Redrawing the identical screen with no notice looked like the BBS had
    # ignored the keypress rather than rejected it.
    handle_profile_command(sender_id, interface, notice="Invalid choice.")


# ---------------------------------------------------------------------------
# Multi-device user accounts: link/verify/list/delete + shared display alias.
#
# Nested under the Profile menu (not a new top-level menu letter) so it
# needs no config.ini menu-item change. Numeric choices throughout (1-5, 0)
# deliberately avoid colliding with the single-letter top-level menu
# commands (q/b/u/p/x), which -- per message_processing.py's routing --
# always win over an in-progress flow if typed, exactly like every other
# existing multi-step flow in this file.
#
# SECURITY: every function here that identifies "which device is acting"
# takes sender_node_id (the packet's string fromId) as an explicit
# parameter -- never derives it from the numeric sender_id via
# get_node_id_from_num(), which is a live/mutable lookup against
# interface.nodes that isn't a reliable identity proof. sender_node_id is
# threaded in from message_processing.py's routing, which already has it
# in scope from on_receive().
# ---------------------------------------------------------------------------

_ACCOUNT_MENU_TEXT = (
    "\U0001F517 Linked Devices\n"
    "[1] Request link code\n"
    "[2] Enter a code\n"
    "[3] List my devices\n"
    "[4] Set shared alias\n"
    "[5] Unlink a device\n"
    "[6] Request code, delayed (dual-boot)\n"
    "[0] Back"
)


def _account_link_code_ttl_minutes() -> int:
    return _config_int('accounts', 'link_code_ttl_minutes', 10)


def _account_link_code_delay_minutes() -> int:
    return _config_int('accounts', 'link_code_delay_minutes', 2)


def _account_link_requests_per_hour() -> int:
    return _config_int('accounts', 'link_requests_per_hour', 3)


def _account_link_attempts_per_hour() -> int:
    return _config_int('accounts', 'link_attempts_per_hour', 5)


def _account_max_linked_devices() -> int:
    return _config_int('accounts', 'max_linked_devices', 6)


def _account_state(sender_id, step, **values):
    state = {'command': 'ACCOUNT', 'step': step}
    return_to = (get_user_state(sender_id) or {}).get('return_to')
    if return_to:
        state['return_to'] = return_to
    state.update(values)
    update_user_state(sender_id, state)


def handle_account_command(sender_id, interface, return_to=None):
    previous_return = (get_user_state(sender_id) or {}).get('return_to')
    send_message(_ACCOUNT_MENU_TEXT, sender_id, interface)
    state = {'command': 'ACCOUNT', 'step': 1}
    if return_to or previous_return:
        state['return_to'] = return_to or previous_return
    update_user_state(sender_id, state)


def handle_account_steps(sender_id, message, interface, sender_node_id=None):
    if sender_node_id is None:
        # Should never happen for a real interactive DM -- on_receive()
        # always passes it. Defensive guard rather than trusting the
        # numeric sender_id for anything identity-related here.
        send_message("Couldn't verify your device identity. Please try again.", sender_id, interface)
        update_user_state(sender_id, None)
        return

    state = get_user_state(sender_id) or {}
    step = state.get('step', 1)
    choice = message.strip()
    choice_lower = choice.lower()

    if step == 1:
        if choice_lower in ('0', 'x', 'back', 'exit'):
            if state.get('return_to') == 'settings':
                handle_settings_command(sender_id, interface)
            elif state.get('return_to') == 'main':
                handle_help_command(sender_id, interface)
            else:
                handle_profile_command(sender_id, interface)
            return
        if choice == '1':
            _handle_request_link_code(sender_id, interface, sender_node_id)
            return
        if choice == '2':
            send_message("Enter the 6-digit code from your other device, or 0 to cancel:", sender_id, interface)
            _account_state(sender_id, 2)
            return
        if choice == '3':
            _handle_list_devices(sender_id, interface, sender_node_id)
            return
        if choice == '4':
            send_message(
                "Enter a shared alias (max 20 chars). Shown instead of this "
                "device's short name on your posts once you have at least "
                "one linked device. Send 0 to cancel:",
                sender_id, interface,
            )
            _account_state(sender_id, 4)
            return
        if choice == '5':
            _handle_start_unlink(sender_id, interface, sender_node_id)
            return
        if choice == '6':
            _handle_request_link_code(sender_id, interface, sender_node_id, delayed=True)
            return
        send_message(_ACCOUNT_MENU_TEXT, sender_id, interface)
        return

    if step == 2:
        if choice_lower in ('0', 'x', 'cancel'):
            handle_account_command(sender_id, interface)
            return
        _handle_submit_link_code(sender_id, interface, sender_node_id, choice)
        return

    if step == 4:
        if choice_lower in ('0', 'x', 'cancel'):
            handle_account_command(sender_id, interface)
            return
        _handle_set_alias(sender_id, interface, sender_node_id, choice)
        return

    if step == 5:
        _handle_pick_unlink_target(sender_id, interface, choice, state)
        return

    if step == 6:
        _handle_confirm_unlink(sender_id, interface, choice, state)
        return

    handle_account_command(sender_id, interface)


def _handle_request_link_code(sender_id, interface, sender_node_id, delayed=False):
    """Issue a link code.

    ``delayed`` holds the code back by link_code_delay_minutes and then
    sends it to every device already linked to the account, rather than
    replying immediately to the requester. That exists for a dual-boot
    device: it has to reboot into its other protocol before it can receive
    anything, and an immediate reply is simply gone by then.

    The TTL is extended by the delay so the window to actually redeem the
    code is the same as an ordinary request -- otherwise waiting for the
    message would eat most of it.
    """
    if not link_rate_limit_ok(sender_node_id, 'request_code', _account_link_requests_per_hour()):
        send_message("Too many link-code requests recently. Try again later.", sender_id, interface)
        handle_account_command(sender_id, interface)
        return
    account_id = get_account_id_for_node(sender_node_id)
    if account_id is None:
        # Bootstrap: requesting a code with no account yet creates one --
        # "link a second device" and "create my first account" are the
        # same code path, so no separate "create account" step is needed.
        account_id = create_account()
        link_node_to_account(sender_node_id, account_id, home_network(sender_node_id))

    delay = _account_link_code_delay_minutes() if delayed else 0
    ttl = _account_link_code_ttl_minutes() + delay
    code = create_link_code(account_id, sender_node_id, ttl_minutes=ttl)
    record_link_attempt(sender_node_id, 'request_code', True)

    if not delayed:
        send_message(
            "Your link code: " + str(code) + LINE_BREAK
            + "Valid for " + str(ttl) + " minutes, one-time use. "
            "Enter it from your OTHER device: Profile > Linked Devices > "
            "[2] Enter a code.",
            sender_id, interface,
        )
        handle_account_command(sender_id, interface)
        return

    queue_delayed_link_code(account_id, code, sender_node_id, delay, ttl)
    others = [n for n in get_linked_node_ids(account_id) if n != sender_node_id]
    if others:
        send_message(
            "Link code queued. In " + str(delay) + " minute(s) it will be sent to "
            "your " + str(len(others)) + " other linked device(s). Reboot into the "
            "other protocol now; the code stays valid for " + str(ttl) + " minutes.",
            sender_id, interface,
        )
    else:
        # Nothing else is linked yet, so a delayed send can only come back to
        # this same node. Say so plainly rather than implying it will reach an
        # identity the account has never seen.
        send_message(
            "Link code queued and will be sent here in " + str(delay) + " minute(s). "
            "NOTE: no other devices are linked yet, so it can only come back to "
            "THIS node -- if this device reboots into another protocol it returns "
            "as a new identity and will not receive it. For a first-time link, "
            "use [1] instead.",
            sender_id, interface,
        )
    handle_account_command(sender_id, interface)


def _handle_submit_link_code(sender_id, interface, sender_node_id, code):
    if not link_rate_limit_ok(sender_node_id, 'submit_code', _account_link_attempts_per_hour()):
        record_link_attempt(sender_node_id, 'submit_code', False)
        send_message("Too many attempts. Try again later.", sender_id, interface)
        handle_account_command(sender_id, interface)
        return
    ok, msg = redeem_link_code(
        code, sender_node_id, home_network(sender_node_id),
        max_devices=_account_max_linked_devices(),
    )
    record_link_attempt(sender_node_id, 'submit_code', ok)
    send_message(msg, sender_id, interface)
    handle_account_command(sender_id, interface)


def _handle_list_devices(sender_id, interface, sender_node_id):
    account_id = get_account_id_for_node(sender_node_id)
    if account_id is None:
        send_message("No linked devices yet. Choose [1] to get a link code.", sender_id, interface)
        handle_account_command(sender_id, interface)
        return
    detail = get_linked_nodes_detail(account_id)
    alias = get_account_alias(account_id)
    lines = [f"\U0001F517 Account alias: {alias or '(none set)'}"]
    for i, (node_id, network, _linked_at) in enumerate(detail):
        marker = " (this device)" if node_id == sender_node_id else ""
        lines.append(f"{i + 1:02d}. {node_id} [{network}]{marker}")
    send_message("\n".join(lines), sender_id, interface)
    handle_account_command(sender_id, interface)


def _handle_set_alias(sender_id, interface, sender_node_id, alias_text):
    account_id = get_account_id_for_node(sender_node_id)
    if account_id is None:
        send_message("You don't have any linked devices yet. Get a link code first.", sender_id, interface)
        handle_account_command(sender_id, interface)
        return
    alias = alias_text.strip()[:20]
    if not set_account_alias(account_id, alias):
        # The alias is the byline on everything this account posts, so
        # letting two accounts share one would be impersonation.
        send_message(f'"{alias}" is already taken. Pick a different alias.', sender_id, interface)
        handle_account_command(sender_id, interface)
        return
    send_message(f'Alias set to "{alias}".' if alias else 'Alias cleared.', sender_id, interface)
    handle_account_command(sender_id, interface)


def _handle_start_unlink(sender_id, interface, sender_node_id):
    account_id = get_account_id_for_node(sender_node_id)
    if account_id is None:
        send_message("No linked devices to unlink.", sender_id, interface)
        handle_account_command(sender_id, interface)
        return
    detail = get_linked_nodes_detail(account_id)
    if len(detail) <= 1:
        send_message("You only have one device linked -- nothing to unlink.", sender_id, interface)
        handle_account_command(sender_id, interface)
        return
    lines = ["Reply with the number of the device to unlink:"]
    for i, (node_id, network, _linked_at) in enumerate(detail):
        lines.append(f"{i + 1:02d}. {node_id} [{network}]")
    send_message("\n".join(lines), sender_id, interface)
    _account_state(sender_id, 5, devices=detail)


def _handle_pick_unlink_target(sender_id, interface, choice, state):
    devices = state.get('devices', [])
    try:
        idx = int(choice) - 1
    except ValueError:
        idx = -1
    if idx < 0 or idx >= len(devices):
        send_message("Invalid selection.", sender_id, interface)
        handle_account_command(sender_id, interface)
        return
    node_id = devices[idx][0]
    send_message(f"Unlink {node_id}? [Y]es [N]o", sender_id, interface)
    _account_state(sender_id, 6, unlink_node_id=node_id)


def _handle_confirm_unlink(sender_id, interface, choice, state):
    node_id = state.get('unlink_node_id')
    if choice.strip().lower() in ('y', 'yes', '1'):
        if node_id and unlink_node(node_id):
            send_message(f"Unlinked {node_id}.", sender_id, interface)
        else:
            send_message("Couldn't unlink that device (it may already be your only one).", sender_id, interface)
    else:
        send_message("Cancelled.", sender_id, interface)
    handle_account_command(sender_id, interface)


def handle_zork_steps(sender_id, message, interface):
    state = get_user_state(sender_id) or {}
    game_id = state.get('game_id', 'zork1')
    choice = message.strip()
    if len(choice) == 2 and choice[1].lower() == 'x':
        choice = choice[0]

    if choice.lower() in ('save', 'restore'):
        send_message("Your game auto-saves after each command. No manual save needed.", sender_id, interface)
        update_user_state(sender_id, {'command': 'ZORK', 'step': 1, 'game_id': game_id})
        return

    if choice.lower() in ('x', 'quit', 'exit'):
        stop_zork_session(sender_id, game_id)
        send_message("Exited game.", sender_id, interface)
        handle_help_command(sender_id, interface, 'utilities')
        return

    response = send_zork_command(sender_id, choice, game_id)
    send_message(response, sender_id, interface)
    # Capture score if the game output contains one
    parsed = parse_game_score(response)
    if parsed:
        score, max_score, moves = parsed
        node_id = get_node_id_from_num(sender_id, interface)
        short_name = get_node_short_name(node_id, interface) or str(sender_id)
        upsert_game_score(sender_id, game_id, short_name, score, max_score, moves)
    update_user_state(sender_id, {'command': 'ZORK', 'step': 1, 'game_id': game_id})


def handle_trivia_steps(sender_id, message, interface):
    """Route input to the active Trivia King door and persist its score."""
    state = get_user_state(sender_id) or {}
    game_id = state.get('game_id', trivia_port.GAME_ID)
    response = trivia_port.command(sender_id, message)
    send_message(response, sender_id, interface)
    if not trivia_port.active(sender_id):
        score, moves = trivia_port.finish_score(sender_id)
        node_id = get_node_id_from_num(sender_id, interface)
        short_name = get_node_short_name(node_id, interface) or str(sender_id)
        upsert_game_score(sender_id, game_id, short_name, score, 0, moves)
        handle_games_command(sender_id, interface)
    else:
        update_user_state(sender_id, {'command': 'TRIVIA', 'step': 1, 'game_id': game_id})


def handle_stats_steps(sender_id, message, step, interface):
    message = message.lower().strip()
    if len(message) == 2 and message[1] == 'x':
        message = message[0]

    if step == 1:
        _stats_alias = {'1': 'n', '2': 'h', '3': 'r', '0': 'x'}
        choice = _stats_alias.get(message, message)
        if choice == 'x':
            handle_help_command(sender_id, interface)
            return
        elif choice == 'n':
            current_time = int(time.time())
            timeframes = {
                "All time": None,
                "Last 24 hours": 86400,
                "Last 8 hours": 28800,
                "Last hour": 3600
            }
            total_nodes_summary = []

            for period, seconds in timeframes.items():
                if seconds is None:
                    total_nodes = len(interface.nodes)
                else:
                    time_limit = current_time - seconds
                    total_nodes = sum(1 for node in interface.nodes.values() if node.get('lastHeard') is not None and node['lastHeard'] >= time_limit)
                total_nodes_summary.append(f"- {period}: {total_nodes}")

            response = "Total nodes seen:\n" + "\n".join(total_nodes_summary)
            send_message(response, sender_id, interface)
            handle_stats_command(sender_id, interface)
        elif choice == 'h':
            hw_models = {}
            for node in interface.nodes.values():
                hw_model = node['user'].get('hwModel', 'Unknown')
                hw_models[hw_model] = hw_models.get(hw_model, 0) + 1
            response = "Hardware Models:\n" + "\n".join([f"{model}: {count}" for model, count in hw_models.items()])
            send_message(response, sender_id, interface)
            handle_stats_command(sender_id, interface)
        elif choice == 'r':
            roles = {}
            for node in interface.nodes.values():
                role = node['user'].get('role', 'Unknown')
                roles[role] = roles.get(role, 0) + 1
            response = "Roles:\n" + "\n".join([f"{role}: {count}" for role, count in roles.items()])
            send_message(response, sender_id, interface)
            handle_stats_command(sender_id, interface)


def handle_bb_steps(sender_id, message, step, state, interface, bbs_nodes):
    boards = state.get('boards', get_bulletin_boards()) if state else get_bulletin_boards()
    if step == 1:
        if message.lower() in ('e', 'x'):
            handle_help_command(sender_id, interface, 'bbs')
            return

        board_index = None
        message_clean = message.strip()
        message_lower = message_clean.lower()

        if message_clean.isdigit():
            parsed_index = int(message_clean)
            if 1 <= parsed_index <= len(boards):
                board_index = parsed_index - 1
            elif 0 <= parsed_index < len(boards):
                board_index = parsed_index
        else:
            name_lookup = {board.lower(): index for index, board in enumerate(boards)}
            if message_lower in name_lookup:
                board_index = name_lookup[message_lower]
            elif len(message_lower) == 1:
                matching_indexes = [index for index, board in enumerate(boards) if board.lower().startswith(message_lower)]
                if len(matching_indexes) == 1:
                    board_index = matching_indexes[0]

        if board_index is None:
            send_message("Invalid board selection. Use number, board name, or first letter.", sender_id, interface)
            handle_bulletin_command(sender_id, interface)
            return

        send_board_action_menu(sender_id, interface, boards[board_index], boards)

    elif step == 2:
        board_name = state['board']
        if message.lower() == 'r':
            bulletins = get_bulletins(board_name)
            if bulletins:
                send_message(f"Select a bulletin number to view from {board_name}:", sender_id, interface)
                for bulletin in bulletins:
                    send_message(f"[{bulletin[0]}] {bulletin[1]}", sender_id, interface)
                update_user_state(sender_id, {'command': 'BULLETIN_READ', 'step': 3, 'board': board_name})
            else:
                send_message(f"No bulletins in {board_name}.", sender_id, interface)
                handle_bb_steps(sender_id, 'e', 1, state, interface, bbs_nodes)
        elif message.lower() == 'p':
            if board_name.lower() == 'urgent':
                node_id = get_node_id_from_num(sender_id, interface)
                allow_lists = _urgent_board_allow_lists(interface)
                logging.info(f"Checking permissions for node_id: {node_id} with allowed_nodes: {allow_lists}")  # Debug statement
                # Empty everywhere = no restriction configured = open to all
                # (matches the original single-list behavior exactly).
                if any(allow_lists) and not account_authorized(node_id, allow_lists):
                    send_message("You don't have permission to post to this board.", sender_id, interface)
                    handle_bb_steps(sender_id, 'e', 1, state, interface, bbs_nodes)
                    return
            send_message("What is the subject of your bulletin? Keep it short. [0] Cancel", sender_id, interface)
            update_user_state(sender_id, {'command': 'BULLETIN_POST', 'step': 4, 'board': board_name})

    elif step == 3:
        try:
            bulletin_id = int(message)
        except ValueError:
            send_message("Invalid bulletin number. Please try again.", sender_id, interface)
            return
        bulletin = get_bulletin_content(bulletin_id)
        if bulletin is None:
            send_message("Bulletin not found. Please try again.", sender_id, interface)
            return
        sender_short_name, date, subject, content, unique_id, content_complete, expected_length = bulletin
        notice = _incomplete_notice(content_complete, expected_length, content)
        send_message(f"From: {sender_short_name}\nDate: {date}\nSubject: {subject}\n- - - - - - -\n{content}{notice}", sender_id, interface)
        board_name = state['board']
        handle_bb_steps(sender_id, 'e', 1, state, interface, bbs_nodes)

    elif step == 4:
        if message.lower() in ('0', 'x', 'cancel'):
            send_message("Bulletin cancelled.", sender_id, interface)
            handle_bulletin_command(sender_id, interface)
            return
        subject = message
        send_message("Send the contents of your bulletin. Send END when finished, or 0 to cancel.", sender_id, interface)
        update_user_state(sender_id, {'command': 'BULLETIN_POST_CONTENT', 'step': 5, 'board': state['board'], 'subject': subject, 'content': ''})

    elif step == 5:
        if message.lower() in ('0', 'cancel'):
            send_message("Bulletin cancelled.", sender_id, interface)
            handle_bulletin_command(sender_id, interface)
            return
        if message.lower() == "end":
            board = state['board']
            subject = state['subject']
            content = state['content']
            node_id = get_node_id_from_num(sender_id, interface)
            sender_short_name = resolve_display_name(node_id, interface)
            if not sender_short_name:
                send_message("Error: Unable to retrieve your node information.", sender_id, interface)
                update_user_state(sender_id, None)
                return
            unique_id = add_bulletin(board, sender_short_name, subject, content, bbs_nodes, interface)
            send_message(f"Your bulletin '{subject}' has been posted to {board}.\n(╯°□°)╯📄📌[{board}]", sender_id, interface)
            handle_bb_steps(sender_id, 'e', 1, state, interface, bbs_nodes)
        else:
            state['content'] += message + "\n"
            update_user_state(sender_id, state)



def handle_mail_steps(sender_id, message, step, state, interface, bbs_nodes):
    message = message.strip()
    if len(message) == 2 and message[1] == 'x':
        message = message[0]
    if step in (3, 5, 7) and message.lower() in ('0', 'cancel'):
        send_message("Mail cancelled.", sender_id, interface)
        handle_mail_command(sender_id, interface)
        return

    if step == 1:
        choice = message.lower()
        _mail_step1_alias = {'1': 'r', '2': 's', '3': 'a', '0': 'x'}
        choice = _mail_step1_alias.get(choice, choice)
        if choice == 'r':
            sender_node_id = get_node_id_from_num(sender_id, interface)
            mail = get_mail(sender_node_id)
            if mail:
                send_message(f"You have {len(mail)} mail messages. Select a message number to read:", sender_id, interface)
                for msg in mail:
                    send_message(f"-{msg[0]}-\nDate: {msg[3]}\nFrom: {msg[1]}\nSubject: {msg[2]}", sender_id, interface)
                update_user_state(sender_id, {'command': 'MAIL', 'step': 2})
            else:
                send_message("There are no messages in your mailbox.📭", sender_id, interface)
                handle_mail_command(sender_id, interface)
        elif choice == 's':
            _start_mail_recipient_selection(sender_id, interface)
        elif choice == 'a':
            handle_active_users_command(sender_id, interface)
        elif choice == 'x':
            handle_help_command(sender_id, interface)
        else:
            # This chain had no else, so a wrong key here sent nothing at
            # all -- indistinguishable from a dead link.
            handle_mail_command(sender_id, interface, notice="Invalid choice.")

    elif step == 2:
        try:
            mail_id = int(message)
        except ValueError:
            send_message("Invalid message number. Please try again.", sender_id, interface)
            return
        try:
            sender_node_id = get_node_id_from_num(sender_id, interface)
            sender, date, subject, content, unique_id, content_complete, expected_length = get_mail_content(mail_id, sender_node_id)
            notice = _incomplete_notice(content_complete, expected_length, content)
            send_message(f"Date: {date}\nFrom: {sender}\nSubject: {subject}\n{content}{notice}", sender_id, interface)
            send_message("What would you like to do with this message?\n[1]Keep [2]Delete [3]Reply", sender_id, interface)
            update_user_state(sender_id, {'command': 'MAIL', 'step': 4, 'mail_id': mail_id, 'unique_id': unique_id, 'sender': sender, 'subject': subject, 'content': content})
        except TypeError:
            logging.info(f"Node {sender_id} tried to access non-existent message")
            send_message("Mail not found", sender_id, interface)
            update_user_state(sender_id, None)

    elif step == 3:
        recipient = _resolve_mail_relay_recipient(message, get_node_id_from_num(sender_id, interface))
        if recipient is None:
            send_message("That relay user was not found, is ambiguous, or has not opted in.", sender_id, interface)
            handle_mail_command(sender_id, interface)
        else:
            send_message(f"What is the subject of your message to {recipient['display_name']}?\nKeep it short. [0] Cancel", sender_id, interface)
            update_user_state(sender_id, {
                'command': 'MAIL', 'step': 5,
                'recipient_id': recipient['recipient_node_id'],
                'recipient_name': recipient['display_name'],
            })

    elif step == 4:
        _mail_step4_alias = {'2': 'd', '3': 'r', '1': 'k'}
        choice4 = _mail_step4_alias.get(message.lower(), message.lower())
        if choice4 == "d":
            unique_id = state['unique_id']
            sender_node_id = get_node_id_from_num(sender_id, interface)
            delete_mail(unique_id, sender_node_id, bbs_nodes, interface)
            send_message("The message has been deleted 🗑️", sender_id, interface)
            update_user_state(sender_id, None)
        elif choice4 == "r":
            sender = state['sender']
            send_message(f"Send your reply to {sender} now, followed by a message with END", sender_id, interface)
            update_user_state(sender_id, {'command': 'MAIL', 'step': 7, 'reply_to_mail_id': state['mail_id'], 'subject': f"Re: {state['subject']}", 'content': ''})
        else:
            send_message("The message has been kept in your inbox.✉️", sender_id, interface)
            update_user_state(sender_id, None)

    elif step == 5:
        subject = message
        send_message("Send your message in one or more parts. Send END when done, or 0 to cancel.", sender_id, interface)
        update_user_state(sender_id, {
            'command': 'MAIL', 'step': 7,
            'recipient_id': state['recipient_id'],
            'recipient_name': state.get('recipient_name'),
            'subject': subject, 'content': '',
        })

    elif step == 6:
        try:
            selected_node_index = int(message)
        except ValueError:
            send_message("Invalid selection. Please reply with a valid number.", sender_id, interface)
            return
        if selected_node_index < 0 or selected_node_index >= len(state['nodes']):
            send_message("Invalid selection. Please reply with a valid number.", sender_id, interface)
            return
        selected_node = state['nodes'][selected_node_index]
        recipient_id = selected_node['num']
        recipient_name = get_node_name(recipient_id, interface)
        send_message(f"What is the subject of your message to {recipient_name}?\nKeep it short.", sender_id, interface)
        update_user_state(sender_id, {'command': 'MAIL', 'step': 5, 'recipient_id': recipient_id})

    elif step == 7:
        if message.lower() == "end":
            if 'reply_to_mail_id' in state:
                recipient_id = get_sender_id_by_mail_id(state['reply_to_mail_id'])  # Get the sender ID from the mail ID
            else:
                recipient_id = state.get('recipient_id')
            if not get_mail_relay_preference(recipient_id):
                send_message("That user is not accepting relayed mail.", sender_id, interface)
                handle_mail_command(sender_id, interface)
                return
            subject = state['subject']
            content = state['content']
            recipient_name = state.get('recipient_name') or get_node_name(recipient_id, interface)

            sender_short_name = resolve_display_name(get_node_id_from_num(sender_id, interface), interface)
            unique_id = add_mail(get_node_id_from_num(sender_id, interface), sender_short_name, recipient_id, subject, content, bbs_nodes, interface)
            send_message(f"Mail has been posted to the mailbox of {recipient_name}.\n(╯°□°)╯📨📬", sender_id, interface)

            update_user_state(sender_id, None)
            update_user_state(sender_id, {'command': 'MAIL', 'step': 8})
        else:
            state['content'] += message + "\n"
            update_user_state(sender_id, state)

    elif step == 8:
        if message.lower() == "y":
            handle_mail_command(sender_id, interface)
        else:
            send_message("Okay, feel free to send another command.", sender_id, interface)
            update_user_state(sender_id, None)

    elif step == 9:
        entries = state.get('directory', [])
        page_count = max(1, (len(entries) + _MAIL_DIRECTORY_PAGE_SIZE - 1) // _MAIL_DIRECTORY_PAGE_SIZE)
        page = max(0, min(int(state.get('directory_page', 0)), page_count - 1))
        choice = message.lower()
        if choice in ('0', 'x', 'cancel'):
            handle_mail_command(sender_id, interface)
            return
        if choice == 'a':
            send_message("Enter an opted-in account alias, short name, or exact node ID:", sender_id, interface)
            update_user_state(sender_id, {'command': 'MAIL', 'step': 3})
            return
        if choice in ('n', 'p'):
            page += 1 if choice == 'n' else -1
            page = max(0, min(page, page_count - 1))
            state['directory_page'] = page
            update_user_state(sender_id, state)
            send_message(_mail_directory_page(entries, page, selecting=True), sender_id, interface)
            return
        try:
            selected_index = int(message) - 1
        except ValueError:
            send_message("Invalid selection. Reply with a listed number, N, P, or X.", sender_id, interface)
            return
        visible = entries[page * _MAIL_DIRECTORY_PAGE_SIZE:(page + 1) * _MAIL_DIRECTORY_PAGE_SIZE]
        if selected_index < 0 or selected_index >= len(visible):
            send_message("Invalid selection. Please choose a listed user.", sender_id, interface)
            return
        selected = visible[selected_index]
        send_message(f"What is the subject of your message to {selected['display_name']}?\nKeep it short.", sender_id, interface)
        update_user_state(sender_id, {
            'command': 'MAIL', 'step': 5,
            'recipient_id': selected['recipient_node_id'],
            'recipient_name': selected['display_name'],
        })

    elif step == 10:
        entries = state.get('directory', [])
        page_count = max(1, (len(entries) + _MAIL_DIRECTORY_PAGE_SIZE - 1) // _MAIL_DIRECTORY_PAGE_SIZE)
        page = max(0, min(int(state.get('directory_page', 0)), page_count - 1))
        choice = message.lower()
        if choice == 'x':
            handle_mail_command(sender_id, interface)
            return
        if choice in ('n', 'p'):
            page = max(0, min(page + (1 if choice == 'n' else -1), page_count - 1))
            state['directory_page'] = page
            update_user_state(sender_id, state)
            send_message(_mail_directory_page(entries, page, selecting=False), sender_id, interface)
            return
        send_message("Reply N, P, or X.", sender_id, interface)


def handle_wall_of_shame_command(sender_id, interface):
    response = "Devices with battery levels below 20%:\n"
    for node_id, node in interface.nodes.items():
        metrics = node.get('deviceMetrics', {})
        battery_level = metrics.get('batteryLevel', 101)
        if battery_level < 20:
            long_name = node['user']['longName']
            response += f"{long_name} - Battery {battery_level}%\n"
    if response == "Devices with battery levels below 20%:\n":
        response = "No devices with battery levels below 20% found."
    send_message(response, sender_id, interface)
    handle_help_command(sender_id, interface, 'utilities')


def handle_channel_directory_command(sender_id, interface):
    response = "📚CHANNEL DIRECTORY📚\nWhat would you like to do?\n[1]View [2]Post [0]Back"
    send_message(response, sender_id, interface)
    update_user_state(sender_id, {'command': 'CHANNEL_DIRECTORY', 'step': 1})


def _send_channel_categories(sender_id, interface):
    categories = get_channel_categories()
    if not categories:
        send_message("No channels available in the directory.", sender_id, interface)
        handle_channel_directory_command(sender_id, interface)
        return
    response = "Select a channel category to view:\n" + "\n".join(
        [f"[{i}] {category[0]} ({category[1]} post{'s' if category[1] != 1 else ''})"
         for i, category in enumerate(categories, 1)])
    send_message(response + "\n[0] Back", sender_id, interface)
    update_user_state(sender_id, {
        'command': 'CHANNEL_DIRECTORY', 'step': 2, 'categories': categories,
    })


def handle_channel_directory_steps(sender_id, message, step, state, interface):
    message = message.strip()
    if len(message) == 2 and message[1] == 'x':
        message = message[0]

    if step == 1:
        _chdir_alias = {'1': 'v', '2': 'p', '0': 'x'}
        choice = _chdir_alias.get(message.lower(), message.lower())
        if choice == 'x':
            handle_help_command(sender_id, interface)
            return
        elif choice == 'v':
            _send_channel_categories(sender_id, interface)
        elif choice == 'p':
            send_message("Name your channel for the directory:", sender_id, interface)
            update_user_state(sender_id, {'command': 'CHANNEL_DIRECTORY', 'step': 3})

    elif step == 2:
        if message.lower() in ('0', 'x', 'back', 'exit'):
            handle_channel_directory_command(sender_id, interface)
            return
        try:
            category_index = int(message) - 1
        except ValueError:
            send_message("Invalid selection. Please try again.", sender_id, interface)
            return
        categories = state.get('categories', [])
        if 0 <= category_index < len(categories):
            channel_name = categories[category_index][0]
            posts = get_channels_by_name(channel_name)
            if posts:
                post_lines = []
                for i, post in enumerate(posts, 1):
                    post_id = post[0]
                    comments = get_channel_comments(post_id)
                    if comments:
                        latest_commenter = comments[0][1]
                        post_lines.append(f"[{i}] {latest_commenter}")
                    else:
                        post_lines.append(f"[{i}] No comments yet")
                response = f"{channel_name} posts:\n" + "\n".join(post_lines) + "\n[0] Back"
                send_message(response, sender_id, interface)
                update_user_state(sender_id, {'command': 'CHANNEL_DIRECTORY', 'step': 5, 'posts': posts, 'channel_name': channel_name})
                return
            send_message("No posts found in that category.", sender_id, interface)
        else:
            send_message("Invalid selection. Please try again.", sender_id, interface)
        handle_channel_directory_command(sender_id, interface)

    elif step == 5:
        if message.lower() in ('0', 'x', 'back', 'exit'):
            _send_channel_categories(sender_id, interface)
            return
        try:
            post_index = int(message) - 1
        except ValueError:
            send_message("Invalid post number. Please try again.", sender_id, interface)
            return
        posts = state.get('posts', [])
        if 0 <= post_index < len(posts):
            channel_id = posts[post_index][0]
            channel = get_channel_by_id(channel_id)
            if channel is None:
                send_message("Channel post not found.", sender_id, interface)
                handle_channel_directory_command(sender_id, interface)
                return
            _, channel_name, channel_url = channel
            send_message(
                f"Channel Name: {channel_name}\nPost ID: {channel_id}\nChannel URL/PSK:\n{channel_url}",
                sender_id,
                interface
            )
            send_message("[1]View comments [2]Comment [0]Exit", sender_id, interface)
            update_user_state(sender_id, {'command': 'CHANNEL_DIRECTORY', 'step': 6, 'channel_id': channel_id, 'channel_name': channel_name})
        else:
            send_message("Invalid post number. Please try again.", sender_id, interface)

    elif step == 6:
        _ch6_alias = {'1': 'v', '2': 'c', '0': 'x'}
        choice = _ch6_alias.get(message.lower().strip(), message.lower().strip())
        if choice == 'x':
            handle_channel_directory_command(sender_id, interface)
            return
        if choice == 'v':
            channel_id = state.get('channel_id')
            comments = get_channel_comments(channel_id)
            if comments:
                for i, comment in enumerate(comments, start=1):
                    sender_short_name, date, content = comment[1], comment[2], comment[3]
                    send_message(f"[{i}] {sender_short_name} @ {date}\n{content}", sender_id, interface)
            else:
                send_message("No comments yet for this post.", sender_id, interface)
            send_message("[1]View comments [2]Comment [0]Exit", sender_id, interface)
            return
        if choice == 'c':
            send_message("Send your comment. Send END on a new message when finished.", sender_id, interface)
            update_user_state(sender_id, {
                'command': 'CHANNEL_DIRECTORY',
                'step': 7,
                'channel_id': state.get('channel_id'),
                'channel_name': state.get('channel_name'),
                'comment_content': ''
            })
            return
        send_message("Invalid choice. Use 1, 2, or 0.", sender_id, interface)

    elif step == 7:
        if message.strip().lower() == 'end':
            content = state.get('comment_content', '').strip()
            if not content:
                send_message("Comment was empty. Nothing posted.", sender_id, interface)
            else:
                node_short_name = resolve_display_name(get_node_id_from_num(sender_id, interface), interface) or "Unknown"
                add_channel_comment(state.get('channel_id'), node_short_name, content,
                                    bbs_nodes=interface.bbs_nodes, interface=interface)
                send_message("Comment posted.", sender_id, interface)
            send_message("[1]View comments [2]Comment [0]Exit", sender_id, interface)
            update_user_state(sender_id, {
                'command': 'CHANNEL_DIRECTORY',
                'step': 6,
                'channel_id': state.get('channel_id'),
                'channel_name': state.get('channel_name')
            })
        else:
            state['comment_content'] = state.get('comment_content', '') + message + "\n"
            update_user_state(sender_id, state)

    elif step == 3:
        if message.lower() in ('0', 'x', 'cancel'):
            handle_channel_directory_command(sender_id, interface)
            return
        channel_name = message
        send_message("Send the channel URL or PSK, or 0 to cancel:", sender_id, interface)
        update_user_state(sender_id, {'command': 'CHANNEL_DIRECTORY', 'step': 4, 'channel_name': channel_name})

    elif step == 4:
        if message.lower() in ('0', 'x', 'cancel'):
            handle_channel_directory_command(sender_id, interface)
            return
        channel_url = message
        channel_name = state['channel_name']
        add_channel(channel_name, channel_url, interface.bbs_nodes, interface)
        send_message(f"Your channel '{channel_name}' has been added to the directory.", sender_id, interface)
        handle_channel_directory_command(sender_id, interface)


def handle_send_mail_command(sender_id, message, interface, bbs_nodes):
    try:
        parts = message.split(",,", 3)
        if len(parts) != 4:
            send_message("Send Mail Quick Command format:\n!SM,,{recipient},,{subject},,{message}", sender_id, interface)
            return

        _, recipient_query, subject, content = parts
        recipient = _resolve_mail_relay_recipient(
            recipient_query, get_node_id_from_num(sender_id, interface)
        )
        if recipient is None:
            send_message(
                f"Relay user '{recipient_query}' was not found, is ambiguous, or has not opted in. Send !AU to browse.",
                sender_id, interface,
            )
            return

        recipient_id = recipient['recipient_node_id']
        recipient_name = recipient['display_name']
        sender_short_name = resolve_display_name(get_node_id_from_num(sender_id, interface), interface)

        unique_id = add_mail(get_node_id_from_num(sender_id, interface), sender_short_name, recipient_id, subject,
                             content, bbs_nodes, interface)
        send_message(f"Mail has been sent to {recipient_name}.", sender_id, interface)

    except Exception as e:
        logging.error(f"Error processing send mail command: {e}")
        send_message("Error processing send mail command.", sender_id, interface)


def handle_check_mail_command(sender_id, interface):
    try:
        sender_node_id = get_node_id_from_num(sender_id, interface)
        mail = get_mail(sender_node_id)
        if not mail:
            send_message("You have no new messages.", sender_id, interface)
            return

        response = "📬 You have the following messages:\n"
        for i, msg in enumerate(mail):
            response += f"{i + 1:02d}. From: {msg[1]}, Subject: {msg[2]}\n"
        response += "\nPlease reply with the number of the message you want to read."
        send_message(response, sender_id, interface)

        update_user_state(sender_id, {'command': 'CHECK_MAIL', 'step': 1, 'mail': mail})

    except Exception as e:
        logging.error(f"Error processing check mail command: {e}")
        send_message("Error processing check mail command.", sender_id, interface)


def handle_read_mail_command(sender_id, message, state, interface):
    try:
        mail = state.get('mail', [])
        message_number = int(message) - 1

        if message_number < 0 or message_number >= len(mail):
            send_message("Invalid message number. Please try again.", sender_id, interface)
            return

        mail_id = mail[message_number][0]
        sender_node_id = get_node_id_from_num(sender_id, interface)
        sender, date, subject, content, unique_id, content_complete, expected_length = get_mail_content(mail_id, sender_node_id)
        response = f"Date: {date}\nFrom: {sender}\nSubject: {subject}\n\n{content}{_incomplete_notice(content_complete, expected_length, content)}"
        send_message(response, sender_id, interface)
        send_message("What would you like to do with this message?\n[1]Keep [2]Delete [3]Reply", sender_id, interface)
        update_user_state(sender_id, {'command': 'CHECK_MAIL', 'step': 2, 'mail_id': mail_id, 'unique_id': unique_id, 'sender': sender, 'subject': subject, 'content': content})

    except ValueError:
        send_message("Invalid input. Please enter a valid message number.", sender_id, interface)
    except Exception as e:
        logging.error(f"Error processing read mail command: {e}")
        send_message("Error processing read mail command.", sender_id, interface)


def handle_delete_mail_confirmation(sender_id, message, state, interface, bbs_nodes):
    try:
        choice = message.lower().strip()
        if len(choice) == 2 and choice[1] == 'x':
            choice = choice[0]
        _kdr_alias = {'2': 'd', '3': 'r', '1': 'k'}
        choice = _kdr_alias.get(choice, choice)

        if choice == 'd':
            unique_id = state['unique_id']
            sender_node_id = get_node_id_from_num(sender_id, interface)
            delete_mail(unique_id, sender_node_id, bbs_nodes, interface)
            send_message("The message has been deleted 🗑️", sender_id, interface)
            update_user_state(sender_id, None)
        elif choice == 'r':
            sender = state['sender']
            send_message(f"Send your reply to {sender} now, followed by a message with END", sender_id, interface)
            update_user_state(sender_id, {'command': 'MAIL', 'step': 7, 'reply_to_mail_id': state['mail_id'], 'subject': f"Re: {state['subject']}", 'content': ''})
        else:
            send_message("The message has been kept in your inbox.✉️", sender_id, interface)
            update_user_state(sender_id, None)

    except Exception as e:
        logging.error(f"Error processing delete mail confirmation: {e}")
        send_message("Error processing delete mail confirmation.", sender_id, interface)



def handle_post_bulletin_command(sender_id, message, interface, bbs_nodes):
    try:
        parts = message.split(",,", 3)
        if len(parts) != 4:
            send_message("Post Bulletin Quick Command format:\n!PB,,{board_name},,{subject},,{content}", sender_id, interface)
            return

        _, board_name, subject, content = parts
        sender_short_name = resolve_display_name(get_node_id_from_num(sender_id, interface), interface)

        unique_id = add_bulletin(board_name, sender_short_name, subject, content, bbs_nodes, interface)
        send_message(f"Your bulletin '{subject}' has been posted to {board_name}.", sender_id, interface)


    except Exception as e:
        logging.error(f"Error processing post bulletin command: {e}")
        send_message("Error processing post bulletin command.", sender_id, interface)


def handle_check_bulletin_command(sender_id, message, interface):
    try:
        # Split the message only once
        parts = message.split(",,", 1)
        if len(parts) != 2 or not parts[1].strip():
            send_message("Check Bulletins Quick Command format:\n!CB,,board_name", sender_id, interface)
            return

        boards = get_bulletin_boards()
        board_lookup = {board.lower(): board for board in boards}
        board_name_key = parts[1].strip().lower()
        if board_name_key not in board_lookup:
            send_message(f"Invalid board name. Available boards: {', '.join(boards)}", sender_id, interface)
            return
        board_name = board_lookup[board_name_key]

        bulletins = get_bulletins(board_name)
        if not bulletins:
            send_message(f"No bulletins available on {board_name} board.", sender_id, interface)
            return

        response = f"📰 Bulletins on {board_name} board:\n"
        for i, bulletin in enumerate(bulletins):
            response += f"[{i+1:02d}] Subject: {bulletin[1]}, From: {bulletin[2]}, Date: {bulletin[3]}\n"
        response += "\nPlease reply with the number of the bulletin you want to read."
        send_message(response, sender_id, interface)

        update_user_state(sender_id, {'command': 'CHECK_BULLETIN', 'step': 1, 'board_name': board_name, 'bulletins': bulletins})

    except Exception as e:
        logging.error(f"Error processing check bulletin command: {e}")
        send_message("Error processing check bulletin command.", sender_id, interface)

def handle_read_bulletin_command(sender_id, message, state, interface):
    try:
        bulletins = state.get('bulletins', [])
        message_number = int(message) - 1

        if message_number < 0 or message_number >= len(bulletins):
            send_message("Invalid bulletin number. Please try again.", sender_id, interface)
            return

        bulletin_id = bulletins[message_number][0]
        sender, date, subject, content, unique_id, content_complete, expected_length = get_bulletin_content(bulletin_id)
        response = f"Date: {date}\nFrom: {sender}\nSubject: {subject}\n\n{content}{_incomplete_notice(content_complete, expected_length, content)}"
        send_message(response, sender_id, interface)

        update_user_state(sender_id, None)

    except ValueError:
        send_message("Invalid input. Please enter a valid bulletin number.", sender_id, interface)
    except Exception as e:
        logging.error(f"Error processing read bulletin command: {e}")
        send_message("Error processing read bulletin command.", sender_id, interface)


def handle_post_channel_command(sender_id, message, interface):
    try:
        parts = message.split(",,", 2)
        if len(parts) != 3:
            send_message("Post Channel Quick Command format:\n!CHP,,{channel_name},,{channel_url}", sender_id, interface)
            return

        _, channel_name, channel_url = parts
        bbs_nodes = interface.bbs_nodes
        add_channel(channel_name, channel_url, bbs_nodes, interface)
        send_message(f"Channel '{channel_name}' has been added to the directory.", sender_id, interface)

    except Exception as e:
        logging.error(f"Error processing post channel command: {e}")
        send_message("Error processing post channel command.", sender_id, interface)


def handle_check_channel_command(sender_id, interface):
    try:
        channels = get_channels()
        if not channels:
            send_message("No channels available in the directory.", sender_id, interface)
            return

        response = "Available Channels:\n"
        for i, channel in enumerate(channels):
            response += f"{i + 1:02d}. Name: {channel[0]}\n"
        response += "\nPlease reply with the number of the channel you want to view."
        send_message(response, sender_id, interface)

        update_user_state(sender_id, {'command': 'CHECK_CHANNEL', 'step': 1, 'channels': channels})

    except Exception as e:
        logging.error(f"Error processing check channel command: {e}")
        send_message("Error processing check channel command.", sender_id, interface)


def handle_read_channel_command(sender_id, message, state, interface):
    try:
        channels = state.get('channels', [])
        message_number = int(message) - 1

        if message_number < 0 or message_number >= len(channels):
            send_message("Invalid channel number. Please try again.", sender_id, interface)
            return

        channel_name, channel_url = channels[message_number]
        response = f"Channel Name: {channel_name}\nChannel URL: {channel_url}"
        send_message(response, sender_id, interface)

        update_user_state(sender_id, None)

    except ValueError:
        send_message("Invalid input. Please enter a valid channel number.", sender_id, interface)
    except Exception as e:
        logging.error(f"Error processing read channel command: {e}")
        send_message("Error processing read channel command.", sender_id, interface)


def handle_list_channels_command(sender_id, interface):
    try:
        channels = get_channels()
        if not channels:
            send_message("No channels available in the directory.", sender_id, interface)
            return

        response = "Available Channels:\n"
        for i, channel in enumerate(channels):
            response += f"{i+1:02d}. Name: {channel[0]}\n"
        response += "\nPlease reply with the number of the channel you want to view."
        send_message(response, sender_id, interface)

        update_user_state(sender_id, {'command': 'LIST_CHANNELS', 'step': 1, 'channels': channels})

    except Exception as e:
        logging.error(f"Error processing list channels command: {e}")
        send_message("Error processing list channels command.", sender_id, interface)


def handle_quick_help_command(sender_id, interface):
    response = (
        "✈️QUICK COMMANDS✈️\n"
        "!SM,, - Send Mail\n!CM - Check Mail\n!AU - Relay Directory\n"
        "!PB,, - Post Bulletin\n!CB,, - Check Bulletins\n"
        "!CHP,, - Post Channel\n!CHL - List Channels\n"
        "Global menus: !Q !B !U !P !N !A !S !X"
    )
    send_message(response, sender_id, interface)
