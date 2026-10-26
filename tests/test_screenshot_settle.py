import json
import sys
from pathlib import Path

import pytest

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from plugins.screenshot_settle import cdp_screenshot
from plugins.screenshot_settle.screenshot_settle import ScreenshotSettle


class TestParseSettleSeconds:

    @pytest.mark.parametrize("raw,expected", [
        (None, 5),
        ("", 5),
        ("5", 5),
        ("2.5", 2.5),
        ("0", 0),
        ("-3", 5),
        ("not-a-number", 5),
        ("60", 30),
    ])
    def test_values(self, raw, expected):
        assert ScreenshotSettle._parse_settle_seconds(raw) == expected

    def test_nan_falls_back_to_default(self):
        assert ScreenshotSettle._parse_settle_seconds("nan") == 5


def test_missing_browser_returns_no_image(monkeypatch):
    monkeypatch.setattr(cdp_screenshot, "find_browser", lambda: None)
    assert cdp_screenshot.take_settled_screenshot("http://example.test/", (800, 480)) is None


def test_custom_css_is_encoded_safely():
    css = 'a::after { content: "</style>\\n\\t"; }\n/* comment */'
    script = cdp_screenshot._custom_css_script(css)
    encoded = script.split("var css = ", 1)[1].split(";\n", 1)[0]
    assert json.loads(encoded) == css
    assert cdp_screenshot.STYLE_ELEMENT_ID in script