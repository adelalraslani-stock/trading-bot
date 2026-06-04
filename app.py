from flask import Flask, request, jsonify
import os

app = Flask(__name__)

@app.route('/')
def home():
    return 'Trading Bot is Running ✅'

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = request.get_json(force=True)
        action = data.get('action', '')
        symbol = data.get('symbol', 'SPY')
        price  = data.get('price', '')
        
        print(f"Signal received: {action} {symbol} @ {price}")
        
        return jsonify({
            'status' : 'received',
            'action' : action,
            'symbol' : symbol,
            'price'  : price
        })
    
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
