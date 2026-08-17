"""기술적 지표 계산 — pandas/numpy만 사용 (TA-Lib 등 컴파일 의존성 없음).

입력 DataFrame은 컬럼 open/high/low/close/volume 을 가지며 날짜 오름차순 정렬.
모든 함수는 원본을 수정하지 않고 새 Series/DataFrame을 반환.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import config as C


# ── 이동평균 ───────────────────────────────────────────────
def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


# ── RSI (Wilder) ──────────────────────────────────────────
def rsi(close: pd.Series, n: int = C.RSI_PERIOD) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    # Wilder smoothing == ewm(alpha=1/n)
    avg_gain = gain.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    avg_loss = loss.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - (100.0 / (1.0 + rs))
    # 손실이 0이면 RSI 100
    out = out.where(avg_loss.ne(0.0) | avg_gain.isna(), 100.0)
    return out


# ── MACD ──────────────────────────────────────────────────
def macd(close: pd.Series,
         fast: int = C.MACD_FAST,
         slow: int = C.MACD_SLOW,
         signal: int = C.MACD_SIGNAL) -> pd.DataFrame:
    macd_line = ema(close, fast) - ema(close, slow)
    signal_line = macd_line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return pd.DataFrame({
        "macd": macd_line,
        "signal": signal_line,
        "hist": macd_line - signal_line,
    })


# ── 볼린저밴드 ─────────────────────────────────────────────
def bollinger(close: pd.Series,
              n: int = C.BB_PERIOD,
              k: float = C.BB_STD) -> pd.DataFrame:
    mid = sma(close, n)
    sd = close.rolling(n, min_periods=n).std(ddof=0)
    upper, lower = mid + k * sd, mid - k * sd
    width = (upper - lower) / mid.replace(0.0, np.nan)
    # %B : 밴드 내 상대 위치 (0=하단, 1=상단)
    pct_b = (close - lower) / (upper - lower).replace(0.0, np.nan)
    return pd.DataFrame({"mid": mid, "upper": upper, "lower": lower,
                         "width": width, "pct_b": pct_b})


# ── True Range / ATR ──────────────────────────────────────
def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    return pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)


def atr(df: pd.DataFrame, n: int = C.ATR_PERIOD) -> pd.Series:
    return true_range(df).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


# ── ADX / DI (Wilder) ─────────────────────────────────────
def adx(df: pd.DataFrame, n: int = C.ADX_PERIOD) -> pd.DataFrame:
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)

    tr = true_range(df)
    atr_ = tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(
        alpha=1 / n, adjust=False, min_periods=n).mean() / atr_.replace(0.0, np.nan)
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(
        alpha=1 / n, adjust=False, min_periods=n).mean() / atr_.replace(0.0, np.nan)

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan)
    adx_ = dx.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    return pd.DataFrame({"adx": adx_, "plus_di": plus_di, "minus_di": minus_di})


# ── OBV ───────────────────────────────────────────────────
def obv(df: pd.DataFrame) -> pd.Series:
    direction = np.sign(df["close"].diff().fillna(0.0))
    return (direction * df["volume"]).cumsum()


# ── 스토캐스틱(Slow) ────────────────────────────────────────
def stochastic(df: pd.DataFrame, k_period: int = 14,
               smooth: int = 3, d_period: int = 3) -> pd.DataFrame:
    low_n = df["low"].rolling(k_period, min_periods=k_period).min()
    high_n = df["high"].rolling(k_period, min_periods=k_period).max()
    raw_k = (df["close"] - low_n) / (high_n - low_n).replace(0.0, np.nan) * 100.0
    k = raw_k.rolling(smooth, min_periods=smooth).mean()   # Slow %K
    d = k.rolling(d_period, min_periods=d_period).mean()   # %D
    return pd.DataFrame({"k": k, "d": d})


def slope_pct(s: pd.Series, n: int) -> pd.Series:
    """n봉 전 대비 변화율(%). 기준값이 0이거나 부호가 다르면 절대값 기준."""
    base = s.shift(n)
    denom = base.abs().replace(0.0, np.nan)
    return (s - base) / denom * 100.0


# ── 스윙 고/저 (지지·저항) ─────────────────────────────────
def swing_levels(df: pd.DataFrame, lookback: int = C.STRUCTURE_LOOKBACK) -> pd.DataFrame:
    """최근 lookback봉의 최고가/최저가.
    당일 돌파 판정을 위해 '전일까지'의 최고가(prior_high)도 함께 제공."""
    high_n = df["high"].rolling(lookback, min_periods=max(10, lookback // 3)).max()
    low_n = df["low"].rolling(lookback, min_periods=max(10, lookback // 3)).min()
    return pd.DataFrame({
        "high_n": high_n,
        "low_n": low_n,
        "prior_high": high_n.shift(1),
        "prior_low": low_n.shift(1),
    })


# ── 한 번에 전부 붙이기 ────────────────────────────────────
def enrich(df: pd.DataFrame) -> pd.DataFrame:
    """OHLCV DataFrame에 모든 지표 컬럼을 추가한 새 DataFrame 반환."""
    out = df.copy()
    close = out["close"]

    for p in C.MA_PERIODS:
        out[f"ma{p}"] = sma(close, p)

    out["rsi"] = rsi(close)

    m = macd(close)
    out["macd"], out["macd_signal"], out["macd_hist"] = m["macd"], m["signal"], m["hist"]

    b = bollinger(close)
    out["bb_mid"], out["bb_upper"], out["bb_lower"] = b["mid"], b["upper"], b["lower"]
    out["bb_width"], out["bb_pct_b"] = b["width"], b["pct_b"]
    # 밴드폭의 120봉 내 백분위 (수축 판정용)
    out["bb_width_pct"] = out["bb_width"].rolling(120, min_periods=40).rank(pct=True)

    a = adx(out)
    out["adx"], out["plus_di"], out["minus_di"] = a["adx"], a["plus_di"], a["minus_di"]

    out["atr"] = atr(out)
    out["atr_pct"] = out["atr"] / close.replace(0.0, np.nan) * 100.0

    out["obv"] = obv(out)
    out["obv_slope"] = slope_pct(out["obv"], 20)
    out["obv_max60"] = out["obv"].rolling(60, min_periods=20).max()

    st = stochastic(out)
    out["stoch_k"], out["stoch_d"] = st["k"], st["d"]

    out["vol_ma"] = sma(out["volume"], C.VOL_MA_PERIOD)
    out["vol_ratio"] = out["volume"] / out["vol_ma"].replace(0.0, np.nan)

    sw = swing_levels(out)
    for c in sw.columns:
        out[c] = sw[c]

    out["ret_pct"] = close.pct_change() * 100.0
    return out
