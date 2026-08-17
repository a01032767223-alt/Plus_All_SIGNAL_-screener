"""추세전환 초기진입 탐지 — 장기 하락 후 상승 전환 초기 구간을 정량화한다.

목표는 '바닥을 정확히 맞추는 것'이 아니라, 하락 추세 → 하락세 둔화 → 바닥 형성 →
초기 상승 전환의 과정을 여러 독립 지표로 나눠 점수화해서 다른 사람보다 먼저
후보를 좁히는 것이다. 그래서 RSI 30 이하, MACD 골든크로스 같은 단일 지표 하나로
판단하지 않고, 아래 6개 카테고리(가격구조·거래량·모멘텀·이동평균·변동성·코인특화)를
전부 독립적인 증거로 취급해 가중합한다.

먼저 '최근 120봉 고점 대비 15% 이상 하락한 종목'만 후보로 본다 — 이미 상승 추세인
종목이나 하락폭이 미미한 종목까지 '반전 후보'로 잡으면 기존 매수신호 탭과 겹치고
노이즈만 늘어난다.

점수 = 0~100. 배점은 다음과 같다 (합계 100, 코인이 아니면 코인특화 10점을 뺀
90점 만점을 100점으로 재환산한다):
  가격구조 25 · 거래량 20 · 모멘텀 20 · 이동평균 15 · 변동성 10 · 코인특화 10

원 스펙에는 코인 특화 신호로 Funding Rate·OI·롱숏비율·청산 데이터도 있었지만,
이 앱의 데이터 소스인 업비트는 현물(spot) 전용 거래소라 파생상품 데이터를
제공하지 않는다. 별도 선물 거래소(바이낸스 등) 연동은 종목 코드 매핑, 별도 API
키·레이트리밋 관리 등 리스크가 커서 이번 범위에서는 뺐다 — README에 명시한다.
대신 이미 갖고 있는 데이터만으로 신뢰성 있게 계산 가능한 'BTC 대비 상대강도'를
코인 특화 점수로 쓴다.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from . import indicators as I
from . import rules as R

# ─────────────────────────────────────────────────────────
# 파라미터
# ─────────────────────────────────────────────────────────
LOOKBACK_MAX = 120      # 하락 구간을 살펴볼 최대 봉수 (스펙: 20~120일)
LOOKBACK_MIN = 20
MIN_DECLINE_PCT = 15.0  # 이 이상 하락한 종목만 반전 후보로 본다
SWING_ORDER = 3         # 스윙 고/저 판정 시 좌우로 비교할 봉수
BTC_RS_LOOKBACK = 20    # BTC 대비 상대강도 비교 구간

EARLY_MIN_SCORE = 35.0     # 이 미만이면 후보에서 제외
CONFIRMED_MIN_SCORE = 0.0  # 돌파만 확인되면 점수와 무관하게 '확인 진입' (아래 stage_of 참고)

CAT_LABELS = {
    "structure": "가격 구조", "volume": "거래량", "momentum": "모멘텀",
    "ma": "이동평균", "volatility": "변동성/시장구조", "coin": "코인 특화",
}
CAT_MAX = {"structure": 25.0, "volume": 20.0, "momentum": 20.0,
           "ma": 15.0, "volatility": 10.0, "coin": 10.0}

STAGE_LABEL = {
    "early": "초기 진입", "confirmed": "확인 진입",
}
STAGE_HELP = {
    "early": "가격이 아직 직전 반등 고점(저항)을 못 뚫었지만, 신저가 실패·다이버전스·"
             "거래량 변화 같은 사전 신호가 여러 개 겹치기 시작한 단계입니다. "
             "먼저 들어가는 만큼 되돌림(가짜 반등) 위험도 가장 큽니다.",
    "confirmed": "가격이 실제로 직전 반등 고점(저항)을 뚫고 올라와, 하락 추세가 "
                 "구조적으로 꺾였다는 게 데이터로 확인된 단계입니다. 초기 진입보다 "
                 "늦게 들어가지만 다시 신저가로 미끄러질 위험은 상대적으로 낮습니다.",
}


def _fmt(v) -> str:
    return R.fmt_price(v)


# ─────────────────────────────────────────────────────────
# 스윙 고점/저점
# ─────────────────────────────────────────────────────────
def _swing_lows(s: pd.Series, order: int = SWING_ORDER) -> pd.Series:
    """좌우 order봉보다 낮은(=확정된) 국지적 저점 위치만 True.
    마지막 order봉은 아직 확정되지 않으므로 자연히 제외된다."""
    cond = pd.Series(True, index=s.index)
    for k in range(1, order + 1):
        cond &= s <= s.shift(k)
        cond &= s <= s.shift(-k)
    return cond.fillna(False)


def _swing_highs(s: pd.Series, order: int = SWING_ORDER) -> pd.Series:
    cond = pd.Series(True, index=s.index)
    for k in range(1, order + 1):
        cond &= s >= s.shift(k)
        cond &= s >= s.shift(-k)
    return cond.fillna(False)


# ─────────────────────────────────────────────────────────
# 하락 여부 게이트 (반전 후보 자격)
# ─────────────────────────────────────────────────────────
def decline_context(df: pd.DataFrame) -> dict | None:
    window = df.tail(LOOKBACK_MAX)
    if len(window) < LOOKBACK_MIN:
        return None
    hi_idx = window["high"].idxmax()
    hi_val = float(window.loc[hi_idx, "high"])
    cur_close = float(df["close"].iloc[-1])
    if hi_val <= 0:
        return None
    decline_pct = (cur_close / hi_val - 1) * 100.0
    bars_since_high = len(window.loc[hi_idx:]) - 1
    return {"high": hi_val, "high_date": hi_idx,
            "decline_pct": decline_pct, "bars_since_high": bars_since_high}


# ─────────────────────────────────────────────────────────
# A. 가격 구조 (25점)
# ─────────────────────────────────────────────────────────
def _sig_no_new_low(window: pd.DataFrame):
    if len(window) < 40:
        return 0.0, False, "데이터 부족"
    recent, prior = window["low"].tail(20), window["low"].iloc[-40:-20]
    if prior.empty or recent.empty:
        return 0.0, False, "데이터 부족"
    r_min, p_min = float(recent.min()), float(prior.min())
    if r_min >= p_min:
        return 5.0, True, f"최근 20봉 최저가({_fmt(r_min)})가 이전 20봉 최저가" \
                           f"({_fmt(p_min)}) 이상 — 신저가 없음"
    gap = (p_min / r_min - 1) * 100
    return max(0.0, 5.0 - gap * 0.5), False, f"최근 20봉에서 신저가 갱신 (이전 저점 대비 −{gap:.1f}%)"


def _sig_higher_low(sw_lows: pd.Series):
    if len(sw_lows) < 2:
        return 0.0, False, "확정된 저점이 2개 미만 — 판단 불가"
    l1, l2 = float(sw_lows.iloc[-2]), float(sw_lows.iloc[-1])
    if l2 > l1:
        rise = (l2 / l1 - 1) * 100
        return min(5.0, 2.5 + rise * 0.3), True, \
            f"Higher Low 형성: {_fmt(l1)} → {_fmt(l2)} (+{rise:.1f}%)"
    return 0.0, False, f"저점이 아직 낮아지는 중 ({_fmt(l1)} → {_fmt(l2)})"


def _sig_swing_high_break(window: pd.DataFrame, sw_lows: pd.Series, cur_close: float):
    if sw_lows.empty:
        return 0.0, False, "확정된 저점 없음 — 돌파 기준선 산출 불가", None
    last_low_date = sw_lows.index[-1]
    after = window.loc[last_low_date:, "high"]
    if len(after) < 3:
        return 0.0, False, "저점 이후 데이터 부족", None
    neckline = float(after.iloc[:-1].max())
    if cur_close > neckline:
        over = (cur_close / neckline - 1) * 100
        return min(10.0, 7.0 + over * 0.6), True, \
            f"저점 이후 직전 반등 고점({_fmt(neckline)}) 돌파 (+{over:.1f}%)", neckline
    dist = (neckline / cur_close - 1) * 100
    return max(0.0, 4.0 - dist * 0.2), False, \
        f"직전 반등 고점({_fmt(neckline)})까지 {dist:.1f}% 남음", neckline


def _sig_trendline_break(window: pd.DataFrame, cur_close: float):
    sw_high_mask = _swing_highs(window["high"])
    sw_highs = window["high"][sw_high_mask]
    if len(sw_highs) < 2:
        return 0.0, False, "하락추세선을 그릴 고점이 부족함"
    xs = np.array([window.index.get_loc(d) for d in sw_highs.index], dtype=float)
    ys = sw_highs.values.astype(float)
    slope, intercept = np.polyfit(xs, ys, 1)
    if slope >= 0:
        return 0.0, False, "최근 고점들이 하락 추세선을 이루지 않음"
    line_val = slope * (len(window) - 1) + intercept
    if line_val <= 0:
        return 0.0, False, "추세선 값 이상 — 판단 불가"
    if cur_close > line_val:
        return 5.0, True, f"고점을 연결한 장기 하락추세선({_fmt(line_val)}) 상향 돌파"
    return 0.0, False, f"하락추세선({_fmt(line_val)}) 아직 못 넘음"


# ─────────────────────────────────────────────────────────
# B. 거래량 (20점)
# ─────────────────────────────────────────────────────────
def _sig_vol_decline_shrink(window: pd.DataFrame):
    if len(window) < 40:
        return 0.0, False, "데이터 부족"
    ret = window["close"].pct_change()
    down_recent = window["volume"].tail(20)[ret.tail(20) < 0]
    down_prior = window["volume"].iloc[-40:-20][ret.iloc[-40:-20] < 0]
    if down_recent.empty or down_prior.empty:
        return 0.0, False, "하락일 표본 부족"
    r_avg, p_avg = float(down_recent.mean()), float(down_prior.mean())
    if r_avg < p_avg:
        shrink = (1 - r_avg / p_avg) * 100
        return min(5.0, shrink * 0.15), True, f"하락일 평균 거래량 {shrink:.0f}% 감소 — 매도 압력 약화"
    return 0.0, False, "하락일 거래량이 줄지 않음"


def _sig_vol_rally_increase(window: pd.DataFrame):
    if len(window) < 40:
        return 0.0, False, "데이터 부족"
    ret = window["close"].pct_change()
    up_recent = window["volume"].tail(20)[ret.tail(20) > 0]
    up_prior = window["volume"].iloc[-40:-20][ret.iloc[-40:-20] > 0]
    if up_recent.empty or up_prior.empty:
        return 0.0, False, "상승일 표본 부족"
    r_avg, p_avg = float(up_recent.mean()), float(up_prior.mean())
    if r_avg > p_avg:
        grow = (r_avg / p_avg - 1) * 100
        return min(5.0, grow * 0.1), True, f"상승일 평균 거래량 {grow:.0f}% 증가 — 매수세 유입"
    return 0.0, False, "상승일 거래량이 늘지 않음"


def _sig_vol_breakout(cur):
    vr, ret = R._f(cur.get("vol_ratio")), R._f(cur.get("ret_pct"), 0.0)
    if math.isnan(vr):
        return 0.0, False, "거래량 데이터 부족"
    if vr >= 1.3 and ret > 0:
        return min(5.0, 2.0 + (vr - 1.3) * 3), True, \
            f"거래량 20일 평균의 {vr:.1f}배 + 상승 마감({ret:+.1f}%)"
    return 0.0, False, f"거래량 평균의 {vr:.1f}배 — 돌파 기준(1.3배) 미달"


def _sig_obv_divergence(window: pd.DataFrame, sw_lows: pd.Series):
    if len(sw_lows) < 2:
        return 0.0, False, "확정된 저점 2개 미만 — 판단 불가"
    d1, d2 = sw_lows.index[-2], sw_lows.index[-1]
    p1, p2 = float(sw_lows.loc[d1]), float(sw_lows.loc[d2])
    o1, o2 = window.loc[d1, "obv"], window.loc[d2, "obv"]
    if pd.isna(o1) or pd.isna(o2):
        return 0.0, False, "OBV 데이터 부족"
    if p2 <= p1 and o2 > o1:
        return 5.0, True, "가격 저점은 낮아졌지만 OBV는 높아짐 — 상승 다이버전스(매집 추정)"
    if p2 > p1 and o2 > o1:
        return 3.0, True, "저점·OBV 모두 상승 (다이버전스는 아니지만 방향 일치)"
    return 0.0, False, "OBV 다이버전스 미확인"


# ─────────────────────────────────────────────────────────
# C. 모멘텀 (20점)
# ─────────────────────────────────────────────────────────
def _sig_rsi_divergence(window: pd.DataFrame, sw_lows: pd.Series):
    if len(sw_lows) < 2:
        return 0.0, False, "확정된 저점 2개 미만 — 판단 불가"
    d1, d2 = sw_lows.index[-2], sw_lows.index[-1]
    p1, p2 = float(sw_lows.loc[d1]), float(sw_lows.loc[d2])
    r1, r2 = window.loc[d1, "rsi"], window.loc[d2, "rsi"]
    if pd.isna(r1) or pd.isna(r2):
        return 0.0, False, "RSI 데이터 부족"
    if p2 <= p1 and r2 > r1:
        return min(7.0, 4.0 + (r2 - r1) * 0.3), True, \
            f"가격 저점은 낮아졌지만 RSI는 상승({r1:.0f}→{r2:.0f}) — 상승 다이버전스"
    return 0.0, False, "RSI 다이버전스 미확인"


def _sig_macd_divergence(window: pd.DataFrame, sw_lows: pd.Series):
    if len(sw_lows) < 2:
        return 0.0, False, "확정된 저점 2개 미만 — 판단 불가"
    d1, d2 = sw_lows.index[-2], sw_lows.index[-1]
    p1, p2 = float(sw_lows.loc[d1]), float(sw_lows.loc[d2])
    m1, m2 = window.loc[d1, "macd"], window.loc[d2, "macd"]
    if pd.isna(m1) or pd.isna(m2):
        return 0.0, False, "MACD 데이터 부족"
    if p2 <= p1 and m2 > m1:
        return 7.0, True, "가격 저점은 낮아졌지만 MACD는 상승 — 상승 다이버전스"
    return 0.0, False, "MACD 다이버전스 미확인"


def _sig_macd_hist_improve(edf: pd.DataFrame):
    h = edf["macd_hist"].tail(5)
    if len(h) < 5 or h.isna().any():
        return 0.0, False, "데이터 부족"
    improving = h.iloc[-1] > h.iloc[0] and (h.diff().tail(3) > 0).sum() >= 2
    if improving:
        return 3.0, True, f"MACD 히스토그램 개선 중 ({h.iloc[0]:.2f} → {h.iloc[-1]:.2f})"
    return 0.0, False, "히스토그램 개선 뚜렷하지 않음"


def _sig_stoch_reversal(cur, prev):
    k, d = R._f(cur.get("stoch_k")), R._f(cur.get("stoch_d"))
    kp, dp = R._f(prev.get("stoch_k")), R._f(prev.get("stoch_d"))
    if any(math.isnan(v) for v in (k, d, kp, dp)):
        return 0.0, False, "스토캐스틱 데이터 부족"
    cross_up = kp <= dp and k > d
    from_oversold = kp <= 25 or dp <= 25
    if cross_up and from_oversold:
        return 3.0, True, f"스토캐스틱 과매도권에서 %K가 %D 상향 돌파 ({k:.0f}/{d:.0f})"
    if k > kp and k <= 30:
        return 1.5, False, f"스토캐스틱 저점권에서 반등 중 ({k:.0f})"
    return 0.0, False, f"스토캐스틱 반전 신호 없음 ({k:.0f}/{d:.0f})"


# ─────────────────────────────────────────────────────────
# D. 이동평균 (15점)
# ─────────────────────────────────────────────────────────
def _sig_ma20_recover(edf: pd.DataFrame):
    cur = edf.iloc[-1]
    close, ma20 = R._f(cur["close"]), R._f(cur.get("ma20"))
    if math.isnan(ma20):
        return 0.0, False, "20일선 데이터 부족"
    tail10 = edf.tail(10)
    was_below = (tail10["close"] < tail10["ma20"]).sum() >= 5
    if close > ma20 and was_below:
        return 5.0, True, f"최근까지 20일선 아래였다가 회복 (현재가 {_fmt(close)} > 20MA {_fmt(ma20)})"
    if close > ma20:
        return 3.0, True, "20일선 위 유지"
    return 0.0, False, "아직 20일선 아래"


def _sig_ma20_flatten(edf: pd.DataFrame):
    ma = edf["ma20"].tail(15)
    if len(ma) < 15 or ma.isna().any():
        return 0.0, False, "데이터 부족"
    slope_recent = ma.iloc[-1] - ma.iloc[-6]
    slope_prior = ma.iloc[-6] - ma.iloc[-11]
    if slope_recent > slope_prior:
        return 3.0, True, "20일선 하락 기울기가 완만해지는 중"
    return 0.0, False, "20일선 하락세 지속"


def _sig_ma20_turn_up(edf: pd.DataFrame):
    ma = edf["ma20"].tail(6)
    if len(ma) < 6 or ma.isna().any():
        return 0.0, False, "데이터 부족"
    if ma.iloc[-1] > ma.iloc[0]:
        return 3.0, True, "20일선 자체가 상승 전환"
    return 0.0, False, "20일선 아직 상승 전환 아님"


def _sig_ma60_recover(cur):
    close, ma60 = R._f(cur["close"]), R._f(cur.get("ma60"))
    if math.isnan(ma60):
        return 0.0, False, "60일선 데이터 부족"
    if close > ma60:
        return 4.0, True, f"60일선({_fmt(ma60)}) 회복"
    dist = (ma60 / close - 1) * 100
    return 0.0, False, f"60일선까지 {dist:.1f}% 남음"


# ─────────────────────────────────────────────────────────
# E. 변동성 / 시장구조 (10점)
# ─────────────────────────────────────────────────────────
def _sig_atr_pattern(edf: pd.DataFrame):
    atrp = edf["atr_pct"].tail(40)
    if len(atrp) < 40 or atrp.isna().sum() > 5:
        return 0.0, False, "데이터 부족"
    prior_min = atrp.iloc[:-10].min()
    recent = atrp.iloc[-10:]
    if recent.iloc[-1] > recent.min() and recent.min() <= prior_min * 1.1:
        return 3.0, True, "변동성 수축 후 재확대 국면"
    return 0.0, False, "변동성 수축→확대 패턴 미확인"


def _sig_bb_squeeze_break(edf: pd.DataFrame):
    cur = edf.iloc[-1]
    wp, pb = R._f(cur.get("bb_width_pct")), R._f(cur.get("bb_pct_b"))
    if math.isnan(wp) or math.isnan(pb):
        return 0.0, False, "볼린저밴드 데이터 부족"
    was_squeeze = bool((edf["bb_width_pct"].tail(15) <= 0.25).any())
    if was_squeeze and pb >= 0.6:
        return 4.0, True, "밴드 수축 후 상단 방향 돌파"
    return 0.0, False, "밴드 수축·돌파 패턴 미확인"


def _sig_vol_structure(window: pd.DataFrame):
    ret = window["close"].pct_change()
    if len(ret) < 40:
        return 0.0, False, "데이터 부족"
    recent_std, decline_std = ret.tail(10).std(), ret.iloc[-40:-10].std()
    if pd.isna(recent_std) or pd.isna(decline_std) or decline_std == 0:
        return 0.0, False, "데이터 부족"
    if recent_std < decline_std * 0.7:
        return 3.0, True, "저점권 일일 변동성이 하락 구간 대비 눈에 띄게 진정됨"
    return 0.0, False, "변동성 진정 뚜렷하지 않음"


# ─────────────────────────────────────────────────────────
# F. 코인 특화 (10점) — BTC 대비 상대강도만 사용 (아래 모듈 docstring 참고)
# ─────────────────────────────────────────────────────────
def _sig_btc_relative_strength(edf: pd.DataFrame, btc_edf: pd.DataFrame | None,
                                lookback: int = BTC_RS_LOOKBACK):
    if btc_edf is None or len(btc_edf) <= lookback or len(edf) <= lookback:
        return 0.0, False, "BTC 비교 데이터 없음"
    alt_ret = (edf["close"].iloc[-1] / edf["close"].iloc[-lookback - 1] - 1) * 100
    btc_ret = (btc_edf["close"].iloc[-1] / btc_edf["close"].iloc[-lookback - 1] - 1) * 100
    rel = alt_ret - btc_ret
    if rel > 0:
        return min(10.0, 4.0 + rel * 0.3), True, \
            f"최근 {lookback}일 BTC 대비 상대수익률 +{rel:.1f}%p (BTC {btc_ret:+.1f}% · 본종목 {alt_ret:+.1f}%)"
    return 0.0, False, f"최근 {lookback}일 BTC 대비 상대수익률 {rel:.1f}%p — 아직 열위"


# ─────────────────────────────────────────────────────────
# 진입 단계 구분
# ─────────────────────────────────────────────────────────
def stage_of(total: float, breakout_confirmed: bool) -> str | None:
    """가장 중요한 구분: '초기 진입' vs '확인 진입'.

    확인 진입 = 가격이 실제로 직전 반등 고점(저항)을 돌파해, 구조적 전환이 데이터로
    확인된 상태. 초기 진입 = 아직 돌파 전이지만 여러 독립 신호가 쌓이기 시작한 상태
    (더 빨리 잡을 수 있는 대신 되돌림 위험이 큼)."""
    if total < EARLY_MIN_SCORE:
        return None
    return "confirmed" if breakout_confirmed else "early"


# ─────────────────────────────────────────────────────────
# 종합 평가
# ─────────────────────────────────────────────────────────
def evaluate_reversal(df: pd.DataFrame, asset_class: str,
                       btc_df: pd.DataFrame | None = None) -> dict | None:
    """일봉 OHLCV(df) 기준 추세전환 초기탐지 평가.

    asset_class: "kr" | "us" | "coin". coin이고 btc_df가 주어지면 코인 특화 10점이
    합산되고, 아니면 90점 만점을 100점으로 재환산한다 (TF_WEIGHTS 재정규화와 같은 방식).
    반환값이 None이면: 데이터 부족, 또는 최근 120봉 고점 대비 15% 이상 하락하지
    않아 애초에 '반전 후보' 대상이 아님.
    """
    if df is None or len(df) < LOOKBACK_MIN + 20:
        return None
    ctx = decline_context(df)
    if ctx is None or ctx["decline_pct"] > -MIN_DECLINE_PCT:
        return None

    edf = I.enrich(df)
    window = edf.tail(LOOKBACK_MAX)
    cur = edf.iloc[-1]
    prev = edf.iloc[-2] if len(edf) >= 2 else cur
    sw_lows = window["low"][_swing_lows(window["low"])]

    reasons: list[dict] = []

    def add(cat, key, label, pts, ok, detail):
        reasons.append({"cat": cat, "key": key, "label": label,
                        "points": round(float(pts), 2), "ok": bool(ok), "detail": detail})

    cat_scores = {c: 0.0 for c in CAT_MAX}
    breakout_confirmed = False

    # A. 가격구조
    p, ok, d = _sig_no_new_low(window)
    cat_scores["structure"] += p; add("structure", "no_new_low", "신저가 발생 실패", p, ok, d)
    p, ok, d = _sig_higher_low(sw_lows)
    cat_scores["structure"] += p; add("structure", "higher_low", "Higher Low 형성", p, ok, d)
    p, ok, d, _neck = _sig_swing_high_break(window, sw_lows, float(cur["close"]))
    cat_scores["structure"] += p; add("structure", "swing_high_break", "직전 Swing High 돌파", p, ok, d)
    breakout_confirmed = ok
    p, ok, d = _sig_trendline_break(window, float(cur["close"]))
    cat_scores["structure"] += p; add("structure", "trendline_break", "장기 하락추세선 돌파", p, ok, d)

    # B. 거래량
    p, ok, d = _sig_vol_decline_shrink(window)
    cat_scores["volume"] += p; add("volume", "vol_decline_shrink", "하락 거래량 감소", p, ok, d)
    p, ok, d = _sig_vol_rally_increase(window)
    cat_scores["volume"] += p; add("volume", "vol_rally_increase", "상승 시 거래량 증가", p, ok, d)
    p, ok, d = _sig_vol_breakout(cur)
    cat_scores["volume"] += p; add("volume", "vol_breakout", "거래량 돌파", p, ok, d)
    p, ok, d = _sig_obv_divergence(window, sw_lows)
    cat_scores["volume"] += p; add("volume", "obv_divergence", "OBV 상승 다이버전스", p, ok, d)

    # C. 모멘텀
    p, ok, d = _sig_rsi_divergence(window, sw_lows)
    cat_scores["momentum"] += p; add("momentum", "rsi_divergence", "RSI 상승 다이버전스", p, ok, d)
    p, ok, d = _sig_macd_divergence(window, sw_lows)
    cat_scores["momentum"] += p; add("momentum", "macd_divergence", "MACD 상승 다이버전스", p, ok, d)
    p, ok, d = _sig_macd_hist_improve(edf)
    cat_scores["momentum"] += p; add("momentum", "macd_hist", "MACD 히스토그램 개선", p, ok, d)
    p, ok, d = _sig_stoch_reversal(cur, prev)
    cat_scores["momentum"] += p; add("momentum", "stoch_reversal", "스토캐스틱 반전", p, ok, d)

    # D. 이동평균
    p, ok, d = _sig_ma20_recover(edf)
    cat_scores["ma"] += p; add("ma", "ma20_recover", "20일선 회복", p, ok, d)
    p, ok, d = _sig_ma20_flatten(edf)
    cat_scores["ma"] += p; add("ma", "ma20_flatten", "20일선 하락세 종료", p, ok, d)
    p, ok, d = _sig_ma20_turn_up(edf)
    cat_scores["ma"] += p; add("ma", "ma20_turn_up", "20일선 상승 전환", p, ok, d)
    p, ok, d = _sig_ma60_recover(cur)
    cat_scores["ma"] += p; add("ma", "ma60_recover", "60일선 회복", p, ok, d)

    # E. 변동성/시장구조
    p, ok, d = _sig_atr_pattern(edf)
    cat_scores["volatility"] += p; add("volatility", "atr_pattern", "ATR 수축 후 확대", p, ok, d)
    p, ok, d = _sig_bb_squeeze_break(edf)
    cat_scores["volatility"] += p; add("volatility", "bb_squeeze", "볼린저밴드 수축 후 상방 돌파", p, ok, d)
    p, ok, d = _sig_vol_structure(window)
    cat_scores["volatility"] += p; add("volatility", "vol_structure", "저점 변동성 구조 변화", p, ok, d)

    # F. 코인 특화
    coin_available = asset_class == "coin" and btc_df is not None
    if coin_available:
        p, ok, d = _sig_btc_relative_strength(edf, btc_df)
        cat_scores["coin"] += p; add("coin", "btc_rs", "BTC 대비 상대강도", p, ok, d)

    max_possible = 100.0 if coin_available else 90.0
    total_raw = sum(cat_scores.values())
    score = round(min(100.0, total_raw * 100.0 / max_possible), 1)
    stage = stage_of(score, breakout_confirmed)

    ok_reasons = sorted([r for r in reasons if r["ok"] and r["points"] > 0],
                        key=lambda r: -r["points"])

    return {
        "score": score,
        "stage": stage,
        "stage_label": STAGE_LABEL.get(stage, ""),
        "breakout_confirmed": bool(breakout_confirmed),
        "decline_pct": round(ctx["decline_pct"], 1),
        "high_price": round(ctx["high"], 4),
        "bars_since_high": ctx["bars_since_high"],
        "category_scores": {k: round(v, 1) for k, v in cat_scores.items() if k != "coin" or coin_available},
        "category_max": {k: v for k, v in CAT_MAX.items() if k != "coin" or coin_available},
        "signals": reasons,
        "reasons": ok_reasons[:6],          # "왜 이 종목인가"
        "signals_passed": sum(1 for r in reasons if r["ok"]),
        "signals_total": len(reasons),
        "price": round(float(cur["close"]), 4),
        "change_pct": round(R._f(cur.get("ret_pct"), 0.0), 2),
    }
