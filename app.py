from flask import Flask, request, jsonify
from ib_insync import *
import os, datetime

app = Flask(__name__)

ACCOUNT = 'DUQ733599'
TAKE_PROFIT = 0.10
STOP_LOSS = 0.50

def connect_ibkr():
    ib = IB()
    ib.connect('trade.interactivebrokers.com', 10090, clientId=1)
    return ib

def place_option_order(symbol, action):
    ib = connect_ibkr()
    
    stock = Stock(symbol, 'SMART', 'USD')
    ib.qualifyContracts(stock)
    ticker = ib.reqMktData(stock)
    ib.sleep(2)
    price = ticker.marketPrice()
    
    today = datetime.date.today()
    friday = today + datetime.timedelta((4 - today.weekday()) % 7)
    expiry = friday.strftime('%Y%m%d')
    strike = round(price / 5) * 5
    right = 'C' if action == 'CALL' else 'P'
    
    option = Option(symbol, expiry, strike, right, 'SMART')
    ib.qualifyContracts(option)
    
    opt_ticker = ib.reqMktData(option)
    ib.sleep(2)
    opt_price = opt_ticker.marketPrice()
    
    tp = round(opt_price * (1 + TAKE_PROFIT), 2)
    sl = round(opt_price * (1 - STOP_LOSS), 2)
    
    bracket = ib.bracketOrder('BUY', 1,
        limitPrice=round(opt_price * 1.01, 2),
        takeProfitPrice=tp,
        stopLossPrice=sl,
        account=ACCOUNT)
    
    for order in bracket:
        ib.placeOrder(option, order)
    
    ib.sleep(2)
    ib.disconnect()
    
    return {'symbol': symbol, 'action': action,
            'strike': strike, 'expiry': expiry,
            'price': opt_price, 'tp': tp, 'sl': sl}

@app.route('/')
def home():
    return 'Trading Bot is Running ✅'

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = request.get_json(force=True)
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
