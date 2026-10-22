# One image for every Python service in this stack. The mode is chosen per
# service in docker-compose.yml (entrypoint/command), so all services share
# one build.
# Digest pinned 2026-09-13; bump deliberately, not by surprise.
# psycopg, psycopg-pool and valkey are polcore.db and polcore.kv (PostgreSQL
# and the live-state store); pinned for the same reason.
FROM python:3.12-slim@sha256:2fe5997d249a808b8eeea52c58a1dbffbba28754dc11699ef5c029f2d818ce79

RUN apt-get update \
    && apt-get install -y --no-install-recommends openssl \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir pyyaml pycryptodome pillow \
        "psycopg[binary]==3.3.6" "psycopg-pool==3.3.3" "valkey==6.1.1"

# The unprivileged account the title images and the standalone services run
# as (USER 1000 in the images built on this one). It matches the owner of the
# /data and /logs bind mounts on a production box, so files written by one
# stay readable by the others. This image itself stays root: the http (80),
# dns (53) and mail (25/110) services bind privileged ports from it.
RUN useradd --uid 1000 --user-group --create-home --shell /usr/sbin/nologin pol

WORKDIR /app
COPY . /app/

# The commit this image was built from, and when, for the admin panel's
# update notices (docs/deployer.md, "Updates"). The publishing workflow
# passes both (the same commit as its org.opencontainers.image.revision
# label); a build that does not leaves them empty, which reads as unknown.
# Last, so the layers above stay cached from one commit to the next.
ARG OPENLOBBY_REVISION=""
ARG OPENLOBBY_BUILT_AT=""
ENV OPENLOBBY_REVISION=$OPENLOBBY_REVISION \
    OPENLOBBY_BUILT_AT=$OPENLOBBY_BUILT_AT

# Default: the observation stub (dns / http / tcp modes). Services override
# the entrypoint to run their own module (responders.py, ucscgi.py, ...).
ENTRYPOINT ["python", "stub.py"]
