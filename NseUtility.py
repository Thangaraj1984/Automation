"""
    * NSE UTILITY - Cookie-based approach *

    Fetches NIFTY option chain data from NSE India.

    Strategy:
    1. Open browser ONCE → visit NSE → extract cookies + headers
    2. Use Python `requests` with those cookies for fast API calls
    3. Re-extract cookies only when they expire (~15 min)

    This is the same approach that works in Excel VBA:
    - Open IE/Edge to get session cookies
    - Use XMLHTTP with those cookies for subsequent requests

    Requirements:
    - pandas
    - requests
    - playwright (pip install playwright && playwright install chromium)
"""

import pandas as pd
import requests
import time
import sys
import os
import json
import threading
from datetime import datetime


class NseUtils:

    BASE_URL = 'https://www.nseindia.com'
    OC_API = '/api/option-chain-v3'
    CONTRACT_API = '/api/option-chain-contract-info'

    # Headers that NSE expects (captured from real browser request)
    DEFAULT_HEADERS = {
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/120.0.0.0 Safari/537.36'
        ),
        'Accept': '*/*',
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept-Encoding': 'gzip, deflate',
        'Referer': 'https://www.nseindia.com/option-chain',
        'sec-ch-ua': '"Chromium";v="120", "Not:A-Brand";v="99"',
        'sec-ch-ua-mobile': '?0',
        'sec-ch-ua-platform': '"Windows"',
    }

    COOKIE_TTL = 900  # Cookies valid for ~15 minutes

    def __init__(self, headless=False):
        """
        :param headless: If True, try headless browser (may not work with NSE).
                         If False, opens a visible browser briefly.
        """
        self._headless = headless
        self._session = requests.Session()
        self._session.headers.update(self.DEFAULT_HEADERS)
        self._cookies_time = 0
        self._lock = threading.Lock()
        self._expiry_dates = []
        print("[NSE] NseUtils initialized (cookie-based approach)")

    def _cookies_valid(self):
        """Check if cookies are still fresh."""
        return (
            time.time() - self._cookies_time < self.COOKIE_TTL
            and len(self._session.cookies) > 0
        )

    def _extract_cookies_via_browser(self):
        """
        Open browser → visit NSE → extract cookies → close browser.
        This takes ~5s and only needs to happen every ~10 minutes.
        """
        from playwright.sync_api import sync_playwright

        print("[NSE] Extracting cookies via browser...")
        start = time.time()

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=self._headless,
                args=[
                    '--disable-http2',
                    '--disable-blink-features=AutomationControlled',
                    '--no-sandbox',
                ]
            )
            context = browser.new_context(
                user_agent=self.DEFAULT_HEADERS['User-Agent'],
                viewport={'width': 1920, 'height': 1080},
            )
            page = context.new_page()
            page.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', "
                "{get: () => undefined})"
            )

            # Go directly to option chain page
            try:
                page.goto(f'{self.BASE_URL}/option-chain',
                          wait_until='domcontentloaded', timeout=60000)
            except Exception:
                pass

            # Wait until the critical Akamai/bot-manager cookies are set.
            # These are the cookies NSE checks on API requests:
            #   _abck, ak_bmsc, bm_sv — Akamai Bot Manager
            #   nsit — NSE session token
            required_cookies = {'_abck', 'ak_bmsc', 'bm_sv'}
            min_wait = 15  # always wait at least 15 seconds
            for i in range(min_wait):
                cookies = context.cookies()
                found = {c['name'] for c in cookies
                         if 'nseindia' in c.get('domain', '')}
                if required_cookies.issubset(found) and i >= min_wait - 1:
                    print(f"[NSE] All required cookies found after {i+1}s")
                    break
                page.wait_for_timeout(1000)
            else:
                if required_cookies.issubset(found):
                    print(f"[NSE] All required cookies found after {min_wait}s")
                else:
                    print(f"[NSE] Timeout waiting for cookies. Got: {found}")

            # Extract all NSE cookies
            cookies = context.cookies()
            nse_cookies = {}
            for c in cookies:
                if 'nseindia' in c.get('domain', ''):
                    nse_cookies[c['name']] = c['value']

            # Close browser
            try:
                browser.close()
            except Exception:
                pass

        # Apply cookies to requests session
        self._session.cookies.clear()
        for name, value in nse_cookies.items():
            self._session.cookies.set(name, value, domain='.nseindia.com')

        self._cookies_time = time.time()
        elapsed = round(time.time() - start, 1)
        print(f"[NSE] Cookies extracted in {elapsed}s: {list(nse_cookies.keys())}")

        # Warm up session with a main page GET
        try:
            self._session.get(self.BASE_URL, timeout=10)
        except Exception:
            pass

    def _ensure_cookies(self):
        """Ensure we have valid cookies, refreshing if needed."""
        with self._lock:
            if not self._cookies_valid():
                self._extract_cookies_via_browser()

    def _get_api(self, path, params=None):
        """
        Make a GET request to NSE API using session cookies.
        Returns parsed JSON or None.
        """
        self._ensure_cookies()

        url = f'{self.BASE_URL}{path}'
        try:
            r = self._session.get(url, params=params, timeout=15)
            r.raise_for_status()
            data = r.json()
            if data and isinstance(data, dict):
                return data
        except requests.exceptions.RequestException as e:
            print(f"[NSE] API request failed: {e}")
            # Invalidate cookies so they get refreshed next time
            self._cookies_time = 0
        except json.JSONDecodeError:
            print(f"[NSE] Invalid JSON response from {path}")
            self._cookies_time = 0

        return None

    def get_expiry_dates(self, symbol='NIFTY'):
        """Fetch available expiry dates for a symbol."""
        data = self._get_api(self.CONTRACT_API, params={'symbol': symbol})
        if data and 'expiryDates' in data:
            self._expiry_dates = data['expiryDates']
            return data['expiryDates']
        return self._expiry_dates

    def get_option_chain(self, symbol='NIFTY', expiry=None, indices=True):
        """
        Fetches option chain data from NSE.

        :param symbol: 'NIFTY', 'BANKNIFTY', etc.
        :param expiry: Specific expiry date string (e.g., '10-Feb-2026').
                       If None, fetches nearest expiry.
        :param indices: True for index options.
        :return: (DataFrame, expiry_dates, underlying_value, timestamp)
        """
        # Get expiry dates first if we don't have them
        if not expiry:
            expiries = self.get_expiry_dates(symbol)
            if expiries:
                expiry = expiries[0]  # Nearest expiry
            else:
                raise ValueError("Could not fetch expiry dates from NSE")

        # Fetch option chain for specific expiry
        params = {
            'type': 'Indices' if indices else 'Equities',
            'symbol': symbol,
            'expiry': expiry,
        }
        data = self._get_api(self.OC_API, params=params)

        if not data or 'records' not in data:
            raise ValueError(f"No option chain data returned for {symbol} {expiry}")

        return self._parse_option_chain(data, expiry)

    def _parse_option_chain(self, data, expiry):
        """Parse raw NSE option chain JSON into a DataFrame."""
        records = data['records']
        timestamp = records.get('timestamp', '')
        underlying_value = records.get('underlyingValue', 0)
        expiry_dates = records.get('expiryDates', self._expiry_dates)

        rows = []
        for item in records.get('data', []):
            row = {
                'strikePrice': item['strikePrice'],
                'expiryDate': item.get('expiryDates', item.get('expiryDate', expiry)),
            }

            # CE (Call) data
            if 'CE' in item:
                ce = item['CE']
                row.update({
                    'CE_OI': ce.get('openInterest', 0),
                    'CE_Chng_OI': ce.get('changeinOpenInterest', 0),
                    'CE_Volume': ce.get('totalTradedVolume', 0),
                    'CE_IV': ce.get('impliedVolatility', 0),
                    'CE_LTP': ce.get('lastPrice', 0),
                    'CE_Open': ce.get('open', 0),
                    'CE_High': ce.get('high', 0),
                    'CE_Low': ce.get('low', 0),
                    'CE_Close': ce.get('close', ce.get('lastPrice', 0)),
                    'CE_pClose': ce.get('pClose', 0),
                    'CE_Change': ce.get('change', 0),
                    'CE_pChange': ce.get('pChange', 0),
                    'CE_BidQty': ce.get('bidQty', ce.get('buyQuantity1', 0)),
                    'CE_BidPrice': ce.get('bidprice', ce.get('buyPrice1', 0)),
                    'CE_AskPrice': ce.get('askPrice', ce.get('sellPrice1', 0)),
                    'CE_AskQty': ce.get('askQty', ce.get('sellQuantity1', 0)),
                })
            else:
                row.update({k: 0 for k in [
                    'CE_OI', 'CE_Chng_OI', 'CE_Volume', 'CE_IV', 'CE_LTP',
                    'CE_Open', 'CE_High', 'CE_Low', 'CE_Close', 'CE_pClose',
                    'CE_Change', 'CE_pChange',
                    'CE_BidQty', 'CE_BidPrice', 'CE_AskPrice', 'CE_AskQty'
                ]})

            # PE (Put) data
            if 'PE' in item:
                pe = item['PE']
                row.update({
                    'PE_OI': pe.get('openInterest', 0),
                    'PE_Chng_OI': pe.get('changeinOpenInterest', 0),
                    'PE_Volume': pe.get('totalTradedVolume', 0),
                    'PE_IV': pe.get('impliedVolatility', 0),
                    'PE_LTP': pe.get('lastPrice', 0),
                    'PE_Open': pe.get('open', 0),
                    'PE_High': pe.get('high', 0),
                    'PE_Low': pe.get('low', 0),
                    'PE_Close': pe.get('close', pe.get('lastPrice', 0)),
                    'PE_pClose': pe.get('pClose', 0),
                    'PE_Change': pe.get('change', 0),
                    'PE_pChange': pe.get('pChange', 0),
                    'PE_BidQty': pe.get('bidQty', pe.get('buyQuantity1', 0)),
                    'PE_BidPrice': pe.get('bidprice', pe.get('buyPrice1', 0)),
                    'PE_AskPrice': pe.get('askPrice', pe.get('sellPrice1', 0)),
                    'PE_AskQty': pe.get('askQty', pe.get('sellQuantity1', 0)),
                })
            else:
                row.update({k: 0 for k in [
                    'PE_OI', 'PE_Chng_OI', 'PE_Volume', 'PE_IV', 'PE_LTP',
                    'PE_Open', 'PE_High', 'PE_Low', 'PE_Close', 'PE_pClose',
                    'PE_Change', 'PE_pChange',
                    'PE_BidQty', 'PE_BidPrice', 'PE_AskPrice', 'PE_AskQty'
                ]})

            rows.append(row)

        df = pd.DataFrame(rows)
        df = df.sort_values('strikePrice').reset_index(drop=True)

        return df, expiry_dates, underlying_value, timestamp

    def get_nifty_weekly_options(self, num_strikes=20):
        """
        Fetches NIFTY 50 weekly options for the nearest expiry.
        Returns a dict with spot_price, atm_strike, expiry, data (DataFrame), etc.
        """
        try:
            df, expiry_dates, spot_price, timestamp = self.get_option_chain(
                'NIFTY', indices=True
            )

            nearest_expiry = expiry_dates[0] if expiry_dates else None
            if not nearest_expiry:
                return None

            # Filter to nearest expiry
            weekly_df = df[df['expiryDate'] == nearest_expiry].copy()

            # Filter strikes around ATM
            atm_strike = round(spot_price / 50) * 50
            lower = atm_strike - (num_strikes * 50)
            upper = atm_strike + (num_strikes * 50)
            weekly_df = weekly_df[
                (weekly_df['strikePrice'] >= lower) &
                (weekly_df['strikePrice'] <= upper)
            ]
            weekly_df = weekly_df.sort_values('strikePrice').reset_index(drop=True)

            return {
                'spot_price': spot_price,
                'atm_strike': atm_strike,
                'expiry': nearest_expiry,
                'timestamp': timestamp,
                'expiry_dates': expiry_dates,
                'data': weekly_df
            }
        except Exception as e:
            print(f"[NSE] Error fetching weekly options: {e}")
            import traceback
            traceback.print_exc()
            return None

    def get_multiple_expiry_options(self, num_expiries=3, num_strikes=15):
        """Fetches NIFTY options for multiple weekly expiries."""
        try:
            expiry_dates = self.get_expiry_dates('NIFTY')
            if not expiry_dates:
                return None

            result = {
                'timestamp': '',
                'expiry_dates': expiry_dates[:5],
                'expiries': {}
            }

            for expiry in expiry_dates[:num_expiries]:
                df, _, spot, ts = self.get_option_chain('NIFTY', expiry=expiry)
                atm = round(spot / 50) * 50
                lo, hi = atm - num_strikes * 50, atm + num_strikes * 50
                filtered = df[
                    (df['strikePrice'] >= lo) & (df['strikePrice'] <= hi)
                ].sort_values('strikePrice').reset_index(drop=True)

                result['expiries'][expiry] = filtered
                result['spot_price'] = spot
                result['atm_strike'] = atm
                result['timestamp'] = ts

            return result
        except Exception as e:
            print(f"[NSE] Error fetching multiple expiry options: {e}")
            return None
