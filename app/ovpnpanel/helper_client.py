"""Client for the privileged helper (a root daemon listening on a unix socket).

The panel runs without privileges: everything that needs root (certificates, server.conf, the
OpenVPN management socket, restarts, the journal) goes through this narrow, validated interface.
"""
import json
import socket

from .config import CONFIG

MAX_RESPONSE = 4 * 1024 * 1024


class HelperError(Exception):
    """code: unavailable | invalid | not_found | failed"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def call(op: str, timeout: float = 90, **args):
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(CONFIG.helper_socket)
    except (OSError, AttributeError) as e:
        raise HelperError("unavailable", f"helper not reachable ({e})")
    try:
        with s:
            s.sendall(json.dumps({"op": op, "args": args}).encode() + b"\n")
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > MAX_RESPONSE:
                    raise HelperError("failed", "helper response too large")
    except OSError as e:
        raise HelperError("unavailable", f"helper communication error ({e})")
    try:
        reply = json.loads(buf)
    except ValueError:
        raise HelperError("failed", "malformed helper response")
    if not reply.get("ok"):
        raise HelperError(reply.get("error", "failed"), reply.get("message", "helper error"))
    return reply.get("result")
