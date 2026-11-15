"""Prepares a release: sets the version and pins the installer to the exact application files.

    python tools/release.py 1.0.0

1. writes the version into app/ovpnpanel/__init__.py
2. hashes every file under app/ (the same way proxmox/create-ct.sh does after downloading)
3. writes the tag name and that hash into proxmox/create-ct.sh

Then commit, tag (v<version>) and push. The installer fetched from that tag refuses to run if the
application files differ from what was released. app/ must not change after this step.
"""
import hashlib
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
APP = ROOT / "app"


def tree_hash() -> str:
    """sha256 over "<sha256>  <path>\n" lines sorted by path: `find | sort | sha256sum | sha256sum`."""
    lines = []
    for path in sorted(p for p in APP.rglob("*") if p.is_file() and "__pycache__" not in p.parts
                       and p.suffix != ".pyc"):
        data = path.read_bytes()
        if b"\r" in data:
            sys.exit(f"{path.relative_to(ROOT)} has CRLF line endings: the hash would differ on Linux")
        lines.append(f"{hashlib.sha256(data).hexdigest()}  {path.relative_to(ROOT).as_posix()}\n")
    return hashlib.sha256("".join(lines).encode()).hexdigest()


def sub_once(text: str, pattern: str, repl: str, what: str) -> str:
    new, n = re.subn(pattern, repl, text, count=1, flags=re.M)
    if n != 1:
        sys.exit(f"could not find {what}")
    return new


def main():
    if len(sys.argv) != 2 or not re.fullmatch(r"\d+\.\d+\.\d+", sys.argv[1]):
        sys.exit("usage: python tools/release.py MAJOR.MINOR.PATCH")
    version = sys.argv[1]

    init = APP / "ovpnpanel" / "__init__.py"
    init.write_text(sub_once(init.read_text(encoding="utf-8"), r'^__version__ = ".*"$',
                             f'__version__ = "{version}"', "__version__"), encoding="utf-8", newline="\n")

    digest = tree_hash()
    script = ROOT / "proxmox" / "create-ct.sh"
    text = script.read_text(encoding="utf-8")
    text = sub_once(text, r'^PINNED_REF=".*"$', f'PINNED_REF="v{version}"', "PINNED_REF")
    text = sub_once(text, r'^PINNED_TREE_SHA256=".*"$', f'PINNED_TREE_SHA256="{digest}"', "PINNED_TREE_SHA256")
    script.write_text(text, encoding="utf-8", newline="\n")
    print(f"version {version}, tag v{version}, application files sha256 {digest}")


if __name__ == "__main__":
    main()
