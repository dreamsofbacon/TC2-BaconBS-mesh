import configparser
import logging
import os
import random
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
)
from utils import (
    get_node_id_from_num, get_node_info,
    get_node_short_name, get_user_state, get_zork_save_sync_notice, send_message,
    update_user_state,
    select_gateway_peer, send_api_request, register_api_request,
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

# Ordered list of playable games (matches GAMES keys in zork_port)
GAME_LIST = list(GAMES.items())  # [(game_id, {name, ...}), ...]

# Read the configuration for menu options
config = configparser.ConfigParser()
config.read('config.ini')


def _parse_menu_items(value: str) -> list[str]:
    return [item.strip().upper() for item in value.split(',') if item.strip()]


main_menu_items = _parse_menu_items(config.get('menu', 'main_menu_items', fallback='Q,B,U,P,X'))
bbs_menu_items = _parse_menu_items(config.get('menu', 'bbs_menu_items', fallback='M,B,C,J,X'))
utilities_menu_items = _parse_menu_items(config.get('menu', 'utilities_menu_items', fallback='S,F,W,G,X'))

if 'G' not in utilities_menu_items and 'Z' not in utilities_menu_items:
    if 'X' in utilities_menu_items:
        exit_index = utilities_menu_items.index('X')
        utilities_menu_items.insert(exit_index, 'G')
    else:
        utilities_menu_items.append('G')
# Treat legacy Z config entry as G
if 'Z' in utilities_menu_items and 'G' not in utilities_menu_items:
    utilities_menu_items[utilities_menu_items.index('Z')] = 'G'


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


def build_menu(items, menu_name):
    menu_items = [item.strip().upper() for item in items if item and item.strip()]
    if menu_name == "🛠️Utilities Menu🛠️":
        # Ensure G is present; migrate legacy Z
        if 'Z' in menu_items and 'G' not in menu_items:
            menu_items[menu_items.index('Z')] = 'G'
        if 'G' not in menu_items:
            if 'X' in menu_items:
                menu_items.insert(menu_items.index('X'), 'G')
            else:
                menu_items.append('G')

        number_map = {
            'S': "[1] Stats",
            'F': "[2] Fortune",
            'W': "[3] Wall of Shame",
            'G': "[4] Games",
            'A': "[5] API Gateway",
            'X': "[0] Exit",
        }
        menu_str = f"{menu_name}\n"
        for item in menu_items:
            if item in number_map:
                menu_str += number_map[item] + "\n"
        return menu_str

    if menu_name == "📰BBS Menu📰":
        number_map = {
            'M': "[1] Mail",
            'B': "[2] Bulletins",
            'C': "[3] Channel Dir",
            'J': "[4] JS8CALL",
            'X': "[0] Exit",
        }
    else:
        # Main menu
        number_map = {
            'Q': "[1] Quick Commands",
            'B': "[2] BBS",
            'U': "[3] Utilities",
            'P': "[4] Profile",
            'X': "[0] Exit",
        }
    menu_str = f"{menu_name}\n"
    for item in menu_items:
        if item in number_map:
            menu_str += number_map[item] + "\n"
    return menu_str

def handle_help_command(sender_id, interface, menu_name=None):
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
    send_message(response, sender_id, interface)


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


def handle_mail_command(sender_id, interface):
    response = "✉️Mail Menu✉️\nWhat would you like to do with mail?\n[1]Read [2]Send [0]Exit"
    send_message(response, sender_id, interface)
    update_user_state(sender_id, {'command': 'MAIL', 'step': 1})



def handle_bulletin_command(sender_id, interface):
    boards = get_bulletin_boards()
    board_options = "\n".join([f"[{index}] {board}" for index, board in enumerate(boards, start=1)])
    response = (
        f"📰Bulletin Menu📰\nWhich board would you like to enter?\n{board_options}"
        "\nReply with board number, name, or first letter.\nE[X]IT"
    )
    send_message(response, sender_id, interface)
    update_user_state(sender_id, {'command': 'BULLETIN_MENU', 'step': 1, 'boards': boards})


def handle_exit_command(sender_id, interface):
    send_message("Type 'HELP' for a list of commands.", sender_id, interface)
    update_user_state(sender_id, None)


def handle_stats_command(sender_id, interface):
    response = "📊Stats Menu📊\nWhat stats would you like to view?\n[1]Nodes [2]Hardware [3]Roles [0]Exit"
    send_message(response, sender_id, interface)
    update_user_state(sender_id, {'command': 'STATS', 'step': 1})


def handle_fortune_command(sender_id, interface):
    try:
        with open('fortunes.txt', 'r') as file:
            fortunes = file.readlines()
        if not fortunes:
            send_message("No fortunes available.", sender_id, interface)
            return
        fortune = random.choice(fortunes).strip()
        decorated_fortune = f"🔮 {fortune} 🔮"
        send_message(decorated_fortune, sender_id, interface)
    except Exception as e:
        send_message(f"Error generating fortune: {e}", sender_id, interface)


def handle_games_command(sender_id, interface):
    menu = "🎮 Games 🎮\n"
    for i, (game_id, info) in enumerate(GAME_LIST, start=1):
        menu += f"[{i}] {info['name']}\n"
    menu += "[S]cores [H]all of Fame [0]Exit"
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
    """Reuse the urgent-board allow-list: only listed nodes may use a gateway.
    Empty allow-list = open (matches existing allow_list semantics)."""
    node_id = get_node_id_from_num(sender_id, interface)
    allowed = getattr(interface, 'allowed_nodes', None)
    if allowed and node_id not in allowed:
        return False
    return True


def handle_apigw_command(sender_id, interface):
    if not _apigw_authorized(sender_id, interface):
        send_message("API gateway: your node is not on the allow-list.", sender_id, interface)
        handle_help_command(sender_id, interface, 'utilities')
        return
    menu = ("🌐 API Gateway\n[1] Ask Project Nomad\n[2] HTTP GET (allowed hosts)\n[0] Exit\n"
            "Send a number:")
    send_message(menu, sender_id, interface)
    update_user_state(sender_id, {'command': 'APIGW', 'step': 1})


def _apigw_submit(sender_id, interface, kind, payload, label):
    """Dispatch a composed request: fulfill locally if this node is a gateway,
    otherwise forward to a gateway peer. Response returns asynchronously."""
    import gateway
    import uuid as _uuid
    node_id = get_node_id_from_num(sender_id, interface)
    rid = _uuid.uuid4().hex[:6]

    if gateway.is_gateway_enabled():
        # Local fast path: no mesh round-trip; DM the result straight back.
        def _reply(status, body):
            prefix = "" if str(status) in ("200", "OK") else f"[{status}] "
            send_message(f"{prefix}{body}", sender_id, interface)
        gateway.handle_apireq(rid, node_id, kind, payload,
                              getattr(interface, 'allowed_nodes', None), _reply)
        send_message(f"Asked {label}… reply will arrive shortly.", sender_id, interface)
        update_user_state(sender_id, None)
        return

    peer = select_gateway_peer(interface)
    if not peer:
        send_message("No internet gateway is reachable on the mesh right now.", sender_id, interface)
        update_user_state(sender_id, None)
        return
    register_api_request(rid, sender_id)
    if not send_api_request(rid, node_id, kind, payload, peer, interface):
        from utils import pop_api_request
        pop_api_request(rid)
        send_message("Request too long for one packet — please shorten it.", sender_id, interface)
        update_user_state(sender_id, None)
        return
    send_message(f"Sent to gateway {peer} via {label}… waiting for reply.", sender_id, interface)
    update_user_state(sender_id, None)  # response/timeout delivered asynchronously


def handle_apigw_steps(sender_id, message, interface):
    choice = message.strip()
    state = get_user_state(sender_id) or {}
    step = state.get('step', 1)
    if choice.lower() in ('x', '0', 'exit'):
        handle_help_command(sender_id, interface, 'utilities')
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
    US = "\x1f"
    mode = state.get('mode', 'ai')
    if not choice:
        send_message("Empty input — cancelled.", sender_id, interface)
        handle_help_command(sender_id, interface, 'utilities')
        return
    if mode == 'ai':
        _apigw_submit(sender_id, interface, 'r', f"ai{US}{choice}", "Project Nomad")
    else:
        _apigw_submit(sender_id, interface, 'h', f"GET{US}{choice}{US}", "HTTP")


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


def handle_profile_command(sender_id, interface):
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
    lines.append("[1]Edit Bio [0]Back")
    send_message("\n".join(lines), sender_id, interface)
    update_user_state(sender_id, {'command': 'PROFILE', 'step': 1})


def handle_profile_steps(sender_id, message, interface):
    state = get_user_state(sender_id) or {}
    choice = message.strip()
    if state.get('step') == 2:
        bio = choice[:100]
        update_user_bio(sender_id, bio)
        send_message("Bio updated!", sender_id, interface)
        handle_profile_command(sender_id, interface)
        return
    if choice.lower() in ('0', 'x', 'back', 'exit'):
        handle_help_command(sender_id, interface)
        return
    if choice.lower() in ('e', '1'):
        send_message("Enter your bio (max 100 chars):", sender_id, interface)
        update_user_state(sender_id, {'command': 'PROFILE', 'step': 2})
        return
    handle_profile_command(sender_id, interface)


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

        board_name = boards[board_index]
        bulletins = get_bulletins(board_name)
        response = f"{board_name} has {len(bulletins)} messages.\n[1]Read [2]Post [0]Exit"
        send_message(response, sender_id, interface)
        update_user_state(sender_id, {'command': 'BULLETIN_ACTION', 'step': 2, 'board': board_name, 'boards': boards})

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
                allowed_nodes = interface.allowed_nodes
                logging.info(f"Checking permissions for node_id: {node_id} with allowed_nodes: {allowed_nodes}")  # Debug statement
                if allowed_nodes and node_id not in allowed_nodes:
                    send_message("You don't have permission to post to this board.", sender_id, interface)
                    handle_bb_steps(sender_id, 'e', 1, state, interface, bbs_nodes)
                    return
            send_message("What is the subject of your bulletin? Keep it short.", sender_id, interface)
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
        subject = message
        send_message("Send the contents of your bulletin. Send a message with END when finished.", sender_id, interface)
        update_user_state(sender_id, {'command': 'BULLETIN_POST_CONTENT', 'step': 5, 'board': state['board'], 'subject': subject, 'content': ''})

    elif step == 5:
        if message.lower() == "end":
            board = state['board']
            subject = state['subject']
            content = state['content']
            node_id = get_node_id_from_num(sender_id, interface)
            node_info = interface.nodes.get(node_id)
            if node_info is None:
                send_message("Error: Unable to retrieve your node information.", sender_id, interface)
                update_user_state(sender_id, None)
                return
            sender_short_name = node_info['user'].get('shortName', f"Node {sender_id}")
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

    if step == 1:
        choice = message.lower()
        _mail_step1_alias = {'1': 'r', '2': 's', '0': 'x'}
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
                update_user_state(sender_id, None)
        elif choice == 's':
            send_message("What is the Short Name of the node you want to leave a message for?", sender_id, interface)
            update_user_state(sender_id, {'command': 'MAIL', 'step': 3})
        elif choice == 'x':
            handle_help_command(sender_id, interface)

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
        short_name = message.lower()
        nodes = get_node_info(interface, short_name)
        if not nodes:
            send_message("I'm unable to find that node in my database.", sender_id, interface)
            handle_mail_command(sender_id, interface)
        elif len(nodes) == 1:
            recipient_id = nodes[0]['num']
            recipient_name = get_node_name(recipient_id, interface)
            send_message(f"What is the subject of your message to {recipient_name}?\nKeep it short.", sender_id, interface)
            update_user_state(sender_id, {'command': 'MAIL', 'step': 5, 'recipient_id': recipient_id})
        else:
            send_message("There are multiple nodes with that short name. Which one would you like to leave a message for?", sender_id, interface)
            for i, node in enumerate(nodes):
                send_message(f"[{i}] {node['longName']}", sender_id, interface)
            update_user_state(sender_id, {'command': 'MAIL', 'step': 6, 'nodes': nodes})

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
        send_message("Send your message. You can send it in multiple messages if it's too long for one.\nSend a single message with END when you're done", sender_id, interface)
        update_user_state(sender_id, {'command': 'MAIL', 'step': 7, 'recipient_id': state['recipient_id'], 'subject': subject, 'content': ''})

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
            subject = state['subject']
            content = state['content']
            recipient_name = get_node_name(recipient_id, interface)

            sender_short_name = get_node_short_name(get_node_id_from_num(sender_id, interface), interface)
            unique_id = add_mail(get_node_id_from_num(sender_id, interface), sender_short_name, recipient_id, subject, content, bbs_nodes, interface)
            send_message(f"Mail has been posted to the mailbox of {recipient_name}.\n(╯°□°)╯📨📬", sender_id, interface)

            notification_message = f"You have a new mail message from {sender_short_name}. Check your mailbox by responding to this message with CM."
            send_message(notification_message, recipient_id, interface)

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


def handle_channel_directory_command(sender_id, interface):
    response = "📚CHANNEL DIRECTORY📚\nWhat would you like to do?\n[1]View [2]Post [0]Exit"
    send_message(response, sender_id, interface)
    update_user_state(sender_id, {'command': 'CHANNEL_DIRECTORY', 'step': 1})


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
            categories = get_channel_categories()
            if categories:
                response = "Select a channel category to view:\n" + "\n".join(
                    [f"[{i}] {category[0]} ({category[1]} post{'s' if category[1] != 1 else ''})" for i, category in enumerate(categories)])
                send_message(response, sender_id, interface)
                update_user_state(sender_id, {'command': 'CHANNEL_DIRECTORY', 'step': 2, 'categories': categories})
            else:
                send_message("No channels available in the directory.", sender_id, interface)
                handle_channel_directory_command(sender_id, interface)
        elif choice == 'p':
            send_message("Name your channel for the directory:", sender_id, interface)
            update_user_state(sender_id, {'command': 'CHANNEL_DIRECTORY', 'step': 3})

    elif step == 2:
        try:
            category_index = int(message)
        except ValueError:
            send_message("Invalid selection. Please try again.", sender_id, interface)
            return
        categories = state.get('categories', [])
        if 0 <= category_index < len(categories):
            channel_name = categories[category_index][0]
            posts = get_channels_by_name(channel_name)
            if posts:
                post_lines = []
                for i, post in enumerate(posts):
                    post_id = post[0]
                    comments = get_channel_comments(post_id)
                    if comments:
                        latest_commenter = comments[0][1]
                        post_lines.append(f"[{i}] {latest_commenter}")
                    else:
                        post_lines.append(f"[{i}] No comments yet")
                response = f"{channel_name} posts:\n" + "\n".join(post_lines)
                send_message(response, sender_id, interface)
                update_user_state(sender_id, {'command': 'CHANNEL_DIRECTORY', 'step': 5, 'posts': posts, 'channel_name': channel_name})
                return
            send_message("No posts found in that category.", sender_id, interface)
        else:
            send_message("Invalid selection. Please try again.", sender_id, interface)
        handle_channel_directory_command(sender_id, interface)

    elif step == 5:
        try:
            post_index = int(message)
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
                node_short_name = get_node_short_name(get_node_id_from_num(sender_id, interface), interface) or "Unknown"
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
        channel_name = message
        send_message("Send a message with your channel URL or PSK:", sender_id, interface)
        update_user_state(sender_id, {'command': 'CHANNEL_DIRECTORY', 'step': 4, 'channel_name': channel_name})

    elif step == 4:
        channel_url = message
        channel_name = state['channel_name']
        add_channel(channel_name, channel_url, interface.bbs_nodes, interface)
        send_message(f"Your channel '{channel_name}' has been added to the directory.", sender_id, interface)
        handle_channel_directory_command(sender_id, interface)


def handle_send_mail_command(sender_id, message, interface, bbs_nodes):
    try:
        parts = message.split(",,", 3)
        if len(parts) != 4:
            send_message("Send Mail Quick Command format:\nSM,,{short_name},,{subject},,{message}", sender_id, interface)
            return

        _, short_name, subject, content = parts
        nodes = get_node_info(interface, short_name.lower())
        if not nodes:
            send_message(f"Node with short name '{short_name}' not found.", sender_id, interface)
            return
        if len(nodes) > 1:
            send_message(f"Multiple nodes with short name '{short_name}' found. Please be more specific.", sender_id,
                         interface)
            return

        recipient_id = nodes[0]['num']
        recipient_name = get_node_name(recipient_id, interface)
        sender_short_name = get_node_short_name(get_node_id_from_num(sender_id, interface), interface)

        unique_id = add_mail(get_node_id_from_num(sender_id, interface), sender_short_name, recipient_id, subject,
                             content, bbs_nodes, interface)
        send_message(f"Mail has been sent to {recipient_name}.", sender_id, interface)

        notification_message = f"You have a new mail message from {sender_short_name}. Check your mailbox by responding to this message with CM."
        send_message(notification_message, recipient_id, interface)

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
            send_message("Post Bulletin Quick Command format:\nPB,,{board_name},,{subject},,{content}", sender_id, interface)
            return

        _, board_name, subject, content = parts
        sender_short_name = get_node_short_name(get_node_id_from_num(sender_id, interface), interface)

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
            send_message("Check Bulletins Quick Command format:\nCB,,board_name", sender_id, interface)
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
            send_message("Post Channel Quick Command format:\nCHP,,{channel_name},,{channel_url}", sender_id, interface)
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
    response = ("✈️QUICK COMMANDS✈️\nSend command below for usage info:\nSM,, - Send "
                "Mail\nCM - Check Mail\nPB,, - Post Bulletin\nCB,, - Check Bulletins\n")
    send_message(response, sender_id, interface)
