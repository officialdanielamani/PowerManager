#!/usr/bin/env python3
"""Download front-end libraries into app/static/vendor (Docker build runs this automatically).

Uses npm registry tarballs, so no CDN is needed at runtime and the server works offline.
Usage: python scripts/fetch_vendor.py [dest_dir]
"""
import io
import os
import sys
import tarfile
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


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    dest = sys.argv[1] if len(sys.argv) > 1 else os.path.join(here, "..", "app", "static", "vendor")
    for name, version, files in FILES:
        if all(os.path.exists(os.path.join(dest, d)) for _, d in files):
            continue
        url = f"{REGISTRY}/{name}/-/{name}-{version}.tgz"
        print(f"Downloading {name}@{version}")
        with urllib.request.urlopen(url, timeout=60) as resp:
            data = resp.read()
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            for src, dst in files:
                out = os.path.join(dest, dst)
                os.makedirs(os.path.dirname(out), exist_ok=True)
                with tar.extractfile(src) as f, open(out, "wb") as o:
                    o.write(f.read())
    print(f"Vendor files ready in {os.path.abspath(dest)}")


if __name__ == "__main__":
    main()
