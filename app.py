import os
import time as time_module
import datetime
from datetime import timedelta, time
import threading
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# ==============================
# Config — Strategy v6.2
# (نفس استراتيجية v6.1 — التعديل الوحيد: رد فوري على TradingView + حماية من الإشارات المكررة)
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

# --- Trading windows (Saudi time = UTC+3) — v7 month test ---
W1_START = time(16, 40)   # 4:40 PM — بداية النافذة الأولى
W1_END   = time(18, 15)   # 6:15 PM — نهاية النافذة الأولى
W2_START = time(20, 0)    # 8:00 PM — بداية النافذة الثانية
W2_END   = time(21, 0)    # 9:00 PM — نهاية النافذة الثانية
FORCE_CLOSE = time(21, 30)  # 9:30 PM — إغلاق إجباري لأي مركز مفتوح (حماية)

# --- Profit / Loss ---
TP_W1  = 0.15    # ربح 15% لصفقة النافذة الأولى
TP_W2  = 0.05    # ربح 5% لصفقة النافذة الثانية
SL_PCT = 0.60    # وقف خسارة 60%

# --- Position sizing & limits ---
PORTFOLIO_PCT       = 0.60  # الدخول 60% من قيمة المحفظة
MAX_TRADES_PER_WIN  = 1     # صفقة واحدة فقط لكل نافذة

# --- قواعد إيقاف اليوم ---
# 1) أي صفقة تضرب وقف الخسارة → إيقاف التداول لبقية اليوم (يوم متقلب)
# 2) إشارة عكسية وصفقة مفتوحة → إغلاق الصفقة فوراً + إيقاف بقية اليوم

CHECK_INTERVAL = 30        # مراقبة كل 30 ثانية

# --- حالة إيقاف اليوم (بعد وقف خسارة أو إشارة عكسية) ---
_day_stop = {'date': None}
_day_stop_lock = threading.Lock()

def set_day_stopped(reason):
    with _day_stop_lock:
        _day_stop['date'] = saudi_now().date()
    print(f"[DayStop] Trading stopped for the rest of today — {reason}")

def is_day_stopped():
    with _day_stop_lock:
        return _day_stop['date'] == saudi_now().date()

# --- حماية من الإشارات المكررة (إعادة إرسال TradingView) ---
DEDUP_WINDOW_SEC = 90      # أي إشارة مطابقة خلال 90 ثانية تعتبر مكررة وتتجاهل
_last_signals = {}
_dedup_lock = threading.Lock()

def is_duplicate_signal(symbol, action):
    """يرجع True إذا نفس الإشارة (رمز+اتجاه) وصلت خلال نافذة التكرار"""
    key = f"{symbol}:{action}"
    now = time_module.time()
    with _dedup_lock:
        last = _last_signals.get(key)
        if last is not None and (now - last) < DEDUP_WINDOW_SEC:
            return True
        _last_signals[key] = now
        return False

# ==============================
# Time helpers
# ==============================
def saudi_now():
    return datetime.datetime.utcnow() + timedelta(hours=3)

def get_current_window():
    """يرجع (رقم النافذة، نسبة الربح) أو (None, None) إذا خارج النوافذ"""
    t = saudi_now().time()
    if W1_START <= t < W1_END:
        return 1, TP_W1
    if W2_START <= t < W2_END:
        return 2, TP_W2
    return None, None

def is_force_close_time():
    return saudi_now().time() >= FORCE_CLOSE

# ==============================
# Daily trade counter
# (يقرأ من Alpaca مباشرة — ما يتأثر بإعادة تشغيل السيرفر)
# ==============================
def get_window_trade_count(window):
    """يعد صفقات الشراء المنفذة اليوم داخل نافذة معينة (يقرأ من Alpaca — يصمد أمام إعادة التشغيل)"""
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

        w_start = W1_START if window == 1 else W2_START
        w_end   = W1_END   if window == 1 else W2_END
        count = 0
        for o in orders:
            if o.get('side') != 'buy':
                continue
            if o.get('status') not in ['filled', 'partially_filled', 'new', 'accepted', 'pending_new']:
                continue
            sub = o.get('submitted_at') or ''
            try:
                sub_dt = datetime.datetime.fromisoformat(sub.replace('Z', '+00:00'))
                sub_saudi = (sub_dt.replace(tzinfo=None) + timedelta(hours=3)).time()
            except Exception:
                continue
            if w_start <= sub_saudi < w_end:
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
    """يحسب عدد العقود = 60% من المحفظة ÷ سعر العقد"""
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

    print(f"[Qty] Funds={funds:.2f} | Budget(60%)={budget:.2f} | OptionPrice={option_price} | Qty={qty}")

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

            # --- وقف الخسارة → إغلاق + إيقاف بقية اليوم ---
            if current_price <= sl_price:
                print(f"[SL Hit] {symbol_occ} @ {current_price} (SL={sl_price:.2f})")
                close_position_full(symbol_occ)
                set_day_stopped(f"SL hit on {symbol_occ}")
                return

        except Exception as e:
            print(f"[Monitor Error] {symbol_occ}: {e}")

# ==============================
# Order placement
# ==============================
def place_option_order(symbol, action):
    # 0) هل اليوم موقوف؟ (بعد وقف خسارة أو إشارة عكسية)
    if is_day_stopped():
        print("[DayStop] Signal ignored — trading stopped for today")
        return {'status': 'ignored', 'message': 'Trading stopped for today (SL hit or reverse signal)'}

    # 1) النافذة الحالية + نسبة الربح
    window, tp_pct = get_current_window()
    if window is None:
        return {'status': 'ignored', 'message': 'Outside trading windows (4:40-6:15 PM / 8:00-9:00 PM Saudi)'}

    # 2) قاعدة الإشارة العكسية: صفقة مفتوحة بالاتجاه المعاكس → إغلاق + إيقاف بقية اليوم (بدون شراء)
    opposite_type = 'P' if action == 'CALL' else 'C'
    for pos in get_open_positions():
        pos_symbol = pos.get('symbol', '')
        if pos_symbol.startswith(symbol) and len(pos_symbol) > 12 and pos_symbol[12] == opposite_type:
            print(f"[Reverse] Opposite signal received — closing {pos_symbol} and stopping for today")
            close_position_full(pos_symbol)
            set_day_stopped(f"Reverse signal ({action}) closed {pos_symbol}")
            return {'status': 'closed_reverse', 'message': f'Closed {pos_symbol} on reverse signal — trading stopped for today'}

    # 3) حد صفقة واحدة لكل نافذة
    trade_count = get_window_trade_count(window)
    if trade_count >= MAX_TRADES_PER_WIN:
        print(f"[Limit] Window {window} trade limit reached ({trade_count}/{MAX_TRADES_PER_WIN})")
        return {'status': 'ignored', 'message': f'Window {window} limit reached: {trade_count}/{MAX_TRADES_PER_WIN}'}

    # 4) سعر السهم
    price = get_stock_price(symbol)
    if price is None:
        return {'status': 'error', 'message': 'Failed to get stock price — aborting'}

    symbol_occ, strike, expiry = build_occ_symbol(symbol, action, price)

    # 5) حساب الكمية: 60% من المحفظة
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
        'window': window,
        'trade_number_in_window': trade_count + 1,
        'status': 'success'
    }

def process_signal_async(symbol, action):
    """تنفيذ الصفقة في الخلفية بعد الرد الفوري على TradingView"""
    try:
        result = place_option_order(symbol, action)
        print(f"[Async Result] {symbol} {action} → {result}")
    except Exception as e:
        print(f"[Async Error] {symbol} {action}: {e}")

# ==============================
# Routes
# ==============================
@app.route('/')
def home():
    return 'Trading Bot v7.0 — Month Test | W1 4:40-6:15 TP15% | W2 8:00-9:00 TP5% | 1 trade/window | 60% sizing | SL 60% | day-stop on SL or reverse ✅'

@app.route('/status')
def status():
    positions = get_open_positions()
    return jsonify({
        'saudi_time': saudi_now().strftime('%Y-%m-%d %H:%M:%S'),
        'current_window': get_current_window()[0],
        'day_stopped': is_day_stopped(),
        'w1_trades': get_window_trade_count(1),
        'w2_trades': get_window_trade_count(2),
        'available_funds': get_available_funds(),
        'active_positions': positions,
        'count': len(positions)
    })

@app.route('/webhook', methods=['POST'])
def webhook():
    """يرد على TradingView فوراً (أقل من ثانية) وينفذ الصفقة في الخلفية"""
    try:
        data = request.get_json(force=True, silent=True) or {}
        print(f"[Webhook] Received: {data}")

        action = data.get('action', '').upper()
        symbol = data.get('symbol', 'SPY').upper()

        if symbol not in ['SPY', 'QQQ']:
            symbol = 'SPY'

        if action not in ['CALL', 'PUT']:
            return jsonify({'status': 'error', 'message': f'Invalid action: {action}'}), 400

        # حماية من التكرار: إعادة إرسال TradingView لنفس الإشارة تتجاهل فوراً
        if is_duplicate_signal(symbol, action):
            print(f"[Dedup] Duplicate {action} {symbol} ignored")
            return jsonify({'status': 'ignored', 'message': 'Duplicate signal (retry) ignored'})

        # تنفيذ الصفقة في الخلفية والرد الفوري
        t = threading.Thread(target=process_signal_async, args=(symbol, action))
        t.daemon = True
        t.start()

        return jsonify({'status': 'accepted', 'message': f'{action} {symbol} received — processing'})

    except Exception as e:
        print(f"[Webhook Error] {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
