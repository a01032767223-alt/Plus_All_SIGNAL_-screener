"""국내주식 수집 모듈 검증 — 네트워크 없이 가짜 응답을 주입해서 확인.

야후 응답의 MultiIndex 구조, 네이버 리터럴 파싱, 대체 경로 전환,
필터가 실제로 무엇을 걸러내는지를 본다.

실행: python tests/test_kr_source.py
"""
from __future__ import annotations

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from screener.sources import kr_stock as K


def _fake_bars(n=300, seed=0, start=10000.0):
    rng = np.random.default_rng(seed)
    close = start * np.exp(np.cumsum(rng.normal(0.0004, 0.018, n)))
    idx = pd.bdate_range(end="2026-08-14", periods=n)
    return pd.DataFrame({
        "Open": close, "High": close * 1.01, "Low": close * 0.99,
        "Close": close, "Volume": rng.lognormal(11, 0.3, n),
    }, index=idx)


def _fake_universe(codes_markets):
    return pd.DataFrame(
        {"name": [f"종목{c}" for c, _ in codes_markets],
         "market": [m for _, m in codes_markets],
         "marketcap": [1e12] * len(codes_markets)},
        index=pd.Index([c for c, _ in codes_markets], name="code"))


# ── 야후 심볼 매핑 ─────────────────────────────────────────
def test_yahoo_symbol_suffix():
    assert K._yahoo_symbol("005930", "KOSPI") == "005930.KS"
    assert K._yahoo_symbol("247540", "KOSDAQ") == "247540.KQ"
    assert K._yahoo_symbol("123456", "알수없음") == "123456.KS"   # 기본값 KOSPI


# ── 야후 MultiIndex 응답 파싱 ──────────────────────────────
def test_fetch_ohlcv_yahoo_multiindex():
    uni = _fake_universe([("005930", "KOSPI"), ("247540", "KOSDAQ"), ("000001", "KOSPI")])
    parts = {"005930.KS": _fake_bars(seed=1), "247540.KQ": _fake_bars(seed=2),
             "000001.KS": _fake_bars(n=40, seed=3)}          # 데이터 짧은 종목
    raw = pd.concat(parts, axis=1)                            # (티커, 필드) MultiIndex

    fake_yf = types.ModuleType("yfinance")
    fake_yf.download = lambda *a, **k: raw
    sys.modules["yfinance"] = fake_yf

    frames = K.fetch_ohlcv_yahoo(uni, days=300, verbose=False)

    assert set(frames) == {"005930", "247540"}, "MIN_BARS 미달 종목은 빠져야 한다"
    df = frames["005930"]
    assert list(df.columns) == K.OHLCV_COLS
    assert df.index.is_monotonic_increasing and df.index.tz is None
    assert (df["close"] > 0).all()


def test_fetch_ohlcv_yahoo_survives_batch_failure():
    """한 배치가 터져도 나머지는 수집돼야 한다."""
    uni = _fake_universe([(f"{i:06d}", "KOSPI") for i in range(K.CHUNK + 3)])
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("첫 배치 네트워크 오류")
        syms = a[0] if a else k["tickers"]
        return pd.concat({s: _fake_bars(seed=hash(s) % 100) for s in syms}, axis=1)

    fake_yf = types.ModuleType("yfinance")
    fake_yf.download = flaky
    sys.modules["yfinance"] = fake_yf

    frames = K.fetch_ohlcv_yahoo(uni, days=300, verbose=False)
    assert calls["n"] == 2
    assert 0 < len(frames) < len(uni), f"두 번째 배치만 수집돼야 함: {len(frames)}"


# ── 네이버 리터럴 응답 파싱 ────────────────────────────────
def test_naver_literal_parsing():
    body = ("[['날짜', '시가', '고가', '저가', '종가', '거래량', '외국인소진율'],\n"
            + ",\n".join(
                f'["2026{m:02d}{d:02d}", 100, 110, 95, {100 + i}, {1000 + i}, 50.0]'
                for i, (m, d) in enumerate([(3, x) for x in range(1, 29)] +
                                           [(4, x) for x in range(1, 29)] +
                                           [(5, x) for x in range(1, 29)] +
                                           [(6, x) for x in range(1, 29)] +
                                           [(7, x) for x in range(1, 29)]))
            + "]")

    class R:
        text = body
    K.requests = types.SimpleNamespace(get=lambda *a, **k: R())
    try:
        df = K._naver_ohlcv("005930", 300)
    finally:
        import requests as real
        K.requests = real

    assert df is not None and len(df) == 140
    assert list(df.columns) == K.OHLCV_COLS
    assert df.index.is_monotonic_increasing
    assert df["close"].iloc[0] == 100 and df["close"].iloc[-1] == 239


def test_naver_rejects_non_literal_body():
    class R:
        text = "<html>로그인이 필요합니다</html>"
    K.requests = types.SimpleNamespace(get=lambda *a, **k: R())
    try:
        assert K._naver_ohlcv("005930", 300) is None
    finally:
        import requests as real
        K.requests = real


# ── 필터 ─────────────────────────────────────────────────
def test_name_and_marketcap_filters():
    uni = pd.DataFrame(
        {"name": ["정상종목", "삼성전자우", "한화3우B", "미래에셋스팩1호", "소형주", "대우건설"],
         "market": ["KOSPI"] * 6,
         "marketcap": [1e12, 1e12, 1e12, 1e12, 1e9, 1e12]},
        index=["000001", "000002", "000003", "000004", "000005", "000006"])

    out = K.apply_name_filters(uni)
    assert set(out.index) == {"000001", "000006"}, set(out.index)
    assert "대우건설" in out["name"].tolist(), "이름 중간의 '우'는 걸러지면 안 된다"


# ── 통합 흐름 ────────────────────────────────────────────
def test_load_falls_back_to_naver_when_yahoo_thin():
    """야후 수집률이 절반 미만이면 네이버로 보충해야 한다."""
    uni = _fake_universe([(f"{i:06d}", "KOSPI") for i in range(10)])
    K.fetch_universe = lambda: uni
    K.fetch_ohlcv_yahoo = lambda u, d=None, verbose=True: {"000000": K._tidy(_fake_bars(seed=9))}
    called = {"naver": None}

    def fake_naver(codes, days=None, verbose=True):
        called["naver"] = list(codes)
        return {c: K._tidy(_fake_bars(seed=i)) for i, c in enumerate(codes[:6])}
    K.fetch_ohlcv_naver = fake_naver

    frames, meta = K.load()
    assert called["naver"] is not None, "네이버 대체 경로가 호출돼야 한다"
    assert "000000" not in called["naver"], "야후로 이미 받은 종목은 다시 안 받아야 한다"
    assert len(frames) == 7
    assert set(frames) == set(meta.index)
    assert "turnover" in meta.columns and (meta["turnover"] > 0).all()
    assert meta["source"].iloc[0] in ("naver", "yahoo+naver")


def test_load_raises_when_all_sources_dead():
    K.fetch_universe = lambda: _fake_universe([("000001", "KOSPI")])
    K.fetch_ohlcv_yahoo = lambda *a, **k: {}
    K.fetch_ohlcv_naver = lambda *a, **k: {}
    try:
        K.load()
    except RuntimeError as e:
        assert "한 건도" in str(e)
    else:
        raise AssertionError("모든 소스가 죽으면 RuntimeError를 던져야 한다")


def test_load_turnover_filter():
    uni = _fake_universe([("000001", "KOSPI"), ("000002", "KOSPI")])
    rich = _fake_bars(seed=4, start=50000)
    poor = _fake_bars(seed=5, start=50000)
    poor["Volume"] = 1.0                                   # 거래대금 = 종가 × 1 → 하한 미달
    K.fetch_universe = lambda: uni
    K.fetch_ohlcv_yahoo = lambda u, d=None, verbose=True: {
        "000001": K._tidy(rich), "000002": K._tidy(poor)}
    K.fetch_ohlcv_naver = lambda *a, **k: {}

    frames, meta = K.load()
    assert set(frames) == {"000001"}, "거래대금 미달 종목이 남았다"


if __name__ == "__main__":
    originals = {n: getattr(K, n) for n in
                 ("fetch_universe", "fetch_ohlcv_yahoo", "fetch_ohlcv_naver", "requests")}
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            for n, v in originals.items():
                setattr(K, n, v)                            # 테스트 간 오염 방지
            try:
                fn()
                print(f"  PASS  {name}")
            except AssertionError as e:
                fails += 1
                print(f"  FAIL  {name}: {e}")
    print("\n실패" if fails else "\n전체 통과", f"({fails} failed)")
    raise SystemExit(1 if fails else 0)
