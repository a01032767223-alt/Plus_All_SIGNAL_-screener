"""국내주식(KOSPI/KOSDAQ) 데이터 수집.

■ 왜 KRX를 안 쓰는가
  2025-12-27부터 KRX 정보데이터시스템이 회원 로그인 필수로 전환되어
  서버에서 직접 수집할 수 없다(로그인 없이 호출하면 JSON 대신 로그인 페이지가 온다).

■ 실측 근거 (2026-08-14, GitHub Actions 러너 = 미국 IP)
  ✅ FinanceDataReader 종목목록   0.8s   942종목
  ✅ 야후 일괄 다운로드           1.1s   121일 × 4종목
  ✅ 네이버 siseJson             0.9s   종목당
  ❌ KRX 직접 호출 / pykrx        로그인 필요

■ 구조
  종목목록 : FinanceDataReader → (실패 시) 네이버 시가총액 페이지
  일봉     : 야후 일괄 다운로드 → (수집률 저조 시) 네이버 siseJson 종목별

  야후는 한 번에 수십 종목을 받아오므로 전종목이 수십 초면 끝난다.
  덕분에 증분 캐시 없이 매번 전체를 새로 받아도 되고, 캐시 정합성 문제가 사라진다.
"""
from __future__ import annotations

import ast
import time
from datetime import datetime, timedelta

import pandas as pd
import requests

from .. import config as C
# 야후 수집 경로는 미국주식과 완전히 같은 로직이라 공통부에서 가져다 쓴다
from ._yahoo import CHUNK, OHLCV_COLS, batch_download, tidy as _tidy

# CHUNK·OHLCV_COLS는 이 모듈의 이름으로도 참조되므로 재수출임을 명시한다
__all__ = ["CHUNK", "OHLCV_COLS", "YAHOO_SUFFIX", "fetch_universe",
           "apply_name_filters", "fetch_ohlcv_yahoo", "fetch_ohlcv_naver", "load"]

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Referer": "https://finance.naver.com/",
}
YAHOO_SUFFIX = {"KOSPI": ".KS", "KOSDAQ": ".KQ", "KONEX": ".KN"}


# ─────────────────────────────────────────────────────────
# 1. 종목 목록
# ─────────────────────────────────────────────────────────
def _universe_fdr() -> pd.DataFrame:
    import FinanceDataReader as fdr

    frames = []
    for mk in ("KOSPI", "KOSDAQ"):
        df = fdr.StockListing(mk)
        if df is None or df.empty:
            continue
        df = df.copy()
        df.columns = [str(c) for c in df.columns]
        if "Market" not in df.columns:
            df["Market"] = mk
        frames.append(df)
    if not frames:
        raise RuntimeError("FinanceDataReader 종목목록이 비어 있습니다")

    all_df = pd.concat(frames, ignore_index=True)
    out = pd.DataFrame({
        "name": all_df.get("Name", pd.Series(dtype=object)).astype(str),
        "market": all_df["Market"].astype(str),
    })
    out.index = all_df["Code"].astype(str).str.zfill(6)
    out.index.name = "code"

    # 시가총액 컬럼명이 버전에 따라 다르다
    for cand in ("Marcap", "MarketCap", "Marketcap", "시가총액"):
        if cand in all_df.columns:
            out["marketcap"] = pd.to_numeric(all_df[cand], errors="coerce").values
            break
    for cand in ("Amount", "거래대금"):
        if cand in all_df.columns:
            out["turnover"] = pd.to_numeric(all_df[cand], errors="coerce").values
            break

    out = out[~out.index.duplicated(keep="first")]
    print(f"[kr] 종목목록(FinanceDataReader): {len(out):,}종목 "
          f"(시가총액 {'있음' if 'marketcap' in out else '없음'})")
    return out


def _universe_naver() -> pd.DataFrame:
    """FDR이 실패했을 때 쓰는 대체 경로 — 네이버 시가총액 페이지."""
    rows = []
    for sosok, market in ((0, "KOSPI"), (1, "KOSDAQ")):
        for page in range(1, 35):
            r = requests.get("https://finance.naver.com/sise/sise_market_sum.naver",
                             params={"sosok": sosok, "page": page},
                             headers=UA, timeout=15)
            r.encoding = "euc-kr"
            try:
                tables = pd.read_html(r.text)
            except ValueError:
                break
            tbl = max(tables, key=len) if tables else None
            if tbl is None or "종목명" not in tbl.columns:
                break
            tbl = tbl.dropna(subset=["종목명"])
            if tbl.empty:
                break
            codes = pd.Series(r.text).str.extractall(r"/item/main\.naver\?code=(\d{6})")[0]
            codes = codes.drop_duplicates().tolist()
            names = tbl["종목명"].astype(str).tolist()
            cap = (tbl["시가총액"] * 1e8).tolist() if "시가총액" in tbl.columns else [None] * len(names)
            for code, nm, mc in zip(codes, names, cap):
                rows.append({"code": code, "name": nm, "market": market, "marketcap": mc})
            time.sleep(0.2)
    if not rows:
        raise RuntimeError("네이버 종목목록도 받지 못했습니다")
    out = pd.DataFrame(rows).drop_duplicates(subset="code").set_index("code")
    print(f"[kr] 종목목록(네이버 대체): {len(out):,}종목")
    return out


def fetch_universe() -> pd.DataFrame:
    try:
        return _universe_fdr()
    except Exception as e:
        print(f"[kr] FinanceDataReader 실패 → 네이버로 전환: {type(e).__name__}: {e}")
        return _universe_naver()


def apply_name_filters(uni: pd.DataFrame) -> pd.DataFrame:
    """우선주·스팩·리츠 등 지표 해석이 다른 종목 제외 + 시가총액 하한."""
    df, before = uni.copy(), len(uni)

    names = df["name"].astype(str)
    pat = "|".join(C.KR_EXCLUDE_PATTERNS)
    df = df[~names.str.contains(pat, na=False, regex=True)]
    df = df[~df["name"].astype(str).str.fullmatch(r".*[0-9]?우(B|C)?")]

    if "marketcap" in df.columns and df["marketcap"].notna().any():
        df = df[(df["marketcap"].isna()) | (df["marketcap"] >= C.KR_MIN_MARKETCAP)]

    print(f"[kr] 종목명·시가총액 필터: {before:,} → {len(df):,}종목")
    return df


# ─────────────────────────────────────────────────────────
# 2. 일봉 — 야후 일괄 다운로드
# ─────────────────────────────────────────────────────────
def _yahoo_symbol(code: str, market: str) -> str:
    return f"{code}{YAHOO_SUFFIX.get(market, '.KS')}"


def fetch_ohlcv_yahoo(uni: pd.DataFrame, days: int = C.HISTORY_DAYS,
                      verbose: bool = True) -> dict[str, pd.DataFrame]:
    """야후 일괄 다운로드. 배치 루프 자체는 미국주식과 공통부를 쓴다."""
    start = (datetime.now() - timedelta(days=int(days * 1.5))).strftime("%Y-%m-%d")
    sym_to_code = {_yahoo_symbol(c, m): c for c, m in uni["market"].items()}
    frames = batch_download(list(sym_to_code), start, min_bars=C.MIN_BARS,
                            verbose=verbose, tag="kr")
    return {sym_to_code[s]: df for s, df in frames.items()}


# ─────────────────────────────────────────────────────────
# 3. 일봉 — 네이버 (야후 실패 시 대체)
# ─────────────────────────────────────────────────────────
def _naver_ohlcv(code: str, days: int) -> pd.DataFrame | None:
    end = datetime.now()
    start = end - timedelta(days=int(days * 1.5))
    r = requests.get("https://api.finance.naver.com/siseJson.naver",
                     params={"symbol": code, "requestType": 1,
                             "startTime": start.strftime("%Y%m%d"),
                             "endTime": end.strftime("%Y%m%d"),
                             "timeframe": "day"},
                     headers=UA, timeout=15)
    body = r.text.strip()
    if not body.startswith("["):
        return None
    # 응답은 JSON이 아니라 파이썬 리터럴 형태(작은따옴표)라 literal_eval로 읽는다
    rows = ast.literal_eval(body)
    if len(rows) < 2:
        return None
    df = pd.DataFrame(rows[1:], columns=rows[0])
    df = df.rename(columns={"날짜": "date", "시가": "open", "고가": "high",
                            "저가": "low", "종가": "close", "거래량": "volume"})
    if "date" not in df.columns:
        return None
    df["date"] = pd.to_datetime(df["date"].astype(str), format="%Y%m%d", errors="coerce")
    df = df.dropna(subset=["date"]).set_index("date")
    return _tidy(df.rename(columns=str.title))


def fetch_ohlcv_naver(codes: list[str], days: int = C.HISTORY_DAYS,
                      verbose: bool = True) -> dict[str, pd.DataFrame]:
    frames, fails = {}, 0
    for i, code in enumerate(codes, 1):
        try:
            df = _naver_ohlcv(code, days)
            if df is not None and len(df) >= C.MIN_BARS:
                frames[code] = df
        except Exception:
            fails += 1
            if fails > 40 and fails > i * 0.5:
                print(f"[kr] 네이버 연속 실패 과다({fails}건) → 중단")
                break
        if verbose and i % 200 == 0:
            print(f"  ... {i}/{len(codes)}종목 (확보 {len(frames)})", flush=True)
        time.sleep(0.12)
    print(f"[kr] 네이버 수집 완료: {len(frames):,}/{len(codes):,}종목")
    return frames


# ─────────────────────────────────────────────────────────
# 4. 통합 진입점
# ─────────────────────────────────────────────────────────
def load(days: int = C.HISTORY_DAYS) -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    """(종목별 일봉, 종목 메타) 반환. 소스 장애 시 자동으로 대체 경로를 탄다."""
    uni = apply_name_filters(fetch_universe())

    frames, source = {}, "yahoo"
    try:
        frames = fetch_ohlcv_yahoo(uni, days)
    except Exception as e:
        print(f"[kr] 야후 전체 실패: {type(e).__name__}: {e}")

    # 수집률이 절반도 안 되면 야후가 국내 종목을 제대로 못 주는 상황 → 네이버로 재시도
    if len(frames) < len(uni) * 0.5:
        print(f"[kr] 야후 수집률 저조({len(frames)}/{len(uni)}) → 네이버로 재시도")
        naver_frames = fetch_ohlcv_naver([c for c in uni.index if c not in frames], days)
        if naver_frames:
            frames.update(naver_frames)
            source = "yahoo+naver" if len(frames) > len(naver_frames) else "naver"

    if not frames:
        raise RuntimeError(
            "국내주식 일봉을 한 건도 받지 못했습니다. "
            "야후·네이버 양쪽 모두 실패 — 진단 워크플로(데이터소스 진단)를 실행해 보세요.")

    # 거래대금(종가×거래량) 하한 — 종목목록에 거래대금이 없어도 여기서 걸러진다
    turnover = {c: float(df["close"].iloc[-1] * df["volume"].iloc[-1]) for c, df in frames.items()}
    keep = [c for c, v in turnover.items() if v >= C.KR_MIN_TURNOVER]
    print(f"[kr] 거래대금 필터({C.KR_MIN_TURNOVER/1e8:.0f}억 이상): "
          f"{len(frames):,} → {len(keep):,}종목")

    meta = uni.loc[[c for c in keep if c in uni.index]].copy()
    meta["turnover"] = [turnover[c] for c in meta.index]
    meta["source"] = source
    return {c: frames[c] for c in meta.index}, meta
