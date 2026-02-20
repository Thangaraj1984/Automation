"""
NIFTY 50 Weekly Options - Flask API Server

Simple Flask server that fetches data from NSE using cookies.
No Playwright hacks in the main process — the browser only opens
briefly to extract cookies (every ~10 min), then all API calls
use plain HTTP requests.
"""

from flask import Flask, jsonify, request
from flask_cors import CORS
from NseUtility import NseUtils
import threading
import time
import math
from datetime import datetime, date

# ===================== CONFIGURATION =====================
REFRESH_INTERVAL = 60      # Seconds between data refreshes
NUM_STRIKES = 20           # Strikes above/below ATM
PORT = 5000
RISK_FREE_RATE = 0.07      # ~7% (India 10Y bond yield)
# =========================================================


# ===================== GREEKS (Black-Scholes) =====================
def _norm_cdf(x):
    """Standard normal CDF using an accurate approximation."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def _norm_pdf(x):
    """Standard normal PDF."""
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)

def calc_greeks(spot, strike, iv, days_to_expiry, option_type='CE', r=RISK_FREE_RATE):
    """
    Calculate Option Greeks using Black-Scholes model.
    Returns dict with delta, gamma, theta, vega.
    """
    try:
        if iv <= 0 or days_to_expiry <= 0 or spot <= 0 or strike <= 0:
            return {'delta': 0, 'gamma': 0, 'theta': 0, 'vega': 0}

        T = days_to_expiry / 365.0
        sigma = iv / 100.0  # IV comes as percentage

        d1 = (math.log(spot / strike) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
        d2 = d1 - sigma * math.sqrt(T)

        gamma = _norm_pdf(d1) / (spot * sigma * math.sqrt(T))
        vega = spot * _norm_pdf(d1) * math.sqrt(T) / 100  # per 1% IV change

        if option_type == 'CE':
            delta = round(_norm_cdf(d1), 4)
            theta = (-(spot * _norm_pdf(d1) * sigma) / (2 * math.sqrt(T))
                     - r * strike * math.exp(-r * T) * _norm_cdf(d2)) / 365
        else:  # PE
            delta = round(_norm_cdf(d1) - 1, 4)
            theta = (-(spot * _norm_pdf(d1) * sigma) / (2 * math.sqrt(T))
                     + r * strike * math.exp(-r * T) * _norm_cdf(-d2)) / 365

        return {
            'delta': round(delta, 4),
            'gamma': round(gamma, 4),
            'theta': round(theta, 2),
            'vega':  round(vega, 2),
        }
    except Exception:
        return {'delta': 0, 'gamma': 0, 'theta': 0, 'vega': 0}

def days_until_expiry(expiry_str):
    """Parse expiry string like '17-Feb-2026' and return days to expiry."""
    try:
        exp = datetime.strptime(expiry_str, '%d-%b-%Y').date()
        return max((exp - date.today()).days, 0)
    except Exception:
        return 0


# ===================== MAX PAIN CALCULATOR =====================
def calc_max_pain(data_df):
    """
    Calculate Max Pain — the strike where option writers lose the LEAST money.
    At expiry, options gravitate toward this price.
    """
    strikes = sorted(data_df['strikePrice'].unique())
    min_pain = float('inf')
    max_pain_strike = 0

    for test_strike in strikes:
        total_pain = 0
        for _, row in data_df.iterrows():
            s = row['strikePrice']
            ce_oi = row.get('CE_OI', 0)
            pe_oi = row.get('PE_OI', 0)

            # CE writers' loss: max(0, test_strike - strike) * CE_OI
            if test_strike > s:
                total_pain += (test_strike - s) * ce_oi
            # PE writers' loss: max(0, strike - test_strike) * PE_OI
            if test_strike < s:
                total_pain += (s - test_strike) * pe_oi

        if total_pain < min_pain:
            min_pain = total_pain
            max_pain_strike = test_strike

    return int(max_pain_strike)


# ===================== OI SUPPORT/RESISTANCE =====================
def calc_oi_levels(data_df, spot, num_levels=3):
    """
    Find Support & Resistance levels from OI walls.
    - Highest PE OI strikes BELOW spot = Support (put writers defend)
    - Highest CE OI strikes ABOVE spot = Resistance (call writers defend)
    Returns: { 'support': [list], 'resistance': [list] }
    """
    # Support = highest PE OI below/at spot
    pe_below = data_df[data_df['strikePrice'] <= spot].nlargest(num_levels, 'PE_OI')
    support = sorted(pe_below['strikePrice'].tolist())

    # Resistance = highest CE OI above/at spot
    ce_above = data_df[data_df['strikePrice'] >= spot].nlargest(num_levels, 'CE_OI')
    resistance = sorted(ce_above['strikePrice'].tolist())

    return {
        'support': [int(s) for s in support],
        'resistance': [int(r) for r in resistance],
    }


# ===================== BID-ASK SPREAD ANALYSIS =====================
def calc_bid_ask_pressure(row):
    """
    Analyze bid-ask data for demand/supply pressure.
    Returns dict with ce_pressure, pe_pressure (-1 to +1 scale).
    +1 = strong buying pressure, -1 = strong selling pressure.
    """
    # CE bid-ask pressure
    ce_bid = float(row.get('CE_BidQty', 0))
    ce_ask = float(row.get('CE_AskQty', 0))
    if ce_bid + ce_ask > 0:
        ce_pressure = round((ce_bid - ce_ask) / (ce_bid + ce_ask), 2)
    else:
        ce_pressure = 0

    # PE bid-ask pressure
    pe_bid = float(row.get('PE_BidQty', 0))
    pe_ask = float(row.get('PE_AskQty', 0))
    if pe_bid + pe_ask > 0:
        pe_pressure = round((pe_bid - pe_ask) / (pe_bid + pe_ask), 2)
    else:
        pe_pressure = 0

    # Bid-ask spread (% of LTP) — lower = more liquid
    ce_ltp = float(row.get('CE_LTP', 1)) or 1
    pe_ltp = float(row.get('PE_LTP', 1)) or 1
    ce_spread = round((float(row.get('CE_AskPrice', 0)) - float(row.get('CE_BidPrice', 0))) / ce_ltp * 100, 2)
    pe_spread = round((float(row.get('PE_AskPrice', 0)) - float(row.get('PE_BidPrice', 0))) / pe_ltp * 100, 2)

    return {
        'ce_pressure': ce_pressure, 'pe_pressure': pe_pressure,
        'ce_spread': ce_spread, 'pe_spread': pe_spread,
    }
# ==================================================================


# Shared data store
data_store = {
    'weekly': None,
    'last_fetch': None,
    'last_error': None,
    'fetch_count': 0,
}
store_lock = threading.Lock()

# Shared NseUtils instance — used by both background fetcher and OHLC endpoint
# Ensures cookies are always warm (background fetcher keeps them fresh)
_shared_nse = None
_shared_nse_lock = threading.Lock()

# ===================== FLASK APP =====================
app = Flask(__name__)
CORS(app)


@app.route('/')
def home():
    with store_lock:
        has_data = data_store['weekly'] is not None
    return jsonify({
        'status': 'running',
        'has_data': has_data,
        'last_fetch': data_store['last_fetch'],
        'fetch_count': data_store['fetch_count'],
        'endpoints': {
            '/api/options/sheets': 'Google Sheets formatted data (with signals)',
            '/api/options': 'Full JSON options data',
            '/api/options/daily-ohlc': 'Daily OHLC data for all strikes',
            '/api/spot': 'NIFTY spot price',
            '/api/expiries': 'Available expiry dates',
            '/api/health': 'Server health check',
        },
        'server_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    })


@app.route('/api/options')
def get_options():
    with store_lock:
        result = data_store['weekly']
    if not result:
        return jsonify({'error': 'Data not yet available. Please wait...'}), 503
    return jsonify({
        'status': 'success',
        'spot_price': result['spot_price'],
        'atm_strike': result['atm_strike'],
        'expiry': result['expiry'],
        'timestamp': result['timestamp'],
        'expiry_dates': result['expiry_dates'],
        'data': result['data'].to_dict(orient='records')
    })


@app.route('/api/options/sheets')
def get_options_sheets():
    """Google Sheets optimized format."""
    with store_lock:
        result = data_store['weekly']
    if not result:
        return jsonify({'error': 'Data not yet available'}), 503

    data_df = result['data']
    atm = result['atm_strike']

    # Respect strikes parameter from Google Sheets
    num_strikes = request.args.get('strikes', NUM_STRIKES, type=int)
    if num_strikes < len(data_df) // 2:
        atm_idx = data_df[data_df['strikePrice'] == atm].index
        if len(atm_idx) > 0:
            center = atm_idx[0]
            start = max(0, center - num_strikes)
            end = min(len(data_df), center + num_strikes + 1)
            data_df = data_df.iloc[start:end].reset_index(drop=True)

    headers = [
        'Buildup',
        'OI', 'Chng OI', 'Volume', 'IV', 'LTP',
        'Change', 'Delta', 'Gamma', 'Theta', 'Vega',
        'Strike',
        'Delta', 'Gamma', 'Theta', 'Vega',
        'Change', 'LTP',
        'IV', 'Volume', 'Chng OI', 'OI',
        'Buildup', 'Signal'
    ]

    def get_buildup(ltp, open_price, prev_close_change, oi_change):
        """
        Determine buildup type using INTRADAY price direction + OI change.

        Professional/Institutional logic:
        - Price direction = LTP vs Open (intraday), NOT vs yesterday
        - OI direction = changeinOpenInterest (vs yesterday) — this IS correct
        - Fallback to prev close change if Open is 0 (pre-market/no trade)

        Buildup types:
        - Long Buildup:    Price UP   + OI UP   → Fresh buying (new longs)
        - Short Buildup:   Price DOWN + OI UP   → Fresh selling (new shorts)
        - Short Covering:  Price UP   + OI DOWN → Sellers panicking (buying back)
        - Long Unwinding:  Price DOWN + OI DOWN → Buyers exiting (profit booking)
        """
        # Use intraday direction (LTP vs Open) as primary
        if open_price > 0 and ltp > 0:
            price_direction = ltp - open_price
        else:
            # Fallback: use change from previous close
            price_direction = prev_close_change

        if price_direction > 0 and oi_change > 0:
            return 'Long Buildup'
        elif price_direction < 0 and oi_change > 0:
            return 'Short Buildup'
        elif price_direction > 0 and oi_change < 0:
            return 'Short Covering'
        elif price_direction < 0 and oi_change < 0:
            return 'Long Unwinding'
        return '-'

    def get_buy_signal(ce_buildup, pe_buildup, ce_greeks, pe_greeks,
                       ce_iv, pe_iv, ce_oi_chg, pe_oi_chg, ce_vol, pe_vol,
                       strike, spot, market_direction, extras):
        """
        SELLER PANIC STRATEGY — Signal when option WRITERS are panicking or
        booking profits, creating momentum for option BUYERS to enter.

        BUY CE when:
          - CE sellers are SHORT COVERING (panic buying back CEs) → resistance breaking
          - PE sellers are adding SHORT BUILDUP (new PE writing) → building floor below
          - Confirmed by volume surge, favorable delta, low IV

        BUY PE when:
          - PE sellers are SHORT COVERING (panic buying back PEs) → support breaking
          - CE sellers are adding SHORT BUILDUP (new CE writing) → building ceiling above
          - Confirmed by volume surge, favorable delta, low IV

        Returns: (signal_text, reason_note) tuple
        """
        reasons = []
        score = 0
        signal_type = None  # 'CE' or 'PE'

        ba = extras.get('bid_ask', {})
        max_pain = extras.get('max_pain', 0)
        support = extras.get('support', [])
        resistance = extras.get('resistance', [])

        # ============================================================
        # SCENARIO A: CE SHORT COVERING → Buy CE
        # CE writers panicking (price ↑, OI ↓) = resistance breaking
        # ============================================================
        ce_chg = extras.get('ce_price_chg', 0)
        pe_chg = extras.get('pe_price_chg', 0)
        ce_ltp = extras.get('ce_ltp', 0)
        pe_ltp = extras.get('pe_ltp', 0)
        ce_open = extras.get('ce_open', 0)
        pe_open = extras.get('pe_open', 0)
        ce_total_oi = extras.get('ce_oi', 0)
        pe_total_oi = extras.get('pe_oi', 0)
        # Intraday move (LTP vs Open)
        ce_intraday = round(ce_ltp - ce_open, 2) if ce_open > 0 else ce_chg
        pe_intraday = round(pe_ltp - pe_open, 2) if pe_open > 0 else pe_chg

        if ce_buildup == 'Short Covering':
            signal_type = 'CE'
            score += 3
            reasons.append(f'⚡ CE SELLERS PANICKING (Short Covering) (+3)')
            reasons.append(f'   CE Open: ₹{ce_open} → LTP: ₹{ce_ltp} ({ce_intraday:+.2f} intraday)')
            reasons.append(f'   CE OI: {ce_total_oi:,} (Chg: {ce_oi_chg:,}) — OI decreasing = sellers exiting')

            # Confirmation: PE writers adding positions (building floor)
            if pe_buildup == 'Short Buildup':
                score += 2
                reasons.append(f'✅ PE sellers writing new puts (Short Buildup) (+2)')
                reasons.append(f'   PE Open: ₹{pe_open} → LTP: ₹{pe_ltp} ({pe_intraday:+.2f}) | PE OI: +{pe_oi_chg:,} = support floor')
            elif pe_buildup == 'Long Unwinding':
                score += 1
                reasons.append(f'PE holders exiting (Long Unwinding) — less downside pressure (+1)')

            # Confirmation: CE OI dropping fast (sellers leaving)
            if ce_oi_chg < -500:
                score += 1
                reasons.append(f'CE OI dropping fast ({ce_oi_chg:,}) — mass seller exit (+1)')

            # Volume surge = momentum
            if ce_vol > 1000:
                score += 1
                reasons.append(f'CE Volume surge ({ce_vol:,}) — buying momentum (+1)')

            # Bid pressure = buyers aggressive
            if ba.get('ce_pressure', 0) > 0.2:
                score += 1
                reasons.append(f'CE buyers aggressive (bid pressure {ba["ce_pressure"]:+.2f}) (+1)')

        # ============================================================
        # SCENARIO B: PE Long Unwinding → Buy CE (secondary)
        # PE sellers booking profits (PE price ↓, OI ↓) = floor removed → upside clear
        # ============================================================
        elif pe_buildup == 'Long Unwinding' and ce_buildup in ('Long Buildup', 'Short Covering'):
            signal_type = 'CE'
            score += 2
            reasons.append(f'📊 PE SELLERS BOOKING PROFITS (Long Unwinding) (+2)')
            reasons.append(f'   PE Open: ₹{pe_open} → LTP: ₹{pe_ltp} ({pe_intraday:+.2f}) — sellers closing')
            reasons.append(f'   PE OI: {pe_total_oi:,} (Chg: {pe_oi_chg:,}) — OI decreasing = sellers exiting')
            if ce_buildup == 'Short Covering':
                score += 2
                reasons.append(f'⚡ CE sellers also panicking (Short Covering) (+2)')
                reasons.append(f'   CE Open: ₹{ce_open} → LTP: ₹{ce_ltp} ({ce_intraday:+.2f}) | CE OI Chg: {ce_oi_chg:,}')
            elif ce_buildup == 'Long Buildup':
                score += 1
                reasons.append(f'Fresh CE buying (Long Buildup) — CE LTP: ₹{ce_ltp} (+1)')

            if ce_vol > 1000:
                score += 1
                reasons.append(f'CE Volume surge ({ce_vol:,}) — buyers active (+1)')

        # ============================================================
        # SCENARIO C: PE SHORT COVERING → Buy PE
        # PE writers panicking (price ↑, OI ↓) = support breaking
        # ============================================================
        elif pe_buildup == 'Short Covering':
            signal_type = 'PE'
            score += 3
            reasons.append(f'⚡ PE SELLERS PANICKING (Short Covering) (+3)')
            reasons.append(f'   PE Open: ₹{pe_open} → LTP: ₹{pe_ltp} ({pe_intraday:+.2f} intraday)')
            reasons.append(f'   PE OI: {pe_total_oi:,} (Chg: {pe_oi_chg:,}) — OI decreasing = sellers exiting')

            # Confirmation: CE writers adding positions (building ceiling)
            if ce_buildup == 'Short Buildup':
                score += 2
                reasons.append(f'✅ CE sellers writing new calls (Short Buildup) (+2)')
                reasons.append(f'   CE Open: ₹{ce_open} → LTP: ₹{ce_ltp} ({ce_intraday:+.2f}) | CE OI: +{ce_oi_chg:,} = resistance ceiling')
            elif ce_buildup == 'Long Unwinding':
                score += 1
                reasons.append(f'CE holders exiting (Long Unwinding) — less upside pressure (+1)')

            # Confirmation: PE OI dropping fast (sellers leaving)
            if pe_oi_chg < -500:
                score += 1
                reasons.append(f'PE OI dropping fast ({pe_oi_chg:,}) — mass seller exit (+1)')

            # Volume surge
            if pe_vol > 1000:
                score += 1
                reasons.append(f'PE Volume surge ({pe_vol:,}) — selling momentum (+1)')

            # Bid pressure = put buyers aggressive
            if ba.get('pe_pressure', 0) > 0.2:
                score += 1
                reasons.append(f'PE buyers aggressive (bid pressure {ba["pe_pressure"]:+.2f}) (+1)')

        # ============================================================
        # SCENARIO D: CE Long Unwinding → Buy PE (secondary)
        # CE sellers booking profits (CE price ↓, OI ↓) = ceiling removed → downside clear
        # ============================================================
        elif ce_buildup == 'Long Unwinding' and pe_buildup in ('Long Buildup', 'Short Covering'):
            signal_type = 'PE'
            score += 2
            reasons.append(f'📊 CE SELLERS BOOKING PROFITS (Long Unwinding) (+2)')
            reasons.append(f'   CE Open: ₹{ce_open} → LTP: ₹{ce_ltp} ({ce_intraday:+.2f}) — sellers closing')
            reasons.append(f'   CE OI: {ce_total_oi:,} (Chg: {ce_oi_chg:,}) — OI decreasing = sellers exiting')
            if pe_buildup == 'Short Covering':
                score += 2
                reasons.append(f'⚡ PE sellers also panicking (Short Covering) (+2)')
                reasons.append(f'   PE Open: ₹{pe_open} → LTP: ₹{pe_ltp} ({pe_intraday:+.2f}) | PE OI Chg: {pe_oi_chg:,}')
            elif pe_buildup == 'Long Buildup':
                score += 1
                reasons.append(f'Fresh PE buying (Long Buildup) — PE LTP: ₹{pe_ltp} (+1)')

            if pe_vol > 1000:
                score += 1
                reasons.append(f'PE Volume surge ({pe_vol:,}) — sellers active (+1)')

        # No seller panic detected at this strike
        if signal_type is None:
            return '', ''

        # ============================================================
        # QUALITY FILTERS (apply to both CE and PE)
        # ============================================================
        greeks = ce_greeks if signal_type == 'CE' else pe_greeks
        iv = ce_iv if signal_type == 'CE' else pe_iv
        delta = abs(greeks['delta'])

        # Delta must be in tradeable range (0.20-0.65)
        if not (0.20 <= delta <= 0.65):
            return '', ''

        # IV penalty — don't buy overpriced options
        if iv > 30:
            score -= 2
            reasons.append(f'IV very high ({iv}%) — premium costly (-2)')
        elif iv > 20:
            score -= 1
            reasons.append(f'IV elevated ({iv}%) — slightly costly (-1)')

        # Liquidity check
        spread = ba.get('ce_spread' if signal_type == 'CE' else 'pe_spread', 0)
        if spread > 5:
            score -= 1
            reasons.append(f'Spread wide ({spread:.1f}%) — poor liquidity (-1)')

        # Max Pain confirmation
        if max_pain > 0:
            if signal_type == 'CE' and strike <= max_pain + 150:
                score += 1
                reasons.append(f'Strike {strike} within Max Pain {max_pain} range (+1)')
            elif signal_type == 'PE' and strike >= max_pain - 150:
                score += 1
                reasons.append(f'Strike {strike} within Max Pain {max_pain} range (+1)')

        # S/R proximity
        if signal_type == 'CE' and any(abs(strike - s) <= 50 for s in support):
            score += 1
            reasons.append(f'Strike near OI support — safe entry (+1)')
        if signal_type == 'PE' and any(abs(strike - r) <= 50 for r in resistance):
            score += 1
            reasons.append(f'Strike near OI resistance — safe entry (+1)')

        # ============================================================
        # FINAL SIGNAL — need score ≥ 3
        # ============================================================
        if score < 3:
            return '', ''

        if signal_type == 'CE':
            if score >= 5:
                label = '\U0001f7e2 Strong Buy CE'
            else:
                label = '\U0001f7e2 Buy CE'
        else:
            if score >= 5:
                label = '\U0001f534 Strong Buy PE'
            else:
                label = '\U0001f534 Buy PE'

        # Build detailed panic summary header
        if signal_type == 'CE':
            panic_summary = (f"🚨 CE SELLER PANIC DETECTED\n"
                             f"CE Open: ₹{ce_open} → LTP: ₹{ce_ltp} ({ce_intraday:+.2f} intraday)\n"
                             f"CE OI: {ce_total_oi:,} (Chg: {ce_oi_chg:,}) — sellers exiting\n")
        else:
            panic_summary = (f"🚨 PE SELLER PANIC DETECTED\n"
                             f"PE Open: ₹{pe_open} → LTP: ₹{pe_ltp} ({pe_intraday:+.2f} intraday)\n"
                             f"PE OI: {pe_total_oi:,} (Chg: {pe_oi_chg:,}) — sellers exiting\n")

        note = (f"{panic_summary}"
                f"{'─' * 35}\n"
                f"Signal: {label} | Score: {score}\n"
                f"Strike: {strike} | Spot: {spot}\n"
                f"{'─' * 35}\n"
                f"Greeks:\n"
                f"  Delta: {greeks['delta']} | Gamma: {greeks['gamma']}\n"
                f"  Theta: {greeks['theta']}/day | Vega: {greeks['vega']}\n"
                f"  IV: {iv}%\n"
                f"{'─' * 35}\n"
                f"Levels:\n"
                f"  Max Pain: {max_pain}\n"
                f"  Support: {support}\n"
                f"  Resistance: {resistance}\n"
                f"{'─' * 35}\n"
                f"Why this signal:\n" +
                '\n'.join(reasons))
        return label, note

    # Counters for overall market sentiment
    atm = result['atm_strike']
    spot = result['spot_price']
    dte = days_until_expiry(result['expiry'])

    # ---- Pre-compute analytics from FULL dataset ----
    max_pain_strike = calc_max_pain(data_df)
    oi_levels = calc_oi_levels(data_df, spot)

    # ---- PASS 1: Build rows + count buildups (no signals yet) ----
    rows = []
    row_extras = []  # store per-row data needed for signal pass

    # Sentiment counters — ONLY from near-ATM strikes (±5 strikes = ±250 pts)
    atm_sentiment = {
        'ce_long_buildup': 0, 'ce_short_buildup': 0,
        'ce_short_covering': 0, 'ce_long_unwinding': 0,
        'pe_long_buildup': 0, 'pe_short_buildup': 0,
        'pe_short_covering': 0, 'pe_long_unwinding': 0,
    }
    # Track ATM option price changes for actual market direction
    atm_ce_change = 0
    atm_pe_change = 0
    atm_ce_oi_chg = 0
    atm_pe_oi_chg = 0
    # Intraday position tracking
    atm_ce_open = 0
    atm_ce_high = 0
    atm_ce_low = 0
    atm_ce_ltp = 0
    atm_pe_open = 0
    atm_pe_high = 0
    atm_pe_low = 0
    atm_pe_ltp = 0

    for _, row in data_df.iterrows():
        ce_buildup = get_buildup(
            float(row['CE_LTP']), float(row.get('CE_Open', 0)),
            float(row['CE_Change']), int(row['CE_Chng_OI'])
        )
        pe_buildup = get_buildup(
            float(row['PE_LTP']), float(row.get('PE_Open', 0)),
            float(row['PE_Change']), int(row['PE_Chng_OI'])
        )

        strike = int(row['strikePrice'])

        # Only count near-ATM strikes for sentiment (±250 points = ±5 strikes)
        if abs(strike - atm) <= 250:
            key_map = {'Long Buildup': 'long_buildup', 'Short Buildup': 'short_buildup',
                       'Short Covering': 'short_covering', 'Long Unwinding': 'long_unwinding'}
            if ce_buildup in key_map:
                atm_sentiment['ce_' + key_map[ce_buildup]] += 1
            if pe_buildup in key_map:
                atm_sentiment['pe_' + key_map[pe_buildup]] += 1

        # Capture exact ATM strike option price changes
        if strike == atm:
            atm_ce_change = float(row['CE_Change'])
            atm_pe_change = float(row['PE_Change'])
            atm_ce_oi_chg = int(row['CE_Chng_OI'])
            atm_pe_oi_chg = int(row['PE_Chng_OI'])
            atm_ce_open = float(row.get('CE_Open', 0))
            atm_ce_high = float(row.get('CE_High', 0))
            atm_ce_low = float(row.get('CE_Low', 0))
            atm_ce_ltp = float(row.get('CE_LTP', 0))
            atm_pe_open = float(row.get('PE_Open', 0))
            atm_pe_high = float(row.get('PE_High', 0))
            atm_pe_low = float(row.get('PE_Low', 0))
            atm_pe_ltp = float(row.get('PE_LTP', 0))

        ce_iv = float(row['CE_IV'])
        pe_iv = float(row['PE_IV'])

        ce_greeks = calc_greeks(spot, strike, ce_iv, dte, 'CE')
        pe_greeks = calc_greeks(spot, strike, pe_iv, dte, 'PE')

        # Bid-Ask analysis for this strike
        bid_ask = calc_bid_ask_pressure(row)

        rows.append([
            ce_buildup,
            int(row['CE_OI']), int(row['CE_Chng_OI']), int(row['CE_Volume']),
            round(ce_iv, 2), round(float(row['CE_LTP']), 2),
            round(float(row['CE_Change']), 2),
            ce_greeks['delta'], ce_greeks['gamma'], ce_greeks['theta'], ce_greeks['vega'],
            strike,
            pe_greeks['delta'], pe_greeks['gamma'], pe_greeks['theta'], pe_greeks['vega'],
            round(float(row['PE_Change']), 2), round(float(row['PE_LTP']), 2),
            round(pe_iv, 2), int(row['PE_Volume']),
            int(row['PE_Chng_OI']), int(row['PE_OI']),
            pe_buildup,
            ''  # placeholder for signal
        ])
        row_extras.append({
            'ce_buildup': ce_buildup, 'pe_buildup': pe_buildup,
            'ce_greeks': ce_greeks, 'pe_greeks': pe_greeks,
            'ce_iv': ce_iv, 'pe_iv': pe_iv,
            'ce_oi_chg': int(row['CE_Chng_OI']), 'pe_oi_chg': int(row['PE_Chng_OI']),
            'ce_vol': int(row['CE_Volume']), 'pe_vol': int(row['PE_Volume']),
            'strike': strike,
            'bid_ask': bid_ask,
            'ce_pchange': float(row.get('CE_pChange', 0)),
            'pe_pchange': float(row.get('PE_pChange', 0)),
            'ce_price_chg': round(float(row['CE_Change']), 2),
            'pe_price_chg': round(float(row['PE_Change']), 2),
            'ce_ltp': round(float(row['CE_LTP']), 2),
            'pe_ltp': round(float(row['PE_LTP']), 2),
            'ce_open': round(float(row.get('CE_Open', 0)), 2),
            'pe_open': round(float(row.get('PE_Open', 0)), 2),
            'ce_oi': int(row['CE_OI']),
            'pe_oi': int(row['PE_OI']),
            'max_pain': max_pain_strike,
            'support': oi_levels['support'],
            'resistance': oi_levels['resistance'],
        })

    total_ce_oi = int(data_df['CE_OI'].sum())
    total_pe_oi = int(data_df['PE_OI'].sum())
    total_ce_vol = int(data_df['CE_Volume'].sum())
    total_pe_vol = int(data_df['PE_Volume'].sum())

    # ---- Determine market sentiment — INSTANT (no refresh history needed) ----
    bullish_points = 0
    bearish_points = 0
    sentiment_reasons = []

    # ======= Factor 1: ATM CE LTP vs Open (weight=3) =======
    # CE above today's open = market rose intraday, below = fell intraday
    if atm_ce_open > 0 and atm_ce_ltp > 0:
        ce_vs_open = atm_ce_ltp - atm_ce_open
        ce_vs_open_pct = (ce_vs_open / atm_ce_open) * 100
        if ce_vs_open_pct > 3:
            bullish_points += 3
            sentiment_reasons.append(f'ATM CE up {ce_vs_open_pct:+.1f}% from open ({atm_ce_open}→{atm_ce_ltp}) → Bullish (+3)')
        elif ce_vs_open_pct > 1:
            bullish_points += 2
            sentiment_reasons.append(f'ATM CE up {ce_vs_open_pct:+.1f}% from open → Mildly Bullish (+2)')
        elif ce_vs_open_pct < -3:
            bearish_points += 3
            sentiment_reasons.append(f'ATM CE down {ce_vs_open_pct:+.1f}% from open ({atm_ce_open}→{atm_ce_ltp}) → Bearish (+3)')
        elif ce_vs_open_pct < -1:
            bearish_points += 2
            sentiment_reasons.append(f'ATM CE down {ce_vs_open_pct:+.1f}% from open → Mildly Bearish (+2)')
        else:
            sentiment_reasons.append(f'ATM CE flat vs open ({ce_vs_open_pct:+.1f}%)')

    # ======= Factor 2: ATM PE LTP vs Open (weight=3) =======
    # PE below today's open = market rose (puts lost value), above = fell
    if atm_pe_open > 0 and atm_pe_ltp > 0:
        pe_vs_open = atm_pe_ltp - atm_pe_open
        pe_vs_open_pct = (pe_vs_open / atm_pe_open) * 100
        if pe_vs_open_pct < -3:
            bullish_points += 3
            sentiment_reasons.append(f'ATM PE down {pe_vs_open_pct:+.1f}% from open — puts losing → Bullish (+3)')
        elif pe_vs_open_pct < -1:
            bullish_points += 2
            sentiment_reasons.append(f'ATM PE down {pe_vs_open_pct:+.1f}% from open → Mildly Bullish (+2)')
        elif pe_vs_open_pct > 3:
            bearish_points += 3
            sentiment_reasons.append(f'ATM PE up {pe_vs_open_pct:+.1f}% from open — puts gaining → Bearish (+3)')
        elif pe_vs_open_pct > 1:
            bearish_points += 2
            sentiment_reasons.append(f'ATM PE up {pe_vs_open_pct:+.1f}% from open → Mildly Bearish (+2)')
        else:
            sentiment_reasons.append(f'ATM PE flat vs open ({pe_vs_open_pct:+.1f}%)')

    # ======= Factor 3: ATM CE position in day range (weight=3) =======
    # Near High = bullish momentum, Near Low = bearish momentum
    if atm_ce_high > atm_ce_low and atm_ce_high > 0:
        ce_range = atm_ce_high - atm_ce_low
        if ce_range > 0:
            ce_position = (atm_ce_ltp - atm_ce_low) / ce_range
            if ce_position > 0.7:
                bullish_points += 3
                sentiment_reasons.append(f'ATM CE near day high ({ce_position:.0%} of range) → Bullish (+3)')
            elif ce_position < 0.3:
                bearish_points += 3
                sentiment_reasons.append(f'ATM CE near day low ({ce_position:.0%} of range) → Bearish (+3)')
            else:
                sentiment_reasons.append(f'ATM CE mid-range ({ce_position:.0%})')

    # ======= Factor 4: ATM PE position in day range (weight=3) =======
    # PE near High = bearish, PE near Low = bullish
    if atm_pe_high > atm_pe_low and atm_pe_high > 0:
        pe_range = atm_pe_high - atm_pe_low
        if pe_range > 0:
            pe_position = (atm_pe_ltp - atm_pe_low) / pe_range
            if pe_position > 0.7:
                bearish_points += 3
                sentiment_reasons.append(f'ATM PE near day high ({pe_position:.0%} of range) → Bearish (+3)')
            elif pe_position < 0.3:
                bullish_points += 3
                sentiment_reasons.append(f'ATM PE near day low ({pe_position:.0%} of range) → Bullish (+3)')
            else:
                sentiment_reasons.append(f'ATM PE mid-range ({pe_position:.0%})')

    # ======= Factor 5: Near-ATM OI writing pattern (weight=2) =======
    # CE Short Buildup near ATM = writers selling CE = resistance = BEARISH
    # PE Short Buildup near ATM = writers selling PE = support = BULLISH
    if atm_sentiment['ce_short_buildup'] > atm_sentiment['ce_long_buildup']:
        bearish_points += 2
        sentiment_reasons.append(f'Near-ATM CE writers active ({atm_sentiment["ce_short_buildup"]} short) → Bearish (+2)')
    elif atm_sentiment['ce_long_buildup'] > atm_sentiment['ce_short_buildup']:
        bullish_points += 1
        sentiment_reasons.append(f'Near-ATM CE buyers active ({atm_sentiment["ce_long_buildup"]} long) → Bullish (+1)')

    if atm_sentiment['pe_short_buildup'] > atm_sentiment['pe_long_buildup']:
        bullish_points += 2
        sentiment_reasons.append(f'Near-ATM PE writers active ({atm_sentiment["pe_short_buildup"]} short) → Bullish (+2)')
    elif atm_sentiment['pe_long_buildup'] > atm_sentiment['pe_short_buildup']:
        bearish_points += 1
        sentiment_reasons.append(f'Near-ATM PE buyers active ({atm_sentiment["pe_long_buildup"]} long) → Bearish (+1)')

    # ======= Factor 6: PCR (weight=2) =======
    pcr_oi = round(total_pe_oi / total_ce_oi, 2) if total_ce_oi > 0 else 0
    if pcr_oi > 1.2:
        bullish_points += 2
        sentiment_reasons.append(f'PCR {pcr_oi} > 1.2 (PE writers dominant) → Bullish (+2)')
    elif pcr_oi < 0.7:
        bearish_points += 2
        sentiment_reasons.append(f'PCR {pcr_oi} < 0.7 (CE writers dominant) → Bearish (+2)')
    elif pcr_oi > 1.0:
        bullish_points += 1
        sentiment_reasons.append(f'PCR {pcr_oi} > 1.0 → Mildly Bullish (+1)')
    elif pcr_oi < 1.0:
        bearish_points += 1
        sentiment_reasons.append(f'PCR {pcr_oi} < 1.0 → Mildly Bearish (+1)')

    # ======= Factor 7: ATM OI change imbalance (weight=1) =======
    if atm_ce_oi_chg > atm_pe_oi_chg * 1.5 and atm_ce_oi_chg > 0:
        bearish_points += 1
        sentiment_reasons.append(f'ATM CE OI adding fast ({atm_ce_oi_chg:,}) = resistance → Bearish (+1)')
    elif atm_pe_oi_chg > atm_ce_oi_chg * 1.5 and atm_pe_oi_chg > 0:
        bullish_points += 1
        sentiment_reasons.append(f'ATM PE OI adding fast ({atm_pe_oi_chg:,}) = support → Bullish (+1)')

    # ======= Factor 8: Max Pain vs Spot (weight=2) =======
    if max_pain_strike > 0:
        if spot < max_pain_strike - 100:
            bullish_points += 2
            sentiment_reasons.append(f'Spot ({spot}) below Max Pain ({max_pain_strike}) — pull up expected → Bullish (+2)')
        elif spot > max_pain_strike + 100:
            bearish_points += 2
            sentiment_reasons.append(f'Spot ({spot}) above Max Pain ({max_pain_strike}) — pull down expected → Bearish (+2)')
        else:
            sentiment_reasons.append(f'Spot near Max Pain ({max_pain_strike}) — range-bound')

    # ======= Factor 9: OI walls — imbalance (weight=1) =======
    highest_ce_oi = int(data_df.loc[data_df['strikePrice'] >= spot, 'CE_OI'].max()) if len(data_df[data_df['strikePrice'] >= spot]) > 0 else 0
    highest_pe_oi = int(data_df.loc[data_df['strikePrice'] <= spot, 'PE_OI'].max()) if len(data_df[data_df['strikePrice'] <= spot]) > 0 else 0
    if highest_pe_oi > highest_ce_oi * 1.3:
        bullish_points += 1
        sentiment_reasons.append(f'PE OI wall ({highest_pe_oi:,}) > CE wall ({highest_ce_oi:,}) — strong support → Bullish (+1)')
    elif highest_ce_oi > highest_pe_oi * 1.3:
        bearish_points += 1
        sentiment_reasons.append(f'CE OI wall ({highest_ce_oi:,}) > PE wall ({highest_pe_oi:,}) — strong resistance → Bearish (+1)')

    # Need clear edge (at least 2 points more) to declare direction
    if bullish_points >= bearish_points + 2:
        market_sentiment = 'BULLISH'
    elif bearish_points >= bullish_points + 2:
        market_sentiment = 'BEARISH'
    else:
        market_sentiment = 'NEUTRAL'

    sentiment_reasons.insert(0, f'RESULT: {market_sentiment} (Bull:{bullish_points} vs Bear:{bearish_points})')

    # ---- PASS 2: Generate signals using market direction ----
    signal_notes = {}  # {row_index: reason_note}
    for i, extras in enumerate(row_extras):
        signal, note = get_buy_signal(
            extras['ce_buildup'], extras['pe_buildup'],
            extras['ce_greeks'], extras['pe_greeks'],
            extras['ce_iv'], extras['pe_iv'],
            extras['ce_oi_chg'], extras['pe_oi_chg'],
            extras['ce_vol'], extras['pe_vol'],
            extras['strike'], spot,
            market_sentiment, extras
        )
        rows[i][23] = signal  # col 24 (index 23) = Signal
        if note:
            signal_notes[i] = note

    return jsonify({
        'status': 'success',
        'metadata': {
            'spot_price': result['spot_price'],
            'atm_strike': atm,
            'expiry': result['expiry'],
            'timestamp': result['timestamp'],
            'total_ce_oi': total_ce_oi,
            'total_pe_oi': total_pe_oi,
            'total_ce_volume': total_ce_vol,
            'total_pe_volume': total_pe_vol,
            'pcr_oi': round(total_pe_oi / total_ce_oi, 2) if total_ce_oi > 0 else 0,
            'pcr_volume': round(total_pe_vol / total_ce_vol, 2) if total_ce_vol > 0 else 0,
            'expiry_dates': result['expiry_dates'],
            'market_sentiment': market_sentiment,
            'sentiment': atm_sentiment,
            'sentiment_reasons': sentiment_reasons,
            'max_pain': max_pain_strike,
            'oi_support': oi_levels['support'],
            'oi_resistance': oi_levels['resistance'],
        },
        'headers': headers,
        'rows': rows,
        'signal_notes': signal_notes
    })


@app.route('/api/options/daily-ohlc')
def get_daily_ohlc():
    """Fetches fresh OHLC data for all strikes (for daily close capture).
    Call this after 8 PM IST when NSE updates close prices.
    Query params: expiry (optional) - specific expiry date.
    """
    global _shared_nse
    with _shared_nse_lock:
        if _shared_nse is None:
            _shared_nse = NseUtils(headless=True)

    try:
        req_expiry = request.args.get('expiry', None)
        df, expiry_dates, spot, timestamp = _shared_nse.get_option_chain(
            'NIFTY', expiry=req_expiry, indices=True
        )

        # Use the first (nearest) expiry if none specified
        nearest = expiry_dates[0] if expiry_dates else ''
        used_expiry = req_expiry or nearest

        # Filter to requested expiry only
        filtered = df[df['expiryDate'] == used_expiry].copy()
        filtered = filtered.sort_values('strikePrice').reset_index(drop=True)

        # Build rows: Date, Expiry, Strike, CE OHLC, CE OI, CE Vol, CE IV,
        #             PE OHLC, PE OI, PE Vol, PE IV
        today = datetime.now().strftime('%Y-%m-%d')
        rows = []
        for _, r in filtered.iterrows():
            rows.append([
                today,
                used_expiry,
                int(r['strikePrice']),
                round(float(r.get('CE_Open', 0)), 2),
                round(float(r.get('CE_High', 0)), 2),
                round(float(r.get('CE_Low', 0)), 2),
                round(float(r.get('CE_Close', 0)), 2),
                int(r.get('CE_OI', 0)),
                int(r.get('CE_Volume', 0)),
                round(float(r.get('CE_IV', 0)), 2),
                round(float(r.get('PE_Open', 0)), 2),
                round(float(r.get('PE_High', 0)), 2),
                round(float(r.get('PE_Low', 0)), 2),
                round(float(r.get('PE_Close', 0)), 2),
                int(r.get('PE_OI', 0)),
                int(r.get('PE_Volume', 0)),
                round(float(r.get('PE_IV', 0)), 2),
            ])

        return jsonify({
            'status': 'success',
            'date': today,
            'expiry': used_expiry,
            'spot_price': spot,
            'timestamp': timestamp,
            'expiry_dates': expiry_dates,
            'strike_count': len(rows),
            'headers': [
                'Date', 'Expiry', 'Strike',
                'CE Open', 'CE High', 'CE Low', 'CE Close', 'CE OI', 'CE Volume', 'CE IV',
                'PE Open', 'PE High', 'PE Low', 'PE Close', 'PE OI', 'PE Volume', 'PE IV'
            ],
            'rows': rows
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'status': 'error', 'error': str(e)}), 500


@app.route('/api/spot')
def get_spot():
    with store_lock:
        result = data_store['weekly']
    if not result:
        return jsonify({'error': 'Data not yet available'}), 503
    return jsonify({
        'status': 'success',
        'spot_price': result['spot_price'],
        'timestamp': result['timestamp']
    })


@app.route('/api/expiries')
def get_expiries():
    with store_lock:
        result = data_store['weekly']
    if not result:
        return jsonify({'error': 'Data not yet available'}), 503
    return jsonify({
        'status': 'success',
        'expiry_dates': result['expiry_dates'],
        'nearest_expiry': result['expiry']
    })


@app.route('/api/health')
def health():
    with store_lock:
        has_data = data_store['weekly'] is not None
    return jsonify({
        'status': 'healthy' if has_data else 'initializing',
        'has_data': has_data,
        'server_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'last_fetch': data_store['last_fetch'],
        'last_error': data_store['last_error'],
        'fetch_count': data_store['fetch_count'],
    })


# ===================== BACKGROUND FETCHER =====================

def background_fetcher():
    """Background thread that periodically fetches data from NSE."""
    global _shared_nse
    print("[BG] Background data fetcher started")
    with _shared_nse_lock:
        if _shared_nse is None:
            _shared_nse = NseUtils(headless=True)
    nse = _shared_nse

    while True:
        try:
            ts = datetime.now().strftime('%H:%M:%S')
            print(f"[BG] Fetching NSE data... ({ts})")
            start = time.time()

            result = nse.get_nifty_weekly_options(num_strikes=NUM_STRIKES)

            elapsed = round(time.time() - start, 1)

            if result:
                with store_lock:
                    data_store['weekly'] = result
                    data_store['last_fetch'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    data_store['last_error'] = None
                    data_store['fetch_count'] += 1

                print(f"[BG] ✅ Done in {elapsed}s | "
                      f"Spot: {result['spot_price']} | "
                      f"ATM: {result['atm_strike']} | "
                      f"Expiry: {result['expiry']} | "
                      f"Strikes: {len(result['data'])}")
            else:
                with store_lock:
                    data_store['last_error'] = 'No data returned'
                print(f"[BG] ⚠️ No data returned ({elapsed}s)")

        except Exception as e:
            with store_lock:
                data_store['last_error'] = str(e)
            print(f"[BG] ❌ Error: {e}")

        time.sleep(REFRESH_INTERVAL)


# ===================== MAIN =====================

if __name__ == '__main__':
    import sys
    import os
    import signal
    public_base_url = os.getenv('PUBLIC_BASE_URL', f'http://localhost:{PORT}')

    # Clean shutdown handler
    def shutdown(signum, frame):
        print("\n[SERVER] Shutting down...")
        os._exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    print("=" * 60)
    print("  NIFTY 50 Weekly Options - Live Data Server")
    print("=" * 60)
    print(f"  URL   : {public_base_url}")
    print(f"  Listen: 0.0.0.0:{PORT}")
    print(f"")
    print(f"  Sheets API: /api/options/sheets")
    print(f"  Refresh   : every {REFRESH_INTERVAL}s")
    print(f"  PID       : {os.getpid()}")
    print("=" * 60)

    # Start background fetcher as daemon thread
    fetcher_thread = threading.Thread(target=background_fetcher, daemon=True)
    fetcher_thread.start()

    # Run Flask in main thread
    app.run(host='0.0.0.0', port=PORT, debug=False)
