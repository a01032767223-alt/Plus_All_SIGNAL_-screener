"""야후 파이낸스 일괄 다운로드 공통부.

국내주식·미국주식이 같은 경로를 쓴다. 시장별로 다른 건 심볼 표기뿐이라
정리(tidy)와 배치 루프만 여기 모아둔다.
"""
from __future__ import annotations

import pandas as pd

OHLCV_COLS = ["open", "high", "low", "close", "volume"]
CHUNK = 60          # 요청당 종목 수. 늘리면 야후가 조용히 빈 응답을 준다.


def tidy(df: pd.DataFrame) -> pd.DataFrame | None:
    """야후 응답 한 종목분을 표준 OHLCV로 정리. 쓸 수 없으면 None."""
    if df is None or df.empty:
        return None
    df = df.rename(columns={"Open": "open", "High": "high", "Low": "low",
                            "Close": "close", "Volume": "volume"})
    if not all(c in df.columns for c in OHLCV_COLS):
        return None
    out = df[OHLCV_COLS].astype("float64").dropna(subset=["close"])
    out = out[out["close"] > 0]
    out.index = pd.to_datetime(out.index).tz_localize(None)
    return out[~out.index.duplicated(keep="last")].sort_index() if len(out) else None


def batch_download(symbols: list[str], start: str, min_bars: int,
                   verbose: bool = True, tag: str = "yahoo",
                   chunk: int = CHUNK) -> dict[str, pd.DataFrame]:
    """야후 심볼 리스트 → {심볼: 일봉}. 한 배치가 실패해도 나머지는 계속한다."""
    import yfinance as yf

    frames: dict[str, pd.DataFrame] = {}
    for i in range(0, len(symbols), chunk):
        batch = symbols[i:i + chunk]
        try:
            # auto_adjust=True — 액면분할·배당을 소급 반영한 연속 시계열을 받는다.
            # False로 두면 분할일에 -90% 같은 가짜 폭락이 남아 이동평균·RSI·손익비가
            # 통째로 망가진다(엔비디아 10:1 분할이면 목표가가 현재가의 10배로 계산됨).
            # 조정은 과거 봉에만 걸리므로 최근 종가 = 실제 현재가로 그대로 유지된다.
            raw = yf.download(batch, start=start, interval="1d", group_by="ticker",
                              auto_adjust=True, actions=False, progress=False,
                              threads=True, timeout=30)
        except Exception as e:
            print(f"[{tag}] 배치 {i // chunk + 1} 실패: {type(e).__name__}: {e}")
            continue
        if raw is None or raw.empty:
            continue

        for sym in batch:
            try:
                sub = raw[sym] if isinstance(raw.columns, pd.MultiIndex) else raw
            except KeyError:
                continue
            t = tidy(sub)
            if t is not None and len(t) >= min_bars:
                frames[sym] = t

        if verbose and (i // chunk) % 5 == 0:
            print(f"  ... {min(i + chunk, len(symbols))}/{len(symbols)}종목 "
                  f"(누적 {len(frames)}종목 확보)", flush=True)

    rate = len(frames) / max(1, len(symbols)) * 100
    print(f"[{tag}] 야후 수집 완료: {len(frames):,}/{len(symbols):,}종목 ({rate:.0f}%)")
    return frames
