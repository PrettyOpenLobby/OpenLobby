#!/usr/bin/env bash
# First-run setup for an OpenLobby server on Linux. docs/setup.md explains
# every step it takes and the options below.
#
#   bash setup.sh                 from a checkout
#   curl -fsSL https://raw.githubusercontent.com/PrettyOpenLobby/OpenLobby/main/setup.sh | bash
#   curl -fsSL .../setup.sh | bash -s -- --yes --reach lan
#
# It checks Docker and git, clones OpenLobby when it is not run from a
# checkout, asks who will play and proposes the address settings for that,
# writes .env, starts the stack, sets the admin panel's password and prints
# what to do next. Run again, it keeps the .env it finds.
#
# The whole script is one function, called on the last line, so a download
# that stops halfway runs nothing.

openlobby_setup() {
set -u
set -o pipefail

local REPO_URL_DEFAULT="https://github.com/PrettyOpenLobby/OpenLobby.git"
local RAW_URL="https://raw.githubusercontent.com/PrettyOpenLobby/OpenLobby/main/setup.sh"
local LOOKUP_URL="https://api.ipify.org"

# ------------------------------------------------------------------ options

local YES="${OPENLOBBY_YES:-}"
local DRY=""
local DIR="${OPENLOBBY_DIR:-}"
local REPO_URL="${OPENLOBBY_REPO:-$REPO_URL_DEFAULT}"
local REACH="${OPENLOBBY_REACH:-}"
local ADDRESS="${OPENLOBBY_ADDRESS:-}"
local PUBLIC_ADDRESS="${OPENLOBBY_PUBLIC_ADDRESS:-}"
local PS2="${OPENLOBBY_PS2:-}"
local IMAGES="${OPENLOBBY_IMAGES:-}"
local CLIENTS="${OPENLOBBY_CLIENTS:-}"
local DEPLOYER="${OPENLOBBY_DEPLOYER:-}"
local LOOKUP="${OPENLOBBY_LOOKUP_PUBLIC:-}"
local INSTALL_OK="${OPENLOBBY_INSTALL_DOCKER:-}"
local START=1
local CHANGE_ADDRESS=""

usage() {
    cat <<'EOF'
Usage: setup.sh [options]

Sets up an OpenLobby server on this Linux machine: checks Docker and git,
clones OpenLobby if needed, writes .env, starts the stack and sets the admin
panel's password. Running it again keeps the existing .env.

  --yes                 take the default for every question (no prompts)
  --dry-run             show what would be done; change nothing
  --dir DIR             the OpenLobby checkout to use or create
                        (default: the checkout this script is in, else
                        ~/openlobby-server/openlobby)
  --repo URL            clone from URL instead of the GitHub repository
  --reach lan|vpn|internet
                        who plays: this network only, friends over a VPN,
                        or anyone over the internet
  --address ADDR        the address clients reach this machine on
  --public-address ADDR the public address (internet case)
  --lookup-public       ask an outside service for the public address
  --ps2 | --no-ps2      whether PlayStation 2 consoles will play (they need
                        this server's DNS)
  --images published|build
                        run the published images (default) or build locally
  --deployer | --no-deployer
                        run the deployer, which lets the admin panel install
                        games (default: on)
  --clients DIR|none    folder of your own client installs for the deployer
  --change-address      on an existing .env, redo the address questions
  --install-docker      with --yes, allow installing Docker and git
  --no-start            write .env and stop there
  -h, --help            this text

Every option can also be given as an environment variable: OPENLOBBY_YES=1,
OPENLOBBY_DIR, OPENLOBBY_REPO, OPENLOBBY_REACH, OPENLOBBY_ADDRESS,
OPENLOBBY_PUBLIC_ADDRESS, OPENLOBBY_LOOKUP_PUBLIC=1, OPENLOBBY_PS2=yes|no,
OPENLOBBY_IMAGES, OPENLOBBY_DEPLOYER=yes|no, OPENLOBBY_CLIENTS,
OPENLOBBY_INSTALL_DOCKER=1. Options win over the environment.
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --yes|-y) YES=1 ;;
        --dry-run) DRY=1 ;;
        --dir) DIR="${2:-}"; shift ;;
        --dir=*) DIR="${1#*=}" ;;
        --repo) REPO_URL="${2:-}"; shift ;;
        --repo=*) REPO_URL="${1#*=}" ;;
        --reach) REACH="${2:-}"; shift ;;
        --reach=*) REACH="${1#*=}" ;;
        --address) ADDRESS="${2:-}"; shift ;;
        --address=*) ADDRESS="${1#*=}" ;;
        --public-address) PUBLIC_ADDRESS="${2:-}"; shift ;;
        --public-address=*) PUBLIC_ADDRESS="${1#*=}" ;;
        --lookup-public) LOOKUP=1 ;;
        --ps2) PS2=yes ;;
        --no-ps2) PS2=no ;;
        --images) IMAGES="${2:-}"; shift ;;
        --images=*) IMAGES="${1#*=}" ;;
        --deployer) DEPLOYER=yes ;;
        --no-deployer) DEPLOYER=no ;;
        --clients) CLIENTS="${2:-}"; shift ;;
        --clients=*) CLIENTS="${1#*=}" ;;
        --change-address) CHANGE_ADDRESS=1 ;;
        --install-docker) INSTALL_OK=1 ;;
        --no-start) START="" ;;
        -h|--help) usage; return 0 ;;
        *) echo "setup.sh: unknown option $1 (see --help)" >&2; return 2 ;;
    esac
    shift
done

case "$REACH" in ""|lan|vpn|internet) ;; *) echo "setup.sh: --reach is lan, vpn or internet" >&2; return 2 ;; esac
case "$IMAGES" in ""|published|build) ;; *) echo "setup.sh: --images is published or build" >&2; return 2 ;; esac
case "$PS2" in ""|yes|no|1|0) ;; *) echo "setup.sh: OPENLOBBY_PS2 is yes or no" >&2; return 2 ;; esac
[ "$PS2" = 1 ] && PS2=yes
[ "$PS2" = 0 ] && PS2=no
case "$DEPLOYER" in 1) DEPLOYER=yes ;; 0) DEPLOYER=no ;; esac
# an address given on the command line is a request to set it
if [ -n "$REACH" ] || [ -n "$ADDRESS" ]; then CHANGE_ADDRESS=1; fi

# ------------------------------------------------------------------ helpers

say() { printf '%s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
warn() { printf 'Warning: %s\n' "$*" >&2; }
die() { printf 'setup.sh: %s\n' "$*" >&2; exit 1; }

local HAVE_TTY=""
if [ -z "$YES" ] && (exec </dev/tty) 2>/dev/null; then HAVE_TTY=1; fi

# ask "question" "default" -> REPLY (the default with --yes or no terminal)
local REPLY=""
ask() {
    if [ -n "$YES" ]; then REPLY="$2"; return 0; fi
    if [ -z "$HAVE_TTY" ]; then
        die "no terminal to ask on; run with --yes (and the options in --help)"
    fi
    if [ -n "$2" ]; then printf '%s [%s]: ' "$1" "$2" >/dev/tty
    else printf '%s: ' "$1" >/dev/tty; fi
    IFS= read -r REPLY </dev/tty || REPLY=""
    [ -z "$REPLY" ] && REPLY="$2"
    return 0
}

# yesno "question" y|n -> status 0 for yes
yesno() {
    local hint="y/N"
    [ "$2" = y ] && hint="Y/n"
    if [ -n "$YES" ]; then [ "$2" = y ]; return; fi
    ask "$1 ($hint)" ""
    case "$REPLY" in
        [Yy]*) return 0 ;;
        [Nn]*) return 1 ;;
        *) [ "$2" = y ] ;;
    esac
}

# run a command that changes something; with --dry-run, print it instead
run() {
    if [ -n "$DRY" ]; then say "  would run: $*"; return 0; fi
    "$@"
}

local SUDO=""
if [ "$(id -u)" != 0 ]; then
    if command -v sudo >/dev/null 2>&1; then SUDO="sudo"; else SUDO="NOSUDO"; fi
fi

# run_root "script" : runs a block of shell commands as root
run_root() {
    if [ -n "$DRY" ]; then say "  would run as root:"; printf '%s\n' "$1" | sed 's/^/    /'; return 0; fi
    if [ "$SUDO" = NOSUDO ]; then die "these need root and sudo is not installed; run them as root, then run this script again"; fi
    $SUDO sh -e -c "$1"
}

randstr() { # length charset
    LC_ALL=C tr -dc "$2" </dev/urandom 2>/dev/null | head -c "$1"
}

is_ipv4() {
    local ip="$1" o
    case "$ip" in *[!0-9.]*|"") return 1 ;; esac
    local IFS=.
    # shellcheck disable=SC2086
    set -- $ip
    [ $# -eq 4 ] || return 1
    for o in "$@"; do
        [ -n "$o" ] && [ "$o" -le 255 ] 2>/dev/null || return 1
    done
    return 0
}

is_private() {
    case "$1" in
        10.*|192.168.*|172.1[6-9].*|172.2[0-9].*|172.3[01].*) return 0 ;;
    esac
    return 1
}

is_cgnat() {
    case "$1" in
        100.6[4-9].*|100.[7-9][0-9].*|100.1[01][0-9].*|100.12[0-7].*) return 0 ;;
    esac
    return 1
}

is_special() { # loopback, link-local, unspecified
    case "$1" in 127.*|169.254.*|0.*) return 0 ;; esac
    return 1
}

# network_of 198.51.100.5/24 -> 198.51.100.0/24
network_of() {
    local addr="${1%/*}" bits="${1#*/}"
    [ "$bits" = "$1" ] && bits=32
    local IFS=.
    # shellcheck disable=SC2086
    set -- $addr
    local n=$(( ($1 << 24) | ($2 << 16) | ($3 << 8) | $4 ))
    local mask=$(( bits == 0 ? 0 : (0xFFFFFFFF << (32 - bits)) & 0xFFFFFFFF ))
    n=$(( n & mask ))
    printf '%d.%d.%d.%d/%d' $(( (n >> 24) & 255 )) $(( (n >> 16) & 255 )) \
        $(( (n >> 8) & 255 )) $(( n & 255 )) "$bits"
}

# env_get KEY FILE -> the last active value, quotes removed
env_get() {
    [ -f "$2" ] || return 0
    local v
    v="$(grep -E "^$1=" "$2" | tail -n 1 | cut -d= -f2- | tr -d '\r')"
    v="${v#\"}"; v="${v%\"}"; v="${v#\'}"; v="${v%\'}"
    printf '%s' "$v"
}

# env_set FILE KEY VALUE: replace the active line, else fill in the
# commented example line, else append. An empty value never uncomments.
env_set() {
    local f="$1" k="$2" v="$3" mode
    if grep -qE "^$k=" "$f"; then mode=active
    elif [ -z "$v" ]; then return 0
    elif grep -qE "^# ?$k=" "$f"; then mode=comment
    else printf '%s=%s\n' "$k" "$v" >>"$f"; return 0
    fi
    K="$k" V="$v" M="$mode" awk '
        BEGIN { k = ENVIRON["K"]; v = ENVIRON["V"]; m = ENVIRON["M"]; done = 0 }
        {
            if (!done && m == "active" && index($0, k "=") == 1) { print k "=" v; done = 1; next }
            if (!done && m == "comment" && ($0 ~ ("^# ?" k "="))) { print k "=" v; done = 1; next }
            print
        }' "$f" >"$f.tmp" && mv "$f.tmp" "$f"
}

# ------------------------------------------------------------------ where

step "OpenLobby setup"
[ -n "$DRY" ] && say "Dry run: nothing is changed, every change is printed instead."

local SELF="${BASH_SOURCE[0]:-}"
local CHECKOUT=""
if [ -z "$DIR" ] && [ -n "$SELF" ] && [ -f "$SELF" ]; then
    local here
    here="$(cd "$(dirname "$SELF")" && pwd)"
    if [ -f "$here/docker-compose.yml" ] && [ -f "$here/.env.example" ] && [ -f "$here/tools/stack.py" ]; then
        CHECKOUT="$here"
    fi
fi
local NEED_CLONE=""
if [ -z "$CHECKOUT" ]; then
    if [ -z "$DIR" ]; then
        ask "Directory for the OpenLobby checkout (its parent holds the game checkouts too)" "$HOME/openlobby-server/openlobby"
        DIR="$REPLY"
    fi
    case "$DIR" in "~"/*) DIR="$HOME/${DIR#"~/"}" ;; /*) ;; *) DIR="$(pwd)/$DIR" ;; esac
    DIR="${DIR%/}"
    if [ -f "$DIR/docker-compose.yml" ] && [ -f "$DIR/.env.example" ]; then
        CHECKOUT="$DIR"
    elif [ -e "$DIR" ] && [ -n "$(ls -A "$DIR" 2>/dev/null)" ]; then
        die "$DIR exists and is not an OpenLobby checkout; pick another --dir"
    else
        CHECKOUT="$DIR"; NEED_CLONE=1
    fi
fi
local PARENT
PARENT="$(dirname "$CHECKOUT")"
say "Checkout: $CHECKOUT"
say "Game checkouts go beside it, in $PARENT"

# ------------------------------------------------------------------ tools

step "Checking Docker and git"
local OS_ID="" OS_LIKE="" OS_NAME="this system"
if [ -r /etc/os-release ]; then
    OS_ID="$(. /etc/os-release && printf '%s' "${ID:-}")"
    OS_LIKE="$(. /etc/os-release && printf '%s' "${ID_LIKE:-}")"
    OS_NAME="$(. /etc/os-release && printf '%s' "${PRETTY_NAME:-${ID:-this system}}")"
fi
say "System: $OS_NAME"
local FAMILY=""
case "$OS_ID" in
    debian|raspbian) FAMILY=debian ;;
    ubuntu) FAMILY=ubuntu ;;
    fedora) FAMILY=fedora ;;
    *) case " $OS_LIKE " in *" ubuntu "*) FAMILY=other-ubuntu ;; *" debian "*) FAMILY=other-debian ;; esac ;;
esac

local missing_git="" missing_docker="" missing_compose=""
command -v git >/dev/null 2>&1 || missing_git=1
command -v docker >/dev/null 2>&1 || missing_docker=1
if [ -z "$missing_docker" ] && ! docker compose version >/dev/null 2>&1; then missing_compose=1; fi
[ -z "$missing_git" ] && say "git: $(git --version 2>/dev/null)"
[ -z "$missing_docker" ] && say "docker: $(docker --version 2>/dev/null)"
[ -z "$missing_docker" ] && [ -z "$missing_compose" ] && say "compose: $(docker compose version --short 2>/dev/null)"

if [ -n "$missing_git$missing_docker$missing_compose" ]; then
    local what=""
    [ -n "$missing_git" ] && what="git "
    [ -n "$missing_docker" ] && what="${what}Docker Engine and the compose plugin"
    [ -z "$missing_docker" ] && [ -n "$missing_compose" ] && what="${what}the Docker compose plugin (Compose v2)"
    say "Missing: $what"
    local block=""
    case "$FAMILY" in
        debian|ubuntu)
            local repo_os="$OS_ID"
            [ "$OS_ID" = raspbian ] && repo_os=raspbian
            if [ -n "$missing_docker$missing_compose" ]; then
                block="apt-get update
apt-get install -y ca-certificates curl git
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/$repo_os/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo \"deb [arch=\$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/$repo_os \$(. /etc/os-release && echo \"\${UBUNTU_CODENAME:-\$VERSION_CODENAME}\") stable\" > /etc/apt/sources.list.d/docker.list
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker"
            else
                block="apt-get update
apt-get install -y git"
            fi
            ;;
        fedora)
            if [ -n "$missing_docker$missing_compose" ]; then
                local addrepo="dnf config-manager --add-repo https://download.docker.com/linux/fedora/docker-ce.repo"
                if dnf --version 2>/dev/null | grep -qi dnf5; then
                    addrepo="dnf config-manager addrepo --from-repofile=https://download.docker.com/linux/fedora/docker-ce.repo"
                fi
                block="dnf -y install dnf-plugins-core git
$addrepo
dnf -y install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker"
            else
                block="dnf -y install git"
            fi
            ;;
    esac
    if [ -z "$block" ]; then
        say "Install them with your distribution's packages or Docker's instructions"
        say "(https://docs.docker.com/engine/install/), then run this script again."
        exit 1
    fi
    say ""
    say "These are Docker's own install steps for $OS_NAME"
    say "(https://docs.docker.com/engine/install/), to be run as root:"
    printf '%s\n' "$block" | sed 's/^/    /'
    if [ "$FAMILY" = ubuntu ] && [ -n "$missing_docker" ]; then
        say "Ubuntu's own packages work too: apt-get install docker.io docker-compose-v2 git"
    fi
    local consent=""
    if [ -n "$YES" ]; then
        [ -n "$INSTALL_OK" ] && consent=1
    elif yesno "Run these commands now?" n; then
        consent=1
    fi
    if [ -z "$consent" ]; then
        say "Nothing installed. Run them yourself (or pick your distribution's"
        say "packages), then run this script again."
        exit 1
    fi
    run_root "$block" || die "the install commands failed; see the output above"
    if [ -n "$DRY" ]; then
        say "Dry run: stopping here, since the rest needs Docker installed."
        exit 0
    fi
fi

# Can this user talk to the Docker daemon?
local derr=""
if ! derr="$(docker info 2>&1 >/dev/null)"; then
    case "$derr" in
        *"permission denied"*|*"Permission denied"*)
            say "Docker is installed but your user ($(id -un)) may not use it."
            say "Members of the docker group can; membership counts from your next login."
            say "Being in that group is equivalent to root on this machine."
            say "    sudo usermod -aG docker $(id -un)"
            if yesno "Add $(id -un) to the docker group now?" n; then
                run_root "usermod -aG docker $(id -un)"
                say "Done. Log out and back in (or run: newgrp docker), then run this script again."
            fi
            exit 1 ;;
        *)
            say "The Docker daemon is not answering:"
            printf '%s\n' "$derr" | head -n 3 | sed 's/^/    /'
            say "    sudo systemctl enable --now docker"
            if yesno "Start it now with that command?" n; then
                run_root "systemctl enable --now docker"
                if [ -z "$DRY" ] && ! docker info >/dev/null 2>&1; then die "Docker still does not answer"; fi
            else
                exit 1
            fi ;;
    esac
fi

local COMPOSE_VER
COMPOSE_VER="$(docker compose version --short 2>/dev/null | tr -d 'v\r')"
compose_at_least() { # major minor
    local maj="${COMPOSE_VER%%.*}" rest="${COMPOSE_VER#*.}"
    local min="${rest%%.*}"
    case "$maj$min" in *[!0-9]*|"") return 1 ;; esac
    [ "$maj" -gt "$1" ] || { [ "$maj" -eq "$1" ] && [ "$min" -ge "$2" ]; }
}
local DESKTOP=""
case "$(docker info --format '{{.OperatingSystem}}' 2>/dev/null)" in *"Docker Desktop"*) DESKTOP=1 ;; esac

# ------------------------------------------------------------------ clone

if [ -n "$NEED_CLONE" ]; then
    step "Getting OpenLobby"
    run mkdir -p "$PARENT" || die "cannot create $PARENT"
    run git clone "$REPO_URL" "$CHECKOUT" || die "git clone failed"
    if [ -n "$DRY" ] && [ ! -f "$CHECKOUT/.env.example" ]; then
        say "Dry run: the checkout does not exist yet, so the rest is a sketch."
    fi
fi

local ENV_FILE="$CHECKOUT/.env"
local EXAMPLE="$CHECKOUT/.env.example"
local NEW_ENV=1
[ -f "$ENV_FILE" ] && NEW_ENV=""

# ------------------------------------------------------------------ addresses

# Detect this machine's addresses. LAN = the default route's interface.
local DEF_IF="" LAN_ADDR="" LAN_CIDR="" VPN_IF="" VPN_ADDR="" VPN_CIDR="" IF_PUBLIC=""
detect_addresses() {
    command -v ip >/dev/null 2>&1 || { warn "the ip command is missing, so addresses are not detected"; return 0; }
    DEF_IF="$(ip -4 route show default 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "dev") { print $(i + 1); exit }}')"
    local line ifn cidr a
    while read -r line; do
        [ -n "$line" ] || continue
        ifn="$(printf '%s\n' "$line" | awk '{print $2}')"
        cidr="$(printf '%s\n' "$line" | awk '{for (i = 1; i < NF; i++) if ($i == "inet") { print $(i + 1); exit }}')"
        a="${cidr%/*}"
        is_ipv4 "$a" || continue
        is_special "$a" && continue
        case "$ifn" in
            wg*|wt*|tailscale*|zt*)
                if [ -z "$VPN_ADDR" ]; then
                    VPN_IF="$ifn"; VPN_ADDR="$a"
                    if is_cgnat "$a" && [ "${cidr#*/}" = 32 ]; then
                        VPN_CIDR="$(network_of "$a/10")"     # a Tailscale-style /32 in the shared range
                    elif [ "${cidr#*/}" = 32 ] || [ "$cidr" = "$a" ]; then
                        VPN_CIDR="$(network_of "$a/24")"
                    else
                        VPN_CIDR="$(network_of "$cidr")"
                    fi
                fi
                continue ;;
            docker*|br-*|veth*|virbr*|lo) continue ;;
        esac
        if [ -z "$LAN_ADDR" ] || [ "$ifn" = "$DEF_IF" ] && [ "$LAN_IF_SET" != 1 ]; then
            LAN_ADDR="$a"; LAN_CIDR="$(network_of "$cidr")"
            [ "$ifn" = "$DEF_IF" ] && LAN_IF_SET=1
        fi
        if ! is_private "$a" && ! is_cgnat "$a" && [ -z "$IF_PUBLIC" ]; then IF_PUBLIC="$a"; fi
    done <<EOF
$(ip -o -4 addr show 2>/dev/null)
EOF
}
local LAN_IF_SET=0

port53_busy() {
    command -v ss >/dev/null 2>&1 || return 1
    [ -n "$(ss -Hlun 'sport = :53' 2>/dev/null)" ]
}

# The six address keys, decided below
local A_ADV="" A_PUB="" A_LAN="" A_BIND="" A_CLIENTS="" A_DNSPUB=""

choose_addresses() {
    step "Who will play on this server?"
    detect_addresses
    [ -n "$LAN_ADDR" ] && say "This machine's local network address: $LAN_ADDR ($LAN_CIDR)"
    [ -n "$VPN_ADDR" ] && say "VPN interface $VPN_IF: $VPN_ADDR ($VPN_CIDR)"
    [ -n "$IF_PUBLIC" ] && is_private "$LAN_ADDR" && say "Public address on an interface: $IF_PUBLIC"
    if [ -z "$REACH" ]; then
        say "  1) people on this network only"
        say "  2) friends over a VPN (WireGuard, Tailscale, ZeroTier and the like)"
        say "  3) anyone over the internet"
        local def=1
        [ -n "$VPN_ADDR" ] && def=2
        ask "Choose 1, 2 or 3" "$def"
        case "$REPLY" in 1|lan) REACH=lan ;; 2|vpn) REACH=vpn ;; 3|internet) REACH=internet ;; *) die "expected 1, 2 or 3" ;; esac
    fi

    case "$REACH" in
    lan)
        # a console on this network is inside the default POL_DNS_CLIENTS
        A_ADV="${ADDRESS:-$LAN_ADDR}"
        if port53_busy; then
            say "Something on this machine already listens on port 53 (often systemd-resolved),"
            say "so the DNS service is pinned to $A_ADV with POL_DNS_BIND."
            A_BIND="$A_ADV"
        fi
        ;;
    vpn)
        if [ -z "$ADDRESS" ] && [ -z "$VPN_ADDR" ]; then
            say "No VPN interface (wg*, wt*, tailscale*, zt*) was found."
            ask "This machine's address on the VPN" ""
            ADDRESS="$REPLY"
        fi
        A_ADV="${ADDRESS:-$VPN_ADDR}"
        local vcidr="$VPN_CIDR"
        if [ -z "$vcidr" ] || [ "$A_ADV" != "$VPN_ADDR" ]; then vcidr="$(network_of "$A_ADV/24")"; fi
        A_BIND="$A_ADV"
        A_CLIENTS="127.0.0.0/8,$vcidr"
        [ -z "$PS2" ] && { if yesno "Will a PlayStation 2 on this machine's local network play too? (a console cannot join the VPN)" n; then PS2=yes; else PS2=no; fi; }
        if [ "$PS2" = yes ] && [ -n "$LAN_ADDR" ]; then
            A_BIND=""
            A_CLIENTS="127.0.0.0/8,$vcidr,$LAN_CIDR"
            A_LAN="$LAN_ADDR"
        fi
        ;;
    internet)
        local pub="${PUBLIC_ADDRESS:-${ADDRESS:-}}"
        if [ -z "$pub" ] && [ -n "$IF_PUBLIC" ]; then pub="$IF_PUBLIC"; fi
        if [ -z "$pub" ]; then
            say "This machine has no public address on its own interfaces, so it is"
            say "probably behind a router. Its public address can be looked up by"
            say "asking $LOOKUP_URL (one HTTPS request; nothing else is sent)."
            local doit=""
            if [ -n "$LOOKUP" ]; then doit=1
            elif [ -z "$YES" ] && yesno "Look it up there?" n; then doit=1; fi
            if [ -n "$doit" ]; then
                if command -v curl >/dev/null 2>&1; then pub="$(curl -fsS --max-time 10 "$LOOKUP_URL" 2>/dev/null | tr -d '\r\n ')"
                elif command -v wget >/dev/null 2>&1; then pub="$(wget -qO- --timeout=10 "$LOOKUP_URL" 2>/dev/null | tr -d '\r\n ')"
                fi
                is_ipv4 "$pub" || { warn "the lookup gave no address"; pub=""; }
                [ -n "$pub" ] && say "The lookup says: $pub"
            fi
            if [ -z "$pub" ]; then
                ask "The public address players will connect to" ""
                pub="$REPLY"
            fi
        fi
        A_ADV="$pub"
        [ -z "$PS2" ] && { if yesno "Will PlayStation 2 consoles play over the internet? (they need this server's DNS open to the internet)" n; then PS2=yes; else PS2=no; fi; }
        if [ "$PS2" = yes ]; then
            A_PUB="$pub"
            A_DNSPUB=1
        else
            # PCs use the hosts installer; keep the resolver off the public side
            if [ -n "$LAN_ADDR" ] && is_private "$LAN_ADDR"; then A_BIND="$LAN_ADDR"; else A_BIND="127.0.0.1"; fi
        fi
        ;;
    esac
    [ -n "$A_ADV" ] || die "no address to advertise; give one with --address"
    is_ipv4 "$A_ADV" || die "$A_ADV is not an IPv4 address"

    say ""
    say "Proposed address settings for .env (docs/configuration.md):"
    show_addr
    if [ -z "$YES" ] && ! yesno "Use these?" y; then
        say "Enter a value, or - to leave a setting empty (the default)."
        edit_one POL_ADVERTISE A_ADV
        edit_one POL_ADVERTISE_PUBLIC A_PUB
        edit_one POL_ADVERTISE_LAN A_LAN
        edit_one POL_DNS_BIND A_BIND
        edit_one POL_DNS_CLIENTS A_CLIENTS
        edit_one POL_DNS_PUBLIC A_DNSPUB
    fi
}

show_addr() {
    say "  POL_ADVERTISE=$A_ADV"
    say "  POL_ADVERTISE_PUBLIC=$A_PUB"
    say "  POL_ADVERTISE_LAN=$A_LAN"
    say "  POL_DNS_BIND=$A_BIND"
    say "  POL_DNS_CLIENTS=$A_CLIENTS"
    say "  POL_DNS_PUBLIC=$A_DNSPUB"
}

edit_one() { # KEY VARNAME
    local cur="${!2}"
    ask "  $1" "$cur"
    [ "$REPLY" = "-" ] && REPLY=""
    printf -v "$2" '%s' "$REPLY"
}

# ------------------------------------------------------------------ .env

step "Settings (.env)"
local WORK
WORK="$(mktemp "${TMPDIR:-/tmp}/openlobby-env.XXXXXX")" || die "mktemp failed"
# shellcheck disable=SC2064
trap "rm -f '$WORK' '$WORK.tmp'" EXIT

local SET_ADDR="" GEN_DB="" GEN_TOKEN=""
if [ -n "$NEW_ENV" ]; then
    if [ -f "$EXAMPLE" ]; then tr -d '\r' <"$EXAMPLE" >"$WORK"; else : >"$WORK"; fi
    choose_addresses
    SET_ADDR=1
else
    tr -d '\r' <"$ENV_FILE" >"$WORK"
    say "Found $ENV_FILE; keeping its values."
    say "Current address: POL_ADVERTISE=$(env_get POL_ADVERTISE "$WORK")"
    if [ -n "$CHANGE_ADDRESS" ] || { [ -z "$YES" ] && yesno "Change the address settings?" n; }; then
        choose_addresses
        SET_ADDR=1
    fi
fi

if [ -n "$SET_ADDR" ]; then
    env_set "$WORK" POL_ADVERTISE "$A_ADV"
    env_set "$WORK" POL_ADVERTISE_PUBLIC "$A_PUB"
    env_set "$WORK" POL_ADVERTISE_LAN "$A_LAN"
    env_set "$WORK" POL_DNS_BIND "$A_BIND"
    env_set "$WORK" POL_DNS_CLIENTS "$A_CLIENTS"
    env_set "$WORK" POL_DNS_PUBLIC "$A_DNSPUB"
fi

# Images
local PUBLISHED=""
if [ -n "$NEW_ENV" ]; then
    if [ -z "$IMAGES" ]; then
        say ""
        say "Images: published images are pulled ready-made from ghcr.io (faster);"
        say "building compiles them here from this checkout (several minutes)."
        ask "Use published images or build locally? (published/build)" published
        case "$REPLY" in b*) IMAGES=build ;; *) IMAGES=published ;; esac
    fi
    if [ "$IMAGES" = published ] && ! compose_at_least 2 24; then
        warn "the published images need Docker Compose 2.24 or newer (this is ${COMPOSE_VER:-unknown}); building locally instead"
        IMAGES=build
    fi
    if [ "$IMAGES" = published ]; then
        env_set "$WORK" COMPOSE_FILE "docker-compose.yml:docker-compose.ghcr.yml"
        PUBLISHED=1
    fi
else
    case "$(env_get COMPOSE_FILE "$WORK")" in *ghcr*) PUBLISHED=1 ;; esac
fi

# Database password: only on a new .env, and never for a database that
# already exists (it keeps the password it was created with).
local PROJECT
PROJECT="$(env_get COMPOSE_PROJECT_NAME "$WORK")"
[ -n "$PROJECT" ] || PROJECT="$(basename "$CHECKOUT" | tr 'A-Z' 'a-z' | tr -cd 'a-z0-9_-')"
if [ -z "$(env_get POL_DB_PASSWORD "$WORK")" ]; then
    if [ -n "$NEW_ENV" ]; then
        if docker volume inspect "${PROJECT}_pol-pgdata" >/dev/null 2>&1; then
            warn "volume ${PROJECT}_pol-pgdata already exists, so POL_DB_PASSWORD is left as it was (empty = openlobby)"
        else
            env_set "$WORK" POL_DB_PASSWORD "$(randstr 32 'A-Za-z0-9')"
            GEN_DB=1
        fi
    fi
fi

# The deployer
local USE_DEPLOYER=""
if [ -n "$(env_get POL_DEPLOYER_TOKEN "$WORK")" ] && [ "$DEPLOYER" != no ]; then
    USE_DEPLOYER=1
elif [ -n "$NEW_ENV" ] || [ "$DEPLOYER" = yes ]; then
    if [ -n "$DESKTOP" ]; then
        say "This Docker is Docker Desktop; the deployer needs Docker Engine, so it is left off."
    elif [ "$DEPLOYER" = yes ]; then
        USE_DEPLOYER=1
    elif [ "$DEPLOYER" != no ]; then
        say ""
        say "The deployer lets the admin panel install, enable and update games"
        say "(docs/deployer.md). It holds the Docker socket, which makes it as"
        say "powerful as root here; it publishes no port and needs a token."
        if yesno "Run the deployer?" y; then USE_DEPLOYER=1; fi
    fi
    if [ -n "$USE_DEPLOYER" ]; then
        env_set "$WORK" POL_DEPLOYER_TOKEN "$(randstr 43 'A-Za-z0-9_-')"
        GEN_TOKEN=1
    fi
elif [ -z "$(env_get POL_DEPLOYER_TOKEN "$WORK")" ]; then
    say "The deployer is not set up in this .env; run with --deployer to add it."
fi
if [ -n "$USE_DEPLOYER" ]; then
    [ -n "$(env_get POL_DEPLOY_ROOT "$WORK")" ] || env_set "$WORK" POL_DEPLOY_ROOT "$PARENT"
    if [ "$(basename "$CHECKOUT")" != openlobby ] && [ -z "$(env_get POL_DEPLOY_CORE "$WORK")" ]; then
        env_set "$WORK" POL_DEPLOY_CORE "$CHECKOUT"
    fi
    [ -n "$PUBLISHED" ] && [ -z "$(env_get POL_DEPLOY_GHCR "$WORK")" ] && env_set "$WORK" POL_DEPLOY_GHCR 1
    if [ -z "$(env_get POL_DEPLOY_CLIENTS "$WORK")" ] && [ "$CLIENTS" != none ]; then
        if [ -z "$CLIENTS" ]; then
            say ""
            say "Optional: a folder for your own client installs (copies of your game"
            say "install folders). When a game needs data from your client, the admin"
            say "panel can pick an install from this folder to import it from. The"
            say "deployer only reads it. Without one, the panel takes uploaded archives"
            say "instead. Enter - to skip."
            ask "Client installs folder" "$PARENT/clients"
            CLIENTS="$REPLY"
        fi
        if [ -n "$CLIENTS" ] && [ "$CLIENTS" != "-" ] && [ "$CLIENTS" != none ]; then
            case "$CLIENTS" in "~"/*) CLIENTS="$HOME/${CLIENTS#"~/"}" ;; /*) ;; *) CLIENTS="$(pwd)/$CLIENTS" ;; esac
            env_set "$WORK" POL_DEPLOY_CLIENTS "$CLIENTS"
            run mkdir -p "$CLIENTS"
        fi
    fi
fi

# Show and write
if [ -n "$DRY" ]; then
    say ""
    say "Dry run: the .env that would be written ($ENV_FILE), secrets hidden:"
    grep -E '^[A-Z_]+=.+' "$WORK" | sed -E 's/^(POL_DB_PASSWORD|POL_DEPLOYER_TOKEN|POL_ADMIN_PASSWORD)=.*/\1=<secret>/' | sed 's/^/    /'
elif [ -n "$NEW_ENV" ] || ! cmp -s "$WORK" "$ENV_FILE"; then
    [ -n "$NEW_ENV" ] || cp -p "$ENV_FILE" "$ENV_FILE.bak" 2>/dev/null
    # umask 077: the copy is never readable by others, not even for a moment
    (umask 077 && rm -f "$ENV_FILE.new" && cp "$WORK" "$ENV_FILE.new") &&
        chmod 600 "$ENV_FILE.new" && mv "$ENV_FILE.new" "$ENV_FILE" || die "could not write $ENV_FILE"
    say "Wrote $ENV_FILE (readable by you only)."
    [ -n "$NEW_ENV" ] || say "The previous one is in $ENV_FILE.bak."
else
    say "$ENV_FILE is unchanged."
fi
[ -z "$DRY" ] && [ -f "$ENV_FILE" ] && chmod 600 "$ENV_FILE"
if [ -z "$DRY" ]; then
    [ -n "$GEN_DB" ] && say "Generated a database password (POL_DB_PASSWORD, kept in .env)."
    [ -n "$GEN_TOKEN" ] && say "Generated a deployer token (POL_DEPLOYER_TOKEN, kept in .env)."
fi

if [ -z "$START" ]; then
    say ""
    say "Not starting (--no-start). Start it later with this script, or docs/setup.md, \"By hand\"."
    exit 0
fi

# ------------------------------------------------------------------ start

step "Starting the stack"
cd "$CHECKOUT" 2>/dev/null || { [ -n "$DRY" ] || die "cannot enter $CHECKOUT"; }
local HAVE_PY=""
command -v python3 >/dev/null 2>&1 && HAVE_PY=1
local GAMES=""
[ -s "$CHECKOUT/.games" ] && GAMES="$(tr '\n' ' ' <"$CHECKOUT/.games")"
local -a DC=(docker compose)
if [ -n "$GAMES" ]; then
    [ -n "$HAVE_PY" ] || die "games are enabled ($GAMES) and starting them needs python3 (tools/stack.py)"
    DC=(python3 tools/stack.py compose)
    [ -n "$PUBLISHED" ] && DC+=(--ghcr)
    say "Enabled games: $GAMES(started through tools/stack.py)"
fi
if [ -n "$PUBLISHED" ]; then
    [ -z "$GAMES" ] && { run "${DC[@]}" pull || die "pulling the images failed"; }
    run "${DC[@]}" up -d || die "docker compose up failed; see the output above"
else
    run "${DC[@]}" up -d --build || die "docker compose up failed; see the output above"
fi
if [ -n "$USE_DEPLOYER" ]; then
    run docker compose --profile deployer up -d deployer || warn "the deployer did not start; docker compose logs deployer says why"
fi

if [ -z "$DRY" ]; then
    say "Waiting for the services to report healthy (up to 3 minutes)..."
    local waited=0 st pending
    while :; do
        st="$("${DC[@]}" ps -a --format '{{.Service}} {{.State}} {{.Health}}' 2>/dev/null)"
        pending="$(printf '%s\n' "$st" | awk 'NF && ($2 != "running" || ($3 != "" && $3 != "healthy")) {print $1}' | tr '\n' ' ')"
        [ -n "$st" ] && [ -z "$pending" ] && { say "All services are up."; break; }
        if [ "$waited" -ge 180 ]; then
            warn "still waiting on: $pending(docker compose ps; docker compose logs <service>)"
            break
        fi
        sleep 5; waited=$((waited + 5))
    done
fi

# ------------------------------------------------------------------ admin

step "Admin panel password"
local ADMIN_USER ADMIN_PW="" ADMIN_STATE=""
ADMIN_USER="$(env_get POL_ADMIN_USER "$WORK")"
[ -n "$ADMIN_USER" ] || ADMIN_USER=admin
if [ -n "$DRY" ]; then
    say "  would run: ${DC[*]} exec -T admin python admin.py --show-admin"
    say "  and, when no credential is stored, generate a password and pipe it to"
    say "  ${DC[*]} exec -T admin python admin.py --set-password"
else
    local shown
    shown="$("${DC[@]}" exec -T admin python admin.py --show-admin 2>&1 </dev/null)"
    case "$shown" in
        *"user="*)
            ADMIN_STATE=kept
            local stored
            stored="$(printf '%s\n' "$shown" | sed -n "s/.*user='\([^']*\)'.*/\1/p" | head -n 1)"
            [ -n "$stored" ] && ADMIN_USER="$stored"
            say "A password is already set for $ADMIN_USER; leaving it alone." ;;
        *"no stored credential"*)
            ADMIN_PW="$(randstr 20 'A-Za-z0-9')"
            local tries=0 out=""
            while [ "$tries" -lt 5 ]; do
                out="$(printf '%s\n' "$ADMIN_PW" | "${DC[@]}" exec -T admin python admin.py --set-password 2>&1)"
                case "$out" in *"credential set"*) ADMIN_STATE=new; break ;; esac
                tries=$((tries + 1)); sleep 3
            done
            if [ "$ADMIN_STATE" = new ]; then say "Set a new password for $ADMIN_USER."
            else
                ADMIN_PW=""
                warn "could not set the password: $(printf '%s' "$out" | tail -n 1)"
                say "Set one by hand: docker compose exec admin python admin.py --set-password"
            fi ;;
        *)
            warn "could not ask the admin service: $(printf '%s' "$shown" | tail -n 1)"
            say "Set a password by hand: docker compose exec admin python admin.py --set-password" ;;
    esac
fi

# ------------------------------------------------------------------ doctor

step "Checking the setup (tools/doctor.py)"
if [ -n "$HAVE_PY" ]; then
    run python3 tools/doctor.py || say "(doctor.py reported problems; each FAIL line says what to do)"
else
    say "python3 is not installed, so tools/doctor.py was skipped. Install python3"
    say "and run it later from $CHECKOUT."
fi

# ------------------------------------------------------------------ summary

local ADV
ADV="$(env_get POL_ADVERTISE "$WORK")"
[ -n "$ADV" ] || ADV="127.0.0.1"
local PS2_DNS
PS2_DNS="$(env_get POL_ADVERTISE_LAN "$WORK")"
[ -n "$PS2_DNS" ] || PS2_DNS="$ADV"
if [ -z "$REACH" ]; then
    if [ -n "$(env_get POL_DNS_CLIENTS "$WORK")" ] || is_cgnat "$ADV"; then REACH=vpn
    elif is_private "$ADV" || is_special "$ADV"; then REACH=lan
    else REACH=internet
    fi
fi

step "Done"
say "Admin panel:"
if [ -n "${SSH_CONNECTION:-}" ]; then
    local sip sport
    sip="$(printf '%s' "$SSH_CONNECTION" | awk '{print $3}')"
    sport="$(printf '%s' "$SSH_CONNECTION" | awk '{print $4}')"
    local pflag=""
    [ -n "$sport" ] && [ "$sport" != 22 ] && pflag="-p $sport "
    say "  You are on SSH, and the panel only listens on this machine. On your own"
    say "  computer, run"
    say "    ssh -N ${pflag}-L 8090:127.0.0.1:8090 $(id -un)@$sip"
    say "  and open http://127.0.0.1:8090 there."
else
    say "  http://127.0.0.1:8090 (on this machine only)"
fi
case "$ADMIN_STATE" in
    new)
        say "  User: $ADMIN_USER"
        say "  Password: $ADMIN_PW"
        say "  This is the only time it is shown. Change it on the panel, Settings > Sign-in." ;;
    kept) say "  User: $ADMIN_USER (password unchanged)" ;;
esac
say ""
say "Players:"
say "  PC: copy client/Install-PolConnect.ps1 to the player's PC and run, in an"
say "  elevated PowerShell:"
say "    powershell -ExecutionPolicy Bypass -File .\\Install-PolConnect.ps1 -Server $ADV"
say "  PlayStation 2: set the console's DNS server to $PS2_DNS (docs/client.md)."
say "  Accounts: create them on the admin panel, or registration codes for the"
say "  in-client sign-up."
say ""
if [ -n "$USE_DEPLOYER" ]; then
    say "Games: on the admin panel, Settings > Install games."
else
    say "Games: python3 tools/stack.py (docs/games.md)."
fi
say ""
say "Firewall (docs/ports.md):"
case "$REACH" in
    lan)
        say "  On a local network nothing needs opening beyond what this machine's own"
        say "  firewall blocks." ;;
    vpn)
        say "  Allow these on the VPN interface: TCP 80, 51200-51271, 51273-51310,"
        say "  51330, 53001-53015, 54000 (25 and 110 for generic mail accounts),"
        say "  and 53/udp for clients using this server's DNS." ;;
    internet)
        say "  Forward or open TCP 80, 51200-51271, 51273-51310, 51330, 53001-53015"
        say "  and 54000 (25 and 110 for generic mail accounts, 443 for PlayStation 2)."
        if [ -n "$(env_get POL_DNS_PUBLIC "$WORK")" ]; then
            say "  53/udp too, for the consoles (restricted public DNS is on)."
        else
            say "  Keep 53/udp closed to the internet."
        fi
        say "  Docker's published ports bypass ufw and firewalld; docs/ports.md,"
        say "  \"Docker and host firewalls\", has what works." ;;
esac
say ""
say "Run this script again any time; it keeps .env. docs/setup.md has the details."
}

openlobby_setup "$@"
