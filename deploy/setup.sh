#!/usr/bin/env bash
# Precedent Graph: one-shot setup for a fresh Ubuntu 24.04 server.
#   1. put your Anthropic key in deploy/.env
#   2. sudo bash deploy/setup.sh
# Safe to re-run: it rebuilds and restarts the stack with the current code.
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")"          # .../precedent-graph/deploy
say()  { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[warn] %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m[error] %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Run as root:  sudo bash $0"
if [ ! -f .env ]; then
  cp .env.example .env
  die "Created deploy/.env from the template. Put your ANTHROPIC_API_KEY in it and re-run."
fi
[ -f ../data/precedents_full.json.xz ] || die "data/precedents_full.json.xz is missing; re-extract the package."

# ---------- read and check .env ----------
get() { grep -E "^$1=" .env | tail -1 | cut -d= -f2- | tr -d '"'"'"' \r' ; }
KEY="$(get ANTHROPIC_API_KEY)"; DOMAIN="$(get DOMAIN)"
[ -n "$KEY" ] || die "ANTHROPIC_API_KEY is empty in deploy/.env"
[[ "$KEY" == sk-ant-* ]] || warn "ANTHROPIC_API_KEY doesn't start with 'sk-ant-'; check it."
[ -n "$DOMAIN" ] || die "DOMAIN is empty in deploy/.env"

# ---------- packages ----------
say "Installing Docker, Compose and the firewall"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ca-certificates curl ufw dnsutils docker.io docker-compose-v2 >/dev/null
systemctl enable --now docker >/dev/null

# ---------- ports 80/443 must be free for Caddy ----------
for svc in apache2 nginx caddy; do
  if systemctl is-active --quiet "$svc" 2>/dev/null; then
    warn "$svc is running on this host and holds ports 80/443; stopping and disabling it."
    systemctl disable --now "$svc" >/dev/null 2>&1 || true
  fi
done
if ss -ltnp 2>/dev/null | grep -E ':(80|443)\s' | grep -vq docker-proxy; then
  ss -ltnp | grep -E ':(80|443)\s' || true
  die "Something else is listening on port 80 or 443 (see above). Stop it and re-run."
fi

# ---------- firewall ----------
say "Configuring the firewall (SSH, HTTP, HTTPS)"
ufw allow OpenSSH >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw allow 443/udp >/dev/null
ufw --force enable >/dev/null

# ---------- DNS sanity check ----------
say "Checking DNS for $DOMAIN"
DNS_IP="$(dig +short A "$DOMAIN" | tail -1 || true)"
HOST_IPS="$(hostname -I 2>/dev/null || true)"
if [ -z "$DNS_IP" ]; then
  warn "$DOMAIN does not resolve yet. HTTPS will start working once DNS propagates (Caddy retries automatically)."
elif ! grep -qw "$DNS_IP" <<<"$HOST_IPS"; then
  warn "$DOMAIN resolves to $DNS_IP but this server's addresses are: $HOST_IPS"
  warn "If the DNS record is right (e.g. behind NAT), ignore this. Otherwise fix the A record."
else
  echo "$DOMAIN -> $DNS_IP (this server)"
fi

# ---------- build and start ----------
say "Building and starting FalkorDB, the app and Caddy (first build takes a few minutes)"
docker compose up -d --build

say "Waiting for the app to load the graph into FalkorDB (first start: 1–3 minutes)"
for i in $(seq 1 120); do
  status="$(docker inspect -f '{{.State.Health.Status}}' "$(docker compose ps -q app)" 2>/dev/null || echo starting)"
  if [ "$status" = "healthy" ]; then break; fi
  if [ "$status" = "unhealthy" ] || [ "$i" -eq 120 ]; then
    docker compose logs --tail 80 app
    die "The app did not become healthy. The logs above show why."
  fi
  sleep 5; printf '.'
done
echo

HEALTH="$(docker compose exec -T app python -c "import urllib.request;print(urllib.request.urlopen('http://localhost:8000/api/health').read().decode())")"
echo "$HEALTH"

say "Checking HTTPS via Caddy"
ok=0
for i in $(seq 1 24); do
  if curl -fsS --max-time 10 "https://$DOMAIN/api/health" >/dev/null 2>&1; then ok=1; break; fi
  sleep 5
done
if [ "$ok" = 1 ]; then
  echo "HTTPS is live."
else
  warn "https://$DOMAIN isn't answering yet. Usually DNS or certificate issuance still in progress."
  warn "Watch it with:  docker compose -f $(pwd)/docker-compose.yml logs -f caddy"
fi

cat <<EOF

────────────────────────────────────────────────────────────
  Precedent Graph is running.

  App:          https://$DOMAIN
  Health:       https://$DOMAIN/api/health
  MCP endpoint: https://$DOMAIN/mcp/
                claude mcp add --transport http precedents https://$DOMAIN/mcp/

  FalkorDB Browser (private, via SSH tunnel):
                ssh -L 3000:localhost:3000 root@<server-ip>   then open http://localhost:3000

  Logs:         cd $(pwd) && docker compose logs -f app
  Restart:      cd $(pwd) && docker compose restart app
  Update code:  replace the files, then re-run  sudo bash deploy/setup.sh
────────────────────────────────────────────────────────────
EOF
