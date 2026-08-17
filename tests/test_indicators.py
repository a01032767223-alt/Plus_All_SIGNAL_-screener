"""지표·점수 로직 검증.

실행: python -m pytest tests -q      (또는 python tests/test_indicators.py)
지표는 독립 구현(순수 파이썬 루프)과 대조해 검증한다.
"""
from __future__ import annotations

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from screener import config as C
from screener import indicators as I
from screener import rules as R
from screener import score as S


def _synth(n=300, seed=1):
    rng = np.random.default_rng(seed)
    close = 10000 * np.exp(np.cumsum(rng.normal(0.0005, 0.02, n)))
    high = close * (1 + np.abs(rng.normal(0, 0.01, n)))
    low = close * (1 - np.abs(rng.normal(0, 0.01, n)))
    open_ = np.r_[close[0], close[:-1]]
    vol = rng.lognormal(11, 0.4, n)
    idx = pd.bdate_range(end="2026-08-14", periods=n)
    return pd.DataFrame({"open": open_, "high": high, "low": low,
                         "close": close, "volume": vol}, index=idx)


# ── 가중치 ────────────────────────────────────────────────
def test_weights_sum_100():
    assert sum(C.WEIGHTS.values()) == 100


# ── SMA ──────────────────────────────────────────────────
def test_sma_exact():
    s = pd.Series([1, 2, 3, 4, 5], dtype=float)
    assert I.sma(s, 3).iloc[-1] == 4.0
    assert pd.isna(I.sma(s, 3).iloc[1])


# ── RSI : Wilder 원식(루프)과 대조 ─────────────────────────
def _rsi_reference(close, n=14):
    """Wilder 원식 그대로의 재귀 구현."""
    deltas = np.diff(close)
    seed = deltas[:n]
    up = seed[seed > 0].sum() / n
    down = -seed[seed < 0].sum() / n
    out = [np.nan] * len(close)
    for i in range(n, len(close)):
        d = deltas[i - 1]
        up = (up * (n - 1) + max(d, 0)) / n
        down = (down * (n - 1) + max(-d, 0)) / n
        rs = up / down if down != 0 else np.inf
        out[i] = 100 - 100 / (1 + rs)
    return np.array(out)


def test_rsi_matches_wilder_reference():
    df = _synth(200, seed=3)
    mine = I.rsi(df["close"], 14).to_numpy()
    ref = _rsi_reference(df["close"].to_numpy(), 14)
    # 시드 초기화 방식 차이로 앞부분은 수렴 전 → 뒤쪽 100개 비교
    a, b = mine[-100:], ref[-100:]
    assert np.nanmax(np.abs(a - b)) < 0.5, np.nanmax(np.abs(a - b))


def test_rsi_bounds_and_extremes():
    up_only = pd.Series(np.arange(1, 120, dtype=float))
    assert I.rsi(up_only).iloc[-1] > 99.9
    down_only = pd.Series(np.arange(120, 1, -1, dtype=float))
    assert I.rsi(down_only).iloc[-1] < 0.1
    r = I.rsi(_synth()["close"]).dropna()
    assert r.between(0, 100).all()


# ── MACD ─────────────────────────────────────────────────
def test_macd_definition():
    close = _synth(200, seed=5)["close"]
    m = I.macd(close)
    expect = (close.ewm(span=12, adjust=False, min_periods=12).mean()
              - close.ewm(span=26, adjust=False, min_periods=26).mean())
    assert np.allclose(m["macd"].dropna(), expect.dropna())
    assert np.allclose((m["macd"] - m["signal"]).dropna(), m["hist"].dropna())


# ── 볼린저밴드 ────────────────────────────────────────────
def test_bollinger_geometry():
    close = _synth(200, seed=7)["close"]
    b = I.bollinger(close).dropna()
    assert (b["upper"] > b["mid"]).all() and (b["mid"] > b["lower"]).all()
    # %B는 밴드 내 상대위치 정의와 일치해야 함
    recomputed = (close.loc[b.index] - b["lower"]) / (b["upper"] - b["lower"])
    assert np.allclose(b["pct_b"], recomputed)


def test_bollinger_constant_series():
    """분산 0 구간에서 0으로 나누어 폭발하지 않아야 한다."""
    s = pd.Series([100.0] * 60)
    b = I.bollinger(s)
    assert not np.isinf(b["pct_b"].fillna(0)).any()


# ── ATR / ADX ────────────────────────────────────────────
def test_atr_positive_and_tr_definition():
    df = _synth(150, seed=11)
    tr = I.true_range(df).dropna()
    assert (tr >= (df["high"] - df["low"]).loc[tr.index] - 1e-9).all()
    assert (I.atr(df).dropna() > 0).all()


def test_adx_range():
    df = _synth(300, seed=13)
    a = I.adx(df).dropna()
    assert a["adx"].between(0, 100).all()
    assert a["plus_di"].between(0, 100).all()
    assert a["minus_di"].between(0, 100).all()


def test_adx_strong_uptrend_gives_plus_di_dominance():
    n = 120
    close = np.linspace(100, 300, n)
    df = pd.DataFrame({"open": close, "high": close * 1.01,
                       "low": close * 0.99, "close": close,
                       "volume": np.full(n, 1000.0)})
    a = I.adx(df).iloc[-1]
    assert a["plus_di"] > a["minus_di"]
    assert a["adx"] > 40


# ── OBV ──────────────────────────────────────────────────
def test_obv_manual():
    df = pd.DataFrame({"open": [1, 1, 1, 1], "high": [1, 1, 1, 1],
                       "low": [1, 1, 1, 1],
                       "close": [10.0, 11.0, 10.5, 12.0],
                       "volume": [100.0, 200.0, 150.0, 300.0]})
    # 0, +200, -150, +300 누적
    assert I.obv(df).tolist() == [0.0, 200.0, 50.0, 350.0]


# ── 점수 로직 ─────────────────────────────────────────────
def test_scores_bounded_and_weighted_sum():
    for seed in range(1, 12):
        edf = I.enrich(_synth(300, seed=seed))
        res = R.score_all(edf)
        assert 0.0 <= res["total"] <= 100.0
        for k, p in res["parts"].items():
            assert 0.0 <= p["score"] <= 100.0, (k, p["score"])
        recomputed = sum(p["score"] * p["weight"] / 100 for p in res["parts"].values())
        assert abs(recomputed - res["total"]) < 0.05


def test_ideal_uptrend_scores_high():
    """20MA>60MA·거래량 급증·신고가·정배열을 모두 만족시킨 인공 시계열."""
    n = 200
    base = np.linspace(100, 180, n) + np.sin(np.arange(n) / 9) * 2
    close = base.copy()
    close[-1] = base[-2] * 1.04                      # 마지막 봉 강한 상승 돌파
    high = close * 1.005
    low = close * 0.995
    vol = np.full(n, 1000.0)
    vol[-1] = 3000.0                                  # 거래량 3배
    df = pd.DataFrame({"open": np.r_[close[0], close[:-1]], "high": high,
                       "low": low, "close": close, "volume": vol},
                      index=pd.bdate_range(end="2026-08-14", periods=n))
    res = R.score_all(I.enrich(df))
    assert res["total"] >= 75, res
    assert res["parts"]["ma"]["ok"] and res["parts"]["volume"]["ok"]
    assert res["parts"]["structure"]["ok"]


def test_downtrend_scores_low():
    n = 200
    close = np.linspace(200, 100, n)
    df = pd.DataFrame({"open": close, "high": close * 1.005, "low": close * 0.995,
                       "close": close, "volume": np.full(n, 1000.0)},
                      index=pd.bdate_range(end="2026-08-14", periods=n))
    res = R.score_all(I.enrich(df))
    assert res["total"] < 45, res["total"]


def test_grade_boundaries():
    assert S.grade_of(85.0) == "S" and S.grade_of(84.99) == "A"
    assert S.grade_of(75.0) == "A" and S.grade_of(74.99) == "B"
    assert S.grade_of(65.0) == "B" and S.grade_of(64.99) == "C"
    assert S.grade_of(54.99) == "-"


def test_risk_levels_sane():
    edf = I.enrich(_synth(300, seed=21))
    r = S.risk_levels(edf)
    assert r["stop"] < r["entry"] < r["target"]
    assert r["rr"] > 0


def test_target_capped_for_beaten_down_stock():
    """고점 대비 크게 빠진 종목이 손익비 10짜리 '좋은 자리'로 둔갑하면 안 된다.

    직전 고점을 1차 목표로 쓰되 손절폭의 MAX_RR_TARGET배를 넘지 않아야 한다.
    """
    n = 200
    close = np.r_[np.linspace(100, 300, 120), np.linspace(300, 120, 80)]
    close[-1] = close[-2] * 1.01
    df = pd.DataFrame({"open": np.r_[close[0], close[:-1]], "high": close * 1.01,
                       "low": close * 0.99, "close": close,
                       "volume": np.full(n, 1000.0)},
                      index=pd.bdate_range(end="2026-08-14", periods=n))
    r = S.risk_levels(I.enrich(df))
    assert r["rr"] <= C.MAX_RR_TARGET + 0.01, r
    assert r["stop"] < r["entry"] < r["target"]


def test_target_still_uses_prior_high_when_close_enough():
    """상한을 씌운다고 해서 가까운 직전 고점까지 잘라내면 안 된다."""
    n = 200
    base = np.linspace(100, 180, n)
    close = base.copy()
    close[-30] = base[-30] * 1.03          # 살짝 위에 있는 직전 고점
    df = pd.DataFrame({"open": np.r_[close[0], close[:-1]], "high": close * 1.002,
                       "low": close * 0.998, "close": close,
                       "volume": np.full(n, 1000.0)},
                      index=pd.bdate_range(end="2026-08-14", periods=n))
    edf = I.enrich(df)
    r = S.risk_levels(edf)
    ph = float(edf["prior_high"].iloc[-1])
    if ph > r["entry"] * 1.005 and (ph - r["entry"]) / (r["entry"] - r["stop"]) <= C.MAX_RR_TARGET:
        assert abs(r["target"] - ph) < 0.01, (r, ph)


def test_weekly_resample():
    df = _synth(300, seed=23)
    w = S.resample_weekly(df)
    assert 55 <= len(w) <= 65                        # 300영업일 ≈ 60주
    assert (w["high"] >= w["low"]).all()
    assert np.isclose(w["volume"].sum(), df["volume"].sum())


def test_evaluate_end_to_end():
    df = _synth(400, seed=29)
    res = S.evaluate({"1d": df, "1w": S.resample_weekly(df)}, "kr")
    assert res is not None
    assert set(res["timeframes"]) == {"1d", "1w"}
    assert 0 <= res["score"] <= 100
    assert len(res["checks"]) == 8


def test_sell_signals_silent_on_clean_uptrend():
    """정배열 상승만 있는 종목은 매도 경고가 없어야 한다."""
    n = 220
    base = np.linspace(100, 180, n) + np.sin(np.arange(n) / 9) * 1.5
    close = base.copy()
    close[-1] = base[-2] * 1.03
    vol = np.full(n, 1000.0)
    vol[-1] = 2500.0
    df = pd.DataFrame({"open": np.r_[close[0], close[:-1]], "high": close * 1.01,
                       "low": close * 0.99, "close": close, "volume": vol},
                      index=pd.bdate_range(end="2026-08-14", periods=n))
    warns = R.sell_signals(I.enrich(df))
    assert warns == [], warns


def test_sell_signals_fire_on_clean_downtrend():
    n = 220
    close = np.linspace(200, 100, n)
    df = pd.DataFrame({"open": close, "high": close * 1.01, "low": close * 0.99,
                       "close": close, "volume": np.full(n, 1000.0)},
                      index=pd.bdate_range(end="2026-08-14", periods=n))
    warns = R.sell_signals(I.enrich(df))
    keys = {w["key"] for w in warns}
    assert {"ma_dead", "structure_break", "adx_bear", "obv_dump"} <= keys, keys
    for w in warns:
        assert set(w) == {"key", "label", "detail", "help"}


def test_sell_signals_catch_topping_pattern_that_still_passes_score():
    """추세는 아직 매수 조건을 충족하지만 최근 과열 후 꺾인 종목.

    이게 이 기능의 존재 이유다 — 총점만으로는 걸러지지 않는 '천장 근처' 종목을
    경고로 잡아내야 한다.
    """
    n = 220
    up = np.linspace(100, 220, n - 8)
    close = np.r_[up, up[-1] * np.array([1.06, 1.09, 1.11, 1.10, 1.08, 1.06, 1.05, 1.03])]
    df = pd.DataFrame({"open": np.r_[close[0], close[:-1]], "high": close * 1.01,
                       "low": close * 0.99, "close": close, "volume": np.full(n, 1000.0)},
                      index=pd.bdate_range(end="2026-08-14", periods=n))
    edf = I.enrich(df)
    res = R.score_all(edf)
    warns = R.sell_signals(edf)
    assert res["total"] >= C.MIN_OUTPUT_SCORE, \
        "이 테스트는 '통과하는데 경고가 뜨는' 상황을 재현해야 의미가 있다"
    assert len(warns) >= 1, "과열 후 꺾인 흐름인데 경고가 하나도 없다"


def test_sell_signals_needs_min_history():
    assert R.sell_signals(I.enrich(_synth(5, seed=1))) == []


def test_evaluate_missing_daily_returns_none():
    df = _synth(400, seed=31)
    assert S.evaluate({"1w": S.resample_weekly(df)}, "kr") is None


def test_short_history_rejected():
    df = _synth(50, seed=33)
    assert S.evaluate({"1d": df}, "kr") is None


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except AssertionError as e:
                fails += 1
                print(f"  FAIL  {name}: {e}")
    print("\n실패" if fails else "\n전체 통과", f"({fails} failed)")
    raise SystemExit(1 if fails else 0)
