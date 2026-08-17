"""파이프라인(run.main) 동작 검증 — 파일 쓰기·알림 결정 로직.

여기서 잡는 건 '조용히 잘못되는' 것들이다.
  - 휴장일에 어제와 같은 데이터로 히스토리를 덮어쓰고 같은 알림을 또 보내는 것
  - 데모 실행이 실제 히스토리 스냅샷을 합성 데이터로 파괴하는 것
  - 시장별 결과 파일이 서로 겹치는 것

실행: python tests/test_pipeline.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from screener import run as RUN
from screener import notify as N

# 이 파일은 RUN.screen_us/kr/coin/demo와 N.dispatch를 가짜로 갈아끼운다.
# pytest는 모든 테스트 파일을 한 프로세스에서 돌리므로, 복원하지 않으면 다른
# 테스트 파일이 이 가짜 함수를 이어받아 엉뚱하게 통과·실패한다.
_ORIGINALS = {"screen_us": RUN.screen_us, "screen_kr": RUN.screen_kr,
             "screen_coin": RUN.screen_coin, "screen_demo": RUN.screen_demo}
_N_ORIGINALS = {"dispatch": N.dispatch}

try:
    import pytest

    @pytest.fixture(autouse=True)
    def _restore_module():
        for n, v in _ORIGINALS.items():
            setattr(RUN, n, v)
        for n, v in _N_ORIGINALS.items():
            setattr(N, n, v)
        yield
        for n, v in _ORIGINALS.items():
            setattr(RUN, n, v)
        for n, v in _N_ORIGINALS.items():
            setattr(N, n, v)
except ImportError:            # pytest 없이 직접 실행할 때는 아래 __main__ 루프가 처리
    pass


class _Chdir:
    def __init__(self, d):
        self.d, self.prev = d, None

    def __enter__(self):
        self.prev = os.getcwd()
        os.chdir(self.d)
        return self.d

    def __exit__(self, *a):
        os.chdir(self.prev)


def _fake_payload(market="us", data_date="2026-08-14", n=3):
    items = [{"symbol": f"SYM{i}", "name": f"Name {i}", "score": 80.0 - i,
              "grade": "A", "price": 100.0 + i, "change_pct": 1.0,
              "risk": {"rr": 2.0}, "market": "NASDAQ", "turnover": 1e8}
             for i in range(n)]
    return {"market": market, "market_label": "미국주식", "currency": "USD",
            "generated_at": "2026-08-15T06:30:00+09:00", "data_date": data_date,
            "scanned": 10, "count": n, "total_found": n, "truncated": 0,
            "grade_counts": {"A": n}, "items": items}


def _run(market, payload, notify_calls, argv_extra=()):
    RUN.screen_us = lambda: payload
    RUN.screen_kr = lambda: payload
    RUN.screen_demo = lambda: payload

    import screener.notify as N
    N.dispatch = lambda *a, **k: notify_calls.append(True)

    argv = sys.argv
    sys.argv = ["run", "--market", market, "--notify", *argv_extra]
    try:
        RUN.main()
    finally:
        sys.argv = argv


# ── 휴장일 가드 ───────────────────────────────────────────
def test_same_data_date_skips_history_and_notification():
    """미국 공휴일에 워크플로가 돌면 어제와 똑같은 마지막 봉이 잡힌다.

    그대로 두면 어제 히스토리를 같은 내용으로 덮어쓰고, 글자 하나 다르지 않은
    알림을 한 번 더 보낸다. 둘 다 하지 않아야 한다.
    """
    with tempfile.TemporaryDirectory() as d, _Chdir(d):
        calls = []
        _run("us", _fake_payload(data_date="2026-08-14"), calls)
        assert calls == [True], "첫 실행은 알림이 나가야 한다"
        hist = os.listdir(os.path.join("docs", "data", "history"))
        assert hist == ["us_2026-08-14.json"], hist

        calls.clear()
        _run("us", _fake_payload(data_date="2026-08-14", n=3), calls)
        assert calls == [], "같은 날짜 데이터로 알림을 또 보내면 안 된다"
        assert os.listdir(os.path.join("docs", "data", "history")) == hist


def test_new_data_date_writes_history_and_notifies():
    with tempfile.TemporaryDirectory() as d, _Chdir(d):
        calls = []
        _run("us", _fake_payload(data_date="2026-08-14"), calls)
        calls.clear()
        _run("us", _fake_payload(data_date="2026-08-17"), calls)
        assert calls == [True]
        assert sorted(os.listdir(os.path.join("docs", "data", "history"))) == \
            ["us_2026-08-14.json", "us_2026-08-17.json"]


def test_coin_is_exempt_from_the_repeat_guard():
    """코인은 24시간 장이라 하루에 여러 번 도는 게 정상 — 가드에 걸리면 안 된다."""
    with tempfile.TemporaryDirectory() as d, _Chdir(d):
        calls = []
        RUN.screen_coin = lambda: _fake_payload(market="coin", data_date="2026-08-14")
        _run("coin", _fake_payload(market="coin", data_date="2026-08-14"), calls)
        calls.clear()
        _run("coin", _fake_payload(market="coin", data_date="2026-08-14"), calls)
        assert calls == [True], "코인은 같은 날짜여도 알림이 나가야 한다"


# ── 데모 안전장치 ─────────────────────────────────────────
def test_demo_never_touches_history_or_notifies():
    """데모는 합성 데이터다. 히스토리는 날짜당 한 번뿐이라 덮어쓰면 복구가 안 된다."""
    with tempfile.TemporaryDirectory() as d, _Chdir(d):
        calls = []
        _run("kr", _fake_payload(market="kr", data_date="2026-08-14"), calls)
        real_hist = os.path.join("docs", "data", "history", "kr_2026-08-14.json")
        real = json.load(open(real_hist, encoding="utf-8"))

        calls.clear()
        _run("demo", _fake_payload(market="kr", data_date="2026-08-14"), calls)
        assert calls == [], "데모가 알림을 보내면 안 된다"
        assert json.load(open(real_hist, encoding="utf-8")) == real, "히스토리가 오염됐다"
        latest = json.load(open(os.path.join("docs", "data", "kr_latest.json"),
                                encoding="utf-8"))
        assert latest.get("demo") is True, "데모 결과에 표시가 있어야 대시보드가 알린다"


# ── 시장 간 파일 분리 ──────────────────────────────────────
def test_markets_write_disjoint_files():
    """세 시장이 공유하는 파일이 있으면 동시 실행 시 git 충돌로 결과가 날아간다."""
    with tempfile.TemporaryDirectory() as d, _Chdir(d):
        calls = []
        _run("kr", _fake_payload(market="kr", data_date="2026-08-14"), calls)
        _run("us", _fake_payload(market="us", data_date="2026-08-14"), calls)

        files = sorted(os.listdir(os.path.join("docs", "data")))
        assert files == ["history", "kr_latest.json", "us_latest.json"], files
        assert "index.json" not in files, \
            "공용 index.json은 세 워크플로가 동시에 고쳐 쓰다 충돌한다"


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            for n, v in _ORIGINALS.items():
                setattr(RUN, n, v)
            for n, v in _N_ORIGINALS.items():
                setattr(N, n, v)
            try:
                fn()
                print(f"  PASS  {name}")
            except AssertionError as e:
                fails += 1
                print(f"  FAIL  {name}: {e}")
    for n, v in _ORIGINALS.items():
        setattr(RUN, n, v)
    for n, v in _N_ORIGINALS.items():
        setattr(N, n, v)
    print("\n실패" if fails else "\n전체 통과", f"({fails} failed)")
    raise SystemExit(1 if fails else 0)
