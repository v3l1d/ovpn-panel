"""Client per l'interfaccia di management di OpenVPN (socket unix).

OpenVPN accetta un solo client di management alla volta e scrive nel log ogni
connessione e disconnessione: si tiene quindi aperta una sola connessione,
condivisa sotto lock e riaperta quando OpenVPN si riavvia.
"""
import socket
import threading

from .config import CONFIG

TIMEOUT = 3
_lock = threading.Lock()
_conn = None  # (socket, file in lettura)


class MgmtError(Exception):
    pass


def _close():
    global _conn
    if _conn:
        for part in reversed(_conn):
            try:
                part.close()
            except OSError:
                pass
    _conn = None


def _open():
    global _conn
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(TIMEOUT)
    s.connect(CONFIG.mgmt_socket)
    _conn = (s, s.makefile("r", encoding="utf-8", errors="replace", newline="\n"))


def _exchange(cmd: str, multiline: bool) -> list:
    s, rf = _conn
    s.sendall((cmd + "\n").encode())
    out = []
    while True:
        line = rf.readline()
        if not line:
            raise ConnectionResetError("connessione chiusa da OpenVPN")
        line = line.rstrip("\r\n")
        if line.startswith(">"):  # benvenuto e notifiche asincrone
            continue
        if line.startswith("ERROR:"):
            raise MgmtError(line[6:].strip())
        if multiline:
            if line == "END":
                return out
            out.append(line)
        elif line.startswith("SUCCESS:"):
            return [line[8:].strip()]


def _command(cmd: str, multiline: bool) -> list:
    with _lock:
        # un secondo tentativo con una connessione nuova copre il riavvio di OpenVPN
        for attempt in (1, 2):
            try:
                if _conn is None:
                    _open()
                return _exchange(cmd, multiline)
            except MgmtError:
                raise
            except OSError as e:
                _close()
                if attempt == 2:
                    raise MgmtError(f"OpenVPN non raggiungibile ({e})")


def clients() -> dict:
    """CN -> dati del client connesso."""
    header, out = None, {}
    for line in _command("status 3", multiline=True):
        f = line.split("\t")
        if f[:2] == ["HEADER", "CLIENT_LIST"]:
            header = f[2:]
        elif f[0] == "CLIENT_LIST" and header:
            row = dict(zip(header, f[1:]))
            out[row.get("Common Name")] = {
                "vpn_ip": row.get("Virtual Address") or None,
                "real_address": row.get("Real Address") or None,
                "connected_since": int(row.get("Connected Since (time_t)") or 0),
                "rx": int(row.get("Bytes Received") or 0),
                "tx": int(row.get("Bytes Sent") or 0),
            }
    return out


def stats() -> dict:
    """load-stats -> {nclients, bytesin, bytesout}"""
    line = _command("load-stats", multiline=False)[0]
    return {k: int(v) for k, v in (p.split("=", 1) for p in line.split(",") if "=" in p)}


def state() -> dict:
    lines = _command("state", multiline=True)
    f = lines[-1].split(",") if lines else []
    return {"up_since": int(f[0]) if f and f[0].isdigit() else None, "state": f[1] if len(f) > 1 else None}


def version() -> str:
    for line in _command("version", multiline=True):
        if line.startswith("OpenVPN Version:"):
            parts = line.split()
            return parts[3] if len(parts) > 3 else line
    return ""


def log(lines: int = 100) -> list:
    out = []
    for line in _command(f"log {int(lines)}", multiline=True):
        ts, _, rest = line.partition(",")
        flags, _, msg = rest.partition(",")
        out.append({"time": int(ts) if ts.isdigit() else None, "flags": flags, "msg": msg})
    return out


def kill(cn: str):
    try:
        _command(f"kill {cn}", multiline=False)
    except MgmtError as e:
        if "common name" not in str(e).lower():  # non connesso: niente da fare
            raise
