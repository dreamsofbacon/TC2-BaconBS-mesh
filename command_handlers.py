import configparser
import logging
import os
import random
import re
import sqlite3
import threading
import time

from meshtastic import BROADCAST_NUM

from db_operations import (
    add_bulletin, add_mail, delete_mail, delete_bulletin,
    delete_channel_comment,
    get_content_source_nodes,
    get_node_role, set_node_role, get_role_updated_at,
    role_at_least, role_rank, normalize_role,
    ASSIGNABLE_ROLES, ROLE_MOD, ROLE_VIP, ROLE_UNREGISTERED,
    get_account_id_for_node,
    count_hidden_bulletins, count_hidden_mail, count_hidden_channel_comments,
    get_bulletin_content, get_bulletins,
    get_mail, get_mail_content, get_latest_delivered_mail, get_latest_mailbox_message,
    add_channel, get_channels, get_sender_id_by_mail_id,
    get_channel_categories, get_channels_by_name, get_channel_by_id,
    add_channel_comment, get_channel_comments,
    auto_upsert_user_profile, get_user_profile, update_user_bio,
    upsert_game_score, get_game_scoreboard, get_user_game_scores, get_hall_of_fame,
    create_account, get_account_id_for_node, get_linked_node_ids,
    get_linked_nodes_detail, link_node_to_account, unlink_node,
    get_help_tips_enabled, set_help_tips_enabled,
    get_mesh_client_names,
    get_account_alias, set_account_alias, create_link_code, redeem_link_code,
    record_link_attempt, link_rate_limit_ok, account_authorized,
    SSH_NODE_PREFIX,
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
    get_view_scope, set_view_scope, clear_view_scope,
    node_display_name, scope_notice, local_identities_for_display,
    welcome_text,
    short_node_id,
    get_node_nicknames, node_ids_for_name, get_max_text_bytes,
    is_bbs_role_management_enabled, is_role_sync_enabled,
    send_node_role_to_bbs_nodes,
)
from zork_port import (
    GAMES,
    has_zork_save,
    has_zork_session,
    parse_game_score,
    resume_zork_session,
    response_limit_for,
    send_zork_command,
    start_zork_session,
    stop_zork_session,
)
import trivia_port
import baconfall_port

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
utilities_menu_items = _parse_menu_items(config.get('menu', 'utilities_menu_items', fallback='F,W,X'))
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


# Backing out of a prompt takes the global prefix, because at these prompts
# the BBS is asking for content and every plausible cancel word is also
# plausible content. A bare "Exit" typed at the channel-name prompt was read
# as a name: the live directory holds a channel called Exit whose URL field
# is a user's complaint about the prompt that trapped them. "0" and "x" have
# the same problem inside a bulletin body or a bio.
#
# Menus are different and keep their bare keys -- there [0] is a line on the
# screen, not something the user composed.
CANCEL_WORDS = ('!exit', '!cancel', '!x', '!0')
CANCEL_HINT = '!cancel'


def is_cancel(message) -> bool:
    """True when the user typed a prefixed cancel word at a content prompt."""
    return str(message or '').strip().casefold() in CANCEL_WORDS


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
    # Stats moved to Settings ([4] View Stats) and Games/Public Chatter
    # moved to the main menu -- see MENU_REQUIRED_AFTER. What is left here
    # genuinely has nowhere more specific to live.
    'F': "Fortune",
    'W': "Wall of Shame",
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
    'G': "Games",
    'H': "Public Chatter",
    'U': "Utilities",
    'N': "Ask Nomad",
    'A': "Web Fetch",
    # One entry, not two. Profile and Settings were split on "who you are"
    # against "what the BBS does for you", which is a real distinction and
    # a poor one to make someone guess at from the main menu: both answers
    # to "change something about me" now live behind one door, headed
    # inside. 'P' is deliberately gone rather than aliased -- menu_layout
    # drops letters with no label, so a config.ini still listing it simply
    # renumbers instead of rendering a blank line. !P still works.
    'S': "Settings & Profile",
    'V': "Node View",
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
#
# Ask Nomad is required alongside Profile/Web Fetch/Settings/Node View: it
# used to be reachable only through Utilities > API Gateway's chooser, which
# was quietly replaced by a direct jump into Web Fetch (see
# handle_apigw_command) and left Ask Nomad with no menu path of its own on
# any config written before this. Web Fetch and Ask Nomad are the two halves
# of what used to be one combined API Gateway entry; each now gets its own
# line rather than being buried behind a chooser.
MENU_REQUIRED = {
    # Profile ahead of the rest: it is the entry a stranger looks for first,
    # and it was defined but never shown -- reachable only as !P, which is
    # the discoverability complaint restated.
    'main': ('N', 'A', 'S', 'V'),
    'bbs': (),
    # Games and Public Chatter moved to the main menu (see
    # MENU_REQUIRED_AFTER) -- showing them here too would be the exact
    # duplication test_api_gateway_no_longer_rendered_under_utilities
    # already guards against for Web Fetch.
    'utilities': (),
}

# A few required entries want a specific neighbor instead of "anywhere
# before Exit": Games and Public Chatter read as afterthoughts tacked onto
# the tail of the main menu (behind Profile, Settings, Node View...) if
# they went through the generic MENU_REQUIRED path above. A returning user
# looks for the door games right after BBS, not at the end of the list.
MENU_REQUIRED_AFTER = {
    'main': (('G', 'B'), ('H', 'G')),
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

    for letter, after in MENU_REQUIRED_AFTER.get(kind, ()):
        if letter in layout:
            continue
        if after in layout:
            layout.insert(layout.index(after) + 1, letter)
        elif 'X' in layout:
            layout.insert(layout.index('X'), letter)
        else:
            layout.append(letter)

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

# ---------------------------------------------------------------------------
# Help tips: one short line under a menu, saying the thing a newcomer asks.
#
# On for everyone until they turn them off, because the people who need them
# are the ones who do not yet know there is a setting. Settings & Profile
# carries the switch.
#
# Every tip is kept to roughly one line for a reason that is not tidiness.
# The main menu is already 176 bytes and a MeshCore packet is 160, so it
# arrives as two transmissions with 2s of pacing between them; a long tip
# buys a third. Tips are therefore short, and turning them off genuinely
# reclaims airtime rather than just tidying the screen.
#
# Keyed by the same names menu_layout uses ('main', 'bbs', 'utilities') plus
# the user_states 'command' of the screens that are not built by build_menu.
# A screen with no entry here simply gets no tip.
# ---------------------------------------------------------------------------

HELP_TIPS = {
    'main': "Tip: reply with a number. !Q lists shortcuts.",
    'bbs': "Tip: Bulletins are notices. Channels are topics with replies.",
    'utilities': "Tip: these are the odds and ends. [0] goes back.",
    'settings': "Tip: [5] turns these tips off.",
    'BULLETIN_MENU': "Tip: pick a board, then a number to read it.",
    'MAIL': "Tip: [0] backs out of any prompt.",
    'CHANNEL_DIRECTORY': "Tip: a channel is a topic. Open one to reply.",
    'GAMES_MENU': "Tip: games keep your score. X leaves a game.",
    'PUBLIC_CHATTER': "Tip: this is live mesh traffic, not BBS posts.",
}


def help_tip(sender_id, key) -> str:
    """The tip line for one screen, or '' when it should not be shown.

    Returns the empty string rather than None so callers can join it
    unconditionally, and so a screen with tips switched off is byte-identical
    to what it was before tips existed.
    """
    tip = HELP_TIPS.get(key)
    if not tip:
        return ''
    try:
        if not get_help_tips_enabled(sender_id):
            return ''
    except Exception:
        # A database that predates the column, or is momentarily unavailable.
        # Showing the tip is the safer failure: it is advice, not an action.
        logging.debug("could not read help tip preference", exc_info=True)
    return tip


def with_help_tip(text, sender_id, key) -> str:
    """Append the screen's tip to a message, if that user wants tips."""
    tip = help_tip(sender_id, key)
    return f"{text}{LINE_BREAK}{tip}" if tip else text


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
        # Deliberately NOT scoped by the Node View lens. This badge is the
        # top-level guarantee that mail is never hidden: it always counts
        # the whole mailbox, so if it says 5 and a narrowed list shows 3,
        # the notice on that list accounts for the other 2. Scoping it here
        # would make that promise impossible to state.
        mail = get_mail(get_node_id_from_num(sender_id, interface))
        response = build_menu(main_menu_items, f"💾Bacon BBS💾 (✉️:{len(mail)})")
    if notice:
        response = f"{notice}{LINE_BREAK}{response}"
    response = with_help_tip(response, sender_id, menu_name or 'main')
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


def urgent_board_permitted(node_id, allow_lists) -> bool:
    """Whether this identity may post to the Urgent board.

    Posting here broadcasts to every node in range and syncs to peers we do
    not run, so it is the one board with a gate on it.

    An empty allow list means "no restriction configured", which is the right
    default for radio: possession of a radio is the credential, and a
    stranger cannot cheaply become your neighbour's node. It stopped being
    the right default the moment a self-registration port opened. An SSH
    identity is issued by this node to anyone who asks, so an empty list must
    not hand it the Urgent board -- docs/SSH-ACCESS.md already states that a
    new account gets no urgent access, and until now that was only true when
    an allow list happened to be populated.

    An SSH account CAN reach it, by linking a real device through the
    one-time code flow and having that device allow-listed. That is the same
    proof of radio possession every other poster gives, offered through the
    only mechanism this system has ever accepted it from.
    """
    if str(node_id or '').startswith(SSH_NODE_PREFIX):
        return bool(any(allow_lists)) and account_authorized(node_id, allow_lists)
    # Radio and MQTT identities keep the original behaviour exactly.
    return not any(allow_lists) or account_authorized(node_id, allow_lists)


def _urgent_refusal(node_id) -> str:
    """Say which rule was hit, so it does not read as a malfunction."""
    if str(node_id or '').startswith(SSH_NODE_PREFIX):
        return ("The Urgent board is radio-only. Link a device under "
                "Linked Devices and ask the operator to allow-list it.")
    return "You don't have permission to post to this board."


def send_board_action_menu(sender_id, interface, board_name, boards, notice=None):
    """Show one board's Read/Post menu and park the user on it.

    Shared with the invalid-key path so a wrong number redraws this menu
    instead of bouncing the reader out to the main menu, which used to lose
    which board they were in without saying anything.
    """
    scope = get_view_scope(sender_id)
    bulletins = get_bulletins(board_name, scope)
    response = f"{board_name} has {len(bulletins)} messages.\n[1]Read [2]Post [0]Back"
    lens = scope_notice(sender_id, count_hidden_bulletins(board_name, scope))
    if lens:
        response = f"{lens}{LINE_BREAK}{response}"
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
    clear_view_scope(sender_id)
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
    send_message(with_help_tip(response, sender_id, 'MAIL'), sender_id, interface)
    update_user_state(sender_id, {'command': 'MAIL', 'step': 1})


def _reply_subject(subject: str) -> str:
    base = re.sub(r'^(?:\s*re:\s*)+', '', str(subject or ''), flags=re.IGNORECASE)
    return f"Re: {base}".rstrip()


def _begin_mail_reply(sender_id, interface, mail_id: int, sender: str, subject: str) -> None:
    send_message(
        f"Send your reply to {sender} now, followed by a message with END",
        sender_id, interface,
    )
    update_user_state(sender_id, {
        'command': 'MAIL', 'step': 7,
        'reply_to_mail_id': mail_id,
        'subject': _reply_subject(subject),
        'content': '',
    })


def handle_quick_reply_command(sender_id, interface):
    """Jump straight into replying to whatever mail is newest.

    Tries the relay-DM delivery record first -- it carries a precise
    delivered_at -- then falls back to whatever is actually newest in the
    ordinary mailbox. Most mail is read the ordinary way (Mail -> Read,
    !CM) and never touches the relay-delivery table at all, so relying on
    that alone left this shortcut reporting nothing despite readable mail
    sitting in the inbox -- confirmed on a live beta test.
    """
    sender_node_id = get_node_id_from_num(sender_id, interface)
    mail = get_latest_delivered_mail(sender_node_id)
    if mail is None:
        mail = get_latest_mailbox_message(sender_node_id)
    if mail is None:
        send_message(
            "No mail to reply to yet. Send !CM to check your mailbox.",
            sender_id, interface,
        )
        return
    _begin_mail_reply(
        sender_id, interface, mail['mail_id'],
        mail['sender_short_name'], mail['subject'],
    )


_MAIL_DIRECTORY_PAGE_SIZE = 6


def mail_directory_page_view(entries: list[dict], page: int) -> tuple:
    """The one definition of what a page of the directory contains.

    Returns (visible, page, page_count) with the page clamped into range.

    Shared by the renderer and by the code that resolves a typed number,
    because those two disagreeing is a silent wrong-recipient bug: the
    screen restarts at [1] on every page, so a resolver indexing the whole
    directory sends page two's [1] to the first person instead of the
    seventh, with no error anywhere. menu_layout exists for the same reason
    -- the main menu's numbers once disagreed with the handlers behind them.
    """
    page_count = max(1, (len(entries) + _MAIL_DIRECTORY_PAGE_SIZE - 1) // _MAIL_DIRECTORY_PAGE_SIZE)
    page = max(0, min(int(page), page_count - 1))
    start = page * _MAIL_DIRECTORY_PAGE_SIZE
    return entries[start:start + _MAIL_DIRECTORY_PAGE_SIZE], page, page_count


def _mail_directory_page(entries: list[dict], page: int, selecting: bool) -> str:
    visible, page, page_count = mail_directory_page_view(entries, page)
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
    else:
        # The selecting view says "Select a relay user:" in its heading.
        # Browsing had no such cue, so the numbers looked decorative.
        controls.append("[#] Write")
    controls.append("[0] Back")
    lines.append(" ".join(controls))
    return "\n".join(lines)


def _directory_selection(entries: list[dict], page: int, message) -> tuple:
    """Resolve a reply against the page in front of the user.

    Returns (entry, problem). Numbers are per-page -- _mail_directory_page
    restarts at [1] on every page -- so this has to be told which page was
    on screen rather than indexing the whole directory.
    """
    try:
        index = int(str(message).strip()) - 1
    except (TypeError, ValueError):
        return None, 'not_a_number'
    visible, _page, _count = mail_directory_page_view(entries, page)
    if 0 <= index < len(visible):
        return visible[index], None
    return None, 'out_of_range'


def _begin_mail_to_directory_entry(sender_id, interface, entry: dict) -> None:
    """Go from a directory listing into writing to that person.

    Shared by both ways of reaching the directory. Browsing it used to be a
    dead end: the entries were numbered, so a number looked like the obvious
    thing to type, and typing one silently redrew the same page.
    """
    send_message(
        f"What is the subject of your message to {entry['display_name']}?\n"
        f"Keep it short. {CANCEL_HINT} to stop",
        sender_id, interface)
    update_user_state(sender_id, {
        'command': 'MAIL', 'step': 5,
        'recipient_id': entry['recipient_node_id'],
        'recipient_name': entry['display_name'],
    })


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
    send_message(with_help_tip(_chatter_windows_text(), sender_id, 'PUBLIC_CHATTER'),
                 sender_id, interface)
    update_user_state(sender_id, {
        'command': 'PUBLIC_CHATTER', 'step': 1,
        'channels': [],
    })


# "meshcore" and "meshtastic" both truncate to "me", which would make the
# two networks indistinguishable in exactly the place the distinction
# matters -- channel 2 on one is unrelated to channel 2 on the other.
_NETWORK_TAGS = {'meshcore': 'MC', 'meshtastic': 'MT', 'mqtt': 'MQ'}


def _network_tag(network: str) -> str:
    text = str(network or '').strip().casefold()
    return _NETWORK_TAGS.get(text, (text[:2] or '??').upper())


# The public form lives in utils now, because Node View labels the same ids.
# Kept as a module name here so the existing readability tests still address
# it where they always have.
_short_node = short_node_id


def _chatter_filter_options(state: dict) -> list:
    """The channels present in this window, as one numbered list.

    Reuses get_public_chatter_filters, which already reports exactly what is
    present -- offering a channel with nothing in it would be a filter that
    can only ever empty the screen.

    Nodes used to be a second section here, headed "Heard by:", listing raw
    capture ids. Those ids are per-RADIO, not per-node, so on a two-radio
    node the list read as two unlabelled 64-character keys meaning "my
    MeshCore radio" and "my Meshtastic radio". Node View owns that dimension
    now, across every screen rather than this one.
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
    return options


def _chatter_filter_text(state: dict) -> str:
    options = state.get('filter_options') or []
    if not options:
        return LINE_BREAK.join([
            "Nothing heard in this window to filter.",
            "[T]ime [0] Back",
        ])
    chosen = set(state.get('channels') or [])
    # One dimension, so no sub-headers: a heading over a single group is
    # noise, and it cost bytes on a screen that has 160 of them.
    lines = ["Channels  (* = shown)"]
    for number, option in enumerate(options, 1):
        # An empty `chosen` means no filter at all -- every channel is
        # actually being shown, same as the web feed -- but the screen
        # used to star nothing, which read as "these are all hidden" on a
        # legend that says * means shown. A live beta test read it exactly
        # that way. Starring everything when nothing is chosen keeps the
        # legend true without spending a single extra byte on new text.
        mark = '*' if (not chosen or option['value'] in chosen) else ' '
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
    if state.get('channels'):
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
        # The node dimension comes from the session's Node View lens, so
        # "show me Chattanooga" means the same thing on this screen as it
        # does on bulletins and mail rather than needing setting twice.
        capture_node_ids=list(get_view_scope(sender_id) or []),
        before_time=state.get('before_time', '') if older else '',
        before_id=int(state.get('before_id', 0)) if older else 0,
    )
    entries = result.get('entries', [])
    state['step'] = 2

    if not entries:
        state['has_more'] = False
        # The empty screen is exactly where the lens has to own up. Without
        # this, a scope that filters out every message reads as a quiet mesh
        # -- the same false "there is nothing here" the empty mailbox and the
        # empty board already guard against.
        lines = ["No older chatter." if older else "Nothing heard in this window."]
        lens = scope_notice(sender_id)
        if lens:
            lines.append(lens)
        lines.append("[T]ime [0] Back")
        send_message(LINE_BREAK.join(lines), sender_id, interface)
        update_user_state(sender_id, state)
        return

    # Fill to the byte ceiling rather than the entry count, so one very long
    # message does not turn a batch into a broadcast. The cursor follows what
    # was actually sent, so nothing is skipped on the next More.
    header = f"Public Chatter {state['hours']}h"
    # Folded into the header rather than sent separately, so the notice is
    # inside the batch's byte budget instead of costing another message.
    _lens = scope_notice(sender_id)
    if _lens:
        header = f"{_lens}{LINE_BREAK}{header}"
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
    key = 'channels'
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
        # Public Chatter is a top-level main-menu entry now, not a Utilities
        # submenu -- back means the main menu, same as Profile and Settings.
        handle_help_command(sender_id, interface)
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
            # Clears the channel filter only. The Node View lens is global
            # and survives -- [A]ll here means "all channels", and silently
            # widening the whole session from a channel screen would be a
            # different promise than the one the key makes.
            state['channels'] = []
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


# Room kept back from the page for the [P]rev/[N]ext/[0] control line, so a
# page that exactly fills the budget does not push its own controls into a
# second chunk -- two seconds of airtime to say "[0] Back".
_NODE_VIEW_CONTROL_BYTES = 40

NODE_VIEW_TITLE = "Node View (*=now)"
# The one sentence that has to do the explaining. It says the two things
# someone who wandered in needs: nothing is being withheld from the network,
# and this control is about reading. Longer phrasings cost a node off the
# page on MeshCore's 160 bytes.
NODE_VIEW_BLURB = "Posts sync everywhere. Pick whose you read."


def _node_view_options(sender_id) -> list:
    """The pickable nodes: All, This node, then whoever actually has content.

    Offering a node that holds nothing gives the user a choice whose only
    outcome is an empty screen they cannot tell apart from a bug, so the
    list comes from what is really stored -- the same rule the chatter
    filter follows.

    All and This node are always present. A lens that cannot return you to
    all of it, or to your own posts, is a trap rather than a filter.
    """
    local_ids = local_identities_for_display()
    nicknames = get_node_nicknames()
    options = [
        {'label': 'All nodes', 'ids': []},
        {'label': 'This node', 'ids': sorted(local_ids)},
    ]

    grouped = {}
    for node_id in get_content_source_nodes():
        if node_id in local_ids:
            continue
        label = node_display_name(node_id, local_ids=local_ids, nicknames=nicknames)
        grouped.setdefault(label, []).append(node_id)

    for label in sorted(grouped):
        # Every id the node answers to, not just the one that had content:
        # a nickname grouping a peer's BBS id with its chatter capture key
        # is the only thing joining those two namespaces, and dropping the
        # rest would filter half a node.
        ids = set(grouped[label]) | set(node_ids_for_name(label))
        options.append({'label': label, 'ids': sorted(ids)})
    return options


def _node_view_text(options, page: int, chosen, max_bytes: int) -> tuple:
    """Render one page, and say whether more follows.

    Filled by bytes rather than a fixed count, like the chatter batch: a
    twelve-node fleet at a fixed count becomes a six-chunk, twelve-second
    transmission.
    """
    chosen_ids = set(chosen or [])
    budget = max(64, int(max_bytes) - _NODE_VIEW_CONTROL_BYTES)
    head = f"{NODE_VIEW_TITLE}{LINE_BREAK}{NODE_VIEW_BLURB}"

    lines = []
    used = len(head.encode('utf-8'))
    index = int(page)
    while index < len(options):
        option = options[index]
        ids = set(option['ids'])
        # All nodes is the current choice exactly when nothing is narrowed.
        marked = (not chosen_ids) if not ids else (ids == chosen_ids)
        line = f"[{index + 1}]{'*' if marked else ' '}{option['label']}"
        cost = len(line.encode('utf-8')) + 1
        if lines and used + cost > budget:
            break
        lines.append(line)
        used += cost
        index += 1

    controls = []
    if index < len(options):
        controls.append("[N]ext")
    if page > 0:
        controls.append("[P]rev")
    controls.append("[0] Back")
    body = [head] + lines + [" ".join(controls)]
    return LINE_BREAK.join(body), index


def _node_view_page_of(options, index: int, interface) -> int:
    """Which page an option falls on, walking the same byte fill.

    Derived by running _node_view_text rather than by dividing, because
    pages are filled to a byte ceiling and hold however many entries fit --
    a fixed page size here would drift from what was actually rendered as
    soon as one node had a longer name than another.
    """
    max_bytes = get_max_text_bytes(interface)
    page = 0
    while page < len(options):
        _, next_page = _node_view_text(options, page, None, max_bytes)
        if index < next_page:
            return page
        if next_page <= page:
            break
        page = next_page
    return 0


def _node_view_page_before(options, page: int, interface) -> int:
    """The page that precedes `page`, by the same byte fill that built it."""
    max_bytes = get_max_text_bytes(interface)
    previous = 0
    cursor = 0
    while cursor < page:
        _, nxt = _node_view_text(options, cursor, None, max_bytes)
        if nxt <= cursor:
            break
        previous = cursor
        cursor = nxt
    return previous


def handle_node_view_command(sender_id, interface, notice=None) -> None:
    """Show the picker from the top."""
    _send_node_view_page(sender_id, interface, 0, notice=notice)


def _send_node_view_page(sender_id, interface, page: int, notice=None) -> None:
    options = _node_view_options(sender_id)
    page = max(0, min(int(page), max(0, len(options) - 1)))
    text, next_page = _node_view_text(
        options, page, get_view_scope(sender_id), get_max_text_bytes(interface))
    if notice:
        text = f"{notice}{LINE_BREAK}{text}"
    send_message(text, sender_id, interface)
    update_user_state(sender_id, {'command': 'NODE_VIEW', 'step': 1,
                                  'options': options, 'page': page,
                                  'next_page': next_page})


def handle_node_view_steps(sender_id, message, interface, state) -> None:
    """One number picks a node; there is no [D]one because this is not a
    multi-select. The choice applies at once and the screen redraws with the
    star moved, so the change is visible before leaving."""
    choice = str(message or '').strip().lower()
    options = state.get('options') or _node_view_options(sender_id)
    page = int(state.get('page', 0))

    if choice in ('0', 'x'):
        clear_user_state(sender_id)
        handle_help_command(sender_id, interface)
        return
    if choice == 'n' and int(state.get('next_page', 0)) < len(options):
        _send_node_view_page(sender_id, interface, int(state.get('next_page', 0)))
        return
    if choice == 'p' and page > 0:
        # The page before this one, found by walking the same byte fill --
        # jumping to 0 made anything past page two unreachable backwards.
        _send_node_view_page(sender_id, interface,
                             _node_view_page_before(options, page, interface))
        return

    if choice.isdigit():
        index = int(choice) - 1
        if 0 <= index < len(options):
            chosen_ids = options[index]['ids']
            # Compared before overwriting: "All nodes" carries [] and an
            # unset scope reads back as None/[], so both sides normalize
            # through the same set() rather than needing a special case
            # for the default.
            unchanged = set(chosen_ids) == set(get_view_scope(sender_id) or [])
            set_view_scope(sender_id, chosen_ids)
            # Redraw the page the choice is ON, not the page it was made
            # from. Numbers are global so that [1] All nodes is always [1]
            # and the "!V=all" every notice ends with stays true -- but on
            # MeshCore's 160 bytes a page holds about three entries, so
            # picking a number from a later page would otherwise redraw a
            # screen with no star anywhere on it. Selecting something and
            # being shown no confirmation reads as the key not working.
            #
            # Re-selecting the already-active option moves no star at all,
            # which is the one case that redraw doesn't cover -- a live
            # beta test read the identical screen as "nothing happened."
            notice = (f"Node View remains {options[index]['label']}."
                     if unchanged else None)
            _send_node_view_page(sender_id, interface,
                                 _node_view_page_of(options, index, interface),
                                 notice=notice)
            return

    _send_node_view_page(sender_id, interface, page, notice="Invalid choice.")


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


def _mail_recipient_refusal(query: str, sender_node_id, prefix: str = "") -> str:
    """Say which rule was actually hit.

    "not found, is ambiguous, or has not opted in" names three causes, and
    when the answer is "that is you" it is none of them. The Relay Directory
    lists the reader -- get_mail_relay_directory only excludes the sender
    when it is asked to, and the browse view does not ask -- so someone who
    has just read their own name there is told it does not exist. That reads
    as a broken lookup rather than a rule.
    """
    match = _resolve_mail_relay_recipient(query, None)
    if match and sender_node_id and str(sender_node_id) in {
            str(node_id) for node_id in match.get('node_ids', [])}:
        return (f"{prefix}That is you. Mail is for reaching somebody else -- "
                "anything you send yourself is already in your inbox.")
    return (f"{prefix}That relay user was not found, is ambiguous, or has "
            "not opted in.")


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
    send_message(with_help_tip(response, sender_id, 'BULLETIN_MENU'),
                 sender_id, interface)
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
    send_message(with_help_tip(menu, sender_id, 'GAMES_MENU'), sender_id, interface)
    update_user_state(sender_id, {'command': 'GAMES_MENU', 'step': 1})


def handle_games_steps(sender_id, message, interface):
    choice = message.strip()
    if choice.lower() in ('x', '0', 'exit'):
        # Games is a top-level main-menu entry now, not a Utilities
        # submenu -- back means the main menu, same as Profile and Settings.
        handle_help_command(sender_id, interface)
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
    if game_id == baconfall_port.game.GAME_ID:
        handle_baconfall_steps(sender_id, None, interface)
        return
    if game_id == trivia_port.GAME_ID:
        send_message(trivia_port.start(sender_id), sender_id, interface)
        update_user_state(sender_id, {'command': 'TRIVIA', 'step': 1, 'game_id': game_id})
        return
    sync_notice = get_zork_save_sync_notice()
    if has_zork_session(sender_id, game_id):
        intro = resume_zork_session(sender_id, game_id)
        send_message(intro, sender_id, interface)
        if not has_zork_session(sender_id, game_id):
            # The process had already died between the check above and the
            # resume attempt. resume_zork_session's own text already says
            # so ("Your previous session ended...") -- the bug was piling a
            # false "Zork I resumed. Send X to exit." on top of that, and
            # then parking the session in ZORK state anyway, where the next
            # command would hit "No active game session" with no
            # explanation of what happened to the one that just "resumed".
            handle_games_command(sender_id, interface)
            return
        if sync_notice:
            send_message(sync_notice, sender_id, interface)
        send_message(f"{game_name} resumed. Send X to exit.", sender_id, interface)
    elif has_zork_save(sender_id, game_id):
        # start_zork_session does real work here before it returns anything:
        # spawn a cold dfrotz process, then a restore/look handshake against
        # it. On a slow start that's several seconds of total silence, which
        # a beta tester read as the BBS having dropped the request rather
        # than being busy.
        send_message("Loading your saved game...", sender_id, interface)
        intro = start_zork_session(sender_id, game_id, max_chars=response_limit_for(interface))
        send_message(intro, sender_id, interface)
        if not has_zork_session(sender_id, game_id):
            # No interpreter installed, the story file missing with
            # autodownload off, or the interpreter process itself failed to
            # spawn -- start_zork_session's own text already explains
            # which. The same false "Saved game restored." used to follow
            # it regardless, and the save was never actually touched.
            handle_games_command(sender_id, interface)
            return
        if sync_notice:
            send_message(sync_notice, sender_id, interface)
        send_message(f"Saved game restored. Send X to exit.", sender_id, interface)
    else:
        intro = start_zork_session(sender_id, game_id, max_chars=response_limit_for(interface))
        send_message(intro, sender_id, interface)
        if not has_zork_session(sender_id, game_id):
            handle_games_command(sender_id, interface)
            return
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


def _web_fetch_prompt(interface, tail: str) -> str:
    """The URL prompt, naming the sites that will actually work.

    "must be an allowed host" asked the user to guess at a list only the
    operator can read. Naming it costs a few bytes and turns the prompt
    into its own answer.
    """
    import gateway
    hosts = gateway.allowed_hosts() if gateway.is_gateway_enabled() else []
    head = "Enter the URL to fetch"
    if not hosts:
        return f"{head}{tail}"
    budget = max(0, get_max_text_bytes(interface) - len(f"{head} (allowed: )") - len(tail))
    shown = []
    for host in hosts:
        candidate = ", ".join(shown + [host])
        if shown and len(candidate.encode('utf-8')) > budget:
            shown.append("...")
            break
        shown.append(host)
    return f"{head} (allowed: {', '.join(shown)}){tail}"


def _refuse_web_fetch(sender_id, interface) -> bool:
    """Say up front when no URL could possibly work, and stay out of the
    URL prompt. A user who types one anyway waits out a round trip over
    the radio to be told 'no allowed_hosts configured' -- which is how
    this feature spent its life looking broken."""
    import gateway
    reason = gateway.web_fetch_blocked_reason()
    if not reason:
        return False
    send_message(reason, sender_id, interface)
    handle_help_command(sender_id, interface)
    return True


def handle_apigw_command(sender_id, interface):
    if not _apigw_authorized(sender_id, interface):
        send_message("API gateway: your node is not on the allow-list.", sender_id, interface)
        handle_help_command(sender_id, interface)
        return
    if _refuse_web_fetch(sender_id, interface):
        return
    send_message(_web_fetch_prompt(interface, f", or {CANCEL_HINT} to stop:"),
                 sender_id, interface)
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
    if is_cancel(choice) or (step == 1 and choice.lower() in ('x', '0', 'exit')):
        handle_help_command(sender_id, interface)
        return

    if step == 1:
        if choice == '1':
            update_user_state(sender_id, {'command': 'APIGW', 'step': 2, 'mode': 'ai'})
            send_message("Type your question for Project Nomad:", sender_id, interface)
        elif choice == '2':
            if _refuse_web_fetch(sender_id, interface):
                return
            update_user_state(sender_id, {'command': 'APIGW', 'step': 2, 'mode': 'http'})
            send_message(_web_fetch_prompt(interface, ":"), sender_id, interface)
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
    send_message(f"Type your question for Project Nomad, or {CANCEL_HINT} to stop:",
                 sender_id, interface)
    update_user_state(sender_id, {'command': 'ASK_NOMAD', 'step': 1})


ASK_NOMAD_FOLLOWUP = f"Reply with another question, or [0]/{CANCEL_HINT} for the main menu."


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
    # is_cancel() catches the bang form (!cancel, !exit, !x, !0); a beta
    # test found that typing it here got submitted AS the question instead
    # of cancelling -- only bare 0/x/exit were recognized, and even those
    # returned to the main menu with no word said about what happened to
    # the question in progress. This is that word.
    if is_cancel(choice) or choice.lower() in ('0', 'x', 'exit'):
        send_message("Question cancelled.", sender_id, interface)
        handle_help_command(sender_id, interface)  # back to the main menu --
        # the immediate parent here, since Ask Nomad is itself a main-menu
        # shortcut ('N'), not a nested submenu.
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
        send_message(f"No scores yet for {info['name']}. Be first!\n[0] Back",
                     sender_id, interface)
    else:
        lines = [f"🏆 {info['name']}"]
        for rank, (short_name, score, max_score, moves) in enumerate(scores, 1):
            ms = f"/{max_score}" if max_score else ""
            lines.append(f"{rank}. {short_name} {score}{ms} {moves}mv")
        lines.append("[0] Back")
        send_message("\n".join(lines), sender_id, interface)
    update_user_state(sender_id, {'command': 'SCOREBOARD', 'step': 1})


def _settings_menu_text(sender_id, interface, sender_node_id=None) -> str:
    """Who you are and what the BBS does for you, on one screen.

    These were two menus. The split was deliberate -- identity in Profile,
    behaviour in Settings -- and the note that used to sit on
    handle_profile_command warned that an earlier combined version "made
    neither easy to find". That warning is about a flat list of unrelated
    entries, so this is not one: your details come first as plain lines, then
    the things you can change, headed so the eye can skip to them.

    Built fresh each time rather than held as a constant, because every line
    reports a current value -- a menu that says "Offline relay" without
    saying whether it is on makes the user open it to find out.
    """
    node_id = sender_node_id or get_node_id_from_num(sender_id, interface)
    lines = []

    profile = get_user_profile(sender_id)
    if profile:
        _, short_name, _long_name, first_seen, _last_seen, msg_count, bio = profile
        lines.append(f"👤 {short_name}")
        # The alias, when set, is the name that actually appears on this
        # person's posts; short_name is whatever their radio reports. Shown
        # only when they differ, which is when "why does my name look like
        # that?" arises.
        alias, devices = '', 0
        try:
            account_id = get_account_id_for_node(node_id) if node_id else None
            if account_id:
                alias = (get_account_alias(account_id) or '').strip()
                devices = len(get_linked_node_ids(account_id) or [])
        except Exception:
            logging.debug("could not read account details for profile", exc_info=True)
        if alias and alias != short_name:
            lines.append(f"Posts as: {alias}")

        stats = f"Since:{(first_seen or '?')[:10]} Msgs:{msg_count}"
        try:
            role = normalize_role(get_node_role(node_id)) if node_id else ''
        except Exception:
            role = ''
        if role and role != ROLE_UNREGISTERED:
            stats += f" Role:{role}"
        lines.append(stats)

        scores = get_user_game_scores(sender_id)
        if scores:
            parts = []
            for game_id, score, max_score in scores[:3]:
                gname = GAMES.get(game_id, {}).get('name', game_id)[:8]
                ms = f"/{max_score}" if max_score else ""
                parts.append(f"{gname}:{score}{ms}")
            lines.append("Scores: " + " ".join(parts))
        if bio:
            lines.append(f"Bio: {bio}")
    else:
        lines.append("👤 You")
        devices = 0

    relay = "On" if (node_id and get_mail_relay_preference(node_id)) else "Off"
    scope = get_view_scope(sender_id)
    lens = "All nodes" if not scope else _scope_label(scope)
    tips = "On" if get_help_tips_enabled(sender_id) else "Off"
    device_note = f" ({devices})" if devices > 1 else ""

    lines.append("⚙️ Settings")
    lines.append("[1] Edit bio")
    lines.append(f"[2] Linked devices{device_note}")
    lines.append(f"[3] Offline mail relay: {relay}")
    lines.append(f"[4] Node View: {lens}")
    lines.append(f"[5] Help tips: {tips}")
    lines.append("[6] About this node")
    lines.append("[7] View Stats")
    lines.append("[0] Back")
    return LINE_BREAK.join(lines)


def _scope_label(scope) -> str:
    """The narrowed lens, said the way the Node View picker says it."""
    try:
        local_ids = local_identities_for_display()
        if set(scope) & set(local_ids or ()):
            return "This node"
        nicknames = get_node_nicknames()
        for node_id in scope:
            return node_display_name(node_id, local_ids=local_ids,
                                     nicknames=nicknames)
    except Exception:
        logging.debug("could not label the view scope", exc_info=True)
    return "Narrowed"


def handle_settings_command(sender_id, interface, sender_node_id=None):
    send_message(with_help_tip(
        _settings_menu_text(sender_id, interface, sender_node_id),
        sender_id, 'settings'), sender_id, interface)
    update_user_state(sender_id, {'command': 'SETTINGS', 'step': 1})


def handle_settings_steps(sender_id, message, interface, sender_node_id=None):
    """Input for the combined Settings & Profile screen.

    Step 2 is the relay confirmation, step 3 is the bio composer. The bio
    used to live in PROFILE's own step 2; both states are still accepted so a
    session that was mid-edit when this shipped does not lose what it typed.
    """
    state = get_user_state(sender_id) or {}
    choice = message.strip()
    lowered = choice.lower()

    if state.get('step') == 2:
        # Confirming the relay toggle: it changes what the BBS does with
        # your mail, so it asks first.
        if lowered not in ('y', 'yes'):
            send_message("Relay setting unchanged.", sender_id, interface)
            handle_settings_command(sender_id, interface, sender_node_id)
            return
        if not sender_node_id:
            send_message("Couldn't verify your device identity.", sender_id, interface)
            handle_settings_command(sender_id, interface, sender_node_id)
            return
        records = set_mail_relay_for_node(
            sender_node_id, bool(state.get('relay_enabled')),
            home_network(sender_node_id))
        for node_id, enabled, updated_at in records:
            send_mail_relay_preference_to_bbs_nodes(
                node_id, enabled, updated_at, interface.bbs_nodes, interface)
        status = "enabled" if state.get('relay_enabled') else "disabled"
        send_message(f"Offline mail relay {status} for all linked devices.",
                     sender_id, interface)
        handle_settings_command(sender_id, interface, sender_node_id)
        return

    if state.get('step') == 3:
        if is_cancel(choice):
            handle_settings_command(sender_id, interface, sender_node_id)
            return
        if lowered == 'clear':
            # A blank line can never reach here to mean "clear it":
            # ssh_server treats an empty line as "just redraw", and an empty
            # payload over the radio never arrives at all. A beta tester
            # found a blank submission silently did nothing and left no way
            # to remove a bio. This is the documented way instead.
            update_user_bio(sender_id, '')
            send_message("Bio cleared.", sender_id, interface)
            handle_settings_command(sender_id, interface, sender_node_id)
            return
        update_user_bio(sender_id, choice[:100])
        send_message("Bio updated!" if len(choice) <= 100
                     else "Bio updated! (trimmed to 100 chars)",
                     sender_id, interface)
        handle_settings_command(sender_id, interface, sender_node_id)
        return

    if lowered in ('0', 'x', 'back', 'exit'):
        handle_help_command(sender_id, interface)
        return
    if lowered in ('1', 'e'):
        send_message(f"Enter your bio (max 100 chars), CLEAR to remove it, or {CANCEL_HINT} to stop:",
                     sender_id, interface)
        update_user_state(sender_id, {'command': 'SETTINGS', 'step': 3})
        return
    if lowered in ('2', 'd'):
        handle_account_command(sender_id, interface)
        return
    if choice == '3':
        if not sender_node_id:
            send_message("Couldn't verify your device identity.", sender_id, interface)
            return
        enabled = not get_mail_relay_preference(sender_node_id)
        action = "Enable" if enabled else "Disable"
        send_message(f"{action} offline mail relay for all linked devices? [Y/N]",
                     sender_id, interface)
        update_user_state(sender_id, {'command': 'SETTINGS', 'step': 2,
                                      'relay_enabled': enabled})
        return
    if choice == '4':
        handle_node_view_command(sender_id, interface)
        return
    if choice == '5':
        # Toggled outright rather than confirmed: it changes nothing but
        # what this person sees, and it is reversible from the line that
        # reports it.
        enabled = not get_help_tips_enabled(sender_id)
        set_help_tips_enabled(sender_id, enabled)
        send_message("Help tips on." if enabled
                     else "Help tips off. Turn them back on here any time.",
                     sender_id, interface)
        handle_settings_command(sender_id, interface, sender_node_id)
        return
    if choice == '6':
        handle_version_command(sender_id, interface)
        handle_settings_command(sender_id, interface, sender_node_id)
        return
    if choice == '7':
        handle_stats_command(sender_id, interface)
        return
    # Redrawing the identical screen with no notice looked like the BBS had
    # ignored the keypress rather than rejected it.
    send_message("Invalid choice." + LINE_BREAK
                 + _settings_menu_text(sender_id, interface, sender_node_id),
                 sender_id, interface)


def handle_profile_command(sender_id, interface, notice=None,
                           sender_node_id=None):
    """Profile is now the top half of Settings & Profile.

    Kept as a name rather than deleted: !P still reaches it, the account
    screens return here when a link finishes, and a user who learned the old
    main-menu letter should land somewhere sensible rather than nowhere.
    """
    if notice:
        send_message(notice, sender_id, interface)
    handle_settings_command(sender_id, interface, sender_node_id)


def handle_profile_steps(sender_id, message, interface, sender_node_id=None):
    """Input arriving in the retired PROFILE state.

    Reachable by someone whose session predates the merge, and by !P. The
    step numbers have to be translated, not just passed along: PROFILE step
    2 was the bio composer, while SETTINGS step 2 is the relay Y/N. Handing
    one to the other verbatim would read somebody's half-written bio as a
    confirmation and answer "no" to it.
    """
    state = get_user_state(sender_id) or {}
    if state.get('command') == 'PROFILE':
        # Only a genuinely legacy state gets rewritten. Once the first
        # keystroke has moved the session to SETTINGS, that state is live --
        # step 3 is a bio half-typed -- and stamping step 1 over it would
        # feed the next line back to the menu as a choice.
        translated = 3 if state.get('step') == 2 else 1
        update_user_state(sender_id, {'command': 'SETTINGS', 'step': translated})
    handle_settings_steps(sender_id, message, interface, sender_node_id)


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
            send_message(f"Enter the 6-digit code from your other device, or {CANCEL_HINT} to stop:", sender_id, interface)
            _account_state(sender_id, 2)
            return
        if choice == '3':
            _handle_list_devices(sender_id, interface, sender_node_id)
            return
        if choice == '4':
            send_message(
                "Enter a shared alias (max 20 chars). Shown instead of this "
                "device's short name on your posts once you have at least "
                f"one linked device. Send {CANCEL_HINT} to stop:",
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
        if is_cancel(choice):
            handle_account_command(sender_id, interface)
            return
        _handle_submit_link_code(sender_id, interface, sender_node_id, choice)
        return

    if step == 4:
        if is_cancel(choice):
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


def _linked_device_label(node_id, roster) -> str:
    """What to call one of a user's linked devices, so they can tell their
    own apart well enough to unlink the right one.

    Everything here comes from what the node already stores -- the radio
    roster in mesh_clients (a device's own advertised name and hardware
    model), and the id's own shape. Nothing new to collect, and nothing a
    user has to set: a device that has never told the mesh its name has no
    name for us to show, and no amount of asking here would produce one.

    mesh_clients rather than interface.nodes, because this screen runs in
    bacon-ssh, a different process from the one holding the radio link --
    that roster is empty here, the same separate-process gap that once
    left Node View inert over SSH. The persisted table is what survives
    the boundary.

    An SSH account identity is never in the roster (it is not a device any
    radio has heard), so it falls back to the shortened id -- which at
    least differs visibly between entries, where seven full 36-character
    "ssh:<uuid>" strings did not. That was the live beta test's actual
    complaint.
    """
    entry = (roster or {}).get(str(node_id)) or {}
    name = (entry.get('short_name') or entry.get('long_name') or '').strip()
    hardware = (entry.get('hw_model') or '').strip()
    # UNSET is what a Meshtastic device reports before anyone configures
    # it -- real, and useless to print. 27 of this node's own roster say
    # it.
    if name and hardware and hardware.upper() != 'UNSET':
        return f"{name} ({hardware})"
    if name:
        return name
    return short_node_id(node_id)


def _handle_list_devices(sender_id, interface, sender_node_id):
    account_id = get_account_id_for_node(sender_node_id)
    if account_id is None:
        send_message("No linked devices yet. Choose [1] to get a link code.", sender_id, interface)
        handle_account_command(sender_id, interface)
        return
    detail = get_linked_nodes_detail(account_id)
    roster = get_mesh_client_names([row[0] for row in detail])
    alias = get_account_alias(account_id)
    lines = [f"\U0001F517 Account alias: {alias or '(none set)'}"]
    for i, (node_id, network, linked_at) in enumerate(detail):
        marker = " (this device)" if node_id == sender_node_id else ""
        # linked_at was already fetched and thrown away, which is most of
        # what a live beta test asked for here: with several opaque
        # "ssh:<uuid>" entries and only one marked as "this device", there
        # was nothing to go on for deciding which OLD one is safe to
        # unlink. The date alone -- not a full timestamp, which would not
        # fit several devices into one screen -- is enough to tell them
        # apart.
        when = f" -- linked {linked_at[:10]}" if linked_at else ""
        label = _linked_device_label(node_id, roster)
        lines.append(f"{i + 1:02d}. {label} [{network}]{marker}{when}")
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
    roster = get_mesh_client_names([row[0] for row in detail])
    lines = ["Reply with the number of the device to unlink:"]
    for i, (node_id, network, linked_at) in enumerate(detail):
        when = f" -- linked {linked_at[:10]}" if linked_at else ""
        label = _linked_device_label(node_id, roster)
        lines.append(f"{i + 1:02d}. {label} [{network}]{when}")
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
    if not choice.startswith('!') and len(choice) == 2 and choice[1].lower() == 'x':
        choice = choice[0]

    if choice.lower() in ('save', 'restore'):
        send_message("Your game auto-saves after each command. No manual save needed.", sender_id, interface)
        update_user_state(sender_id, {'command': 'ZORK', 'step': 1, 'game_id': game_id})
        return

    if choice.lower() in ('x', 'quit', 'exit'):
        stop_zork_session(sender_id, game_id)
        send_message("Exited game.", sender_id, interface)
        # Back to the Games menu you launched from, not Utilities -- Games
        # left Utilities, and this already matched the Scoreboard/Hall of
        # Fame pattern of returning to handle_games_command rather than
        # skipping past it to whatever the parent menu used to be.
        handle_games_command(sender_id, interface)
        return

    response = send_zork_command(sender_id, choice, game_id,
                                 max_chars=response_limit_for(interface))
    send_message(response, sender_id, interface)
    # Capture score if the game output contains one
    parsed = parse_game_score(response)
    if parsed:
        score, max_score, moves = parsed
        node_id = get_node_id_from_num(sender_id, interface)
        short_name = get_node_short_name(node_id, interface) or str(sender_id)
        upsert_game_score(sender_id, game_id, short_name, score, max_score, moves)
    update_user_state(sender_id, {'command': 'ZORK', 'step': 1, 'game_id': game_id})


def handle_baconfall_steps(sender_id, message, interface):
    """Baconfall owns its input and saves before acknowledging each turn."""
    try:
        node_id = get_node_id_from_num(sender_id, interface)
        short_name = get_node_short_name(node_id, interface) or str(sender_id)
        response, leave, _ = baconfall_port.play(sender_id, message, short_name)
    except baconfall_port.SaveUnavailable as exc:
        send_message(str(exc), sender_id, interface)
        handle_games_command(sender_id, interface)
        return
    except sqlite3.Error:
        logging.exception('Baconfall could not save a turn for %s', sender_id)
        send_message('Baconfall could not save this turn. Your previous save is intact; please try again.',
                     sender_id, interface)
        return
    send_message(response, sender_id, interface)
    if leave:
        handle_games_command(sender_id, interface)
    else:
        update_user_state(sender_id, {'command': 'BACONFALL', 'step': 1,
                                     'game_id': baconfall_port.game.GAME_ID})


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
    if not message.startswith('!') and len(message) == 2 and message[1] == 'x':
        message = message[0]

    if step == 1:
        _stats_alias = {'1': 'n', '2': 'h', '3': 'r', '0': 'x'}
        choice = _stats_alias.get(message, message)
        if choice == 'x':
            # Stats lives under Settings now ([4] View Stats), not at the
            # top level -- back means Settings, the same as Linked Devices'
            # [0] returns to the Profile screen it opened from.
            handle_settings_command(sender_id, interface)
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
            scope = get_view_scope(sender_id)
            bulletins = get_bulletins(board_name, scope)
            # One notice on the header, never on the per-bulletin sends
            # below -- those are one radio message each, and that is where
            # the airtime actually goes.
            lens = scope_notice(sender_id, count_hidden_bulletins(board_name, scope))
            if bulletins:
                header = f"Select a bulletin number to view from {board_name}:"
                if lens:
                    header = f"{lens}{LINE_BREAK}{header}"
                send_message(header, sender_id, interface)
                # Positional, not the raw database id -- an id skips
                # whatever was deleted or never synced here, so "[8]" next
                # to "[23]" looked like a bulletin number a reader could
                # type, and "2" for the second item in the list came back
                # "Invalid bulletin number." A beta tester hit this on the
                # live General board.
                for i, bulletin in enumerate(bulletins, start=1):
                    send_message(f"[{i}] {bulletin[1]}", sender_id, interface)
                update_user_state(sender_id, {'command': 'BULLETIN_READ', 'step': 3,
                                              'board': board_name, 'bulletins': bulletins})
            else:
                empty = f"No bulletins in {board_name}."
                if lens:
                    empty = f"{empty}{LINE_BREAK}{lens}"
                send_message(empty, sender_id, interface)
                handle_bb_steps(sender_id, 'e', 1, state, interface, bbs_nodes)
        elif message.lower() == 'p':
            if board_name.lower() == 'urgent':
                node_id = get_node_id_from_num(sender_id, interface)
                allow_lists = _urgent_board_allow_lists(interface)
                logging.info(f"Checking permissions for node_id: {node_id} with allowed_nodes: {allow_lists}")  # Debug statement
                if not urgent_board_permitted(node_id, allow_lists):
                    send_message(_urgent_refusal(node_id), sender_id, interface)
                    handle_bb_steps(sender_id, 'e', 1, state, interface, bbs_nodes)
                    return
            send_message(f"What is the subject of your bulletin? Keep it short. {CANCEL_HINT} to stop", sender_id, interface)
            update_user_state(sender_id, {'command': 'BULLETIN_POST', 'step': 4, 'board': board_name})

    elif step == 3:
        bulletins = state.get('bulletins', [])
        try:
            index = int(message) - 1
            if index < 0 or index >= len(bulletins):
                raise ValueError
        except ValueError:
            send_message("Invalid bulletin number. Please try again.", sender_id, interface)
            return
        bulletin = get_bulletin_content(bulletins[index][0])
        if bulletin is None:
            send_message("Bulletin not found. Please try again.", sender_id, interface)
            return
        sender_short_name, date, subject, content, unique_id, content_complete, expected_length = bulletin
        notice = _incomplete_notice(content_complete, expected_length, content)
        send_message(f"From: {sender_short_name}\nDate: {date}\nSubject: {subject}\n- - - - - - -\n{content}{notice}", sender_id, interface)
        board_name = state['board']
        # Only a moderator is offered anything here, and only they are held
        # on the post afterwards. Everyone else bounces straight back to the
        # list exactly as before -- the option costs them no bytes and no
        # extra key.
        if _can_moderate(sender_id, interface):
            send_message(f"[D]elete this post  [0]Back", sender_id, interface)
            update_user_state(sender_id, {
                'command': 'BULLETIN_MODERATE', 'step': 1,
                'board': board_name, 'boards': state.get('boards', []),
                'unique_id': unique_id, 'subject': subject})
            return
        handle_bb_steps(sender_id, 'e', 1, state, interface, bbs_nodes)

    elif step == 4:
        if is_cancel(message):
            send_message("Bulletin cancelled.", sender_id, interface)
            handle_bulletin_command(sender_id, interface)
            return
        subject = message
        send_message(f"Send the contents of your bulletin. Send END when finished, or {CANCEL_HINT} to stop.", sender_id, interface)
        update_user_state(sender_id, {'command': 'BULLETIN_POST_CONTENT', 'step': 5, 'board': state['board'], 'subject': subject, 'content': ''})

    elif step == 5:
        if is_cancel(message):
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
    # "!x" is a cancel word, not the NX-style trailing-x shorthand.
    if not message.startswith('!') and len(message) == 2 and message[1] == 'x':
        message = message[0]
    if step in (3, 5, 7) and is_cancel(message):
        send_message("Mail cancelled.", sender_id, interface)
        handle_mail_command(sender_id, interface)
        return

    if step == 1:
        choice = message.lower()
        _mail_step1_alias = {'1': 'r', '2': 's', '3': 'a', '0': 'x'}
        choice = _mail_step1_alias.get(choice, choice)
        if choice == 'r':
            sender_node_id = get_node_id_from_num(sender_id, interface)
            scope = get_view_scope(sender_id)
            mail = get_mail(sender_node_id, scope)
            # Mail is the one scope where a lens can hide something someone
            # is waiting on, so the count of what it held back is said out
            # loud -- most of all on the empty mailbox, where otherwise the
            # BBS would flatly claim there is nothing.
            lens = scope_notice(sender_id, count_hidden_mail(sender_node_id, scope),
                                noun='mail')
            if mail:
                header = f"You have {len(mail)} mail messages. Select a message number to read:"
                if lens:
                    header = f"{lens}{LINE_BREAK}{header}"
                send_message(header, sender_id, interface)
                for msg in mail:
                    send_message(f"-{msg[0]}-\nDate: {msg[3]}\nFrom: {msg[1]}\nSubject: {msg[2]}", sender_id, interface)
                update_user_state(sender_id, {'command': 'MAIL', 'step': 2})
            else:
                empty = "There are no messages in your mailbox.📭"
                if lens:
                    empty = f"{empty}{LINE_BREAK}{lens}"
                send_message(empty, sender_id, interface)
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
        if is_cancel(message) or message.strip() == '0':
            # "0" is consistently the back command everywhere else in this
            # BBS, but it was also a syntactically valid message id here --
            # int("0") never raised, so it fell all the way to
            # get_mail_content(0, ...) and came back as "Mail not found"
            # with the session silently cleared. Treat it as the back
            # command it looks like before trying it as an id.
            handle_mail_command(sender_id, interface)
            return
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
            send_message("Mail not found. Reply 0 to go back.", sender_id, interface)

    elif step == 3:
        sender_node_id = get_node_id_from_num(sender_id, interface)
        recipient = _resolve_mail_relay_recipient(message, sender_node_id)
        if recipient is None:
            send_message(_mail_recipient_refusal(message, sender_node_id),
                         sender_id, interface)
            handle_mail_command(sender_id, interface)
        else:
            send_message(f"What is the subject of your message to {recipient['display_name']}?\nKeep it short. {CANCEL_HINT} to stop", sender_id, interface)
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
            _begin_mail_reply(
                sender_id, interface, state['mail_id'], state['sender'], state['subject'])
        else:
            send_message("The message has been kept in your inbox.✉️", sender_id, interface)
            update_user_state(sender_id, None)

    elif step == 5:
        subject = message
        send_message(f"Send your message in one or more parts. Send END when done, or {CANCEL_HINT} to stop.", sender_id, interface)
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
            is_reply = 'reply_to_mail_id' in state
            if is_reply:
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

            # Whether this was a reply or a fresh message, there is nothing
            # left to ask once it is sent -- land back on the mailbox
            # rather than the old step-8 "Send another? [Y/N]" gate, which
            # never printed a prompt of its own and left the next keypress
            # meaning something the user could not see (typing "y" opened
            # the mail menu; anything else silently cleared the session).
            handle_mail_command(sender_id, interface)
        else:
            state['content'] += message + "\n"
            update_user_state(sender_id, state)

    elif step == 9:
        entries = state.get('directory', [])
        _visible, page, page_count = mail_directory_page_view(
            entries, state.get('directory_page', 0))
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
        selected, problem = _directory_selection(entries, page, message)
        if problem == 'not_a_number':
            send_message("Invalid selection. Reply with a listed number, N, P, or X.", sender_id, interface)
            return
        if problem:
            send_message("Invalid selection. Please choose a listed user.", sender_id, interface)
            return
        _begin_mail_to_directory_entry(sender_id, interface, selected)

    elif step == 10:
        entries = state.get('directory', [])
        _visible, page, page_count = mail_directory_page_view(
            entries, state.get('directory_page', 0))
        choice = message.lower()
        # The page footer prints "[0] Back", and 0 is Back everywhere else in
        # the BBS, but this handler only ever accepted 'x' -- so the one key
        # the screen told you to press was the one that did nothing.
        if choice in ('0', 'x'):
            handle_mail_command(sender_id, interface)
            return
        if choice in ('n', 'p'):
            page = max(0, min(page + (1 if choice == 'n' else -1), page_count - 1))
            state['directory_page'] = page
            update_user_state(sender_id, state)
            send_message(_mail_directory_page(entries, page, selecting=False), sender_id, interface)
            return
        # A number picks that person and starts a message to them, the same
        # as it does when the directory is reached through Send. The entries
        # are numbered either way, so a number is the obvious thing to type
        # here -- and it used to redraw the identical page, which reads as
        # the key being broken rather than as "browsing only".
        selected, problem = _directory_selection(entries, page, message)
        if selected is not None:
            _begin_mail_to_directory_entry(sender_id, interface, selected)
            return
        notice = ("No one is listed at that number on this page."
                  if problem == 'out_of_range'
                  else "Reply with a number to write to someone, N, P, or 0.")
        send_message(f"{notice}{LINE_BREAK}"
                     f"{_mail_directory_page(entries, page, selecting=False)}",
                     sender_id, interface)


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
    send_message(with_help_tip(response, sender_id, 'CHANNEL_DIRECTORY'),
                 sender_id, interface)
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
    if not message.startswith('!') and len(message) == 2 and message[1] == 'x':
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
                    # Deliberately unscoped: this is a liveliness preview,
                    # not a read. Filtering it would label a busy post "No
                    # comments yet", which is simply false -- and since the
                    # lens is a view over shared content rather than a
                    # boundary, naming a commenter from another node costs
                    # nothing. The lens applies where comments are read.
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
            scope = get_view_scope(sender_id)
            comments = get_channel_comments(channel_id, scope)
            lens = scope_notice(sender_id,
                                count_hidden_channel_comments(channel_id, scope))
            if comments:
                for i, comment in enumerate(comments, start=1):
                    sender_short_name, date, content = comment[1], comment[2], comment[3]
                    send_message(f"[{i}] {sender_short_name} @ {date}\n{content}", sender_id, interface)
            else:
                send_message("No comments yet for this post.", sender_id, interface)
            # Folded onto the controls line the screen already sends, so a
            # narrowed view costs no extra message.
            controls = "[1]View comments [2]Comment [0]Exit"
            if comments and _can_moderate(sender_id, interface):
                controls = "[1]View comments [2]Comment [D]elete [0]Exit"
                update_user_state(sender_id, {
                    'command': 'COMMENT_MODERATE', 'step': 0,
                    'channel_id': channel_id,
                    # Carried in state so the number a moderator types means
                    # the line they just read, not whatever the database
                    # returns when they get around to typing it.
                    'comments': [(str(c[4]), str(c[1])) for c in comments]})
            if lens:
                controls = f"{lens}{LINE_BREAK}{controls}"
            send_message(controls, sender_id, interface)
            return
        if choice == 'c':
            send_message(f"Send your comment. Send END when finished, or {CANCEL_HINT} to stop.", sender_id, interface)
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
        if is_cancel(message):
            send_message("Comment cancelled.", sender_id, interface)
            send_message("[1]View comments [2]Comment [0]Exit", sender_id, interface)
            update_user_state(sender_id, {
                'command': 'CHANNEL_DIRECTORY',
                'step': 6,
                'channel_id': state.get('channel_id'),
                'channel_name': state.get('channel_name')
            })
            return
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
        if is_cancel(message):
            handle_channel_directory_command(sender_id, interface)
            return
        channel_name = message
        send_message(
            "Send the channel URL or PSK, e.g. a Meshtastic channel URL "
            f"(https://meshtastic.org/e/#...) or a MeshCore PSK/passphrase. "
            f"Not required -- send \"none\" if you don't have one, or "
            f"{CANCEL_HINT} to stop:",
            sender_id, interface)
        update_user_state(sender_id, {'command': 'CHANNEL_DIRECTORY', 'step': 4, 'channel_name': channel_name})

    elif step == 4:
        if is_cancel(message):
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
        sender_node_id = get_node_id_from_num(sender_id, interface)
        recipient = _resolve_mail_relay_recipient(recipient_query, sender_node_id)
        if recipient is None:
            send_message(
                _mail_recipient_refusal(
                    recipient_query, sender_node_id,
                    prefix=f"Relay user '{recipient_query}': ")
                + " Send !AU to browse.",
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
        scope = get_view_scope(sender_id)
        mail = get_mail(sender_node_id, scope)
        lens = scope_notice(sender_id, count_hidden_mail(sender_node_id, scope),
                            noun='mail')
        if not mail:
            empty = "You have no new messages."
            if lens:
                empty = f"{empty}{LINE_BREAK}{lens}"
            send_message(empty, sender_id, interface)
            return

        response = "📬 You have the following messages:\n"
        if lens:
            response = f"{lens}\n{response}"
        for i, msg in enumerate(mail):
            response += f"{i + 1:02d}. From: {msg[1]}, Subject: {msg[2]}\n"
        response += "\nPlease reply with the number of the message you want to read."
        send_message(response, sender_id, interface)

        update_user_state(sender_id, {'command': 'CHECK_MAIL', 'step': 1, 'mail': mail})

    except Exception as e:
        logging.error(f"Error processing check mail command: {e}")
        send_message("Error processing check mail command.", sender_id, interface)


def handle_read_mail_command(sender_id, message, state, interface):
    if is_cancel(message) or message.strip() == '0':
        # "0" is consistently the back command everywhere else in this BBS,
        # but this list is numbered from 1 (see handle_check_mail_command's
        # "01.", "02." ...), so "0" fell outside the valid range and this
        # screen's only response was "Invalid message number. Please try
        # again." forever, with no way out but disconnecting.
        handle_mail_command(sender_id, interface)
        return
    try:
        mail = state.get('mail', [])
        message_number = int(message) - 1

        if message_number < 0 or message_number >= len(mail):
            send_message("Invalid message number, or 0 to go back. Please try again.", sender_id, interface)
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
        if not choice.startswith('!') and len(choice) == 2 and choice[1] == 'x':
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
            _begin_mail_reply(
                sender_id, interface, state['mail_id'], state['sender'], state['subject'])
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
        response = f"Channel Name: {channel_name}\nChannel URL: {channel_url}\n[0] Back"
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


def handle_welcome_command(sender_id, interface, *, first_contact=False):
    """Say what this BBS is and which node you have reached.

    Sent once, unprompted, on someone's very first message, and any time
    afterwards on request. The first-contact copy adds the one line a
    stranger actually needs -- that there is a menu and how to get it --
    because at that moment they have not chosen to be here and may have no
    idea what just answered them.
    """
    text = welcome_text(interface)
    if first_contact:
        text = f"{text}{LINE_BREAK}Send ? any time for the menu."
    send_message(text, sender_id, interface)


def handle_version_command(sender_id, interface):
    """Answer "what am I talking to?" -- the node's name and its version.

    The number existed all along, reachable only from the web admin, the
    Docker build and the version module itself, so nobody on a radio or an
    SSH session could say which release they had reached. That made "is
    the fix live yet?" unanswerable from the side that would notice.
    """
    from version_info import get_display_version
    where = _this_node_label()
    send_message(f"Bacon BBS {get_display_version()}"
                 + (f" on {where}" if where else ''), sender_id, interface)


def _this_node_label() -> str:
    """A name for the node the user is actually connected to, or ''.

    Deliberately not node_display_name: that answers "whose content is
    this?" and so calls every local id 'this node' -- true, and useless in
    a sentence whose whole job is to say WHICH node.

    And deliberately not get_local_node_id() alone. That is a module global
    set when a radio link comes up, so it is empty in bacon-ssh and
    bacon-web-admin, which is the same separate-process gap that once left
    Node View inert over SSH. The persisted link ids are what survive a
    process boundary.
    """
    from db_operations import get_local_node_id, get_persisted_local_link_ids
    candidates = [str(get_local_node_id() or '').strip()]
    try:
        candidates += get_persisted_local_link_ids()
    except Exception:
        logging.debug("could not read local link identities", exc_info=True)
    candidates = [c for c in candidates if c]

    nicknames = get_node_nicknames()
    for node_id in candidates:
        if node_id in nicknames:
            return nicknames[node_id]
    for node_id in candidates:
        if node_id.startswith('mqtt:'):
            tail = node_id.rsplit(':', 1)[-1].strip()
            if tail:
                return tail
    return short_node_id(candidates[0]) if candidates else ''


def handle_quick_help_command(sender_id, interface):
    response = (
        "✈️QUICK COMMANDS✈️\n"
        "!SM,, - Send Mail\n!CM - Check Mail\n!R - Reply to latest mail\n"
        "!AU - Relay Directory\n"
        "!PB,, - Post Bulletin\n!CB,, - Check Bulletins\n"
        "!CHP,, - Post Channel\n!CHL - List Channels\n"
        "!VER - This node and its version\n"
        "!WELCOME - What this BBS is\n"
        "Global menus: !Q !B !G !H !U !P !N !A !S !V !X"
    )
    # Only shown to someone who can use them. A moderator's toolkit listed on
    # everyone's help screen is an invitation to try it, and every attempt
    # costs the node a refusal on air.
    if _role_commands_available(sender_id, interface):
        response += "\n!ROLE,,<node>,,<role> - Set a role\n!WHO,,<node> - Look one up"
    send_message(response, sender_id, interface)


def _role_commands_available(sender_id, interface) -> bool:
    if not is_bbs_role_management_enabled():
        return False
    node_id = get_node_id_from_num(sender_id, interface)
    return bool(node_id) and role_at_least(get_node_role(node_id), ROLE_MOD)


def handle_who_command(sender_id, message, interface):
    """!WHO,,<node id> -- what role somebody has.

    Mod and up, because it reports on other people. A moderator deciding
    whether to act needs to see the current state first, and guessing at it
    from a ban that may not have applied is how you ban someone twice.
    """
    if not _role_commands_available(sender_id, interface):
        send_message(_role_command_refusal(sender_id, interface), sender_id, interface)
        return
    # Split on the separator and require the second half. Taking [-1] of a
    # split that never happened hands back the command word itself, so a
    # bare !WHO looked up a node called "who" and reported a role for it.
    parts = str(message or '').split(',,', 1)
    target = parts[1].strip() if len(parts) == 2 else ''
    if not target:
        send_message("Usage: !WHO,,<node id>", sender_id, interface)
        return
    role = get_node_role(target)
    account_id = get_account_id_for_node(target)
    where = "account" if account_id else "this device"
    send_message(f"{target}\nRole: {role} (set on {where})", sender_id, interface)


def handle_role_command(sender_id, message, interface, bbs_nodes=None):
    """!ROLE,,<node id>,,<role> -- assign a role from the BBS.

    Mod and Admin only, and a mod may not create another mod: promotion to
    the level that can promote stays with Admin, so a single compromised
    moderator cannot widen its own reach. Nobody may assign above their own
    rank for the same reason.
    """
    if not is_bbs_role_management_enabled():
        send_message("Role commands are turned off on this node.", sender_id, interface)
        return
    node_id = get_node_id_from_num(sender_id, interface)
    actor_role = get_node_role(node_id) if node_id else ROLE_UNREGISTERED
    if not role_at_least(actor_role, ROLE_MOD):
        send_message(_role_command_refusal(sender_id, interface), sender_id, interface)
        return

    parts = [p.strip() for p in str(message or '').split(',,')]
    if len(parts) < 3 or not parts[1] or not parts[2]:
        send_message("Usage: !ROLE,,<node id>,,<role>\n"
                     f"Roles: {', '.join(ASSIGNABLE_ROLES)}", sender_id, interface)
        return
    target, requested = parts[1], normalize_role(parts[2])
    if requested not in ASSIGNABLE_ROLES:
        send_message(f"Unknown role. Try: {', '.join(ASSIGNABLE_ROLES)}",
                     sender_id, interface)
        return

    # Checked first, and before the ceiling, so someone trying to promote
    # themselves is told the actual rule rather than being handed a limit
    # that reads like the only thing standing in the way.
    if str(target) == str(node_id):
        send_message("You cannot change your own role.", sender_id, interface)
        return
    # Never above your own rank, and a mod stops below mod: the power to
    # appoint is what turns one bad moderator into several.
    ceiling = ROLE_VIP if actor_role == ROLE_MOD else actor_role
    if role_rank(requested) > role_rank(ceiling):
        send_message(f"You can assign up to {ceiling}.", sender_id, interface)
        return
    target_role = get_node_role(target)
    if role_rank(target_role) > role_rank(actor_role):
        send_message("That person outranks you.", sender_id, interface)
        return

    if set_node_role(target, requested, assigned_by=str(node_id or 'bbs')):
        logging.warning("Role %r set for %s by %s over the BBS",
                        requested, target, node_id)
        send_message(f"{target} is now {requested}.", sender_id, interface)
        _announce_role_change(target, requested, interface, bbs_nodes)
    else:
        send_message("That role could not be set.", sender_id, interface)


def _announce_role_change(node_id, role, interface, bbs_nodes) -> None:
    """Push a role straight out rather than waiting for the next sync pass.

    A ban that takes an hour to reach the other nodes is most of an hour of
    the thing you banned them for.
    """
    try:
        if not bbs_nodes or not interface or not is_role_sync_enabled():
            return
        send_node_role_to_bbs_nodes(node_id, role, get_role_updated_at(node_id),
                                    bbs_nodes, interface)
    except Exception:
        logging.debug("could not announce a role change", exc_info=True)


def _can_moderate(sender_id, interface) -> bool:
    """Whether this session may remove other people's posts.

    Deliberately NOT gated on [roles] bbs_commands. That switch is about
    handing out authority over the radio; taking down a post is the job a
    moderator was appointed to do, and an operator who turns off role
    commands has not said they want moderation to stop.
    """
    node_id = get_node_id_from_num(sender_id, interface)
    return bool(node_id) and role_at_least(get_node_role(node_id), ROLE_MOD)


def handle_bulletin_moderate_steps(sender_id, message, interface, state, bbs_nodes=None):
    """A moderator's Delete on the post they are reading.

    Two steps rather than one. A delete here is tombstoned and travels to
    every node in the fleet, so the cost of a mistyped key is not local --
    and a radio user cannot undo it from the radio. One confirmation is two
    seconds of airtime against a post being removed everywhere by accident.
    """
    choice = str(message or '').strip().lower()
    board = state.get('board')
    boards = state.get('boards', [])

    if state.get('step') == 2:
        if choice in ('y', 'yes'):
            if not _can_moderate(sender_id, interface):
                # Re-checked at the point of action: a role can change
                # between reading a post and confirming the delete.
                send_message("That is for moderators.", sender_id, interface)
            else:
                delete_bulletin(state.get('unique_id'), bbs_nodes or [], interface)
                logging.warning("Bulletin %s deleted by %s over the BBS",
                                state.get('unique_id'),
                                get_node_id_from_num(sender_id, interface))
                send_message("Deleted. Peers drop their copies on the next sync.",
                             sender_id, interface)
        else:
            send_message("Left alone.", sender_id, interface)
        send_board_action_menu(sender_id, interface, board, boards)
        return

    if choice == 'd':
        if not _can_moderate(sender_id, interface):
            send_message("That is for moderators.", sender_id, interface)
            send_board_action_menu(sender_id, interface, board, boards)
            return
        subject = str(state.get('subject') or 'this post')
        send_message(f"Delete \"{subject}\" everywhere? [Y]es  [0]No",
                     sender_id, interface)
        state['step'] = 2
        update_user_state(sender_id, state)
        return

    send_board_action_menu(sender_id, interface, board, boards)


def handle_comment_moderate_steps(sender_id, message, interface, state, bbs_nodes=None):
    """A moderator's Delete on one comment of a channel post."""
    choice = str(message or '').strip().lower()
    channel_id = state.get('channel_id')
    comments = state.get('comments') or []

    if state.get('step') == 2:
        if choice in ('y', 'yes') and state.get('pending'):
            if not _can_moderate(sender_id, interface):
                send_message("That is for moderators.", sender_id, interface)
            else:
                delete_channel_comment(state['pending'], bbs_nodes or [], interface)
                logging.warning("Channel comment %s deleted by %s over the BBS",
                                state['pending'],
                                get_node_id_from_num(sender_id, interface))
                send_message("Deleted. Peers drop their copies on the next sync.",
                             sender_id, interface)
        else:
            send_message("Left alone.", sender_id, interface)
        _return_to_channel_post(sender_id, interface, state)
        return

    if choice in ('0', 'x'):
        # The prompt offers it, so it has to mean going back rather than
        # being parsed as index -1 and answered "no comment at that number".
        _return_to_channel_post(sender_id, interface, state)
        return
    if not choice.isdigit():
        send_message("Reply with a comment number, or 0 to go back.",
                     sender_id, interface)
        _return_to_channel_post(sender_id, interface, state)
        return
    index = int(choice) - 1
    if not 0 <= index < len(comments):
        send_message("No comment at that number.", sender_id, interface)
        _return_to_channel_post(sender_id, interface, state)
        return

    unique_id, who = comments[index]
    send_message(f"Delete the comment by {who} everywhere? [Y]es  [0]No",
                 sender_id, interface)
    state['step'] = 2
    state['pending'] = unique_id
    update_user_state(sender_id, state)


def _return_to_channel_post(sender_id, interface, state) -> None:
    send_message("[1]View comments [2]Comment [0]Exit", sender_id, interface)
    update_user_state(sender_id, {'command': 'CHANNEL_DIRECTORY', 'step': 6,
                                  'channel_id': state.get('channel_id')})


def _role_command_refusal(sender_id, interface) -> str:
    if not is_bbs_role_management_enabled():
        return "Role commands are turned off on this node."
    return "That command is for moderators."
