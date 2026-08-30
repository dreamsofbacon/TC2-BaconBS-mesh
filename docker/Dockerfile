FROM python:3.12-slim

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends frotz \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --shell /usr/sbin/nologin mesh
WORKDIR /home/mesh/bbs

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY fortunes.txt example_config.ini ./
COPY --chmod=755 docker/entrypoint.sh /usr/local/bin/baconbs-entrypoint

RUN mkdir -p /home/mesh/bbs/config && chown -R mesh:mesh /home/mesh/bbs
USER mesh

VOLUME /home/mesh/bbs/config
ENV BBS_CONFIG_PATH=/home/mesh/bbs/config/config.ini
ENV BBS_DB_PATH=/home/mesh/bbs/config/bulletins.db

ENTRYPOINT ["/usr/local/bin/baconbs-entrypoint"]
