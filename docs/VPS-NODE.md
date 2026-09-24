# A public node on a VPS

A node with no radio, on a rented server, that anyone can reach with
`ssh -p 2222 your-vps`. It syncs with the rest of the fleet over MQTT, so
the home network needs no port forwarding.

## What the public can reach

`ssh_server.py` is not OpenSSH. It is an asyncssh application that serves
exactly one thing, the BBS session:

| Request | Result |
|---|---|
| Interactive login | BBS username prompt |
| `ssh host some-command` | refused |
| SFTP / SCP | refused |
| `-L` / `-R` port forwarding | refused |
| Agent / X11 forwarding | refused |

`tests/test_ssh_server.py` checks each of these with `public_access = true`.
What is left is the risk every network service has: a bug in asyncssh or in
the BBS's own input handling. The steps below keep a bug like that inside
the BBS's own directory.

## Node config

```ini
[interface]
type = none            # no radio; syncs over MQTT only

[public_chatter]
sync = false           # do not carry other nodes' overheard traffic

[ssh]
enabled = true
host = 0.0.0.0, ::
port = 2222
public_access = true
registration_limit_per_hour = 5
login_limit_per_hour = 20
max_sessions = 20
idle_timeout_seconds = 1800

[fleet]
trusted_keys = fk...:...   # PUBLIC key only -- never the private signing key

[roles]
remote_role_ceiling = mod  # a peer can never grant admin

[mqtt1]
enabled = true
host = localhost           # the broker runs on this VPS, see below
port = 8883
tls = true
username = vps-node
password = <its own password>
topic_prefix = <your fleet topic>
local_id = <this node's label>
```

## Hardening the server

- **Run the BBS as its own unprivileged user**, owning only the project
  directory. Never root.
- **Tighten the systemd units.** `bacon-ssh.service` already has
  `NoNewPrivileges`, `PrivateTmp` and `ProtectSystem=full`.
  `mesh-bbs.service` has none of them. On the VPS, give both units this
  set:
  ```ini
  NoNewPrivileges=true
  PrivateTmp=true
  ProtectSystem=strict
  ProtectHome=true
  ReadWritePaths=__PROJECT_DIR__
  PrivateDevices=true
  RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
  ```
  `PrivateDevices` hides serial ports, which is fine on a node with no
  radio. Leave it off at home, where the radio is a device.
- **The web admin is never public.** It serves plain HTTP, and it can
  change settings and run updates. Reach it in one of these two ways:
  - **Tailscale (easiest day to day, and works from a phone).** Install it
    on the VPS and on your own devices (`curl -fsSL
    https://tailscale.com/install.sh | sh`, then `sudo tailscale up`). Allow
    8081 only on the Tailscale interface, and open
    `http://<vps-name>:8081` from any device signed in to your Tailscale
    network:
    ```
    sudo ufw allow in on tailscale0 to any port 8081 proto tcp
    ```
    Use the firewall rule, not a Tailscale address in `BBS_WEBGUI_HOST`. If
    Tailscale is not up yet when the admin starts at boot, a bind to that
    address fails.
  - **SSH tunnel (nothing extra installed).** Put
    `BBS_WEBGUI_HOST=127.0.0.1` in `web-admin.env` in the project directory.
    The unit reads that file, it overrides the unit's default, and it
    survives updates. Restart the admin, then connect with
    `ssh -L 8081:localhost:8081 you@your-vps`, then open
    `http://localhost:8081`.

  Either way, change the admin password from the default `change-me` before
  the node goes online.
- **The server's own sshd (port 22) is the real target.** Use key-only
  logins (`PasswordAuthentication no`) and `PermitRootLogin no`. fail2ban
  is optional. It is a different door from the BBS's port 2222, with
  different credentials.
- **Firewall:** allow 22 (admin), 2222 (BBS) and 8883 (MQTT) from
  anywhere, and 8081 only on `tailscale0` if you use Tailscale. Deny
  everything else, including 8081 on the public interface:
  ```
  sudo ufw default deny incoming
  sudo ufw allow 22/tcp
  sudo ufw allow 2222/tcp
  sudo ufw allow 8883/tcp
  sudo ufw enable
  ```
- Keep asyncssh and the OS patched.

## The broker

The broker's location decides where port forwarding is needed. Run
Mosquitto **on the VPS**. Every home node then makes an outbound connection
to it, and the home router needs no open ports.

- TLS on 8883. Let's Encrypt works if the VPS has a domain name.
- `allow_anonymous false`, and one username and password per node.
- An ACL so each node can read and write only the fleet topic:
  ```
  user home-node-1
  topic readwrite <your fleet topic>/#
  ```
- Point each home node's `[mqttN]` at the VPS, and remove the old home
  broker from the fleet.

## If the VPS is broken into

Assume the worst: someone has root on the VPS. What reaches home?

- **Code: nothing.** Fleet updates must be signed, and the private key
  never leaves your own machine. A node with only `trusted_keys` can check
  an instruction but cannot create one.
- **Admin rights: nothing.** A synced role is capped at
  `remote_role_ceiling`, and account sync never carries passwords
  (`apply_synced_node_role`, `apply_synced_account_identity` in
  `db_operations.py`).
- **Your LAN: nothing.** Home nodes only connect out to the broker. Nothing
  on the VPS can open a connection into your network.
- **What an attacker *can* do:** read the fleet's sync traffic, and forge
  unsigned sync frames. That means posts, mail, scores, preferences, and
  bans or mod roles up to the ceiling. These are a nuisance to clean up,
  not a way in. Treat what sync carries as public, because on this
  design, it is.

## Spam and bots

A public login gets bot registrations. `registration_limit_per_hour`
limits them per source address. Moderators and bans work as they do on any
node, and the SSH log records every login outcome
(`journalctl -u bacon-ssh`).
