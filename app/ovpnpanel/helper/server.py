"""The privileged helper: a root daemon on a unix socket, one JSON request per connection.

    request:  {"op": "<name>", "args": {...}}\n
    response: {"ok": true, "result": ...}\n   or   {"ok": false, "error": "<code>", "message": "..."}\n

Access control is layered: the socket is root:<panel group> 0660, every peer is checked with
SO_PEERCRED (only root and the panel user are served), only the operations in ops.OPS exist,
and each of them validates its arguments.
"""
import grp
import json
import logging
import os
import pwd
import socket
import socketserver
import struct
import sys

from ..config import CONFIG
from . import ops
from .common import OpError

log = logging.getLogger("ovpn-panel-helper")
MAX_REQUEST = 64 * 1024


def _panel_uid():
    try:
        return pwd.getpwnam(CONFIG.panel_user).pw_uid
    except KeyError:
        return None


class Handler(socketserver.StreamRequestHandler):
    timeout = 120

    def _reply(self, payload: dict):
        try:
            self.wfile.write(json.dumps(payload).encode() + b"\n")
        except OSError:
            pass

    def _peer_allowed(self) -> bool:
        creds = self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", creds)
        allowed = {0, _panel_uid()} - {None}
        if uid not in allowed:
            log.warning("refused a connection from uid %s", uid)
        return uid in allowed

    def handle(self):
        if not self._peer_allowed():
            return
        line = self.rfile.readline(MAX_REQUEST + 1)
        if not line or len(line) > MAX_REQUEST:
            return self._reply({"ok": False, "error": "invalid", "message": "bad request"})
        try:
            req = json.loads(line)
            name, args = req["op"], req.get("args", {})
            if not isinstance(name, str) or not isinstance(args, dict):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            return self._reply({"ok": False, "error": "invalid", "message": "bad request"})
        fn = ops.OPS.get(name)
        if fn is None:
            return self._reply({"ok": False, "error": "invalid", "message": "unknown operation"})
        try:
            result = fn(**args)
        except OpError as e:
            return self._reply({"ok": False, "error": e.code, "message": e.message})
        except TypeError:
            return self._reply({"ok": False, "error": "invalid", "message": "invalid arguments"})
        except Exception:
            log.exception("operation %s failed", name)
            return self._reply({"ok": False, "error": "failed", "message": "internal error"})
        self._reply({"ok": True, "result": result})


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    request_queue_size = 32


def make_server(path: str = None) -> Server:
    path = path or CONFIG.helper_socket
    os.makedirs(os.path.dirname(path), mode=0o750, exist_ok=True)
    try:
        gid = grp.getgrnam(CONFIG.panel_user).gr_gid
    except KeyError:
        gid = None
    if gid is not None:
        os.chown(os.path.dirname(path), -1, gid)  # -1 keeps the owner (root in production)
        os.chmod(os.path.dirname(path), 0o750)
    if os.path.exists(path):
        os.remove(path)
    old_umask = os.umask(0o177)
    try:
        server = Server(path, Handler)
    finally:
        os.umask(old_umask)
    if gid is not None:
        os.chown(path, -1, gid)
        os.chmod(path, 0o660)
    return server


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)
    if os.geteuid() != 0:
        sys.exit("the helper must run as root")
    server = make_server()
    log.info("helper listening on %s (panel user: %s)", CONFIG.helper_socket, CONFIG.panel_user)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        try:
            os.remove(CONFIG.helper_socket)
        except OSError:
            pass


if __name__ == "__main__":
    main()
