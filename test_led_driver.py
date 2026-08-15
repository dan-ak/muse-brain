import math

from led_driver import (
    DNRGB_MAX_PIXELS,
    WLED_REALTIME_PORT,
    FocusLight,
    Smoother,
    WledStrip,
    build_dnrgb_packets,
    solid,
)


class FakeClock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t


# --- smoothing ---------------------------------------------------------------


def test_first_sample_is_taken_as_is():
    # Starting from zero would make every session open with a slow fade up
    # from blue regardless of the wearer's actual state.
    clk = FakeClock(0.0)
    s = Smoother(tau_s=1.5, clock_fn=clk)
    assert s.update(0.8) == 0.8


def test_smoother_moves_toward_target_without_jumping():
    clk = FakeClock(0.0)
    s = Smoother(tau_s=1.5, clock_fn=clk)
    s.update(0.0)
    clk.t = 0.1
    out = s.update(1.0)
    assert 0.0 < out < 1.0


def test_smoother_reaches_one_time_constant_after_tau():
    clk = FakeClock(0.0)
    s = Smoother(tau_s=1.5, clock_fn=clk)
    s.update(0.0)
    clk.t = 1.5
    assert s.update(1.0) == round(1 - math.exp(-1.0), 6)


def test_smoother_converges_on_a_held_value():
    clk = FakeClock(0.0)
    s = Smoother(tau_s=1.5, clock_fn=clk)
    s.update(0.0)
    for i in range(1, 200):
        clk.t = i * 0.1
        out = s.update(1.0)
    assert out > 0.99


def test_smoother_resets_so_a_reconnect_does_not_fade_from_stale_state():
    clk = FakeClock(0.0)
    s = Smoother(tau_s=1.5, clock_fn=clk)
    s.update(1.0)
    s.reset()
    clk.t = 5.0
    assert s.update(0.2) == 0.2


# --- WLED DNRGB framing ------------------------------------------------------


def test_packet_carries_protocol_and_timeout_header():
    packets = build_dnrgb_packets(solid((255, 0, 0), 1), timeout_s=2)
    assert len(packets) == 1
    assert packets[0][:4] == bytes([4, 2, 0, 0])   # 4 = DNRGB, 3 would be DRGBW


def test_packet_repeats_the_colour_for_every_pixel():
    packets = build_dnrgb_packets(solid((10, 20, 30), 3), timeout_s=2)
    assert packets[0][4:] == bytes([10, 20, 30] * 3)


def test_long_strips_are_split_across_packets():
    packets = build_dnrgb_packets(solid((1, 2, 3), DNRGB_MAX_PIXELS + 10), timeout_s=2)
    assert len(packets) == 2
    assert len(packets[0][4:]) == DNRGB_MAX_PIXELS * 3
    assert len(packets[1][4:]) == 10 * 3


def test_continuation_packet_carries_its_start_index():
    packets = build_dnrgb_packets(solid((1, 2, 3), DNRGB_MAX_PIXELS + 1), timeout_s=2)
    start = (packets[1][2] << 8) | packets[1][3]
    assert start == DNRGB_MAX_PIXELS


def test_packet_fits_in_a_single_datagram():
    packets = build_dnrgb_packets(solid((1, 2, 3), 1000), timeout_s=2)
    for packet in packets:
        assert len(packet) <= 1472


def test_packets_carry_per_pixel_colours():
    pixels = [(1, 2, 3), (4, 5, 6), (7, 8, 9)]
    packets = build_dnrgb_packets(pixels, timeout_s=2)
    assert len(packets) == 1
    assert packets[0][:4] == bytes([4, 2, 0, 0])
    assert packets[0][4:] == bytes([1, 2, 3, 4, 5, 6, 7, 8, 9])


def test_solid_builds_a_uniform_pixel_list():
    assert solid((9, 8, 7), 3) == [(9, 8, 7), (9, 8, 7), (9, 8, 7)]


def test_per_pixel_run_splits_across_datagrams():
    pixels = [(1, 2, 3)] * (DNRGB_MAX_PIXELS + 10)
    packets = build_dnrgb_packets(pixels, timeout_s=2)
    assert len(packets) == 2
    assert len(packets[0][4:]) == DNRGB_MAX_PIXELS * 3
    assert (packets[1][2] << 8) | packets[1][3] == DNRGB_MAX_PIXELS


def test_show_pixels_sends_the_given_colours():
    sock = FakeSocket()
    strip = WledStrip("10.0.0.5", count=3, sock=sock)
    strip.show_pixels([(1, 2, 3), (4, 5, 6), (7, 8, 9)])
    payload, _ = sock.sent[0]
    assert payload[4:] == bytes([1, 2, 3, 4, 5, 6, 7, 8, 9])


def test_empty_pixel_list_sends_nothing():
    sock = FakeSocket()
    strip = WledStrip("10.0.0.5", count=0, sock=sock)
    assert strip.show_pixels([]) is True
    assert sock.sent == []


# --- the UDP sender ----------------------------------------------------------


class FakeSocket:
    def __init__(self, fail_with=None):
        self.sent = []
        self.closed = False
        self._fail_with = fail_with

    def sendto(self, payload, addr):
        if self._fail_with is not None:
            raise self._fail_with
        self.sent.append((payload, addr))

    def close(self):
        self.closed = True


def test_strip_sends_one_datagram_per_packet_to_the_controller():
    sock = FakeSocket()
    strip = WledStrip("10.0.0.5", count=3, sock=sock)
    strip.show((10, 20, 30))
    assert len(sock.sent) == 1
    payload, addr = sock.sent[0]
    assert addr == ("10.0.0.5", WLED_REALTIME_PORT)
    assert payload == bytes([4, 2, 0, 0]) + bytes([10, 20, 30] * 3)


def test_strip_splits_a_long_run_across_datagrams():
    sock = FakeSocket()
    strip = WledStrip("10.0.0.5", count=DNRGB_MAX_PIXELS + 1, sock=sock)
    strip.show((1, 2, 3))
    assert len(sock.sent) == 2


def test_release_stops_sending():
    sock = FakeSocket()
    strip = WledStrip("10.0.0.5", count=3, sock=sock)
    strip.release()
    assert sock.sent == []


def test_a_dead_controller_never_breaks_the_caller():
    # The whole reason this is fire-and-forget UDP: an unplugged or dusted-out
    # controller must not be able to raise into the broadcast loop and take the
    # EEG recording down with it.
    sock = FakeSocket(fail_with=OSError("network unreachable"))
    strip = WledStrip("10.0.0.5", count=3, sock=sock)
    strip.show((10, 20, 30))   # must not raise


def test_strip_reports_send_failures_without_raising():
    sock = FakeSocket(fail_with=OSError("network unreachable"))
    strip = WledStrip("10.0.0.5", count=3, sock=sock)
    assert strip.show((10, 20, 30)) is False
    assert strip.show((10, 20, 30)) is False


def test_one_failed_datagram_does_not_abandon_the_rest():
    # Stopping at the first failure would leave the head of a long strip on the
    # new colour and its tail on the old one until the next frame lands.
    class FlakyFirstSocket(FakeSocket):
        def sendto(self, payload, addr):
            if not self.sent and not getattr(self, "_failed", False):
                self._failed = True
                raise OSError("no buffer space")
            self.sent.append((payload, addr))

    sock = FlakyFirstSocket()
    strip = WledStrip("10.0.0.5", count=DNRGB_MAX_PIXELS + 1, sock=sock)
    assert strip.show((1, 2, 3)) is False
    assert len(sock.sent) == 1   # the second datagram still went out


def test_strip_reports_success():
    sock = FakeSocket()
    strip = WledStrip("10.0.0.5", count=3, sock=sock)
    assert strip.show((10, 20, 30)) is True


def test_closing_the_strip_closes_the_socket():
    sock = FakeSocket()
    strip = WledStrip("10.0.0.5", count=3, sock=sock)
    strip.close()
    assert sock.closed is True


# --- driving from a hub snapshot ---------------------------------------------


def _snapshot(**overrides):
    seat = {
        "id": "p1",
        "raw": 0.0,
        "normalized": 1.0,
        "calibrating": False,
        "connected": True,
        "stale": False,
    }
    seat.update(overrides)
    return {"seats": [seat], "recording": {"active": False, "label": None}}


class FakeStrip:
    def __init__(self):
        self.shown = []
        self.released = 0

    def show(self, rgb):
        self.shown.append(rgb)

    def release(self):
        self.released += 1


def test_drives_the_strip_from_the_watched_seat():
    strip, clk = FakeStrip(), FakeClock(0.0)
    light = FocusLight(strip, seat="p1", tau_s=1.5, clock_fn=clk)
    light.update(_snapshot(normalized=1.0))
    assert strip.shown == [(255, 0, 0)]


def test_a_relaxed_wearer_turns_the_strip_blue():
    strip, clk = FakeStrip(), FakeClock(0.0)
    light = FocusLight(strip, seat="p1", tau_s=1.5, clock_fn=clk)
    light.update(_snapshot(normalized=-1.0))
    assert strip.shown == [(0, 0, 255)]


def test_disconnected_seat_releases_the_strip():
    # Releasing rather than showing a neutral colour lets WLED's own realtime
    # timeout take the strip back to an ambient effect, which is a readable
    # signal that nobody is driving it.
    strip, clk = FakeStrip(), FakeClock(0.0)
    light = FocusLight(strip, seat="p1", tau_s=1.5, clock_fn=clk)
    light.update(_snapshot(connected=False))
    assert strip.shown == []
    assert strip.released == 1


def test_stale_seat_releases_the_strip():
    strip, clk = FakeStrip(), FakeClock(0.0)
    light = FocusLight(strip, seat="p1", tau_s=1.5, clock_fn=clk)
    light.update(_snapshot(stale=True))
    assert strip.shown == []
    assert strip.released == 1


def test_calibrating_seat_releases_the_strip():
    strip, clk = FakeStrip(), FakeClock(0.0)
    light = FocusLight(strip, seat="p1", tau_s=1.5, clock_fn=clk)
    light.update(_snapshot(calibrating=True))
    assert strip.shown == []
    assert strip.released == 1


def test_non_numeric_score_releases_the_strip():
    strip, clk = FakeStrip(), FakeClock(0.0)
    light = FocusLight(strip, seat="p1", tau_s=1.5, clock_fn=clk)
    light.update(_snapshot(normalized=None))
    assert strip.shown == []
    assert strip.released == 1


def test_missing_seat_releases_the_strip():
    strip, clk = FakeStrip(), FakeClock(0.0)
    light = FocusLight(strip, seat="p3", tau_s=1.5, clock_fn=clk)
    light.update(_snapshot())
    assert strip.shown == []
    assert strip.released == 1


def test_other_seats_do_not_drive_the_strip():
    strip, clk = FakeStrip(), FakeClock(0.0)
    light = FocusLight(strip, seat="p1", tau_s=1.5, clock_fn=clk)
    snap = _snapshot(normalized=1.0)
    snap["seats"].append({
        "id": "p2", "raw": 0.0, "normalized": 0.0, "calibrating": False,
        "connected": True, "stale": False,
    })
    light.update(snap)
    assert strip.shown == [(255, 0, 0)]


def test_reconnecting_does_not_fade_from_the_pre_dropout_colour():
    strip, clk = FakeStrip(), FakeClock(0.0)
    light = FocusLight(strip, seat="p1", tau_s=1.5, clock_fn=clk)
    light.update(_snapshot(normalized=1.0))       # red
    clk.t = 1.0
    light.update(_snapshot(connected=False))      # dropout
    clk.t = 2.0
    light.update(_snapshot(normalized=-1.0))      # back, and relaxed
    assert strip.shown[-1] == (0, 0, 255)


def test_smoothing_applies_across_successive_updates():
    strip, clk = FakeStrip(), FakeClock(0.0)
    light = FocusLight(strip, seat="p1", tau_s=1.5, clock_fn=clk)
    light.update(_snapshot(normalized=-1.0))
    clk.t = 0.1
    light.update(_snapshot(normalized=1.0))
    assert strip.shown[0] == (0, 0, 255)
    assert strip.shown[1] != (255, 0, 0)   # eased, not snapped
