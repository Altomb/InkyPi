import requests
from PIL import Image, ImageEnhance, ImageOps, ImageFilter
from io import BytesIO
import os
import base64
import json
import logging
import hashlib
import socket
import struct
import tempfile
import subprocess
import shutil
import time
from urllib.parse import urlparse
from urllib.request import urlopen

logger = logging.getLogger(__name__)

def get_image(image_url):
    response = requests.get(image_url, timeout=30)
    img = None
    if 200 <= response.status_code < 300 or response.status_code == 304:
        img = Image.open(BytesIO(response.content))
    else:
        logger.error(f"Received non-200 response from {image_url}: status_code: {response.status_code}")
    return img

def change_orientation(image, orientation, inverted=False):
    if orientation == 'horizontal':
        angle = 0
    elif orientation == 'vertical':
        angle = 90

    if inverted:
        angle = (angle + 180) % 360

    return image.rotate(angle, expand=1)

def resize_image(image, desired_size, image_settings=[]):
    img_width, img_height = image.size
    desired_width, desired_height = desired_size
    desired_width, desired_height = int(desired_width), int(desired_height)

    img_ratio = img_width / img_height
    desired_ratio = desired_width / desired_height

    keep_width = "keep-width" in image_settings

    x_offset, y_offset = 0,0
    new_width, new_height = img_width,img_height
    # Step 1: Determine crop dimensions
    desired_ratio = desired_width / desired_height
    if img_ratio > desired_ratio:
        # Image is wider than desired aspect ratio
        new_width = int(img_height * desired_ratio)
        if not keep_width:
            x_offset = (img_width - new_width) // 2
    else:
        # Image is taller than desired aspect ratio
        new_height = int(img_width / desired_ratio)
        if not keep_width:
            y_offset = (img_height - new_height) // 2

    # Step 2: Crop the image
    image = image.crop((x_offset, y_offset, x_offset + new_width, y_offset + new_height))

    # Step 3: Resize to the exact desired dimensions (if necessary)
    return image.resize((desired_width, desired_height), Image.LANCZOS)

def apply_image_enhancement(img, image_settings={}):
    # Convert image to RGB mode if necessary for enhancement operations
    # ImageEnhance requires RGB mode for operations like blend
    if img.mode not in ('RGB', 'L'):
        img = img.convert('RGB')

    # Each enhance() call blends over the full image, so skip factors of 1.0
    # (the no-op default) to avoid wasted full-frame passes on low-resource devices.
    enhancements = [
        (ImageEnhance.Brightness, image_settings.get("brightness", 1.0)),
        (ImageEnhance.Contrast, image_settings.get("contrast", 1.0)),
        (ImageEnhance.Color, image_settings.get("saturation", 1.0)),
        (ImageEnhance.Sharpness, image_settings.get("sharpness", 1.0)),
    ]
    for enhancer, factor in enhancements:
        if factor != 1.0:
            img = enhancer(img).enhance(factor)

    return img

def compute_image_hash(image):
    """Compute SHA-256 hash of an image."""
    image = image.convert("RGB")
    img_bytes = image.tobytes()
    return hashlib.sha256(img_bytes).hexdigest()

def take_screenshot_html(html_str, dimensions, timeout_ms=None):
    image = None
    try:
        # Create a temporary HTML file
        with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as html_file:
            html_file.write(html_str.encode("utf-8"))
            html_file_path = html_file.name

        image = take_screenshot(html_file_path, dimensions, timeout_ms)

        # Remove html file
        os.remove(html_file_path)

    except Exception as e:
        logger.error(f"Failed to take screenshot: {str(e)}")

    return image

def _find_chromium_binary():
    """Find the first available Chromium-based binary in system PATH."""
    candidates = ["chromium-headless-shell", "chromium", "chrome"]
    for candidate in candidates:
        path = shutil.which(candidate)
        if path:
            logger.debug(f"Found browser binary: {candidate} at {path}")
            return candidate
    return None


def take_screenshot(target, dimensions, timeout_ms=None):
    image = None
    try:
        # Find available browser binary
        browser = _find_chromium_binary()
        if not browser:
            logger.error("No Chromium-based browser found. Install chromium, chromium-headless-shell, or chrome.")
            return None

        # Create a temporary output file for the screenshot
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as img_file:
            img_file_path = img_file.name

        command = [
            browser,
            target,
            "--headless",
            f"--screenshot={img_file_path}",
            f"--window-size={dimensions[0]},{dimensions[1]}",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--use-gl=swiftshader",
            "--hide-scrollbars",
            "--in-process-gpu",
            "--js-flags=--jitless",
            "--disable-zero-copy",
            "--disable-gpu-memory-buffer-compositor-resources",
            "--disable-extensions",
            "--disable-plugins",
            "--mute-audio",
            "--renderer-process-limit=1",
            "--no-zygote",
            "--no-sandbox"
        ]
        if timeout_ms:
            command.append(f"--timeout={timeout_ms}")
        result = subprocess.run(command, capture_output=True, check=False)

        # Check if the process failed or the output file is missing
        if result.returncode != 0 or not os.path.exists(img_file_path):
            logger.error(f"Failed to take screenshot (return code: {result.returncode})")
            return None

        # Load the image using PIL
        with Image.open(img_file_path) as img:
            image = img.copy()

        # Remove image files
        os.remove(img_file_path)

    except Exception as e:
        logger.error(f"Failed to take screenshot: {str(e)}")

    return image


def _free_tcp_port():
    """Return an available local TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _DevToolsSocket:
    """Minimal WebSocket client for the Chrome DevTools Protocol.

    Implements just enough of RFC 6455 (text frames, client masking, fragmentation
    on read) to issue CDP commands. This avoids adding a third-party dependency.
    """

    def __init__(self, url, timeout=30):
        parsed = urlparse(url)
        host = parsed.hostname
        port = parsed.port or 80
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"

        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        self._next_id = 1

        key = base64.b64encode(os.urandom(16)).decode("ascii")
        handshake = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        self.sock.sendall(handshake.encode("ascii"))
        self._read_handshake()

    def _read_handshake(self):
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("DevTools websocket closed during handshake")
            data += chunk
        status_line = data.split(b"\r\n", 1)[0].decode("latin-1")
        if "101" not in status_line:
            raise RuntimeError(f"DevTools websocket handshake failed: {status_line}")

    def _send_frame(self, payload):
        data = payload.encode("utf-8")
        header = bytearray()
        header.append(0x81)  # FIN + text opcode
        mask = os.urandom(4)
        length = len(data)
        if length < 126:
            header.append(0x80 | length)
        elif length < (1 << 16):
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        header.extend(mask)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.sendall(bytes(header) + masked)

    def _recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise RuntimeError("DevTools websocket closed")
            buf += chunk
        return buf

    def _recv_frame(self):
        first = self._recv_exact(2)
        opcode = first[0] & 0x0F
        length = first[1] & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._recv_exact(8))[0]
        payload = self._recv_exact(length) if length else b""
        return opcode, payload

    def command(self, method, params=None):
        """Send a CDP command and return the matching result, skipping events."""
        message_id = self._next_id
        self._next_id += 1
        self._send_frame(json.dumps({"id": message_id, "method": method, "params": params or {}}))

        while True:
            opcode, payload = self._recv_frame()
            if opcode == 0x8:  # close
                raise RuntimeError("DevTools websocket closed by browser")
            if opcode == 0x9:  # ping -> pong
                self.sock.sendall(b"\x8a\x80" + os.urandom(4))
                continue
            if opcode not in (0x1, 0x0):  # ignore binary/continuation
                continue
            try:
                message = json.loads(payload.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if message.get("id") == message_id:
                if "error" in message:
                    raise RuntimeError(f"CDP {method} failed: {message['error']}")
                return message.get("result", {})

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


def take_screenshot_with_cookies(target, dimensions, cookies=None, timeout_ms=None):
    """Take a screenshot of a URL, optionally authenticating with cookies.

    Cookies are injected through the Chrome DevTools Protocol so that HttpOnly
    and Secure cookies are supported on any recent Chromium version.

    Args:
        target: The URL to screenshot.
        dimensions: A (width, height) tuple for the viewport.
        cookies: Optional list of dicts with keys: name, value, and optionally
            domain, path, secure, httpOnly, sameSite, url. When domain/url are
            omitted, they are derived from the target URL.
        timeout_ms: Optional maximum time in milliseconds to wait for the page.

    Returns:
        A PIL.Image or None if the screenshot could not be taken.
    """
    if not cookies:
        return take_screenshot(target, dimensions, timeout_ms=timeout_ms)

    browser = _find_chromium_binary()
    if not browser:
        logger.error("No Chromium-based browser found. Install chromium, chromium-headless-shell, or chrome.")
        return None

    user_data_dir = tempfile.mkdtemp(prefix="inkypi_screenshot_")
    port = _free_tcp_port()
    command = [
        browser,
        "--headless=new",
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data_dir}",
        f"--window-size={dimensions[0]},{dimensions[1]}",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--use-gl=swiftshader",
        "--hide-scrollbars",
        "--in-process-gpu",
        "--js-flags=--jitless",
        "--disable-zero-copy",
        "--disable-gpu-memory-buffer-compositor-resources",
        "--disable-extensions",
        "--disable-plugins",
        "--mute-audio",
        "--renderer-process-limit=1",
        "--no-zygote",
        "--no-sandbox",
        "about:blank",
    ]

    process = None
    ws = None
    image = None
    try:
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        ws_url = _wait_for_devtools_url(port, timeout=30)
        if not ws_url:
            logger.error("Timed out waiting for the DevTools endpoint to become available.")
            return take_screenshot(target, dimensions, timeout_ms=timeout_ms)

        ws = _DevToolsSocket(ws_url, timeout=30)
        ws.command("Page.enable")
        ws.command("Network.enable")

        # Force the viewport to the requested dimensions so the capture matches
        # the display resolution regardless of window chrome/scrollbars.
        ws.command("Emulation.setDeviceMetricsOverride", {
            "width": dimensions[0],
            "height": dimensions[1],
            "deviceScaleFactor": 1,
            "mobile": False,
        })

        for cookie in _normalize_cookies(target, cookies):
            ws.command("Network.setCookie", cookie)

        load_timeout = (timeout_ms / 1000) if timeout_ms else 40
        nav = ws.command("Page.navigate", {"url": target})

        _wait_for_load(ws, nav.get("loaderId"), load_timeout)
        # Give late client-side rendering a brief moment to settle.
        time.sleep(0.5)

        result = ws.command(
            "Page.captureScreenshot",
            {"format": "png", "captureBeyondViewport": False},
        )
        data = result.get("data")
        if data:
            with Image.open(BytesIO(base64.b64decode(data))) as img:
                image = img.copy()
    except Exception as e:
        logger.error(f"Failed to take screenshot with cookies: {str(e)}")

    finally:
        if ws:
            ws.close()
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
        shutil.rmtree(user_data_dir, ignore_errors=True)

    return image


def _wait_for_devtools_url(port, timeout=30):
    """Poll the DevTools JSON endpoint until a page websocket URL is available."""
    deadline = time.time() + timeout
    endpoint = f"http://127.0.0.1:{port}/json"
    while time.time() < deadline:
        try:
            with urlopen(endpoint, timeout=2) as response:
                targets = json.loads(response.read().decode("utf-8"))
            for target in targets:
                if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
                    return target["webSocketDebuggerUrl"]
        except Exception:
            pass
        time.sleep(0.2)
    return None


def _wait_for_load(ws, loader_id, timeout=40):
    """Wait for the page identified by loader_id to finish loading."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            opcode, payload = ws._recv_frame()
        except socket.timeout:
            break
        if opcode not in (0x1, 0x0):
            continue
        try:
            message = json.loads(payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
        if message.get("method") == "Page.loadEventFired":
            return True
    return False


def _normalize_cookies(target, cookies):
    """Fill in sensible defaults so each cookie is valid for Network.setCookie."""
    parsed = urlparse(target)
    default_domain = parsed.hostname
    default_secure = parsed.scheme == "https"

    normalized = []
    for cookie in cookies:
        name = (cookie.get("name") or "").strip()
        if not name:
            continue

        entry = {
            "name": name,
            "value": cookie.get("value", ""),
            "path": cookie.get("path") or "/",
        }

        domain = cookie.get("domain")
        if domain:
            entry["domain"] = domain.lstrip(".")
            entry["url"] = f"{parsed.scheme}://{domain.lstrip('.')}/"
        elif default_domain:
            entry["url"] = f"{parsed.scheme}://{default_domain}{entry['path']}"

        entry["secure"] = cookie.get("secure", default_secure)
        entry["httpOnly"] = cookie.get("httpOnly", False)
        same_site = cookie.get("sameSite")
        if same_site in ("Strict", "Lax", "None"):
            entry["sameSite"] = same_site
        normalized.append(entry)

    return normalized


def pad_image_blur(img: Image, dimensions: tuple[int, int]) -> Image:
    bkg = ImageOps.fit(img, dimensions)
    bkg = bkg.filter(ImageFilter.BoxBlur(8))
    img = ImageOps.contain(img, dimensions)

    img_size = img.size
    bkg.paste(img, ((dimensions[0] - img_size[0]) // 2, (dimensions[1] - img_size[1]) // 2))
    return bkg
