"""타임프레임 결합 · 등급 산출 · 리스크 레벨 계산."""
from __future__ import annotations

import math

import pandas as pd

from . import config as C
from . import indicators as I
from . import rules as R


def resample_weekly(df: pd.DataFrame) -> pd.DataFrame:
    """일봉 → 주봉 (월요일 시작)."""
    if not isinstance(df.index, pd.DatetimeIndex):
        df = df.copy()
        df.index = pd.to_datetime(df.index)
    w = df.resample("W-FRI").agg({
        "open": "first", "high": "max", "low": "min",
        "close": "last", "volume": "sum",
    }).dropna(subset=["close"])
    return w


def grade_of(score: float) -> str:
    for g, cut in C.GRADE_CUTS:
        if score >= cut:
            return g
    return "-"


def risk_levels(edf: pd.DataFrame) -> dict:
    """진입가·손절·목표·손익비. 일봉 기준."""
    cur = edf.iloc[-1]
    close = float(cur["close"])
    atr = R._f(cur.get("atr"), close * 0.03)
    swing_low = float(edf["low"].tail(20).min())
    atr_stop = close - C.STOP_ATR_MULT * atr
    stop = max(swing_low, atr_stop)          # 둘 중 더 가까운(타이트한) 쪽
    if stop >= close:
        stop = close * 0.95
    risk = close - stop

    prior_high = R._f(cur.get("prior_high"), float("nan"))
    rr2_target = close + C.MIN_RR * risk
    if not math.isnan(prior_high) and prior_high > close * 1.005:
        # 직전 고점을 1차 목표로 삼되 상한을 둔다. 고점 대비 반토막 난 종목은
        # 목표가가 현재가의 2배로 잡혀 손익비가 10을 넘고, 그 숫자만 보면
        # '아주 좋은 자리'처럼 읽힌다. 1차 목표는 도달 가능한 거리여야 한다.
        target = min(prior_high, close + C.MAX_RR_TARGET * risk)
    else:
        target = rr2_target
    rr = (target - close) / risk if risk > 0 else 0.0

    return {
        "entry": round(close, 4),
        "stop": round(stop, 4),
        "stop_pct": round((stop / close - 1) * 100, 2),
        "target": round(target, 4),
        "target_pct": round((target / close - 1) * 100, 2),
        "rr": round(rr, 2),
        "rr_ok": bool(rr >= C.MIN_RR),
        "atr_pct": round(R._f(cur.get("atr_pct"), 0.0), 2),
    }


def evaluate(frames: dict[str, pd.DataFrame], asset_class: str) -> dict | None:
    """frames: {"1d": df, "1w": df, ("4h": df)} → 종합 평가 결과.

    데이터가 부족한 타임프레임은 자동으로 빼고 남은 것들의 비중을 재정규화한다.
    """
    tf_weights = C.TF_WEIGHTS[asset_class]
    tf_results, used = {}, {}

    for tf, w in tf_weights.items():
        df = frames.get(tf)
        min_bars = C.MIN_BARS if tf != "1w" else 45
        if df is None or len(df) < min_bars:
            continue
        edf = I.enrich(df)
        res = R.score_all(edf)
        tf_results[tf] = {"label": C.TF_LABELS[tf], "total": res["total"], "parts": res["parts"]}
        used[tf] = w
        if tf == "1d":
            daily_edf = edf

    if "1d" not in tf_results:
        return None

    norm = sum(used.values())
    combined = sum(tf_results[tf]["total"] * w / norm for tf, w in used.items())

    aligned = all(tf_results[tf]["total"] >= C.MTF_ALIGN_THRESHOLD for tf in tf_results)
    if aligned and len(tf_results) > 1:
        combined = min(100.0, combined + C.MTF_ALIGN_BONUS)

    cur = daily_edf.iloc[-1]
    parts = tf_results["1d"]["parts"]
    # 카드의 미니 막대에 필요한 최소 정보만 (상세 화면은 parts를 씁니다)
    checks = [{"key": k, "ok": p["ok"], "score": p["score"]} for k, p in parts.items()]
    # 매수 점수와는 별개로 계산 — 총점·등급·통과 여부에 영향을 주지 않는 '경고'입니다.
    warnings = R.sell_signals(daily_edf)

    return {
        "score": round(combined, 2),
        "grade": grade_of(combined),
        "headline": R.headline(parts),
        "warnings": warnings,
        "warning_count": len(warnings),
        "mtf_aligned": bool(aligned and len(tf_results) > 1),
        "timeframes": {tf: {"label": v["label"], "score": v["total"]} for tf, v in tf_results.items()},
        # "내 전략" 탭이 브라우저에서 총점을 다시 계산할 때 쓰는 원재료.
        # 타임프레임별 지표 점수는 위에서 이미 구해놨고 지금까지 1d 것만 쓰고 버렸다 —
        # 여기서 꺼내 쓰는 비용은 0이다. (JSON에는 run.py가 매트릭스 파일로 따로 담는다)
        "tf_scores": {tf: {k: p["score"] for k, p in v["parts"].items()}
                      for tf, v in tf_results.items()},
        "parts": parts,
        "checks": checks,
        "checks_passed": sum(1 for c in checks if c["ok"]),
        "risk": risk_levels(daily_edf),
        "price": round(float(cur["close"]), 4),
        "change_pct": round(R._f(cur.get("ret_pct"), 0.0), 2),
        "volume_ratio": round(R._f(cur.get("vol_ratio"), 0.0), 2),
        "rsi": round(R._f(cur.get("rsi"), 0.0), 1),
        "adx": round(R._f(cur.get("adx"), 0.0), 1),
        "last_bar": str(daily_edf.index[-1])[:10],
    }
