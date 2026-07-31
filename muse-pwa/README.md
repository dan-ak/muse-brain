# muse-pwa

The phone app. Pairs a Muse headband over Web Bluetooth, computes a focus score
in the browser, and streams both that and the raw signal to the Pi.

See the [root README](../README.md) for the whole system.

```bash
npm ci
npm run build      # output goes to dist/, which the Pi serves
npm run dev        # port 3001, proxying /ws and /api to a local server.py
```

## Things that will surprise you

**Web Bluetooth needs a secure context.** Over plain HTTP from a LAN address
`navigator.bluetooth` is simply undefined and the pair button cannot work.
`localhost` is exempt, which is why development works without a certificate.

**Android and Chrome only.** iOS has no Web Bluetooth in any browser.

**`web-muse` is patched.** Three upstream bugs are fixed in
`patches/web-muse+1.0.0.patch`, applied automatically on `npm ci`: an unawaited
`startNotifications()` that made pairing fail intermittently, a disconnect
handler that never fired, and a hard requirement for PPG characteristics that
the 2016 Muse does not have. Do not install in a way that skips `postinstall`,
or pairing will start failing again.

**A backgrounded tab is throttled to ~1 Hz.** Browsers clamp timers in hidden
tabs, so a locked screen quietly drops streaming from 30 Hz to 1 Hz and loses
raw samples. The client holds a screen wake lock while streaming, and the
server reports a seat as throttled or stale rather than pretending it is fine.

## Layout

| Path | |
|---|---|
| `src/App.tsx` | the player view: pairing, fit status, calibration, streaming |
| `src/Dashboard.tsx` | operator view at `/dashboard` — all seats, recording control |
| `src/CuedProtocol.tsx` | the 2×2 cued task with audio cues |
| `src/utils/dsp.ts` | FFT, band powers, mains-hum detection |
| `src/utils/protocol.ts` | trial sequence generation, pure and testable |
| `src/utils/cueAudio.ts` | tones and speech |
| `src/serverApi.ts` | same-origin socket URLs and session control |
