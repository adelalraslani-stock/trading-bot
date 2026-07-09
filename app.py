import os
import time as time_module
import datetime
from datetime import timedelta, time
import threading
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# ==============================
# Config — Strategy v6.1
# ==============================
ALPACA_KEY    = os.environ.get('ALPACA_KEY')
ALPACA_SECRET = os.environ.get('ALPACA_SECRET')
BASE_URL      = os.environ.get('ALPACA_BASE_URL', 'https://paper-api.alpaca.markets')
DATA_URL      = 'https://data.alpaca.markets'

HEADERS = {
    'APCA-API-KEY-ID': ALPACA_KEY,
    'APCA-API-SECRET-KEY': ALPACA_SECRET,
    'Content-Type': 'application/json'
}

# --- Trading hours (Saudi time = UTC+3) ---
ENTRY_START   = time(16, 45)   # 4:45 PM  — بداية الدخول
TP_SWITCH     = time(19, 1)    # 7:01 PM  — التحول من TP 15% إلى TP 5%
ENTRY_END     = time(21, 20)   # 9:20 PM  — آخر دخول
FORCE_CLOSE   = time(21, 30)   # 9:30 PM  — إغلاق إجباري لأي مركز مفتوح

# --- Profit / Loss ---
TP_EARLY = 0.15    # ربح 15% للصفقات الداخلة 4:45 – 7:00
TP_LATE  = 0.05    # ربح 5%  للصفقات الداخلة 7:01 – 9:20
SL_PCT   = 0.30    # وقف خسارة 30%

# --- Position sizing & limits ---
PORTFOLIO_PCT      = 0.65  # نسبة الدخول من قيمة المحفظة لكل صفقة
MAX_TRADES_PER_DAY = 2     # حد أقصى صفقتين باليوم

CHECK_INTERVAL = 30        # مراقبة كل 30 ثانية

# ==============================
# Time helpers
# ==============================
def saudi_now():
    return datetime.datetime.utcnow() + timedelta(hours=3)

def get_tp_for_entry():
    """يرجع نسبة الربح حسب وقت الدخول، أو None إذا خارج وقت التداول"""
    t = saudi_now().time()
    if ENTRY_START <= t < TP_SWITCH:
        return TP_EARLY
    if TP_SWITCH <= t <= ENTRY_END:
        return TP_LATE
    return None

def is_force_close_time():
    return saudi_now().time() >= FORCE_CLOSE

# ==============================
# Daily trade counter
# (يقرأ من Alpaca مباشرة — ما يتأثر بإعادة تشغيل السيرفر)
# ==============================
def get_today_trade_count():
    try:
        now_saudi = saudi_now()
        day_start_saudi = now_saudi.replace(hour=0, minute=0, second=0, microsecond=0)
        day_start_utc = day_start_saudi - timedelta(hours=3)
        after = day_start_utc.strftime('%Y-%m-%dT%H:%M:%SZ')

        r = requests.get(
            f"{BASE_URL}/v2/orders",
            headers=HEADERS,
            params={'status': 'all', 'after': after, 'limit': 500}
        )
        orders = r.json()
        if not isinstance(orders, list):
            print(f"[TradeCount] Unexpected response: {orders}")
            return 999  # أمان: لو فشلت القراءة لا يدخل صفقات جديدة

        count = 0
        for o in orders:
            if o.get('side') == 'buy' and o.get('status') in ['filled', 'partially_filled', 'new', 'accepted', 'pending_new']:
                count += 1
        return count
    except Exception as e:
        print(f"[TradeCount Error] {e}")
        return 999  # أمان

# ==============================
# Account & market data
# ==============================
def get_available_funds():
    """يرجع المبلغ المتاح للشراء (أوبشنز)"""
    try:
        r = requests.get(f"{BASE_URL}/v2/account", headers=HEADERS)
        acc = r.json()
        for field in ['options_buying_power', 'non_marginable_buying_power', 'cash', 'buying_power']:
            val = acc.get(field)
            if val is not None:
                funds = float(val)
                if funds > 0:
                    print(f"[Funds] Using {field} = {funds}")
                    return funds
        return None
    except Exception as e:
        print(f"[Funds Error] {e}")
        return None

def get_stock_price(symbol):
    try:
        r = requests.get(
            f"{DATA_URL}/v2/stocks/{symbol}/trades/latest",
            headers=HEADERS
        )
        data = r.json()
        return float(data['trade']['p'])
    except Exception as e:
        print(f"[Price Error] {symbol}: {e}")
        return None

def get_option_price(symbol_occ):
    """يجيب سعر العقد الحالي (ask للشراء، مع fallback على آخر صفقة)"""
    try:
        r = requests.get(
            f"{DATA_URL}/v1beta1/options/quotes/latest",
            headers=HEADERS,
            params={'symbols': symbol_occ}
        )
        data = r.json()
        quote = data.get('quotes', {}).get(symbol_occ)
        if quote:
            ask = float(quote.get('ap') or 0)
            if ask > 0:
                return ask
    except Exception as e:
        print(f"[Option Quote Error] {e}")

    try:
        r = requests.get(
            f"{DATA_URL}/v1beta1/options/trades/latest",
            headers=HEADERS,
            params={'symbols': symbol_occ}
        )
        data = r.json()
        trade = data.get('trades', {}).get(symbol_occ)
        if trade:
            p = float(trade.get('p') or 0)
            if p > 0:
                return p
    except Exception as e:
        print(f"[Option Trade Error] {e}")

    return None

def calculate_qty(symbol_occ):
    """يحسب عدد العقود = 65% من المحفظة ÷ سعر العقد"""
    funds = get_available_funds()
    if funds is None:
        print("[Qty] Failed to get account funds — aborting")
        return None, None

    option_price = get_option_price(symbol_occ)
    if option_price is None or option_price <= 0:
        print(f"[Qty] Failed to get option price for {symbol_occ} — aborting")
        return None, None

    budget = funds * PORTFOLIO_PCT
    qty = int(budget // (option_price * 100))

    print(f"[Qty] Funds={funds:.2f} | Budget(65%)={budget:.2f} | OptionPrice={option_price} | Qty={qty}")

    if qty < 1:
        print("[Qty] Budget too small for even 1 contract — aborting")
        return None, option_price

    return qty, option_price

# ==============================
# OCC symbol
# ==============================
def build_occ_symbol(symbol, action, price):
    expiry = datetime.datetime.utcnow().date()  # 0DTE
    strike = round(price)
    opt_type = 'C' if action == 'CALL' else 'P'
    strike_str = f"{int(strike * 1000):08d}"
    date_str = expiry.strftime('%y%m%d')
    return f"{symbol}{date_str}{opt_type}{strike_str}", strike, expiry

# ==============================
# Positions & orders
# ==============================
def get_open_positions():
    try:
        r = requests.get(f"{BASE_URL}/v2/positions", headers=HEADERS)
        positions = r.json()
        return positions if isinstance(positions, list) else []
    except Exception as e:
        print(f"[Positions Error] {e}")
        return []

def get_position(symbol_occ):
    for p in get_open_positions():
        if p.get('symbol') == symbol_occ:
            return p
    return None

def cancel_open_orders_for(symbol_occ):
    try:
        r = requests.get(f"{BASE_URL}/v2/orders", headers=HEADERS, params={'status': 'open', 'limit': 200})
        for o in r.json():
            if o.get('symbol') == symbol_occ:
                requests.delete(f"{BASE_URL}/v2/orders/{o['id']}", headers=HEADERS)
                print(f"[Cancel] Order {o['id']} for {symbol_occ}")
    except Exception as e:
        print(f"[Cancel Error] {e}")

def market_sell(symbol_occ, qty):
    try:
        order = {
            'symbol': symbol_occ,
            'qty': str(qty),
            'side': 'sell',
            'type': 'market',
            'time_in_force': 'day'
        }
        r = requests.post(f"{BASE_URL}/v2/orders", headers=HEADERS, json=order)
        print(f"[Market Sell] {symbol_occ} x{qty} → {r.status_code}")
        return r.json()
    except Exception as e:
        print(f"[Market Sell Error] {e}")
        return None

def close_position_full(symbol_occ):
    """يلغي أي أوامر معلقة ثم يبيع المركز كامل"""
    cancel_open_orders_for(symbol_occ)
    time_module.sleep(1)
    pos = get_position(symbol_occ)
    if pos:
        qty = abs(int(float(pos['qty'])))
        market_sell(symbol_occ, qty)

# ==============================
# Monitoring thread (SL + Force close)
# ==============================
def monitor_position(symbol_occ, entry_price, qty, tp_pct):
    sl_price = entry_price * (1 - SL_PCT)
    print(f"[Monitor] {symbol_occ} | Entry={entry_price} | TP={tp_pct*100:.0f}% | SL={sl_price:.2f} (-{SL_PCT*100:.0f}%)")

    while True:
        time_module.sleep(CHECK_INTERVAL)
        try:
            pos = get_position(symbol_occ)

            if pos is None:
                print(f"[Monitor] {symbol_occ} closed (TP filled or manual). Stopping monitor.")
                return

            # --- إغلاق إجباري الساعة 9:30 مساءً ---
            if is_force_close_time():
                print(f"[Force Close 9:30] {symbol_occ}")
                close_position_full(symbol_occ)
                return

            current_price = float(pos.get('current_price') or 0)
            if current_price <= 0:
                continue

            # --- وقف الخسارة ---
            if current_price <= sl_price:
                print(f"[SL Hit] {symbol_occ} @ {current_price} (SL={sl_price:.2f})")
                close_position_full(symbol_occ)
                return

        except Exception as e:
            print(f"[Monitor Error] {symbol_occ}: {e}")

# ==============================
# Order placement
# ==============================
def place_option_order(symbol, action):
    # 1) وقت التداول + نسبة الربح
    tp_pct = get_tp_for_entry()
    if tp_pct is None:
        return {'status': 'ignored', 'message': 'Outside trading hours (4:45 PM - 9:20 PM Saudi)'}

    # 2) حد الصفقتين اليومي
    trade_count = get_today_trade_count()
    if trade_count >= MAX_TRADES_PER_DAY:
        print(f"[Limit] Daily trade limit reached ({trade_count}/{MAX_TRADES_PER_DAY})")
        return {'status': 'ignored', 'message': f'Daily limit reached: {trade_count}/{MAX_TRADES_PER_DAY}'}

    # 3) سعر السهم
    price = get_stock_price(symbol)
    if price is None:
        return {'status': 'error', 'message': 'Failed to get stock price — aborting'}

    symbol_occ, strike, expiry = build_occ_symbol(symbol, action, price)

    # 4) إغلاق مركز عكسي إن وجد (الإشارة العكسية تسكر المركز السابق)
    opposite_type = 'P' if action == 'CALL' else 'C'
    for pos in get_open_positions():
        pos_symbol = pos.get('symbol', '')
        if pos_symbol.startswith(symbol) and len(pos_symbol) > 12 and pos_symbol[12] == opposite_type:
            print(f"[Reverse] Closing opposite position {pos_symbol}")
            close_position_full(pos_symbol)
            time_module.sleep(3)  # ننتظر تحرير الرصيد قبل حساب الكمية

    # 5) حساب الكمية: 65% من المحفظة
    qty, option_price = calculate_qty(symbol_occ)
    if qty is None:
        return {'status': 'error', 'message': 'Failed to calculate quantity (funds or option price unavailable)'}

    # 6) شراء
    buy_order = {
        'symbol': symbol_occ,
        'qty': str(qty),
        'side': 'buy',
        'type': 'market',
        'time_in_force': 'day'
    }
    r = requests.post(f"{BASE_URL}/v2/orders", headers=HEADERS, json=buy_order)
    result = r.json()
    print(f"[Buy] {symbol_occ} x{qty} → {r.status_code}: {result}")

    if r.status_code not in [200, 201]:
        return {'status': 'error', 'message': str(result)}

    order_id = result.get('id')

    # 7) انتظار سعر التنفيذ الفعلي
    filled_price = None
    filled_qty = qty
    for _ in range(10):
        time_module.sleep(2)
        try:
            ro = requests.get(f"{BASE_URL}/v2/orders/{order_id}", headers=HEADERS)
            od = ro.json()
            if od.get('status') == 'filled' and od.get('filled_avg_price'):
                filled_price = float(od['filled_avg_price'])
                filled_qty = int(float(od.get('filled_qty') or qty))
                break
        except Exception as e:
            print(f"[Fill Check Error] {e}")

    if filled_price is None:
        print(f"[Warning] Could not confirm fill price for {symbol_occ} — monitoring skipped!")
        return {'status': 'warning', 'message': 'Order sent but fill price unconfirmed', 'occ_symbol': symbol_occ}

    # 8) أمر TP Limit
    tp_price = round(filled_price * (1 + tp_pct), 2)
    tp_order = {
        'symbol': symbol_occ,
        'qty': str(filled_qty),
        'side': 'sell',
        'type': 'limit',
        'limit_price': str(tp_price),
        'time_in_force': 'day'
    }
    rt = requests.post(f"{BASE_URL}/v2/orders", headers=HEADERS, json=tp_order)
    print(f"[TP Order] {symbol_occ} @ {tp_price} ({tp_pct*100:.0f}%) → {rt.status_code}")

    # 9) مراقبة SL + الإغلاق الإجباري
    t = threading.Thread(target=monitor_position, args=(symbol_occ, filled_price, filled_qty, tp_pct))
    t.daemon = True
    t.start()

    return {
        'symbol': symbol,
        'action': action,
        'occ_symbol': symbol_occ,
        'strike': strike,
        'expiry': str(expiry),
        'qty': filled_qty,
        'entry_price': filled_price,
        'tp_pct': f"{tp_pct*100:.0f}%",
        'tp_price': tp_price,
        'sl_pct': f"{SL_PCT*100:.0f}%",
        'portfolio_pct': f"{PORTFOLIO_PCT*100:.0f}%",
        'trade_number_today': trade_count + 1,
        'status': 'success'
    }

# ==============================
# Routes
# ==============================
@app.route('/')
def home():
    return 'Trading Bot v6.1 — TP 15%/5% by time | SL 30% | 65% portfolio sizing | 2 trades/day ✅'

@app.route('/status')
def status():
    positions = get_open_positions()
    return jsonify({
        'saudi_time': saudi_now().strftime('%Y-%m-%d %H:%M:%S'),
        'current_tp': get_tp_for_entry(),
        'trades_today': get_today_trade_count(),
        'max_trades': MAX_TRADES_PER_DAY,
        'available_funds': get_available_funds(),
        'active_positions': positions,
        'count': len(positions)
    })

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = request.get_json(force=True, silent=True) or {}
        print(f"[Webhook] Received: {data}")

        action = data.get('action', '').upper()
        symbol = data.get('symbol', 'SPY').upper()

        if symbol not in ['SPY', 'QQQ']:
            symbol = 'SPY'

        if action not in ['CALL', 'PUT']:
            return jsonify({'status': 'error', 'message': f'Invalid action: {action}'}), 400

        result = place_option_order(symbol, action)
        return jsonify(result)

    except Exception as e:
        print(f"[Webhook Error] {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
