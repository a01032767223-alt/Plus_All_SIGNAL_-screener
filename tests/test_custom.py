"""'내 전략' 탭(사용자 맞춤 가중치) 데이터 검증.

이 파일의 핵심은 test_default_weights_reproduce_official_score 하나다.
사용자가 프리셋을 '기본'으로 두면 내 전략 탭의 순서가 매수신호 탭과 완전히
같아야 한다. 그게 깨지면 이 기능 전체를 믿을 수 없다.

실행: python -m pytest tests -q
"""
from __future__ import annotations

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from screener import config as C
from screener import reversal as REV
from screener import run as RUN
from screener import score as S

KEYS = list(C.WEIGHTS)


# ─────────────────────────────────────────────────────────
# docs/index.html 의 myScore() 와 같은 공식 — 여기서 파이썬으로 복제해 검증한다.
# 한쪽을 고치면 반드시 다른 쪽도 고쳐야 한다.
# ─────────────────────────────────────────────────────────
def recompute(row: list, weights: dict, asset: str) -> float | None:
    tf_order = list(C.TF_WEIGHTS[asset])
    scores = {tf: row[1 + i] for i, tf in enumerate(tf_order)}   # None 이면 데이터 부족
    W = sum(weights.values())
    if W <= 0:
        return None
    used = {tf: C.TF_WEIGHTS[asset][tf] for tf in tf_order if scores[tf] is not None}
    if "1d" not in used:
        return None
    tf_total = {tf: sum(scores[tf][i] * weights[k] for i, k in enumerate(KEYS)) / W
                for tf in used}
    norm = sum(used.values())
    combined = sum(tf_total[tf] * w / norm for tf, w in used.items())
    if len(tf_total) > 1 and all(v >= C.MTF_ALIGN_THRESHOLD for v in tf_total.values()):
        combined = min(100.0, combined + C.MTF_ALIGN_BONUS)
    return max(0.0, min(100.0, combined))


def recompute_reversal(cats: list, weights: dict) -> float | None:
    """추세전환 6카테고리 재계산 — 카테고리별 만점이 다르므로 100 기준으로 환산 후 가중."""
    tot = wsum = 0.0
    for i, k in enumerate(RUN.REV_KEYS):
        if cats[i] is None:                      # 주식에는 coin 카테고리가 없다
            continue
        w = weights.get(k, 0)
        tot += (cats[i] / REV.CAT_MAX[k] * 100.0) * w
        wsum += w
    if wsum <= 0:
        return None
    return max(0.0, min(100.0, tot / wsum))


# ─────────────────────────────────────────────────────────
# 합성 유니버스 — 실제 스크리너와 같은 경로로 매트릭스 행을 만든다
# ─────────────────────────────────────────────────────────
def _universe(n=45, seed=11):
    rng = np.random.default_rng(seed)
    out = []
    for i in range(n):
        m = 300
        steps = rng.normal(rng.normal(0.0012 if i % 3 == 0 else -0.0004, 0.001), 0.022, m)
        if i % 3 == 0:
            steps[-12:] += 0.012
        close = 10000 * np.exp(np.cumsum(steps))
        high = close * (1 + np.abs(rng.normal(0, 0.008, m)))
        low = close * (1 - np.abs(rng.normal(0, 0.008, m)))
        df = pd.DataFrame({"open": np.r_[close[0], close[:-1]], "high": high, "low": low,
                           "close": close, "volume": rng.lognormal(11, 0.4, m)},
                          index=pd.bdate_range(end="2026-08-14", periods=m))
        res = S.evaluate({"1d": df, "1w": S.resample_weekly(df)}, "kr")
        if res is None:
            continue
        sym = f"T{i:05d}"
        out.append((sym, res, RUN._matrix_row(sym, res, "kr", 1.234e10)))
    return out


UNIVERSE = _universe()


# ─────────────────────────────────────────────────────────
# 핵심 불변식
# ─────────────────────────────────────────────────────────
def test_universe_is_not_trivially_small():
    assert len(UNIVERSE) >= 30, "검증 표본이 너무 적으면 불변식 테스트가 의미 없다"


def test_default_weights_reproduce_official_score():
    """기본 가중치로 재계산한 값 == 서버가 계산한 총점.

    허용 오차 0.1점은 매트릭스에 지표 점수를 소수 1자리로 반올림해 담는 데서만 온다.
    """
    worst = 0.0
    for sym, res, row in UNIVERSE:
        mine = recompute(row, C.WEIGHTS, "kr")
        assert mine is not None, sym
        worst = max(worst, abs(mine - res["score"]))
    assert worst < 0.1, f"최대 오차 {worst:.3f}점 — 재계산 공식이 서버와 어긋났다"


def test_default_weights_reproduce_official_ranking():
    """점수뿐 아니라 '순서'가 같아야 한다 — 사용자가 실제로 보는 건 순위다."""
    official = sorted(UNIVERSE, key=lambda t: -t[1]["score"])
    mine = sorted(UNIVERSE, key=lambda t: -recompute(t[2], C.WEIGHTS, "kr"))
    assert [t[0] for t in official] == [t[0] for t in mine]


def test_mtf_align_bonus_is_reproduced():
    """정렬 보너스(+5)까지 재현하지 않으면 상위권 순서가 어긋난다."""
    aligned = [(s, r, row) for s, r, row in UNIVERSE if r["mtf_aligned"]]
    assert aligned, "정렬 보너스가 붙은 종목이 표본에 없어 검증이 불가능하다"
    for sym, res, row in aligned:
        assert abs(recompute(row, C.WEIGHTS, "kr") - res["score"]) < 0.1


def test_custom_weights_actually_change_ranking():
    """가중치를 바꿨는데 순서가 그대로면 기능이 동작하지 않는 것이다."""
    alt = {"ma": 5, "volume": 10, "structure": 10, "rsi": 30,
           "macd": 10, "bb": 10, "adx": 5, "obv": 20}
    base = [t[0] for t in sorted(UNIVERSE, key=lambda t: -recompute(t[2], C.WEIGHTS, "kr"))]
    tuned = [t[0] for t in sorted(UNIVERSE, key=lambda t: -recompute(t[2], alt, "kr"))]
    assert base != tuned


def test_weight_sum_is_normalized():
    """합계가 100이 아니어도(예: 전부 2배) 같은 순서·같은 점수가 나와야 한다."""
    doubled = {k: v * 2 for k, v in C.WEIGHTS.items()}
    for sym, res, row in UNIVERSE[:10]:
        assert abs(recompute(row, doubled, "kr") - recompute(row, C.WEIGHTS, "kr")) < 1e-9


def test_zero_weights_return_none():
    row = UNIVERSE[0][2]
    assert recompute(row, {k: 0 for k in KEYS}, "kr") is None


# ─────────────────────────────────────────────────────────
# 매트릭스 행 구조
# ─────────────────────────────────────────────────────────
def test_matrix_row_shape():
    sym, res, row = UNIVERSE[0]
    tf_order = list(C.TF_WEIGHTS["kr"])
    assert row[0] == sym
    assert len(row) == 1 + len(tf_order) + 5          # symbol + tf별 점수 + 나머지 5개
    for i in range(len(tf_order)):
        block = row[1 + i]
        if block is None:
            continue
        assert len(block) == len(KEYS)
        assert all(0.0 <= v <= 100.0 for v in block)


def test_matrix_row_fields_match_payload_schema():
    """row_fields 설명과 실제 행 길이가 어긋나면 브라우저가 엉뚱한 칸을 읽는다."""
    mp = RUN._matrix_payload("kr", "kr", "2026-08-14", "2026-08-14T16:00:00+09:00",
                             [row for _, _, row in UNIVERSE], [])
    assert len(mp["row_fields"]) == len(UNIVERSE[0][2])
    assert mp["keys"] == KEYS
    assert mp["tfs"] == list(C.TF_WEIGHTS["kr"])


def test_okmask_matches_checks():
    for sym, res, row in UNIVERSE:
        okmask = row[1 + len(C.TF_WEIGHTS["kr"])]
        restored = [bool(okmask >> i & 1) for i in range(len(KEYS))]
        assert restored == [c["ok"] for c in res["checks"]], sym


def test_risk_fields_carried():
    for sym, res, row in UNIVERSE[:10]:
        base = 1 + len(C.TF_WEIGHTS["kr"])
        assert row[base + 1] == round(1.234e10 / 1e6)      # turnover_m
        assert row[base + 2] == res["risk"]["rr"]
        assert row[base + 3] == res["risk"]["atr_pct"]
        assert row[base + 4] == res["warning_count"]


# ─────────────────────────────────────────────────────────
# 프리셋
# ─────────────────────────────────────────────────────────
def test_all_presets_sum_to_100_and_use_known_keys():
    for name, p in C.WEIGHT_PRESETS.items():
        assert sum(p["w"].values()) == 100, name
        assert set(p["w"]) == set(KEYS), name
        assert p["label"] and p["desc"], name


def test_default_preset_equals_server_weights():
    """이게 어긋나면 '기본 프리셋 = 매수신호 탭' 약속이 깨진다."""
    assert C.WEIGHT_PRESETS["default"]["w"] == C.WEIGHTS


def test_all_rev_presets_sum_to_100():
    for name, p in C.REV_WEIGHT_PRESETS.items():
        assert sum(p["w"].values()) == 100, name
        assert set(p["w"]) == set(REV.CAT_MAX), name


def test_rev_default_preset_equals_cat_max():
    assert C.REV_WEIGHT_PRESETS["default"]["w"] == {k: int(v) for k, v in REV.CAT_MAX.items()}


def test_hard_filter_defs_are_well_formed():
    keys = set()
    for f in C.HARD_FILTER_DEFS:
        assert f["key"] not in keys, f"필터 키 중복: {f['key']}"
        keys.add(f["key"])
        assert f["label"] and f["kind"]
        opts = f["opts"]
        if isinstance(opts, dict):                      # 시장별 옵션
            assert set(opts) == {"kr", "us", "coin"}
            for v in opts.values():
                assert v[0] is None                     # 첫 항목은 항상 '제한 없음'
        else:
            assert opts[0] is None


# ─────────────────────────────────────────────────────────
# 추세전환 매트릭스
# ─────────────────────────────────────────────────────────
def _w_bottom(seed=3, rally=0.011):
    rng = np.random.default_rng(seed)
    pre = np.full(40, 10000.0) * (1 + rng.normal(0, 0.003, 40))
    lead = np.full(20, 10000.0) * (1 + rng.normal(0, 0.003, 20))
    dec = lead[-1] * np.exp(np.cumsum(rng.normal(-0.013, 0.017, 50)))
    bounce = dec[-1] * np.exp(np.cumsum(rng.normal(0.008, 0.011, 10)))
    retest = bounce[-1] * np.exp(np.cumsum(rng.normal(-0.004, 0.009, 8)))
    brk = retest[-1] * np.exp(np.cumsum(rng.normal(rally, 0.011, 27)))
    close = np.concatenate([pre, lead, dec, bounce, retest, brk])
    n = len(close)
    return pd.DataFrame({"open": np.r_[close[0], close[:-1]],
                         "high": close * (1 + np.abs(rng.normal(0, 0.006, n))),
                         "low": close * (1 - np.abs(rng.normal(0, 0.006, n))),
                         "close": close, "volume": rng.lognormal(11, 0.25, n)},
                        index=pd.bdate_range(end="2026-08-14", periods=n))


def test_rev_matrix_row_shape_and_recompute():
    rev = REV.evaluate_reversal(_w_bottom(), "kr")
    assert rev is not None
    row = RUN._rev_matrix_row("T00001", rev)
    assert row[0] == "T00001"
    assert len(row[1]) == len(RUN.REV_KEYS)
    assert row[1][RUN.REV_KEYS.index("coin")] is None        # 주식엔 코인 카테고리 없음
    assert row[2] <= -REV.MIN_DECLINE_PCT
    assert row[3] in (0, 1)
    # 기본 배점으로 재계산하면 서버 점수와 같아야 한다
    mine = recompute_reversal(row[1], C.REV_WEIGHT_PRESETS["default"]["w"])
    assert abs(mine - rev["score"]) < 0.15


def test_rev_matrix_keeps_rows_below_the_cut():
    """35점 컷 미달 종목도 매트릭스에는 남아야 한다.

    컷은 사용자가 조절하는 값이라, 컷 통과분만 담으면 비중을 바꿔도 후보가 늘지 않는다.
    """
    kept, below = [], 0
    for seed in range(40):
        df = _w_bottom(seed=seed, rally=0.002)      # 약한 반등 → 컷 미달이 섞이게
        out, rows = [], []
        RUN._add_reversal(out, df, "kr", f"S{seed}", f"이름{seed}", "KOSPI", "#",
                          rev_rows=rows)
        if rows:
            kept.extend(rows)
            if not out:                              # 후보 목록엔 못 들어갔지만
                below += 1                           # 매트릭스에는 남았다
    assert kept, "표본이 하락 15% 게이트를 통과하지 못했다"
    assert below > 0, "컷 미달 종목이 매트릭스에 남는지 확인할 표본이 없다"


def test_rev_recompute_changes_with_weights():
    rev = REV.evaluate_reversal(_w_bottom(seed=5, rally=0.012), "kr")
    row = RUN._rev_matrix_row("X", rev)
    base = recompute_reversal(row[1], C.REV_WEIGHT_PRESETS["default"]["w"])
    tuned = recompute_reversal(row[1], C.REV_WEIGHT_PRESETS["divergence"]["w"])
    assert base is not None and tuned is not None
    assert base != tuned


# ─────────────────────────────────────────────────────────
# 페이로드 결합
# ─────────────────────────────────────────────────────────
def test_payload_carries_matrix_separately():
    """매트릭스는 latest.json이 아니라 별도 파일로 나가야 한다 (첫 로딩 보호)."""
    items = [dict(r, symbol=s, name=s, market="KOSPI", turnover=1.0, marketcap=1.0, link="#")
             for s, r, _ in UNIVERSE[:5]]
    payload = RUN._payload("kr", items, len(UNIVERSE), "2026-08-14",
                           [{"symbol": s} for s, _, _ in UNIVERSE],
                           [], [row for _, _, row in UNIVERSE], [])
    assert "_matrix" in payload
    assert payload["has_matrix"] is True
    m = payload.pop("_matrix")
    assert len(m["rows"]) == len(UNIVERSE)
    # 매트릭스 행 수 == 검색 인덱스 수 (평가 성공 종목 전부가 내 전략 대상)
    assert len(m["rows"]) == len(payload["search_index"])
    assert m["data_date"] == payload["data_date"]
    assert m["generated_at"] == payload["generated_at"]
    assert "rows" not in payload and "keys" not in payload
