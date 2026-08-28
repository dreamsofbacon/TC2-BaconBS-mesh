# syntax=docker/dockerfile:1
#
# Bacon BBS in one container.
#
# The BBS is two processes, not one: server.py drives the radios and the sync
# protocol, and web_admin.py serves the admin GUI. They are separate processes
# that talk to each other through files in a shared directory -- the trigger
# files and runtime_diagnostics.json -- so they cannot be split into two
# containers without inventing a shared volume purely to carry that IPC.
# Running both here keeps the contract the code already has.

FROM python:3.12-slim

# The version is normally the commit count, resolved from .git at runtime.
# Neither .git (excluded by .dockerignore, and it would invalidate the build
# cache on every commit) nor a git binary is present here, so without these
# the container reports the frozen fallback version forever -- exactly the
# failure that made every release look identical to an operator running from
# a source archive. docker/build.sh and the CI workflow fill them in.
ARG BBS_BUILD_NUMBER=""
ARG BBS_GIT_COMMIT=""

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    BBS_BUILD_NUMBER=${BBS_BUILD_NUMBER} \
    BBS_GIT_COMMIT=${BBS_GIT_COMMIT}

# gosu drops root to the PUID/PGID Unraid hands in; see the entrypoint.
# frotz supplies dfrotz, the Z-machine interpreter zork_port.py shells out to.
# It is an optional feature, so a base image or mirror without that package
# must degrade to "Zork unavailable" rather than failing the whole build.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates gosu \
 && { apt-get install -y --no-install-recommends frotz \
      || echo "WARNING: frotz not installed; the Zork games will report no interpreter"; } \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# This image used to copy only *.py, which left the web admin with no
# templates and no stylesheets to serve -- and on a headless server the web
# admin IS the interface. tests/test_docker_image.py asserts every runtime
# asset the code loads from disk is still on this list.
COPY *.py ./
COPY templates/ ./templates/
COPY static/ ./static/
COPY fortunes.txt example_config.ini ./
COPY docker/entrypoint.sh /usr/local/bin/baconbs-entrypoint
RUN chmod 755 /usr/local/bin/baconbs-entrypoint

# Everything that must survive an image rebuild lives on the volume: the
# config, the database, uploaded MQTT TLS material, downloaded Zork stories,
# and the trigger files the two processes signal each other with. Every one of
# these otherwise defaults to the application directory, which is replaced
# wholesale on update -- a container that loses its trigger files silently
# stops responding to "Sync now" in the GUI.
ENV BBS_CONFIG_PATH=/config/config.ini \
    BBS_DB_PATH=/config/bulletins.db \
    BBS_RUNTIME_DIAG_PATH=/config/runtime_diagnostics.json \
    BBS_MANUAL_SYNC_TRIGGER_PATH=/config/manual_sync.trigger \
    BBS_FORCE_CHECK_TRIGGER_PATH=/config/force_check.trigger \
    BBS_PEER_RESYNC_TRIGGER_PATH=/config/resync_peer.trigger \
    BBS_ZORK_SAVE_RESOLVE_TRIGGER_PATH=/config/resolve_zork_save.trigger \
    BBS_RECORD_RESOLVE_TRIGGER_PATH=/config/resolve_record.trigger \
    BBS_LINK_RECONNECT_TRIGGER_PATH=/config/reconnect_link.trigger \
    BBS_LINKS_RELOAD_TRIGGER_PATH=/config/reload_links.trigger \
    BBS_MQTT_CERT_DIR=/config/mqtt-certs \
    BBS_WEBGUI_PORT=8081

# The web admin binds 127.0.0.1 by default, which is correct for a Pi running
# behind its own firewall and useless in a container: nothing outside the
# network namespace could ever reach it. Published ports are the container's
# access control here.
ENV BBS_WEBGUI_HOST=0.0.0.0

# zork_port.py resolves story files relative to the working directory, so
# downloads would land in the image layer and vanish on update.
RUN ln -s /config/data /app/data

# dialout owns the USB serial device nodes a companion radio appears as. The
# uid is fixed at 1000 only as a starting point -- the entrypoint remaps it to
# PUID/PGID, which is how Unraid expects to control file ownership.
RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin mesh \
 && usermod -aG dialout mesh \
 && mkdir -p /config \
 && chown -R mesh:mesh /app /config

VOLUME /config
EXPOSE 8081

# /login is the one route that answers without a session, so this checks that
# Flask is actually serving rather than merely that the process exists.
HEALTHCHECK --interval=60s --timeout=10s --start-period=90s --retries=3 \
  CMD python3 -c "import os,sys,urllib.request; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:' + os.getenv('BBS_WEBGUI_PORT','8081') + '/login', timeout=5).status == 200 else 1)"

ENTRYPOINT ["/usr/local/bin/baconbs-entrypoint"]
