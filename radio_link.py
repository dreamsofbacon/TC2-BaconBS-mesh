"""RadioLink: per-radio mutable state for the (optionally dual-radio) main loop.

server.py's main loop iterates a list of one or two RadioLinks -- one per
active radio interface. In single-radio deployments (the default, and 100%
of deployments before dual-radio bridge mode existed) there is exactly one
link and behavior is unchanged from before this module existed.

In bridge mode there are two: a 'primary' and a 'secondary' link, each with
its own interface, its own bbs_nodes/allowed_nodes/subscriber_nodes (read
from separate config sections -- see config_init.py), and its own sync
bookkeeping (phase-completion sets, pending-sync tracking, reconnect state).
Keeping this state per-link rather than as module globals is what lets one
radio's outage or reconnect-retry cycle proceed without blocking the other
radio's sync cycle -- see server.py's _run_link_tick / _reconnect_link.

Content itself is never routed/relayed between links directly. Both links
read and write the same shared SQLite database; a record synced in via one
radio's interface is picked up by the other radio's own independent sync
loop the next time its mismatch check runs (see db_operations.add_bulletin
and the PHASE 2 discussion in the project plan). RadioLink only tracks the
bookkeeping needed to run that per-interface sync loop concurrently.
"""

from dataclasses import dataclass, field
import threading
import time
from typing import Any, Callable, Optional


@dataclass
class RadioLink:
    name: str                      # 'primary' | 'secondary' | 'mqttN' -- used in logs/diagnostics
    interface: Any
    sync_section: str = 'sync'             # config section holding bbs_nodes/subscriber_nodes
    allow_section: str = 'allow_list'      # config section holding allowed_nodes

    # system_config dict keys refresh_peer_lists_from_config() writes the
    # parsed lists to -- 'bbs_nodes'/'allowed_nodes'/'subscriber_nodes' for
    # the primary link (unchanged from pre-dual-radio behavior) or the '2'
    # / '_mqttN' variants for the secondary/MQTT links, so links never
    # clobber each other.
    bbs_nodes_key: str = 'bbs_nodes'
    allowed_nodes_key: str = 'allowed_nodes'
    subscriber_nodes_key: str = 'subscriber_nodes'

    # Rebuilds this link's interface from system_config alone: (system_config)
    # -> new_interface_or_None. server.py's main() sets this per link
    # (get_interface / get_secondary_interface / a per-name MQTT closure) so
    # _reconnect_link never has to special-case by link name -- that
    # special-casing broke the moment a third link (the first MQTT link)
    # existed. Defaults to None only so tests can construct a bare RadioLink
    # without wiring reconnect support; _reconnect_link falls back to the
    # old name-based behavior in that case.
    reconnect_fn: Optional[Callable[[dict], Any]] = None

    # Set by server.py's liveness check; cleared once a dedicated reconnect
    # thread picks it up. `reconnecting` is true for the duration of that
    # thread's retry-with-backoff loop so the main tick can skip sync work
    # for this link (its interface is dead) without skipping the OTHER link.
    reconnect_needed: threading.Event = field(default_factory=threading.Event)
    reconnecting: bool = False

    # Five-phase sync progress, mirrors what used to be main()-local sets.
    mail_synced_nodes: set = field(default_factory=set)
    bulletins_synced_nodes: set = field(default_factory=set)
    channels_synced_nodes: set = field(default_factory=set)
    profiles_synced_nodes: set = field(default_factory=set)
    game_synced_nodes: set = field(default_factory=set)
    synced_nodes: set = field(default_factory=set)          # P1+P2 both complete
    pending_sync_nodes: set = field(default_factory=set)
    syncstate_advertisement_cache: dict = field(default_factory=dict)

    last_schedule_epoch: float = 0.0
    next_node_sync_check: float = 0.0
    next_incomplete_repair: float = 0.0
    next_api_poll: float = 0.0
    incomplete_attempts: dict = field(default_factory=dict)

    # Diagnostic only (NOT used by the process-wide watchdog -- see server.py
    # comment on _last_main_loop_tick for why blocking anywhere in a link's
    # tick already trips that watchdog without needing per-link tracking).
    # Useful for a per-radio "still ticking?" signal in tests/web admin.
    last_tick: float = field(default_factory=time.time)

    def bump_tick(self) -> None:
        self.last_tick = time.time()

    @property
    def bbs_nodes(self) -> list:
        return list(getattr(self.interface, 'bbs_nodes', []) or [])

    @property
    def allowed_nodes(self) -> list:
        return list(getattr(self.interface, 'allowed_nodes', []) or [])

    @property
    def subscriber_nodes(self) -> list:
        return list(getattr(self.interface, 'subscriber_nodes', []) or [])

    @property
    def protocol_name(self) -> str:
        return str(getattr(self.interface, 'protocol_name', 'Meshtastic'))

    @property
    def network_key(self) -> str:
        """Which network this link's nodes live on -- used as a coarse
        fallback (after peer-list membership, see server._link_for_node) to
        route peer-specific admin actions to the correct link.

        'meshcore'/'meshtastic' preserve the exact prior behavior (anything
        not literally 'meshcore' used to collapse to 'meshtastic', before
        any transport but those two existed). MQTT links instead get a
        link-specific key derived from protocol_name (e.g. 'mqtt:mqtt1')
        rather than a single shared 'mqtt' bucket -- two simultaneous MQTT
        links reporting the same generic bucket would let a message meant
        for one peer wrongly match the other link. Genuine disambiguation
        between same-protocol links comes from peer-list membership, not
        this property; this is only the last-resort fallback for a peer id
        no active link's lists contain yet.
        """
        name = self.protocol_name.strip().lower()
        if name == 'meshcore':
            return 'meshcore'
        if name.startswith('mqtt'):
            return name
        return 'meshtastic'
