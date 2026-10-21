"""Download the docker CLI, the compose plugin and buildx for this image's
architecture, check each against the sha256 pinned here, and install them.
Run by the Dockerfile's first stage; standard library only.

    python fetch_docker.py <amd64|arm64> <dest dir>

To move to newer releases, change the versions and the sums together. The
compose and buildx sums are the ones in each release's checksums.txt; the
docker archive has no published sum, so its sums are those of the files
downloaded from download.docker.com when the version was chosen.
"""
import hashlib
import io
import os
import sys
import tarfile
import urllib.request

DOCKER = "29.8.2"
COMPOSE = "5.6.0"
BUILDX = "0.37.2"

FILES = {
    "amd64": [
        ("https://download.docker.com/linux/static/stable/x86_64/docker-%s.tgz" % DOCKER,
         "995d1ef289677f74fd58d8d2c35727b6a4ee389c69db8638a3e42d0487aa5b0f", "docker"),
        ("https://github.com/docker/compose/releases/download/v%s/docker-compose-linux-x86_64" % COMPOSE,
         "40343e21ca777173e69cff5dbafeb37c6f81f3b0d57d9e597f036e95eb63e76a", "docker-compose"),
        ("https://github.com/docker/buildx/releases/download/v%s/buildx-v%s.linux-amd64" % (BUILDX, BUILDX),
         "982ca20490b45ed1ec8d99795974d3d874a358f75938c9c237305010e6b7e548", "docker-buildx"),
    ],
    "arm64": [
        ("https://download.docker.com/linux/static/stable/aarch64/docker-%s.tgz" % DOCKER,
         "76a624e4a8e5da654d1150e808175125efb5a6f1b6aa1cbd9caee18f51047a50", "docker"),
        ("https://github.com/docker/compose/releases/download/v%s/docker-compose-linux-aarch64" % COMPOSE,
         "733ec76717ceb59052a9609b9dadfb523b2df8eab57a54212872d10a58078ea2", "docker-compose"),
        ("https://github.com/docker/buildx/releases/download/v%s/buildx-v%s.linux-arm64" % (BUILDX, BUILDX),
         "efa38cb7aa7db2dbb9ad049b00b0a9737f66f033626177b5a4e845184ad7ab29", "docker-buildx"),
    ],
}


def main():
    arch, dest = sys.argv[1], sys.argv[2]
    if arch not in FILES:
        sys.exit("no pinned docker binaries for architecture %r" % arch)
    plugins = os.path.join(dest, "cli-plugins")
    os.makedirs(plugins, exist_ok=True)
    for url, want, name in FILES[arch]:
        with urllib.request.urlopen(url, timeout=300) as r:
            data = r.read()
        got = hashlib.sha256(data).hexdigest()
        if got != want:
            sys.exit("%s: sha256 %s, expected %s" % (url, got, want))
        if name == "docker":
            with tarfile.open(fileobj=io.BytesIO(data)) as tf:
                data = tf.extractfile("docker/docker").read()
            path = os.path.join(dest, "docker")
        else:
            path = os.path.join(plugins, name)
        with open(path, "wb") as f:
            f.write(data)
        os.chmod(path, 0o755)
        print("%s %s" % (name, url))


if __name__ == "__main__":
    main()
