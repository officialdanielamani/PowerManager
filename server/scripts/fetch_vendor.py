#!/usr/bin/env python3
"""Re-download the front-end libraries in app/static/vendor (they are committed to the repo).

Only needed to restore deleted files or after changing a version below.
Usage: python scripts/fetch_vendor.py [dest_dir] [--force]
"""
import io
import os
import sys
import tarfile
import time
import urllib.request

REGISTRY = "https://registry.npmjs.org"
# package, version, [(path inside tarball, destination relative to dest_dir)]
FILES = [
    ("bootstrap", "5.3.3", [
        ("package/dist/css/bootstrap.min.css", "bootstrap/bootstrap.min.css"),
        ("package/dist/js/bootstrap.bundle.min.js", "bootstrap/bootstrap.bundle.min.js"),
    ]),
    ("bootstrap-icons", "1.11.3", [
        ("package/font/bootstrap-icons.min.css", "bootstrap-icons/bootstrap-icons.min.css"),
        ("package/font/fonts/bootstrap-icons.woff", "bootstrap-icons/fonts/bootstrap-icons.woff"),
        ("package/font/fonts/bootstrap-icons.woff2", "bootstrap-icons/fonts/bootstrap-icons.woff2"),
    ]),
    ("chart.js", "4.4.1", [
        ("package/dist/chart.umd.js", "chartjs/chart.umd.js"),
    ]),
]


def download(url, attempts=5):
    for i in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=60) as resp:
                return resp.read()
        except OSError as exc:
            if i == attempts - 1:
                raise
            wait = 2 ** (i + 1)
            print(f"  failed ({exc}); retrying in {wait}s")
            time.sleep(wait)


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    args = [a for a in sys.argv[1:] if a != "--force"]
    force = "--force" in sys.argv
    dest = args[0] if args else os.path.join(here, "..", "app", "static", "vendor")
    for name, version, files in FILES:
        if not force and all(os.path.exists(os.path.join(dest, d)) for _, d in files):
            continue
        url = f"{REGISTRY}/{name}/-/{name}-{version}.tgz"
        print(f"Downloading {name}@{version}")
        data = download(url)
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            for src, dst in files:
                out = os.path.join(dest, dst)
                os.makedirs(os.path.dirname(out), exist_ok=True)
                with tar.extractfile(src) as f, open(out, "wb") as o:
                    o.write(f.read())
    print(f"Vendor files ready in {os.path.abspath(dest)}")


if __name__ == "__main__":
    main()
