#!/usr/bin/env bash
# One-time setup of the WhatsApp link server (Evolution API) on a fresh Debian/Ubuntu VM.
#   curl -fsSL https://raw.githubusercontent.com/dumbbel-fit/dumbbel-backend/main/deploy/evolution/setup-vm.sh | sudo WA_DOMAIN=wa.dumbbel.fit bash
# or, from a clone:  sudo WA_DOMAIN=wa.dumbbel.fit bash deploy/evolution/setup-vm.sh
set -euo pipefail

WA_DOMAIN="${WA_DOMAIN:?Set WA_DOMAIN, e.g. WA_DOMAIN=wa.dumbbel.fit}"
DIR=/opt/dumbbel-wa
REPO_RAW=https://raw.githubusercontent.com/dumbbel-fit/dumbbel-backend/main/deploy/evolution

echo "==> Installing Docker"
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh
fi
systemctl enable --now docker

echo "==> Adding 1 GB swap (the free e2-micro VM has 1 GB RAM)"
if ! swapon --show | grep -q /swapfile; then
  fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

SRC="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo "")"
mkdir -p "$DIR" && cd "$DIR"
for f in docker-compose.yml Caddyfile; do
  if [ -n "$SRC" ] && [ -f "$SRC/$f" ] && [ "$SRC" != "$DIR" ]; then
    cp "$SRC/$f" "$f"                       # running from a clone of the repo
  else
    curl -fsSL "$REPO_RAW/$f" -o "$f"       # running via curl | bash
  fi
done

if [ ! -f .env ]; then
  echo "==> Creating secrets in $DIR/.env"
  KEY=$(head -c 48 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 48)
  PGP=$(head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 32)
  printf 'WA_DOMAIN=%s\nEVOLUTION_API_KEY=%s\nPG_PASSWORD=%s\n' "$WA_DOMAIN" "$KEY" "$PGP" > .env
  chmod 600 .env
fi

echo "==> Starting Evolution API + PostgreSQL + HTTPS"
docker compose pull
docker compose up -d

echo "==> Waiting for https://$WA_DOMAIN ..."
for i in $(seq 1 60); do
  if curl -fsS "https://$WA_DOMAIN/" >/dev/null 2>&1; then break; fi
  sleep 5
done
curl -fsS "https://$WA_DOMAIN/" | head -c 200 || echo "(not reachable yet — check the DNS A record and that ports 80/443 are open)"
echo
echo "============================================================"
echo " Put these in the DUMBBEL backend env.yaml, then redeploy it:"
echo "   EVOLUTION_API_URL: \"https://$WA_DOMAIN\""
echo "   EVOLUTION_API_KEY: \"$(grep EVOLUTION_API_KEY .env | cut -d= -f2-)\""
echo "============================================================"
