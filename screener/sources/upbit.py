"""업비트 KRW 마켓 데이터 — 공개 REST API (인증 불필요).

Rate limit(초당 10회)을 지키기 위해 호출 간 최소 간격을 두고, 429는 지수 백오프.
해외 IP 차단 등으로 실패하면 RuntimeError를 던져 run.py가 대체 소스를 쓰도록 한다.
"""
from __future__ import annotations

import time

import pandas as pd
import requests

from .. import config as C

BASE = "https://api.upbit.com/v1"
HEADERS = {"Accept": "application/json", "User-Agent": "signal-screener/1.0"}
_MIN_INTERVAL = 0.12
_last_call = [0.0]


def _get(path: str, params: dict | None = None, retries: int = 4):
    for attempt in range(retries):
        wait = _MIN_INTERVAL - (time.time() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.time()
        try:
            r = requests.get(f"{BASE}{path}", params=params, headers=HEADERS, timeout=15)
        except requests.RequestException as e:
            if attempt == retries - 1:
                raise RuntimeError(f"업비트 접속 실패: {e}") from e
            time.sleep(1.5 * (attempt + 1))
            continue
        if r.status_code == 429:
            time.sleep(1.0 * (attempt + 1))
            continue
        if r.status_code in (403, 418):
            raise RuntimeError(f"업비트가 요청을 거부했습니다(HTTP {r.status_code}). "
                               f"실행 서버 IP 차단 가능성 — 대체 소스 사용 필요")
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"업비트 요청 실패(재시도 초과): {path}")


def list_markets() -> pd.DataFrame:
    """KRW 마켓 목록 + 유의종목 플래그."""
    data = _get("/market/all", {"isDetails": "true"})
    rows = []
    for m in data:
        code = m.get("market", "")
        if not code.startswith("KRW-"):
            continue
        event = m.get("market_event") or {}
        caution = event.get("caution") or {}
        # caution은 항목별 bool dict — 딕셔너리 존재 여부가 아니라 값이 하나라도 True인지 확인해야 한다
        # (빈 dict나 전부 False인 dict를 True로 보면 정상 종목까지 걸러진다)
        caution_on = any(bool(v) for v in caution.values()) if isinstance(caution, dict) else bool(caution)
        warning = bool(event.get("warning")) or m.get("market_warning") == "CAUTION"
        rows.append({
            "market": code,
            "name": m.get("korean_name", code),
            "warning": warning or caution_on,
        })
    return pd.DataFrame(rows).set_index("market")


def tickers(markets: list[str]) -> pd.DataFrame:
    """24시간 거래대금 등 현재 시세 (100개씩 배치)."""
    rows = []
    for i in range(0, len(markets), 100):
        chunk = markets[i:i + 100]
        rows += _get("/ticker", {"markets": ",".join(chunk)})
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    return df.set_index("market")[["trade_price", "acc_trade_price_24h",
                                   "signed_change_rate"]]


_CANDLE_PATH = {"4h": "/candles/minutes/240", "1d": "/candles/days", "1w": "/candles/weeks"}


def candles(market: str, tf: str, count: int = 200) -> pd.DataFrame:
    """OHLCV DataFrame(index=datetime, 오름차순)."""
    data = _get(_CANDLE_PATH[tf], {"market": market, "count": min(count, 200)})
    if not data:
        return pd.DataFrame()
    df = pd.DataFrame(data)
    df = df.rename(columns={
        "candle_date_time_kst": "date",
        "opening_price": "open", "high_price": "high",
        "low_price": "low", "trade_price": "close",
        "candle_acc_trade_volume": "volume",
    })
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    return df[["open", "high", "low", "close", "volume"]].astype("float64")


def universe() -> pd.DataFrame:
    """기본 필터를 통과한 KRW 마켓 종목."""
    mk = list_markets()
    tk = tickers(list(mk.index))
    df = mk.join(tk, how="inner")
    before = len(df)
    df = df[~df["warning"]]
    df = df[df["acc_trade_price_24h"] >= C.COIN_MIN_TURNOVER_24H]
    df = df.sort_values("acc_trade_price_24h", ascending=False)
    print(f"[coin] 유니버스 필터: {before} → {len(df)}종목")
    return df
