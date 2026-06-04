from flask import Flask, request, jsonify
from ib_insync import *
import datetime, math, os

app = Flask(__name__)

IBKR_HOST = os.environ.get('IBKR_HOST', '127.0.0.1')
IBKR_PORT = int(os.environ.get('IBKR_PORT', 7497))
TAKE_PROFIT = 0.10
STOP_LOSS   = 0.50

def get_option_contract(symbol, action):
    ib = IB()
    ib.connect(IBKR_HOST, IBKR_PORT, clientId=1)
    
    today     = datetime.date.today()
    friday    = today + datetime.timedelta((4 - today.weekday()) % 7)
    expiry    = friday.strftime('%Y%m%d')
    
    stock     = Stock(symbol, 'SMART', 'USD')
    ib.qualifyContracts(stock)
    ticker    = ib.reqMktData(stock)
    ib.sleep(2)
    price     = ticker.marketPrice()
    
    strike    = round(price / 5) * 5
    right     = 'C' if action == 'CALL' else 'P'
    
    option = Option(symbol, expiry, strike, right, 'SMART')
    ib.qualifyContracts(option)
    
    opt_ticker = ib.reqMktData(option)
    ib.sleep(2)
    opt_price  = opt_ticker.marketPrice()
    
    tp_price = round(opt_price * (1 + TAKE_PROFIT), 2)
    sl_price = round(opt_price * (1 - STOP_LOSS), 2)
    
    bracket = ib.bracketOrder(
        'BUY', 1,
        limitPrice   = round(opt_price * 1.01, 2),
        takeProfitPrice = tp_price,
        stopLossPrice   = sl_price
    )
    
    for order in bracket:
        ib.placeOrder(option, order)
    
    ib.sleep(2)
    ib.disconnect()
    
    return {
        'symbol'    : symbol,
        'action'    : action,
        'strike'    : strike,
        'expiry'    : expiry,
        'opt_price' : opt_price,
        'tp'        : tp_price,
        'sl'        : sl_price
    }

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data   = request.json
        action = data.get('action')
        symbol = data.get('symbol', 'SPY')
        
        allowed = ['SPY', 'QQQ', 'XSP']
        if symbol not in allowed:
            symbol = 'SPY'
        
        result = get_option_contract(symbol, action)
        return jsonify({'status': 'success', 'data': result})
    
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/')
def home():
    return 'Trading Bot is Running ✅'

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
