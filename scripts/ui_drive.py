"""Drive the running CreatorOS window over the DevTools protocol for UI verification.

Usage:
  python scripts/ui_drive.py click "Media grid"
  python scripts/ui_drive.py clickx Overview 1     (second nav item called "Overview")
  python scripts/ui_drive.py shot  <PROJECT_ROOT>/temp/out.png
  python scripts/ui_drive.py eval  "document.title"

It talks to the Electron renderer that `pnpm dev` started with CREATOROS_DEBUG_PORT set.
This exists so the seven library pages can be verified against real data; it is a dev
tool, is not packaged, and adds no runtime dependency (the WebSocket client below is
implemented on the standard library on purpose).
"""
from __future__ import annotations

import base64
import json
import os
import socket
import struct
import sys
import time
import urllib.request

PORT = int(os.environ.get("CREATOROS_DEBUG_PORT", "9222"))


def _target() -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list", timeout=10) as response:
        pages = [item for item in json.load(response) if item.get("type") == "page"]
    if not pages:
        raise SystemExit("no page target; is the app running with CREATOROS_DEBUG_PORT?")
    return pages[0]


class MiniWs:
    """Just enough RFC 6455 to talk to the DevTools protocol from the standard library."""

    def __init__(self, url: str) -> None:
        parsed = url.split("://", 1)[1]
        hostport, _, path = parsed.partition("/")
        host, _, port_text = hostport.partition(":")
        self.sock = socket.create_connection((host, int(port_text or 80)), timeout=30)
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(request.encode())
        self._buffer = b""
        while b"\r\n\r\n" not in self._buffer:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise SystemExit("websocket handshake failed")
            self._buffer += chunk
        head, _, self._buffer = self._buffer.partition(b"\r\n\r\n")
        if b"101" not in head.split(b"\r\n")[0]:
            raise SystemExit(f"websocket handshake refused: {head[:120]!r}")

    def _read(self, count: int) -> bytes:
        while len(self._buffer) < count:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise SystemExit("websocket closed")
            self._buffer += chunk
        data, self._buffer = self._buffer[:count], self._buffer[count:]
        return data

    def send(self, text: str) -> None:
        payload = text.encode()
        header = bytearray([0x81])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", length)
        mask = os.urandom(4)
        header += mask
        self.sock.sendall(bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def recv(self) -> str:
        chunks: list[bytes] = []
        while True:
            first, second = self._read(2)
            fin = first & 0x80
            opcode = first & 0x0F
            length = second & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read(8))[0]
            payload = self._read(length) if length else b""
            if opcode == 0x8:
                raise SystemExit("websocket closed by peer")
            if opcode == 0x9:  # ping
                continue
            chunks.append(payload)
            if fin:
                return b"".join(chunks).decode()


class Cdp:
    def __init__(self) -> None:
        self.ws = MiniWs(_target()["webSocketDebuggerUrl"])
        self.counter = 0

    def call(self, method: str, **params) -> dict:
        self.counter += 1
        self.ws.send(json.dumps({"id": self.counter, "method": method, "params": params}))
        while True:
            message = json.loads(self.ws.recv())
            if message.get("id") == self.counter:
                if "error" in message:
                    raise RuntimeError(f"{method}: {message['error']}")
                return message.get("result", {})

    def js(self, expression: str):
        result = self.call("Runtime.evaluate", expression=expression, returnByValue=True, awaitPromise=True)
        return result.get("result", {}).get("value")

    def click_nav(self, label: str, index: int = 0) -> bool:
        script = f"""
            (() => {{
              const target = {json.dumps(label)};
              const nodes = [...document.querySelectorAll('nav button')].filter(
                (node) => node.textContent.trim() === target);
              if (!nodes[{index}]) return false;
              nodes[{index}].click();
              return true;
            }})()
        """
        return bool(self.js(script))

    def shot(self, path: str) -> str:
        data = self.call("Page.captureScreenshot", format="png", captureBeyondViewport=False)
        with open(path, "wb") as handle:
            handle.write(base64.b64decode(data["data"]))
        return path


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    action = argv[1]
    cdp = Cdp()
    if action in ("click", "clickx") and len(argv) >= 3:
        index = int(argv[3]) if action == "clickx" and len(argv) >= 4 else 0
        ok = cdp.click_nav(argv[2], index)
        print(f"click {argv[2]!r} #{index}: {ok}")
        return 0 if ok else 1
    if action == "shot" and len(argv) >= 3:
        time.sleep(0.5)
        print("saved", cdp.shot(argv[2]))
        return 0
    if action == "eval" and len(argv) >= 3:
        print(json.dumps(cdp.js(argv[2]), indent=2, default=str)[:6000])
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
