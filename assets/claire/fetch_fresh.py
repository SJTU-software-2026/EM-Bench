#!/usr/bin/env python3
"""Fresh segmented download of CLAIRE data.zip (Zenodo 14635841).

每个 part 必须由「单条连续连接」完成：任何失败都删除该 part 并从 0 重下，
绝不续传 —— 续传会把 CDN 不同缓存副本的字节混在一个 part 里（本次事故根因）。
另要求响应为 206 且 Content-Range 起点正确，200 响应直接重试。
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


def fetch_part(idx, s, e):
    path = os.path.join(DIR, f"part_{idx}")
    expect = e - s + 1
    for attempt in range(1, 61):
        if os.path.exists(path):
            os.remove(path)
        req = urllib.request.Request(
            URL, headers={"Range": f"bytes={s}-{e}", "User-Agent": "EM-Bench/1.0"})
        tmp = path + ".tmp"
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                if resp.status != 206:
                    raise ValueError(f"status {resp.status}")
                cr = resp.headers.get("Content-Range", "")
                if not cr.startswith(f"bytes {s}-"):
                    raise ValueError(f"bad content-range {cr}")
                with open(tmp, "wb") as f:
                    while True:
                        chunk = resp.read(1 << 16)
                        if not chunk:
                            break
                        f.write(chunk)
            if os.path.getsize(tmp) == expect:
                os.rename(tmp, path)
                print(f"part_{idx}: ok (attempt {attempt})", flush=True)
                return True
            print(f"part_{idx}: attempt {attempt} short file "
                  f"({os.path.getsize(tmp)}/{expect})", flush=True)
            os.remove(tmp)
        except Exception as exc:
            if os.path.exists(tmp):
                os.remove(tmp)
            print(f"part_{idx}: attempt {attempt} {exc.__class__.__name__}: "
                  f"{str(exc)[:80]}", flush=True)
            time.sleep(min(3 * attempt, 90))
    print(f"part_{idx}: GAVE UP", flush=True)
    return False


def main():
    threads = []
    for idx in range(N):
        s = idx * PART
        e = (SIZE - 1) if idx == N - 1 else (s + PART - 1)
        t = threading.Thread(target=fetch_part, args=(idx, s, e), daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join()
    ok = True
    for idx in range(N):
        s = idx * PART
        e = (SIZE - 1) if idx == N - 1 else (s + PART - 1)
        p = os.path.join(DIR, f"part_{idx}")
        if not (os.path.exists(p) and os.path.getsize(p) == e - s + 1):
            ok = False
            print(f"part_{idx}: INCOMPLETE", flush=True)
    print("ALL_OK" if ok else "SOME_FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
