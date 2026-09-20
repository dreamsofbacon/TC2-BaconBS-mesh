"""API gateway: fulfills mesh API requests on an internet-connected node.

A requester node sends an APIREQ over the mesh; a gateway node (this module)
validates the requester against the node allow-list, performs the outbound call
(a door from ``services.py``, or an AI chat relay to an Ollama /
OpenAI-compatible endpoint such as Project Nomad), truncates the result for
LoRa, and hands it back to a transport-agnostic ``reply_fn(status, body)`` — which the caller wires
to either an APIRESP over the mesh or a direct DM when the gateway is local.

Safety: requester node-id allow-list, outbound host + scheme allow-list, SSRF
guard (no private/loopback targets), per-node rate limit, response size cap, and
hard request timeouts. Blocking I/O runs on a worker thread so the radio/main
loop (and its 180s watchdog) never blocks.
"""

import ipaddress
import json
import logging
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict, deque
from typing import Callable, Optional, Tuple

import services
from utils import _config_bool, _config_int, _config_raw
from db_operations import account_authorized

# Per-node sliding-window request timestamps, guarded by a lock (touched by the
# radio thread and worker threads).
_rate_lock = threading.Lock()
_recent_requests: dict = defaultdict(deque)


# ── Config ──────────────────────────────────────────────────────────────────

def is_gateway_enabled() -> bool:
    return _config_bool('gateway', 'enabled', False)


def _csv(section: str, option: str, default: str) -> list:
    raw = _config_raw(section, option)
    if raw is None or raw == '':
        raw = default
    return [x.strip() for x in str(raw).split(',') if x.strip()]


def _request_timeout() -> int:
    return _config_int('gateway', 'request_timeout', 20)


def _max_response_bytes() -> int:
    return _config_int('gateway', 'max_response_bytes', 800)


def _rate_limit_per_node() -> int:
    return _config_int('gateway', 'rate_limit_per_node', 5)


def allowed_hosts() -> list:
    """Sites this node will fetch for a raw ``kind='h'`` request. Empty = none.

    This is the old free-form Web Fetch allow-list. Nothing on the menu
    produces such a request any more -- doors carry their own vetted hosts
    (see services.py) -- but an older peer on the mesh can still send one,
    so the path stays, guarded exactly as it was. Empty remains the default,
    which means a raw fetch is refused unless an operator opted in.
    """
    return _csv('gateway', 'allowed_hosts', '')


def gateway_allowed_nodes() -> list:
    """Gateway-specific requester allow-list ([gateway] allowed_nodes). When set,
    it RESTRICTS who may use this gateway to exactly these node IDs — independent
    of the general [allow_list]. Empty = no gateway-specific restriction."""
    return _csv('gateway', 'allowed_nodes', '')


def is_requester_authorized(requester_id, fallback_allowed=None) -> bool:
    """Decide whether *requester_id* may use this gateway.

    - If [gateway] allowed_nodes is set, it is authoritative: only those nodes
      are allowed (lock-down mode, configurable from the web GUI).
    - Otherwise fall back to the general [allow_list] (fallback_allowed): any
      allowed node may use the gateway (the current open default).
    - If both are empty, the gateway is open to all.

    Account-aware: if requester_id isn't directly on the effective list but
    IS linked to a multi-device account (see db_operations.account_authorized)
    and a SIBLING node is on the list, the requester is authorized too. An
    unlinked node's behavior is completely unchanged."""
    gw = gateway_allowed_nodes()
    effective = gw if gw else list(fallback_allowed or [])
    if effective and not account_authorized(requester_id, [effective]):
        return False
    return True


# ── Validation / safety ──────────────────────────────────────────────────────

def _host_is_private(host: str) -> bool:
    """True if *host* resolves to any loopback/private/link-local address (SSRF guard)."""
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return True  # unresolvable → treat as unsafe
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return True
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return True
    return False


def validate_url(url: str) -> Tuple[bool, str]:
    """Check scheme + host against the gateway allow-lists and SSRF guard.
    Returns (ok, reason)."""
    try:
        p = urllib.parse.urlparse(url)
    except Exception:
        return False, "unparseable URL"
    scheme = (p.scheme or '').lower()
    host = (p.hostname or '').lower()
    if scheme not in _csv('gateway', 'allowed_schemes', 'https'):
        return False, f"scheme '{scheme}' not allowed"
    hosts = allowed_hosts()
    if not hosts:
        # Said in a user's words, not a config file's. This is the reply a
        # person gets for every URL on a node whose operator never set the
        # list, and naming the setting told them nothing they could act on.
        return False, "this node has not been allowed to fetch any sites"
    if host not in [h.lower() for h in hosts]:
        return False, f"'{host}' is not on this node's allowed list ({', '.join(hosts)})"
    if _host_is_private(host):
        return False, f"host '{host}' resolves to a private/loopback address"
    return True, ""


def _rate_ok(node_id: str) -> bool:
    limit = _rate_limit_per_node()
    if limit <= 0:
        return True
    now = time.time()
    with _rate_lock:
        dq = _recent_requests[node_id]
        while dq and now - dq[0] > 60.0:
            dq.popleft()
        if len(dq) >= limit:
            return False
        dq.append(now)
        return True


# ── Outbound calls ───────────────────────────────────────────────────────────

def _read_capped(resp) -> str:
    cap = _max_response_bytes()
    raw = resp.read(cap + 1)
    text = raw.decode('utf-8', errors='replace')
    if len(raw) > cap:
        text = text[:cap] + "…[truncated]"
    return text


def perform_http(method: str, url: str, body: str) -> Tuple[str, str]:
    """Generic HTTP proxy call. Returns (status, body_text)."""
    ok, reason = validate_url(url)
    if not ok:
        return "ERR", f"blocked: {reason}"
    method = (method or 'GET').upper()
    data = body.encode('utf-8') if (body and method in ('POST', 'PUT', 'PATCH')) else None
    req = urllib.request.Request(url, data=data, method=method)
    # Re-validate on redirect to prevent allow-list bypass.
    class _Redir(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            ok2, _ = validate_url(newurl)
            if not ok2:
                return None
            return super().redirect_request(req, fp, code, msg, headers, newurl)
    opener = urllib.request.build_opener(_Redir)
    try:
        with opener.open(req, timeout=_request_timeout()) as resp:
            return str(getattr(resp, 'status', 200)), _read_capped(resp)
    except urllib.error.HTTPError as e:
        return str(e.code), _read_capped(e)
    except Exception as e:
        return "ERR", f"request failed: {e}"


# Mapped rather than passed through raw: these are the failures an operator
# actually hits when pointing the relay at a new endpoint, and each one has a
# different fix.
_AI_STATUS_HINTS = {
    401: "endpoint requires authentication; set an API key in Settings > Gateway",
    403: "endpoint rejected the key; check the API key in Settings > Gateway",
    404: "no chat endpoint at that path; check the API dialect setting",
    429: "endpoint is rate limiting; try again shortly",
    500: "endpoint hit an internal error",
    502: "endpoint is up but its AI backend is down",
    503: "endpoint is unavailable; the model may still be loading",
    504: "endpoint timed out; the model may be too slow for the request timeout",
}


def _installed_ai_model_details(base: str, dialect: str, headers: dict, timeout=None):
    """Models the AI server has as {'name', 'size'}, or None if it cannot say.

    Nomad lists them at /api/ollama/installed-models (a bare list); Ollama at
    /api/tags ({"models": [...]}). Entries carry 'name' or 'model'.
    """
    path = "/api/ollama/installed-models" if dialect == 'nomad' else "/api/tags"
    try:
        req = urllib.request.Request(
            f"{base}{path}", headers={k: v for k, v in headers.items() if k != "Content-Type"},
            method='GET')
        with urllib.request.urlopen(
                req, timeout=timeout or min(10, _request_timeout())) as resp:
            doc = json.loads(resp.read().decode('utf-8', errors='replace'))
    except Exception:
        return None
    items = doc.get('models') if isinstance(doc, dict) else doc
    if not isinstance(items, list):
        return None
    found = []
    for item in items:
        if isinstance(item, dict):
            name = item.get('name') or item.get('model')
            size = item.get('size') or 0
        else:
            name, size = item, 0
        if name:
            try:
                size = int(size)
            except (TypeError, ValueError):
                size = 0
            found.append({'name': str(name), 'size': size})
    return found


def _installed_ai_models(base: str, dialect: str, headers: dict, timeout=None):
    """Just the names, for callers that do not care how big anything is."""
    found = _installed_ai_model_details(base, dialect, headers, timeout=timeout)
    return None if found is None else [item['name'] for item in found]


# The model answers for this BBS, so it has to use this BBS's words. Left to
# itself it calls the boards "forums" and the channels "threads", which are
# not things here and send people looking for menus that do not exist. This
# is only the fallback: an operator's own [gateway] ai_system_prompt wins.
DEFAULT_AI_SYSTEM_PROMPT = (
    "You are the assistant for a small off-grid mesh radio BBS, answering over "
    "a slow radio link. Be direct and conversational, and keep replies under "
    "170 characters. Use this BBS's own words for its features: Bulletins are "
    "public posts on boards, Channels are shared topics with comments, Mail is "
    "private between two people, and Public Chatter is radio traffic the node "
    "overheard. Never call any of them forums, threads or subreddits."
)


# Asking a question the AI server cannot answer costs a radio user two
# round trips to find out: type the question, wait, read the error. The AI
# server here is someone else's machine -- when it has no model installed,
# every question fails -- so the answer is to say so at the door instead.
#
# Deliberately fails OPEN: a slow or silent check leaves the feature offered,
# because being unable to ask is worse than a wasted question. Cached, so a
# busy screen does not re-ask the server for every visitor.
AI_AVAILABILITY_TTL_SECONDS = 300.0
AI_AVAILABILITY_TIMEOUT_SECONDS = 3.0
_ai_availability: dict = {}


def _reset_ai_availability() -> None:
    """Test hook, and what Settings calls after the model or URL changes."""
    _ai_availability.clear()
    _ai_model_choice.clear()


def ai_unavailable_reason() -> str:
    """Why Ask Nomad cannot work right now, or '' when it looks usable."""
    # Only this node's own AI server can be checked. A node whose gateway is
    # off, or which has no AI server of its own, may still be served by a
    # peer gateway ('apigw'), and there is no way to ask it from here -- so
    # neither case is reported as unavailable.
    if not is_gateway_enabled():
        return ''
    base = (_config_raw('gateway', 'ai_base_url') or '').rstrip('/')
    if not base:
        return ''
    dialect = (_config_raw('gateway', 'ai_dialect') or 'ollama').lower()
    key = (base, dialect, _configured_ai_model())
    cached = _ai_availability.get(key)
    now = time.time()
    if cached is not None and cached[0] > now:
        return cached[1]

    # Ask the resolver rather than the config: a configured model that is
    # missing is no longer a dead end, so reporting it as one would turn
    # people away from a feature that now works.
    model, _note = resolve_ai_model()
    reason = '' if model else "the AI server has no model installed"
    _ai_availability[key] = (now + AI_AVAILABILITY_TTL_SECONDS, reason)
    return reason


# Which model to actually ask for, which is not always the one in the config.
#
# The AI server belongs to someone else. They install and remove models
# without telling this node, and a name that was right last month answers
# every question with "model not installed" today -- a dead feature on the
# menu, and nothing on screen saying why. This asks the server what it has
# and uses something that exists.
#
# Order of preference:
#   1. the configured model, when the server actually has it;
#   2. the first name in [gateway] ai_model_fallbacks that the server has,
#      so an operator with an opinion gets their way;
#   3. the largest model installed, on the reasoning that a bigger model
#      gives a better answer and the wait is the same order of magnitude
#      either way over a radio.
#
# Cached, because it costs a request, and sticky within that window so a
# conversation does not switch models between one question and the next.
AI_MODEL_TTL_SECONDS = 300.0
_ai_model_choice: dict = {}


def _reset_ai_model_choice() -> None:
    """Test hook, and what Settings calls after the model or URL changes."""
    _ai_model_choice.clear()


def _configured_ai_model() -> str:
    """The operator's chosen model.

    Empty is NOT llama3.2. An empty value used to fall through to that
    default, which meant a blank box in Settings quietly became a request
    for a model nobody had installed -- and the error then named llama3.2,
    a model the operator had never typed, which sent them looking in the
    wrong place entirely.
    """
    return (_config_raw('gateway', 'ai_model') or '').strip()


def _rank_installed(found: list, preferred: list) -> list:
    """Installed models, best first: operator order, then largest."""
    wanted = [p.strip() for p in preferred if p.strip()]
    by_name = {item['name']: item for item in found}
    ranked = [by_name[name] for name in wanted if name in by_name]
    rest = sorted((item for item in found if item['name'] not in wanted),
                  key=lambda item: item['size'], reverse=True)
    return ranked + rest


def resolve_ai_model() -> Tuple[str, str]:
    """(model to ask for, why it is not the configured one).

    The second value is empty when nothing surprising happened. It exists so
    the reason can reach Settings and the log instead of being a silent
    substitution -- an operator who never learns their configured model is
    missing will keep reading that name back and believing it.
    """
    base = (_config_raw('gateway', 'ai_base_url') or '').rstrip('/')
    configured = _configured_ai_model()
    if not base:
        return configured, ''
    dialect = (_config_raw('gateway', 'ai_dialect') or 'ollama').lower()
    preferred = _csv('gateway', 'ai_model_fallbacks', '')
    key = (base, dialect, configured, tuple(preferred))
    cached = _ai_model_choice.get(key)
    now = time.time()
    if cached is not None and cached[0] > now:
        return cached[1], cached[2]

    headers = {}
    api_key = _config_raw('gateway', 'ai_api_key')
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        found = _installed_ai_model_details(
            base, dialect, headers, timeout=AI_AVAILABILITY_TIMEOUT_SECONDS)
    except Exception:
        found = None

    if found is None:
        # The server could not say. Fail open on the configured name: a
        # check that cannot answer must not be what stops someone asking.
        return configured, ''

    chosen, note = configured, ''
    names = [item['name'] for item in found]
    if not configured or configured not in names:
        ranked = _rank_installed(found, preferred)
        if ranked:
            chosen = ranked[0]['name']
            note = (f"no model is set, so this node is using '{chosen}'"
                    if not configured else
                    f"'{configured}' is not installed, so this node is "
                    f"using '{chosen}'")
            logging.warning("AI model fallback: %s (installed: %s)",
                            note, ", ".join(names[:6]) or "none")
        else:
            # No usable model at all. Returning the configured name here
            # would read as "this will work" to every caller that checks.
            chosen, note = '', "the AI server has no model installed"

    _ai_model_choice[key] = (now + AI_MODEL_TTL_SECONDS, chosen, note)
    return chosen, note


def perform_ai_chat(prompt: str) -> Tuple[str, str]:
    """Relay a prompt to the configured Ollama / OpenAI-compatible chat endpoint."""
    base = (_config_raw('gateway', 'ai_base_url') or '').rstrip('/')
    if not base:
        return "ERR", "AI relay not configured (ai_base_url)"
    dialect = (_config_raw('gateway', 'ai_dialect') or 'ollama').lower()
    model, _ = resolve_ai_model()
    if not model:
        return "ERR", ("no AI model is set for this node, and the AI server "
                       "offered none to fall back on")
    system = _config_raw('gateway', 'ai_system_prompt') or DEFAULT_AI_SYSTEM_PROMPT
    messages = ([{"role": "system", "content": system}] if system else []) + \
               [{"role": "user", "content": prompt}]
    headers = {"Content-Type": "application/json"}
    if dialect == 'openai':
        url = f"{base}/v1/chat/completions"
        payload = {"model": model, "messages": messages}
        key = _config_raw('gateway', 'ai_api_key')
        if key:
            headers["Authorization"] = f"Bearer {key}"
    elif dialect == 'nomad':
        # Project N.O.M.A.D proxies Ollama under its own /api/ollama/ prefix;
        # response shape is identical to Ollama ({message:{content}}).
        url = f"{base}/api/ollama/chat"
        payload = {"model": model, "messages": messages, "stream": False}
        key = _config_raw('gateway', 'ai_api_key')
        if key:
            headers["Authorization"] = f"Bearer {key}"
    else:  # ollama
        url = f"{base}/api/chat"
        payload = {"model": model, "messages": messages, "stream": False}
    try:
        req = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'),
                                     headers=headers, method='POST')
        with urllib.request.urlopen(req, timeout=_request_timeout()) as resp:
            doc = json.loads(resp.read().decode('utf-8', errors='replace'))
        if dialect == 'openai':
            reply = doc['choices'][0]['message']['content']
        else:
            reply = doc['message']['content']
        cap = _max_response_bytes()
        reply = str(reply).strip()
        if not reply:
            # An empty answer used to be relayed as a successful "200",
            # and a zero-length body sends nothing at all -- the user got
            # the follow-up prompt with no answer above it and no error to
            # explain the gap. Ollama returns this when it served the
            # request only to load the model, so it is a normal thing to
            # hit on the first question after the model is evicted.
            reason = doc.get('done_reason') or doc.get('finish_reason') or ''
            logging.warning(
                "AI endpoint returned an empty answer (model=%s, done_reason=%r)",
                model, reason)
            detail = f" (done_reason: {reason})" if reason else ""
            return "ERR", ("AI endpoint returned an empty answer" + detail
                           + " — the model may still be loading; ask again")
        if len(reply.encode('utf-8')) > cap:
            reply = reply.encode('utf-8')[:cap].decode('utf-8', errors='ignore') + "…[truncated]"
        return "200", reply
    except urllib.error.HTTPError as e:
        if e.code == 404 and dialect in ('nomad', 'ollama'):
            # Ollama answers 404 for a model it does not have, and Nomad
            # passes that straight through -- as a web page, so it read as a
            # wrong path. The live Nomad had the right path and no models at
            # all. Ask the server which models it has before blaming the path.
            installed = _installed_ai_models(base, dialect, headers)
            if installed is not None and model not in installed:
                have = ", ".join(installed[:4]) if installed else "none"
                return "ERR", (f"AI model '{model}' is not installed on the AI server "
                               f"(installed: {have}). Install it there, or set the "
                               f"model in Settings > Gateway")
        # A bare "HTTP Error 403" over a mesh radio is a dead end -- the user
        # can't open devtools. Name the likely cause instead.
        hint = _AI_STATUS_HINTS.get(e.code)
        return "ERR", f"AI endpoint returned {e.code}" + (f" -- {hint}" if hint else "")
    except Exception as e:
        return "ERR", f"AI request failed: {e}"


# ── Request dispatch ─────────────────────────────────────────────────────────

def handle_apireq(rid: str, requester_id: str, kind: str, payload: str,
                  allowed_nodes, reply_fn: Callable[[str, str], None]) -> None:
    """Validate + dispatch an API request on a worker thread.

    ``reply_fn(status, body)`` is transport-agnostic — the caller wires it to an
    APIRESP over the mesh or a local DM. Returns immediately; the call runs off
    the radio/main thread.
    """
    if not is_requester_authorized(requester_id, allowed_nodes):
        reply_fn("ERR", "not authorized to use this gateway")
        return
    if not _rate_ok(requester_id):
        reply_fn("ERR", "rate limit exceeded, try again shortly")
        return

    def _worker():
        try:
            US = "\x1f"
            if kind == 'd':  # door -- a curated text service
                door_id, _, arg = payload.partition(US)
                status, result = services.run_door(door_id.strip(), arg)
            elif kind == 'r':  # relay
                target, _, body = payload.partition(US)
                if target.strip().lower() == 'ai':
                    status, result = perform_ai_chat(body)
                else:
                    status, result = "ERR", f"unknown relay target '{target}'"
            else:  # http
                parts = payload.split(US, 2)
                method = parts[0] if len(parts) > 0 else 'GET'
                url = parts[1] if len(parts) > 1 else ''
                hbody = parts[2] if len(parts) > 2 else ''
                status, result = perform_http(method, url, hbody)
        except Exception as e:
            status, result = "ERR", f"gateway error: {e}"
        try:
            reply_fn(status, result)
        except Exception as e:
            logging.warning(f"gateway reply_fn failed for rid={rid}: {e}")

    threading.Thread(target=_worker, name=f"apigw-{rid}", daemon=True).start()
