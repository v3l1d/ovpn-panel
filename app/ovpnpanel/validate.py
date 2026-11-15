"""Strict validators shared by the panel and the privileged helper.

The helper runs as root and never trusts its peer: everything it receives is validated again here.
All patterns use fullmatch: a bare "$" also matches before a trailing newline, which would allow
newline injection into configuration files.
"""
import ipaddress
import re

NAME_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{1,31}")
CN_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{1,31}_[0-9a-f]{6}")
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
HOST_RE = re.compile(rf"(?=.{{1,253}}\Z){_LABEL}(?:\.{_LABEL})*")


class ValidationError(ValueError):
    pass


def name(value) -> str:
    if not isinstance(value, str) or not NAME_RE.fullmatch(value):
        raise ValidationError("invalid name")
    return value


def cn(value) -> str:
    if not isinstance(value, str) or not CN_RE.fullmatch(value):
        raise ValidationError("invalid certificate name")
    return value


def host(value) -> str:
    """A DNS name or an IP address (IPv6 without brackets); returns it normalized."""
    if not isinstance(value, str):
        raise ValidationError("invalid host")
    h = value.strip()
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    try:
        return str(ipaddress.ip_address(h))
    except ValueError:
        pass
    if not HOST_RE.fullmatch(h):
        raise ValidationError("invalid host")
    return h.lower()


def split_csv(value) -> list:
    if isinstance(value, (list, tuple)):
        items = [str(v).strip() for v in value]
    else:
        items = [p.strip() for p in str(value).split(",")]
    return [i for i in items if i]


def ipv4_addresses(items, label: str, limit: int = 8) -> list:
    items = split_csv(items)
    if len(items) > limit:
        raise ValidationError(f"{label}: at most {limit} entries")
    try:
        return [str(ipaddress.IPv4Address(i)) for i in items]
    except ValueError as e:
        raise ValidationError(f"{label}: {e}")


def ipv4_networks(items, label: str, limit: int = 64) -> list:
    items = split_csv(items)
    if len(items) > limit:
        raise ValidationError(f"{label}: at most {limit} entries")
    try:
        return [str(ipaddress.IPv4Network(i, strict=False)) for i in items]
    except ValueError as e:
        raise ValidationError(f"{label}: {e}")


def int_range(value, label: str, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise ValidationError(f"{label}: must be a number between {lo} and {hi}")
    return value
