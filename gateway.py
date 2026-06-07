"""API gateway: fulfills mesh API requests on an internet-connected node.

A requester node sends an APIREQ over the mesh; a gateway node (this module)
validates the requester against the node allow-list, performs the outbound call
(generic HTTP proxy, or an AI chat relay to an Ollama / OpenAI-compatible
endpoint such as Project Nomad), truncates the result for LoRa, and hands it
back to a transport-agnostic ``reply_fn(status, body)`` — which the caller wires
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

from utils import _config_bool, _config_int, _config_raw

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
    allowed_hosts = _csv('gateway', 'allowed_hosts', '')
    if not allowed_hosts:
        return False, "no allowed_hosts configured"
    if host not in [h.lower() for h in allowed_hosts]:
        return False, f"host '{host}' not in allow-list"
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


def perform_ai_chat(prompt: str) -> Tuple[str, str]:
    """Relay a prompt to the configured Ollama / OpenAI-compatible chat endpoint."""
    base = (_config_raw('gateway', 'ai_base_url') or '').rstrip('/')
    if not base:
        return "ERR", "AI relay not configured (ai_base_url)"
    dialect = (_config_raw('gateway', 'ai_dialect') or 'ollama').lower()
    model = _config_raw('gateway', 'ai_model') or 'llama3.2'
    system = _config_raw('gateway', 'ai_system_prompt') or ''
    messages = ([{"role": "system", "content": system}] if system else []) + \
               [{"role": "user", "content": prompt}]
    headers = {"Content-Type": "application/json"}
    if dialect == 'openai':
        url = f"{base}/v1/chat/completions"
        payload = {"model": model, "messages": messages}
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
        if len(reply.encode('utf-8')) > cap:
            reply = reply.encode('utf-8')[:cap].decode('utf-8', errors='ignore') + "…[truncated]"
        return "200", reply
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
    if allowed_nodes and requester_id not in allowed_nodes:
        reply_fn("ERR", "not authorized to use this gateway")
        return
    if not _rate_ok(requester_id):
        reply_fn("ERR", "rate limit exceeded, try again shortly")
        return

    def _worker():
        try:
            US = "\x1f"
            if kind == 'r':  # relay
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
