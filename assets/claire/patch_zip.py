#!/usr/bin/env python3
"""Patch corrupted zip entry byte-ranges in data.zip by re-fetching them."""
import os
import sys
import time
import urllib.request

URL = "https://zenodo.org/records/14635841/files/data.zip?download=1"
DIR = os.path.dirname(os.path.abspath(__file__))
ZIP = os.path.join(DIR, "data.zip")

# (start, end_exclusive, label)
REGIONS = [
    (22181642, 218647169, "model_lookup_train"),
    (387540711, 554021061, "esm_emb_dict_ec2"),
]


def fetch_region(start, end, label):
    need = end - start
    out = os.path.join(DIR, f"patch_{label}.bin")
    fail = 0
    while True:
        have = os.path.getsize(out) if os.path.exists(out) else 0
        if have >= need:
            print(f"{label}: fetched {need}", flush=True)
            return True
        if fail >= 60:
            print(f"{label}: GAVE UP at {have}/{need}", flush=True)
            return False
        req = urllib.request.Request(
            URL, headers={"Range": f"bytes={start+have}-{end-1}",
                          "User-Agent": "EM-Bench/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                with open(out, "ab") as f:
                    while True:
                        chunk = resp.read(1 << 16)
                        if not chunk:
                            break
                        f.write(chunk)
            fail = 0
        except Exception as exc:
            fail += 1
            print(f"{label}: retry {fail} ({exc.__class__.__name__})", flush=True)
            time.sleep(min(2 ** min(fail, 6), 60))
    return False


def main():
    ok = True
    for s, e, label in REGIONS:
        if not fetch_region(s, e, label):
            ok = False
            continue
        patch = os.path.join(DIR, f"patch_{label}.bin")
        with open(ZIP, "r+b") as zf, open(patch, "rb") as pf:
            zf.seek(s)
            zf.write(pf.read())
        print(f"{label}: patched into data.zip", flush=True)
    print("PATCH_OK" if ok else "PATCH_FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
