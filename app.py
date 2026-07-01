from flask import Flask, request, jsonify
import requests, os, datetime, threading, time

app = Flask(__name__)

ALPACA_KEY    = os.environ.get('ALPACA_KEY')
ALPACA_SECRET = os.environ.get('ALPACA_SECRET')
ALPACA_BASE   = os.environ.get('ALPACA_BASE_URL', 'https://paper-api.alpaca.markets')

# ==============================
# إعدادات الخسارة (ثابتة)
# ==============================
STOP_LOSS_PCT = 0.50   # 50% خسارة

# نسب الربح لكل نافذة زمنية
TP_WINDOW1 = 0.10   # 10% — نافذة 4:45-6:30 PM السعودية
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
            print(f"[Price Error] Invalid price returned: {price}")
            return None
        return price
    except Exception as e:
        print(f"[Price Error] Failed to fetch price for {symbol}: {e}")
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
# فلتر الوقت — يرجع رقم النافذة أو None إذا خارج الأوقات
# ==============================
def get_trading_window(signal_time=None):
    """
    يرجع:
    - 1 إذا داخل نافذة 4:45-6:30 PM السعودية (TP=10%)
    - 2 إذا داخل نافذة 8:10-10:00 PM السعودية (TP=5%)
    - None إذا خارج الأوقات المسموحة
    """
    try:
        if signal_time:
            signal_dt = datetime.datetime.fromisoformat(signal_time.replace('Z', '+00:00'))
        else:
            signal_dt = datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc)

        ny_time = signal_dt - datetime.timedelta(hours=4)  # UTC-4 (EDT)

        # النافذة الأولى: 4:45-6:30 PM السعودية = 9:45-11:30 AM ET → TP 10%
        window1_start = ny_time.replace(hour=9,  minute=45, second=0, microsecond=0)
        window1_end   = ny_time.replace(hour=11, minute=30, second=0, microsecond=0)

        # النافذة الثانية: 8:10-10:00 PM السعودية = 1:10-3:00 PM ET → TP 5%
        window2_start = ny_time.replace(hour=13, minute=10, second=0, microsecond=0)
        window2_end   = ny_time.replace(hour=15, minute=0,  second=0, microsecond=0)

        if window1_start <= ny_time < window1_end:
            print(f"[Filter] Window 1 — 4:45-6:30 PM KSA | TP=10%")
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
# جلب البوزيشنات المفتوحة من Alpaca مباشرة
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
                        right = sym[9]
                        action = 'CALL' if right == 'C' else 'PUT'
                    except:
                        action = 'CALL'
                    positions[base] = {
                        'occ_symbol'       : sym,
                        'action'           : action,
                        'unrealized_plpc'  : float(pos.get('unrealized_plpc', 0)),
                        'current_price'    : float(pos.get('current_price', 0)),
                        'avg_entry_price'  : float(pos.get('avg_entry_price', 0)),
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
        print(f"[Cancel] Order {order_id}: {r.status_code}")
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
def close_position_market(occ_symbol, qty="5"):
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
# مراقبة الـ TP و SL — يعتمد على Alpaca API مباشرة
# ==============================
def monitor_tp_sl(symbol, symbol_occ, tp_id, entry_price):
    print(f"[Monitor] Started for {symbol_occ}")
    max_checks = 480
    checks     = 0

    while checks < max_checks:
        time.sleep(30)
        checks += 1

        try:
            tp_r      = requests.get(f"{ALPACA_BASE}/v2/orders/{tp_id}", headers=HEADERS, timeout=10)
            tp_status = tp_r.json().get('status', '') if tp_r.status_code == 200 else 'unknown'
            print(f"[Monitor] {symbol_occ} | TP={tp_status} | Check={checks}")

            if tp_status == 'filled':
                print(f"[Monitor] TP filled for {symbol_occ}")
                break

            if tp_status in ['cancelled', 'canceled', 'expired']:
                print(f"[Monitor] TP cancelled/expired for {symbol_occ}")
                break

            pos_r = requests.get(f"{ALPACA_BASE}/v2/positions/{symbol_occ}", headers=HEADERS, timeout=10)

            if pos_r.status_code == 404:
                print(f"[Monitor] Position already closed for {symbol_occ}")
                break

            if pos_r.status_code == 200:
                pos_data          = pos_r.json()
                unrealized_pl_pct = float(pos_data.get('unrealized_plpc', 0))
                current_price     = float(pos_data.get('current_price', 0))
                print(f"[Monitor] P/L={unrealized_pl_pct:.2%} | Price={current_price} | SL threshold=-{STOP_LOSS_PCT:.0%}")

                if unrealized_pl_pct <= -STOP_LOSS_PCT:
                    print(f"[Monitor] SL triggered! P/L={unrealized_pl_pct:.2%} — Closing {symbol_occ}")
                    close_position_market(symbol_occ)
                    break

        except Exception as e:
            print(f"[Monitor Error] {e}")

    print(f"[Monitor] Done for {symbol_occ}")

# ==============================
# وضع TP بعد تنفيذ الأوردر + بدء مراقبة SL
# ==============================
def place_tp_and_monitor(symbol, symbol_occ, order_id, take_profit_pct):
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
        print(f"[TP] No fill price after 8 attempts. Skipping.")
        return

    opt_price = float(filled_price)
    tp_price  = round(opt_price * (1 + take_profit_pct), 2)
    sl_price  = round(opt_price * (1 - STOP_LOSS_PCT), 2)

    print(f"[TP] Entry={opt_price} | TP={tp_price} ({take_profit_pct*100:.0f}%) | SL={sl_price} (internal)")

    tp_order = {
        "symbol"       : symbol_occ,
        "qty"          : "5",
        "side"         : "sell",
        "type"         : "limit",
        "limit_price"  : str(tp_price),
        "time_in_force": "day"
    }
    tp_r  = requests.post(f"{ALPACA_BASE}/v2/orders", json=tp_order, headers=HEADERS, timeout=10)
    tp_id = tp_r.json().get('id') if tp_r.status_code in [200, 201] else None
    print(f"[TP] Order status={tp_r.status_code} | id={tp_id}")

    if not tp_id:
        print(f"[TP] Failed to place TP order!")
        tp_id = order_id

    t = threading.Thread(target=monitor_tp_sl, args=(symbol, symbol_occ, tp_id, opt_price))
    t.daemon = True
    t.start()

# ==============================
# الدالة الرئيسية لتنفيذ الصفقة
# ==============================
def place_option_order(symbol, action, signal_time=None, take_profit_pct=0.10):
    print(f"\n{'='*50}")
    print(f"[Signal] {action} {symbol} @ {signal_time}")

    open_positions = get_open_positions()
    existing       = open_positions.get(symbol)

    if existing:
        print(f"[Check] Open position found: {existing['occ_symbol']} | Action={existing['action']}")

        if existing['action'] != action:
            print(f"[Reverse] Closing {existing['occ_symbol']} — opposite signal received")
            close_position_market(existing['occ_symbol'])
            time.sleep(2)
        else:
            print(f"[Skip] Same direction already open. Skipping.")
            return {'status': 'skipped', 'reason': 'same direction already open'}

    price  = get_latest_price(symbol)

    if price is None:
        print(f"[Abort] Could not fetch price for {symbol}. Signal ignored — no trade placed.")
        return {'status': 'error', 'message': f'Price fetch failed for {symbol} — trade aborted for safety'}

    expiry = get_expiry(signal_time)
    strike = round(price)

    # QQQ: نفس سترايك الإشارة (بدون تعديل)

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
        t = threading.Thread(target=place_tp_and_monitor, args=(symbol, symbol_occ, order_id, take_profit_pct))
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
    return 'Trading Bot v4 - 10% TP'

@app.route('/status')
def status():
    positions = get_open_positions()
    return jsonify({
        'active_positions': positions,
        'count'           : len(positions)
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

        if symbol not in ['SPY', 'QQQ']:
            symbol = 'SPY'

        if action not in ['CALL', 'PUT']:
            return jsonify({'status': 'error', 'message': f'Invalid action: {action}'}), 400

        # فلتر الوقت — تحديد النافذة الزمنية ونسبة الربح
        window = get_trading_window(signal_time)
        if window is None:
            return jsonify({'status': 'ignored', 'message': 'Outside trading windows — signal ignored'})

        # نسبة الربح حسب النافذة
        take_profit_pct = TP_WINDOW1 if window == 1 else TP_WINDOW2
        print(f"[Webhook] Window={window} | TP={take_profit_pct*100:.0f}%")

        result = place_option_order(symbol, action, signal_time, take_profit_pct)
        return jsonify({'status': 'success', 'data': result})

    except Exception as e:
        print(f"[Webhook Error] {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
