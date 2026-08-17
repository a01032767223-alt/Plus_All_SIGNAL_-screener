"""실전 규모(기본 1,800종목) 매트릭스를 만들어 성능 점검용으로 덮어쓴다.

데모 데이터는 30종목뿐이라 카드 렌더 병목이 드러나지 않는다. 국내주식 실제
유니버스와 비슷한 크기로 부풀려 슬라이더 반응 속도를 재기 위한 스크립트다.

    python -m screener.run --market demo          # 먼저 데모 데이터 생성
    python tests/e2e/make_big_matrix.py           # 1,800종목으로 부풀리기
    python tests/e2e/my_strategy_perf.py          # 성능 측정
    python tests/e2e/make_big_matrix.py --restore # 원래 데모 데이터로 되돌리기

주의: docs/data/kr_*.json을 덮어쓴다. 끝나면 반드시 --restore 하거나
`python -m screener.run --market demo`로 다시 만들어라.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import random
import shutil

DATA = os.path.join("docs", "data")
LATEST = os.path.join(DATA, "kr_latest.json")
MATRIX = os.path.join(DATA, "kr_matrix.json")
BAK = os.path.join(DATA, "_perf_backup")


def _read(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _write(p, o):
    with open(p, "w", encoding="utf-8") as f:
        json.dump(o, f, ensure_ascii=False, separators=(",", ":"))


def restore() -> int:
    if not os.path.isdir(BAK):
        print("되돌릴 백업이 없습니다. python -m screener.run --market demo 를 다시 실행하세요.")
        return 1
    for name in ("kr_latest.json", "kr_matrix.json"):
        shutil.copy(os.path.join(BAK, name), os.path.join(DATA, name))
    shutil.rmtree(BAK)
    print("데모 데이터로 되돌렸습니다.")
    return 0


def inflate(n: int, seed: int) -> int:
    if not os.path.exists(MATRIX):
        print("docs/data/kr_matrix.json 이 없습니다. 먼저 --market demo 를 실행하세요.")
        return 1
    os.makedirs(BAK, exist_ok=True)
    for name in ("kr_latest.json", "kr_matrix.json"):
        shutil.copy(os.path.join(DATA, name), os.path.join(BAK, name))

    rng = random.Random(seed)
    d, m = _read(LATEST), _read(MATRIX)
    # 주봉까지 갖춘 행만 씨앗으로 쓴다 (한쪽이 비면 재계산 경로가 달라진다)
    seeds = [r for r in m["rows"] if r[1] and r[2]]
    if not seeds:
        print("씨앗으로 쓸 행이 없습니다.")
        return 1

    def jitter(block):
        return [round(min(100.0, max(0.0, v + rng.uniform(-25, 25))), 1) for v in block]

    rows, sidx, i = list(m["rows"]), list(d["search_index"]), 0
    while len(rows) < n:
        src = seeds[i % len(seeds)]
        sym = f"9{len(rows):05d}"
        rows.append([sym, jitter(src[1]), jitter(src[2]), rng.randint(0, 255),
                     rng.randint(1, 90000), round(rng.uniform(.4, 6), 2),
                     round(rng.uniform(1, 9), 1), rng.randint(0, 3)])
        sidx.append({"symbol": sym, "name": f"대량{len(rows):05d}", "market": "KOSPI",
                     "price": 10000 + len(rows), "change_pct": round(rng.uniform(-8, 8), 2),
                     "score": round(rng.uniform(20, 80), 2), "grade": "-", "link": "#"})
        i += 1

    m["rows"], d["search_index"], d["scanned"] = rows, sidx, len(rows)
    _write(MATRIX, m)
    _write(LATEST, d)

    raw = os.path.getsize(MATRIX)
    with open(MATRIX, "rb") as f:
        gz = len(gzip.compress(f.read(), 9))
    print(f"매트릭스 {len(rows):,}종목 · 원본 {raw/1024:.0f} KB · "
          f"gzip 전송 {gz/1024:.0f} KB · 종목당 {raw/len(rows):.0f} B")
    print("점검이 끝나면 --restore 로 되돌리세요.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=1800, help="부풀릴 종목 수 (기본 1800)")
    ap.add_argument("--seed", type=int, default=5)
    ap.add_argument("--restore", action="store_true", help="원래 데모 데이터로 되돌린다")
    a = ap.parse_args()
    return restore() if a.restore else inflate(a.rows, a.seed)


if __name__ == "__main__":
    raise SystemExit(main())
