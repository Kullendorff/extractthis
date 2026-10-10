"""Mäter OCR-tillförlitlighet mot facit (tests/fixtures/truth.json).

CER = teckenfelfrekvens (Levenshtein / facitlängd) efter normalisering av
blanksteg och markdown. Jämför effort=fast (ett försök) med effort=best.

    python tests/bench_ocr.py            # båda lägena
    python tests/bench_ocr.py --only best
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scripts"))

from xt.common import CACHE_DIR  # noqa: E402
from xt.router import extract_file  # noqa: E402


def norm(s: str) -> str:
    s = re.sub(r"<!--.*?-->", " ", s, flags=re.S)
    s = re.sub(r"[#*_>`⟦⟧]", "", s)
    s = s.replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", s).strip().lower()


def levenshtein(a: str, b: str) -> int:
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def cer(got: str, truth: str) -> float:
    t = norm(truth)
    return levenshtein(norm(got), t) / max(1, len(t))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=["fast", "best"])
    a = ap.parse_args()
    truth = json.loads((HERE / "fixtures" / "truth.json").read_text(encoding="utf-8"))
    efforts = [a.only] if a.only else ["fast", "best"]
    shutil.rmtree(CACHE_DIR / "ocr", ignore_errors=True)  # mät riktiga körningar, inte cache
    rows = []
    for name, lines in truth.items():
        row = {"fil": name}
        for eff in efforts:
            t0 = time.time()
            doc = extract_file(HERE / "fixtures" / name, effort=eff, lang="auto")
            got = "\n".join(s["text"] for s in doc.sections)
            row[eff] = cer(got, "\n".join(lines))
            row[eff + "_s"] = time.time() - t0
            row[eff + "_rev"] = len(doc.meta.get("review") or {})
        rows.append(row)
    print(f"\n{'fil':32}" + "".join(f"{e + ' CER':>11}{'tid':>7}{'gransk':>8}" for e in efforts))
    for r in rows:
        print(f"{r['fil']:32}" + "".join(f"{r[e]:>10.1%} {r[e + '_s']:>5.1f}s {r[e + '_rev']:>6}" for e in efforts))
    for e in efforts:
        print(f"{'MEDEL ' + e:32}{sum(r[e] for r in rows) / len(rows):>10.1%}")


if __name__ == "__main__":
    main()
