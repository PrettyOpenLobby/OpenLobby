"""Build the OpenLobby Setup program: python setup/build.py [--version v1.2.3]

One file made with PyInstaller (pip install pyinstaller==6.11.1), written to
dist/ at the repository root. The program carries no copy of OpenLobby: it
fetches it on its first run. Build each platform's program on that platform;
.github/workflows/setup-app.yml does it for Windows, Linux and macOS.
"""
import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
NAMES = {"win32": "OpenLobby-Setup", "darwin": "OpenLobby-Setup", "linux": "OpenLobby-Setup-linux-x86_64"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", default="dev", help="the release tag the program reports")
    args = ap.parse_args()
    work = os.path.join(ROOT, "build", "setup")
    os.makedirs(work, exist_ok=True)
    version = os.path.join(work, "setup-version.txt")
    with open(version, "w", encoding="utf-8") as f:
        f.write(args.version + "\n")
    name = NAMES.get(sys.platform, NAMES["linux"])
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--console",
                    "--name", name, "--distpath", os.path.join(ROOT, "dist"),
                    "--workpath", os.path.join(work, "work"), "--specpath", work,
                    "--add-data", version + os.pathsep + ".",
                    os.path.join(HERE, "openlobby_setup.py")], check=True, cwd=ROOT)
    print(os.path.join(ROOT, "dist", name + (".exe" if sys.platform == "win32" else "")))


if __name__ == "__main__":
    main()
