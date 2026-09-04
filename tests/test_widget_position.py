from __future__ import annotations

import locale
import os
import runpy
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))


def _load_widget_module() -> dict[str, object]:
    """Load the widget script without leaking state into the rest of the suite.

    Two side effects have to be contained: the module reads the runtime settings, so
    it is pointed at a scratch directory instead of the user's real cache and config;
    and importing GTK calls setlocale(LC_ALL, ""), which would otherwise switch date
    formatting away from the "C" locale that the other test modules rely on.
    """
    with tempfile.TemporaryDirectory() as scratch:
        previous = {key: os.environ.get(key) for key in ("AI_STATUS_CONFIG_DIR", "AI_STATUS_CACHE_DIR")}
        previous_locale = locale.setlocale(locale.LC_ALL)
        os.environ["AI_STATUS_CONFIG_DIR"] = str(Path(scratch) / "config")
        os.environ["AI_STATUS_CACHE_DIR"] = str(Path(scratch) / "cache")
        try:
            return runpy.run_path(str(ROOT / "bin" / "ai-agent-status-widget"), run_name="widget_position_test")
        finally:
            locale.setlocale(locale.LC_ALL, previous_locale)
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


MODULE = _load_widget_module()
StatusWidget = MODULE["StatusWidget"]
Gdk = MODULE["Gdk"]
CARD_WIDTH = MODULE["CARD_WIDTH"]


def rect(x: int, y: int, width: int, height: int) -> "Gdk.Rectangle":
    area = Gdk.Rectangle()
    area.x, area.y, area.width, area.height = x, y, width, height
    return area


# A laptop panel parked below and to the right of two external screens: the virtual
# screen spans 5120x2520, but its bottom-left corner belongs to no monitor at all.
L_SHAPED_LAYOUT = [
    rect(0, 0, 2560, 1440),
    rect(2560, 0, 2560, 1440),
    rect(1248, 1440, 1920, 1080),
]


class _Stub:
    """Stands in for the window so the clamp can be tested without a display."""

    def __init__(self, areas: list["Gdk.Rectangle"]) -> None:
        self.areas = areas

    def monitor_workareas(self) -> list["Gdk.Rectangle"]:
        return self.areas

    clamp_to_visible = StatusWidget.clamp_to_visible


def on_a_monitor(x: int, y: int, areas: list["Gdk.Rectangle"]) -> bool:
    return any(a.x <= x < a.x + a.width and a.y <= y < a.y + a.height for a in areas)


class ClampToVisibleTests(unittest.TestCase):
    def clamp(self, x: int, y: int, areas: list["Gdk.Rectangle"] | None = None) -> tuple[int, int]:
        return _Stub(L_SHAPED_LAYOUT if areas is None else areas).clamp_to_visible(x, y)

    def test_position_on_a_monitor_is_left_alone(self) -> None:
        for x, y in ((40, 80), (4752, 0), (2000, 2000)):
            with self.subTest(position=(x, y)):
                self.assertEqual(self.clamp(x, y), (x, y))

    def test_dead_zone_of_an_l_shaped_layout_is_rescued(self) -> None:
        # Saved while the laptop sat further left; now inside the virtual screen but
        # on no physical panel, which used to leave the widget running yet invisible.
        rescued = self.clamp(460, 2211)
        self.assertNotEqual(rescued, (460, 2211))
        self.assertTrue(on_a_monitor(*rescued, L_SHAPED_LAYOUT))

    def test_position_beyond_every_monitor_is_rescued(self) -> None:
        for x, y in ((9000, 9000), (-500, -500), (0, 2519)):
            with self.subTest(position=(x, y)):
                self.assertTrue(on_a_monitor(*self.clamp(x, y), L_SHAPED_LAYOUT))

    def test_rescue_picks_the_nearest_monitor(self) -> None:
        # Just below the left screen, so the widget should come back on the left one.
        x, _ = self.clamp(460, 2211)
        self.assertLess(x, 2560)
        # Just right of the right screen: the rescue must not jump across the desk.
        x, _ = self.clamp(5300, 700)
        self.assertGreaterEqual(x, 2560)

    def test_rescued_window_keeps_its_width_on_screen(self) -> None:
        x, _ = self.clamp(9000, 9000)
        self.assertLessEqual(x + CARD_WIDTH, 5120)

    def test_no_reported_monitors_leaves_the_position_untouched(self) -> None:
        self.assertEqual(self.clamp(460, 2211, areas=[]), (460, 2211))


if __name__ == "__main__":
    unittest.main()
