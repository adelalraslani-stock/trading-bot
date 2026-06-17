from flask import Flask, request, jsonify
import requests, os, datetime, threading, time

app = Flask(__name__)

ALPACA_KEY    = os.environ.get('ALPACA_KEY')
ALPACA_SECRET = os.environ.get('ALPACA_SECRET')
ALPACA_BASE   = os.environ.get('ALPACA_BASE_URL', 'https://paper-api.alpaca.markets')

# ==============================
# إعدادات الربح والخسارة
# ==============================
TAKE_PROFIT_PCT = 0.05   # 5% ربح
STOP_LOSS_PCT   = 0.50   # 50% خسارة

HEADERS = {
    'APCA-API-KEY-ID'    : ALPACA_KEY,
    'APCA-API-SECRET-KEY': ALPACA_SECRET
}

# ==============================
# سجل البوزيشنز النشطة لكل رمز
# مفتاح: symbol (SPY/QQQ/XSP)
# قيمة: {occ_symbol, action, tp_id, sl_id}
# ==============================
active_positions = {}
positions_lock   = threading.Lock()

# ==============================
# جلب أحدث سعر للسهم
# ==============================
def get_latest_price(symbol):
    try:
        url = f"https://data.alpaca.markets/v2/stocks/{symbol}/quotes/latest"
        r   = requests.get(url, headers=HEADERS, timeout=10)
        return float(r.json()['quote']['ap'])
    except Exception as e:
        print(f"[Price Error] {e}")
        return 500.00

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
# إلغاء أوردر محدد
# ==============================
def cancel_order(order_id):
    try:
        r = requests.delete(f"{ALPACA_BASE}/v2/orders/{order_id}", headers=HEADERS, timeout=10)
        print(f"[Cancel] Order {order_id}: {r.status_code}")
    except Exception as e:
        print(f"[Cancel Error] {e}")

# ==============================
# إغلاق البوزيشن الحالية فوراً عند إشارة عكسية
# ==============================
def close_position(symbol):
    with positions_lock:
        pos = active_positions.get(symbol)
        if not pos:
            return False

        occ_symbol = pos['occ_symbol']
        tp_id      = pos.get('tp_id')
        sl_id      = pos.get('sl_id')

    print(f"[Reverse] Closing {occ_symbol} due to reverse signal")

    # إلغاء الـ TP و SL أولاً
    if tp_id:
        cancel_order(tp_id)
    if sl_id:
        cancel_order(sl_id)

    time.sleep(1)

    # بيع البوزيشن بسعر السوق فوراً
    close_order = {
        "symbol"       : occ_symbol,
        "qty"          : "5",
        "side"         : "sell",
        "type"         : "market",
        "time_in_force": "day"
    }
    r = requests.post(f"{ALPACA_BASE}/v2/orders", json=close_order, headers=HEADERS, timeout=10)
    print(f"[Reverse] Close status: {r.status_code} | {r.json().get('status','')}")

    # مسح البوزيشن من السجل
    with positions_lock:
        active_positions.pop(symbol, None)

    return True

# ==============================
# مراقبة الـ TP و SL
# ==============================
def monitor_tp_sl(symbol, symbol_occ, tp_id, sl_id):
    print(f"[Monitor] Started for {symbol_occ}")
    max_checks = 120
    checks     = 0

    while checks < max_checks:
        time.sleep(60)
        checks += 1

        # تحقق إذا البوزيشن اتغيرت (إشارة عكسية أغلقتها)
        with positions_lock:
            pos = active_positions.get(symbol)
            if not pos or pos['occ_symbol'] != symbol_occ:
                print(f"[Monitor] Position changed or closed. Stopping monitor for {symbol_occ}")
                break

        try:
            tp_r = requests.get(f"{ALPACA_BASE}/v2/orders/{tp_id}", headers=HEADERS, timeout=10).json()
            sl_r = requests.get(f"{ALPACA_BASE}/v2/orders/{sl_id}", headers=HEADERS, timeout=10).json()

            tp_status = tp_r.get('status', '')
            sl_status = sl_r.get('status', '')

            print(f"[Monitor] {symbol_occ} TP={tp_status} | SL={sl_status}")

            if tp_status == 'filled':
                print(f"[Monitor] TP filled! Cancelling SL")
                cancel_order(sl_id)
                with positions_lock:
                    active_positions.pop(symbol, None)
                break

            elif sl_status == 'filled':
                print(f"[Monitor] SL filled! Cancelling TP")
                cancel_order(tp_id)
                with positions_lock:
                    active_positions.pop(symbol, None)
                break

            elif tp_status in ['cancelled', 'canceled', 'expired']:
                cancel_order(sl_id)
                with positions_lock:
                    active_positions.pop(symbol, None)
                break

            elif sl_status in ['cancelled', 'canceled', 'expired']:
                cancel_order(tp_id)
                with positions_lock:
                    active_positions.pop(symbol, None)
                break

        except Exception as e:
            print(f"[Monitor Error] {e}")

    print(f"[Monitor] Done for {symbol_occ}")

# ==============================
# وضع TP و SL بعد تنفيذ الأوردر
# ==============================
def place_tp_sl(symbol, symbol_occ, order_id):
    filled_price = None
    for attempt in range(5):
        time.sleep(3)
        try:
            r            = requests.get(f"{ALPACA_BASE}/v2/orders/{order_id}", headers=HEADERS, timeout=10)
            data         = r.json()
            filled_price = data.get('filled_avg_price')
            status       = data.get('status', '')
            print(f"[TP/SL] Attempt {attempt+1}: status={status}, filled={filled_price}")
            if filled_price:
                break
        except Exception as e:
            print(f"[TP/SL Error] {e}")

    if not filled_price:
        print(f"[TP/SL] No fill price. Skipping.")
        return

    opt_price = float(filled_price)
    tp_price  = round(opt_price * (1 + TAKE_PROFIT_PCT), 2)
    sl_price  = round(opt_price * (1 - STOP_LOSS_PCT), 2)

    print(f"[TP/SL] Entry={opt_price} | TP={tp_price} | SL={sl_price}")

    tp_order = {
        "symbol"       : symbol_occ,
        "qty"          : "5",
        "side"         : "sell",
        "type"         : "limit",
        "limit_price"  : str(tp_price),
        "time_in_force": "day"
    }
    sl_order = {
        "symbol"       : symbol_occ,
        "qty"          : "5",
        "side"         : "sell",
        "type"         : "stop",
        "stop_price"   : str(sl_price),
        "time_in_force": "day"
    }

    tp_r = requests.post(f"{ALPACA_BASE}/v2/orders", json=tp_order, headers=HEADERS, timeout=10)
    sl_r = requests.post(f"{ALPACA_BASE}/v2/orders", json=sl_order, headers=HEADERS, timeout=10)

    print(f"[TP] {tp_price}: {tp_r.status_code}")
    print(f"[SL] {sl_price}: {sl_r.status_code}")

    tp_id = tp_r.json().get('id') if tp_r.status_code in [200, 201] else None
    sl_id = sl_r.json().get('id') if sl_r.status_code in [200, 201] else None

    if tp_id and sl_id:
        with positions_lock:
            # تحقق إذا البوزيشن لا تزال نفسها (ما اتغيرت بإشارة عكسية)
            pos = active_positions.get(symbol)
            if pos and pos['occ_symbol'] == symbol_occ:
                pos['tp_id'] = tp_id
                pos['sl_id'] = sl_id
                pos['entry'] = opt_price
                pos['tp']    = tp_price
                pos['sl']    = sl_price

        t = threading.Thread(target=monitor_tp_sl, args=(symbol, symbol_occ, tp_id, sl_id))
        t.daemon = True
        t.start()

# ==============================
# الدالة الرئيسية
# ==============================
def place_option_order(symbol, action, signal_time=None):
    print(f"\n{'='*50}")
    print(f"[Signal] {action} {symbol} @ {signal_time}")

    # ==============================
    # تحقق من إشارة عكسية
    # ==============================
    with positions_lock:
        existing = active_positions.get(symbol)

    if existing and existing['action'] != action:
        print(f"[Reverse] Opposite signal! Current={existing['action']} New={action}")
        close_position(symbol)
        time.sleep(2)  # انتظر قليلاً قبل الدخول الجديد

    price  = get_latest_price(symbol)
    expiry = get_expiry(signal_time)
    strike = round(price)

    # QQQ: سترايك +2 للـ CALL و -2 للـ PUT
    if symbol == 'QQQ':
        strike = strike + 2 if action == 'CALL' else strike - 2

    symbol_occ = build_occ_symbol(symbol, expiry, action, strike)
    print(f"[OCC] {symbol_occ} | Price={price} | Strike={strike} | Expiry={expiry}")

    order = {
        "symbol"       : symbol_occ,
        "qty"          : "5",
        "side"         : "buy",
        "type"         : "market",
        "time_in_force": "day"
    }

    r      = requests.post(f"{ALPACA_BASE}/v2/orders", json=order, headers=HEADERS, timeout=10)
    result = r.json()
    print(f"[Buy] Status={r.status_code} | Order={result.get('id','')}")

    if r.status_code in [200, 201]:
        order_id = result.get('id')

        # سجل البوزيشن الجديدة فوراً
        with positions_lock:
            active_positions[symbol] = {
                'occ_symbol': symbol_occ,
                'action'    : action,
                'tp_id'     : None,
                'sl_id'     : None
            }

        t = threading.Thread(target=place_tp_sl, args=(symbol, symbol_occ, order_id))
        t.daemon = True
        t.start()

    return {
        'symbol'    : symbol,
        'action'    : action,
        'price'     : price,
        'strike'    : strike,
        'expiry'    : str(expiry),
        'occ_symbol': symbol_occ,
        'status'    : r.status_code,
        'result'    : result
    }

# ==============================
# Routes
# ==============================

@app.route('/')
def home():
    return 'Trading Bot v3 is Running ✅'

@app.route('/status')
def status():
    with positions_lock:
        return jsonify({
            'active_positions': active_positions,
            'count'           : len(active_positions)
        })

@app.route('/test')
def test():
    now    = datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
    result = place_option_order('SPY', 'CALL', now)
    return jsonify(result)

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data        = request.get_json(force=True, silent=True) or {}
        print(f"[Webhook] Received: {data}")

        action      = data.get('action', '').upper()
        symbol      = data.get('symbol', 'SPY').upper()
        signal_time = data.get('time', None)

        if symbol not in ['SPY', 'QQQ', 'XSP']:
            symbol = 'SPY'

        if action not in ['CALL', 'PUT']:
            return jsonify({'status': 'error', 'message': f'Invalid action: {action}'}), 400

        result = place_option_order(symbol, action, signal_time)
        return jsonify({'status': 'success', 'data': result})

    except Exception as e:
        print(f"[Webhook Error] {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
