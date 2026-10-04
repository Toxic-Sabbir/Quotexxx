#!/usr/bin/env python3
"""Local dashboard for viewing historically validated Quotex signals."""

import argparse
import asyncio
import json
import re
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import main as bot


ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "web"
STATE_LOCK = threading.Lock()
STATE = {
    "running": False,
    "status": "idle",
    "message": "Ready for a scan.",
    "signals": [],
    "summary": None,
    "settings": None,
    "error": None,
    "updated_at": None,
}


def snapshot():
    with STATE_LOCK:
        return json.loads(json.dumps(STATE))


def update_state(**values):
    with STATE_LOCK:
        STATE.update(values)
        STATE["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")


def validate_settings(payload):
    if not isinstance(payload, dict):
        raise ValueError("Settings must be sent as a JSON object.")

    pairs = [pair.strip() for pair in str(payload.get("pairs", "")).split(",") if pair.strip()]
    if not pairs or len(pairs) > 12:
        raise ValueError("Enter between 1 and 12 asset pairs.")
    if any(not re.fullmatch(r"[A-Za-z0-9_-]{2,32}", pair) for pair in pairs):
        raise ValueError("Pairs may contain letters, numbers, underscores, and hyphens only.")

    try:
        days = float(payload.get("history_days", 7))
        min_accuracy = float(payload.get("min_accuracy", 75))
        max_signals = int(payload.get("max_signals", 10))
        train_ratio = float(payload.get("train_ratio", 0.7))
        payout_pct = float(payload.get("payout_pct", 80))
    except (TypeError, ValueError):
        raise ValueError("Numeric settings contain an invalid value.") from None

    start_time = str(payload.get("start_time", "00:00"))
    end_time = str(payload.get("end_time", "23:59"))
    if not 1 <= days <= 90:
        raise ValueError("History must be between 1 and 90 days.")
    if not 0 < min_accuracy <= 100:
        raise ValueError("Minimum training accuracy must be from 1 to 100%.")
    if not 1 <= max_signals <= 100:
        raise ValueError("Signal limit must be between 1 and 100.")
    if not 0.4 <= train_ratio <= 0.9:
        raise ValueError("Train split must be between 40% and 90%.")
    if not 0 < payout_pct <= 100:
        raise ValueError("Payout must be from 1 to 100%.")
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", start_time):
        raise ValueError("Start time must use 24-hour HH:MM format.")
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", end_time):
        raise ValueError("End time must use 24-hour HH:MM format.")

    return {
        "pairs": pairs,
        "history_days": days,
        "min_accuracy": min_accuracy,
        "max_signals": max_signals,
        "train_ratio": train_ratio,
        "payout_pct": payout_pct,
        "start_time": start_time,
        "end_time": end_time,
    }


async def scan(settings):
    if not bot.EMAIL or not bot.PASSWORD:
        raise RuntimeError("Add QUOTEX_EMAIL and QUOTEX_PASSWORD to .env before scanning.")
    if bot.Quotex is None:
        raise RuntimeError("Quotex is not installed. Install the pyquotex package to scan markets.")
    if not bot.check_expiry():
        raise RuntimeError("This bot build is outside its configured usage period.")

    all_patterns = {}
    total_candles = 0
    for index, pair in enumerate(settings["pairs"], start=1):
        update_state(
            status="fetching",
            message=f"Fetching M1 history for {pair} ({index}/{len(settings['pairs'])}).",
        )
        candles = await bot.fetch_pair_data(
            bot.EMAIL,
            bot.PASSWORD,
            pair,
            settings["history_days"],
            60,
            max_workers=4,
        )
        if not candles:
            continue

        total_candles += len(candles)
        update_state(status="analyzing", message=f"Validating patterns for {pair}.")
        patterns = bot.analyze_pair(
            candles,
            60,
            settings["min_accuracy"],
            settings["start_time"],
            settings["end_time"],
            settings["train_ratio"],
            15,
            pair_label=pair,
        )
        for pattern in patterns:
            pattern["combined_score"] = bot.combined_score(pattern)
        all_patterns[pair] = patterns

    signals = bot.generate_future_signals(
        all_patterns,
        settings["max_signals"],
        settings["payout_pct"],
    )
    tested = [signal["test_accuracy"] for signal in signals if signal.get("test_accuracy") is not None]
    summary = {
        "pairs_scanned": len(settings["pairs"]),
        "pairs_with_data": len(all_patterns),
        "candles": total_candles,
        "signals": len(signals),
        "average_test_accuracy": round(sum(tested) / len(tested), 1) if tested else None,
        "breakeven": round(100 / (100 + settings["payout_pct"]) * 100, 1),
        "completed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    update_state(
        running=False,
        status="complete",
        message=f"Scan complete. {len(signals)} validated signal(s) found.",
        signals=signals,
        summary=summary,
        error=None,
    )


def run_scan(settings):
    try:
        asyncio.run(scan(settings))
    except Exception as exc:
        update_state(
            running=False,
            status="error",
            message="The scan could not be completed.",
            error=f"{type(exc).__name__}: {exc}",
        )


class DashboardHandler(BaseHTTPRequestHandler):
    def send_json(self, data, status=200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/status":
            self.send_json({
                **snapshot(),
                "ready": bool(bot.EMAIL and bot.PASSWORD and bot.Quotex),
                "credentials_configured": bool(bot.EMAIL and bot.PASSWORD),
                "quotex_installed": bot.Quotex is not None,
            })
            return
        if path in ("/", "/index.html", "/app.css", "/app.js"):
            filename = "index.html" if path in ("/", "/index.html") else path.lstrip("/")
            file_path = WEB_ROOT / filename
            content_type = "text/html; charset=utf-8" if filename.endswith(".html") else (
                "text/css; charset=utf-8" if filename.endswith(".css") else "text/javascript; charset=utf-8"
            )
            try:
                body = file_path.read_bytes()
            except OSError:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def do_POST(self):
        global STATE
        if urlparse(self.path).path != "/api/scan":
            self.send_error(404)
            return
        if self.headers.get("Content-Length", "0").isdigit() is False:
            self.send_json({"error": "Invalid request size."}, status=400)
            return
        length = int(self.headers.get("Content-Length", "0"))
        if length > 16_384:
            self.send_json({"error": "Request is too large."}, status=413)
            return
        try:
            payload = json.loads(self.rfile.read(length))
            settings = validate_settings(payload)
        except (json.JSONDecodeError, ValueError) as exc:
            self.send_json({"error": str(exc)}, status=400)
            return

        with STATE_LOCK:
            if STATE["running"]:
                self.send_json({"error": "A scan is already running."}, status=409)
                return
            STATE.update({
                "running": True,
                "status": "starting",
                "message": "Preparing market scan.",
                "signals": [],
                "summary": None,
                "settings": settings,
                "error": None,
                "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            })
        threading.Thread(target=run_scan, args=(settings,), daemon=True).start()
        self.send_json({"accepted": True, "state": snapshot()}, status=202)

    def log_message(self, format_string, *args):
        print(f"[web] {self.address_string()} {format_string % args}")


def main():
    parser = argparse.ArgumentParser(description="Run the local Quotex signal dashboard.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="HTTP port (default: 8765)")
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    print(f"Signal dashboard running at http://{args.host}:{args.port}")
    print("Signals are informational; this dashboard does not place trades.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()