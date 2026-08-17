"""지표 → 0~100 점수 변환 규칙.

각 함수는 (score, detail, ok) 를 돌려준다.
  score : 0~100 연속값 (순위를 만들기 위해 이분법을 쓰지 않는다)
  detail: 대시보드에 그대로 보여줄 사람말 설명
  ok    : 사용자가 정의한 '핵심 매수 조건' 충족 여부 (체크리스트용)
"""
from __future__ import annotations

import math

import numpy as np

from . import config as C


def _f(x, default=float("nan")) -> float:
    """NaN/None 안전 변환."""
    try:
        v = float(x)
        return default if math.isnan(v) or math.isinf(v) else v
    except (TypeError, ValueError):
        return default


def _clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


def _interp(x: float, xs: list[float], ys: list[float]) -> float:
    """구간 선형보간 — 지표값을 점수로 부드럽게 매핑."""
    return float(np.interp(x, xs, ys))


# ─────────────────────────────────────────────────────────
# 1. 이동평균선 (25%)  — 20MA > 60MA + 가격이 20MA 위
# ─────────────────────────────────────────────────────────
def score_ma(cur, prev5) -> tuple[float, str, bool]:
    close = _f(cur["close"])
    ma5, ma20, ma60 = _f(cur.get("ma5")), _f(cur.get("ma20")), _f(cur.get("ma60"))
    if any(math.isnan(v) for v in (close, ma20, ma60)):
        return 0.0, "이동평균 계산에 필요한 데이터 부족", False

    pts, notes = 0.0, []

    # 20MA > 60MA (중기 추세 전환)
    gap = (ma20 / ma60 - 1) * 100 if ma60 else 0.0
    if ma20 > ma60:
        pts += _interp(gap, [0, 3], [30, 40])
        notes.append(f"20MA>60MA (+{gap:.1f}%)")
    else:
        pts += _interp(gap, [-5, 0], [0, 15])
        notes.append(f"20MA<60MA ({gap:.1f}%)")

    # 종가 > 20MA
    dev = (close / ma20 - 1) * 100 if ma20 else 0.0
    if close > ma20:
        pts += 30
        notes.append(f"종가 20MA 위 (이격 {dev:+.1f}%)")
    else:
        pts += _interp(dev, [-5, 0], [0, 12])
        notes.append(f"종가 20MA 아래 ({dev:+.1f}%)")

    # 20MA 기울기 (5봉)
    ma20_prev = _f(prev5.get("ma20")) if prev5 is not None else float("nan")
    slope = (ma20 / ma20_prev - 1) * 100 if ma20_prev and not math.isnan(ma20_prev) else 0.0
    pts += _interp(slope, [-1, 0, 1.5], [0, 8, 20])
    notes.append(f"20MA 5일 기울기 {slope:+.2f}%")

    # 정배열
    if not math.isnan(ma5) and ma5 > ma20 > ma60:
        pts += 10
        notes.append("정배열(5>20>60)")

    # 과열 감점 — 20MA 대비 15% 이상 벌어지면 추격매수 구간
    if dev > 15:
        penalty = min(25.0, (dev - 15) * 1.5)
        pts -= penalty
        notes.append(f"과열 감점 −{penalty:.0f}")

    ok = (ma20 > ma60) and (close > ma20)
    return _clamp(pts), " · ".join(notes), ok


# ─────────────────────────────────────────────────────────
# 2. 거래량 (20%) — 상승 시 거래량 ≥ 평균의 1.5배
# ─────────────────────────────────────────────────────────
def score_volume(cur) -> tuple[float, str, bool]:
    vr = _f(cur.get("vol_ratio"))
    ret = _f(cur.get("ret_pct"), 0.0)
    if math.isnan(vr):
        return 0.0, "거래량 평균 산출 불가", False

    base = _interp(vr, [0.3, 0.7, 1.0, 1.5, 2.0, 2.5, 4.0],
                       [5,   18,  30,  65,  85,  100, 100])
    if ret < -0.5:
        base *= 0.40
        note = f"거래량 {vr:.1f}배지만 하락 마감({ret:+.1f}%) → 매도 물량 가능"
    elif ret < 0.5:
        base *= 0.75
        note = f"거래량 {vr:.1f}배, 보합({ret:+.1f}%)"
    else:
        note = f"상승({ret:+.1f}%) + 거래량 20일 평균의 {vr:.1f}배"

    ok = (vr >= 1.5) and (ret > 0)
    return _clamp(base), note, ok


# ─────────────────────────────────────────────────────────
# 3. 가격구조 (20%) — 저항 돌파 또는 주요 지지선 반등
# ─────────────────────────────────────────────────────────
def score_structure(cur) -> tuple[float, str, bool]:
    close = _f(cur["close"])
    prior_high, low_n, high_n = _f(cur.get("prior_high")), _f(cur.get("low_n")), _f(cur.get("high_n"))
    ma20, ma60 = _f(cur.get("ma20")), _f(cur.get("ma60"))
    ret = _f(cur.get("ret_pct"), 0.0)
    if math.isnan(prior_high) or math.isnan(low_n) or math.isnan(high_n) or high_n <= low_n:
        return 0.0, "가격 구조 판단에 필요한 데이터 부족", False

    # 60봉 박스 내 위치 (0=바닥, 1=천장)
    pos = (close - low_n) / (high_n - low_n)

    # (a) 저항 돌파
    if close > prior_high:
        over = (close / prior_high - 1) * 100
        return _clamp(_interp(over, [0, 3], [92, 100])), \
               f"60봉 최고가 돌파 (+{over:.1f}%)", True
    if close >= prior_high * 0.98:
        return 82.0, f"저항({fmt_price(prior_high)}) 돌파 임박 — 박스 상단 {pos*100:.0f}% 지점", True

    # (b) 주요 지지선 반등
    supports = [("20MA", ma20), ("60MA", ma60), ("직전 저점", _f(cur.get("prior_low")))]
    for name, lvl in supports:
        if math.isnan(lvl) or lvl <= 0:
            continue
        near = abs(close / lvl - 1) * 100
        if near <= 3 and ret > 0:
            return _clamp(_interp(near, [0, 3], [80, 68])), \
                   f"{name}({fmt_price(lvl)}) 지지 반등 ({ret:+.1f}%)", True

    # (c) 그 외 — 박스 내 위치로 채점
    sc = _interp(pos, [0.0, 0.3, 0.6, 0.85, 1.0], [15, 28, 55, 72, 80])
    if ret <= 0:
        sc *= 0.85
    return _clamp(sc), f"박스 내 {pos*100:.0f}% 지점 (저항 {fmt_price(prior_high)})", False


# ─────────────────────────────────────────────────────────
# 4. RSI (10%) — 30~50에서 상승 전환
# ─────────────────────────────────────────────────────────
def score_rsi(cur, prev) -> tuple[float, str, bool]:
    r = _f(cur.get("rsi"))
    rp = _f(prev.get("rsi")) if prev is not None else float("nan")
    if math.isnan(r):
        return 0.0, "RSI 데이터 부족", False
    rising = (not math.isnan(rp)) and r > rp
    arrow = "상승 중" if rising else "하락·횡보"

    if 30 <= r <= 50:
        sc = 100.0 if rising else 45.0
    elif 50 < r <= 60:
        sc = 80.0 if rising else 50.0
    elif 60 < r <= 70:
        sc = 55.0 if rising else 38.0
    elif r > 70:
        sc = _clamp(_interp(r, [70, 85], [40, 10]))
    else:  # r < 30
        sc = 35.0 if rising else 12.0

    ok = (30 <= r <= 50) and rising
    return _clamp(sc), f"RSI {r:.1f} ({arrow})", ok


# ─────────────────────────────────────────────────────────
# 5. MACD (8%) — 골든크로스 + 히스토그램 증가
# ─────────────────────────────────────────────────────────
def score_macd(cur, prev, recent) -> tuple[float, str, bool]:
    m, s, h = _f(cur.get("macd")), _f(cur.get("macd_signal")), _f(cur.get("macd_hist"))
    hp = _f(prev.get("macd_hist")) if prev is not None else float("nan")
    if any(math.isnan(v) for v in (m, s, h)):
        return 0.0, "MACD 데이터 부족", False

    hist_up = (not math.isnan(hp)) and h > hp
    # 최근 3봉 내 골든크로스 여부
    gc = False
    if recent is not None and len(recent) >= 2:
        diff = (recent["macd"] - recent["macd_signal"]).tail(4).tolist()
        for i in range(1, len(diff)):
            if diff[i - 1] <= 0 < diff[i]:
                gc = True

    if gc and hist_up:
        sc, note = 100.0, "골든크로스 3봉 이내 + 히스토그램 증가"
    elif gc:
        sc, note = 88.0, "골든크로스 3봉 이내"
    elif m > s and hist_up:
        sc, note = 82.0, "시그널 위 + 히스토그램 증가"
    elif m > s:
        sc, note = 66.0, "시그널 위 (모멘텀 둔화)"
    elif hist_up:
        sc, note = 45.0, "시그널 아래지만 히스토그램 축소(반등 조짐)"
    else:
        sc, note = 15.0, "데드크로스 구간"

    if m > 0:
        sc = _clamp(sc + 5)
        note += " · MACD 0선 위"

    ok = gc and hist_up
    return _clamp(sc), note, ok


# ─────────────────────────────────────────────────────────
# 6. 볼린저밴드 (7%) — 하단 반등 또는 수축 후 상단 돌파
# ─────────────────────────────────────────────────────────
def score_bb(cur) -> tuple[float, str, bool]:
    close, up, lo = _f(cur["close"]), _f(cur.get("bb_upper")), _f(cur.get("bb_lower"))
    pct_b, wpct = _f(cur.get("bb_pct_b")), _f(cur.get("bb_width_pct"))
    ret = _f(cur.get("ret_pct"), 0.0)
    if any(math.isnan(v) for v in (up, lo, pct_b)):
        return 0.0, "볼린저밴드 데이터 부족", False

    squeeze = (not math.isnan(wpct)) and wpct <= 0.20
    sq_txt = "밴드 수축 후 " if squeeze else ""

    if close > up:
        sc = 100.0 if squeeze else 74.0
        note, ok = f"{sq_txt}상단 돌파 (%B {pct_b:.2f})", squeeze
    elif squeeze and pct_b >= 0.55:
        sc, note, ok = 86.0, f"밴드 수축 + 상단 방향 (%B {pct_b:.2f})", True
    elif pct_b <= 0.15 and ret > 0:
        sc, note, ok = 80.0, f"하단 반등 (%B {pct_b:.2f}, {ret:+.1f}%)", True
    elif 0.5 <= pct_b <= 0.9:
        sc, note, ok = 64.0, f"중단선 위 진행 (%B {pct_b:.2f})", False
    else:
        sc = _interp(pct_b, [0.0, 0.3, 0.5], [25, 38, 52])
        note, ok = f"%B {pct_b:.2f}", False
    return _clamp(sc), note, ok


# ─────────────────────────────────────────────────────────
# 7. ADX (5%) — ADX > 20~25 이면서 +DI > -DI
# ─────────────────────────────────────────────────────────
def score_adx(cur) -> tuple[float, str, bool]:
    a, p, m = _f(cur.get("adx")), _f(cur.get("plus_di")), _f(cur.get("minus_di"))
    if any(math.isnan(v) for v in (a, p, m)):
        return 0.0, "ADX 데이터 부족", False
    bull = p > m
    if not bull:
        sc = _clamp(_interp(a, [15, 30], [30, 8]))
        return sc, f"ADX {a:.0f} · −DI 우위(하락 추세)", False
    if a >= 25:
        sc = 100.0 if a <= 45 else _clamp(_interp(a, [45, 60], [100, 78]))
    elif a >= 20:
        sc = _interp(a, [20, 25], [70, 95])
    else:
        sc = _interp(a, [10, 20], [25, 65])
    ok = a >= 20 and bull
    return _clamp(sc), f"ADX {a:.0f} · +DI {p:.0f} > −DI {m:.0f}", ok


# ─────────────────────────────────────────────────────────
# 8. OBV (5%) — 매집 흐름 확인
# ─────────────────────────────────────────────────────────
def score_obv(cur) -> tuple[float, str, bool]:
    o, sl, mx = _f(cur.get("obv")), _f(cur.get("obv_slope")), _f(cur.get("obv_max60"))
    if math.isnan(sl):
        return 0.0, "OBV 데이터 부족", False
    at_high = (not math.isnan(mx)) and (not math.isnan(o)) and o >= mx * 0.999
    if sl > 0 and at_high:
        return 100.0, "OBV 상승 + 60봉 신고 (매집)", True
    if sl > 0:
        return _clamp(_interp(sl, [0, 15], [70, 92])), f"OBV 20봉 상승 ({sl:+.1f}%)", True
    if sl > -5:
        return 42.0, f"OBV 횡보 ({sl:+.1f}%)", False
    return 15.0, f"OBV 하락 ({sl:+.1f}%)", False


# ─────────────────────────────────────────────────────────
# 통합
# ─────────────────────────────────────────────────────────
CORE_CONDITION_TEXT = {
    "ma": "20MA > 60MA + 가격이 20MA 위",
    "volume": "상승 시 거래량 ≥ 평균의 1.5배",
    "structure": "저항 돌파 또는 주요 지지선 반등",
    "rsi": "30~50에서 상승 전환",
    "macd": "골든크로스 + 히스토그램 증가",
    "bb": "하단 반등 또는 수축 후 상단 돌파",
    "adx": "ADX > 20~25 + +DI > −DI",
    "obv": "OBV 상승 추세 (매집)",
}


# ─────────────────────────────────────────────────────────
# 초보자용 해설 — 지표가 무엇이고 왜 보는지
# ─────────────────────────────────────────────────────────
BEGINNER_HELP = {
    "ma": "최근 며칠간 주가의 평균을 이은 선입니다. 20일 평균선이 60일 평균선보다 위에 있고 "
          "현재가가 그 위에 있으면 '오르는 흐름'으로 봅니다. 다만 평균선에서 너무 멀리 "
          "떨어져 있으면 단기 과열이라 감점합니다.",
    "volume": "얼마나 많이 거래됐는지입니다. 가격이 오를 때 거래량이 평소보다 크게 늘면 "
              "'실제로 사려는 사람이 많다'는 신호로 봅니다. 반대로 거래량이 폭증하면서 "
              "가격이 떨어지면 파는 물량이 쏟아진 것이라 감점합니다.",
    "structure": "최근 60일 중 가장 높았던 가격이 '저항', 가장 낮았던 가격이 '지지'입니다. "
                 "저항을 뚫고 올라가면 위쪽에 팔려는 물량이 사라졌다는 뜻이고, "
                 "지지선에서 다시 튀어오르면 아래쪽에 사려는 사람이 있다는 뜻입니다.",
    "rsi": "0~100 사이 값으로 '얼마나 과열됐는지'를 봅니다. 70을 넘으면 너무 올라 숨 고르기가 "
           "올 수 있고, 30 아래면 너무 떨어진 상태입니다. 30~50 구간에서 방향을 위로 "
           "틀 때가 가장 좋은 점수를 받습니다.",
    "macd": "빠른 평균선과 느린 평균선의 차이입니다. 빠른 선이 느린 선을 아래에서 위로 "
            "뚫는 것을 '골든크로스'라 부르고, 상승 전환 신호로 봅니다.",
    "bb": "평균선 위아래로 그린 통로입니다. 통로가 좁아졌다가(에너지 응축) 위로 뚫고 나가면 "
          "큰 움직임의 시작일 수 있습니다. 아래쪽 벽에 닿았다가 튀어오르는 것도 신호입니다.",
    "adx": "'추세가 얼마나 힘 있는가'만 봅니다. 방향은 +DI와 −DI로 판단합니다. "
           "ADX가 20~25를 넘고 +DI가 −DI보다 크면 상승 추세에 힘이 실린 상태입니다.",
    "obv": "가격이 오른 날의 거래량은 더하고 내린 날은 빼서 누적한 값입니다. "
           "가격은 아직 조용한데 이 값이 계속 오르면 '누군가 조용히 모으고 있다'고 봅니다.",
}

# 카드에 한 줄로 붙일 태그 (조건 충족 시)
HEADLINE_TAG = {
    "ma": "상승 추세", "volume": "거래량 급증", "structure": "저항 돌파·지지 반등",
    "rsi": "RSI 반등 구간", "macd": "MACD 골든크로스", "bb": "볼린저 돌파",
    "adx": "추세 강함", "obv": "매집 흐름",
}


def fmt_price(v: float) -> str:
    """국내주식(정수)과 코인(소수)을 한 함수로 처리."""
    v = _f(v)
    if math.isnan(v):
        return "-"
    if abs(v) >= 1000:
        return f"{v:,.0f}"
    if abs(v) >= 1:
        return f"{v:,.2f}"
    return f"{v:,.4f}"


def fmt_qty(v: float) -> str:
    v = _f(v)
    if math.isnan(v):
        return "-"
    if abs(v) >= 1e8:
        return f"{v/1e8:,.1f}억"
    if abs(v) >= 1e4:
        return f"{v/1e4:,.1f}만"
    return f"{v:,.0f}"


def _row(k: str, v: str, note: str = "") -> dict:
    return {"k": k, "v": v, "note": note}


def indicator_metrics(edf) -> dict[str, list[dict]]:
    """지표별 '실제 수치' 목록. 점수의 근거를 숫자로 그대로 보여주기 위한 것."""
    cur = edf.iloc[-1]
    prev = edf.iloc[-2] if len(edf) >= 2 else cur
    prev5 = edf.iloc[-6] if len(edf) >= 6 else cur

    close = _f(cur["close"])
    ma5, ma20, ma60, ma120 = (_f(cur.get(f"ma{p}")) for p in (5, 20, 60, 120))
    ma20_prev = _f(prev5.get("ma20"))

    dev20 = (close / ma20 - 1) * 100 if ma20 else float("nan")
    slope20 = (ma20 / ma20_prev - 1) * 100 if ma20_prev else float("nan")
    if not any(math.isnan(x) for x in (ma5, ma20, ma60)):
        order = "정배열 (5 > 20 > 60)" if ma5 > ma20 > ma60 else \
                "역배열 (5 < 20 < 60)" if ma5 < ma20 < ma60 else "혼재"
    else:
        order = "-"

    vol, vol_ma = _f(cur.get("volume")), _f(cur.get("vol_ma"))
    vr = _f(cur.get("vol_ratio"))

    hi, lo = _f(cur.get("high_n")), _f(cur.get("low_n"))
    prior_high = _f(cur.get("prior_high"))
    pos = (close - lo) / (hi - lo) * 100 if hi > lo else float("nan")
    to_res = (prior_high / close - 1) * 100 if close and prior_high else float("nan")

    r, rp = _f(cur.get("rsi")), _f(prev.get("rsi"))
    m, s, h = _f(cur.get("macd")), _f(cur.get("macd_signal")), _f(cur.get("macd_hist"))
    hp = _f(prev.get("macd_hist"))
    bu, bm, bl = _f(cur.get("bb_upper")), _f(cur.get("bb_mid")), _f(cur.get("bb_lower"))
    pb, bw, bwp = _f(cur.get("bb_pct_b")), _f(cur.get("bb_width")), _f(cur.get("bb_width_pct"))
    a, pdi, mdi = _f(cur.get("adx")), _f(cur.get("plus_di")), _f(cur.get("minus_di"))
    osl, o, omx = _f(cur.get("obv_slope")), _f(cur.get("obv")), _f(cur.get("obv_max60"))

    def pct(v, digits=2):
        return "-" if math.isnan(v) else f"{v:+.{digits}f}%"

    return {
        "ma": [
            _row("현재가", fmt_price(close)),
            _row("5일 평균선", fmt_price(ma5)),
            _row("20일 평균선", fmt_price(ma20), "단기 흐름의 기준선"),
            _row("60일 평균선", fmt_price(ma60), "중기 흐름의 기준선"),
            _row("120일 평균선", fmt_price(ma120)),
            _row("20일선 대비 이격", pct(dev20), "+15% 넘으면 과열로 감점"),
            _row("20일선 5일 기울기", pct(slope20), "양수면 평균선이 올라가는 중"),
            _row("배열 상태", order),
        ],
        "volume": [
            _row("당일 거래량", fmt_qty(vol) + "주"),
            _row("20일 평균 거래량", fmt_qty(vol_ma) + "주"),
            _row("평균 대비", "-" if math.isnan(vr) else f"{vr:.2f}배", "1.5배 이상이 기준"),
            _row("당일 등락률", pct(_f(cur.get("ret_pct"), 0.0))),
        ],
        "structure": [
            _row("60일 최고가 (저항)", fmt_price(hi), "이 가격을 뚫으면 돌파"),
            _row("60일 최저가 (지지)", fmt_price(lo), "이 근처는 매수세가 나오던 구간"),
            _row("박스 내 위치", "-" if math.isnan(pos) else f"{pos:.0f}%",
                 "0%=바닥, 100%=천장"),
            _row("저항까지 거리", pct(to_res), "음수면 이미 돌파한 상태"),
        ],
        "rsi": [
            _row("RSI (14일)", "-" if math.isnan(r) else f"{r:.1f}", "70↑ 과열 / 30↓ 침체"),
            _row("전일 RSI", "-" if math.isnan(rp) else f"{rp:.1f}"),
            _row("전일 대비", "-" if math.isnan(r - rp) else f"{r - rp:+.1f}",
                 "양수면 위로 방향 전환"),
        ],
        "macd": [
            _row("MACD", fmt_price(m), "0보다 크면 중기 상승권"),
            _row("시그널", fmt_price(s), "MACD가 이 선을 위로 뚫으면 골든크로스"),
            _row("히스토그램", fmt_price(h), "MACD − 시그널"),
            _row("전일 히스토그램", fmt_price(hp), "늘어나는 중이면 힘이 붙는 것"),
        ],
        "bb": [
            _row("상단선", fmt_price(bu)),
            _row("중심선 (20일 평균)", fmt_price(bm)),
            _row("하단선", fmt_price(bl)),
            _row("%B (통로 내 위치)", "-" if math.isnan(pb) else f"{pb:.2f}",
                 "0=하단, 1=상단"),
            _row("밴드폭", "-" if math.isnan(bw) else f"{bw*100:.1f}%"),
            _row("밴드폭 백분위", "-" if math.isnan(bwp) else f"{bwp*100:.0f}%",
                 "20% 이하면 응축 상태"),
        ],
        "adx": [
            _row("ADX (14일)", "-" if math.isnan(a) else f"{a:.1f}", "20~25 넘으면 추세 형성"),
            _row("+DI (상승 힘)", "-" if math.isnan(pdi) else f"{pdi:.1f}"),
            _row("−DI (하락 힘)", "-" if math.isnan(mdi) else f"{mdi:.1f}",
                 "+DI가 더 크면 상승 우위"),
        ],
        "obv": [
            _row("OBV 20봉 변화", pct(osl, 1), "양수면 매집, 음수면 분산"),
            _row("60봉 신고 여부",
                 "신고 갱신" if (not math.isnan(o) and not math.isnan(omx) and o >= omx * 0.999)
                 else "미갱신"),
        ],
    }


def headline(parts: dict) -> str:
    """카드에 한 줄로 붙일 요약 — 충족한 조건 중 비중 큰 순으로 최대 3개."""
    ok = [(p["weight"], HEADLINE_TAG.get(k, p["label"]))
          for k, p in parts.items() if p["ok"]]
    if not ok:
        return "충족 조건 없음 — 참고용"
    ok.sort(key=lambda x: -x[0])
    return " · ".join(t for _, t in ok[:3])


def score_all(edf) -> dict:
    """지표가 붙은 DataFrame(enrich 결과) → 지표별 점수 dict."""
    cur = edf.iloc[-1]
    prev = edf.iloc[-2] if len(edf) >= 2 else None
    prev5 = edf.iloc[-6] if len(edf) >= 6 else None
    recent = edf.tail(6)

    raw = {
        "ma":        score_ma(cur, prev5),
        "volume":    score_volume(cur),
        "structure": score_structure(cur),
        "rsi":       score_rsi(cur, prev),
        "macd":      score_macd(cur, prev, recent),
        "bb":        score_bb(cur),
        "adx":       score_adx(cur),
        "obv":       score_obv(cur),
    }

    metrics = indicator_metrics(edf)
    parts, total = {}, 0.0
    for key, (sc, note, ok) in raw.items():
        w = C.WEIGHTS[key]
        total += sc * w / 100.0
        parts[key] = {
            "label": C.INDICATOR_LABELS[key],
            "score": round(sc, 1),
            "weight": w,
            "contrib": round(sc * w / 100.0, 2),
            "detail": note,
            "ok": bool(ok),
            "metrics": metrics.get(key, []),   # 실제 수치
        }
        # 조건문·초보자 해설은 종목마다 같은 내용이므로 JSON 최상단에 한 번만 담습니다
        # (200종목 × 8지표만큼 반복하면 모바일에서 로딩이 느려집니다)
    return {"total": round(_clamp(total), 2), "parts": parts}


# ═══════════════════════════════════════════════════════════
# 매도·경계 신호 감지
# ═══════════════════════════════════════════════════════════
# 총점은 '매수 조건에 얼마나 부합하는가'의 가중합입니다. 비중이 큰 이동평균·거래량·
# 가격구조가 강하면, RSI·MACD·OBV가 전부 반대 신호를 보내도 총점 자체는 통과선을
# 넘을 수 있습니다 — 예를 들어 추세는 아직 살아있지만 RSI가 과열 후 꺾이기 시작한
# '천장 근처' 종목이 그렇습니다. 그래서 점수를 깎거나 종목을 빼는 대신, 8개 지표를
# 매수 관점의 정반대로 다시 훑어서 걸리는 게 있으면 경고로 별도 노출합니다.
# (매수 후보에서 자동으로 제외하지 않는 이유: 상승 초입에도 RSI 단기 과열 같은
#  경보가 섞여 나올 수 있어, 종목째로 빼면 좋은 진입 기회까지 함께 날아갑니다.
#  선택은 최종적으로 사람이 하는 게 맞다고 판단했습니다.)
SELL_SIGNAL_LABEL = {
    "ma_dead": "이동평균 역배열 전환",
    "structure_break": "지지선 이탈",
    "volume_dump": "대량 거래 하락",
    "rsi_topping": "RSI 과열 후 하락 전환",
    "macd_dead_cross": "MACD 데드크로스",
    "adx_bear": "하락 추세에 힘 실림",
    "obv_dump": "OBV 하락 (자금 이탈 의심)",
}
SELL_SIGNAL_HELP = {
    "ma_dead": "단기(20일) 평균선이 중기(60일) 평균선 아래로 내려갔고, 그 20일선마저 "
               "계속 처지는 중입니다. 매수 조건(20MA>60MA)의 정반대 상태입니다.",
    "structure_break": "최근 60일 중 가장 낮았던 가격 근처까지 다시 밀렸습니다. "
                       "그동안 받쳐주던 매수세가 힘을 잃었을 수 있습니다.",
    "volume_dump": "거래량이 평소보다 크게 늘면서 가격이 떨어졌습니다. "
                   "누군가 물량을 대량으로 정리하고 있다는 신호로 봅니다.",
    "rsi_topping": "RSI가 70을 넘어 과열 구간에 있다가 다시 꺾이기 시작했습니다. "
                   "단기 급등 뒤 숨 고르기(조정)가 나올 수 있는 자리입니다.",
    "macd_dead_cross": "빠른 평균선이 느린 평균선을 위에서 아래로 뚫었습니다. "
                       "골든크로스의 정반대로, 중기 하락 전환 신호입니다.",
    "adx_bear": "추세에 힘이 실리고 있는데 그 방향이 하락입니다 (−DI가 +DI보다 큼).",
    "obv_dump": "가격이 오른 날보다 내린 날의 거래량이 더 커서 누적값이 줄고 있습니다. "
               "조용히 물량을 던지고 있다는 뜻일 수 있습니다.",
}


def sell_signals(edf) -> list[dict]:
    """일봉 기준 매도·경계 신호 목록. 점수·등급·통과 여부에는 영향을 주지 않는다."""
    if len(edf) < 6:
        return []
    cur, prev = edf.iloc[-1], edf.iloc[-2]
    recent = edf.tail(6)
    out: list[dict] = []

    def add(key: str, detail: str) -> None:
        out.append({"key": key, "label": SELL_SIGNAL_LABEL[key],
                    "detail": detail, "help": SELL_SIGNAL_HELP[key]})

    # 1) 이동평균 역배열 전환 — score_ma의 정반대
    ma20, ma60 = _f(cur.get("ma20")), _f(cur.get("ma60"))
    ma20_5ago = _f(edf.iloc[-6].get("ma20"))
    slope20 = (ma20 / ma20_5ago - 1) * 100 if ma20_5ago else float("nan")
    if not math.isnan(ma20) and not math.isnan(ma60) and ma20 < ma60 \
            and (math.isnan(slope20) or slope20 < 0):
        add("ma_dead", f"20일선({fmt_price(ma20)}) < 60일선({fmt_price(ma60)}) · "
                       f"20일선 5일 기울기 {slope20:+.2f}%" if not math.isnan(slope20)
            else f"20일선({fmt_price(ma20)}) < 60일선({fmt_price(ma60)})")

    # 2) 지지선 붕괴 — score_structure의 정반대
    close, low_n = _f(cur["close"]), _f(cur.get("low_n"))
    if not math.isnan(low_n) and low_n > 0 and close <= low_n * 1.02:
        add("structure_break", f"60일 최저가({fmt_price(low_n)}) 근접·이탈 (현재가 {fmt_price(close)})")

    # 3) 대량 거래 하락 — score_volume의 정반대
    ret, vr = _f(cur.get("ret_pct"), 0.0), _f(cur.get("vol_ratio"))
    if ret <= -1.5 and not math.isnan(vr) and vr >= 1.5:
        add("volume_dump", f"{ret:+.1f}% 하락 + 거래량 20일 평균의 {vr:.1f}배")

    # 4) RSI 과열 후 하락 전환 — score_rsi의 정반대
    r, rp = _f(cur.get("rsi")), _f(prev.get("rsi"))
    if r > 70 and not math.isnan(rp) and r < rp:
        add("rsi_topping", f"RSI {rp:.1f} → {r:.1f}")

    # 5) MACD 데드크로스 — score_macd의 골든크로스 판정과 대칭
    dc = False
    if len(recent) >= 2:
        diff = (recent["macd"] - recent["macd_signal"]).tail(4).tolist()
        for i in range(1, len(diff)):
            if diff[i - 1] >= 0 > diff[i]:
                dc = True
    if dc:
        add("macd_dead_cross", "MACD가 시그널선을 아래로 하향 돌파 (3봉 이내)")

    # 6) 하락 추세에 힘 실림 — score_adx의 정반대
    a, pdi, mdi = _f(cur.get("adx")), _f(cur.get("plus_di")), _f(cur.get("minus_di"))
    if not math.isnan(a) and a >= 20 and not math.isnan(mdi) and not math.isnan(pdi) \
            and mdi > pdi:
        add("adx_bear", f"ADX {a:.0f} · −DI({mdi:.0f}) > +DI({pdi:.0f})")

    # 7) OBV 하락 — score_obv의 정반대
    osl = _f(cur.get("obv_slope"))
    if not math.isnan(osl) and osl < -5:
        add("obv_dump", f"OBV 20봉 변화 {osl:+.1f}%")

    return out
