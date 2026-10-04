#!/usr/bin/env python3
# =============================================================================
# Decompiled by Telegram @Mohamed_TraderTBT
# =============================================================================
"""
Quotex Future Signal Generator — CLI PKT Edition v9 (Pro)
==========================================================
Same fetch logic as v8 (pyquotex) + pattern analysis, but with:
 - Professional colored CLI (pure ANSI, no extra deps)
 - Extra pattern: Engulfing
 - REAL out-of-sample backtest: patterns are learned on a "train"
   slice of history and validated on a separate "test" slice, so the
   accuracy % you see is not just curve-fit to the same data.
 - Optional AI confirmation layer (KiloCode / OpenAI-compatible gateway):
   after statistical backtesting, an LLM reviews each surviving signal
   as a second, independent opinion and can veto/downgrade weak ones.
"""

import os
import re
import sys
import time
import json
import asyncio
import csv
import logging
import multiprocessing as mp
import queue
import threading
from datetime import datetime, timedelta
from collections import defaultdict
import statistics
import warnings

warnings.filterwarnings("ignore", category=DeprecationWarning)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

sys.path.insert(0, ".")

from pyquotex.stable_api import Quotex

logging.basicConfig(level=logging.WARNING)

for _noisy in ("websockets", "websockets.client", "websockets.server", "asyncio"):
    logging.getLogger(_noisy).setLevel(logging.CRITICAL)


class _TimeoutCounter(logging.Handler):
    """Silently counts 'batch fetch timeout' / deprecation warnings instead
    of printing each one — we show a single summary line after the fetch."""

    def __init__(self):
        super().__init__()
        self.count = 0

    def emit(self, record):
        self.count += 1


def _silence_logger(name):
    """Force a logger to be quiet even if the library already attached its
    own StreamHandler directly to it (propagate=False alone won't help
    in that case, since the handler fires before propagation matters)."""
    lg = logging.getLogger(name)
    lg.handlers.clear()
    lg.propagate = False
    lg.setLevel(logging.WARNING)
    return lg


_hist_logger = _silence_logger("pyquotex._api.history")

import contextlib

_NOISY_STDOUT_PATTERNS = (
    "[WS DEBUG]",
    "[DEBUG] Websocket authorization",
    "Received while not authenticated",
    "get_candles_deep is deprecated",
)


class _FilteredStdout:
    """Passes everything through to the real stdout EXCEPT lines matching
    known noisy debug patterns. Unlike a full suppress, this never hides
    things like PIN/verification-code prompts, since those don't match
    the filtered patterns and get written through immediately on flush."""

    def __init__(self, real_stdout):
        self.real = real_stdout
        self.buffer = ""

    def write(self, s):
        self.buffer += s
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            if not any(p in line for p in _NOISY_STDOUT_PATTERNS):
                self.real.write(line + "\n")

    def flush(self):
        if self.buffer:
            if not any(p in self.buffer for p in _NOISY_STDOUT_PATTERNS):
                self.real.write(self.buffer)
            self.buffer = ""
        self.real.flush()

    def isatty(self):
        return self.real.isatty()


@contextlib.contextmanager
def suppress_stdout():
    """Filters out known noisy debug print() lines during connect(), while
    still passing through anything else (including PIN/verification-code
    prompts) so the program never silently waits on hidden input."""
    old_stdout = sys.stdout
    sys.stdout = _FilteredStdout(old_stdout)
    try:
        yield
    finally:
        sys.stdout.flush()
        sys.stdout = old_stdout


EMAIL = os.getenv("QUOTEX_EMAIL", "")
PASSWORD = os.getenv("QUOTEX_PASSWORD", "")

if not EMAIL or not PASSWORD:
    print("ERROR: Set QUOTEX_EMAIL and QUOTEX_PASSWORD in .env or export them")
    sys.exit(1)

OPENROUTER_API_KEY = "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"
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "nvidia/nemotron-3-ultra-550b-a55b:free")
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1/chat/completions"

G_CLIENT = None


# ---------------------------------------------------------------------------
# Colored-CLI helpers (pure ANSI, no external dependencies)
# Decompiled by Telegram @Mohamed_TraderTBT
# ---------------------------------------------------------------------------
class C:
    RESET = "\x1b[0m"
    BOLD = "\x1b[1m"
    DIM = "\x1b[2m"
    ITALIC = "\x1b[3m"
    GREEN = "\x1b[32m"
    RED = "\x1b[31m"
    YELLOW = "\x1b[33m"
    CYAN = "\x1b[36m"
    MAGENTA = "\x1b[35m"
    BLUE = "\x1b[34m"
    WHITE = "\x1b[97m"
    GREY = "\x1b[90m"
    BG_GREEN = "\x1b[42m"
    BG_RED = "\x1b[41m"


def c(text, *codes):
    return "".join(codes) + str(text) + C.RESET


def hr(char="─", width=78, color=C.GREY):
    print(c(char * width, color))


_ANSI_RE = re.compile("\\033\\[[0-9;]*m")


def _visible_len(s):
    return len(_ANSI_RE.sub("", s))


def _pad_center(text, width):
    """Centers plain text (no ANSI codes) within `width` columns."""
    vis = _visible_len(text)
    if vis >= width:
        return text[:width]
    pad = width - vis
    left = pad // 2
    return " " * left + text + " " * (pad - left)


def box_top(width=78, color=C.CYAN, corner_l="╔", corner_r="╗", fill="═"):
    print(c(corner_l + fill * (width - 2) + corner_r, color))


def box_bottom(width=78, color=C.CYAN, corner_l="╚", corner_r="╝", fill="═"):
    print(c(corner_l + fill * (width - 2) + corner_r, color))


def box_divider(width=78, color=C.CYAN):
    print(c("╠" + "─" * (width - 2) + "╣", color))


def box_line(text="", width=78, border_color=C.CYAN, text_color=None,
             bold=False, center=True, side="║"):
    text_color = text_color or border_color
    inner = width - 4
    vis = _visible_len(text)
    if vis > inner:
        plain = text
        pad = 0
    else:
        plain = text
        pad = inner - vis
    body = _pad_center(plain, inner) if center else plain + " " * pad
    if bold and "\x1b[" not in plain:
        styled = c(body, C.BOLD, text_color)
    elif "\x1b[" in plain:
        styled = body
    else:
        styled = c(body, text_color)
    print(c(f"{side} ", border_color) + styled + c(f" {side}", border_color))


def title(text, color=C.CYAN):
    hr("═", 78, color)
    print(c(f" {text}", C.BOLD, color))
    hr("═", 78, color)


def section(text):
    print()
    print(c("▸ ", C.BOLD, C.BLUE) + c(text, C.BOLD, C.WHITE))
    hr("─", 78, C.GREY)


def ok(text):
    print(c(f"  ✔ {text}", C.GREEN))


def warn(text):
    print(c(f"  ⚠ {text}", C.YELLOW))


def err(text):
    print(c(f"  ✘ {text}", C.RED))


def info(text):
    print(c(f"  ℹ {text}", C.CYAN))


class Spinner:
    """Lightweight animated spinner for waits that don't have a natural
    progress percentage (login, connecting) — runs in a background
    thread so it animates while the awaited call is in flight."""

    FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    def __init__(self, message, color=C.CYAN):
        self.message = message
        self.color = color
        self._stop = threading.Event()
        self._thread = None

    def _spin(self):
        i = 0
        while not self._stop.is_set():
            frame = self.FRAMES[i % len(self.FRAMES)]
            print(f"\r  {c(frame, C.BOLD, self.color)} {self.message}", end="", flush=True)
            i += 1
            time.sleep(0.08)

    def __enter__(self):
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self._stop.set()
        self._thread.join()
        clear_len = len(self.message) + 6
        print("\r" + " " * clear_len + "\r", end="", flush=True)


# ---------------------------------------------------------------------------
# License / expiry gate
# Decompiled by Telegram @Mohamed_TraderTBT
# ---------------------------------------------------------------------------
EXPIRY_DATE = "2026-10-10"
EXPIRY_DAYS = 16
EXPIRY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".qtx_expiry")
_HARD_EXPIRY = datetime.strptime(EXPIRY_DATE, "%Y-%m-%d")


def _get_first_run():
    """Read first-run timestamp from tracking file."""
    if not os.path.exists(EXPIRY_FILE):
        return None
    try:
        with open(EXPIRY_FILE, "r") as f:
            return datetime.strptime(f.read().strip(), "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _set_first_run(now):
    try:
        with open(EXPIRY_FILE, "w") as f:
            f.write(now.strftime("%Y-%m-%d %H:%M:%S"))
    except Exception:
        pass


def check_expiry():
    """Checks expiry using BOTH hardcoded date and file tracking.
    Whichever is STRICTER (earlier) wins — deleting the file does NOT help."""
    now = datetime.now()
    hard_expired = now >= _HARD_EXPIRY
    first_run = _get_first_run()
    if first_run is None:
        _set_first_run(now)
        file_expired = False
    else:
        file_expired = EXPIRY_DAYS - (now - first_run).total_seconds() / 86400 <= 0
    if hard_expired or file_expired:
        print()
        box_top(78, C.RED)
        box_line("ACCESS EXPIRED", 78, C.RED, text_color=C.WHITE, bold=True)
        box_line("Usage limit reached. Contact developer.", 78, C.RED, text_color=C.YELLOW)
        box_bottom(78, C.RED)
        print()
        return False
    info("License active.")
    return True


# ---------------------------------------------------------------------------
# Quotex connection
# Decompiled by Telegram @Mohamed_TraderTBT
# ---------------------------------------------------------------------------
async def ensure_quotex_session(email, password):
    """Connect once in the MAIN process so PIN/2FA prompts appear where
    stdin actually works.  The client is kept alive in G_CLIENT so
    fetch_pair_data can reuse it directly — no child processes, no
    repeated PIN prompts."""
    global G_CLIENT
    info("Pre-authenticating (PIN/2FA prompt appears here if needed)...")
    try:
        G_CLIENT = Quotex(email=email, password=password, lang="en")
        G_CLIENT.debug_ws_enable = False
        with suppress_stdout():
            with Spinner("Connecting to Quotex..."):
                check, msg = await asyncio.wait_for(G_CLIENT.connect(), timeout=120)
        if check:
            ok("Quotex session established — connection kept alive for fetching.")
            return True
        else:
            err(f"Quotex pre-auth failed: {msg}")
            G_CLIENT = None
            return True
    except asyncio.TimeoutError:
        err("Quotex pre-auth timed out (120s). Check your internet or credentials.")
        G_CLIENT = None
        return True
    except Exception as e:
        err(f"Quotex pre-auth error: {type(e).__name__}: {e}")
        G_CLIENT = None
        return True


# ---------------------------------------------------------------------------
# Pattern engine
# Decompiled by Telegram @Mohamed_TraderTBT
# ---------------------------------------------------------------------------
class PatternEngine:
    """
    Learns time-of-day pattern statistics from a set of candles, grouped
    into time buckets (e.g. 15/30/60 minutes) instead of exact minutes.
    Exact-minute buckets (bucket_minutes=1) only recur once per day, so
    sample counts stay tiny (3-4) no matter how much history you fetch —
    wider buckets pool many candles together so accuracy numbers become
    statistically meaningful instead of coin-flip noise.
    """

    def __init__(self, candles, bucket_minutes=1):
        self.candles = candles
        # "Timeless" mode ignores the clock entirely (bucket_minutes <= 0).
        self.timeless = bucket_minutes is not None and bucket_minutes <= 0
        if self.timeless:
            self.bucket_minutes = 1
        else:
            self.bucket_minutes = max(1, bucket_minutes)

    def _time_key(self, timestamp):
        if self.timeless:
            return "ALL"
        if self.bucket_minutes <= 1:
            return datetime.fromtimestamp(timestamp).strftime("%H:%M")
        dt = datetime.fromtimestamp(timestamp)
        total_min = dt.hour * 60 + dt.minute
        bucket_start = total_min // self.bucket_minutes * self.bucket_minutes
        bucket_end = bucket_start + self.bucket_minutes
        sh, sm = divmod(bucket_start, 60)
        eh, em = divmod(bucket_end % 1440, 60)
        return f"{sh:02d}:{sm:02d}-{eh:02d}:{em:02d}"

    def learn(self):
        'Returns dict: pattern_name -> {time_key: {"bull":n,"bear":n,"total":n}}'
        return {
            "Heavy": self._heavy_zones(),
            "Reversal": self._support_resistance(),
            "Momentum": self._trend_momentum(),
            "Engulfing": self._engulfing(),
        }

    def _heavy_zones(self):
        sizes = [abs(c["close"] - c["open"]) for c in self.candles if c.get("open")]
        stats = defaultdict(lambda: {"bull": 0, "bear": 0, "total": 0})
        if not sizes:
            return stats
        avg_size = statistics.mean(sizes)
        for c in self.candles:
            size = abs(c["close"] - c["open"])
            if size >= avg_size * 1.5:
                time_key = self._time_key(c["time"])
                stats[time_key]["total"] += 1
                if c["close"] > c["open"]:
                    stats[time_key]["bull"] += 1
                else:
                    stats[time_key]["bear"] += 1
        return stats

    def _support_resistance(self):
        stats = defaultdict(lambda: {"bull": 0, "bear": 0, "total": 0})
        for i in range(1, len(self.candles) - 1):
            prev, curr, nxt = self.candles[i - 1], self.candles[i], self.candles[i + 1]
            time_key = self._time_key(curr["time"])
            prev_bull = prev["close"] > prev["open"]
            prev_bear = prev["close"] < prev["open"]
            next_bull = nxt["close"] > nxt["open"]
            next_bear = nxt["close"] < nxt["open"]
            if prev_bear and next_bull and curr["close"] >= curr["open"]:
                stats[time_key]["bull"] += 1
                stats[time_key]["total"] += 1
            elif prev_bull and next_bear and curr["close"] <= curr["open"]:
                stats[time_key]["bear"] += 1
                stats[time_key]["total"] += 1
        return stats

    def _trend_momentum(self):
        stats = defaultdict(lambda: {"bull": 0, "bear": 0, "total": 0})
        for i in range(2, len(self.candles)):
            c1, c2, c3 = self.candles[i - 2], self.candles[i - 1], self.candles[i]
            time_key = self._time_key(c3["time"])
            if c1["close"] > c1["open"] and c2["close"] > c2["open"] and c3["close"] > c3["open"]:
                stats[time_key]["bull"] += 1
                stats[time_key]["total"] += 1
            elif c1["close"] < c1["open"] and c2["close"] < c2["open"] and c3["close"] < c3["open"]:
                stats[time_key]["bear"] += 1
                stats[time_key]["total"] += 1
        return stats

    def _engulfing(self):
        "Bullish/bearish engulfing candle pattern by time-of-day."
        stats = defaultdict(lambda: {"bull": 0, "bear": 0, "total": 0})
        for i in range(1, len(self.candles)):
            prev, curr = self.candles[i - 1], self.candles[i]
            time_key = self._time_key(curr["time"])
            prev_body_low = min(prev["open"], prev["close"])
            prev_body_high = max(prev["open"], prev["close"])
            curr_body_low = min(curr["open"], curr["close"])
            curr_body_high = max(curr["open"], curr["close"])
            engulfs = curr_body_low <= prev_body_low and curr_body_high >= prev_body_high
            if not engulfs:
                continue
            if curr["close"] > curr["open"] and prev["close"] < prev["open"]:
                stats[time_key]["bull"] += 1
                stats[time_key]["total"] += 1
            elif curr["close"] < curr["open"] and prev["close"] > prev["open"]:
                stats[time_key]["bear"] += 1
                stats[time_key]["total"] += 1
        return stats


# ---------------------------------------------------------------------------
# Scoring / validation
# Decompiled by Telegram @Mohamed_TraderTBT
# ---------------------------------------------------------------------------
def score_patterns(learned, min_accuracy, min_samples=20, source="TRAIN"):
    "Turn raw learned stats into scored BUY/SELL patterns."
    results = []
    for pattern_name, time_stats in learned.items():
        for time_key, stats in time_stats.items():
            if stats["total"] < min_samples:
                continue
            bull_pct = stats["bull"] / stats["total"] * 100
            bear_pct = stats["bear"] / stats["total"] * 100
            if bull_pct >= min_accuracy:
                results.append({
                    "time": time_key,
                    "direction": "BUY",
                    "confidence": round(bull_pct, 1),
                    "pattern": pattern_name,
                    "samples": stats["total"],
                    "source": source,
                    "reason": f"{stats['bull']}/{stats['total']} bullish",
                })
            elif bear_pct >= min_accuracy:
                results.append({
                    "time": time_key,
                    "direction": "SELL",
                    "confidence": round(bear_pct, 1),
                    "pattern": pattern_name,
                    "samples": stats["total"],
                    "source": source,
                    "reason": f"{stats['bear']}/{stats['total']} bearish",
                })
    return results


def validate_on_test(train_signals, test_learned, min_samples=10):
    """
    For every signal learned on TRAIN data, check how it *actually*
    performed on the TEST (unseen) data at the same time-of-day and
    pattern. This is the honest, out-of-sample accuracy number.
    Signals with too few test samples are marked 'insufficient data'.
    """
    validated = []
    for sig in train_signals:
        test_stats = test_learned.get(sig["pattern"], {}).get(sig["time"])
        if not test_stats or test_stats["total"] < min_samples:
            sig["test_accuracy"] = None
            sig["test_samples"] = test_stats["total"] if test_stats else 0
        else:
            hits = test_stats["bull"] if sig["direction"] == "BUY" else test_stats["bear"]
            sig["test_accuracy"] = round(hits / test_stats["total"] * 100, 1)
            sig["test_samples"] = test_stats["total"]
        validated.append(sig)
    return validated


# ---------------------------------------------------------------------------
# AI confirmation layer (OpenAI-compatible gateway)
# Decompiled by Telegram @Mohamed_TraderTBT
# ---------------------------------------------------------------------------
def get_ai_client():
    """Builds an OpenAI-compatible client pointed at the KiloCode Gateway.
    Returns None (with a warning) if the openai SDK isn't installed or
    no OPENROUTER_API_KEY is configured — the rest of the script keeps working
    without the AI layer in that case."""
    if OpenAI is None:
        warn("`openai` package not installed — AI confirmation layer disabled.")
        warn("Install it with: pip install openai")
        return None
    if not OPENROUTER_API_KEY:
        warn("OPENROUTER_API_KEY not set — AI confirmation layer disabled.")
        warn("Get a key at https://app.kilocode.ai (Dashboard → API Keys) and put it in .env")
        return None
    return OpenAI(api_key=OPENROUTER_API_KEY, base_url=OPENROUTER_BASE_URL)


def _build_ai_prompt(pair, signals, payout_pct):
    breakeven = 100 / (100 + payout_pct) * 100
    lines = [
        "You are a skeptical quantitative reviewer checking candlestick-pattern trading signals for statistical soundness BEFORE any money is risked. These signals were mined from historical OTC candle data and already passed a chronological train/test split (accuracy shown is out-of-sample).",
        f"Instrument: {pair}. Broker payout: {payout_pct}% (breakeven accuracy ≈ {breakeven:.1f}%).",
        "",
        "For EACH signal below, judge reliability using: out-of-sample accuracy vs breakeven, sample size (small samples = noisy, treat <15 with suspicion), whether accuracy is far above breakeven or just barely clears it, and any sign the pattern is just curve-fit noise rather than a real recurring edge.",
        "",
        "Signals:",
    ]
    for i, s in enumerate(signals):
        test_acc = f"{s['test_accuracy']}%" if s.get("test_accuracy") is not None else "N/A (not enough unseen data)"
        lines.append(
            f"{i}. time={s['time']} pattern={s['pattern']} direction={s['direction']} "
            f"train_confidence={s['confidence']}% test_accuracy={test_acc} "
            f"test_samples={s.get('test_samples', 0)} reason=\"{s['reason']}\""
        )
    lines.append("")
    lines.append('Respond with ONLY a JSON array (no prose, no markdown fences), one object per signal in the SAME order, each shaped exactly like: {"score": <0-100 int, your reliability rating>, "verdict": "confirm"|"downgrade"|"reject", "note": "<max 15 words, why>"}')
    return "\n".join(lines)


def ai_review_signals(client, pair, signals, payout_pct, model=OPENROUTER_MODEL):
    """
    Sends every statistically-surviving signal for one pair to the AI in a
    SINGLE batched request (not one call per signal — cheaper, faster,
    avoids hammering the API) and asks for an independent reliability
    rating. This is a second opinion, not a black-box guarantee: it can
    only downgrade/reject signals whose numbers don't look sound, or
    confirm ones that do. If the AI call fails or returns something we
    can't parse, we skip the AI layer for this pair and keep the
    statistical result untouched — the script never silently drops
    signals just because the AI step broke.
    """
    if not signals or client is None:
        for s in signals:
            s["ai_score"] = None
            s["ai_verdict"] = None
            s["ai_note"] = None
        return signals
    prompt = _build_ai_prompt(pair, signals, payout_pct)
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "You output strict JSON only, nothing else."},
                {"role": "user", "content": prompt},
            ],
            temperature=0,
        )
        raw = resp.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.strip("`")
            if raw.lower().startswith("json"):
                raw = raw[4:].strip()
        reviews = json.loads(raw)
        if not isinstance(reviews, list) or len(reviews) != len(signals):
            raise ValueError("AI response shape mismatch")
        for s, r in zip(signals, reviews):
            s["ai_score"] = r.get("score")
            s["ai_verdict"] = r.get("verdict")
            s["ai_note"] = r.get("note")
        return signals
    except Exception as e:
        warn(f"{pair}: AI confirmation layer failed ({e}) — keeping statistical result only.")
        for s in signals:
            s["ai_score"] = None
            s["ai_verdict"] = None
            s["ai_note"] = None
        return signals


def combined_score(sig):
    """Blend the honest out-of-sample backtest accuracy with the AI's
    independent reliability score (when available) into one ranking
    number. Backtest accuracy is weighted higher (60/40) because it's
    a measured out-of-sample statistic, not an opinion — the AI score
    is a sanity-check layer on top, not a replacement for it."""
    test_acc = sig.get("test_accuracy")
    ai_score = sig.get("ai_score")
    if test_acc is not None and ai_score is not None:
        return round(test_acc * 0.6 + ai_score * 0.4, 1)
    if test_acc is not None:
        return test_acc
    if ai_score is not None:
        return ai_score
    return sig.get("confidence", 0)


def retrain_on_full_data(signals, candles, bucket_minutes, min_samples=5):
    """
    Final step for signals that ALREADY passed honest out-of-sample
    validation (test_accuracy is not None): recompute that same
    pattern/time/direction's accuracy using the FULL history (train +
    test candles combined) so the reported number rests on a bigger,
    more stable sample instead of just the 30% test slice.

    This does NOT replace test_accuracy — test_accuracy is still the
    real proof the pattern isn't curve-fit, since it was computed on
    data the pattern never "saw" while being selected. full_accuracy is
    a secondary, larger-sample re-estimate of the SAME already-proven
    pattern, not a new claim.

    Unvalidated signals (test_accuracy is None) are left alone —
    retraining them on the full set (which includes data we never held
    out for them) would just be circular curve-fitting again.
    """
    full_learned = PatternEngine(candles, bucket_minutes).learn()
    direction_field = {"BUY": "bull", "SELL": "bear"}
    for sig in signals:
        if sig.get("test_accuracy") is None:
            sig["full_accuracy"] = None
            sig["full_samples"] = 0
            continue
        stats = full_learned.get(sig["pattern"], {}).get(sig["time"])
        if not stats or stats["total"] < min_samples:
            sig["full_accuracy"] = None
            sig["full_samples"] = stats["total"] if stats else 0
            continue
        field = direction_field[sig["direction"]]
        sig["full_accuracy"] = round(stats[field] / stats["total"] * 100, 1)
        sig["full_samples"] = stats["total"]
    return signals


# ---------------------------------------------------------------------------
# Train/test split, time-window filtering, clash resolution
# Decompiled by Telegram @Mohamed_TraderTBT
# ---------------------------------------------------------------------------
def split_train_test(candles, train_ratio=0.7):
    """Chronological split — train on earlier candles, test on later
    ones. This avoids lookahead bias (never test on data older than
    what you trained on)."""
    cutoff = int(len(candles) * train_ratio)
    return candles[:cutoff], candles[cutoff:]


def filter_time_window(patterns, start_time, end_time):
    start_dt = datetime.strptime(start_time, "%H:%M")
    end_dt = datetime.strptime(end_time, "%H:%M")
    out = []
    for p in patterns:
        bucket_start_str = p["time"].split("-")[0]
        signal_dt = datetime.strptime(bucket_start_str, "%H:%M")
        if start_dt <= signal_dt <= end_dt:
            out.append(p)
    return out


def remove_clashes(patterns):
    time_groups = defaultdict(list)
    for p in patterns:
        time_groups[p["time"]].append(p)
    best = []
    for group in time_groups.values():
        group.sort(
            key=lambda x: (x.get("test_accuracy") is not None, x.get("test_accuracy") or 0, x["confidence"]),
            reverse=True,
        )
        best.append(group[0])
    best.sort(key=lambda x: x["time"])
    return best


def _hm_to_min(hm_str):
    h, m = map(int, hm_str.split(":"))
    return h * 60 + m


def find_best_minute(pattern_name, direction, bucket_key, exact_learned, min_samples=2):
    """
    Drill-down: given a bucket-level signal that passed backtesting (e.g.
    '19:00-19:15' SELL on Momentum), look inside that window at exact-minute
    stats (from the SAME train data) to find which specific minute inside
    the window historically leaned hardest in that direction. This gives
    an actionable exact trade time on top of the reliable wide-window
    validation. Note: this specific-minute number is NOT separately
    out-of-sample tested (too few samples for that) — it's a best-guess
    entry point *within* an already-validated window, not a new claim.
    """
    start_str, end_str = bucket_key.split("-")
    start_min, end_min = _hm_to_min(start_str), _hm_to_min(end_str)
    wrap = end_min <= start_min
    if wrap:
        end_min += 1440
    direction_field = "bull" if direction == "BUY" else "bear"
    pattern_stats = exact_learned.get(pattern_name, {})
    best_minute, best_pct, best_samples = None, -1, 0
    for time_key, stats in pattern_stats.items():
        if stats["total"] < min_samples:
            continue
        t = _hm_to_min(time_key)
        if wrap and t < start_min:
            t += 1440
        if not (start_min <= t < end_min):
            continue
        pct = stats[direction_field] / stats["total"] * 100
        if pct > best_pct or (pct == best_pct and stats["total"] > best_samples):
            best_minute, best_pct, best_samples = time_key, pct, stats["total"]
    if best_minute is None:
        return None
    return {"minute": best_minute, "pct": round(best_pct, 1), "samples": best_samples}


def analyze_timeless(candles, train_ratio=0.7, min_samples=30):
    """
    Time-INDEPENDENT pattern check. Ignores clock time completely and pools
    every occurrence of each pattern (Heavy/Reversal/Momentum/Engulfing)
    across the whole history into one big sample. This trades away the
    "what time to trade" info in exchange for much larger, more statistically
    solid sample sizes (hundreds-thousands vs. 15-25 per time-slot) — the
    cleanest possible test of whether a pattern has ANY real edge at all,
    independent of any time-of-day story.
    """
    train_candles, test_candles = split_train_test(candles, train_ratio)
    train_learned = PatternEngine(train_candles, bucket_minutes=0).learn()
    test_learned = PatternEngine(test_candles, bucket_minutes=0).learn()
    results = []
    for pattern_name, time_stats in train_learned.items():
        stats = time_stats.get("ALL")
        if not stats or stats["total"] < min_samples:
            continue
        bull_pct = stats["bull"] / stats["total"] * 100
        bear_pct = stats["bear"] / stats["total"] * 100
        direction = "BUY" if bull_pct >= bear_pct else "SELL"
        train_pct = max(bull_pct, bear_pct)
        train_samples = stats["total"]
        test_stats = test_learned.get(pattern_name, {}).get("ALL")
        if test_stats and test_stats["total"] >= min_samples:
            hits = test_stats["bull"] if direction == "BUY" else test_stats["bear"]
            test_pct = round(hits / test_stats["total"] * 100, 1)
            test_samples = test_stats["total"]
        else:
            test_pct = None
            test_samples = test_stats["total"] if test_stats else 0
        results.append({
            "pattern": pattern_name,
            "direction": direction,
            "train_pct": round(train_pct, 1),
            "train_samples": train_samples,
            "test_pct": test_pct,
            "test_samples": test_samples,
        })
    results.sort(key=lambda x: (x["test_pct"] is not None, x["test_pct"] or 0), reverse=True)
    return results


# ---------------------------------------------------------------------------
# Result presentation / signal assembly
# Decompiled by Telegram @Mohamed_TraderTBT
# ---------------------------------------------------------------------------
def print_timeless_results(pair, results, payout_pct=80):
    breakeven = 100 / (100 + payout_pct) * 100
    section(f"TIME-INDEPENDENT PATTERN CHECK — {pair} (large-sample, ignores clock)")
    header = f"  {'Pattern':<11} {'Dir':<5} {'Train %':<9} {'Train N':<9} {'Test %':<9} {'Test N':<8} {'Breakeven':<10}"
    print(c(header, C.BOLD, C.WHITE))
    hr("─", 78, C.GREY)
    if not results:
        warn("No pattern had enough samples even at this scale — try more history days.")
        return
    for r in results:
        dir_color = C.GREEN if r["direction"] == "BUY" else C.RED
        dir_txt = c(f"{r['direction']:<5}", C.BOLD, dir_color)
        if r["test_pct"] is None:
            test_str = c(f"{'N/A':<9}", C.GREY)
            verdict = c(f"{'? unknown':<10}", C.GREY)
        else:
            test_str_raw = f"{r['test_pct']}%"
            if r["test_pct"] >= breakeven:
                test_str = c(f"{test_str_raw:<9}", C.BOLD, C.GREEN)
                verdict = c(f"{'✔ above':<10}", C.GREEN)
            else:
                test_str = c(f"{test_str_raw:<9}", C.RED)
                verdict = c(f"{'✘ below':<10}", C.RED)
        print(
            f"  {r['pattern']:<11} {dir_txt} {str(r['train_pct']) + '%':<9} "
            f"{r['train_samples']:<9} {test_str} {r['test_samples']:<8} {verdict}"
        )
    hr("─", 78, C.GREY)
    tested = [r for r in results if r["test_pct"] is not None]
    if tested:
        avg = sum(r["test_pct"] for r in tested) / len(tested)
        print(f"  Average test accuracy across patterns: {c(f'{avg:.1f}%', C.BOLD)} (breakeven at {payout_pct}% payout ≈ {breakeven:.1f}%)")
        info(f"'Breakeven' = accuracy needed just to not lose money at {payout_pct}% payout — consistently clearing it (not just one pattern by luck) is the real bar.")


def generate_future_signals(all_pair_patterns, max_signals, payout_pct=80.0):
    breakeven = 100 / (100 + payout_pct) * 100
    date_str = datetime.now().strftime("%Y-%m-%d")
    all_signals = []
    dropped_unvalidated = 0
    dropped_below_breakeven = 0
    for pair, patterns in all_pair_patterns.items():
        for p in patterns:
            # Only keep signals that were validated out-of-sample AND clear breakeven.
            if p.get("test_accuracy") is None:
                dropped_unvalidated += 1
                continue
            if p["test_accuracy"] < breakeven:
                dropped_below_breakeven += 1
                continue
            all_signals.append({
                "date": date_str,
                "time_pkt": (p.get("best_minute") or p["time"]) + " PKT",
                "pair": pair,
                "direction": p["direction"],
                "confidence": p["confidence"],
                "confidence_str": f"{p['confidence']:.1f}%",
                "test_accuracy": p.get("test_accuracy"),
                "test_accuracy_str": f"{p['test_accuracy']:.1f}%" if p.get("test_accuracy") is not None else "N/A",
                "test_samples": p.get("test_samples", 0),
                "full_accuracy": p.get("full_accuracy"),
                "full_accuracy_str": f"{p['full_accuracy']:.1f}%" if p.get("full_accuracy") is not None else "N/A",
                "full_samples": p.get("full_samples", 0),
                "pattern": p["pattern"],
                "reason": p["reason"],
                "best_minute": p.get("best_minute"),
                "best_minute_pct": p.get("best_minute_pct"),
                "best_minute_samples": p.get("best_minute_samples", 0),
                "ai_score": p.get("ai_score"),
                "ai_verdict": p.get("ai_verdict"),
                "ai_note": p.get("ai_note"),
                "combined_score": p.get("combined_score", p["confidence"]),
            })
    all_signals.sort(key=lambda x: (x["combined_score"], x["confidence"]), reverse=True)
    top = all_signals[:max_signals]
    if len(top) < max_signals:
        warn(
            f"Only {len(top)} signal(s) actually passed validation + breakeven "
            f"(≈{breakeven:.1f}% at {payout_pct}% payout) — showing {len(top)} instead of the "
            f"{max_signals} requested rather than padding the list with unproven ones."
        )
    if dropped_unvalidated or dropped_below_breakeven:
        info(
            f"Filtered out: {dropped_unvalidated} unvalidated (no test data yet), "
            f"{dropped_below_breakeven} below breakeven on the test slice."
        )
    top.sort(key=lambda x: x["time_pkt"])
    return top


def save_signals(signals, filename):
    with open(filename, mode="w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "Date", "Time (PKT)", "Pair", "Direction", "Train Confidence",
            "Backtested Accuracy (test slice)", "Test Samples",
            "Full-Data Accuracy (train+test)", "Full-Data Samples",
            "AI Score", "AI Verdict", "AI Note", "Combined Score",
            "Pattern", "Reason", "Best Minute", "Best Minute Train %", "Best Minute Samples",
        ])
        for s in signals:
            w.writerow([
                s["date"],
                s["time_pkt"],
                s["pair"],
                s["direction"],
                s["confidence_str"],
                s["test_accuracy_str"],
                s["test_samples"],
                s["full_accuracy_str"],
                s["full_samples"],
                s.get("ai_score") if s.get("ai_score") is not None else "",
                s.get("ai_verdict") or "",
                s.get("ai_note") or "",
                s.get("combined_score", ""),
                s["pattern"],
                s["reason"],
                s.get("best_minute") or "",
                s.get("best_minute_pct") or "",
                s.get("best_minute_samples") or "",
            ])
    ok(f"Saved {len(signals)} signals to: {filename}")


# ---------------------------------------------------------------------------
# Input prompts + banner
# Decompiled by Telegram @Mohamed_TraderTBT
# ---------------------------------------------------------------------------
def ask(label):
    return input(c("[>>] ", C.BOLD, C.GREEN) + c(label, C.GREEN))


def ask_key(label):
    return input(c("[?] ", C.BOLD, C.YELLOW) + c(label, C.YELLOW))


def print_banner():
    # Big "DECOMPILED" logo (letter blocks that exist: D E C O M P I L).
    L = {
        "D": ["██████╗ ", "██╔══██╗", "██║  ██║", "██║  ██║", "██████╔╝", "╚═════╝ "],
        "E": ["███████╗", "██╔════╝", "█████╗  ", "██╔══╝  ", "███████╗", "╚══════╝"],
        "C": [" ██████╗", "██╔════╝", "██║     ", "██║     ", "╚██████╗", " ╚═════╝"],
        "O": [" ██████╗ ", "██╔═══██╗", "██║   ██║", "██║   ██║", "╚██████╔╝", " ╚═════╝ "],
        "M": ["███╗   ███╗", "████╗ ████║", "██╔████╔██║", "██║╚██╔╝██║", "██║ ╚═╝ ██║", "╚═╝     ╚═╝"],
        "P": ["██████╗ ", "██╔══██╗", "██████╔╝", "██╔═══╝ ", "██║     ", "╚═╝     "],
        "I": ["██╗", "██║", "██║", "██║", "██║", "╚═╝"],
        "L": ["██╗     ", "██║     ", "██║     ", "██║     ", "███████╗", "╚══════╝"],
    }
    word = "DECOMPILED"
    print()
    for r in range(6):
        print(c("  " + " ".join(L[ch][r] for ch in word), C.BOLD, C.GREEN))
    print(c("  ══════════════════════════════════════════════════════════ ", C.GREY))
    print(c("  Decompiled by Telegram @Mohamed_TraderTBT", C.BOLD, C.GREEN))
    print()


def print_signals(signals, ai_enabled=False):
    if not signals:
        return
    date_str = datetime.now().strftime("%Y-%m-%d")
    W_IDX, W_TIME, W_PAIR, W_DIR, W_TEST, W_PAT = 4, 15, 18, 6, 9, 11
    width = 78

    import hashlib
    sig_hash = hashlib.md5(
        "|".join(f"{s['pair']}{s['time_pkt']}{s['direction']}" for s in signals).encode()
    ).hexdigest()[:12].upper()

    print()
    box_top(width, C.GREEN, corner_l="╔", corner_r="╗", fill="═")
    box_line(
        f"[ SIGNAL REPORT ] {date_str} · {len(signals)} ENTRIES · HASH {sig_hash}",
        width, C.GREEN, text_color=C.WHITE, bold=True,
    )
    box_divider(width, C.GREEN)

    header = f"{'#':<{W_IDX}}{'Time':<{W_TIME}}{'Pair':<{W_PAIR}}{'Dir':<{W_DIR}}{'Test %':<{W_TEST}}{'Pattern':<{W_PAT}}"
    box_line(header, width, C.GREEN, text_color=C.WHITE, bold=True, center=False, side="│")
    print(c("├" + "─" * (width - 2) + "┤", C.GREEN))

    for idx, s in enumerate(signals, 1):
        dir_color = C.GREEN if s["direction"] == "BUY" else C.RED
        dir_txt = c(f"{s['direction']:<{W_DIR}}", C.BOLD, dir_color)
        if s.get("test_accuracy") is None:
            bt = c(f"{'N/A':<{W_TEST}}", C.GREY)
        elif s["test_accuracy"] >= 70:
            bt = c(f"{s['test_accuracy_str']:<{W_TEST}}", C.BOLD, C.GREEN)
        elif s["test_accuracy"] >= 50:
            bt = c(f"{s['test_accuracy_str']:<{W_TEST}}", C.YELLOW)
        else:
            bt = c(f"{s['test_accuracy_str']:<{W_TEST}}", C.RED)
        row_plain = f"{idx:<{W_IDX}}{s['time_pkt']:<{W_TIME}}{s['pair']:<{W_PAIR}}"
        row = c(row_plain, C.WHITE) + dir_txt + bt + c(f"{s['pattern']:<{W_PAT}}", C.MAGENTA)
        box_line(row, width, C.GREEN, center=False, side="│")

    box_bottom(width, C.GREEN, corner_l="└", corner_r="┘", fill="─")
    print()
    print(c(f"  [+] {len(signals)} signals extracted · out-of-sample tested · not financial advice", C.BOLD, C.GREEN))

    # Copy-paste-friendly block for pasting into a channel.
    print()
    box_top(width, C.CYAN, corner_l="╔", corner_r="╗", fill="═")
    box_line("📢 CHANNEL COPY-PASTE BLOCK", width, C.CYAN, text_color=C.WHITE, bold=True)
    box_bottom(width, C.CYAN, corner_l="╚", corner_r="╝", fill="═")
    print()
    print(c(f"📊 QUOTEX SIGNALS — {date_str}", C.BOLD, C.WHITE))
    print()
    for idx, s in enumerate(signals, 1):
        emoji = "🟢" if s["direction"] == "BUY" else "🔴"
        line = f"{idx}. {s['pair']} | {s['time_pkt']} | {s['direction']} {emoji} | Test: {s['test_accuracy_str']} | {s['pattern']}"
        print(c(line, C.WHITE))
    print()
    print(c("  [i] channel block ready — copy above", C.GREY))
    print()


# ---------------------------------------------------------------------------
# Candle fetching (progress bar + retries)
# Decompiled by Telegram @Mohamed_TraderTBT
# ---------------------------------------------------------------------------
def print_progress(fetched_seconds, total_seconds, candle_count, start_ts):
    pct = min(fetched_seconds / total_seconds, 1.0) if total_seconds > 0 else 0
    bar_len = 35
    filled = int(bar_len * pct)
    if pct >= 1:
        bar_color = C.GREEN
    elif pct >= 0.5:
        bar_color = C.CYAN
    else:
        bar_color = C.BLUE
    bar = c("━" * filled, bar_color) + c("─" * (bar_len - filled), C.GREY)
    icon = "✔" if pct >= 1 else "⬇"
    elapsed = time.time() - start_ts
    if pct > 0.01:
        eta_str = str(timedelta(seconds=int(elapsed / pct * (1 - pct))))
    else:
        eta_str = "calculating..."
    days_fetched = fetched_seconds / 86400
    print(
        f"\r  {c(icon, C.BOLD, bar_color)} {bar} {c(f'{pct * 100:5.1f}%', C.BOLD, bar_color)} "
        f"{c(f'{days_fetched:.2f}d', C.WHITE)} · {c(f'{candle_count:,}', C.WHITE)} candles · ⏱"
        f"{elapsed:.0f}s · ETA {eta_str}   ",
        end="", flush=True,
    )


async def fetch_pair_data(email, password, pair, days, timeframe, max_attempts=3, max_workers=5):
    """Fetch candles using the global G_CLIENT (pre-authenticated).
    Falls back to a fresh client only if G_CLIENT is dead."""
    global G_CLIENT
    duration_seconds = int(86400 * days)
    fetch_timeout = max(300, int(days * 90))
    all_candles = None
    fetch_time = 0.0
    for attempt in range(1, max_attempts + 1):
        print(c(f"  [>>] FETCH {pair} · {days}d M1", C.GREEN), end="", flush=True)
        start_time = time.time()
        client = G_CLIENT
        fresh_client = False
        if client is None:
            info("No global client — creating fresh connection (PIN may be asked)...")
            client = Quotex(email=email, password=password, lang="en")
            client.debug_ws_enable = False
            fresh_client = True
            with suppress_stdout():
                with Spinner(f"handshake {pair}..."):
                    check, msg = await asyncio.wait_for(client.connect(), timeout=120)
            if not check:
                print(f"\r  {c('[✘]', C.BOLD, C.RED)} {pair} CONNECTION FAILED          ")
                continue

        def on_progress(current, total, label, worker_label):
            pct = min(current / total, 1.0) if total else 0
            print(
                f"\r  {c('[>>]', C.BOLD, C.GREEN)} {c(f'{pair:<16}', C.WHITE)} "
                f"{c(f'{pct * 100:5.1f}%', C.CYAN)} · {c(f'{current:,}', C.WHITE)} candles",
                end="", flush=True,
            )

        try:
            try:
                with suppress_stdout():
                    candles = await asyncio.wait_for(
                        client.get_candles_deep(
                            pair, duration_seconds, timeframe,
                            progress_callback=on_progress, max_workers=max_workers,
                        ),
                        timeout=fetch_timeout,
                    )
            except TypeError:
                # Older pyquotex builds don't accept max_workers.
                candles = await asyncio.wait_for(
                    client.get_candles_deep(pair, duration_seconds, timeframe, progress_callback=on_progress),
                    timeout=fetch_timeout,
                )
            all_candles = candles
            fetch_time = time.time() - start_time
            break
        except asyncio.TimeoutError:
            print(f"\r  {c('[..]', C.YELLOW)} {pair} timeout — retrying...          ")
            if fresh_client:
                try:
                    await client.close()
                except Exception:
                    pass
            G_CLIENT = None
            if attempt < max_attempts:
                await asyncio.sleep(5)
                continue
            print(f"\r  {c('[✘]', C.BOLD, C.RED)} {pair} TIMEOUT — skipped          ")
            return None
        except Exception as e:
            err_msg = f"{type(e).__name__}: {e}"
            err(f"{pair}: {err_msg}")
            if fresh_client:
                try:
                    await client.close()
                except Exception:
                    pass
            G_CLIENT = None
            if attempt < max_attempts:
                await asyncio.sleep(5)
                continue
            return None

    if all_candles:
        print(
            f"\r  {c('[✔]', C.BOLD, C.GREEN)} {c(f'{pair:<16}', C.WHITE)} "
            f"{c(f'{len(all_candles):>7,}', C.GREEN)} candles  {c(f'{fetch_time:.1f}s', C.GREY)}      "
        )
    else:
        print(f"\r  {c('[✘]', C.BOLD, C.RED)} {c(f'{pair:<16}', C.WHITE)} NO DATA           ")

    if all_candles:
        return [cd for cd in all_candles if cd.get("open") is not None]
    return None


# ---------------------------------------------------------------------------
# Per-pair analysis pipeline
# Decompiled by Telegram @Mohamed_TraderTBT
# ---------------------------------------------------------------------------
def analyze_pair(candles, timeframe, min_accuracy, start_time, end_time,
                 train_ratio=0.7, bucket_minutes=1, pair_label=""):
    "Learn on TRAIN slice, validate on TEST slice — honest accuracy."
    train_candles, test_candles = split_train_test(candles, train_ratio)
    train_days = len(train_candles) * timeframe / 86400 if train_candles else 0
    test_days = len(test_candles) * timeframe / 86400 if test_candles else 0

    train_min_samples = max(6, min(20, int(train_days * 0.6)))
    test_min_samples = max(3, min(10, int(test_days * 0.6)))

    train_learned = PatternEngine(train_candles, bucket_minutes).learn()
    test_learned = PatternEngine(test_candles, bucket_minutes).learn()

    signals = score_patterns(train_learned, min_accuracy, min_samples=train_min_samples, source="TRAIN")
    label = pair_label or "Pair"
    if not signals:
        warn(f"{label}: 0 candidates cleared TRAIN — lower accuracy / add history.")

    signals = validate_on_test(signals, test_learned, min_samples=test_min_samples)
    n_validated = sum(1 for s in signals if s.get("test_accuracy") is not None)

    signals = filter_time_window(signals, start_time, end_time)
    signals = remove_clashes(signals)
    signals = retrain_on_full_data(signals, candles, bucket_minutes)

    # For wide buckets, drill down to the single best exact minute inside each window.
    if bucket_minutes > 1 and signals:
        exact_learned = PatternEngine(train_candles, 1).learn()
        for sig in signals:
            drill = find_best_minute(sig["pattern"], sig["direction"], sig["time"], exact_learned)
            if not drill:
                continue
            sig["best_minute"] = drill["minute"]
            sig["best_minute_pct"] = drill["pct"]
            sig["best_minute_samples"] = drill["samples"]
    return signals


# ---------------------------------------------------------------------------
# Interactive entry point
# Decompiled by Telegram @Mohamed_TraderTBT
# ---------------------------------------------------------------------------
async def main():
    print_banner()

    if not check_expiry():
        return

    section("ENTER YOUR SETTINGS")

    pairs_input = ask("Pairs (comma separated, e.g. USDPKR_otc,EURUSD_otc): ").strip()
    pairs = [p.strip() for p in pairs_input.split(",") if p.strip()]
    if not pairs:
        err("No pairs entered. Exiting.")
        return

    while True:
        try:
            history_days = float(ask("History days (e.g. 7, 14, 30): ").strip())
            if history_days > 0:
                break
        except ValueError:
            warn("Invalid number")

    timeframe = 60
    info("Timeframe locked to M1 (60s)")

    start_time = ask("Start time PKT (HH:MM, default 00:00): ").strip() or "00:00"
    end_time = ask("End time PKT (HH:MM, default 23:59): ").strip() or "23:59"

    while True:
        try:
            min_accuracy = float(ask("Min accuracy % on TRAIN data (e.g. 75, 80): ").strip())
            if 0 < min_accuracy <= 100:
                break
        except ValueError:
            warn("Invalid number")

    while True:
        try:
            max_signals = int(ask("How many signals you want? (e.g. 10, 15): ").strip())
            if max_signals > 0:
                break
        except ValueError:
            warn("Invalid number")

    while True:
        try:
            train_ratio = ask("Train/Test split % for backtest (default 70): ").strip()
            train_ratio = float(train_ratio) / 100 if train_ratio else 0.7
            if 0.4 <= train_ratio <= 0.9:
                break
            warn("Enter something between 40 and 90")
        except ValueError:
            warn("Invalid number")

    bucket_minutes = 15
    info("Time bucket locked to 15 minutes")

    while True:
        try:
            payout_input = ask("Broker payout % for breakeven check (default 80): ").strip()
            payout_pct = float(payout_input) if payout_input else 80.0
            if 0 < payout_pct <= 100:
                break
            warn("Enter something between 1 and 100")
        except ValueError:
            warn("Invalid number")

    fetch_workers = 4
    info("Fetch workers locked to 4")

    ai_enabled = False
    ai_client = None
    info("AI confirmation layer disabled (hardcoded)")

    output_file = ask("Output filename (default: future_signals.csv): ").strip() or "future_signals.csv"

    section("SETTINGS CONFIRMED")
    width = 78
    box_top(width, C.BLUE, corner_l="┌", corner_r="┐", fill="─")
    rows = [
        ("Pairs", ", ".join(pairs)),
        ("History", f"{history_days} days"),
        ("Timeframe", "60s (M1) — LOCKED"),
        ("PKT Window", f"{start_time} — {end_time}"),
        ("Min Accuracy", f"{min_accuracy}% (train slice)"),
        ("Train / Test", f"{int(train_ratio * 100)}% / {int((1 - train_ratio) * 100)}%"),
        ("Bucket Size", "15 min — LOCKED"),
        ("Signals", str(max_signals)),
        ("Payout", f"{payout_pct}%"),
        ("Workers", "4 — LOCKED"),
        ("AI Layer", "OFF — LOCKED"),
        ("Output", output_file),
    ]
    for label, value in rows:
        line = f"{label:<15}{value}"
        box_line(line, width, C.BLUE, text_color=C.WHITE, center=False, side="│")
    box_bottom(width, C.BLUE, corner_l="└", corner_r="┘", fill="─")

    confirm = ask_key("Proceed? (y/n): ").strip().lower()
    if confirm != "y":
        warn("Cancelled.")
        return

    print()
    print(c("  [>>] INITIATING MULTI-PAIR SCAN", C.BOLD, C.GREEN))

    all_pair_patterns = {}
    for pair in pairs:
        candles = await fetch_pair_data(
            EMAIL, PASSWORD, pair, history_days, timeframe, max_workers=fetch_workers
        )
        if candles:
            patterns = analyze_pair(
                candles, timeframe, min_accuracy, start_time, end_time,
                train_ratio, bucket_minutes, pair_label=pair,
            )
            if ai_enabled and patterns:
                patterns = ai_review_signals(ai_client, pair, patterns, payout_pct)
            for p in patterns:
                p["combined_score"] = combined_score(p)
            all_pair_patterns[pair] = patterns
            print(c(
                f"  [✔] {c(f'{pair:<16}', C.WHITE)} {c(f'{len(patterns):>3}', C.GREEN)} patterns passed backtest",
                C.GREEN,
            ))
        else:
            print(c(f"  [✘] {c(f'{pair:<16}', C.WHITE)} skipped (no data)", C.RED))
        if pair != pairs[-1]:
            print(c("  [..] cooldown", C.GREY), end="", flush=True)
            await asyncio.sleep(5)
            print("\r                  \r", end="", flush=True)

    print()
    print(c("  [>>] COMPILING SIGNAL LIST", C.BOLD, C.GREEN))
    signals = generate_future_signals(all_pair_patterns, max_signals, payout_pct)

    if signals:
        print_signals(signals, ai_enabled)
        save_signals(signals, output_file)
    else:
        err("No signals found. Try lower accuracy, more history, or a wider time window.")

    print()
    width = 78
    box_top(width, C.GREEN, corner_l="╔", corner_r="╗", fill="═")
    box_line("[ SESSION TERMINATED ] SIGNALS DELIVERED", width, C.GREEN, text_color=C.WHITE, bold=True)
    box_bottom(width, C.GREEN, corner_l="╚", corner_r="╝", fill="═")
    print()


if __name__ == "__main__":
    asyncio.run(main())
