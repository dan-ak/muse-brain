#!/usr/bin/env bash
#
# Issue a Let's Encrypt certificate for the Pi over DNS-01.
#
# DNS-01 rather than HTTP-01 because the Pi is never publicly reachable: the
# hostname resolves to a private address, so the ACME server can never fetch a
# file from it. Proving control of the DNS record sidesteps that entirely.
#
# Needs internet. Run it at home, not on the playa.
#
# Required:
#   MUSE_DOMAIN         hostname the phones will open, e.g. brain.a-ibk.com
#   MUSE_ACME_EMAIL     address for Let's Encrypt expiry notices
#   MUSE_DNS_PROVIDER   acme.sh DNS hook, e.g. dns_cf for Cloudflare
#
# Plus whatever credentials that hook needs, exported into this environment.
# For Cloudflare that is CF_Token plus either CF_Zone_ID (single domain) or
# CF_Account_ID. Other providers are listed at
# https://github.com/acmesh-official/acme.sh/wiki/dnsapi
#
# The Cloudflare token needs only Zone > DNS > Edit, scoped to the one zone.
# Do not use the Global API Key: leaking it compromises the whole account,
# whereas this token can only touch DNS records in that single zone.
#
# Example:
#   export MUSE_DOMAIN=brain.a-ibk.com
#   export MUSE_ACME_EMAIL=dan.acostakane@gmail.com
#   export MUSE_DNS_PROVIDER=dns_cf
#   export CF_Token=...      # Zone > DNS > Edit, scoped to a-ibk.com
#   export CF_Zone_ID=...    # from the zone overview page, right sidebar
#   sudo -E ./scripts/issue-cert.sh
#
# Note the -E: sudo strips the environment by default, so without it the
# credentials above never reach acme.sh.

set -euo pipefail

TLS_DIR="${MUSE_TLS_DIR:-/etc/muse-brain/tls}"
ACME_HOME="${ACME_HOME:-$HOME/.acme.sh}"
ACME="$ACME_HOME/acme.sh"
RELOAD_CMD="${MUSE_RELOAD_CMD:-systemctl restart muse-brain}"

fail() {
  echo "error: $*" >&2
  exit 1
}

for var in MUSE_DOMAIN MUSE_ACME_EMAIL MUSE_DNS_PROVIDER; do
  if [ -z "${!var:-}" ]; then
    fail "$var is not set. See the header of this script."
  fi
done

if [ ! -x "$ACME" ]; then
  echo "acme.sh not found at $ACME, installing it."
  command -v curl >/dev/null || fail "curl is required to install acme.sh"
  curl -fsSL https://get.acme.sh | sh -s "email=$MUSE_ACME_EMAIL"
  [ -x "$ACME" ] || fail "acme.sh install did not produce $ACME"
fi

# Default is ZeroSSL, which needs a separate account. Let's Encrypt is what the
# design assumes.
"$ACME" --set-default-ca --server letsencrypt

echo "Issuing a certificate for $MUSE_DOMAIN via $MUSE_DNS_PROVIDER."
"$ACME" --issue --dns "$MUSE_DNS_PROVIDER" -d "$MUSE_DOMAIN"

mkdir -p "$TLS_DIR"

# install-cert is what registers the renewal hook, so the reload command runs
# automatically on every future renewal. Never copy the files by hand.
"$ACME" --install-cert -d "$MUSE_DOMAIN" \
  --fullchain-file "$TLS_DIR/fullchain.pem" \
  --key-file "$TLS_DIR/privkey.pem" \
  --reloadcmd "$RELOAD_CMD"

chmod 640 "$TLS_DIR/privkey.pem"
if id -u muse >/dev/null 2>&1; then
  chown root:muse "$TLS_DIR/privkey.pem" "$TLS_DIR/fullchain.pem"
fi

echo
echo "Certificate installed in $TLS_DIR"
openssl x509 -in "$TLS_DIR/fullchain.pem" -noout -subject -dates

cat <<'REMINDER'

Two things this script cannot do for you:

  1. Point DNS at the Pi. The certificate proves you own the name; it does not
     make the name resolve. Add the record on the GL.iNet router so it resolves
     with no uplink -- a public A record is useless once you are off-grid.

  2. Renew on the playa. This certificate is good for 90 days and renewal needs
     internet. Re-run this script shortly before you leave and confirm with
     scripts/preflight.sh that you have comfortable margin.
REMINDER
