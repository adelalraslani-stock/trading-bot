from flask import Flask, request, jsonify
import requests, os, datetime, threading, time

app = Flask(__name__)

ALPACA_KEY    = os.environ.get('ALPACA_KEY')
ALPACA_SECRET = os.environ.get('ALPACA_SECRET')
ALPACA_BASE   = os.environ.get('ALPACA_BASE_URL', 'https://paper-api.alpaca.markets')

# ==============================
# إعدادات الاستراتيجية
# ==============================
STOP_LOSS_PCT      = 0.35   # 35% خسارة أولية
TRAILING_PCT       = 0.15   # 15% تحت أعلى سعر
TRAILING_ACTIVATE  = 0.10   # يبدأ الـ Trailing بعد +10% ربح
MAX_PROFIT_PCT     = 1.00   # 100% ربح = الدبل (يبيع فوراً)

# نسب الربح الثابتة (لو ما وصل للـ Trailing)
TP_WINDOW1 = 0.15   # 15% — نافذة 4:45-6:30 PM السعودية
TP_WINDOW2 = 0.05   # 5%  — نافذة 8:10-10:00 PM السعودية

HEADERS = {
    'APCA-API-KEY-ID'    : ALPACA_KEY,
    'APCA-API-SECRET-KEY': ALPACA_SECRET
}

# ==============================
# جلب أحدث سعر للسهم
# ==============================
def get_latest_price(symbol):
    try:
        url = f"https://data.alpaca.markets/v2/stocks/{symbol}/quotes/latest"
        r   = requests.get(url, headers=HEADERS, timeout=10)
        price = float(r.json()['quote']['ap'])
        if price <= 0:
            print(f"[Price Error] Invalid price: {price}")
            return None
        return price
    except Exception as e:
        print(f"[Price Error] {e}")
        return None

# ==============================
# تحديد تاريخ انتهاء الأوبشن
# ==============================
def get_expiry(signal_time=None):
    try:
        if signal_time:
            signal_dt = datetime.datetime.fromisoformat(signal_time.replace('Z', '+00:00'))
        else:
            signal_dt = datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc)

        ny_time = signal_dt - datetime.timedelta(hours=4)
        cutoff  = ny_time.replace(hour=15, minute=30, second=0, microsecond=0)

        if ny_time < cutoff:
            expiry = ny_time.date()
        else:
            next_day = ny_time.date() + datetime.timedelta(days=1)
            while next_day.weekday() >= 5:
                next_day += datetime.timedelta(days=1)
            expiry = next_day

        return expiry
    except Exception as e:
        print(f"[Expiry Error] {e}")
        return datetime.date.today()

# ==============================
# بناء رمز OCC للأوبشن
# ==============================
def build_occ_symbol(symbol, expiry, action, strike):
    right = 'C' if action == 'CALL' else 'P'
    return f"{symbol}{expiry.strftime('%y%m%d')}{right}{int(strike * 1000):08d}"

# ==============================
# فلتر الوقت — يرجع رقم النافذة أو None
# ==============================
def get_trading_window(signal_time=None):
    try:
        if signal_time:
            signal_dt = datetime.datetime.fromisoformat(signal_time.replace('Z', '+00:00'))
        else:
            signal_dt = datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc)

        ny_time = signal_dt - datetime.timedelta(hours=4)

        window1_start = ny_time.replace(hour=9,  minute=45, second=0, microsecond=0)
        window1_end   = ny_time.replace(hour=11, minute=30, second=0, microsecond=0)
        window2_start = ny_time.replace(hour=13, minute=10, second=0, microsecond=0)
        window2_end   = ny_time.replace(hour=15, minute=0,  second=0, microsecond=0)

        if window1_start <= ny_time < window1_end:
            print(f"[Filter] Window 1 — 4:45-6:30 PM KSA | Trailing ON")
            return 1

        if window2_start <= ny_time < window2_end:
            print(f"[Filter] Window 2 — 8:10-10:00 PM KSA | TP=5%")
            return 2

        print(f"[Filter] Outside trading windows — ignored")
        return None

    except Exception as e:
        print(f"[Filter Error] {e}")
        return None

# ==============================
# جلب البوزيشنات المفتوحة من Alpaca
# ==============================
def get_open_positions():
    try:
        r = requests.get(f"{ALPACA_BASE}/v2/positions", headers=HEADERS, timeout=10)
        if r.status_code != 200:
            return {}

        positions = {}
        for pos in r.json():
            sym = pos.get('symbol', '')
            for base in ['SPY', 'QQQ']:
                if sym.startswith(base):
                    try:
                        right  = sym[9]
                        action = 'CALL' if right == 'C' else 'PUT'
                    except:
                        action = 'CALL'
                    positions[base] = {
                        'occ_symbol'      : sym,
                        'action'          : action,
                        'unrealized_plpc' : float(pos.get('unrealized_plpc', 0)),
                        'current_price'   : float(pos.get('current_price', 0)),
                        'avg_entry_price' : float(pos.get('avg_entry_price', 0)),
                    }
                    break
        return positions
    except Exception as e:
        print(f"[Positions Error] {e}")
        return {}

# ==============================
# إلغاء أوردر محدد
# ==============================
def cancel_order(order_id):
    try:
        r = requests.delete(f"{ALPACA_BASE}/v2/orders/{order_id}", headers=HEADERS, timeout=10)
        print(f"[Cancel] {order_id}: {r.status_code}")
    except Exception as e:
        print(f"[Cancel Error] {e}")

# ==============================
# إلغاء كل الأوردرات المفتوحة لرمز معين
# ==============================
def cancel_all_orders_for_symbol(occ_symbol):
    try:
        r = requests.get(f"{ALPACA_BASE}/v2/orders?status=open&limit=100", headers=HEADERS, timeout=10)
        if r.status_code == 200:
            for order in r.json():
                if order.get('symbol') == occ_symbol:
                    cancel_order(order.get('id'))
    except Exception as e:
        print(f"[Cancel All Error] {e}")

# ==============================
# إغلاق بوزيشن بسعر السوق
# ==============================
def close_position_market(occ_symbol, qty="1"):
    try:
        cancel_all_orders_for_symbol(occ_symbol)
        time.sleep(1)

        close_order = {
            "symbol"       : occ_symbol,
            "qty"          : qty,
            "side"         : "sell",
            "type"         : "market",
            "time_in_force": "day"
        }
        r = requests.post(f"{ALPACA_BASE}/v2/orders", json=close_order, headers=HEADERS, timeout=10)
        print(f"[Close] {occ_symbol} Status={r.status_code} | {r.json().get('status','')}")
        return r.status_code in [200, 201]
    except Exception as e:
        print(f"[Close Error] {e}")
        return False

# ==============================
# مراقبة Trailing Stop (نافذة 1)
# ==============================
def monitor_trailing(symbol, symbol_occ, entry_price, qty):
    print(f"[Trailing] Started for {symbol_occ} | Entry={entry_price}")

    highest_price  = entry_price
    trailing_active = False
    max_checks      = 480  # 4 ساعات
    checks          = 0
    sl_price        = round(entry_price * (1 - STOP_LOSS_PCT), 2)
    max_tp_price    = round(entry_price * (1 + MAX_PROFIT_PCT), 2)

    print(f"[Trailing] SL={sl_price} | Max TP={max_tp_price} | Trailing activates at +{TRAILING_ACTIVATE*100:.0f}%")

    while checks < max_checks:
        time.sleep(30)
        checks += 1

        try:
            # جلب السعر الحالي من البوزيشن
            pos_r = requests.get(f"{ALPACA_BASE}/v2/positions/{symbol_occ}", headers=HEADERS, timeout=10)

            if pos_r.status_code == 404:
                print(f"[Trailing] Position closed for {symbol_occ}")
                break

            if pos_r.status_code == 200:
                pos_data          = pos_r.json()
                current_price     = float(pos_data.get('current_price', 0))
                unrealized_pl_pct = float(pos_data.get('unrealized_plpc', 0))

                # تحديث أعلى سعر
                if current_price > highest_price:
                    highest_price = current_price
                    print(f"[Trailing] New high: {highest_price:.2f} | P/L={unrealized_pl_pct:.2%}")

                # حساب Trailing Stop الحالي
                trailing_stop = round(highest_price * (1 - TRAILING_PCT), 2)

                print(f"[Trailing] Check={checks} | Price={current_price:.2f} | High={highest_price:.2f} | Trail={trailing_stop:.2f} | P/L={unrealized_pl_pct:.2%} | Active={trailing_active}")

                # 1. بيع فوري لو وصل الدبل (100%)
                if current_price >= max_tp_price:
                    print(f"[Trailing] 🎯 MAX TP reached! {current_price:.2f} >= {max_tp_price:.2f} — Selling!")
                    close_position_market(symbol_occ, str(qty))
                    break

                # 2. تفعيل الـ Trailing بعد +10%
                if unrealized_pl_pct >= TRAILING_ACTIVATE and not trailing_active:
                    trailing_active = True
                    print(f"[Trailing] ✅ Trailing ACTIVATED at {current_price:.2f} | P/L={unrealized_pl_pct:.2%}")

                # 3. لو الـ Trailing نشط وانكسر الـ Trailing Stop
                if trailing_active and current_price <= trailing_stop:
                    print(f"[Trailing] 🔴 Trailing Stop hit! Price={current_price:.2f} <= Trail={trailing_stop:.2f} — Selling!")
                    close_position_market(symbol_occ, str(qty))
                    break

                # 4. SL أولي (قبل تفعيل الـ Trailing)
                if not trailing_active and unrealized_pl_pct <= -STOP_LOSS_PCT:
                    print(f"[Trailing] 🔴 SL triggered! P/L={unrealized_pl_pct:.2%} — Selling!")
                    close_position_market(symbol_occ, str(qty))
                    break

        except Exception as e:
            print(f"[Trailing Error] {e}")

    print(f"[Trailing] Done for {symbol_occ}")

# ==============================
# مراقبة TP ثابت (نافذة 2)
# ==============================
def monitor_fixed_tp(symbol, symbol_occ, tp_id, entry_price, qty):
    print(f"[Monitor] Started for {symbol_occ}")
    max_checks = 480
    checks     = 0
    sl_price   = round(entry_price * (1 - STOP_LOSS_PCT), 2)

    while checks < max_checks:
        time.sleep(30)
        checks += 1

        try:
            tp_r      = requests.get(f"{ALPACA_BASE}/v2/orders/{tp_id}", headers=HEADERS, timeout=10)
            tp_status = tp_r.json().get('status', '') if tp_r.status_code == 200 else 'unknown'
            print(f"[Monitor] {symbol_occ} | TP={tp_status} | Check={checks}")

            if tp_status == 'filled':
                print(f"[Monitor] ✅ TP filled!")
                break

            if tp_status in ['cancelled', 'canceled', 'expired']:
                print(f"[Monitor] TP cancelled/expired")
                break

            pos_r = requests.get(f"{ALPACA_BASE}/v2/positions/{symbol_occ}", headers=HEADERS, timeout=10)

            if pos_r.status_code == 404:
                print(f"[Monitor] Position closed")
                break

            if pos_r.status_code == 200:
                pos_data          = pos_r.json()
                unrealized_pl_pct = float(pos_data.get('unrealized_plpc', 0))
                current_price     = float(pos_data.get('current_price', 0))
                print(f"[Monitor] P/L={unrealized_pl_pct:.2%} | Price={current_price} | SL=-{STOP_LOSS_PCT:.0%}")

                if unrealized_pl_pct <= -STOP_LOSS_PCT:
                    print(f"[Monitor] 🔴 SL triggered! Closing {symbol_occ}")
                    cancel_order(tp_id)
                    time.sleep(1)
                    close_position_market(symbol_occ, str(qty))
                    break

        except Exception as e:
            print(f"[Monitor Error] {e}")

    print(f"[Monitor] Done for {symbol_occ}")

# ==============================
# وضع TP ثابت (نافذة 2) + بدء مراقبة
# ==============================
def place_fixed_tp(symbol, symbol_occ, order_id, take_profit_pct, qty):
    filled_price = None
    for attempt in range(8):
        time.sleep(3)
        try:
            r            = requests.get(f"{ALPACA_BASE}/v2/orders/{order_id}", headers=HEADERS, timeout=10)
            data         = r.json()
            filled_price = data.get('filled_avg_price')
            status       = data.get('status', '')
            print(f"[TP] Attempt {attempt+1}: status={status} filled={filled_price}")
            if filled_price:
                break
        except Exception as e:
            print(f"[TP Error] {e}")

    if not filled_price:
        print(f"[TP] No fill price. Skipping.")
        return

    opt_price = float(filled_price)
    tp_price  = round(opt_price * (1 + take_profit_pct), 2)
    sl_price  = round(opt_price * (1 - STOP_LOSS_PCT), 2)

    print(f"[TP] Entry={opt_price} | TP={tp_price} ({take_profit_pct*100:.0f}%) | SL={sl_price}")

    tp_order = {
        "symbol"       : symbol_occ,
        "qty"          : str(qty),
        "side"         : "sell",
        "type"         : "limit",
        "limit_price"  : str(tp_price),
        "time_in_force": "day"
    }
    tp_r  = requests.post(f"{ALPACA_BASE}/v2/orders", json=tp_order, headers=HEADERS, timeout=10)
    tp_id = tp_r.json().get('id') if tp_r.status_code in [200, 201] else None
    print(f"[TP] Order status={tp_r.status_code} | id={tp_id}")

    if not tp_id:
        tp_id = order_id

    t = threading.Thread(target=monitor_fixed_tp, args=(symbol, symbol_occ, tp_id, opt_price, qty))
    t.daemon = True
    t.start()

# ==============================
# بدء مراقبة Trailing (نافذة 1)
# ==============================
def start_trailing(symbol, symbol_occ, order_id, qty):
    filled_price = None
    for attempt in range(8):
        time.sleep(3)
        try:
            r            = requests.get(f"{ALPACA_BASE}/v2/orders/{order_id}", headers=HEADERS, timeout=10)
            data         = r.json()
            filled_price = data.get('filled_avg_price')
            status       = data.get('status', '')
            print(f"[Trailing] Attempt {attempt+1}: status={status} filled={filled_price}")
            if filled_price:
                break
        except Exception as e:
            print(f"[Trailing Init Error] {e}")

    if not filled_price:
        print(f"[Trailing] No fill price. Skipping.")
        return

    entry_price = float(filled_price)
    print(f"[Trailing] Entry confirmed: {entry_price}")

    t = threading.Thread(target=monitor_trailing, args=(symbol, symbol_occ, entry_price, qty))
    t.daemon = True
    t.start()

# ==============================
# الدالة الرئيسية
# ==============================
def place_option_order(symbol, action, signal_time=None, window=1):
    print(f"\n{'='*50}")
    print(f"[Signal] {action} {symbol} @ {signal_time} | Window={window}")

    # تحديد الكمية حسب النافذة
    qty = 4 if window == 1 else 2

    # تحقق من البوزيشنات المفتوحة
    open_positions = get_open_positions()
    existing       = open_positions.get(symbol)

    if existing:
        print(f"[Check] Open: {existing['occ_symbol']} | Action={existing['action']}")
        if existing['action'] != action:
            print(f"[Reverse] Closing {existing['occ_symbol']}")
            close_position_market(existing['occ_symbol'], str(qty))
            time.sleep(2)
        else:
            print(f"[Skip] Same direction. Skipping.")
            return {'status': 'skipped'}

    price = get_latest_price(symbol)
    if price is None:
        print(f"[Abort] Price fetch failed for {symbol}")
        return {'status': 'error', 'message': 'Price fetch failed'}

    expiry     = get_expiry(signal_time)
    strike     = round(price)
    symbol_occ = build_occ_symbol(symbol, expiry, action, strike)

    print(f"[OCC] {symbol_occ} | Price={price} | Strike={strike} | Expiry={expiry} | Qty={qty}")

    order = {
        "symbol"       : symbol_occ,
        "qty"          : str(qty),
        "side"         : "buy",
        "type"         : "market",
        "time_in_force": "day"
    }

    r      = requests.post(f"{ALPACA_BASE}/v2/orders", json=order, headers=HEADERS, timeout=10)
    result = r.json()
    print(f"[Buy] Status={r.status_code} | Order={result.get('id','')}")

    if r.status_code in [200, 201]:
        order_id = result.get('id')

        if window == 1:
            # نافذة 1: Trailing Stop
            t = threading.Thread(target=start_trailing, args=(symbol, symbol_occ, order_id, qty))
        else:
            # نافذة 2: TP ثابت 5%
            t = threading.Thread(target=place_fixed_tp, args=(symbol, symbol_occ, order_id, TP_WINDOW2, qty))

        t.daemon = True
        t.start()

    return {
        'symbol'    : symbol,
        'action'    : action,
        'price'     : price,
        'strike'    : strike,
        'expiry'    : str(expiry),
        'occ_symbol': symbol_occ,
        'qty'       : qty,
        'window'    : window,
        'status'    : r.status_code,
        'result'    : result
    }

# ==============================
# Routes
# ==============================

@app.route('/')
def home():
    return 'Trading Bot v5 — Trailing Stop ✅'

@app.route('/status')
def status():
    positions = get_open_positions()
    return jsonify({'active_positions': positions, 'count': len(positions)})

@app.route('/test')
def test():
    now    = datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
    result = place_option_order('SPY', 'CALL', now, window=1)
    return jsonify(result)

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data        = request.get_json(force=True, silent=True) or {}
        print(f"[Webhook] Received: {data}")

        action      = data.get('action', '').upper()
        symbol      = data.get('symbol', 'SPY').upper()
        signal_time = data.get('time', None)

        if symbol not in ['SPY', 'QQQ']:
            symbol = 'SPY'

        if action not in ['CALL', 'PUT']:
            return jsonify({'status': 'error', 'message': f'Invalid action: {action}'}), 400

        window = get_trading_window(signal_time)
        if window is None:
            return jsonify({'status': 'ignored', 'message': 'Outside trading windows'})

        print(f"[Webhook] Window={window}")
        result = place_option_order(symbol, action, signal_time, window)
        return jsonify({'status': 'success', 'data': result})

    except Exception as e:
        print(f"[Webhook Error] {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
