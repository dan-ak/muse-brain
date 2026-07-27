# Running the Muse host on a Raspberry Pi

The Pi serves the PWA and collects player telemetry. Phones pair their own
headsets over Web Bluetooth and stream focus scores back; the Pi never talks to
a Muse directly, because a Muse 2 accepts only one Bluetooth connection at a
time.

## Why this has to be HTTPS

Web Bluetooth and service workers are only available in a **secure context**:
an HTTPS origin, or the `localhost` exemption. Loading the app from
`http://192.168.8.1` leaves `navigator.bluetooth` undefined, so the pair button
cannot work. There is no flag or setting on the phone that changes this short of
per-device developer configuration.

That is the whole reason for the certificate. It is not about eavesdropping on a
camp network — it is the price of admission for the browser API the project is
built on.

## One-time setup

### 1. Pick a hostname

Use a subdomain of a domain you already own, e.g. `brain.example.com`. It never
needs to be publicly reachable; you only need control of its DNS records so
Let's Encrypt can verify ownership.

### 2. Install the code

```bash
sudo useradd --system --home /opt/muse-brain muse
sudo git clone https://github.com/dan-ak/muse-brain /opt/muse-brain
cd /opt/muse-brain
sudo python3 -m venv .venv
sudo .venv/bin/pip install -r requirements-server.txt
```

Build the PWA bundle. Node on the Pi works but is slow; building on the laptop
and copying `muse-pwa/dist/` across is fine, since the bundle is
architecture-independent.

```bash
cd muse-pwa && npm ci && npm run build
```

### 3. Issue the certificate

Needs internet, so do this at home.

```bash
export MUSE_DOMAIN=brain.example.com
export MUSE_ACME_EMAIL=you@example.com
export MUSE_DNS_PROVIDER=dns_cf        # see acme.sh dnsapi docs for others
export CF_Token=...
export CF_Account_ID=...
sudo -E ./scripts/issue-cert.sh
```

### 4. Make the name resolve on the router

This is the step that is easy to skip at home and fatal on the playa.

With no uplink, phones cannot reach public DNS, so a public A record does
nothing. The GL.iNet router has to answer for the name itself. Add to its
dnsmasq configuration:

```
address=/brain.example.com/192.168.8.2
```

using whatever address the Pi actually holds — give it a DHCP reservation so it
does not move. Setting the same record in public DNS as well is worth doing: it
makes the identical URL work on your home network while testing.

### 5. Install the service

```bash
sudo cp deploy/muse-brain.service /etc/systemd/system/
sudo chown -R muse:muse /opt/muse-brain
sudo systemctl enable --now muse-brain
```

The unit binds port 443 as a non-root user through `CAP_NET_BIND_SERVICE`, so
phones open a bare `https://brain.example.com` with no port number.

## Checking it

```bash
MUSE_DOMAIN=brain.example.com ./scripts/preflight.sh
```

This checks certificate expiry, that the certificate actually covers the
hostname, that the name resolves to the Pi, and that the server answers over
HTTPS rather than HTTP. Each of those failures looks identical from a phone —
the pair button does nothing — so it is worth checking them separately.

## Before you leave

The certificate lasts 90 days and **renewal requires internet**. Reissue it
shortly before departure and confirm the margin:

```bash
MUSE_DOMAIN=brain.example.com ./scripts/preflight.sh
```

Aim for well over 30 days remaining at departure.

One related trap: phones validate certificate dates against their own clock. A
phone that has been offline for a week, or that boots with a flat battery and a
reset clock, can reject a perfectly valid certificate. If exactly one phone
refuses to load the site while others are fine, check its date before
suspecting the Pi.

## Local development

With no certificate configured the server falls back to plain HTTP, which is
fine because `localhost` is exempt from the secure-context rule:

```bash
cd muse-pwa && npm run build && cd ..
.venv/bin/python server.py --port 8443
# open http://localhost:8443
```

To exercise the real TLS path without a public certificate:

```bash
./scripts/make-dev-cert.sh ./tls localhost 127.0.0.1
MUSE_TLS_CERT=./tls/fullchain.pem MUSE_TLS_KEY=./tls/privkey.pem \
  .venv/bin/python server.py --port 8443
```

A self-signed certificate proves the server works, but it will not let a phone
pair: clicking through the browser warning still leaves the origin untrusted,
and Web Bluetooth stays unavailable. Only a real certificate does that.

Setting only one of `MUSE_TLS_CERT` and `MUSE_TLS_KEY` is a startup error rather
than a fallback to HTTP, because a silent downgrade would present as Bluetooth
mysteriously vanishing on every phone.

## Ports

The two-server layout is gone. There is one process on one port serving both the
app and `/ws/{p1,p2}`, which means one certificate and a page that derives its
own socket URL. Nothing on a phone needs an IP address typed into it.

| Was | Now |
|-----|-----|
| 3001 — Vite dev server serving the PWA | 443 — `server.py`, app and websockets |
| 3000 — standalone websocket server | (same origin, `/ws/{player}`) |

`npm run dev` still serves on 3001 for UI work, and still talks to a separately
running `server.py` because the socket URL follows `window.location`.
