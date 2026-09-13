# One image for every Python service in this stack. The mode is chosen per
# service in docker-compose.yml (entrypoint/command), so all services share
# one build.
# Digest pinned 2026-09-13; bump deliberately, not by surprise.
FROM python:3.12-slim@sha256:2fe5997d249a808b8eeea52c58a1dbffbba28754dc11699ef5c029f2d818ce79

RUN apt-get update \
    && apt-get install -y --no-install-recommends openssl \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir pyyaml pycryptodome pillow

WORKDIR /app
COPY . /app/

# Default: the observation stub (dns / http / tcp modes). Services override
# the entrypoint to run their own module (responders.py, ucscgi.py, ...).
ENTRYPOINT ["python", "stub.py"]
