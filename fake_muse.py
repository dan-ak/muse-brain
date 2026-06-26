"""Synthetic OSC client that mimics Mind Monitor for smoke-testing the visualizer.

Sends a 10 Hz sinusoid (alpha-band) plus pink-ish noise on /muse/eeg at 256 Hz,
and a slow circular gyro pattern on /muse/gyro.
"""

import argparse
import math
import time

import numpy as np
from pythonosc.udp_client import SimpleUDPClient

SAMPLE_RATE = 256


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args()
    client = SimpleUDPClient(args.host, args.port)
    print(f"Streaming fake Muse data to {args.host}:{args.port}. Ctrl-C to stop.")

    period = 1.0 / SAMPLE_RATE
    rng = np.random.default_rng(0)
    next_t = time.perf_counter()
    t = 0.0
    i = 0
    while True:
        # 10 Hz alpha + 20 Hz beta + 1/f-ish noise across 4 channels.
        base = 30 * math.sin(2 * math.pi * 10 * t) + 12 * math.sin(2 * math.pi * 20 * t)
        ch = [base + rng.normal(0, 8) for _ in range(4)]
        client.send_message("/muse/eeg", ch)

        if i % 32 == 0:  # 8 Hz gyro + accelerometer updates
            gx = 50 * math.sin(2 * math.pi * 0.2 * t)
            gy = 50 * math.cos(2 * math.pi * 0.2 * t)
            client.send_message("/muse/gyro", [gx, gy, 0.0])
            ax = 0.02 * math.sin(2 * math.pi * 0.2 * t)
            ay = 0.02 * math.cos(2 * math.pi * 0.2 * t)
            client.send_message("/muse/acc", [ax, ay, 1.0])

        if i % 256 == 0:  # 1 Hz band-power elements
            theta = -0.5 + 0.2 * math.sin(2 * math.pi * 0.1 * t)
            beta = -0.8 + 0.2 * math.cos(2 * math.pi * 0.1 * t)
            client.send_message("/muse/elements/theta_absolute", [theta] * 4)
            client.send_message("/muse/elements/beta_absolute", [beta] * 4)

        i += 1
        t += period
        next_t += period
        sleep = next_t - time.perf_counter()
        if sleep > 0:
            time.sleep(sleep)


if __name__ == "__main__":
    main()
