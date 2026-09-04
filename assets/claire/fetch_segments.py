#!/usr/bin/env python3
"""4MB 小分段下载 data.zip（应对连接周期性停顿），逐响应校验对齐。

- 每段一次短连接；响应必须是 206 且 Content-Range 起点与请求一致，否则丢弃重试。
- 段文件独立落盘；join 后 unzip -t 验证，坏条目对应的段按需重下（见 heal 循环）。
用法:
    python fetch_segments.py download        # 下载全部缺失段
    python fetch_segments.py join            # 拼接 data.zip 并 unzip -t
    python fetch_segments.py heal            # 找出坏段并重下（循环直到通过）
"""
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile

URL = "https://zenodo.org/records/14635841/files/data.zip?download=1"
SIZE = 921446213
SEG = 4 * 1024 * 1024
N_SEG = (SIZE + SEG - 1) // SEG
DIR = os.path.dirname(os.path.abspath(__file__))
SEGDIR = os.path.join(DIR, "segs")
ZIP = os.path.join(DIR, "data.zip")


def seg_file(i):
    return os.path.join(SEGDIR, f"seg_{i:04d}")


def expected_size(i):
    s = i * SEG
    e = min(s + SEG - 1, SIZE - 1)
    return e - s + 1


def fetch_seg(i, attempts=40):
    s = i * SEG
    e = min(s + SEG - 1, SIZE - 1)
    need = e - s + 1
    tmp = seg_file(i) + ".tmp"
    for attempt in range(1, attempts + 1):
        if os.path.exists(tmp):
            os.remove(tmp)
        req = urllib.request.Request(
            URL, headers={"Range": f"bytes={s}-{e}", "User-Agent": "EM-Bench/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                if resp.status != 206:
                    raise ValueError(f"status {resp.status}")
                cr = resp.headers.get("Content-Range", "")
                if not cr.startswith(f"bytes {s}-"):
                    raise ValueError(f"bad content-range {cr}")
                with open(tmp, "wb") as f:
                    got = 0
                    while got < need:
                        chunk = resp.read(min(1 << 16, need - got))
                        if not chunk:
                            break
                        f.write(chunk)
                        got += len(chunk)
            if os.path.getsize(tmp) == need:
                os.rename(tmp, seg_file(i))
                return True
            print(f"seg {i}: attempt {attempt} short", flush=True)
            os.remove(tmp)
        except Exception as exc:
            if os.path.exists(tmp):
                os.remove(tmp)
            if attempt % 10 == 0:
                print(f"seg {i}: attempt {attempt} {exc.__class__.__name__} "
                      f"{str(exc)[:60]}", flush=True)
            time.sleep(min(2 * attempt, 60))
    return False


def download_missing():
    os.makedirs(SEGDIR, exist_ok=True)
    missing = [i for i in range(N_SEG)
               if not (os.path.exists(seg_file(i))
                       and os.path.getsize(seg_file(i)) == expected_size(i))]
    print(f"missing {len(missing)}/{N_SEG} segments", flush=True)
    if not missing:
        return True
    results = {}
    lock = threading.Lock()

    def worker(q):
        while True:
            with lock:
                if not q:
                    return
                i = q.pop()
            results[i] = fetch_seg(i)
            done = N_SEG - len(q)
            if done % 25 == 0 or done == N_SEG:
                print(f"progress {done}/{N_SEG}", flush=True)
    q = list(reversed(missing))
    threads = [threading.Thread(target=worker, args=(q,), daemon=True)
               for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    bad = [i for i, ok in results.items() if not ok]
    print(f"done; failed segments: {len(bad)}", flush=True)
    return not bad


def join_and_test():
    parts = []
    for i in range(N_SEG):
        p = seg_file(i)
        if not (os.path.exists(p) and os.path.getsize(p) == expected_size(i)):
            print(f"seg {i} missing/wrong size", flush=True)
            return False
        parts.append(p)
    with open(ZIP, "wb") as out:
        for p in parts:
            with open(p, "rb") as f:
                out.write(f.read())
    print(f"joined {SIZE} == {os.path.getsize(ZIP)}", flush=True)
    return os.path.getsize(ZIP) == SIZE and test_zip()


def test_zip():
    bad = []
    try:
        with zipfile.ZipFile(ZIP) as z:
            for info in z.infolist():
                try:
                    z.read(info)
                except Exception as exc:
                    bad.append((info.filename, str(exc)))
    except Exception as exc:
        print(f"zip open failed: {exc}", flush=True)
        return False
    if bad:
        for name, err in bad:
            print(f"  BAD {name}: {err}", flush=True)
        return False
    print("zip fully OK", flush=True)
    return True


def heal():
    """unzip -t 失败条目 -> 定位其字节区间 -> 重下覆盖的段 -> 重测（最多 5 轮）。"""
    for rnd in range(1, 6):
        if test_zip():
            return True
        print(f"--- heal round {rnd}", flush=True)
        try:
            with zipfile.ZipFile(ZIP) as z:
                infos = [(i, z.getinfo(i)) for i in z.namelist()]
        except Exception as exc:
            print(f"cannot open zip: {exc}", flush=True)
            return False
        bad_ranges = []
        for name, info in infos:
            try:
                with zipfile.ZipFile(ZIP) as z:
                    z.read(info)
            except Exception:
                off = info.header_offset
                end = off + 30 + len(info.filename.encode()) + info.extra.__len__() \
                    + info.compress_size
                bad_ranges.append((off, end, name))
                print(f"  heal target {name} bytes[{off},{end})", flush=True)
        segs_to_refetch = set()
        for off, end, name in bad_ranges:
            for i in range(off // SEG, end // SEG + 1):
                if 0 <= i < N_SEG:
                    segs_to_refetch.add(i)
        if not segs_to_refetch:
            print("no bad segments identified", flush=True)
            return False
        print(f"refetching {len(segs_to_refetch)} segments", flush=True)
        for i in sorted(segs_to_refetch):
            p = seg_file(i)
            if os.path.exists(p):
                os.remove(p)
        if not download_missing():
            print("segment download incomplete", flush=True)
            return False
        if not join_and_test():
            continue
        return True
    return False


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "download"
    if cmd == "download":
        sys.exit(0 if download_missing() else 1)
    elif cmd == "join":
        sys.exit(0 if join_and_test() else 1)
    elif cmd == "heal":
        sys.exit(0 if heal() else 1)
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()
