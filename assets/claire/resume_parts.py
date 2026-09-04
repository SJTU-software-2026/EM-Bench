#!/usr/bin/env python3
"""Resilient segmented resume downloader for CLAIRE data.zip (Zenodo 14635841).

The CDN kills long transfers at random offsets, so each part is downloaded
in an inner loop that re-requests the remaining Range until the part is
complete. Parts are processed in threads (one per part).
"""
import os
import sys
import threading
import time
import urllib.request

URL = "https://zenodo.org/records/14635841/files/data.zip?download=1"
SIZE = 921446213
N = 8
PART = SIZE // N
DIR = os.path.dirname(os.path.abspath(__file__))


def ranges():
    for i in range(N):
        s = i * PART
        e = (SIZE - 1) if i == N - 1 else (s + PART - 1)
        yield i, s, e


def fetch_part(idx, s, e):
    path = os.path.join(DIR, f"part_{idx}")
    expect = e - s + 1
    fail = 0
    while True:
        have = os.path.getsize(path) if os.path.exists(path) else 0
        if have >= expect:
            print(f"part_{idx}: done ({have}/{expect})", flush=True)
            return True
        if fail >= 40:
            print(f"part_{idx}: GAVE UP at {have}/{expect}", flush=True)
            return False
        start = s + have
        req = urllib.request.Request(
            URL, headers={"Range": f"bytes={start}-{e}", "User-Agent": "EM-Bench/1.0"}
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                with open(path, "ab") as f:
                    while True:
                        chunk = resp.read(1 << 16)
                        if not chunk:
                            break
                        f.write(chunk)
            fail = 0
            if os.path.getsize(path) >= expect:
                print(f"part_{idx}: done ({expect}/{expect})", flush=True)
                return True
        except Exception as exc:
            fail += 1
            print(f"part_{idx}: retry {fail} ({exc.__class__.__name__})", flush=True)
            time.sleep(min(2 ** min(fail, 6), 60))


def main():
    only = [int(x) for x in sys.argv[1:]] or list(range(N))
    threads = []
    for idx, s, e in ranges():
        if idx not in only:
            continue
        t = threading.Thread(target=fetch_part, args=(idx, s, e), daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join()
    ok = all(
        os.path.getsize(os.path.join(DIR, f"part_{i}")) >= (r[2] - r[1] + 1)
        for i, _, _ in ranges() if i in only
    )
    print("ALL_OK" if ok else "SOME_FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
