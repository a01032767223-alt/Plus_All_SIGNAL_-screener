"""미국주식 수집 모듈 검증 — 네트워크 없이 가짜 응답을 주입해서 확인.

야후 심볼 변환(BRK.B → BRK-B), 목록 소스 3단 폴백, 거래소 매핑 파싱,
주가·거래대금 필터가 실제로 무엇을 걸러내는지를 본다.

실행: python tests/test_us_source.py
"""
from __future__ import annotations

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from screener import config as C
from screener.sources import _yahoo
from screener.sources import us_stock as U


def _fake_bars(n=300, seed=0, start=200.0, vol=2_000_000.0):
    rng = np.random.default_rng(seed)
    close = start * np.exp(np.cumsum(rng.normal(0.0004, 0.015, n)))
    idx = pd.bdate_range(end="2026-08-14", periods=n)
    return pd.DataFrame({
        "Open": close, "High": close * 1.01, "Low": close * 0.99,
        "Close": close, "Volume": np.full(n, vol),
    }, index=idx)


def _uni(symbols):
    return pd.DataFrame({"name": [f"{s} Inc." for s in symbols], "sector": ""},
                        index=pd.Index(symbols, name="symbol"))


# 테스트마다 모듈 함수를 갈아끼우므로, 다음 테스트로 오염이 새지 않게 되돌린다.
_PATCHED = ("_sp500_fdr", "_sp500_csv", "_ndx_wiki", "fetch_universe",
            "fetch_ohlcv", "fetch_exchange_map", "requests")
_ORIGINALS = {n: getattr(U, n) for n in _PATCHED}

try:
    import pytest

    @pytest.fixture(autouse=True)
    def _restore_module():
        for n, v in _ORIGINALS.items():
            setattr(U, n, v)
        yield
        for n, v in _ORIGINALS.items():
            setattr(U, n, v)
except ImportError:            # pytest 없이 직접 실행할 때는 아래 __main__ 루프가 처리
    pass


# ── 심볼 변환 ────────────────────────────────────────────
def test_yahoo_symbol_class_shares():
    """야후는 클래스주에 점이 아니라 하이픈을 쓴다. 빠지면 버크셔가 통째로 사라진다."""
    assert U.to_yahoo("AAPL") == "AAPL"
    assert U.to_yahoo("BRK.B") == "BRK-B"
    assert U.to_yahoo("BF.B") == "BF-B"
    assert U.to_yahoo("brk.b") == "BRK-B"


# ── 티커 형식 필터 ────────────────────────────────────────
def test_symbol_filter_keeps_class_shares_drops_junk():
    uni = _uni(["AAPL", "BRK.B", "GOOGL", "ABCD.W", "^GSPC", "TOOLONGSYM"])
    out = U.apply_symbol_filters(uni)
    assert set(out.index) == {"AAPL", "BRK.B", "GOOGL"}, set(out.index)


# ── 목록 소스 폴백 ───────────────────────────────────────
def test_universe_falls_back_to_fdr_then_snapshot():
    """CSV가 죽으면 FDR, FDR도 죽으면 내장 스냅샷까지 내려가야 한다.

    순서가 중요하다 — CSV는 클래스주의 점(BRK.B)을 보존하지만 FDR은 지워버린다.
    """
    calls = []

    U._sp500_csv = lambda: calls.append("csv") or (_ for _ in ()).throw(RuntimeError("csv down"))
    U._sp500_fdr = lambda: calls.append("fdr") or (_ for _ in ()).throw(RuntimeError("fdr down"))
    U._ndx_wiki = lambda: calls.append("wiki") or (_ for _ in ()).throw(RuntimeError("wiki down"))

    uni = U.fetch_universe()
    assert calls == ["csv", "fdr", "wiki"], calls
    assert len(uni) > 100, "내장 스냅샷만으로도 100종목 이상은 나와야 한다"
    assert "AAPL" in uni.index and "NVDA" in uni.index


def test_universe_merges_nasdaq100_extras_without_duplicates():
    U._sp500_csv = lambda: _uni(["AAPL", "MSFT", "JPM"])
    U._ndx_wiki = lambda: ["AAPL", "MSFT", "MELI", "ARM"]   # 앞 둘은 이미 S&P500

    uni = U.fetch_universe()
    assert list(uni.index) == ["AAPL", "MSFT", "JPM", "MELI", "ARM"]
    assert not uni.index.duplicated().any()
    assert uni.loc["AAPL", "name"] == "AAPL Inc.", "S&P500 쪽 종목명이 보존돼야 한다"


def test_universe_survives_nasdaq100_failure_only():
    """나스닥100만 실패해도 S&P500은 살아 있어야 한다."""
    U._sp500_csv = lambda: _uni(["AAPL", "MSFT"])
    U._ndx_wiki = lambda: (_ for _ in ()).throw(RuntimeError("wiki down"))

    uni = U.fetch_universe()
    assert "AAPL" in uni.index
    assert "NVDA" in uni.index, "나스닥100 내장 스냅샷으로 보충돼야 한다"


# ── 거래소 매핑 · 링크 ────────────────────────────────────
def test_exchange_map_parses_pipe_files():
    # 실제 파일의 푸터는 파이프가 붙어 있다. 파이프 없는 가짜 푸터를 쓰면
    # 길이 가드에 먼저 걸려서 startswith 가드가 있는지 없는지 검증하지 못한다.
    nasdaq_txt = ("Symbol|Security Name|Market Category\r\n"
                  "AAPL|Apple Inc.|Q\r\nMSFT|Microsoft|Q\r\n"
                  "File Creation Time: 0814202602:00|||||\r\n")
    other_txt = ("ACT Symbol|Security Name|Exchange|CQS Symbol\r\n"
                 "JPM|JPMorgan|N|JPM\r\nAAA|NYSE American Co|A|AAA\r\n"
                 "SPY|SPDR S&P 500|P|SPY\r\nCBOE|Cboe Global|Z|CBOE\r\n"
                 "IBKR|Interactive Brokers|V|IBKR\r\n"
                 "File Creation Time: 0814202602:00|||||\r\n")

    class R:
        def __init__(self, text):
            self.text, self.status_code = text, 200

        def raise_for_status(self):
            pass

    def fake_get(url, **kw):
        return R(nasdaq_txt if "nasdaqlisted" in url else other_txt)

    orig = U.requests
    U.requests = types.SimpleNamespace(get=fake_get)
    try:
        m = U.fetch_exchange_map()
    finally:
        U.requests = orig

    assert m["AAPL"] == "NASDAQ" and m["MSFT"] == "NASDAQ"
    assert m["JPM"] == "NYSE" and m["AAA"] == "AMEX"
    # P=NYSE Arca, Z=Cboe BZX, V=IEX — 예전엔 각각 AMEX/NASDAQ/NYSE로 잘못 찍혀
    # 네이버 링크가 깨졌다(예: CBOE → CBOE.O)
    assert m["SPY"] == "ARCA" and m["CBOE"] == "CBOE" and m["IBKR"] == "IEX"
    assert not any(k.startswith("File Creation") for k in m), "푸터 줄이 티커로 섞이면 안 된다"


def test_exchange_map_survives_total_failure():
    orig = U.requests

    def boom(*a, **k):
        raise ConnectionError("차단됨")
    U.requests = types.SimpleNamespace(get=boom)
    try:
        assert U.fetch_exchange_map() == {}, "실패해도 예외 없이 빈 dict여야 한다"
    finally:
        U.requests = orig


def test_link_falls_back_to_yahoo_without_exchange():
    assert U.link_for("AAPL", "NASDAQ").endswith("AAPL.O/total")
    assert U.link_for("JPM", "NYSE").endswith("JPM.N/total")
    assert U.link_for("AAPL", None) == "https://finance.yahoo.com/quote/AAPL"
    assert U.link_for("AAPL", "US") == "https://finance.yahoo.com/quote/AAPL"


def test_link_uses_yahoo_for_class_shares():
    """네이버 해외주식은 BRK.B 같은 클래스주 표기가 달라 링크가 깨진다."""
    assert U.link_for("BRK.B", "NYSE") == "https://finance.yahoo.com/quote/BRK-B"


# ── 통합 흐름 ────────────────────────────────────────────
def test_load_applies_price_and_turnover_filters():
    uni = _uni(["AAPL", "PENNY", "THIN"])
    frames = {
        "AAPL":  _yahoo.tidy(_fake_bars(seed=1, start=200.0, vol=2_000_000)),   # 통과
        "PENNY": _yahoo.tidy(_fake_bars(seed=2, start=2.0, vol=50_000_000)),    # 주가 미달
        "THIN":  _yahoo.tidy(_fake_bars(seed=3, start=200.0, vol=10)),          # 거래대금 미달
    }
    U.fetch_universe = lambda: uni
    U.fetch_ohlcv = lambda u, days=None, verbose=True: frames
    U.fetch_exchange_map = lambda: {"AAPL": "NASDAQ"}

    out, meta = U.load()
    assert set(out) == {"AAPL"}, set(out)
    assert meta.loc["AAPL", "market"] == "NASDAQ"
    assert meta.loc["AAPL", "turnover"] > C.US_MIN_TURNOVER
    assert set(out) == set(meta.index)


def test_load_raises_when_no_bars():
    U.fetch_universe = lambda: _uni(["AAPL"])
    U.fetch_ohlcv = lambda *a, **k: {}
    try:
        U.load()
    except RuntimeError as e:
        assert "한 건도" in str(e)
    else:
        raise AssertionError("일봉을 못 받으면 RuntimeError를 던져야 한다")


def test_load_exchange_lookup_handles_both_symbol_forms():
    """나스닥트레이더는 점 표기(BRK.B)를 쓴다. 야후 표기로도 찾을 수 있어야 한다."""
    for key in ("BRK.B", "BRK-B"):
        U.fetch_universe = lambda: _uni(["BRK.B"])
        U.fetch_ohlcv = lambda u, days=None, verbose=True: {
            "BRK.B": _yahoo.tidy(_fake_bars(seed=4, start=400.0, vol=1_000_000))}
        U.fetch_exchange_map = lambda k=key: {k: "NYSE"}

        _, meta = U.load()
        assert meta.loc["BRK.B", "market"] == "NYSE", key


def test_normalize_symbols_repairs_dot_stripped_class_shares():
    """FinanceDataReader는 위키백과를 읽으며 심볼의 점을 지운다.

    BRKB/BFB는 야후에도 네이버에도 없는 심볼이라, 고치지 않으면 버크셔·
    브라운포맨이 아무 오류 없이 유니버스에서 사라진다. 이 앱에서 가장
    조용한 종류의 버그라 명시적으로 고정한다.
    """
    out = list(U.normalize_symbols(pd.Index(["BRKB", "BFB", "BFA", "AAPL", "BRK.B"])))
    assert out == ["BRK.B", "BF.B", "BF.A", "AAPL", "BRK.B"], out


def test_universe_repairs_fdr_dot_stripped_symbols_end_to_end():
    U._sp500_csv = lambda: (_ for _ in ()).throw(RuntimeError("csv down"))
    U._sp500_fdr = lambda: _uni(["AAPL", "BRKB", "BFB"])      # FDR이 실제로 주는 형태
    U._ndx_wiki = lambda: []

    uni = U.apply_symbol_filters(U.fetch_universe())
    assert "BRK.B" in uni.index and "BRKB" not in uni.index
    assert U.to_yahoo("BRK.B") == "BRK-B"


# ── 야후 배치 (공통부) ────────────────────────────────────
def test_batch_download_multiindex_and_min_bars():
    parts = {"AAPL": _fake_bars(seed=1), "MSFT": _fake_bars(seed=2),
             "TINY": _fake_bars(n=40, seed=3)}
    raw = pd.concat(parts, axis=1)

    fake_yf = types.ModuleType("yfinance")
    captured = {}

    def fake_download(*a, **k):
        captured.update(k)
        return raw
    fake_yf.download = fake_download
    saved = sys.modules.get("yfinance")
    sys.modules["yfinance"] = fake_yf
    try:
        frames = _yahoo.batch_download(["AAPL", "MSFT", "TINY"], "2025-01-01",
                                       min_bars=C.MIN_BARS, verbose=False)
    finally:                       # 스텁을 남기면 뒤에 도는 테스트가 오염된다
        if saved is None:
            sys.modules.pop("yfinance", None)
        else:
            sys.modules["yfinance"] = saved

    assert set(frames) == {"AAPL", "MSFT"}
    assert list(frames["AAPL"].columns) == _yahoo.OHLCV_COLS
    assert frames["AAPL"].index.tz is None
    # 액면분할 소급 반영이 꺼지면 분할일에 가짜 폭락이 남는다
    assert captured.get("auto_adjust") is True, "auto_adjust가 꺼져 있다"


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            for n, v in _ORIGINALS.items():
                setattr(U, n, v)                            # 테스트 간 오염 방지
            try:
                fn()
                print(f"  PASS  {name}")
            except AssertionError as e:
                fails += 1
                print(f"  FAIL  {name}: {e}")
    print("\n실패" if fails else "\n전체 통과", f"({fails} failed)")
    raise SystemExit(1 if fails else 0)
