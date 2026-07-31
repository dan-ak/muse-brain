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
#   export MUSE_ACME_EMAIL=you@example.com
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
# The reload has to tolerate the unit being absent. On a first install the
# service does not exist yet, and when issuing from a laptop for a Pi it never
# will. Neither is a reason to fail the issue, and acme.sh stores this command
# for every future renewal, so an intolerant one would break renewals too.
RELOAD_CMD="${MUSE_RELOAD_CMD:-systemctl restart muse-brain 2>/dev/null || true}"

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

# acme.sh exits non-zero when it decides a renewal is not due yet. For us that
# is success -- a valid certificate exists -- but set -e would abort here and
# skip the install and permission steps below, silently leaving whatever was in
# $TLS_DIR beforehand. Re-running this script has to be safe and idempotent.
issue_status=0
"$ACME" --issue --dns "$MUSE_DNS_PROVIDER" -d "$MUSE_DOMAIN" || issue_status=$?
if [ "$issue_status" -ne 0 ]; then
  echo "acme.sh exited $issue_status (typically 'renewal not due'); continuing to install."
fi

mkdir -p "$TLS_DIR"

# install-cert is what registers the renewal hook, so the reload command runs
# automatically on every future renewal. Never copy the files by hand.
#
# acme.sh exits non-zero when the reload hook fails even though the certificate
# installed perfectly well, so judge success by whether the files landed rather
# than by the exit status. Letting set -e abort here would skip the permission
# fixes below and leave a key the service cannot read.
if ! "$ACME" --install-cert -d "$MUSE_DOMAIN" \
  --fullchain-file "$TLS_DIR/fullchain.pem" \
  --key-file "$TLS_DIR/privkey.pem" \
  --reloadcmd "$RELOAD_CMD"; then
  echo "note: acme.sh reported an error (usually the reload hook). Checking the files." >&2
fi

for f in fullchain.pem privkey.pem; do
  [ -s "$TLS_DIR/$f" ] || fail "$TLS_DIR/$f was not written"
done

chmod 644 "$TLS_DIR/fullchain.pem"

if id -u muse >/dev/null 2>&1; then
  chown root:muse "$TLS_DIR/privkey.pem" "$TLS_DIR/fullchain.pem"
  chmod 640 "$TLS_DIR/privkey.pem"
  key_note="readable by the muse service user"
else
  # No service user here, so keep the key as tight as possible rather than
  # opening it up for a group that does not exist.
  chmod 600 "$TLS_DIR/privkey.pem"
  key_note="root-only -- there is no 'muse' user on this machine yet"
fi

echo
echo "Certificate installed in $TLS_DIR (key $key_note)"
openssl x509 -in "$TLS_DIR/fullchain.pem" -noout -subject -dates

cat <<'REMINDER'

Three things this script cannot do for you:

  1. Put the certificate where it will be used. acme.sh registered its renewal
     cron on THIS machine, and renewals will rewrite the files here. Run this
     script on the Pi for the production certificate, so renewal and reload
     happen where the service actually runs. Issuing on a laptop is a fine test
     of the credentials, but it leaves the Pi with a certificate that silently
     goes stale.

  2. Point DNS at the Pi. The certificate proves you own the name; it does not
     make the name resolve. Add the record on the GL.iNet router so it resolves
     with no uplink -- a public A record is useless once you are off-grid.

  3. Renew on the playa. This certificate is good for 90 days and renewal needs
     internet. Re-run this script shortly before you leave and confirm with
     scripts/preflight.sh that you have comfortable margin.
REMINDER
