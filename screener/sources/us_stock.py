"""미국주식(S&P500 + 나스닥100) 데이터 수집.

■ 유니버스
  대형 우량주 위주 550~600종목. 러셀 소형주·페니주를 넣지 않는 이유는
  (1) 야후 데이터 품질이 들쭉날쭉하고 (2) 유동성이 얕아 지표가 쉽게 왜곡되며
  (3) 국내 투자자가 실제로 매매하기 어려운 종목이 많기 때문이다.

  구성 목록 : GitHub datasets CSV → FinanceDataReader → 내장 스냅샷
  나스닥100 : 위키백과 → 내장 스냅샷 (S&P500과 합집합)
  일봉      : 야후 일괄 다운로드 (KOSPI와 동일한 경로)

■ 심볼 주의 — 이 모듈에서 가장 조용히 터지는 부분
  1) 야후는 클래스주를 점이 아닌 하이픈으로 쓴다. BRK.B → BRK-B, BF.B → BF-B.
  2) FinanceDataReader는 위키백과를 읽으며 심볼의 점을 아예 지워버려서
     BRK.B가 BRKB로 온다. 이건 어느 사이트에도 없는 심볼이라 종목이
     오류 하나 없이 사라진다. 그래서 점을 보존하는 CSV를 1순위로 두고,
     FDR로 내려가더라도 normalize_symbols()가 되돌린다.

■ 상장거래소
  네이버 해외주식 링크(AAPL.O 형태)를 만들려면 거래소를 알아야 한다.
  나스닥트레이더 심볼 디렉터리에서 받아오되, 실패하면 야후 링크로 대체한다.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import requests

from .. import config as C
from ._yahoo import batch_download

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"}

SP500_CSV = ("https://raw.githubusercontent.com/datasets/s-and-p-500-companies"
             "/main/data/constituents.csv")
NDX_WIKI = "https://en.wikipedia.org/wiki/Nasdaq-100"
NASDAQ_DIR = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_DIR = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"

# 네이버 해외주식 URL 접미사. 네이버가 지원하는 3개 거래소만 매핑하고,
# ARCA·Cboe·IEX 상장 종목은 링크를 만들지 않고 야후로 보낸다.
NAVER_SUFFIX = {"NASDAQ": "O", "NYSE": "N", "AMEX": "A"}

# otherlisted.txt의 Exchange 코드 (나스닥트레이더 파일 명세)
#   A=NYSE American(구 AMEX)  N=NYSE  P=NYSE Arca  Z=Cboe BZX  V=IEX
OTHER_EXCHANGE = {"A": "AMEX", "N": "NYSE", "P": "ARCA", "Z": "CBOE", "V": "IEX"}

# 상장폐지·합병으로 사라진 티커가 섞여도 야후에서 조용히 누락될 뿐이라
# 스냅샷이 조금 낡아도 파이프라인은 멈추지 않는다. 갱신은 선택 사항.
NDX_SNAPSHOT = (
    "AAPL ABNB ADBE ADI ADP ADSK AEP AMAT AMD AMGN AMZN ANSS APP ARM ASML AVGO AXON AZN "
    "BIIB BKNG BKR CCEP CDNS CDW CEG CHTR CMCSA COST CPRT CRWD CSCO CSGP CSX CTAS CTSH "
    "DASH DDOG DXCM EA EXC FANG FAST FTNT GEHC GFS GILD GOOG GOOGL HON IDXX INTC INTU "
    "ISRG KDP KHC KLAC LIN LRCX LULU MAR MCHP MDB MDLZ MELI META MNST MRVL MSFT MSTR MU "
    "NFLX NVDA NXPI ODFL ON ORLY PANW PAYX PCAR PDD PEP PLTR PYPL QCOM REGN ROP ROST "
    "SBUX SNPS TEAM TMUS TSLA TTD TTWO TXN VRSK VRTX WBD WDAY XEL ZS"
).split()

# 목록 소스가 전부 죽었을 때 최소한 굴러가게 하는 비상용 (시총 상위 대형주)
SP500_FALLBACK = (
    "AAPL MSFT NVDA AMZN GOOGL GOOG META AVGO TSLA BRK.B JPM LLY V UNH XOM MA COST HD "
    "PG JNJ WMT NFLX ABBV CRM BAC ORCL CVX MRK KO AMD PEP TMO LIN ADBE CSCO ACN MCD "
    "ABT WFC PM IBM GE QCOM CAT TXN DHR VZ INTU AMGN NOW ISRG CMCSA PFE DIS UBER SPGI "
    "GS AXP RTX AMAT NEO BKNG T LOW HON SYK BLK PGR ELV TJX VRTX C LMT ADP MDT SCHW "
    "BSX PLD MMC CB ETN REGN PANW ADI KLAC SBUX MU CI SO DE UPS ZTS BMY MO FI ICE "
    "APH DUK CME EOG SHW WM ITW PYPL AON MCK CVS NOC TGT USB CSX PNC EMR MSI FCX GD"
).split()


# ─────────────────────────────────────────────────────────
# 1. 구성 종목 목록
# ─────────────────────────────────────────────────────────
def _sp500_fdr() -> pd.DataFrame:
    import FinanceDataReader as fdr

    df = fdr.StockListing("S&P500")
    if df is None or df.empty:
        raise RuntimeError("FinanceDataReader S&P500 목록이 비어 있습니다")
    df = df.copy()
    df.columns = [str(c) for c in df.columns]
    sym = next(c for c in ("Symbol", "Code", "Ticker") if c in df.columns)
    nm = next((c for c in ("Name", "Security", "Company") if c in df.columns), sym)
    out = pd.DataFrame({"name": df[nm].astype(str),
                        "sector": df.get("Sector", pd.Series([""] * len(df))).astype(str)})
    out.index = df[sym].astype(str).str.strip().str.upper()
    print(f"[us] S&P500 목록(FinanceDataReader): {len(out)}종목")
    return out


def _sp500_csv() -> pd.DataFrame:
    """1순위 소스 — 깃허브 datasets 리포의 구성종목 CSV (클래스주 점 표기 보존)."""
    r = requests.get(SP500_CSV, headers=UA, timeout=25)
    r.raise_for_status()
    from io import StringIO

    df = pd.read_csv(StringIO(r.text))
    out = pd.DataFrame({"name": df["Security"].astype(str),
                        "sector": df.get("GICS Sector", pd.Series([""] * len(df))).astype(str)})
    out.index = df["Symbol"].astype(str).str.strip().str.upper()
    print(f"[us] S&P500 목록(GitHub CSV 대체): {len(out)}종목")
    return out


def _ndx_wiki() -> list[str]:
    r = requests.get(NDX_WIKI, headers=UA, timeout=25)
    r.raise_for_status()
    from io import StringIO

    for tbl in pd.read_html(StringIO(r.text)):
        cols = [str(c) for c in tbl.columns]
        col = next((c for c in cols if c.lower() in ("ticker", "symbol")), None)
        if col and 90 <= len(tbl) <= 110:
            syms = tbl[col].astype(str).str.strip().str.upper().tolist()
            print(f"[us] 나스닥100 목록(위키백과): {len(syms)}종목")
            return syms
    raise RuntimeError("위키백과에서 나스닥100 표를 찾지 못했습니다")


def normalize_symbols(index: pd.Index) -> pd.Index:
    """클래스주 표기를 점 형태(BRK.B)로 되돌린다.

    FinanceDataReader는 위키백과를 읽으면서 `Symbol.str.replace('.', '')`를 하기 때문에
    버크셔가 `BRKB`, 브라운포맨이 `BFB`로 온다. 이 상태로는 야후에도 없고
    (야후는 BRK-B) 네이버에도 없어서 종목이 조용히 사라진다. 실제로 존재하는
    클래스주는 손에 꼽으므로 명시적으로 되돌린다.
    """
    fix = {"BRKB": "BRK.B", "BFB": "BF.B", "BFA": "BF.A"}
    return pd.Index([fix.get(s, s) for s in index.astype(str).str.strip().str.upper()],
                    name=index.name)


def fetch_universe() -> pd.DataFrame:
    """S&P500 ∪ 나스닥100. index=티커, columns=[name, sector].

    목록 소스 순서는 '점 표기를 보존하는 쪽'이 먼저다. FinanceDataReader는
    클래스주의 점을 지워버려서 2순위로 내린다(내려와도 normalize_symbols가 고친다).
    """
    try:
        sp = _sp500_csv()
    except Exception as e:
        print(f"[us] GitHub CSV 실패 → FinanceDataReader로 전환: {type(e).__name__}: {e}")
        try:
            sp = _sp500_fdr()
        except Exception as e2:
            print(f"[us] FinanceDataReader도 실패 → 내장 스냅샷 사용: {type(e2).__name__}: {e2}")
            sp = pd.DataFrame({"name": SP500_FALLBACK, "sector": ""},
                              index=pd.Index(SP500_FALLBACK))
    sp.index = normalize_symbols(sp.index)

    try:
        ndx = _ndx_wiki()
    except Exception as e:
        print(f"[us] 나스닥100 동적 수집 실패 → 내장 스냅샷 사용: {type(e).__name__}: {e}")
        ndx = NDX_SNAPSHOT

    ndx = list(normalize_symbols(pd.Index(ndx)))
    extra = [s for s in ndx if s and s not in sp.index]
    if extra:
        sp = pd.concat([sp, pd.DataFrame({"name": extra, "sector": ""},
                                         index=pd.Index(extra))])
    sp = sp[~sp.index.duplicated(keep="first")]
    sp.index.name = "symbol"
    print(f"[us] 유니버스 확정: {len(sp)}종목 (나스닥100 추가분 {len(extra)})")
    return sp


def apply_symbol_filters(uni: pd.DataFrame) -> pd.DataFrame:
    """지수·워런트·유닛처럼 주가 지표를 그대로 적용할 수 없는 티커 제외.

    유니버스가 이미 S&P500·나스닥100이라 걸릴 게 거의 없지만, 목록 소스가
    바뀌었을 때 이상한 심볼이 야후 배치를 통째로 망가뜨리는 걸 막는 안전장치다.
    클래스주(BRK.B)는 정상 종목이므로 반드시 살려둔다.
    """
    before = len(uni)
    idx = pd.Series(uni.index.astype(str).str.upper(), index=uni.index)
    ok = (idx.str.fullmatch(r"[A-Z]{1,5}(\.[A-Z])?")          # AAPL, BRK.B
          & ~idx.str.endswith((".W", ".U", ".R")))
    out = uni[ok.to_numpy()]
    if len(out) != before:
        dropped = [s for s in uni.index if s not in out.index]
        print(f"[us] 티커 형식 필터: {before} → {len(out)}종목 (제외: {dropped[:10]})")
    return out


# ─────────────────────────────────────────────────────────
# 2. 상장거래소 (링크 생성용, 실패해도 무방)
# ─────────────────────────────────────────────────────────
def fetch_exchange_map() -> dict[str, str]:
    """{티커: NASDAQ|NYSE|AMEX}. 나스닥트레이더 공개 심볼 디렉터리."""
    out: dict[str, str] = {}
    try:
        r = requests.get(NASDAQ_DIR, headers=UA, timeout=25)
        r.raise_for_status()
        for line in r.text.splitlines()[1:]:
            parts = line.split("|")
            if len(parts) > 1 and parts[0] and not parts[0].startswith("File Creation"):
                out[parts[0].strip().upper()] = "NASDAQ"
    except Exception as e:
        print(f"[us] 나스닥 심볼 디렉터리 실패: {type(e).__name__}: {e}")

    try:
        r = requests.get(OTHER_DIR, headers=UA, timeout=25)
        r.raise_for_status()
        for line in r.text.splitlines()[1:]:
            parts = line.split("|")
            if len(parts) > 2 and parts[0] and not parts[0].startswith("File Creation"):
                # 모르는 코드는 NYSE로 추정하지 않고 "US"로 둔다 → 야후 링크로 빠진다
                out.setdefault(parts[0].strip().upper(),
                               OTHER_EXCHANGE.get(parts[2].strip(), "US"))
    except Exception as e:
        print(f"[us] 기타 심볼 디렉터리 실패: {type(e).__name__}: {e}")

    print(f"[us] 거래소 매핑 {len(out):,}건 확보" if out else "[us] 거래소 매핑 없음 → 야후 링크 사용")
    return out


def link_for(symbol: str, exchange: str | None) -> str:
    """국내 투자자에게 익숙한 네이버 해외주식 페이지. 확실하지 않으면 야후.

    클래스주(BRK.B)는 네이버 표기 규칙이 달라 링크가 깨질 수 있어 야후로 보낸다.
    끊어진 링크보다 낯선 링크가 낫다.
    """
    sfx = NAVER_SUFFIX.get(exchange or "")
    if sfx and "." not in symbol:
        return f"https://m.stock.naver.com/worldstock/stock/{symbol}.{sfx}/total"
    return f"https://finance.yahoo.com/quote/{to_yahoo(symbol)}"


# ─────────────────────────────────────────────────────────
# 3. 일봉
# ─────────────────────────────────────────────────────────
def to_yahoo(symbol: str) -> str:
    """BRK.B → BRK-B. 야후는 클래스 구분자로 하이픈을 쓴다."""
    return symbol.replace(".", "-").upper()


def fetch_ohlcv(uni: pd.DataFrame, days: int = C.HISTORY_DAYS,
                verbose: bool = True) -> dict[str, pd.DataFrame]:
    start = (datetime.now() - timedelta(days=int(days * 1.5))).strftime("%Y-%m-%d")
    sym_to_code = {to_yahoo(s): s for s in uni.index}
    frames = batch_download(list(sym_to_code), start, min_bars=C.MIN_BARS,
                            verbose=verbose, tag="us")
    return {sym_to_code[s]: df for s, df in frames.items()}


# ─────────────────────────────────────────────────────────
# 4. 통합 진입점
# ─────────────────────────────────────────────────────────
def load(days: int = C.HISTORY_DAYS) -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    """(종목별 일봉, 종목 메타) 반환."""
    uni = apply_symbol_filters(fetch_universe())

    frames = fetch_ohlcv(uni, days)
    if not frames:
        raise RuntimeError(
            "미국주식 일봉을 한 건도 받지 못했습니다. 야후 응답을 확인하세요 "
            "(진단 워크플로 '데이터소스 진단' 실행).")

    # 주가·거래대금 하한 — 페니주와 거래가 말라붙은 종목을 걸러낸다
    keep, turnover = [], {}
    for sym, df in frames.items():
        px = float(df["close"].iloc[-1])
        amt = float((df["close"] * df["volume"]).tail(20).mean())
        turnover[sym] = amt
        if px >= C.US_MIN_PRICE and amt >= C.US_MIN_TURNOVER:
            keep.append(sym)
    print(f"[us] 주가(${C.US_MIN_PRICE:.0f} 이상)·거래대금"
          f"(${C.US_MIN_TURNOVER/1e6:.0f}M 이상) 필터: {len(frames)} → {len(keep)}종목")

    exch = fetch_exchange_map()
    meta = uni.loc[[s for s in keep if s in uni.index]].copy()
    # 나스닥트레이더는 점 표기(BRK.B)를 쓰므로 그쪽을 먼저 조회한다
    meta["market"] = [exch.get(s) or exch.get(to_yahoo(s)) or "US" for s in meta.index]
    meta["turnover"] = [turnover[s] for s in meta.index]
    meta["marketcap"] = 0.0
    meta["source"] = "yahoo"
    return {s: frames[s] for s in meta.index}, meta
