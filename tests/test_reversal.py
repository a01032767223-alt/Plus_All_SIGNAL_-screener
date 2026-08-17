"""추세전환 초기탐지(screener/reversal.py) 검증.

실행: python -m pytest tests -q
"""
from __future__ import annotations

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from screener import indicators as I
from screener import reversal as REV


# ─────────────────────────────────────────────────────────
# 합성 데이터 헬퍼
# ─────────────────────────────────────────────────────────
def _flat_ohlcv(n=150, level=10000.0, seed=1):
    rng = np.random.default_rng(seed)
    close = np.full(n, level) * (1 + rng.normal(0, 0.003, n))
    high = close * (1 + np.abs(rng.normal(0, 0.005, n)))
    low = close * (1 - np.abs(rng.normal(0, 0.005, n)))
    open_ = np.r_[close[0], close[:-1]]
    vol = rng.lognormal(11, 0.3, n)
    idx = pd.bdate_range(end="2026-08-14", periods=n)
    return pd.DataFrame({"open": open_, "high": high, "low": low,
                         "close": close, "volume": vol}, index=idx)


def _uptrend_ohlcv(n=200, seed=2):
    rng = np.random.default_rng(seed)
    close = 10000 * np.exp(np.cumsum(rng.normal(0.0015, 0.015, n)))
    high = close * (1 + np.abs(rng.normal(0, 0.005, n)))
    low = close * (1 - np.abs(rng.normal(0, 0.005, n)))
    open_ = np.r_[close[0], close[:-1]]
    vol = rng.lognormal(11, 0.3, n)
    idx = pd.bdate_range(end="2026-08-14", periods=n)
    return pd.DataFrame({"open": open_, "high": high, "low": low,
                         "close": close, "volume": vol}, index=idx)


def _w_bottom_ohlcv(seed=1, rally_drift=0.010):
    """평탄 구간(고점) → 급락(~45%) → 저점→반등→재하락(Higher Low)→돌파.
    120봉 관찰창 안에 고점부터 돌파까지 전부 들어오도록 길이를 맞췄다."""
    rng = np.random.default_rng(seed)
    pre_pad = np.full(40, 10000.0) * (1 + rng.normal(0, 0.003, 40))
    lead = np.full(20, 10000.0) * (1 + rng.normal(0, 0.003, 20))
    decline = lead[-1] * np.exp(np.cumsum(rng.normal(-0.013, 0.017, 50)))
    low1 = decline[-1]
    bounce = low1 * np.exp(np.cumsum(rng.normal(0.008, 0.011, 10)))
    interim_high = bounce[-1]
    retest = interim_high * np.exp(np.cumsum(rng.normal(-0.004, 0.009, 8)))
    low2 = retest[-1]
    breakout = low2 * np.exp(np.cumsum(rng.normal(rally_drift, 0.011, 27)))
    close = np.concatenate([pre_pad, lead, decline, bounce, retest, breakout])
    n = len(close)
    high = close * (1 + np.abs(rng.normal(0, 0.006, n)))
    low = close * (1 - np.abs(rng.normal(0, 0.006, n)))
    open_ = np.r_[close[0], close[:-1]]
    vol = rng.lognormal(11, 0.25, n)
    vol[60:110] *= np.linspace(2.2, 1.0, 50)          # 하락 구간 거래량 감소 추세
    vol[128:] *= np.linspace(1.2, 2.0, n - 128)        # 돌파 구간 거래량 증가
    idx = pd.bdate_range(end="2026-08-14", periods=n)
    return pd.DataFrame({"open": open_, "high": high, "low": low,
                         "close": close, "volume": vol}, index=idx)


# ─────────────────────────────────────────────────────────
# 배점 합계
# ─────────────────────────────────────────────────────────
def test_cat_max_sums_to_100():
    assert sum(REV.CAT_MAX.values()) == 100


# ─────────────────────────────────────────────────────────
# 스토캐스틱 (indicators.py 추가분)
# ─────────────────────────────────────────────────────────
def test_stochastic_bounds():
    df = _uptrend_ohlcv()
    edf = I.enrich(df)
    k, d = edf["stoch_k"].dropna(), edf["stoch_d"].dropna()
    assert len(k) > 0 and len(d) > 0
    assert (k >= 0).all() and (k <= 100).all()
    assert (d >= 0).all() and (d <= 100).all()


# ─────────────────────────────────────────────────────────
# 스윙 고/저 탐지
# ─────────────────────────────────────────────────────────
def test_swing_lows_finds_local_minimum():
    s = pd.Series([10, 8, 6, 4, 6, 8, 10, 8, 6, 4, 6, 8, 10], dtype=float)
    mask = REV._swing_lows(s, order=2)
    lows = s[mask]
    # 인덱스 3과 9가 국지적 저점(값 4)이어야 한다
    assert 3 in lows.index and 9 in lows.index
    assert (lows == 4).all()


def test_swing_highs_finds_local_maximum():
    s = pd.Series([0, 2, 4, 6, 4, 2, 0, 2, 4, 6, 4, 2, 0], dtype=float)
    mask = REV._swing_highs(s, order=2)
    highs = s[mask]
    assert 3 in highs.index and 9 in highs.index
    assert (highs == 6).all()


# ─────────────────────────────────────────────────────────
# 하락 게이트
# ─────────────────────────────────────────────────────────
def test_decline_context_none_for_short_data():
    assert REV.decline_context(_flat_ohlcv(n=10)) is None


def test_decline_context_detects_high_and_decline():
    df = _w_bottom_ohlcv()
    ctx = REV.decline_context(df)
    assert ctx is not None
    assert ctx["decline_pct"] < -REV.MIN_DECLINE_PCT   # 45% 급락 설계이므로 게이트를 넉넉히 통과
    assert ctx["high"] > df["close"].iloc[-1]


# ─────────────────────────────────────────────────────────
# 개별 신호 함수
# ─────────────────────────────────────────────────────────
def test_sig_higher_low_positive_case():
    lows = pd.Series([100.0, 110.0], index=pd.bdate_range("2026-01-01", periods=2))
    pts, ok, detail = REV._sig_higher_low(lows)
    assert ok is True and pts > 0


def test_sig_higher_low_negative_case():
    lows = pd.Series([110.0, 100.0], index=pd.bdate_range("2026-01-01", periods=2))
    pts, ok, detail = REV._sig_higher_low(lows)
    assert ok is False and pts == 0.0


def test_sig_higher_low_insufficient_data():
    lows = pd.Series([100.0], index=pd.bdate_range("2026-01-01", periods=1))
    pts, ok, detail = REV._sig_higher_low(lows)
    assert ok is False and pts == 0.0


def test_stage_of_thresholds():
    assert REV.stage_of(10.0, breakout_confirmed=True) is None      # 점수 미달이면 돌파해도 후보 아님
    assert REV.stage_of(50.0, breakout_confirmed=False) == "early"
    assert REV.stage_of(50.0, breakout_confirmed=True) == "confirmed"


# ─────────────────────────────────────────────────────────
# 종합 평가 evaluate_reversal
# ─────────────────────────────────────────────────────────
def test_evaluate_reversal_none_for_uptrend():
    """하락한 적 없는 종목은 애초에 반전 후보가 아니다 (기존 매수신호 탭과 역할 분리)."""
    assert REV.evaluate_reversal(_uptrend_ohlcv(), "kr") is None


def test_evaluate_reversal_none_for_short_data():
    assert REV.evaluate_reversal(_flat_ohlcv(n=30), "kr") is None


def test_evaluate_reversal_w_bottom_detected():
    df = _w_bottom_ohlcv(seed=3, rally_drift=0.011)
    res = REV.evaluate_reversal(df, "kr")
    assert res is not None
    assert res["stage"] in ("early", "confirmed")
    assert res["decline_pct"] <= -REV.MIN_DECLINE_PCT
    assert 0.0 <= res["score"] <= 100.0
    # 스펙에 명시된 6개 카테고리 중 코인이 아니므로 5개만 있어야 한다
    assert set(res["category_scores"].keys()) == {"structure", "volume", "momentum", "ma", "volatility"}
    assert "coin" not in res["category_scores"]
    # "왜 이 종목인가" — 충족된 신호는 근거 텍스트를 반드시 동반해야 한다
    assert isinstance(res["reasons"], list)
    for r in res["reasons"]:
        assert r["ok"] is True and r["points"] > 0
        assert r["detail"] and r["label"]
    # 신호 총 개수는 항상 19개(가격구조4+거래량4+모멘텀4+이동평균4+변동성3)
    assert res["signals_total"] == 19
    assert res["signals_passed"] <= res["signals_total"]


def test_evaluate_reversal_stage_matches_breakout_flag():
    """확인 진입이면 반드시 구조적 돌파(swing_high_break)가 True여야 한다."""
    df = _w_bottom_ohlcv(seed=5, rally_drift=0.012)
    res = REV.evaluate_reversal(df, "kr")
    if res and res["stage"] == "confirmed":
        assert res["breakout_confirmed"] is True
    if res and res["stage"] == "early":
        assert res["breakout_confirmed"] is False


# ─────────────────────────────────────────────────────────
# 코인 특화 — BTC 대비 상대강도
# ─────────────────────────────────────────────────────────
def test_coin_reversal_uses_btc_relative_strength_when_available():
    alt = _w_bottom_ohlcv(seed=7, rally_drift=0.014)   # 강한 반등(= BTC 대비 우위 유도)
    btc = _w_bottom_ohlcv(seed=7, rally_drift=0.002)   # 약한 반등

    without_btc = REV.evaluate_reversal(alt, "coin", btc_df=None)
    with_btc = REV.evaluate_reversal(alt, "coin", btc_df=btc)
    assert without_btc is not None and with_btc is not None
    # btc_df가 없으면 코인 카테고리가 아예 빠지고 90점 만점 재환산만 있어야 한다
    assert "coin" not in without_btc["category_scores"]
    assert "coin" in with_btc["category_scores"]
    assert set(with_btc["category_max"].keys()) == {"structure", "volume", "momentum",
                                                      "ma", "volatility", "coin"}


def test_coin_reversal_skips_btc_rs_for_btc_itself_gracefully():
    """run.py에서 KRW-BTC 자기 자신은 btc_df=None으로 넘긴다 — 크래시 없이 90점 재환산되는지 확인."""
    df = _w_bottom_ohlcv(seed=7, rally_drift=0.012)
    res = REV.evaluate_reversal(df, "coin", btc_df=None)
    assert res is not None
    assert "coin" not in res["category_scores"]
