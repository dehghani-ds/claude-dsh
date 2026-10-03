#!/usr/bin/env bash
# Reach the Claude Workbench dashboard from your phone, on any network, over Tailscale.
#
# Tailscale builds a private network ("tailnet") between your own devices. Only devices
# signed into your tailnet can reach the dashboard — it is never put on the open internet,
# and the traffic is end-to-end encrypted even over hotel wifi or mobile data.
#
#   ./tailscale-remote.sh              set it up and print the phone URL (+ a QR code)
#   ./tailscale-remote.sh --url        just print the URL again
#   ./tailscale-remote.sh --direct     no HTTPS proxy: serve on http://100.x.y.z:PORT instead
#   ./tailscale-remote.sh --off        stop sharing over the tailnet (the dashboard stays local)
#   ./tailscale-remote.sh --port 9000  the dashboard runs on another port
#
# On the phone: install the Tailscale app, sign in with the same account, then open the URL.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT=8765
MODE=setup

while [ $# -gt 0 ]; do
  case "$1" in
    --port) PORT="${2:?--port needs a number}"; shift 2 ;;
    --direct) MODE=direct; shift ;;
    --url) MODE=url; shift ;;
    --off) MODE=off; shift ;;
    -h|--help) sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
done

say()  { printf '\033[1m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m! %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }
ts()   { tailscale "$@"; }

# ---- 1. tailscale installed?
if ! command -v tailscale >/dev/null; then
  say "Tailscale isn't installed. It needs root, so run this once:"
  echo
  echo "    curl -fsSL https://tailscale.com/install.sh | sh"
  echo
  echo "  (that is Tailscale's own installer; it adds their apt repo and the tailscaled service)"
  echo "  Then run this script again."
  exit 1
fi

# ---- 2. logged in?
state="$(ts status --json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin).get("BackendState",""))' 2>/dev/null || true)"
if [ "$state" != "Running" ]; then
  say "Tailscale is installed but not connected (state: ${state:-unknown}). Run:"
  echo
  echo "    sudo tailscale up"
  echo
  echo "  It prints a link — open it and sign in. Sign the phone in to the same account."
  echo "  Then run this script again."
  exit 1
fi

read -r TS_IP TS_DNS <<<"$(ts status --json | python3 -c '
import json, sys
me = json.load(sys.stdin)["Self"]
ips = me.get("TailscaleIPs") or []
v4 = next((i for i in ips if ":" not in i), ips[0] if ips else "")
print(v4, (me.get("DNSName") or "").rstrip("."))')"
[ -n "$TS_IP" ] || die "could not read this machine's tailnet address from: tailscale status --json"

qr() {  # a QR code in the terminal, when a tool for it is around
  if command -v qrencode >/dev/null; then qrencode -t ANSIUTF8 -m 2 "$1"
  elif ts status --json >/dev/null 2>&1 && ts --help 2>&1 | grep -q '\bqr\b'; then ts qr "$1" 2>/dev/null || true
  else echo "  (install qrencode for a scannable QR code: sudo apt install qrencode)"; fi
}

phone_note() {
  echo
  say "On your phone"
  echo "  1. install Tailscale (App Store / Play Store) and sign in with the same account"
  echo "  2. turn the VPN on — it stays on, and costs nothing when you're not using it"
  echo "  3. open: $1"
  echo "  4. optional: 'Add to Home Screen' to get a full-screen, app-like dashboard"
  echo
  echo "  Everything works from there: chats, worktrees, the shell, groups and tags."
  echo "  The page carries a per-run token, so after restarting the server it reloads itself once."
}

case "$MODE" in
off)
  ts serve --https=443 off 2>/dev/null || true
  say "✓ Stopped sharing the dashboard over the tailnet."
  echo "  If you had started it with --direct, also drop --tailscale from the server:"
  echo "      ./install-service.sh --port $PORT     (or restart python3 server.py without it)"
  exit 0 ;;

url)
  if host="$(ts serve status --json 2>/dev/null | python3 -c '
import json, sys
cfg = json.load(sys.stdin) or {}
port = sys.argv[1]
for h, web in (cfg.get("Web") or {}).items():
    for hd in (web.get("Handlers") or {}).values():
        if (hd.get("Proxy") or "").rstrip("/").endswith(":" + port):
            print(h.rsplit(":", 1)[0]); raise SystemExit
raise SystemExit(1)' "$PORT")"; then
    url="https://$host/"
  else
    url="http://$TS_IP:$PORT/"
    warn "no HTTPS proxy is set up — this URL only works if the server runs with --tailscale"
  fi
  say "$url"; echo; qr "$url"; phone_note "$url"
  exit 0 ;;

direct)
  say "Direct mode: the dashboard itself listens on the tailnet address."
  url="http://$TS_IP:$PORT/"
  echo "  Start the server with --tailscale so it binds $TS_IP as well as 127.0.0.1:"
  echo
  echo "      python3 $DIR/server.py --port $PORT --tailscale"
  echo
  echo "  As the systemd service, add it to ExecStart in ~/.config/systemd/user/workbench.service"
  echo "  (or re-run ./install-service.sh after editing workbench.service), then:"
  echo "      systemctl --user restart workbench"
  echo
  say "$url"; echo; qr "$url"
  warn "plain HTTP — fine inside the tailnet (WireGuard already encrypts it), but the browser"
  warn "will call it 'not secure' and some phone features (service workers) stay off."
  phone_note "$url"
  exit 0 ;;
esac

# ---- 3. setup: let tailscale serve terminate HTTPS and proxy to the local dashboard
say "Pointing https://${TS_DNS:-this machine}/ at the dashboard on 127.0.0.1:$PORT …"
echo "  The first time, Tailscale asks you to enable Serve for your tailnet: open the link it prints"
echo "  and click Enable. This waits until you have (Ctrl+C to give up)."
echo
# show tailscale's output live (it may print that link and wait) while also keeping it to inspect
log="$(mktemp)"; trap 'rm -f "$log"' EXIT
serve() { ts serve "$@" 2>&1 | tee -a "$log"; return "${PIPESTATUS[0]}"; }
# current syntax (Tailscale ≥ 1.52) takes just the target; older releases wanted a path before it
if ! serve --bg --yes --https=443 "http://127.0.0.1:$PORT" &&
   ! { grep -qi 'invalid argument\|not defined' "$log" && serve --bg --https=443 / "http://127.0.0.1:$PORT"; }; then
  out="$(cat "$log")"
  echo
  if grep -qi 'access denied\|permission\|operator' <<<"$out"; then
    warn "Your user may not change Tailscale's settings. Allow it once with:"
    echo "      sudo tailscale set --operator=\$USER"
    echo "  then run this script again."
  elif grep -qi 'cert\|https\|magicdns' <<<"$out"; then
    warn "Tailscale couldn't get an HTTPS certificate for this machine."
    echo "  Enable both in the admin console (one click each, free):"
    echo "      https://login.tailscale.com/admin/dns   → MagicDNS, then HTTPS Certificates"
    echo "  Then run this script again. Or skip HTTPS entirely:  $0 --direct --port $PORT"
  else
    warn "tailscale serve failed. You can still use plain HTTP: $0 --direct --port $PORT"
  fi
  exit 1
fi

url="https://${TS_DNS}/"
say "✓ Sharing the dashboard on your tailnet: $url"
echo "  Only devices signed into your tailnet can reach it (serve, not funnel — nothing is public)."
echo "  This survives reboots. Undo with: $0 --off"
echo
qr "$url"
phone_note "$url"
