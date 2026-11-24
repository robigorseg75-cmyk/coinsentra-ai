#!/usr/bin/env python3
"""
Bybit USDT-perpetual bot v4  (LONG + SHORT, 8x isolated by default)

Two strategies, picked automatically by market regime:
  trend   : 15m trend (EMA20/50 + ADX>18) + 5m trigger (EMA9/21 cross or RSI-50 pullback)
  revert  : sideways market (15m ADX<20, flat EMAs) -> fade a Bollinger-band extreme after a bounce candle
Each strategy has its own self-learning model (logistic regression):
  - bootstrapped from ~14 days of history, 3-fold walk-forward self-test on unseen data
  - updated after every closed trade, fully retrained every 24h
Trade management:
  - volatility-based stop, take profit, partial profit (50%) at half the target, stop moves to break-even,
    trailing stop, time stop
Risk:
  - risk-based sizing, max positions, max same-direction positions (crypto moves together),
    cooldown after a loss, pause after 3 losses in a row, daily loss halt, max-drawdown kill switch
  - in live mode a strategy only trades if its own self-test shows a positive edge
MODE=paper (default) = fake money, real prices.  MODE=live = real orders.
Settings are environment variables (see README.md). Never put API keys in this file.
"""
import os, sys, json, time, logging, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
log = logging.getLogger("bot")


def env(k, d):
    return os.getenv(k, d)


MODE = env("MODE", "paper").lower()
LIVE = MODE == "live"
TESTNET = env("TESTNET", "false").lower() == "true"
SYMBOLS = [s.strip() for s in env(
    "SYMBOLS", "BTC/USDT:USDT,ETH/USDT:USDT,SOL/USDT:USDT,XRP/USDT:USDT,DOGE/USDT:USDT").split(",") if s.strip()]
LEV = int(env("LEVERAGE", "8"))
SL_MAX = float(env("SL_PCT", "0.015"))
TP_PCT = float(env("TP_PCT", "0.02"))
RR = TP_PCT / SL_MAX
RR_REV = float(env("RR_REVERT", "1.0"))
PARTIAL_AT = float(env("PARTIAL_AT", "0.5"))       # take half off at 50% of the TP distance
TRAIL_START = float(env("TRAIL_START", "0.010"))
TRAIL_GAP = float(env("TRAIL_GAP", "0.006"))
TIME_STOP_H = float(env("TIME_STOP_HOURS", "4"))
RISK = float(env("RISK_PER_TRADE", "0.02"))
MAX_POS = int(env("MAX_POSITIONS", "3"))
MAX_SAME_DIR = int(env("MAX_SAME_DIRECTION", "2"))
DAILY_LOSS = float(env("DAILY_LOSS_LIMIT", "0.08"))
MAX_DD = float(env("MAX_DRAWDOWN", "0.30"))
COOLDOWN_MIN = int(env("COOLDOWN_MIN", "30"))
STREAK_PAUSE_H = float(env("STREAK_PAUSE_HOURS", "2"))
START_EQUITY = float(env("START_EQUITY", "50"))
FEE = float(env("FEE", "0.00055"))
SLIP = float(env("SLIPPAGE", "0.0002"))
COST = 2 * (FEE + SLIP)
BE = 0.0012                                        # break-even stop offset (covers fees)
HIST_DAYS = int(env("HIST_DAYS", "14"))
RETRAIN_H = float(env("RETRAIN_HOURS", "24"))
REQUIRE_EDGE = env("REQUIRE_EDGE", "true").lower() == "true"
LOOP_SEC = int(env("LOOP_SECONDS", "20"))
STATE_FILE = env("STATE_FILE", "state.json")
STRATS = ["trend", "revert"]
NFEAT = 10


def notify(msg):
    tok, chat = env("TELEGRAM_TOKEN", ""), env("TELEGRAM_CHAT_ID", "")
    if not tok or not chat:
        return

    def _send():
        try:
            import urllib.request, urllib.parse
            data = urllib.parse.urlencode({"chat_id": chat, "text": msg}).encode()
            urllib.request.urlopen(urllib.request.Request(
                f"https://api.telegram.org/bot{tok}/sendMessage", data=data), timeout=6)
        except Exception as e:
            log.warning("telegram failed: %s", e)
    threading.Thread(target=_send, daemon=True).start()


# ----------------------------------------------------------------- indicators
def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def rsi(c, n=14):
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def atr(df, n=14):
    pc = df.close.shift()
    tr = pd.concat([df.high - df.low, (df.high - pc).abs(), (df.low - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def adx(df, n=14):
    up, dn = df.high.diff(), -df.low.diff()
    pdm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=df.index)
    mdm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=df.index)
    a = atr(df, n)
    pdi = 100 * pdm.ewm(alpha=1 / n, adjust=False).mean() / a
    mdi = 100 * mdm.ewm(alpha=1 / n, adjust=False).mean() / a
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False).mean()


# ----------------------------------------------------------------- data
def to_df(raw):
    df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
    df = df.drop_duplicates("ts").sort_values("ts")
    df["ts"] = pd.to_datetime(df.ts, unit="ms")
    return df.reset_index(drop=True)


def closed(df, minutes):
    now = pd.Timestamp.now("UTC").tz_localize(None)
    return df[df.ts + pd.Timedelta(minutes=minutes) <= now].reset_index(drop=True)


def fetch_hist(ex, sym, tf, days):
    ms = {"5m": 300_000, "15m": 900_000}[tf]
    since = int(time.time() * 1000) - days * 86_400_000
    out = []
    while True:
        chunk = ex.fetch_ohlcv(sym, tf, since=since, limit=1000)
        if not chunk:
            break
        out += chunk
        nxt = chunk[-1][0] + ms
        if len(chunk) < 1000 or nxt <= since:
            break
        since = nxt
    return to_df(out)


def build(d5, d15):
    """d5 / d15 hold CLOSED candles only. 15m values only become visible after that candle closed."""
    d = d5.copy()
    d["ef"], d["es"] = ema(d.close, 9), ema(d.close, 21)
    d["rsi"], d["atr"], d["adx"] = rsi(d.close), atr(d), adx(d)
    d["atrp"] = d.atr / d.close
    d["vr"] = d.volume / d.volume.rolling(20).mean()
    d["sma"], d["sd"] = d.close.rolling(20).mean(), d.close.rolling(20).std()
    d["bbpos"] = (d.close - d.sma) / (2 * d.sd)            # +-1 = on the 2-sigma band
    d["ret3"] = d.close.pct_change(3) / d.atrp
    d["close_ts"] = d.ts + pd.Timedelta(minutes=5)
    h = d15.copy()
    h["h_ef"], h["h_es"] = ema(h.close, 20), ema(h.close, 50)
    h["h_adx"], h["h_atr"] = adx(h), atr(h)
    h["avail"] = h.ts + pd.Timedelta(minutes=15)
    h = h[["avail", "h_ef", "h_es", "h_adx", "h_atr"]]
    d = pd.merge_asof(d.sort_values("close_ts"), h.sort_values("avail"),
                      left_on="close_ts", right_on="avail", direction="backward")
    d["trend"] = np.where((d.h_ef > d.h_es) & (d.h_adx > 18), 1,
                          np.where((d.h_ef < d.h_es) & (d.h_adx > 18), -1, 0))
    d["htf_gap"] = (d.h_ef - d.h_es) / d.h_atr
    # --- trend strategy
    cross_up = (d.ef > d.es) & (d.ef.shift() <= d.es.shift())
    cross_dn = (d.ef < d.es) & (d.ef.shift() >= d.es.shift())
    pull_up = (d.rsi > 50) & (d.rsi.shift() <= 50) & (d.ef > d.es)
    pull_dn = (d.rsi < 50) & (d.rsi.shift() >= 50) & (d.ef < d.es)
    sig_t = np.where((cross_up | pull_up) & (d.trend == 1), 1,
                     np.where((cross_dn | pull_dn) & (d.trend == -1), -1, 0))
    # --- mean-reversion strategy (only in a sideways regime)
    rng = (d.h_adx < 20) & (d.htf_gap.abs() < 1.0)
    lo = (d.bbpos.shift() <= -1.0) & (d.close > d.close.shift()) & (d.rsi < 40)
    hi = (d.bbpos.shift() >= 1.0) & (d.close < d.close.shift()) & (d.rsi > 60)
    sig_r = np.where(rng & lo, 1, np.where(rng & hi, -1, 0))
    d["sig"] = np.where(sig_t != 0, sig_t, sig_r)
    d["strat"] = np.where(sig_t != 0, 0, 1)
    ok = (d.vr > 0.8) & (d.atrp > 0.0008) & (d.atrp < 0.02)
    d.loc[~ok, "sig"] = 0
    return d


def features(row, side):
    h = row.ts.hour + row.ts.minute / 60
    return [float(np.clip(side * row.htf_gap, -5, 5) / 5),
            float(np.clip(side * (row.ef - row.es) / row.atr, -3, 3) / 3),
            float(side * (row.rsi - 50) / 50),
            float(row.adx / 50),
            float(min(row.atrp * 50, 2)),
            float(min(row.vr, 3) / 3),
            float(np.sin(2 * np.pi * h / 24)),
            float(np.cos(2 * np.pi * h / 24)),
            float(side * np.clip(row.bbpos, -2, 2) / 2),
            float(side * np.clip(row.ret3, -5, 5) / 5)]


def stops_for(atrp, strat):
    if strat == "revert":
        sl = min(SL_MAX, max(0.005, 1.2 * atrp))
        return sl, sl * RR_REV
    sl = min(SL_MAX, max(0.006, 1.5 * atrp))
    return sl, sl * RR


def simulate(H, L, C, i, side, sl, tp, horizon=48):
    """Mirror of live management: stop, take-profit, half off at PARTIAL_AT*tp then break-even stop."""
    if i + horizon >= len(C):
        return None
    e, banked, stage, half = C[i], 0.0, 0, tp * PARTIAL_AT
    for j in range(i + 1, i + horizon + 1):
        lvl = e * (1 - side * sl) if stage == 0 else e * (1 + side * BE)
        if side == 1:
            hit_sl, hit_h, hit_tp = L[j] <= lvl, H[j] >= e * (1 + half), H[j] >= e * (1 + tp)
        else:
            hit_sl, hit_h, hit_tp = H[j] >= lvl, L[j] <= e * (1 - half), L[j] <= e * (1 - tp)
        if stage == 0:
            if hit_sl:                       # same-candle double hit counts as a loss (pessimistic)
                return -sl - COST, 0
            if hit_tp:
                return tp - COST, 1
            if hit_h:
                banked, stage = 0.5 * half, 1
        else:
            if hit_sl:
                r = banked + 0.5 * BE - COST
                return r, int(r > 0)
            if hit_tp:
                return banked + 0.5 * tp - COST, 1
    ret = (C[i + horizon] - e) / e * side
    r = banked + (0.5 if stage else 1.0) * ret - COST
    return r, int(r > 0)


# ----------------------------------------------------------------- model
class Model:
    def __init__(self):
        self.w, self.b = np.zeros(NFEAT), 0.0
        self.active, self.thr, self.edge_ok, self.n_live = False, 0.5, False, 0

    def p(self, X):
        return 1 / (1 + np.exp(-(np.asarray(X, float) @ self.w + self.b)))

    def fit(self, X, y, epochs=300, lr=0.2, l2=0.02):
        X, y = np.asarray(X, float), np.asarray(y, float)
        if len(y) < 10:
            return
        for _ in range(epochs):
            g = self.p(X) - y
            self.w -= lr * (X.T @ g / len(y) + l2 * self.w)
            self.b -= lr * g.mean()

    def update(self, x, y, lr=0.03):
        x = np.asarray(x, float)
        g = float(self.p(x)) - y
        self.w -= lr * (g * x + 0.01 * self.w)
        self.b -= lr * g
        self.n_live += 1

    def dump(self):
        return dict(w=self.w.tolist(), b=self.b, active=self.active, thr=self.thr,
                    edge_ok=self.edge_ok, n_live=self.n_live)

    def load(self, d):
        self.w, self.b = np.array(d["w"]), d["b"]
        self.active, self.thr, self.edge_ok, self.n_live = d["active"], d["thr"], d["edge_ok"], d["n_live"]


# ----------------------------------------------------------------- bot
class Bot:
    def __init__(self, ex):
        self.ex = ex
        self.models = {s: Model() for s in STRATS}
        self.st = dict(wallet=START_EQUITY, pos={}, journal=[], peak=START_EQUITY, day="", day_start=START_EQUITY,
                       bucket={}, cool={}, kill=False, n=0, wins=0, pnl=0.0, streak=0, pause_until=0.0,
                       last_train=0.0, last_report=0.0, by={s: [0, 0.0] for s in STRATS})
        self.halted = ""
        self.load()

    # ---- persistence
    def load(self):
        try:
            with open(STATE_FILE) as f:
                s = json.load(f)
            for k, m in s.pop("models").items():
                if k in self.models:
                    self.models[k].load(m)
            self.st.update(s)
            log.info("state loaded from %s", STATE_FILE)
        except Exception:
            log.info("fresh state")

    def save(self):
        try:
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump({**self.st, "models": {k: m.dump() for k, m in self.models.items()}}, f)
            os.replace(tmp, STATE_FILE)
        except Exception as e:
            log.warning("save failed: %s", e)

    @property
    def pos(self):
        return self.st["pos"]

    # ---- learning
    def retrain(self):
        hist = {s: [] for s in STRATS}
        for sym in SYMBOLS:
            try:
                d5 = closed(fetch_hist(self.ex, sym, "5m", HIST_DAYS), 5)
                d15 = closed(fetch_hist(self.ex, sym, "15m", HIST_DAYS + 4), 15)
                d = build(d5, d15).dropna(
                    subset=["atr", "h_atr", "rsi", "vr", "htf_gap", "bbpos", "ret3"]).reset_index(drop=True)
            except Exception as e:
                log.warning("history %s failed: %s", sym, e)
                continue
            H, L, C = d.high.values, d.low.values, d.close.values
            for i in np.where(d.sig.values != 0)[0]:
                side, strat = int(d.sig.values[i]), STRATS[int(d.strat.values[i])]
                sl, tp = stops_for(float(d.atrp.values[i]), strat)
                res = simulate(H, L, C, i, side, sl, tp)
                if res:
                    hist[strat].append((d.close_ts.values[i], features(d.iloc[i], side), res[0], res[1]))
        for s in STRATS:
            self.fit_strat(s, hist[s])
        self.st["last_train"] = time.time()

    def fit_strat(self, name, rows):
        rows.sort(key=lambda z: z[0])
        n, m = len(rows), self.models[name]
        m.active, m.edge_ok = False, False
        log.info("[%s] self-test on %d historical signals", name, n)
        if n >= 60:
            X = np.array([r[1] for r in rows])
            r_ = np.array([r[2] for r in rows])
            y = np.array([r[3] for r in rows])
            chunks = np.array_split(np.arange(n), 4)
            ps, rs, ys = [], [], []
            for k in (1, 2, 3):                                  # walk-forward: always test on the unseen future
                tr, te = np.concatenate(chunks[:k]), chunks[k]
                tm = Model()
                tm.fit(X[tr], y[tr])
                ps.append(tm.p(X[te]))
                rs.append(r_[te])
                ys.append(y[te])
            p, r, yy = np.concatenate(ps), np.concatenate(rs), np.concatenate(ys)
            base = float(r.mean())
            log.info("[%s] unseen data: no-model avg trade %+.3f%% win %.0f%% (%d trades)",
                     name, base * 100, yy.mean() * 100, len(r))
            best = None
            for thr in (0.50, 0.52, 0.54, 0.56, 0.58, 0.60, 0.65):
                sel = p >= thr
                if sel.sum() >= 15 and (best is None or r[sel].mean() > best[1]):
                    best = (thr, float(r[sel].mean()), int(sel.sum()), float(yy[sel].mean()))
            if best:
                log.info("[%s] model thr %.2f -> %d trades, avg %+.3f%%, win %.0f%%",
                         name, best[0], best[2], best[1] * 100, best[3] * 100)
                m.thr = best[0]
                m.active = best[1] > 0 and best[1] > base
            m.edge_ok = base > 0 or bool(best and best[1] > 0)
        X = [r[1] for r in rows] + [j["x"] for j in self.st["journal"] if j.get("s") == name]
        y = [r[3] for r in rows] + [j["y"] for j in self.st["journal"] if j.get("s") == name]
        m.w, m.b = np.zeros(NFEAT), 0.0
        m.fit(X, y)
        log.info("[%s] model active=%s thr=%.2f edge_ok=%s", name, m.active, m.thr, m.edge_ok)

    # ---- account
    def account(self, prices):
        used = sum(p["qty"] * p["entry"] for p in self.pos.values()) / LEV
        if LIVE:
            b = self.ex.fetch_balance()
            eq = float(b["total"].get("USDT") or 0)
            free = b["free"].get("USDT")
            return eq, float(free if free is not None else eq - used)
        unreal = sum((prices.get(s, p["entry"]) - p["entry"]) * p["qty"] * p["side"] for s, p in self.pos.items())
        eq = self.st["wallet"] + unreal
        return eq, eq - used

    def min_amount(self, sym):
        m = self.ex.market(sym)
        return (m.get("limits", {}).get("amount", {}) or {}).get("min") or 0

    # ---- orders
    def open_pos(self, sym, side, qty, px, sl_pct, tp_pct, x, strat):
        fill = px * (1 + side * SLIP)
        sl, tp = fill * (1 - side * sl_pct), fill * (1 + side * tp_pct)
        if LIVE:
            for fn in (lambda: self.ex.set_margin_mode("isolated", sym), lambda: self.ex.set_leverage(LEV, sym)):
                try:
                    fn()
                except Exception:
                    pass
            o = self.ex.create_order(sym, "market", "buy" if side == 1 else "sell", qty, None,
                                     {"stopLoss": float(self.ex.price_to_precision(sym, sl)),
                                      "takeProfit": float(self.ex.price_to_precision(sym, tp))})
            fill = float(o.get("average") or o.get("price") or px)
        else:
            self.st["wallet"] -= qty * fill * FEE
        self.pos[sym] = dict(side=side, entry=fill, qty=qty, sl=sl, tp=tp, peak=fill, x=x, strat=strat,
                             sl_pct=sl_pct, tp_pct=tp_pct, part=False, realized=0.0, t=time.time())
        msg = "OPEN %s %s [%s] qty=%s @%.6g sl=%.6g tp=%.6g" % (
            "LONG" if side == 1 else "SHORT", sym, strat, qty, fill, sl, tp)
        log.info(msg)
        notify(("LIVE " if LIVE else "PAPER ") + msg)

    def set_sl(self, sym, sl):
        try:
            self.ex.private_post_v5_position_trading_stop({
                "category": "linear", "symbol": self.ex.market(sym)["id"], "tpslMode": "Full", "positionIdx": 0,
                "stopLoss": str(self.ex.price_to_precision(sym, sl))})
        except Exception as e:
            log.warning("stop update failed %s: %s", sym, e)

    def partial(self, sym, px):
        p, s = self.pos[sym], self.pos[sym]["side"]
        p["part"] = True
        q = float(self.ex.amount_to_precision(sym, p["qty"] / 2))
        mn = self.min_amount(sym)
        if q > 0 and q >= mn and p["qty"] - q >= mn and q * px >= 5.5:
            if LIVE:
                self.ex.create_order(sym, "market", "sell" if s == 1 else "buy", q, None, {"reduceOnly": True})
            fill = px * (1 - s * SLIP)
            pnl = (fill - p["entry"]) * q * s - q * fill * FEE
            if not LIVE:
                self.st["wallet"] += pnl
            p["realized"] += pnl
            p["qty"] = round(p["qty"] - q, 10)
            log.info("PARTIAL %s %s: took %s off, %+.3f banked", "LONG" if s == 1 else "SHORT", sym, q, pnl)
        be = p["entry"] * (1 + s * BE)
        if (s == 1 and be > p["sl"]) or (s == -1 and be < p["sl"]):
            p["sl"] = be
            if LIVE:
                self.set_sl(sym, be)

    def close_pos(self, sym, px, reason):
        p, s = self.pos[sym], self.pos[sym]["side"]
        if LIVE:
            self.ex.create_order(sym, "market", "sell" if s == 1 else "buy", p["qty"], None, {"reduceOnly": True})
            time.sleep(1.5)
            pnl = self.live_pnl(sym, (px - p["entry"]) * p["qty"] * s)
        else:
            fill = px * (1 - s * SLIP)
            pnl = (fill - p["entry"]) * p["qty"] * s - p["qty"] * fill * FEE
            self.st["wallet"] += pnl
        self.record(sym, pnl, reason)

    def live_pnl(self, sym, fallback):
        try:
            r = self.ex.private_get_v5_position_closed_pnl(
                {"category": "linear", "symbol": self.ex.market(sym)["id"], "limit": 1})
            return float(r["result"]["list"][0]["closedPnl"])
        except Exception:
            return fallback

    def record(self, sym, pnl_final, reason):
        p = self.pos.pop(sym)
        pnl = pnl_final + p.get("realized", 0.0)
        win = int(pnl > 0)
        st = self.st
        st["n"] += 1
        st["wins"] += win
        st["pnl"] += pnl
        strat = p.get("strat", "trend")
        st["by"].setdefault(strat, [0, 0.0])
        st["by"][strat][0] += 1
        st["by"][strat][1] += pnl
        if p.get("x"):
            self.models[strat].update(p["x"], win)
            st["journal"] = (st["journal"] + [dict(x=p["x"], y=win, s=strat)])[-600:]
        if pnl <= 0:
            st["cool"][sym] = time.time() + COOLDOWN_MIN * 60
            st["streak"] += 1
            if st["streak"] >= 3:
                st["pause_until"] = time.time() + STREAK_PAUSE_H * 3600
                st["streak"] = 0
                log.warning("3 losses in a row: no new trades for %.1fh", STREAK_PAUSE_H)
                notify("3 losses in a row: pausing new trades for %.1fh" % STREAK_PAUSE_H)
        else:
            st["streak"] = 0
        msg = "CLOSE %s %s [%s] pnl=%+.3f (%s) | trades=%d win=%.0f%% total=%+.2f" % (
            "LONG" if p["side"] == 1 else "SHORT", sym, strat, pnl, reason, st["n"],
            100 * st["wins"] / max(1, st["n"]), st["pnl"])
        log.info(msg)
        notify(("LIVE " if LIVE else "PAPER ") + msg)

    # ---- position management
    def live_sync(self, prices):
        ps = self.ex.fetch_positions(SYMBOLS)
        now_open = {p["symbol"]: p for p in ps if float(p.get("contracts") or 0) > 0}
        for sym in list(self.pos):
            if sym not in now_open:                      # closed by the exchange stop / take-profit
                p = self.pos[sym]
                fb = (prices.get(sym, p["entry"]) - p["entry"]) * p["qty"] * p["side"]
                self.record(sym, self.live_pnl(sym, fb), "exchange SL/TP")
        for sym, p in now_open.items():
            if sym not in self.pos:
                side = 1 if p["side"] == "long" else -1
                e = float(p["entryPrice"])
                log.warning("adopting untracked position %s", sym)
                self.pos[sym] = dict(side=side, entry=e, qty=float(p["contracts"]), sl=e * (1 - side * SL_MAX),
                                     tp=e * (1 + side * TP_PCT), peak=e, x=None, strat="trend", sl_pct=SL_MAX,
                                     tp_pct=TP_PCT, part=True, realized=0.0, t=time.time())
                self.set_sl(sym, self.pos[sym]["sl"])

    def manage(self, prices):
        if LIVE:
            self.live_sync(prices)
        now = time.time()
        for sym in list(self.pos):
            p, px = self.pos.get(sym), prices.get(sym)
            if not p or not px:
                continue
            s = p["side"]
            now_fav = (px - p["entry"]) / p["entry"] * s
            p["peak"] = max(p["peak"], px) if s == 1 else min(p["peak"], px)
            if not p["part"] and now_fav >= p["tp_pct"] * PARTIAL_AT:
                self.partial(sym, px)
            if (p["peak"] - p["entry"]) / p["entry"] * s >= TRAIL_START:
                new = p["peak"] * (1 - s * TRAIL_GAP)
                if (s == 1 and new > p["sl"]) or (s == -1 and new < p["sl"]):
                    p["sl"] = new
                    if LIVE:
                        self.set_sl(sym, new)
            if not LIVE:
                hit_sl = px <= p["sl"] if s == 1 else px >= p["sl"]
                hit_tp = px >= p["tp"] if s == 1 else px <= p["tp"]
                if hit_sl or hit_tp:
                    self.close_pos(sym, px, "TP" if hit_tp else "SL/trail")
                    continue
            if now - p["t"] > TIME_STOP_H * 3600 and now_fav < 0.003:
                self.close_pos(sym, px, "time stop")

    # ---- entries
    def scan(self, sym, prices):
        b = int(time.time() // 300)
        if self.st["bucket"].get(sym) == b or time.time() % 300 < 4:
            return
        d5 = closed(to_df(self.ex.fetch_ohlcv(sym, "5m", limit=300)), 5)
        d15 = closed(to_df(self.ex.fetch_ohlcv(sym, "15m", limit=150)), 15)
        self.st["bucket"][sym] = b
        d = build(d5, d15)
        if d.empty:
            return
        row = d.iloc[-1]
        if row[["atr", "h_atr", "rsi", "vr", "htf_gap", "bbpos", "ret3"]].isna().any():
            return
        sig = int(row.sig)
        px = prices.get(sym) or float(row.close)
        if sym in self.pos and sig == -self.pos[sym]["side"]:
            self.close_pos(sym, px, "signal flip")
        if sig == 0 or sym in self.pos:
            return
        self.try_open(sym, sig, STRATS[int(row.strat)], row, px)

    def try_open(self, sym, side, strat, row, px):
        if self.halted or self.st["kill"] or self.st["pause_until"] > time.time():
            return
        if len(self.pos) >= MAX_POS or self.st["cool"].get(sym, 0) > time.time():
            return
        if sum(1 for p in self.pos.values() if p["side"] == side) >= MAX_SAME_DIR:
            log.info("skip %s: already %d positions in that direction", sym, MAX_SAME_DIR)
            return
        m = self.models[strat]
        if REQUIRE_EDGE and LIVE and not m.edge_ok:
            log.info("skip %s [%s]: self-test shows no edge for this strategy", sym, strat)
            return
        x = features(row, side)
        p = float(m.p(x))
        if m.active and p < m.thr:
            log.info("skip %s %s [%s]: model p=%.2f < %.2f", sym, "L" if side == 1 else "S", strat, p, m.thr)
            return
        sl, tp = stops_for(float(row.atrp), strat)
        eq, avail = self.account({sym: px})
        scale = float(np.clip(0.5 + p, 0.75, 1.25)) if m.active else 1.0
        notional = min(eq * RISK * scale / sl, eq * LEV / MAX_POS, avail * LEV * 0.95)
        qty = float(self.ex.amount_to_precision(sym, notional / px))
        mn = self.min_amount(sym)
        if qty <= 0 or qty < mn or qty * px < 5.5:
            log.info("skip %s: size %.6g below exchange minimum %.6g (account too small for this coin)", sym, qty, mn)
            return
        self.open_pos(sym, side, qty, px, sl, tp, x, strat)

    # ---- main loop
    def tick(self):
        t = self.ex.fetch_tickers(SYMBOLS)
        prices = {s: float(v["last"]) for s, v in t.items() if v.get("last")}
        self.manage(prices)
        eq, _ = self.account(prices)
        day = time.strftime("%Y-%m-%d", time.gmtime())
        if day != self.st["day"]:
            if self.st["day"]:
                notify("Daily summary: equity %.2f | trades %d | total pnl %+.2f" % (eq, self.st["n"], self.st["pnl"]))
            self.st["day"], self.st["day_start"] = day, eq
        self.st["peak"] = max(self.st["peak"], eq)
        self.halted = ""
        if eq < self.st["day_start"] * (1 - DAILY_LOSS):
            self.halted = "daily loss limit"
        if eq < self.st["peak"] * (1 - MAX_DD) and not self.st["kill"]:
            self.st["kill"] = True
            msg = "MAX DRAWDOWN reached (equity %.2f, peak %.2f): no new trades. Open positions keep their stops." % (
                eq, self.st["peak"])
            log.error(msg)
            notify(msg)
        for sym in SYMBOLS:
            try:
                self.scan(sym, prices)
            except Exception as e:
                log.warning("scan %s failed: %s", sym, e)
        if time.time() - self.st["last_train"] > RETRAIN_H * 3600:
            self.retrain()
        if time.time() - self.st["last_report"] > 3600:
            self.st["last_report"] = time.time()
            log.info("STATUS %s equity=%.2f open=%s trades=%d by_strategy=%s halted=%s", MODE.upper(), eq,
                     {s: ("L" if p["side"] == 1 else "S") for s, p in self.pos.items()}, self.st["n"],
                     {k: (v[0], round(v[1], 2)) for k, v in self.st["by"].items()},
                     self.halted or ("KILL" if self.st["kill"] else "no"))
        self.save()

    def run(self, max_iters=None):
        if time.time() - self.st["last_train"] > RETRAIN_H * 3600 or not any(m.w.any() for m in self.models.values()):
            self.retrain()
        it = 0
        while True:
            try:
                self.tick()
            except Exception:
                log.exception("tick failed")
            it += 1
            if max_iters and it >= max_iters:
                return
            time.sleep(LOOP_SEC)


# ----------------------------------------------------------------- startup
def health_server():
    port = int(env("PORT", "0"))
    if not port:
        return

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass

    threading.Thread(target=HTTPServer(("0.0.0.0", port), H).serve_forever, daemon=True).start()


def make_exchange():
    import ccxt
    ex = ccxt.bybit({"apiKey": env("BYBIT_API_KEY", ""), "secret": env("BYBIT_API_SECRET", ""),
                     "enableRateLimit": True, "options": {"defaultType": "swap"}})
    if TESTNET:
        ex.set_sandbox_mode(True)
    ex.load_markets()
    return ex


def main():
    if LIVE and not (env("BYBIT_API_KEY", "") and env("BYBIT_API_SECRET", "")):
        sys.exit("MODE=live needs BYBIT_API_KEY and BYBIT_API_SECRET as environment variables")
    if SL_MAX * LEV > 0.6:
        sys.exit("SL_PCT x LEVERAGE is too close to liquidation; lower one of them")
    health_server()
    log.info("starting in %s mode%s | %s | leverage %dx", MODE.upper(), " (testnet)" if TESTNET else "",
             ",".join(SYMBOLS), LEV)
    notify("Bot started in %s mode" % MODE.upper())
    Bot(make_exchange()).run()


if __name__ == "__main__":
    main()
