from flask import Flask, request, jsonify
import requests, os, datetime

app = Flask(__name__)

ALPACA_KEY     = os.environ.get('ALPACA_KEY')
ALPACA_SECRET  = os.environ.get('ALPACA_SECRET')
ALPACA_BASE    = os.environ.get('ALPACA_BASE_URL', 'https://paper-api.alpaca.markets')

TAKE_PROFIT    = 0.10
STOP_LOSS      = 0.50

HEADERS = {
    'APCA-API-KEY-ID'    : ALPACA_KEY,
    'APCA-API-SECRET-KEY': ALPACA_SECRET
}

def get_latest_price(symbol):
    url = f"https://data.alpaca.markets/v2/stocks/{symbol}/quotes/latest"
    r = requests.get(url, headers=HEADERS)
    return r.json()['quote']['ap']

def place_option_order(symbol, action):
    price    = get_latest_price(symbol)
    today    = datetime.date.today()
    friday   = today + datetime.timedelta((4 - today.weekday()) % 7)
    expiry   = friday.strftime('%Y-%m-%d')
    strike   = round(price / 5) * 5
    opt_type = 'call' if action == 'CALL' else 'put'

    order = {
        "symbol"        : f"{symbol}{friday.strftime('%y%m%d')}{opt_type[0].upper()}{int(strike*1000):08d}",
        "qty"           : "1",
        "side"          : "buy",
        "type"          : "market",
        "time_in_force" : "day"
    }

    url = f"{ALPACA_BASE}/v2/orders"
    r   = requests.post(url, json=order, headers=HEADERS)

    return {
        'symbol' : symbol,
        'action' : action,
        'strike' : strike,
        'expiry' : expiry,
        'status' : r.status_code,
        'result' : r.json()
    }

@app.route('/')
def home():
    return 'Trading Bot is Running ✅'

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data   = request.get_json(force=True)
        action = data.get('action', '')
        symbol = data.get('symbol', 'SPY')

        if symbol not in ['SPY', 'QQQ', 'XSP']:
            symbol = 'SPY'

        result = place_option_order(symbol, action)
        return jsonify({'status': 'success', 'data': result})

    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
