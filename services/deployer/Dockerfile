# The deployer (docs/deployer.md): Python, git, the docker CLI, the compose
# plugin and buildx, nothing else. Same Python base as services/Dockerfile.
#
# The docker binaries are the official static releases, pinned by version and
# sha256 in fetch_docker.py (docker 29.8.2, compose 5.6.0, buildx 0.37.2).
# git comes from the base image's Debian release.
FROM python:3.12-slim@sha256:2fe5997d249a808b8eeea52c58a1dbffbba28754dc11699ef5c029f2d818ce79 AS fetch
ARG TARGETARCH=amd64
COPY fetch_docker.py /fetch_docker.py
RUN python /fetch_docker.py "$TARGETARCH" /out

FROM python:3.12-slim@sha256:2fe5997d249a808b8eeea52c58a1dbffbba28754dc11699ef5c029f2d818ce79
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    # The checkouts belong to the host user. git refuses to work in a
    # repository another user owns unless told it is safe; the deployer runs
    # git as the checkout's owner where it can, and this covers the rest.
    && git config --system --add safe.directory '*'
COPY --from=fetch /out/docker /usr/local/bin/docker
COPY --from=fetch /out/cli-plugins/ /usr/local/lib/docker/cli-plugins/
WORKDIR /app
COPY deployer.py /app/deployer.py

# Root, on purpose: the Docker socket it is given is owned by root (and the
# docker group, whose id differs from host to host), and it starts git and
# stack.py as the owner of the checkouts so files it writes on the host are
# theirs. Anything that can use the socket is root on the host anyway; see
# the warning in docs/deployer.md.
USER root
VOLUME /deployer
ENTRYPOINT ["python", "/app/deployer.py"]
