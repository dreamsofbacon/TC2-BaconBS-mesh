# Radios and public chatter

Open **More → Radios** to inspect the radios attached to this BBS. Each panel identifies the network and local slot, connection state, channel names, contacts or known nodes, and supported identity controls. The old `/system/meshtastic` address still opens Radios.

A configured connection is not necessarily connected. **Configured** means the web service has no recent report from the BBS. **Unavailable** means the BBS has no usable connection, and **reconnecting** means recovery is in progress. Controls require a current connected report. One radio may recover while the other remains usable.

## Setup

Keep your existing `config.ini`; upgrades do not require recreating accounts or the database. In **Settings → Devices**, choose the primary connection and, optionally, enable the secondary connection:

| Installation | Primary | Secondary |
| --- | --- | --- |
| Meshtastic only | Serial or TCP | Disabled |
| MeshCore only | MeshCore serial, TCP, or BLE | Disabled |
| Both networks | Either network | The other network |
| MQTT only | None | Disabled |

Set the address or serial port for each connection. Each radio keeps its own peer and allow lists (`sync`/`allow_list` and `sync2`/`allow_list2`). Save settings and restart the BBS for connection changes when the settings page requests it. MeshCore retains its existing Python 3.10+ requirement; installations without MeshCore retain existing dependency markers.

The running BBS owns both connections. Leave it running while using Radios. The web service uses a local command mailbox, so it does not open a competing serial, TCP, or BLE connection. Core controls and these pages work offline; optional external clients are not required.

## Sending

Open **Public Chatter → Send to a local radio channel**, or use the composer on Radios. Explicitly choose a local radio and then one of its reported channels. The form counts UTF-8 bytes, including multibyte characters such as emoji. Oversized messages are rejected rather than split or truncated.

History can include observations from remote BBS nodes. Those observations are not send targets. Selecting a history filter never selects a transmission destination. Equal channel numbers on different receiving radios are kept separate in the web filters.

The server validates the selection again before dispatch. A replaced connection or changed channel invalidates the old selection; refresh the page and select again. A disconnected destination never falls back to the other radio.

- **Queued:** the local BBS has not claimed the request yet. Requests expire after 60 seconds if unclaimed.
- **Dispatching:** the BBS has claimed it. Do not repeat the action.
- **Submitted:** the radio library accepted the message or setting request. This does not confirm reception by listeners.
- **Confirmed:** the radio acknowledged a supported control operation (not public-message delivery).
- **Failed / expired:** validation failed or the request expired before dispatch.
- **Unknown:** the result is uncertain. The BBS never automatically retries an uncertain operation. Check the radio before composing a new send.

Repeat submissions with the same operation ID return the existing operation. A crash after dispatch starts cannot replay that operation. Use the status-check button if a browser request fails.

## Supported controls

Both networks report cached channel and contact/node information and support local channel messaging. Meshtastic supports its long display name; MeshCore supports its display name, contact removal, and refreshing contacts/channels. Unsupported operations are labelled explicitly. Meshtastic name changes report submission rather than claiming an acknowledgement its API does not provide. Existing licensed status is preserved when changing its name.

Actual channel names take precedence. A known MeshCore Public channel is displayed as **#Public**; unknown channel zero is not assumed to be Public or LongFast. Meshtastic can use a modem preset reported by the radio when its primary channel name is blank. Historic guessed names cannot be distinguished reliably from real names; existing records and packet IDs are retained for peer compatibility.

## Service operation and upgrades

`radio_admin.py` isolates command storage, current-radio validation, and dispatch. The mailbox defaults to `radio_admin.db` beside the application. Set `BBS_RADIO_ADMIN_PATH` to the same persistent local path in both services when using a custom layout. Both service accounts need access. Keep this mailbox local; never synchronize it across BBS nodes. Preserve it across restarts to retain operation IDs.

The diagnostics snapshot carries an additive `radio_admin` field. Existing readers and older peers do not need to understand it. This release does not change the main database schema, synchronization protocol, account records, tombstones, or retention policies. Channel-name repair is restricted to observations from the local receiving radio. MQTT chatter distribution is unchanged; the composer does not bridge public transmissions between radios.

Restart the BBS and web services after installing the code. To roll back, stop the services, restore the previous code, and restart them; keep the databases and configuration. An old BBS ignores the separate mailbox. Never replay uncertain requests manually as part of a rollback.

## Hardware validation

Automated checks use fake radios and temporary data. Before declaring hardware readiness, record library version, firmware, transport and results for Meshtastic only, MeshCore only, and both networks with each primary placement. Verify reception, a deliberately authorized public send, actual channel names, identity changes, supported contact operations, unplug/reconnect, and continued BBS operation on the other radio. No real-radio transmission is part of the automated suite.
