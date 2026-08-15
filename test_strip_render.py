from strip_render import (
    CONCENTRATED,
    OFF,
    RELAXED,
    focus_to_rgb,
    render_solo,
    scale,
)


def test_relaxed_is_blue():
    assert focus_to_rgb(-1.0) == RELAXED


def test_concentrated_is_red():
    assert focus_to_rgb(1.0) == CONCENTRATED


def test_baseline_is_magenta():
    assert focus_to_rgb(0.0) == (255, 0, 255)


def test_scale_dims_every_channel():
    assert scale((200, 100, 50), 0.5) == (100, 50, 25)


def test_scale_to_zero_is_off():
    assert scale((255, 255, 255), 0.0) == OFF


def test_scale_never_exceeds_a_byte():
    assert scale((255, 255, 255), 1.0) == (255, 255, 255)


def test_solo_paints_every_pixel_the_same():
    pixels = render_solo(1.0, 5)
    assert pixels == [CONCENTRATED] * 5


def test_solo_length_matches_the_strip():
    assert len(render_solo(0.0, 144)) == 144


def test_solo_with_no_pixels_is_empty():
    assert render_solo(0.0, 0) == []


def test_ramp_never_dims_in_the_middle():
    # The reason for interpolating around the hue circle instead of lerping RGB:
    # a straight blue->red lerp passes through (127, 0, 127), so the middle of
    # the scale reads as "the lights are broken" rather than as a middle value.
    for i in range(-100, 101):
        assert max(focus_to_rgb(i / 100.0)) == 255


def test_ramp_has_no_green():
    # Hue 240..360 is the blue->magenta->red arc. Any green means the
    # interpolation went the wrong way round the circle and we get a rainbow.
    for i in range(-100, 101):
        assert focus_to_rgb(i / 100.0)[1] == 0


def test_ramp_is_monotonic_in_red():
    reds = [focus_to_rgb(i / 100.0)[0] for i in range(-100, 101)]
    assert reds == sorted(reds)


def test_out_of_range_scores_are_clamped():
    assert focus_to_rgb(-1.5) == focus_to_rgb(-1.0)
    assert focus_to_rgb(1.5) == focus_to_rgb(1.0)
