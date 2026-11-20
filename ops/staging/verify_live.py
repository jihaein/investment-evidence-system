"""Verify a specific provider close and filing are served by the staging API."""
import argparse
import os
from datetime import date
from urllib.parse import urlsplit

import requests


def expect(condition, message):
    if not condition:
        raise SystemExit(message)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("api_url")
    parser.add_argument("--market-stock", required=True)
    parser.add_argument("--market-session", type=date.fromisoformat, required=True)
    parser.add_argument("--filing-stock", required=True)
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--buy-date", type=date.fromisoformat, required=True)
    parser.add_argument("--as-of", type=date.fromisoformat)
    args = parser.parse_args()
    parsed = urlsplit(args.api_url)
    expect(parsed.scheme == "https" and parsed.hostname and not parsed.username
           and not parsed.query and not parsed.fragment, "Use the HTTPS staging API URL")
    url = args.api_url.rstrip("/")
    session = args.market_session.isoformat()
    with requests.Session() as client:
        client.headers["Authorization"] = f"Bearer {os.environ['STAGING_API_TOKEN']}"
        ready = client.get(f"{url}/ready", timeout=30, allow_redirects=False)
        expect(ready.status_code == 200, f"Staging readiness returned HTTP {ready.status_code}")
        close = client.post(f"{url}/portfolio", params={"as_of": session},
                            json=[{"stock_code": args.market_stock, "buy_date": args.buy_date.isoformat()}],
                            timeout=30, allow_redirects=False)
        expect(close.status_code == 200, f"Staging portfolio returned HTTP {close.status_code}")
        market, = close.json()
        expect(market.get("market_session") == session and market.get("price_as_of") == session,
               f"Staging portfolio has yet to publish the {session} close")
        expect(str(market.get("ohlson_price_as_of", "")).startswith(session),
               "Select a covered stock with an Ohlson evaluation that uses the refreshed close")

        query = {"stock_code": args.filing_stock, "buy_date": args.buy_date.isoformat()}
        if args.as_of:
            query["as_of"] = args.as_of.isoformat()
        signal = client.get(f"{url}/signal", params=query, timeout=30, allow_redirects=False)
        expect(signal.status_code == 200, f"Staging signal returned HTTP {signal.status_code}")
        filing = signal.json()
        receipts = {value["detail"].get("rcept_no_eval")
                    for value in (filing.get("evidence") or {}).values()
                    if isinstance(value, dict) and "detail" in value}
        expect(args.receipt in receipts, f"Staging signal has yet to reflect DART receipt {args.receipt}")
        print(f"Verified KRX {session} (release {market['release_id']}) "
              f"and DART {args.receipt} (release {filing['release_id']})")


if __name__ == "__main__":
    main()
