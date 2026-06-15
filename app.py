from flask import Flask, request, jsonify
import requests, os, datetime

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
    except:
        return 755.00

def get_option_price(symbol_occ):
    try:
        url = f"https://data.alpaca.markets/v2/options/snapshots/{symbol_occ}"
        r = requests.get(url, headers=HEADERS)
        data = r.json()
        return data['snapshot']['latestTrade']['p']
    except:
        return None

def get_expiry():
    today = datetime.date.today()
    days_until_friday = (4 - today.weekday()) % 7
    if days_until_friday == 0:
        return today
    return today + datetime.timedelta(days=days_until_friday)

def place_option_order(symbol, action):
    price  = get_latest_price(symbol)
    expiry = get_expiry()
    strike = round(price)
    right  = 'C' if action == 'CALL' else 'P'

    symbol_occ = f"{symbol}{expiry.strftime('%y%m%d')}{right}{int(strike*1000):08d}"

    # نجيب سعر الأوبشن أولاً
    opt_price = get_option_price(symbol_occ)

    if opt_price:
        tp_price = round(opt_price * (1 + TAKE_PROFIT), 2)
        sl_price = round(opt_price * (1 - STOP_LOSS), 2)

        order = {
            "symbol"        : symbol_occ,
            "qty"           : "1",
            "side"          : "buy",
            "type"          : "market",
            "time_in_force" : "day",
            "order_class"   : "bracket",
            "take_profit"   : {"limit_price": str(tp_price)},
            "stop_loss"     : {"stop_price": str(sl_price)}
        }
    else:
        # بدون TP/SL إذا ما قدرنا نجيب السعر
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

    return {
        'symbol'    : symbol,
        'action'    : action,
        'price'     : price,
        'opt_price' : opt_price,
        'strike'    : strike,
        'expiry'    : str(expiry),
        'occ_symbol': symbol_occ,
        'tp'        : round(opt_price * (1 + TAKE_PROFIT), 2) if opt_price else None,
        'sl'        : round(opt_price * (1 - STOP_LOSS), 2) if opt_price else None,
        'status'    : r.status_code,
        'result'    : result
    }

@app.route('/')
def home():
    return 'Trading Bot is Running'

@app.route('/test')
def test():
    result = place_option_order('SPY', 'CALL')
    return jsonify(result)

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data   = request.get_json(force=True, silent=True) or {}
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
