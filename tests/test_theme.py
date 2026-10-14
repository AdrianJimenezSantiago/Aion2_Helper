import pytest

from aion2_helper import theme as T


def test_mix_and_fade():
    assert T.mix("#000000", "#ffffff", 0.5) == "#808080"
    assert T.mix("#102030", "#ffffff", -1) == "#102030"
    assert T.mix("#102030", "#ffffff", 2) == "#ffffff"
    assert T.fade("#ff0000", 1.0, "#000000") == "#ff0000"
    assert T.fade("#ff0000", 0.0, "#000000") == "#000000"


def test_pulse_and_ease_stay_in_range():
    for i in range(50):
        assert 0.2 <= T.pulse(i * 0.07, 1.0, 0.2, 0.8) <= 0.8
    assert T.ease_out(-1) == 0 and T.ease_out(2) == 1 and 0.5 < T.ease_out(0.3) < 1


def test_polar_is_north_up_clockwise():
    assert T.polar(0, 0, 10, 0) == pytest.approx((0, -10))
    assert T.polar(0, 0, 10, 90) == pytest.approx((10, 0))
    assert T.polar(0, 0, 10, 180) == pytest.approx((0, 10))


def test_arrow_points_at_its_bearing():
    pts = T.arrow(0, 0, 90, 10)
    assert (pts[0], pts[1]) == pytest.approx((10, 0))  # the tip, to the east
    assert len(pts) == 8


def test_rounded_rect_clamps_radius():
    pts = T.rounded_rect(0, 0, 10, 4, 50)
    assert len(pts) == 24 and min(pts[0::2]) == 0 and max(pts[1::2]) == 4


def test_compass_and_angles():
    assert [T.compass(d) for d in (0, 44, 46, 180, 359, -10)] == ["N", "NE", "NE", "S", "N", "N"]
    assert T.angle_diff(350, 10) == 20 and T.angle_diff(10, 350) == 340


def test_formatting():
    assert T.fmt_distance(None) == "?" and T.fmt_distance(42.4) == "42 m" and T.fmt_distance(1520) == "1.5 km"
    assert [T.fmt_age(s) for s in (5, 75, 7300)] == ["5s", "1min", "2h"]
    assert T.height_hint(12.2) == "▲12" and T.height_hint(-6) == "▼6" and T.height_hint(2) == ""


def test_zoom_steps():
    assert T.zoom(60, 1) == 90 and T.zoom(60, -1) == 45
    assert T.zoom(30, -1) == 30 and T.zoom(240, 3) == 240
    assert T.zoom(70, 0) == 60  # an odd range snaps to the nearest step


def test_cube_colors_and_fonts():
    assert T.cube_color("key") == T.KEY and T.cube_color("free") == T.FREE and T.cube_color(None) == T.UNKNOWN
    fonts = T.pick_fonts({"DejaVu Sans", "DejaVu Sans Mono"})
    assert fonts["body"][0] == "DejaVu Sans" and fonts["mono"][0] == "DejaVu Sans Mono"
    assert T.pick_fonts(set())["title"][0] == "TkDefaultFont"
