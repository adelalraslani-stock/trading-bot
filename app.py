from flask import Flask, request, jsonify
import requests, os, datetime, threading

app = Flask(__name__)

ALPACA_KEY    = os.environ.get('ALPACA_KEY')
ALPACA_SECRET = os.environ.get('ALPACA_SECRET')
ALPACA_BASE   = os.environ.get('ALPACA_BASE_URL', 'https://paper-api.alpaca.markets')

TAKE_PROFIT   = 0.05
STOP_LOSS     = 0.50

HEADERS = {
    'APCA-API-KEY-ID'    : ALPACA_KEY,
    'APCA-API-SECRET-KEY': ALPACA_SECRET
}

def get_latest_price(symbol):
    try:
        url = f"https://data.alpaca.markets/v2/stocks/{symbol}/quotes/latest"
        r = requests.get(url, headers=HEADERS)
        return r.json()['quote']['ap']
    except Exception as e:
        print(f"Price error: {e}")
        return 755.00

def get_expiry_from_signal(signal_time):
    try:
        signal_dt = datetime.datetime.fromisoformat(signal_time.replace('Z', '+00:00'))
        ny_time = signal_dt - datetime.timedelta(hours=4)
        return ny_time.date()
    except Exception as e:
        print(f"Expiry error: {e}")
        return datetime.date.today()

def place_tp_sl(symbol_occ, order_id):
    import time
    # ننتظر 3 ثواني فقط للتأكد من التنفيذ
    time.sleep(3)
    try:
        r = requests.get(f"{ALPACA_BASE}/v2/orders/{order_id}", headers=HEADERS)
        filled_price = r.json().get('filled_avg_price')
        print(f"Filled: {filled_price}")

        if not filled_price:
            time.sleep(3)
            r = requests.get(f"{ALPACA_BASE}/v2/orders/{order_id}", headers=HEADERS)
            filled_price = r.json().get('filled_avg_price')

        if filled_price:
            opt_price = float(filled_price)
            tp_price  = round(opt_price * (1 + TAKE_PROFIT), 2)
            sl_price  = round(opt_price * (1 - STOP_LOSS), 2)

            tp_order = {
                "symbol"       : symbol_occ,
                "qty"          : "1",
                "side"         : "sell",
                "type"         : "limit",
                "limit_price"  : str(tp_price),
                "time_in_force": "gtc"
            }
            sl_order = {
                "symbol"       : symbol_occ,
                "qty"          : "1",
                "side"         : "sell",
                "type"         : "stop",
                "stop_price"   : str(sl_price),
                "time_in_force": "gtc"
            }

            tp_r = requests.post(f"{ALPACA_BASE}/v2/orders", json=tp_order, headers=HEADERS)
            sl_r = requests.post(f"{ALPACA_BASE}/v2/orders", json=sl_order, headers=HEADERS)

            print(f"TP {tp_price}: {tp_r.status_code}")
            print(f"SL {sl_price}: {sl_r.status_code}")
        else:
            print("No filled price found")

    except Exception as e:
        print(f"TP/SL error: {e}")

def place_option_order(symbol, action, signal_time=None):
    print(f"Signal: {action} {symbol} @ {signal_time}")

    price  = get_latest_price(symbol)
    expiry = get_expiry_from_signal(signal_time)
    strike = round(price)
    right  = 'C' if action == 'CALL' else 'P'

    symbol_occ = f"{symbol}{expiry.strftime('%y%m%d')}{right}{int(strike*1000):08d}"
    print(f"OCC: {symbol_occ}")

    order = {
        "symbol"       : symbol_occ,
        "qty"          : "1",
        "side"         : "buy",
        "type"         : "market",
        "time_in_force": "day"
    }

    r      = requests.post(f"{ALPACA_BASE}/v2/orders", json=order, headers=HEADERS)
    result = r.json()
    print(f"Buy: {r.status_code}")

    if r.status_code in [200, 201]:
        order_id = result.get('id')
        t = threading.Thread(target=place_tp_sl, args=(symbol_occ, order_id))
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

@app.route('/')
def home():
    return 'Trading Bot is Running'

@app.route('/test')
def test():
    now = datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
    result = place_option_order('SPY', 'CALL', now)
    return jsonify(result)

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data        = request.get_json(force=True, silent=True) or {}
        print(f"Webhook: {data}")
        action      = data.get('action', '')
        symbol      = data.get('symbol', 'SPY')
        signal_time = data.get('time', None)

        if symbol not in ['SPY', 'QQQ', 'XSP']:
            symbol = 'SPY'

        if not action:
            return jsonify({'status': 'error', 'message': 'no action'}), 400

        result = place_option_order(symbol, action, signal_time)
        return jsonify({'status': 'success', 'data': result})

    except Exception as e:
        print(f"Error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
