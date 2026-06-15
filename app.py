from flask import Flask, request, jsonify
import requests, os, datetime, time

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
        data = r.json()
        return data['quote']['ap']
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

def get_filled_price(order_id):
    time.sleep(2)
    url = f"{ALPACA_BASE}/v2/orders/{order_id}"
    r = requests.get(url, headers=HEADERS)
    data = r.json()
    filled_price = data.get('filled_avg_price')
    print(f"Filled price: {filled_price}")
    if filled_price:
        return float(filled_price)
    return None

def place_tp_sl(symbol_occ, opt_price):
    tp_price = round(opt_price * (1 + TAKE_PROFIT), 2)
    sl_price = round(opt_price * (1 - STOP_LOSS), 2)

    print(f"Placing TP: {tp_price}, SL: {sl_price}")

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

    print(f"TP response: {tp_r.status_code} - {tp_r.json()}")
    print(f"SL response: {sl_r.status_code} - {sl_r.json()}")

    return tp_price, sl_price

def place_option_order(symbol, action, signal_time=None):
    print(f"Placing order: {action} {symbol} @ {signal_time}")

    price  = get_latest_price(symbol)
    expiry = get_expiry_from_signal(signal_time)
    strike = round(price)
    right  = 'C' if action == 'CALL' else 'P'

    symbol_occ = f"{symbol}{expiry.strftime('%y%m%d')}{right}{int(strike*1000):08d}"
    print(f"OCC Symbol: {symbol_occ}")

    order = {
        "symbol"       : symbol_occ,
        "qty"          : "1",
        "side"         : "buy",
        "type"         : "market",
        "time_in_force": "day"
    }

    url = f"{ALPACA_BASE}/v2/orders"
    r   = requests.post(url, json=order, headers=HEADERS)
    result = r.json()

    print(f"Buy response: {r.status_code} - {result}")

    tp_price = None
    sl_price = None

    if r.status_code in [200, 201]:
        order_id  = result.get('id')
        opt_price = get_filled_price(order_id)

        if opt_price:
            tp_price, sl_price = place_tp_sl(symbol_occ, opt_price)

    return {
        'symbol'    : symbol,
        'action'    : action,
        'price'     : price,
        'strike'    : strike,
        'expiry'    : str(expiry),
        'occ_symbol': symbol_occ,
        'tp'        : tp_price,
        'sl'        : sl_price,
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
        data = request.get_json(force=True, silent=True) or {}
        print(f"Received webhook: {data}")

        action      = data.get('action', '')
        symbol      = data.get('symbol', 'SPY')
        signal_time = data.get('time', None)

        print(f"Action: {action}, Symbol: {symbol}, Time: {signal_time}")

        if symbol not in ['SPY', 'QQQ', 'XSP']:
            symbol = 'SPY'

        if not action:
            print("No action found!")
            return jsonify({'status': 'error', 'message': 'no action'}), 400

        result = place_option_order(symbol, action, signal_time)
        return jsonify({'status': 'success', 'data': result})

    except Exception as e:
        print(f"Webhook error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
