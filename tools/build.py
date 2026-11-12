"""Crea dist/ovpn-panel-proxmox.sh: lo script Proxmox con l'applicazione incorporata.

Il file risultante e' l'unico da copiare sul nodo Proxmox.
    python tools/build.py
"""
import base64
import io
import pathlib
import tarfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
TEXT = {".py", ".sh", ".html", ".txt", ".json", ".md"}


def _add(tar: tarfile.TarFile, path: pathlib.Path):
    rel = path.relative_to(ROOT).as_posix()
    data = path.read_bytes()
    if path.suffix in TEXT:
        data = data.replace(b"\r\n", b"\n")  # sorgenti scritti su Windows
    info = tarfile.TarInfo(rel)
    info.size = len(data)
    info.mode = 0o755 if path.suffix == ".sh" else 0o644
    info.mtime = 0  # build riproducibile
    tar.addfile(info, io.BytesIO(data))


def main():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path in sorted((ROOT / "app").rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                _add(tar, path)
    payload = base64.encodebytes(buf.getvalue()).decode()

    script = (ROOT / "proxmox" / "create-ct.sh").read_text(encoding="utf-8").replace("\r\n", "\n")
    out = ROOT / "dist" / "ovpn-panel-proxmox.sh"
    out.parent.mkdir(exist_ok=True)
    # exit prima del marcatore: bash non deve mai interpretare il payload
    out.write_bytes((script.rstrip("\n") + "\nexit 0\n__PAYLOAD_BELOW__\n" + payload).encode())
    print(f"{out.relative_to(ROOT)}  ({out.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
