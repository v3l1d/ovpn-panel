"""The operations the panel may request from the helper. This is the whole privileged interface.

Every function validates its arguments (the caller is not trusted) and returns plain JSON data.
Anything that is not listed in OPS cannot be requested.
"""
import subprocess
import time

from .. import settings, validate
from ..config import CONFIG
from . import mgmt, pki, serverconf, tlsca
from .common import OpError, cert_end, days_left, log, pki_lock, run

OPS = {}


def op(fn):
    OPS[fn.__name__] = fn
    return fn


def _kill(cn: str):
    try:
        mgmt.kill(cn)
    except mgmt.MgmtError as e:
        log.info("could not disconnect %s: %s", cn, e)


def _cn(value) -> str:
    try:
        return validate.cn(value)
    except validate.ValidationError:
        raise OpError("invalid", "invalid certificate name")


def _days(value) -> int:
    try:
        return validate.int_range(value, "days", 1, 3650)
    except validate.ValidationError as e:
        raise OpError("invalid", str(e))


# ---------------------------------------------------------------- certificates

@op
def issue_cert(cn, days):
    cn, days = _cn(cn), _days(days)
    with pki_lock():
        return {"not_after": pki.issue(cn, days)}


@op
def cert_info(cn):
    return pki.info(_cn(cn))


@op
def profile_material(cn):
    return pki.material(_cn(cn))


@op
def revoke_cert(cn):
    cn = _cn(cn)
    with pki_lock():
        pki.revoke(cn)
    _kill(cn)
    return {}


@op
def renew_cert(old_cn, new_cn, days, keep_disabled=False):
    """Issues the new certificate and revokes the old one as a single step.

    If anything fails after the new certificate was issued, it is revoked again (it was never handed
    out), so a failed renewal leaves the user exactly as before.
    """
    old_cn, new_cn, days = _cn(old_cn), _cn(new_cn), _days(days)
    if not isinstance(keep_disabled, bool):
        raise OpError("invalid", "keep_disabled must be a boolean")
    with pki_lock():
        not_after = pki.issue(new_cn, days)
        try:
            pki.revoke(old_cn)
            if keep_disabled:
                pki.set_disabled(new_cn, True)
        except Exception:
            log.exception("renewal of %s failed, rolling back %s", old_cn, new_cn)
            try:
                pki.revoke(new_cn)
            except Exception:
                log.exception("rollback of %s failed", new_cn)
            raise OpError("failed", "renewal failed and was rolled back")
    _kill(old_cn)
    return {"not_after": not_after}


@op
def set_disabled(cn, disabled):
    cn = _cn(cn)
    if not isinstance(disabled, bool):
        raise OpError("invalid", "disabled must be a boolean")
    with pki_lock():
        pki.set_disabled(cn, disabled)
    if disabled:
        _kill(cn)
    return {}


# ---------------------------------------------------------------- OpenVPN server

def _restart_and_wait(timeout: float = 20):
    try:
        run(list(CONFIG.restart_cmd), timeout=60)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        raise OpError("failed", "OpenVPN failed to restart")
    mgmt.reset()
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if mgmt.state().get("state") == "CONNECTED":
                return
        except mgmt.MgmtError:
            pass
        time.sleep(0.5)
    raise OpError("failed", "OpenVPN did not come back after the restart")


@op
def apply_server_conf(dns, routes, full_tunnel, client_to_client, restart=True):
    """Writes server.conf and restarts OpenVPN; puts the previous file back if it does not start."""
    try:
        params = settings.validate_server({"dns": dns, "routes": routes, "full_tunnel": full_tunnel,
                                           "client_to_client": client_to_client})
    except ValueError as e:
        raise OpError("invalid", str(e))
    new = serverconf.render(params)
    with pki_lock():
        old = serverconf.read()
        serverconf.write_text(new)
        if restart:
            try:
                _restart_and_wait()
            except OpError:
                if old:
                    log.error("OpenVPN did not start with the new settings: restoring the previous server.conf")
                    serverconf.write_text(old)
                    try:
                        _restart_and_wait()
                    except OpError:
                        log.exception("OpenVPN did not come back after the rollback either")
                raise OpError("failed", "OpenVPN rejected the new settings; the previous ones were restored")
    return {}


@op
def restart():
    with pki_lock():
        _restart_and_wait()
    return {}


@op
def kill(cn):
    cn = _cn(cn)
    try:
        mgmt.kill(cn)
    except mgmt.MgmtError as e:
        raise OpError("failed", str(e))
    return {}


@op
def clients():
    try:
        return mgmt.clients()
    except mgmt.MgmtError:
        return {}


@op
def overview():
    out = {"state": None, "stats": {}, "version": ""}
    try:
        out["state"] = mgmt.state()
        out["stats"] = mgmt.stats()
        out["version"] = mgmt.version()
    except mgmt.MgmtError as e:
        out["error"] = str(e)
    return out


@op
def journal(lines=150):
    """OpenVPN log from the journal (full history), without the management interface's own chatter."""
    import json

    if isinstance(lines, bool) or not isinstance(lines, int):
        raise OpError("invalid", "lines must be a number")
    lines = min(max(lines, 1), 1000)
    try:
        r = run(["journalctl", "-u", "openvpn-server@server", "-n", str(lines * 20), "-o", "json", "--no-pager"],
                timeout=15)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        try:
            return [e for e in mgmt.log(lines) if not e["msg"].startswith("MANAGEMENT:")]
        except mgmt.MgmtError as e:
            raise OpError("failed", str(e))
    out = []
    for raw in r.stdout.splitlines():
        try:
            e = json.loads(raw)
        except ValueError:
            continue
        msg = e.get("MESSAGE")
        if isinstance(msg, str) and not msg.startswith("MANAGEMENT:"):
            out.append({"time": int(e["__REALTIME_TIMESTAMP"]) // 1_000_000,
                        "flags": "N" if int(e.get("PRIORITY", 6)) <= 4 else "I", "msg": msg})
    return out[-lines:]


@op
def pki_status():
    """Expiry of every certificate the service depends on."""
    def item(key, label, path, kind="x509"):
        try:
            end = cert_end(path, kind)
            return {"key": key, "label": label, "not_after": end.isoformat(), "days_left": days_left(end)}
        except (subprocess.CalledProcessError, OSError, ValueError):
            return {"key": key, "label": label, "not_after": None, "days_left": None}

    pki_dir = pki.pki_dir()
    items = [
        item("ca", "OpenVPN CA", f"{pki_dir}/ca.crt"),
        item("server", "OpenVPN server certificate", f"{pki_dir}/issued/server.crt"),
        item("crl", "Revocation list (CRL)", f"{CONFIG.server_dir}/crl.pem", "crl"),
        item("panel", "Panel HTTPS certificate", tlsca.leaf_cert()),
        item("panel_ca", "Panel CA", tlsca.ca_cert()),
    ]
    try:
        fp = tlsca.fingerprint()
    except (subprocess.CalledProcessError, OSError):
        fp = ""
    return {"items": items, "panel_ca_fingerprint": fp}
