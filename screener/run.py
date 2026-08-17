"""스크리너 엔트리포인트.

사용법:
  python -m screener.run --market kr          # 국내주식
  python -m screener.run --market us          # 미국주식(S&P500+나스닥100)
  python -m screener.run --market coin        # 업비트 코인
  python -m screener.run --market coin --notify
  python -m screener.run --market demo        # 합성 데이터로 파이프라인 점검
"""
from __future__ import annotations

import argparse
import json
import os
import traceback
from datetime import datetime, timedelta, timezone

import pandas as pd

from . import config as C
from . import reversal as REV
from . import rules as R
from . import score as S

KST = timezone(timedelta(hours=9))
OUT_DIR = os.path.join("docs", "data")
HIST_DIR = os.path.join(OUT_DIR, "history")

MARKET_LABEL = {"kr": "국내주식", "us": "미국주식", "coin": "코인(업비트)"}


# ─────────────────────────────────────────────────────────
def _write_json(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    print(f"[out] {path} ({os.path.getsize(path)/1024:.0f} KB)")


def _load_prev(market: str) -> dict:
    p = os.path.join(OUT_DIR, f"{market}_latest.json")
    if not os.path.exists(p):
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _diff_new(prev: dict, items: list[dict], grades=("S", "A")) -> list[dict]:
    """직전 실행 대비 새로 상위 등급에 진입한 종목 (알림 스팸 방지)."""
    old = {i["symbol"] for i in prev.get("items", []) if i.get("grade") in grades}
    return [i for i in items if i["grade"] in grades and i["symbol"] not in old]


REV_KEYS = list(REV.CAT_MAX)          # structure, volume, momentum, ma, volatility, coin


def _matrix_row(symbol: str, res: dict, asset: str, turnover: float) -> list:
    """'내 전략' 탭이 브라우저에서 다시 점수를 매길 때 쓰는 한 종목 분량의 원재료.

    키 이름을 종목마다 반복하면 파일이 두 배가 되므로 배열(위치 기반)로 담는다.
    순서는 매트릭스 파일 상단의 row_fields가 설명한다.
    이름·현재가·링크는 latest.json의 search_index가 이미 전 종목 기준으로 갖고 있어
    여기서는 symbol만 담고 브라우저에서 합친다.
    """
    keys = list(C.WEIGHTS)
    tf_order = list(C.TF_WEIGHTS[asset])
    tfs = res.get("tf_scores") or {}
    scores = [[round(tfs[tf][k], 1) for k in keys] if tf in tfs else None
              for tf in tf_order]
    # 8지표의 '핵심 조건 충족' 플래그를 정수 하나(비트마스크)로 — 종목당 30바이트 절약
    okmask = 0
    for i, c in enumerate(res.get("checks") or []):
        if c.get("ok"):
            okmask |= 1 << i
    r = res["risk"]
    return [symbol, *scores, okmask,
            int(round(float(turnover or 0.0) / 1e6)),   # 백만 단위 정수
            r["rr"], r["atr_pct"], res.get("warning_count", 0)]


def _rev_matrix_row(symbol: str, rev: dict) -> list:
    """추세전환 카테고리 점수 — 35점 컷을 적용하기 '전' 값을 담는다.

    컷 자체가 사용자 비중에 따라 달라지므로, 컷을 통과한 것만 담으면
    비중을 바꿔도 후보가 늘어나지 않는다. coin 카테고리가 없는 종목은 null.
    """
    cats = rev.get("category_scores") or {}
    return [symbol, [cats.get(k) for k in REV_KEYS],
            rev["decline_pct"], int(bool(rev["breakout_confirmed"]))]


def _add_reversal(out: list[dict], df, asset_class: str, symbol: str, name: str,
                   market_name: str, link: str, btc_df=None,
                   rev_rows: list | None = None) -> None:
    """추세전환 후보 평가 — 실패해도 메인 스코어링 흐름에 영향을 주지 않도록 감싼다."""
    try:
        rev = REV.evaluate_reversal(df, asset_class, btc_df=btc_df)
    except Exception:
        return
    if rev is None:                       # 하락 15% 미만 = 애초에 반전 대상이 아님
        return
    if rev_rows is not None:
        rev_rows.append(_rev_matrix_row(symbol, rev))
    if rev["stage"] is None:              # 대상이긴 하나 기본 배점으로는 컷 미달
        return
    out.append({
        "symbol": symbol, "name": name, "market": market_name, "link": link,
        "price": rev["price"], "change_pct": rev["change_pct"],
        "score": rev["score"], "stage": rev["stage"], "stage_label": rev["stage_label"],
        "breakout_confirmed": rev["breakout_confirmed"],
        "decline_pct": rev["decline_pct"], "high_price": rev["high_price"],
        "bars_since_high": rev["bars_since_high"],
        "category_scores": rev["category_scores"], "category_max": rev["category_max"],
        "signals": rev["signals"], "reasons": rev["reasons"],
        "signals_passed": rev["signals_passed"], "signals_total": rev["signals_total"],
    })


# ─────────────────────────────────────────────────────────
def screen_kr() -> dict:
    from .sources import kr_stock

    frames, meta = kr_stock.load()

    items, errors, last_date = [], 0, None
    search_index, reversal_candidates = [], []
    matrix_rows, rev_matrix_rows = [], []
    for ticker, df in frames.items():
        try:
            if len(df) < C.MIN_BARS:
                continue
            res = S.evaluate({"1d": df, "1w": S.resample_weekly(df)}, "kr")
            if last_date is None or df.index[-1] > last_date:
                last_date = df.index[-1]
            if res is None:
                continue
            info = meta.loc[ticker]
            name = str(info.get("name", ticker))
            market_name = str(info.get("market", ""))
            link = f"https://m.stock.naver.com/domestic/stock/{ticker}/total"
            # 조건 미달 종목도 이름·티커로 찾을 수 있게 기본 정보만 따로 남긴다
            # (지표 상세는 조건을 만족하는 종목에만 붙는다).
            search_index.append({"symbol": ticker, "name": name, "market": market_name,
                                 "price": res["price"], "change_pct": res["change_pct"],
                                 "score": res["score"], "grade": res["grade"], "link": link})
            matrix_rows.append(_matrix_row(ticker, res, "kr",
                                           float(info.get("turnover", 0.0) or 0.0)))
            _add_reversal(reversal_candidates, df, "kr", ticker, name, market_name, link,
                          rev_rows=rev_matrix_rows)
            if res["score"] < C.MIN_OUTPUT_SCORE:
                continue
            res.update({
                "symbol": ticker,
                "name": name,
                "market": market_name,
                "turnover": float(info.get("turnover", 0.0) or 0.0),
                "marketcap": float(info.get("marketcap", 0.0) or 0.0),
                "link": link,
            })
            items.append(res)
        except Exception:
            errors += 1
            if errors <= 3:
                traceback.print_exc()

    data_date = str(last_date)[:10] if last_date is not None else \
        datetime.now(KST).strftime("%Y-%m-%d")
    print(f"[kr] 평가 {len(frames):,}종목 → 후보 {len(items):,} "
          f"(추세전환 {len(reversal_candidates):,}, 오류 {errors})")
    return _payload("kr", items, len(frames), data_date, search_index, reversal_candidates,
                    matrix_rows, rev_matrix_rows)


def screen_us() -> dict:
    from .sources import us_stock

    frames, meta = us_stock.load()

    items, errors, last_date = [], 0, None
    search_index, reversal_candidates = [], []
    matrix_rows, rev_matrix_rows = [], []
    for ticker, df in frames.items():
        try:
            if len(df) < C.MIN_BARS:
                continue
            res = S.evaluate({"1d": df, "1w": S.resample_weekly(df)}, "us")
            if last_date is None or df.index[-1] > last_date:
                last_date = df.index[-1]
            if res is None:
                continue
            info = meta.loc[ticker]
            exch = str(info.get("market", "US"))
            # 미국 종목은 티커가 곧 이름 역할을 하므로 둘 다 보여준다
            name = str(info.get("name", ticker))
            link = us_stock.link_for(ticker, exch)
            search_index.append({"symbol": ticker, "name": name, "market": exch,
                                 "price": res["price"], "change_pct": res["change_pct"],
                                 "score": res["score"], "grade": res["grade"], "link": link})
            matrix_rows.append(_matrix_row(ticker, res, "us",
                                           float(info.get("turnover", 0.0) or 0.0)))
            _add_reversal(reversal_candidates, df, "us", ticker, name, exch, link,
                          rev_rows=rev_matrix_rows)
            if res["score"] < C.MIN_OUTPUT_SCORE:
                continue
            res.update({
                "symbol": ticker,
                "name": name,
                "market": exch,
                "sector": str(info.get("sector", "") or ""),
                "turnover": float(info.get("turnover", 0.0) or 0.0),
                "marketcap": 0.0,
                "link": link,
            })
            items.append(res)
        except Exception:
            errors += 1
            if errors <= 3:
                traceback.print_exc()

    # 미국장 마감 시점의 현지 날짜가 곧 데이터 날짜다 (한국 날짜와 하루 어긋난다)
    data_date = str(last_date)[:10] if last_date is not None else \
        datetime.now(KST).strftime("%Y-%m-%d")
    print(f"[us] 평가 {len(frames):,}종목 → 후보 {len(items):,} "
          f"(추세전환 {len(reversal_candidates):,}, 오류 {errors})")
    return _payload("us", items, len(frames), data_date, search_index, reversal_candidates,
                    matrix_rows, rev_matrix_rows)


def screen_coin() -> dict:
    from .sources import upbit

    uni = upbit.universe()

    # 코인 특화 추세전환 신호(BTC 대비 상대강도)에 쓸 BTC 일봉 — 종목마다 다시 받지
    # 않도록 한 번만 받아 재사용한다. 실패해도 전체 스크리닝은 계속 진행한다
    # (그 경우 코인 특화 10점만 빠지고 90점 만점으로 재환산된다).
    try:
        btc_df = upbit.candles("KRW-BTC", "1d", 200)
        if btc_df.empty:
            btc_df = None
    except Exception:
        btc_df = None

    items, errors = [], 0
    search_index, reversal_candidates = [], []
    matrix_rows, rev_matrix_rows = [], []
    for market, row in uni.iterrows():
        try:
            frames = {}
            for tf in ("4h", "1d", "1w"):
                df = upbit.candles(market, tf, 200)
                if not df.empty:
                    frames[tf] = df
            if "1d" not in frames or len(frames["1d"]) < C.COIN_MIN_LISTED_DAYS:
                continue
            res = S.evaluate(frames, "coin")
            if res is None:
                continue
            name = str(row["name"])
            link = f"https://upbit.com/exchange?code=CRIX.UPBIT.{market}"
            search_index.append({"symbol": market, "name": name, "market": "업비트 KRW",
                                 "price": res["price"], "change_pct": res["change_pct"],
                                 "score": res["score"], "grade": res["grade"], "link": link})
            matrix_rows.append(_matrix_row(market, res, "coin",
                                           float(row["acc_trade_price_24h"])))
            _add_reversal(reversal_candidates, frames["1d"], "coin", market, name, "업비트 KRW", link,
                         btc_df=None if market == "KRW-BTC" else btc_df,
                         rev_rows=rev_matrix_rows)
            if res["score"] < C.MIN_OUTPUT_SCORE:
                continue
            res.update({
                "symbol": market,
                "name": name,
                "market": "업비트 KRW",
                "turnover": float(row["acc_trade_price_24h"]),
                "marketcap": 0.0,
                "link": link,
            })
            items.append(res)
        except Exception:
            errors += 1
            if errors <= 3:
                traceback.print_exc()

    print(f"[coin] 평가 {len(uni)}종목 → 후보 {len(items)} "
          f"(추세전환 {len(reversal_candidates)}, 오류 {errors})")
    return _payload("coin", items, len(uni), datetime.now(KST).strftime("%Y-%m-%d"),
                    search_index, reversal_candidates, matrix_rows, rev_matrix_rows)


def screen_demo() -> dict:
    """네트워크 없이 파이프라인·대시보드를 점검하기 위한 합성 데이터."""
    import numpy as np

    rng = np.random.default_rng(7)
    items, search_index, matrix_rows, rev_matrix_rows = [], [], [], []
    for i in range(24):
        n = 260
        drift = rng.normal(0.0012 if i % 3 == 0 else -0.0003, 0.001)
        steps = rng.normal(drift, 0.022, n)
        if i % 3 == 0:                       # 최근 상승 + 거래량 증가 종목
            steps[-12:] += 0.012
        close = 10000 * np.exp(np.cumsum(steps))
        high = close * (1 + np.abs(rng.normal(0, 0.008, n)))
        low = close * (1 - np.abs(rng.normal(0, 0.008, n)))
        open_ = np.r_[close[0], close[:-1]]
        vol = rng.lognormal(11, 0.4, n)
        if i % 3 == 0:
            vol[-3:] *= rng.uniform(1.8, 3.2)
        # 주말에 실행하면 bdate_range(end=토/일, periods=n)이 n-1개만 반환하는
        # pandas 특성이 있어(끝점이 영업일이 아니면 카운트가 어긋남), 여유 있게
        # 만든 뒤 뒤에서 n개만 잘라 항상 길이를 맞춘다.
        idx = pd.bdate_range(end=datetime.now(), periods=n + 5)[-n:]
        df = pd.DataFrame({"open": open_, "high": high, "low": low,
                           "close": close, "volume": vol}, index=idx)
        res = S.evaluate({"1d": df, "1w": S.resample_weekly(df)}, "kr")
        if res is None:
            continue
        sym, name = f"00000{i:02d}", f"샘플종목{i:02d}"
        mk = "KOSPI" if i % 2 else "KOSDAQ"
        turnover = float(vol[-1] * close[-1])
        # 실제 스크리너와 같은 순서 — 점수 컷 '전'에 검색 인덱스와 매트릭스를 먼저 쌓는다
        search_index.append({"symbol": sym, "name": name, "market": mk,
                             "price": res["price"], "change_pct": res["change_pct"],
                             "score": res["score"], "grade": res["grade"], "link": "#"})
        matrix_rows.append(_matrix_row(sym, res, "kr", turnover))
        if res["score"] < C.MIN_OUTPUT_SCORE:
            continue
        res.update({"symbol": sym, "name": name, "market": mk,
                    "turnover": turnover, "marketcap": 5e11, "link": "#"})
        items.append(res)

    # 추세전환 탭 오프라인 미리보기용 — 급락 후 바닥을 다지는 합성 종목 몇 개를 더 만든다.
    # pre_pad(120봉 관찰창 밖의 지표 워밍업용) + lead(고점 기준) + decline(급락) +
    # bounce/retest(Higher Low) + breakout(반등 고점 돌파)로 구성 — 관찰창(120봉) 안에
    # 고점·급락·바닥 다지기가 전부 들어오도록 길이를 맞췄다.
    reversal_candidates = []
    for i in range(6):
        pre_pad = np.full(100, 10000.0) * (1 + rng.normal(0, 0.003, 100))
        lead = np.full(20, 10000.0) * (1 + rng.normal(0, 0.003, 20))
        decline = lead[-1] * np.exp(np.cumsum(rng.normal(-0.013 - i * 0.0004, 0.017, 50)))
        low1 = decline[-1]
        bounce = low1 * np.exp(np.cumsum(rng.normal(0.008, 0.011, 10)))
        interim_high = bounce[-1]
        retest = interim_high * np.exp(np.cumsum(rng.normal(-0.004, 0.009, 8)))
        low2 = retest[-1]
        breakout = low2 * np.exp(np.cumsum(rng.normal(0.009 + i * 0.001, 0.011, 27)))
        close = np.concatenate([pre_pad, lead, decline, bounce, retest, breakout])
        m = len(close)
        high = close * (1 + np.abs(rng.normal(0, 0.006, m)))
        low = close * (1 - np.abs(rng.normal(0, 0.006, m)))
        open_ = np.r_[close[0], close[:-1]]
        vol = rng.lognormal(11, 0.25, m)
        decl_start, decl_end = 120, 170       # pre_pad(100)+lead(20)=120, decline 50봉
        vol[decl_start:decl_end] *= np.linspace(2.2, 1.0, decl_end - decl_start)
        vol[decl_end + 18:] *= np.linspace(1.2, 2.0, m - (decl_end + 18))
        idx = pd.bdate_range(end=datetime.now(), periods=m + 5)[-m:]
        rdf = pd.DataFrame({"open": open_, "high": high, "low": low,
                            "close": close, "volume": vol}, index=idx)
        _add_reversal(reversal_candidates, rdf, "kr", f"10000{i:02d}", f"반전샘플{i:02d}",
                     "KOSPI" if i % 2 else "KOSDAQ", "#", rev_rows=rev_matrix_rows)
        # 반전 샘플도 '내 전략' 탭에서 보이도록 검색 인덱스·매트릭스에 함께 넣는다
        rres = S.evaluate({"1d": rdf, "1w": S.resample_weekly(rdf)}, "kr")
        if rres is not None:
            search_index.append({"symbol": f"10000{i:02d}", "name": f"반전샘플{i:02d}",
                                 "market": "KOSPI" if i % 2 else "KOSDAQ",
                                 "price": rres["price"], "change_pct": rres["change_pct"],
                                 "score": rres["score"], "grade": rres["grade"], "link": "#"})
            matrix_rows.append(_matrix_row(f"10000{i:02d}", rres, "kr",
                                           float(vol[-1] * close[-1])))

    return _payload("kr", items, len(search_index), datetime.now(KST).strftime("%Y-%m-%d"),
                    search_index, reversal_candidates, matrix_rows, rev_matrix_rows)


# ─────────────────────────────────────────────────────────
def _matrix_payload(market: str, asset: str, data_date: str, generated_at: str,
                    rows: list, rev_rows: list) -> dict:
    """'내 전략' 탭 전용 파일 — 평가에 성공한 '전' 종목의 지표별 원점수.

    latest.json의 items는 55점 이상 상위 200종목뿐이라, 사용자가 비중을 바꿔도
    그 200종목 안에서만 순서가 바뀐다. 내 비중으로 90점이 될 종목이 그 밖에 있으면
    기능 자체가 무의미해지므로 전 종목을 담되, 파일을 따로 떼어 '내 전략' 탭을
    처음 열 때만 받도록 한다 (매수신호·추세전환 탭 로딩에는 영향 없음).
    """
    tf_order = list(C.TF_WEIGHTS[asset])
    return {
        "market": market,
        # latest.json과 짝이 맞는지 브라우저가 대조한다 (배포 타이밍이 어긋난 경우 방어)
        "data_date": data_date,
        "generated_at": generated_at,
        "keys": list(C.WEIGHTS),
        "key_labels": C.INDICATOR_LABELS,
        "tfs": tf_order,
        "tf_labels": {tf: C.TF_LABELS[tf] for tf in tf_order},
        "tf_weights": C.TF_WEIGHTS[asset],
        "align": {"threshold": C.MTF_ALIGN_THRESHOLD, "bonus": C.MTF_ALIGN_BONUS},
        "default_weights": C.WEIGHTS,
        "presets": C.WEIGHT_PRESETS,
        "filters": C.HARD_FILTER_DEFS,
        "concentration_warn": C.WEIGHT_CONCENTRATION_WARN,
        "slider_max": C.MY_SLIDER_MAX,
        "min_output_score": C.MIN_OUTPUT_SCORE,
        # 위치 기반 배열이라 이 설명이 곧 스키마다
        "row_fields": ["symbol"] + [f"s_{tf}" for tf in tf_order]
                      + ["okmask", "turnover_m", "rr", "atr_pct", "warn"],
        "rows": rows,
        # 추세전환 커스텀용 — 35점 컷 적용 전, 하락 15% 이상 전 종목
        "rev_keys": REV_KEYS,
        "rev_labels": REV.CAT_LABELS,
        "rev_max": REV.CAT_MAX,
        "rev_presets": C.REV_WEIGHT_PRESETS,
        "rev_row_fields": ["symbol", "cats", "decline_pct", "breakout"],
        "rev_rows": rev_rows,
    }


def _payload(market: str, items: list[dict], scanned: int, data_date: str,
             search_index: list[dict] | None = None,
             reversal_candidates: list[dict] | None = None,
             matrix_rows: list | None = None,
             rev_matrix_rows: list | None = None) -> dict:
    items.sort(key=lambda x: x["score"], reverse=True)

    counts: dict[str, int] = {}
    for it in items:
        counts[it["grade"]] = counts.get(it["grade"], 0) + 1
    total_found = len(items)

    truncated = max(0, len(items) - C.MAX_OUTPUT_ITEMS)
    if truncated:
        # 조용히 잘라내지 않고 몇 건을 뺐는지 남깁니다
        print(f"[out] 후보 {len(items):,}건 중 상위 {C.MAX_OUTPUT_ITEMS}건만 저장 "
              f"({truncated:,}건 제외, 최저 점수 {items[C.MAX_OUTPUT_ITEMS - 1]['score']:.1f})")
        items = items[:C.MAX_OUTPUT_ITEMS]

    reversal_candidates = list(reversal_candidates or [])
    # 확인 진입 → 초기 진입 순, 그 안에서는 점수 내림차순 (돌파가 이미 확인된 쪽을 먼저 보여준다)
    reversal_candidates.sort(key=lambda x: (x["stage"] != "confirmed", -x["score"]))
    rev_total_found = len(reversal_candidates)
    rev_truncated = max(0, len(reversal_candidates) - C.REV_MAX_OUTPUT_ITEMS)
    if rev_truncated:
        print(f"[out] 추세전환 후보 {len(reversal_candidates):,}건 중 상위 "
              f"{C.REV_MAX_OUTPUT_ITEMS}건만 저장 ({rev_truncated:,}건 제외)")
        reversal_candidates = reversal_candidates[:C.REV_MAX_OUTPUT_ITEMS]
    rev_stage_counts = {"early": 0, "confirmed": 0}
    for rc in reversal_candidates:
        rev_stage_counts[rc["stage"]] = rev_stage_counts.get(rc["stage"], 0) + 1

    generated_at = datetime.now(KST).isoformat(timespec="seconds")
    asset = market if market in C.TF_WEIGHTS else "kr"

    return {
        "market": market,
        "market_label": MARKET_LABEL.get(market, market),
        "currency": C.CURRENCY.get(market, "KRW"),
        "generated_at": generated_at,
        "data_date": data_date,
        # 내 전략 탭이 있는지 브라우저가 미리 알 수 있게 하는 표식 (파일은 따로 받는다)
        "has_matrix": bool(matrix_rows),
        "scanned": scanned,
        "count": len(items),
        "total_found": total_found,
        "truncated": truncated,
        "grade_counts": counts,
        "weights": C.WEIGHTS,
        "indicator_labels": C.INDICATOR_LABELS,
        "grade_cuts": {g: c for g, c in C.GRADE_CUTS},
        # 종목마다 같은 내용이라 최상단에 한 번만 담습니다
        "grade_info": C.GRADE_INFO,
        "indicator_help": R.BEGINNER_HELP,
        "conditions": R.CORE_CONDITION_TEXT,
        "items": items,
        # 조건 미달로 목록엔 안 뜨지만 이름·티커 검색으로는 찾을 수 있는 전체 스캔 대상
        # (기본 정보만 — 지표 상세는 items 쪽에만 있습니다).
        "search_index": search_index or [],
        # 추세전환(바닥권 반전) 후보 — 매수신호 등급과는 별개의 스코어링.
        # 최근 120봉 고점 대비 15% 이상 하락한 종목만 대상으로 하므로, 매수신호 탭의
        # items와는 대체로 겹치지 않는다 (하락 중인 종목은 애초에 매수신호 점수가 낮다).
        "reversal_candidates": reversal_candidates,
        "reversal_total_found": rev_total_found,
        "reversal_truncated": rev_truncated,
        "reversal_stage_counts": rev_stage_counts,
        "reversal_cat_labels": REV.CAT_LABELS,
        "reversal_stage_label": REV.STAGE_LABEL,
        "reversal_stage_help": REV.STAGE_HELP,
        "reversal_min_decline_pct": REV.MIN_DECLINE_PCT,
        # main()이 꺼내서 {market}_matrix.json으로 따로 저장한다 (latest.json에는 안 담긴다)
        "_matrix": _matrix_payload(market, asset, data_date, generated_at,
                                   list(matrix_rows or []), list(rev_matrix_rows or [])),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", required=True, choices=["kr", "us", "coin", "demo"])
    ap.add_argument("--notify", action="store_true", help="텔레그램/이메일 발송")
    ap.add_argument("--daily-summary", action="store_true",
                    help="신규 진입뿐 아니라 전체 요약을 강제로 발송")
    args = ap.parse_args()

    fn = {"kr": screen_kr, "us": screen_us,
          "coin": screen_coin, "demo": screen_demo}[args.market]
    payload = fn()
    market_key = payload["market"]
    matrix = payload.pop("_matrix", None)

    prev = _load_prev(market_key)
    new_items = _diff_new(prev, payload["items"])
    payload["new_entries"] = [i["symbol"] for i in new_items]

    # 휴장일 방어 — 거래소가 쉬면 어제와 똑같은 마지막 봉이 다시 잡힌다.
    # 그대로 두면 (1) 어제 히스토리 스냅샷을 같은 내용으로 덮어쓰고
    # (2) 어제와 글자 하나 다르지 않은 알림을 한 번 더 보낸다.
    # 코인은 24시간 장이라 하루에도 여러 번 도는 게 정상이므로 제외한다.
    repeat = (market_key in ("kr", "us") and bool(prev)
              and prev.get("data_date") == payload["data_date"])
    if repeat:
        print(f"[{market_key}] 데이터 날짜가 직전 실행과 동일({payload['data_date']}) "
              f"→ 휴장일로 보고 히스토리 저장·알림을 건너뜁니다")

    if args.market == "demo":
        # 데모는 합성 데이터다. 실제 결과 파일을 덮어쓰되 히스토리는 절대 건드리지 않는다
        # (히스토리는 그 날짜에 한 번뿐이라 덮어쓰면 복구가 안 된다).
        payload["demo"] = True
        print("[demo] ⚠ docs/data/kr_latest.json 을 합성 데이터로 덮어씁니다 "
              "(히스토리·알림은 건너뜀). 실제 결과가 필요하면 --market kr 을 실행하세요.")

    _write_json(os.path.join(OUT_DIR, f"{market_key}_latest.json"), payload)

    # '내 전략' 탭용 매트릭스 — 사용자가 그 탭을 처음 열 때만 받아가는 별도 파일.
    # 행이 하나도 없으면(전부 평가 실패) 예전 파일을 지우지 않고 그대로 둔다.
    if matrix and matrix["rows"]:
        _write_json(os.path.join(OUT_DIR, f"{market_key}_matrix.json"), matrix)
        print(f"[out] 내 전략 매트릭스 {len(matrix['rows']):,}종목 "
              f"(추세전환 대상 {len(matrix['rev_rows']):,})")

    # 히스토리 스냅샷 (나중에 적중률 검증용) — 상위 60종목만 경량 보관
    if args.market != "demo" and not repeat:
        slim = {k: v for k, v in payload.items() if k != "items"}
        slim["items"] = [{k: it[k] for k in ("symbol", "name", "score", "grade", "price",
                                             "change_pct", "risk")}
                         for it in payload["items"][:60]]
        _write_json(os.path.join(HIST_DIR, f"{market_key}_{payload['data_date']}.json"), slim)

    # 시장별 결과 파일이 서로 겹치지 않도록, 공용 index.json은 만들지 않습니다.
    # 예전에는 세 워크플로가 같은 index.json을 읽고-고쳐-쓰다가 실행이 겹치면
    # git rebase 충돌로 한쪽 실행 결과가 통째로 사라졌습니다(그런데도 초록불).
    # 대시보드는 각 {market}_latest.json 안의 generated_at을 직접 읽습니다.

    if args.notify and args.market != "demo" and not repeat:
        from . import notify
        notify.dispatch(payload, new_items, force_summary=args.daily_summary)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
