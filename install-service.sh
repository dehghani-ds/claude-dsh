#!/usr/bin/env bash
# Install Claude Workbench as a systemd *user* service, so it starts when you log in.
#
#   ./install-service.sh              install (or update) and start it on port 8765
#   ./install-service.sh --port 9000  use another port
#   ./install-service.sh --tailscale  also listen on the tailnet (phone access; see tailscale-remote.sh)
#   ./install-service.sh --dry-run    only print the service file it would install
#   ./install-service.sh --uninstall  stop it and remove the service
#
# Works from wherever this folder lives; nothing needs root.
set -euo pipefail

NAME=workbench
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT="$UNIT_DIR/$NAME.service"
PORT=8765
MODE=install
ARGS=""

while [ $# -gt 0 ]; do
  case "$1" in
    --port) PORT="${2:?--port needs a number}"; shift 2 ;;
    --tailscale) ARGS=" --tailscale"; shift ;;
    --dry-run) MODE=dry; shift ;;
    --uninstall) MODE=uninstall; shift ;;
    -h|--help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
done

say()  { printf '\033[1m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m! %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

if [ "$MODE" = uninstall ]; then
  systemctl --user disable --now "$NAME" 2>/dev/null || true
  rm -f "$UNIT"
  systemctl --user daemon-reload
  say "✓ Removed the $NAME service. Your data (workbench.db) is still in $DIR."
  exit 0
fi

# ---- checks
[[ "$PORT" =~ ^[0-9]+$ ]] && [ "$PORT" -ge 1024 ] && [ "$PORT" -le 65535 ] || die "port must be a number from 1024 to 65535"
PYTHON="$(command -v python3)" || die "python3 not found — install it (sudo apt install python3)"
"$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 8))' || die "python 3.8 or newer is needed"
command -v systemctl >/dev/null || die "systemd not found — run it by hand instead: python3 $DIR/server.py"
command -v git >/dev/null || warn "git not found — the worktree features won't work (sudo apt install git)"
if CLAUDE="$(command -v claude)"; then :; else
  CLAUDE=""; warn "the 'claude' command was not found on your PATH — chatting won't work until Claude Code is installed"
fi

# PATH for the service: this shell's PATH, plus the folder that holds claude (npm/local installs live in odd places)
SVC_PATH="$PATH"
[ -n "$CLAUDE" ] && case ":$SVC_PATH:" in *":$(dirname "$CLAUDE"):"*) ;; *) SVC_PATH="$(dirname "$CLAUDE"):$SVC_PATH" ;; esac

# with a desktop, tie the service to it (needed for the "open terminal" buttons); otherwise start at login
if [ -n "${WAYLAND_DISPLAY:-}${DISPLAY:-}" ]; then TARGET=graphical-session.target; else TARGET=default.target; fi

case "$DIR$SVC_PATH" in *'"'*|*'\\'*) die "the folder path or PATH contains a quote or backslash, which systemd can't handle — move the folder" ;; esac
# escape for sed, and double % because systemd expands %-specifiers
esc() { printf '%s' "$1" | sed -e 's/%/%%/g' -e 's/[\\&|]/\\&/g'; }
UNIT_TEXT="$(sed -e "s|@DIR@|$(esc "$DIR")|g" -e "s|@PORT@|$PORT|g" -e "s|@PYTHON@|$(esc "$PYTHON")|g" \
                 -e "s|@PATH@|$(esc "$SVC_PATH")|g" -e "s|@TARGET@|$TARGET|g" -e "s|@ARGS@|$(esc "$ARGS")|g" \
                 "$DIR/workbench.service")"
grep -q '@[A-Z]*@' <<<"$UNIT_TEXT" && die "internal error: unfilled placeholder in the service file"

if [ "$MODE" = dry ]; then
  say "Would write $UNIT:"; echo; echo "$UNIT_TEXT"; exit 0
fi

# the port must be free, unless it's our own service already running there
if ! systemctl --user is-active --quiet "$NAME" && (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null; then
  die "port $PORT is already in use — pick another with --port"
fi

mkdir -p "$UNIT_DIR"
printf '%s\n' "$UNIT_TEXT" > "$UNIT"
systemctl --user daemon-reload
systemctl --user enable "$NAME" >/dev/null 2>&1
systemctl --user restart "$NAME"

# wait until it answers
for _ in $(seq 1 20); do
  if (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null; then
    say "✓ Claude Workbench is running: http://127.0.0.1:$PORT/"
    [ -n "$ARGS" ] && echo "  phone access over Tailscale: ./tailscale-remote.sh --url --port $PORT"
    echo "  starts automatically when you log in · logs: journalctl --user -u $NAME -f"
    echo "  restart: the ⏻ button in the page, or systemctl --user restart $NAME · remove: $0 --uninstall"
    exit 0
  fi
  sleep 0.5
done
systemctl --user --no-pager status "$NAME" | head -15 >&2 || true
die "the service didn't come up — see: journalctl --user -u $NAME -n 50"
