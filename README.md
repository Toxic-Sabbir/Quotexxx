# Quotexxx Signal Desk

Local web dashboard for the existing Quotex M1 signal generator. It fetches candle history, analyzes patterns, checks them on a chronological holdout slice, and displays qualifying signals. It does not place trades or manage an account.

## Run the dashboard

1. Install Python dependencies: `python3 -m pip install -r requirements.txt`
2. Copy `.env.example` to `.env` and set `QUOTEX_EMAIL` and `QUOTEX_PASSWORD`.
3. Start the local dashboard: `python3 web.py`
4. Open [http://127.0.0.1:8765](http://127.0.0.1:8765).

The dashboard can be opened without dependencies or credentials, but scanning requires the Quotex client and credentials. Credentials remain on the server and are never sent to the browser. The web UI uses the analyzer's existing 1-minute candles, 15-minute buckets, and out-of-sample signal checks. AI review is disabled.

## CLI

Run `python3 main.py` to use the original interactive interface. Both interfaces use the same analyzer and expiry gate.

## Important

Signals are historical analysis, not financial advice or a guarantee of future performance. Review risk independently. The source's existing hard expiry is October 10, 2026; scans remain subject to that check.
