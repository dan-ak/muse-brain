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

Use a subdomain of a domain you already own — this project uses
`brain.a-ibk.com`. It never needs to be publicly reachable, and nothing is ever
served on the apex domain. All you need is control of the DNS records, so
Let's Encrypt can verify ownership by watching a TXT record appear.

A repurposed domain is fine. The certificate says nothing about what the domain
was previously for, and a subdomain keeps this separate from anything else the
zone is doing.

### 2. Install the code

Push from the laptop rather than cloning on the Pi. The repository is private,
so cloning would mean putting a GitHub credential on a machine that spends a
week in the desert, and rsync avoids that entirely. Build the bundle on the
laptop first — it is architecture-independent, so shipping `dist/` means the Pi
never needs Node at all.

```bash
# on the laptop, from the repo root
cd muse-pwa && npm ci && npm run build && cd ..

rsync -a --delete \
  --exclude '.git' --exclude 'node_modules' --exclude '.venv' \
  --exclude 'recordings' --exclude '__pycache__' --exclude 'tls' \
  ./ pi@raspberrypi.local:/home/pi/muse-brain/
```

Then on the Pi:

```bash
sudo useradd --system --home-dir /opt/muse-brain --shell /usr/sbin/nologin muse
sudo mkdir -p /opt/muse-brain /etc/muse-brain/tls
sudo rsync -a --delete --exclude '.venv' /home/pi/muse-brain/ /opt/muse-brain/
sudo python3 -m venv /opt/muse-brain/.venv
sudo /opt/muse-brain/.venv/bin/pip install -r /opt/muse-brain/requirements-server.txt
sudo chown -R muse:muse /opt/muse-brain
```

Updating later is the same two rsyncs followed by
`sudo systemctl restart muse-brain`.

### 3. Issue the certificate

Needs internet, so do this at home — but **run it on the Pi**, not on a laptop.

acme.sh registers a renewal cron on whichever machine issues the certificate,
and renewals rewrite the files there. Issue on the Pi and renewal, file
installation, and the service reload all happen in one place. Issuing on a
laptop works and is a perfectly good test of the Cloudflare credentials, but it
leaves the Pi holding a copy that quietly goes stale after 90 days while the
laptop dutifully renews a certificate nothing uses.

**Create a scoped Cloudflare API token.** In the Cloudflare dashboard, go to
**My Profile → API Tokens → Create Token → Create Custom Token**:

- Permissions: **Zone → DNS → Edit**
- Zone Resources: **Include → Specific zone → a-ibk.com**

Scope it to the one zone rather than all zones, and use a token rather than the
Global API Key — a leaked Global API Key hands over the entire Cloudflare
account, while this token can only edit DNS records in this one zone.

**Grab the Zone ID** from the `a-ibk.com` overview page in Cloudflare, in the
right-hand sidebar under "API". Supplying the Zone ID directly means the token
never needs permission to list your zones.

```bash
export MUSE_DOMAIN=brain.a-ibk.com
export MUSE_ACME_EMAIL=dan.acostakane@gmail.com
export MUSE_DNS_PROVIDER=dns_cf
export CF_Token=...        # the token you just created
export CF_Zone_ID=...      # from the a-ibk.com overview page
sudo -E ./scripts/issue-cert.sh
```

The `-E` matters: `sudo` strips the environment by default, and without it the
script cannot see the credentials. It also preserves `HOME`, so acme.sh installs
itself under the invoking user's home directory owned by root, and registers its
renewal cron as root. That is untidy but harmless.

acme.sh creates a `_acme-challenge.brain.a-ibk.com` TXT record, waits for Let's
Encrypt to read it, then removes it. You do not need to touch DNS by hand for
issuance, and nothing needs to point at the Pi yet.

Create the `muse` service user (step 2) *before* issuing, so the script can make
the private key readable by the group the service runs as. If the user does not
exist yet the key is left `0600 root:root` and the service will fail to start
with a permission error; re-running the script after creating the user fixes it.

### 4. Make the name resolve on the router

This is the step that is easy to skip at home and fatal on the playa.

With no uplink, phones cannot reach public DNS, so a Cloudflare record does
nothing out there. The GL.iNet router has to answer for the name itself. Add to
its dnsmasq configuration (LuCI → Network → DHCP and DNS, or
`/etc/dnsmasq.conf`):

```
address=/brain.a-ibk.com/192.168.8.2
```

using whatever address the Pi actually holds — give it a DHCP reservation so it
does not move.

**Optionally**, also add an A record in Cloudflare for `brain` pointing at the
Pi's LAN address, with the proxy **off** (grey cloud, "DNS only"). An orange
cloud cannot work here: Cloudflare would try to proxy public traffic to a
private address. This record is purely a convenience so the same URL works on
your home network before you leave. If Cloudflare objects to a private address,
skip it — the router entry is the one that matters, and it is what will be
serving the name at Burning Man anyway.

### 5. Install the service

```bash
sudo cp deploy/muse-brain.service /etc/systemd/system/
sudo chown -R muse:muse /opt/muse-brain
sudo systemctl enable --now muse-brain
```

The unit binds port 443 as a non-root user through `CAP_NET_BIND_SERVICE`, so
phones open a bare `https://brain.a-ibk.com` with no port number.

## Checking it

```bash
MUSE_DOMAIN=brain.a-ibk.com ./scripts/preflight.sh
```

This checks certificate expiry, that the certificate actually covers the
hostname, that the name resolves to the Pi, and that the server answers over
HTTPS rather than HTTP. Each of those failures looks identical from a phone —
the pair button does nothing — so it is worth checking them separately.

## Before you leave

The certificate lasts 90 days and **renewal requires internet**. Reissue it
shortly before departure and confirm the margin:

```bash
MUSE_DOMAIN=brain.a-ibk.com ./scripts/preflight.sh
```

Aim for well over 30 days remaining at departure.

**Check that `a-ibk.com` itself does not lapse.** It was registered for a
project that has since been scrapped, so it is exactly the kind of domain that
quietly hits its renewal date. If it expires you cannot issue or renew a
certificate at all. An already-issued certificate keeps working — certificates
are not revalidated against registration — so an expiry mid-event is survivable,
but one shortly before departure is not.

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
