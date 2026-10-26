from plugins.base_plugin.base_plugin import BasePlugin
from plugins.screenshot_settle.cdp_screenshot import take_settled_screenshot
import logging

logger = logging.getLogger(__name__)

DEFAULT_SETTLE_SECONDS = 5
MAX_SETTLE_SECONDS = 30


class ScreenshotSettle(BasePlugin):
    """Screenshot plugin for pages that render their content with JavaScript.

    The stock Screenshot plugin captures as soon as the page has loaded, so a
    client-rendered page is still showing its loading state. This plugin waits
    for the content to settle first; the stock plugin and its behaviour are
    unchanged.
    """

    def generate_image(self, settings, device_config):

        url = settings.get('url')
        if not url:
            raise RuntimeError("URL is required.")

        dimensions = device_config.get_resolution()
        if device_config.get_config("orientation") == "vertical":
            dimensions = dimensions[::-1]

        settle_seconds = self._parse_settle_seconds(settings.get('settle_seconds'))
        logger.info(f"Taking screenshot of url: {url} (settling {settle_seconds:g}s)")

        image = take_settled_screenshot(url, dimensions, settle_seconds=settle_seconds)

        if not image:
            raise RuntimeError("Failed to take screenshot, please check logs.")

        return image

    @staticmethod
    def _parse_settle_seconds(raw):
        """Return a valid settle time, falling back to the default.

        The form posts a string; an empty value means "use the default". A
        value of 0 is honoured so the plugin can behave like the stock one.
        """
        if raw is None or raw == '':
            return DEFAULT_SETTLE_SECONDS

        try:
            value = float(raw)
        except (TypeError, ValueError):
            return DEFAULT_SETTLE_SECONDS

        if value != value or value < 0:  # NaN or negative
            return DEFAULT_SETTLE_SECONDS

        return min(value, MAX_SETTLE_SECONDS)
