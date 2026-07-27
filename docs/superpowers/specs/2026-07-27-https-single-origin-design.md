# HTTPS Single-Origin Host — Design

**Date:** 2026-07-27
**Status:** Approved
**Project:** brain (Muse 2 EEG)

## Goal

Let several Android phones load the Muse PWA from a Raspberry Pi 5 over a
GL.iNet travel router, pair a Muse 2 over Web Bluetooth, and stream focus scores
back to the Pi — with no internet uplink available.

## The problem this solves

Web Bluetooth and Service Worker registration both require a **secure context**:
HTTPS, or the `localhost` exemption. Everything works today only because
development happens at `http://localhost:3001`. The moment a phone loads the app
from the Pi's LAN address over plain HTTP, `navigator.bluetooth` is undefined,
the pair button throws, and the service worker refuses to register. There is no
partial version of this failure — the core feature simply does not exist on an
insecure origin.

A second constraint follows from the first: an HTTPS page may not open a
`ws://` connection. Mixed active content is blocked, so the websocket must be
`wss://`, which means it needs a certificate too.

## Decisions

| Question | Decision |
|----------|----------|
| Ingest path | Android + Web Bluetooth PWA. Mind Monitor/OSC stays as the solo-dev path. |
| Certificate | Real Let's Encrypt cert issued via DNS-01. No per-device CA install. |
| Origin layout | One process, one port: static PWA and websocket share an origin. |
| Server library | `aiohttp` — static files, websockets, and TLS in one process. |
| Cert acquisition | Out of band via `acme.sh`. The server only reads cert files. |
| Player IDs | Unchanged (`p1`, `p2`). Lifting the cap is separate work. |

## Architecture

Today there are two servers: Vite on 3001 serving the PWA, and a `websockets`
process on 3000. The phone has to be told the Pi's IP through a form field.

This replaces both with a single aiohttp application:

- `GET /ws/{player_id}` upgrades to a websocket (validated against known IDs)
- `GET /healthz` returns plain text, for Pi boot checks
- everything else is served from `muse-pwa/dist/`, with `/` mapping to
  `index.html`

Because the page and the socket share an origin, the PWA derives its socket URL
from `window.location` and the IP and port fields disappear from the UI. One
certificate covers both, and there is no CORS or mixed-content surface.

### Why aiohttp

The current `websockets` library cannot serve static files without hand-rolling
an HTTP layer. aiohttp does static files, websocket upgrades, and TLS natively,
stays inside the existing Python stack, and needs one systemd unit. Caddy was
considered and rejected: it would solve HTTPS elegantly but adds a second
runtime, and its DNS-01 support requires provider-specific custom builds.

### TLS configuration

The server takes cert and key paths from `MUSE_TLS_CERT` and `MUSE_TLS_KEY`
(overridable by CLI flags). If neither is set it serves plain HTTP, which keeps
`localhost` development working unchanged. Setting exactly one is a
configuration error and fails loudly at startup rather than silently downgrading
to HTTP — a silent downgrade would produce the exact confusing Bluetooth failure
this whole design exists to prevent.

## Certificate lifecycle

`scripts/issue-cert.sh` wraps `acme.sh`, which supports essentially every DNS
provider through environment variables, so the choice of registrar does not
reach the application. It issues for `$MUSE_DOMAIN` over DNS-01 and installs the
result into `/etc/muse-brain/tls/`.

DNS-01 is required rather than HTTP-01 because the Pi is never publicly
reachable — the name resolves to a private address.

### The Burning Man detail

Certificates are the easy half. The load-bearing part is name resolution.

With no uplink, phones cannot reach public DNS, so a public A record is useless
on the playa. The GL.iNet router must answer for the hostname itself, via a
dnsmasq entry pointing the name at the Pi's LAN address. The public A record is
only a convenience for testing at home, where it makes the same URL work on the
house network.

Two consequences worth writing down now, because both are silent failures that
would surface at the worst possible time:

- The cert expires every 90 days and cannot be renewed without internet. It must
  be reissued shortly before departure, and a cert with more than 30 days left
  at departure is the target.
- Phones validate `notBefore`/`notAfter` against their own clock. A phone that
  has been offline long enough to drift, or that boots with a bad clock, will
  reject a valid certificate.

`scripts/preflight.sh` checks both: days remaining on the cert, and that the
hostname resolves to the expected address from the client's perspective.

## PWA changes

- Derive the socket URL from `window.location`, choosing `wss` on HTTPS pages
  and `ws` otherwise, so localhost development still works.
- Remove the now-meaningless host and port inputs. Keep the player seat select.
- Add a secure-context guard. When `window.isSecureContext` is false or
  `navigator.bluetooth` is missing, show a specific explanation instead of the
  current generic alert. This is the failure future-Dan is most likely to hit on
  a fresh device, and a clear message costs nothing.

## Error handling

Malformed websocket frames are already skipped rather than closing the
connection; that behavior carries over. A second connection claiming an
already-occupied player seat replaces the first, since the common cause is a
phone reconnecting after its screen slept and the stale socket is dead.
Startup fails loudly on unreadable cert files, a partial TLS configuration, or a
missing `dist/` directory.

## Testing

Server behavior is covered by pytest against `aiohttp.test_utils`: static files
serve, `/` returns the app shell, valid player IDs upgrade, unknown IDs are
rejected, score messages update hub state, malformed frames are ignored without
dropping the connection, and a reconnect displaces the stale socket.

TLS is tested by building a context from a throwaway self-signed certificate
generated in a fixture, so the suite never touches Let's Encrypt or the network.
Partial configuration is asserted to raise.

The hub logic is a plain class with no aiohttp imports, so its state transitions
are tested directly.

## Out of scope

Deliberately excluded to keep this reviewable: lifting the two-player cap, the
operator dashboard, session recording on the websocket path, and unifying the
focus metric between the Python and TypeScript implementations. These are the
next steps and each is independently useful.
