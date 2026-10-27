import json
import os
import signal
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


class FakeProcess:
    pid = 4321

    def __init__(self):
        self.waits = 0

    def wait(self, timeout=None):
        self.waits += 1
        return 0


class TestStopBrowser:

    def _prepare(self, monkeypatch, running):
        signals = []
        monkeypatch.setattr(cdp_screenshot.os, "killpg", lambda pgid, sig: signals.append(sig))
        monkeypatch.setattr(cdp_screenshot, "_process_is_running", lambda pid: running)
        monkeypatch.setattr(cdp_screenshot.time, "sleep", lambda seconds: None)
        # Make the graceful wait return immediately.
        clock = iter(range(0, 1000, 10))
        monkeypatch.setattr(cdp_screenshot.time, "time", lambda: next(clock))
        return signals

    def test_graceful_exit_still_clears_group(self, monkeypatch):
        signals = self._prepare(monkeypatch, running=False)
        process = FakeProcess()

        cdp_screenshot._stop_browser(process, 1234)

        # No SIGTERM needed, but the group is cleared before reaping.
        assert signals == [signal.SIGKILL]
        assert process.waits == 1

    def test_hung_browser_gets_sigterm_then_sigkill(self, monkeypatch):
        signals = self._prepare(monkeypatch, running=True)
        process = FakeProcess()

        cdp_screenshot._stop_browser(process, 1234)

        assert signals == [signal.SIGTERM, signal.SIGKILL]
        assert process.waits == 1


class TestProcessIsRunning:

    def test_current_process_is_running(self):
        assert cdp_screenshot._process_is_running(os.getpid())

    def test_unused_pid_is_not_running(self):
        assert not cdp_screenshot._process_is_running(2 ** 22 + 12345)