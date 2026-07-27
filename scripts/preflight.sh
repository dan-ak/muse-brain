#!/usr/bin/env bash
#
# Check the things that fail silently once there is no internet.
#
# Run this before leaving, and again on the playa after the router is up. Every
# check here corresponds to a failure that presents identically to the user --
# "the pair button does nothing" -- but has a completely different cause.
#
# Usage:
#   MUSE_DOMAIN=brain.example.com ./scripts/preflight.sh

set -uo pipefail

TLS_DIR="${MUSE_TLS_DIR:-/etc/muse-brain/tls}"
CERT="${MUSE_TLS_CERT:-$TLS_DIR/fullchain.pem}"
DOMAIN="${MUSE_DOMAIN:-}"
PORT="${MUSE_PORT:-443}"
MIN_DAYS="${MUSE_MIN_CERT_DAYS:-30}"

failures=0

pass() { printf '  \033[32mok\033[0m    %s\n' "$1"; }
warn() { printf '  \033[33mwarn\033[0m  %s\n' "$1"; }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; failures=$((failures + 1)); }

echo
echo "Certificate"

if [ ! -f "$CERT" ]; then
  bad "no certificate at $CERT -- run scripts/issue-cert.sh"
else
  if ! not_after=$(openssl x509 -in "$CERT" -noout -enddate 2>/dev/null | cut -d= -f2); then
    bad "$CERT is not a readable certificate"
  else
    expiry_epoch=$(date -d "$not_after" +%s 2>/dev/null || echo 0)
    now_epoch=$(date +%s)
    days_left=$(( (expiry_epoch - now_epoch) / 86400 ))

    if [ "$expiry_epoch" -eq 0 ]; then
      bad "could not parse the expiry date ($not_after)"
    elif [ "$days_left" -lt 0 ]; then
      bad "certificate EXPIRED $(( -days_left )) days ago -- phones will refuse it"
    elif [ "$days_left" -lt "$MIN_DAYS" ]; then
      warn "only $days_left days left; renewal needs internet, so reissue before you go"
    else
      pass "valid for $days_left more days"
    fi

    # A certificate issued for the wrong name fails exactly like an expired one.
    if [ -n "$DOMAIN" ]; then
      if openssl x509 -in "$CERT" -noout -text 2>/dev/null | grep -q "DNS:$DOMAIN"; then
        pass "covers $DOMAIN"
      else
        bad "does not cover $DOMAIN -- check the SAN list"
      fi
    fi
  fi
fi

echo
echo "Name resolution"

if [ -z "$DOMAIN" ]; then
  warn "MUSE_DOMAIN not set, skipping -- set it to check what phones will see"
else
  resolved=$(getent hosts "$DOMAIN" 2>/dev/null | awk '{print $1}' | head -1)
  if [ -z "$resolved" ]; then
    bad "$DOMAIN does not resolve. Add it to the router's DNS; public DNS is unreachable off-grid."
  else
    pass "$DOMAIN resolves to $resolved"
    # Match against both address families; a name resolving to ::1 is normal and
    # should not produce a warning.
    if ip -o addr show 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | grep -Fxq "$resolved"; then
      pass "that address belongs to this machine"
    else
      warn "$resolved is not an address on this machine -- fine if you are checking from a laptop"
    fi
  fi
fi

echo
echo "Server"

target="${DOMAIN:-localhost}"
if command -v curl >/dev/null 2>&1; then
  # --insecure so a self-signed dev cert still reports reachability; trust is
  # checked above, this only answers "is anything listening".
  if curl -fsS --insecure --max-time 5 "https://$target:$PORT/healthz" >/dev/null 2>&1; then
    pass "https://$target:$PORT/healthz responded"
  elif curl -fsS --max-time 5 "http://$target:$PORT/healthz" >/dev/null 2>&1; then
    bad "server is answering plain HTTP on $PORT -- Web Bluetooth will not work"
  else
    bad "nothing responded on $target:$PORT"
  fi
else
  warn "curl not installed, skipping the reachability check"
fi

echo
if [ "$failures" -gt 0 ]; then
  echo "$failures check(s) failed."
  exit 1
fi
echo "All checks passed."
