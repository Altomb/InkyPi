"""Capture a page with Chrome DevTools so client-rendered content can settle.

Chromium's ``--screenshot`` flag captures as soon as the page has loaded, which
is too early for pages that fetch their data with JavaScript. Waiting through
the DevTools Protocol gives a real wall-clock wait without changing the stock
``utils.image_utils.take_screenshot`` used by the Screenshot plugin.

Dependencies are limited to the standard library and Pillow (already required
by InkyPi).
"""

import base64
import json
import logging
import os
import shutil
import socket
import struct
import subprocess
import tempfile
import time
from io import BytesIO
from urllib.parse import urlparse
from urllib.request import urlopen

from PIL import Image

logger = logging.getLogger(__name__)

BROWSER_CANDIDATES = ("chromium-headless-shell", "chromium", "chrome")
DEVTOOLS_START_TIMEOUT = 30
NAVIGATION_TIMEOUT = 40


def find_browser():
    """Return the first Chromium-based binary available on PATH."""
    for candidate in BROWSER_CANDIDATES:
        if shutil.which(candidate):
            return candidate
    return None


def _free_tcp_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _DevToolsSocket:
    """Minimal WebSocket client for the DevTools Protocol.

    Implements just enough of RFC 6455 to send commands and read replies:
    client masking, fragmented messages, ping/pong and close handling.
    """

    def __init__(self, url, timeout=NAVIGATION_TIMEOUT):
        parsed = urlparse(url)
        self.sock = socket.create_connection((parsed.hostname, parsed.port), timeout=timeout)
        self.sock.settimeout(timeout)
        self._buffer = b""
        self._events = []
        self._next_id = 1

        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        handshake = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {parsed.hostname}:{parsed.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        self.sock.sendall(handshake.encode("ascii"))
        self._read_handshake()

    def _read_handshake(self):
        # Read exactly the response headers and keep any bytes that follow:
        # the browser may already have sent a frame in the same packet.
        while b"\r\n\r\n" not in self._buffer:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("DevTools socket closed during handshake")
            self._buffer += chunk
        head, self._buffer = self._buffer.split(b"\r\n\r\n", 1)
        status_line = head.split(b"\r\n", 1)[0].decode("latin-1")
        if " 101 " not in f" {status_line} ":
            raise RuntimeError(f"DevTools handshake failed: {status_line}")

    def _recv_exact(self, size):
        while len(self._buffer) < size:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("DevTools socket closed")
            self._buffer += chunk
        data, self._buffer = self._buffer[:size], self._buffer[size:]
        return data

    def _send_frame(self, payload, opcode=0x1):
        mask = os.urandom(4)
        length = len(payload)
        frame = bytearray([0x80 | opcode])
        if length < 126:
            frame.append(0x80 | length)
        elif length < (1 << 16):
            frame.append(0x80 | 126)
            frame.extend(struct.pack("!H", length))
        else:
            frame.append(0x80 | 127)
            frame.extend(struct.pack("!Q", length))
        frame.extend(mask)
        frame.extend(bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload)))
        self.sock.sendall(bytes(frame))

    def _recv_message(self):
        parts = []
        while True:
            first, second = self._recv_exact(2)
            opcode = first & 0x0F
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._recv_exact(8))[0]
            mask = self._recv_exact(4) if second & 0x80 else None
            payload = self._recv_exact(length) if length else b""
            if mask:
                payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))

            if opcode == 0x8:
                raise RuntimeError("DevTools socket closed by browser")
            if opcode == 0x9:  # ping
                self._send_frame(payload, opcode=0xA)
                continue
            if opcode == 0xA:  # pong
                continue
            parts.append(payload)
            if first & 0x80:  # FIN
                return b"".join(parts)

    def _recv_json(self):
        return json.loads(self._recv_message().decode("utf-8"))

    def command(self, method, params=None):
        command_id = self._next_id
        self._next_id += 1
        self._send_frame(json.dumps({
            "id": command_id,
            "method": method,
            "params": params or {},
        }).encode("utf-8"))

        while True:
            message = self._recv_json()
            if message.get("id") == command_id:
                if "error" in message:
                    raise RuntimeError(f"{method} failed: {message['error']}")
                return message.get("result", {})
            if "method" in message:
                self._events.append(message)

    def wait_for_event(self, method, timeout):
        deadline = time.time() + timeout
        while True:
            for index, event in enumerate(self._events):
                if event.get("method") == method:
                    del self._events[index]
                    return event.get("params", {})
            remaining = deadline - time.time()
            if remaining <= 0:
                return None
            try:
                self.sock.settimeout(min(remaining, 5))
                message = self._recv_json()
            except socket.timeout:
                continue
            if "method" in message:
                self._events.append(message)
            elif "error" in message:
                raise RuntimeError(f"DevTools error: {message['error']}")

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


def _wait_for_page_target(port, timeout=DEVTOOLS_START_TIMEOUT):
    """Return the WebSocket URL of the browser's page target."""
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


def take_settled_screenshot(target, dimensions, settle_seconds=5):
    """Screenshot target after its content has had time to settle.

    Args:
        target: URL to load.
        dimensions: (width, height) tuple for the viewport.
        settle_seconds: wall-clock time to wait after the load event before
            capturing. Clamped to 0..30 by the caller.

    Returns:
        A PIL.Image, or None if the capture failed.
    """
    browser = find_browser()
    if not browser:
        logger.error(
            "No Chromium-based browser found. Install chromium, chromium-headless-shell, or chrome."
        )
        return None

    user_data_dir = tempfile.mkdtemp(prefix="inkypi_settled_screenshot_")
    port = _free_tcp_port()
    command = [
        browser,
        "--headless",
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data_dir}",
        f"--window-size={dimensions[0]},{dimensions[1]}",
        "--force-device-scale-factor=1",
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
    socket_connection = None
    image = None
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        websocket_url = _wait_for_page_target(port)
        if not websocket_url:
            logger.error("Timed out waiting for the DevTools endpoint.")
            return None

        socket_connection = _DevToolsSocket(websocket_url)
        socket_connection.command("Page.enable")
        socket_connection.command("Emulation.setDeviceMetricsOverride", {
            "width": dimensions[0],
            "height": dimensions[1],
            "deviceScaleFactor": 1,
            "mobile": False,
        })
        navigation = socket_connection.command("Page.navigate", {"url": target})
        if navigation.get("errorText"):
            logger.error(f"Navigation failed: {navigation['errorText']}")
            return None

        if socket_connection.wait_for_event("Page.loadEventFired", NAVIGATION_TIMEOUT) is None:
            # A page that keeps a connection open can delay the load event.
            # The screenshot is still useful once the settle time has passed.
            logger.debug("Timed out waiting for the load event; capturing anyway.")

        if settle_seconds > 0:
            time.sleep(settle_seconds)

        result = socket_connection.command(
            "Page.captureScreenshot",
            {"format": "png", "captureBeyondViewport": False},
        )
        data = result.get("data")
        if data:
            with Image.open(BytesIO(base64.b64decode(data))) as captured:
                image = captured.copy()
        else:
            logger.error("DevTools returned no screenshot data.")

    except Exception as error:
        logger.error(f"Failed to take settled screenshot: {error}")

    finally:
        if socket_connection:
            socket_connection.close()
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
        shutil.rmtree(user_data_dir, ignore_errors=True)

    return image
