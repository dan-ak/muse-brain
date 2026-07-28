#!/usr/bin/env bash
#
# Generate a self-signed certificate for testing the HTTPS path locally.
#
# This is for proving the server, the systemd unit, and the Pi networking work
# before a real certificate exists. It is NOT the Burning Man plan: a browser
# will refuse this certificate, and clicking through the warning does not help
# because Web Bluetooth stays unavailable on an origin with a certificate error.
#
# Use it to check that the server binds, serves the bundle, and upgrades the
# websocket. Use scripts/issue-cert.sh for anything a phone must trust.
#
# Usage:
#   ./scripts/make-dev-cert.sh [output-dir] [hostname-or-ip ...]
#
# Example:
#   ./scripts/make-dev-cert.sh ./tls brain.local 192.168.8.1

set -euo pipefail

OUT_DIR="${1:-./tls}"
shift || true

NAMES=("$@")
if [ ${#NAMES[@]} -eq 0 ]; then
  NAMES=(localhost 127.0.0.1)
fi

mkdir -p "$OUT_DIR"

# Split the names into DNS and IP SAN entries; a certificate for a bare address
# needs an IP entry, and browsers ignore the CN entirely.
SAN=""
for name in "${NAMES[@]}"; do
  if [[ "$name" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    SAN="${SAN}IP:${name},"
  else
    SAN="${SAN}DNS:${name},"
  fi
done
SAN="${SAN%,}"

echo "Generating a self-signed certificate for: $SAN"

openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout "$OUT_DIR/privkey.pem" \
  -out "$OUT_DIR/fullchain.pem" \
  -days 90 \
  -subj "/CN=${NAMES[0]}" \
  -addext "subjectAltName=${SAN}" \
  2>/dev/null

chmod 600 "$OUT_DIR/privkey.pem"

echo
echo "Wrote $OUT_DIR/fullchain.pem and $OUT_DIR/privkey.pem"
echo
echo "Serve with:"
echo "  MUSE_TLS_CERT=$OUT_DIR/fullchain.pem MUSE_TLS_KEY=$OUT_DIR/privkey.pem \\"
echo "    .venv/bin/python server.py"
