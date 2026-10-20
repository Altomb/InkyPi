from plugins.base_plugin.base_plugin import BasePlugin
from utils.image_utils import take_screenshot_with_cookies
import logging

logger = logging.getLogger(__name__)

# Values (from HTML checkboxes) that should be treated as boolean True.
TRUE_VALUES = {"true", "1", "yes", "on"}


class AuthScreenshot(BasePlugin):
    def generate_image(self, settings, device_config):

        url = settings.get('url')
        if not url:
            raise RuntimeError("URL is required.")

        dimensions = device_config.get_resolution()
        if device_config.get_config("orientation") == "vertical":
            dimensions = dimensions[::-1]

        cookies = self._parse_cookies(
            settings.get('cookie-name[]'),
            settings.get('cookie-value[]'),
            settings.get('cookie-domain[]'),
            settings.get('cookie-path[]'),
            settings.get('cookie-secure[]'),
            settings.get('cookie-httponly[]'),
        )

        logger.info(f"Taking screenshot of url: {url} with {len(cookies)} cookie(s)")

        image = take_screenshot_with_cookies(url, dimensions, cookies=cookies, timeout_ms=40000)

        if not image:
            raise RuntimeError("Failed to take screenshot, please check logs.")

        return image

    @staticmethod
    def _parse_cookies(names, values, domains, paths, secures, httponly):
        """Build a list of cookie dicts from the parallel form arrays.

        Empty rows (no name) are skipped. When the same name/domain/path pair is
        given more than once, the last occurrence wins.
        """
        names = names or []
        values = values or []
        domains = domains or []
        paths = paths or []
        secures = secures or []
        httponly = httponly or []

        cookies = {}
        for index, raw_name in enumerate(names):
            name = (raw_name or "").strip()
            if not name:
                continue

            domain = (AuthScreenshot._get(domains, index) or "").strip()
            path = (AuthScreenshot._get(paths, index) or "").strip() or "/"
            key = (name, domain.lower(), path)

            cookies[key] = {
                "name": name,
                "value": AuthScreenshot._get(values, index, ""),
                "domain": domain or None,
                "path": path,
                "secure": AuthScreenshot._is_truthy(AuthScreenshot._get(secures, index)),
                "httpOnly": AuthScreenshot._is_truthy(AuthScreenshot._get(httponly, index)),
                "sameSite": "Lax",
            }

        return list(cookies.values())

    @staticmethod
    def _get(items, index, default=None):
        if index < len(items):
            return items[index]
        return default

    @staticmethod
    def _is_truthy(value):
        return str(value).strip().lower() in TRUE_VALUES
