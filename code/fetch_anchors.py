#!/usr/bin/env python3
"""EM-Bench 构建步骤：校验任务 SMILES（RDKit）并抓取锚点酶的 UniProt 元数据。

输出 tasks/anchors.json：
    { accession: {entryType, sequence, reachable, name, ecs} }
其中 reachable = 是否 Swiss-Prot (reviewed)。
酶矿流程 uniprot_search 使用 reviewed_only=True，因此 reachable=false 的
锚点（如 AspX/BtnX/Q7SIG1，均为 TrEMBL）无法被任何版本检索到——评估时
单独报告“可达锚点”上的指标。
"""
import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

FIELDS = "accession,protein_name,ec,sequence"
RETRY, WAIT = 3, 3


def fetch_batch(accessions):
    query = " OR ".join(f"accession:{a}" for a in accessions)
    url = (
        "https://rest.uniprot.org/uniprotkb/search?"
        + urllib.parse.urlencode(
            {"query": query, "fields": FIELDS, "size": str(len(accessions) * 2),
             "format": "json"}
        )
    )
    for attempt in range(RETRY):
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.load(resp)
        except Exception as exc:  # noqa: BLE001
            print(f"  fetch attempt {attempt + 1} failed: {exc}", file=sys.stderr)
            time.sleep(WAIT * (attempt + 1))
    return {"results": []}


def validate_smiles(tasks):
    """RDKit 解析校验（miniprot 环境自带 rdkit）。"""
    try:
        from rdkit import Chem
    except ImportError:
        print("  [warn] rdkit 不可用，跳过 SMILES 校验")
        return
    bad = []
    for t in tasks:
        for side, smi in (("reactant", t["reaction_smiles"].split(">>")[0]),
                          ("product", t["reaction_smiles"].split(">>")[1])):
            if Chem.MolFromSmiles(smi) is None:
                bad.append((t["id"], side, smi))
    if bad:
        for b in bad:
            print(f"  [FAIL] {b[0]} {b[1]} 无法解析: {b[2]}")
        sys.exit(1)
    print(f"  [ok] {len(tasks)} 个任务反应 SMILES 均通过 RDKit 解析校验")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", default=str(ROOT / "tasks" / "tasks.json"))
    ap.add_argument("--out", default=str(ROOT / "tasks" / "anchors.json"))
    args = ap.parse_args()

    tasks = json.load(open(args.tasks))["tasks"]
    validate_smiles(tasks)

    anchors = {}
    for t in tasks:
        for a in t["anchors"]:
            anchors.setdefault(a["accession"], {"task_ids": [], "grade": 0, "label": a.get("label", "")})
            anchors[a["accession"]]["task_ids"].append(t["id"])
            anchors[a["accession"]]["grade"] = max(anchors[a["accession"]]["grade"], a["grade"])

    accs = sorted(anchors)
    print(f"抓取 {len(accs)} 个锚点: {', '.join(accs)}")
    data = fetch_batch(accs)
    found = {}
    for r in data.get("results", []):
        pd = r.get("proteinDescription", {}) or {}
        rec = pd.get("recommendedName") or pd.get("submittedName") or [{}]
        if isinstance(rec, list):
            rec = rec[0] if rec else {}
        name = rec.get("fullName", {})
        name = (name.get("value") if isinstance(name, dict) else name) or ""
        ecs = [e.get("value") for e in (rec.get("ecNumbers") or [])]
        entry_type = r.get("entryType", "")
        found[r["primaryAccession"]] = {
            "entryType": entry_type,
            "reachable": entry_type == "UniProtKB reviewed (Swiss-Prot)",
            "name": name,
            "ecs": ecs,
            "sequence": r.get("sequence", {}).get("value", ""),
        }

    out = {}
    for acc in accs:
        meta = found.get(acc)
        if meta is None:
            print(f"  [FAIL] 锚点 {acc} 在 UniProtKB 中不存在")
            sys.exit(1)
        out[acc] = {**anchors[acc], **meta}
        print(f"  {acc}: {meta['entryType'][:30]:30s} reachable={meta['reachable']} grade={anchors[acc]['grade']} {meta['name'][:40]}")

    json.dump(out, open(args.out, "w"), indent=2, ensure_ascii=False)
    print(f"写入 {args.out}")


if __name__ == "__main__":
    main()
