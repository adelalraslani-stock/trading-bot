from flask import Flask, request, jsonify
import requests, os, datetime, threading, time

app = Flask(__name__)

ALPACA_KEY    = os.environ.get('ALPACA_KEY')
ALPACA_SECRET = os.environ.get('ALPACA_SECRET')
ALPACA_BASE   = os.environ.get('ALPACA_BASE_URL', 'https://paper-api.alpaca.markets')

TELEGRAM_TOKEN   = os.environ.get('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')

TIMEFRAME       = "3m"
TAKE_PROFIT_PCT = 0.50   # الهدف الأقصى — بيع كامل فوري عند الوصول له (+50%)
STOP_LOSS_PCT   = 0.30   # وقف الخسارة الابتدائي (-30%) — قبل ما يتفعّل رفع الوقف
# ==============================
# منطق وقف الخسارة المتحرك (مثال 1 — وقف يقفز ويثبت):
#   - يشتري العقد ووقف الخسارة الابتدائي -30%
#   - أول ما يحقق ربح +15% -> يُرفع الوقف ليصير عند +15% ويثبت هناك
#   - يستمر مع الصفقة صعودًا حتى:
#       * إما يوصل الهدف الأقصى +50% -> يبيع (ربح كامل)
#       * أو يرجع وينزل للوقف المثبّت +15% -> يبيع (ربح مؤمّن +15%)
#       * أو (قبل تحقيق 15%) ينزل للوقف الابتدائي -30% -> يبيع
# TRAIL_TRIGGER_PCT: عتبة تفعيل رفع الوقف | TRAIL_LOCK_PCT: مستوى الوقف بعد التفعيل
# ==============================
TRAIL_TRIGGER_PCT = 0.15   # عند تحقيق +15% يُرفع الوقف
TRAIL_LOCK_PCT    = 0.15   # الوقف يثبت عند +15%
ORDER_QTY       = "10"
ALLOWED_SYMBOLS = ["SPY", "QQQ", "META", "AVGO", "MSTR"]
HEADERS = {
    'APCA-API-KEY-ID'    : ALPACA_KEY,
    'APCA-API-SECRET-KEY': ALPACA_SECRET
}

KSA_TZ = datetime.timezone(datetime.timedelta(hours=3))

# ==============================
# التعديل: حالة الإيقاف المؤقت (pause/resume) عبر أوامر تيلجرام
# ==============================
bot_paused      = False
bot_paused_lock = threading.Lock()

# ==============================
# حماية من الإشارات المكررة (إعادة إرسال TradingView عند التايم آوت)
# أي إشارة مطابقة (رمز+اتجاه) خلال 90 ثانية تُتجاهل فورًا
# ==============================
DEDUP_WINDOW_SEC = 90
_last_signals = {}
_dedup_lock = threading.Lock()

def is_duplicate_signal(symbol, action):
    key = f"{symbol}:{action}"
    now = time.time()
    with _dedup_lock:
        last = _last_signals.get(key)
        if last is not None and (now - last) < DEDUP_WINDOW_SEC:
            return True
        _last_signals[key] = now
        return False

# ==============================
# التعديل: تتبع ربح/خسارة اليوم (يُصفَّر تلقائيًا عند تغيّر التاريخ بتوقيت السعودية)
# ==============================
daily_pnl = {'date': None, 'total': 0.0, 'wins': 0, 'losses': 0}
daily_pnl_lock = threading.Lock()

# ==============================
# التعديل: حجم الصفقة الديناميكي بناءً على نسبة من رصيد المحفظة
# ==============================
POSITION_SIZE_PCT = 0.50   # نسبة الكاش المستخدمة لكل صفقة (البقية احتياطي)

# ==============================
# التعديل: هدف يومي (صفقتين ناجحتين) + سقف فشل (3 صفقات) — على SPY و QQQ فقط
# ==============================
DAILY_LIMIT_SYMBOLS = ["SPY", "QQQ"]
DAILY_WIN_TARGET    = 2
DAILY_LOSS_LIMIT    = 3

daily_trade_state = {'date': None, 'wins': 0, 'losses': 0, 'stopped': False, 'stop_reason': None}
daily_trade_lock  = threading.Lock()


def get_ksa_date_str():
    return datetime.datetime.now(KSA_TZ).strftime('%Y-%m-%d')


def record_trade_pnl(pnl_amount):
    """يسجّل نتيجة صفقة مغلقة ضمن إحصائيات اليوم الحالي (يُصفَّر تلقائيًا كل يوم جديد)."""
    with daily_pnl_lock:
        today = get_ksa_date_str()
        if daily_pnl['date'] != today:
            daily_pnl['date']   = today
            daily_pnl['total']  = 0.0
            daily_pnl['wins']   = 0
            daily_pnl['losses'] = 0
        daily_pnl['total'] += pnl_amount
        if pnl_amount >= 0:
            daily_pnl['wins'] += 1
        else:
            daily_pnl['losses'] += 1


def record_daily_trade_result(symbol, is_win):
    """يسجّل نتيجة صفقة SPY/QQQ ضمن هدف اليوم (صفقتان ناجحتان) وسقف الفشل (3 صفقات).
    يوقف استقبال إشارات جديدة على SPY/QQQ تلقائيًا عند تحقق أي من الشرطين، حتى يبدأ يوم تداول جديد."""
    if symbol not in DAILY_LIMIT_SYMBOLS:
        return
    with daily_trade_lock:
        today = get_ksa_date_str()
        if daily_trade_state['date'] != today:
            daily_trade_state['date']        = today
            daily_trade_state['wins']        = 0
            daily_trade_state['losses']      = 0
            daily_trade_state['stopped']     = False
            daily_trade_state['stop_reason'] = None

        if daily_trade_state['stopped']:
            return  # اليوم متوقف أصلاً، لا داعي لإعادة الفحص

        if is_win:
            daily_trade_state['wins'] += 1
        else:
            daily_trade_state['losses'] += 1

        if daily_trade_state['wins'] >= DAILY_WIN_TARGET:
            daily_trade_state['stopped']     = True
            daily_trade_state['stop_reason'] = 'هدف'
            send_telegram_message(
                f"🏁 تم تحقيق الهدف اليومي (SPY/QQQ)\n\n"
                f"تحققت {DAILY_WIN_TARGET} صفقة ناجحة اليوم — تم إيقاف استقبال إشارات جديدة على SPY/QQQ حتى يوم تداول جديد.\n\n"
                f"الصفقات الرابحة: {daily_trade_state['wins']}\n"
                f"الصفقات غير الرابحة: {daily_trade_state['losses']}"
            )
        elif daily_trade_state['losses'] >= DAILY_LOSS_LIMIT:
            daily_trade_state['stopped']     = True
            daily_trade_state['stop_reason'] = 'خسائر'
            send_telegram_message(
                f"🛑 تم بلوغ الحد الأقصى للمحاولات الفاشلة (SPY/QQQ)\n\n"
                f"{DAILY_LOSS_LIMIT} صفقات بدون ربح حقيقي اليوم — تم إيقاف استقبال إشارات جديدة على SPY/QQQ حتى يوم تداول جديد.\n\n"
                f"الصفقات الرابحة: {daily_trade_state['wins']}\n"
                f"الصفقات غير الرابحة: {daily_trade_state['losses']}"
            )


def is_daily_limit_reached(symbol):
    """يتحقق هل SPY/QQQ متوقفة اليوم بسبب تحقيق الهدف أو بلوغ سقف الفشل."""
    if symbol not in DAILY_LIMIT_SYMBOLS:
        return False
    with daily_trade_lock:
        today = get_ksa_date_str()
        if daily_trade_state['date'] != today:
            return False   # يوم جديد لم يبدأ عدّه بعد
        return daily_trade_state['stopped']


def now_ksa_text():
    now = datetime.datetime.now(KSA_TZ)
    return now.strftime('%Y-%m-%d'), now.strftime('%H:%M:%S')


def send_telegram_message(message):
    try:
        if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
            print("[Telegram] Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID")
            return False
        url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        data = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
        r    = requests.post(url, data=data, timeout=10)
        print(f"[Telegram] Status={r.status_code}")
        return r.status_code == 200
    except Exception as e:
        print(f"[Telegram Error] {e}")
        return False


def get_ksa_time(signal_time=None):
    try:
        if signal_time:
            signal_dt = datetime.datetime.fromisoformat(signal_time.replace('Z', '+00:00'))
        else:
            signal_dt = datetime.datetime.now(datetime.timezone.utc)
        return signal_dt.astimezone(KSA_TZ)
    except Exception as e:
        print(f"[Time Error] {e}")
        return datetime.datetime.now(KSA_TZ)


def should_accept_signal(signal_time=None):
    ksa_time     = get_ksa_time(signal_time)
    current_time = ksa_time.time()
    market_start = datetime.time(16, 45)
    market_end   = datetime.time(22, 15)
    if market_start <= current_time <= market_end:
        return True
    print(f"[Time Filter] Ignored | KSA={ksa_time.strftime('%H:%M:%S')}")
    return False


def get_latest_price(symbol):
    try:
        url   = f"https://data.alpaca.markets/v2/stocks/{symbol}/quotes/latest"
        r     = requests.get(url, headers=HEADERS, timeout=10)
        price = float(r.json()['quote']['ap'])
        if price <= 0:
            return None
        print(f"[Price] {symbol} = {price}")
        return price
    except Exception as e:
        print(f"[Price Error] {symbol}: {e}")
        return None


def get_option_latest_price(occ_symbol):
    """يجلب آخر سعر (ask) لعقد الأوبشن من بيانات أسعار الأوبشن عند Alpaca (لحساب حجم الصفقة).
    يرجع None لو تعذّر الجلب — الكود المستدعي يرجع للكمية الثابتة الاحتياطية عندها."""
    try:
        url = "https://data.alpaca.markets/v1beta1/options/quotes/latest"
        r   = requests.get(url, headers=HEADERS, params={'symbols': occ_symbol}, timeout=10)
        if r.status_code == 200:
            data  = r.json()
            quote = (data.get('quotes') or {}).get(occ_symbol, {})
            ask   = quote.get('ap') or quote.get('bp')
            if ask and float(ask) > 0:
                return float(ask)
        return None
    except Exception as e:
        print(f"[Option Price Error] {occ_symbol}: {e}")
        return None


def calculate_order_qty(occ_symbol):
    """يحسب عدد العقود بناءً على نسبة من رصيد المحفظة (POSITION_SIZE_PCT) وسعر العقد الفعلي، بحد أدنى عقد واحد.
    لو تعذّر جلب سعر العقد أو بيانات الحساب، يرجع الكمية الثابتة الافتراضية (ORDER_QTY) كخيار احتياطي آمن."""
    try:
        account      = get_account_info()
        option_price = get_option_latest_price(occ_symbol)
        if not account or not option_price:
            print(f"[Position Sizing] فشل الجلب — استخدام الكمية الافتراضية {ORDER_QTY}")
            return ORDER_QTY

        available_cash = float(account.get('cash', 0))
        budget         = available_cash * POSITION_SIZE_PCT
        qty            = int(budget // (option_price * 100))
        if qty < 1:
            qty = 1

        print(f"[Position Sizing] Cash=${available_cash:.2f} | Budget(={POSITION_SIZE_PCT:.0%})=${budget:.2f} | OptionAsk=${option_price} | Qty={qty}")
        return str(qty)
    except Exception as e:
        print(f"[Position Sizing Error] {e}")
        return ORDER_QTY


def get_expiry(symbol, signal_time=None):
    try:
        if signal_time:
            signal_dt = datetime.datetime.fromisoformat(signal_time.replace('Z', '+00:00'))
        else:
            signal_dt = datetime.datetime.now(datetime.timezone.utc)
        ny_time  = signal_dt - datetime.timedelta(hours=4)
        today_ny = ny_time.date()
        if symbol in ["META", "AVGO", "MSTR"]:
            days_ahead = (4 - today_ny.weekday()) % 7
            if days_ahead == 0:
                days_ahead = 7
            expiry = today_ny + datetime.timedelta(days=days_ahead)
        else:
            next_day = today_ny + datetime.timedelta(days=1)
            while next_day.weekday() >= 5:
                next_day += datetime.timedelta(days=1)
            expiry = next_day
        return expiry
    except Exception as e:
        print(f"[Expiry Error] {e}")
        return datetime.date.today()


def build_occ_symbol(symbol, expiry, action, strike):
    right = 'C' if action == 'CALL' else 'P'
    return f"{symbol}{expiry.strftime('%y%m%d')}{right}{int(strike * 1000):08d}"


def detect_option_action(occ_symbol):
    try:
        for base in ALLOWED_SYMBOLS:
            if occ_symbol.startswith(base):
                right_index = len(base) + 6
                right = occ_symbol[right_index]
                return "CALL" if right == "C" else "PUT"
        return "CALL"
    except Exception as e:
        print(f"[Detect Error] {e}")
        return "CALL"


def format_contract_label(occ_symbol):
    try:
        for base in ALLOWED_SYMBOLS:
            if occ_symbol.startswith(base):
                right_index = len(base) + 6
                right       = occ_symbol[right_index]
                strike_raw  = occ_symbol[right_index + 1:]
                strike      = int(strike_raw) / 1000
                action      = "CALL" if right == "C" else "PUT"
                if strike.is_integer():
                    strike = int(strike)
                return f"{base} {strike} {action}"
        return occ_symbol
    except Exception as e:
        print(f"[Contract Format Error] {e}")
        return occ_symbol


def get_open_positions():
    try:
        r = requests.get(f"{ALPACA_BASE}/v2/positions", headers=HEADERS, timeout=10)
        if r.status_code != 200:
            return {}
        positions = {}
        for pos in r.json():
            sym = pos.get('symbol', '')
            for base in ALLOWED_SYMBOLS:
                if sym.startswith(base):
                    action = detect_option_action(sym)
                    positions[base] = {
                        'occ_symbol'     : sym,
                        'contract_label' : format_contract_label(sym),
                        'action'         : action,
                        'qty'            : pos.get('qty', ORDER_QTY),
                        'unrealized_plpc': float(pos.get('unrealized_plpc', 0)),
                        'current_price'  : float(pos.get('current_price', 0)),
                        'avg_entry_price': float(pos.get('avg_entry_price', 0)),
                    }
                    break
        return positions
    except Exception as e:
        print(f"[Positions Error] {e}")
        return {}


def cancel_order(order_id):
    try:
        r = requests.delete(f"{ALPACA_BASE}/v2/orders/{order_id}", headers=HEADERS, timeout=10)
        print(f"[Cancel] {order_id} | Status={r.status_code}")
    except Exception as e:
        print(f"[Cancel Error] {e}")


def cancel_all_orders_for_symbol(occ_symbol):
    try:
        r = requests.get(f"{ALPACA_BASE}/v2/orders?status=open&limit=100", headers=HEADERS, timeout=10)
        if r.status_code == 200:
            for order in r.json():
                if order.get('symbol') == occ_symbol:
                    cancel_order(order.get('id'))
    except Exception as e:
        print(f"[Cancel All Error] {e}")


def close_position_market(occ_symbol, qty=ORDER_QTY, reason="خروج"):
    try:
        cancel_all_orders_for_symbol(occ_symbol)
        time.sleep(0.5)
        close_order = {
            "symbol"       : occ_symbol,
            "qty"          : str(qty),
            "side"         : "sell",
            "type"         : "market",
            "time_in_force": "day"
        }
        r = requests.post(f"{ALPACA_BASE}/v2/orders", json=close_order, headers=HEADERS, timeout=10)
        print(f"[Close] {occ_symbol} | Status={r.status_code}")
        return r.status_code in [200, 201]
    except Exception as e:
        print(f"[Close Error] {e}")
        return False


def get_account_info():
    try:
        r = requests.get(f"{ALPACA_BASE}/v2/account", headers=HEADERS, timeout=10)
        if r.status_code == 200:
            return r.json()
        return None
    except Exception as e:
        print(f"[Account Error] {e}")
        return None


def close_all_open_positions():
    """يغلق كل الصفقات المفتوحة فورًا بغض النظر عن الربح/الخسارة، ويسجّل النتيجة في إحصائيات اليوم."""
    positions = get_open_positions()
    if not positions:
        return []
    closed = []
    for symbol, pos in positions.items():
        try:
            qty             = pos.get('qty', ORDER_QTY)
            current_price   = pos.get('current_price', 0)
            avg_entry_price = pos.get('avg_entry_price', 0)
            pnl_amount      = (current_price - avg_entry_price) * float(qty) * 100
            ok = close_position_market(pos['occ_symbol'], qty, "إغلاق يدوي")
            if ok:
                record_trade_pnl(pnl_amount)
                record_daily_trade_result(symbol, pnl_amount > 0)
                closed.append({'symbol': symbol, 'contract': pos['contract_label'], 'pnl': pnl_amount})
        except Exception as e:
            print(f"[Close All Position Error] {symbol}: {e}")
    return closed


def breakeven_all_open_positions():
    """يبيع فقط الصفقات المفتوحة اللي وصلت لنقطة التعادل أو أعلى (ربح ≥ 0%)، ويتجاهل الصفقات الخاسرة حاليًا."""
    positions = get_open_positions()
    if not positions:
        return [], []
    closed, skipped = [], []
    for symbol, pos in positions.items():
        try:
            unrealized_pl_pct = pos.get('unrealized_plpc', 0)
            qty                = pos.get('qty', ORDER_QTY)
            current_price      = pos.get('current_price', 0)
            avg_entry_price    = pos.get('avg_entry_price', 0)
            if unrealized_pl_pct >= 0:
                pnl_amount = (current_price - avg_entry_price) * float(qty) * 100
                ok = close_position_market(pos['occ_symbol'], qty, "بيع براس المال")
                if ok:
                    record_trade_pnl(pnl_amount)
                    record_daily_trade_result(symbol, pnl_amount > 0)
                    closed.append({'symbol': symbol, 'contract': pos['contract_label'], 'pnl': pnl_amount})
            else:
                skipped.append({'symbol': symbol, 'contract': pos['contract_label'], 'pl_pct': unrealized_pl_pct})
        except Exception as e:
            print(f"[Breakeven Position Error] {symbol}: {e}")
    return closed, skipped


def monitor_tp_sl(symbol, occ_symbol, entry_price, qty_ordered, timeframe):
    print(f"[Monitor] Started for {occ_symbol}")
    max_checks = 480
    checks     = 0

    # منطق الوقف المتحرك (مثال 1): trail_active=False يعني الوقف لسه ابتدائي -30%
    # أول ما يحقق +15% -> trail_active=True والوقف يقفز ويثبت عند +15%
    trail_active = False

    while checks < max_checks:
        time.sleep(30)
        checks += 1
        try:
            pos_r = requests.get(f"{ALPACA_BASE}/v2/positions/{occ_symbol}", headers=HEADERS, timeout=10)
            if pos_r.status_code == 404:
                print(f"[Monitor] Position closed: {occ_symbol}")
                break
            if pos_r.status_code != 200:
                continue

            pos_data          = pos_r.json()
            unrealized_pl_pct = float(pos_data.get('unrealized_plpc', 0))
            current_price     = float(pos_data.get('current_price', 0))
            avg_entry_price   = float(pos_data.get('avg_entry_price', 0))
            qty               = pos_data.get('qty', qty_ordered)
            contract_label    = format_contract_label(occ_symbol)

            try:
                total_pnl = (current_price - avg_entry_price) * float(qty) * 100
            except:
                total_pnl = 0

            # تفعيل الوقف المتحرك: أول ما يلمس +15% يُرفع الوقف ويثبت عند +15%
            if not trail_active and unrealized_pl_pct >= TRAIL_TRIGGER_PCT:
                trail_active = True
                print(f"[Monitor] {occ_symbol} | 🔒 تم رفع الوقف إلى +{TRAIL_LOCK_PCT:.0%} (تحقق +{unrealized_pl_pct:.2%})")
                date_txt, time_txt = now_ksa_text()
                send_telegram_message(
                    f"🔒 تم رفع وقف الخسارة\n\n"
                    f"الرمز: {symbol}\n"
                    f"العقد: {contract_label}\n"
                    f"الصفقة حققت +{unrealized_pl_pct:.2%}\n"
                    f"الوقف الآن مثبّت عند: +{TRAIL_LOCK_PCT:.0%}\n"
                    f"الهدف الأقصى: +{TAKE_PROFIT_PCT:.0%}\n\n"
                    f"التاريخ: {date_txt}\n"
                    f"الساعة: {time_txt}"
                )

            stop_label = f"+{TRAIL_LOCK_PCT:.0%} (مرفوع)" if trail_active else f"-{STOP_LOSS_PCT:.0%} (ابتدائي)"
            print(f"[Monitor] {occ_symbol} | P/L={unrealized_pl_pct:.2%} | Stop={stop_label} | Price={current_price} | Check={checks}")

            # 1) وصول الهدف الأقصى +50% -> بيع كامل فوري (صفقة ناجحة)
            if unrealized_pl_pct >= TAKE_PROFIT_PCT:
                close_position_market(occ_symbol, qty, "ربح")
                record_trade_pnl(total_pnl)
                record_daily_trade_result(symbol, True)
                date_txt, time_txt = now_ksa_text()
                send_telegram_message(
                    f"🎯 تم البيع على الهدف الأقصى\n\n"
                    f"الرمز: {symbol}\n"
                    f"الفريم: {timeframe}\n"
                    f"العقد: {contract_label}\n"
                    f"الكمية: {qty}\n"
                    f"سعر الدخول: {avg_entry_price}\n"
                    f"سعر البيع: {current_price}\n"
                    f"نسبة الربح: {unrealized_pl_pct:.2%}\n"
                    f"💰 إجمالي الربح: ${total_pnl:+.2f}\n\n"
                    f"التاريخ: {date_txt}\n"
                    f"الساعة: {time_txt}"
                )
                break

            # 2) الوقف مرفوع وارتد لـ +15% -> بيع بربح مؤمّن (صفقة ناجحة)
            if trail_active and unrealized_pl_pct <= TRAIL_LOCK_PCT:
                close_position_market(occ_symbol, qty, "وقف مرفوع +15%")
                record_trade_pnl(total_pnl)
                record_daily_trade_result(symbol, True)
                date_txt, time_txt = now_ksa_text()
                send_telegram_message(
                    f"🔒 تم البيع عند الوقف المرفوع\n\n"
                    f"الرمز: {symbol}\n"
                    f"الفريم: {timeframe}\n"
                    f"العقد: {contract_label}\n"
                    f"الكمية: {qty}\n"
                    f"سعر الدخول: {avg_entry_price}\n"
                    f"سعر البيع: {current_price}\n"
                    f"الوقف المرفوع: +{TRAIL_LOCK_PCT:.0%}\n"
                    f"نسبة الربح عند البيع: {unrealized_pl_pct:.2%}\n"
                    f"💰 إجمالي الربح: ${total_pnl:+.2f}\n\n"
                    f"التاريخ: {date_txt}\n"
                    f"الساعة: {time_txt}"
                )
                break

            # 3) الوقف لسه ابتدائي (ما تحقق 15%) وينزل لـ -30% -> بيع وقف خسارة (فاشلة)
            if not trail_active and unrealized_pl_pct <= -STOP_LOSS_PCT:
                close_position_market(occ_symbol, qty, "وقف خسارة")
                record_trade_pnl(total_pnl)
                record_daily_trade_result(symbol, False)
                date_txt, time_txt = now_ksa_text()
                send_telegram_message(
                    f"🔴 تم البيع وقف خسارة\n\n"
                    f"الرمز: {symbol}\n"
                    f"الفريم: {timeframe}\n"
                    f"العقد: {contract_label}\n"
                    f"الكمية: {qty}\n"
                    f"سعر الدخول: {avg_entry_price}\n"
                    f"سعر البيع: {current_price}\n"
                    f"نسبة الخسارة: {unrealized_pl_pct:.2%}\n"
                    f"💸 إجمالي الخسارة: ${total_pnl:+.2f}\n\n"
                    f"التاريخ: {date_txt}\n"
                    f"الساعة: {time_txt}"
                )
                break

        except Exception as e:
            print(f"[Monitor Exception] {e}")

    print(f"[Monitor] Done for {occ_symbol}")


def wait_for_filled_price(order_id):
    for attempt in range(10):
        time.sleep(2)
        try:
            r = requests.get(f"{ALPACA_BASE}/v2/orders/{order_id}", headers=HEADERS, timeout=10)
            if r.status_code == 200:
                data             = r.json()
                filled_avg_price = data.get('filled_avg_price')
                print(f"[Fill Check] Attempt={attempt+1} | Status={data.get('status')} | Filled={filled_avg_price}")
                if filled_avg_price:
                    return float(filled_avg_price), data.get('status')
        except Exception as e:
            print(f"[Fill Check Error] {e}")
    return None, None


def place_option_order(symbol, action, timeframe, signal_time=None):
    print(f"\n{'='*60}")
    print(f"[Signal] Symbol={symbol} | Action={action} | Timeframe={timeframe}")

    if symbol not in ALLOWED_SYMBOLS:
        return {'status': 'error', 'reason': 'الرمز غير مدعوم'}

    if action not in ['CALL', 'PUT']:
        return {'status': 'error', 'reason': 'الإشارة غير صحيحة'}

    with bot_paused_lock:
        if bot_paused:
            print(f"[Paused] Signal ignored: {symbol} {action}")
            return {'status': 'paused', 'reason': 'البوت متوقف مؤقتًا (/resume للاستئناف)'}

    # ==============================
    # التعديل: هدف يومي (SPY/QQQ) — تجاهل أي إشارة جديدة لو تحقق الهدف أو سقف الفشل اليوم
    # ==============================
    if is_daily_limit_reached(symbol):
        with daily_trade_lock:
            reason = daily_trade_state.get('stop_reason')
        print(f"[Daily Limit] Signal ignored for {symbol} (reason={reason})")
        return {
            'status': 'daily_limit_reached',
            'reason': 'تم تحقيق الهدف اليومي' if reason == 'هدف' else 'تم بلوغ سقف الصفقات الفاشلة اليوم'
        }

    if not should_accept_signal(signal_time):
        return {'status': 'ignored', 'reason': 'خارج وقت التداول 4:45 PM - 10:15 PM KSA'}

    # ملاحظة: تأكيد إغلاق الشمعة (فوق/تحت شمعة الإشارة) يتم الآن داخل
    # مؤشر Pine Script نفسه قبل إرسال التنبيه، لذلك ينفذ البايثون فورًا
    # بمجرد استقبال الإشارة دون انتظار إضافي هنا.

    open_positions = get_open_positions()
    existing       = open_positions.get(symbol)

    if existing:
        if existing['action'] != action:
            reversal_pnl = (existing.get('current_price', 0) - existing.get('avg_entry_price', 0)) * float(existing.get('qty', ORDER_QTY)) * 100
            close_position_market(existing['occ_symbol'], existing.get('qty', ORDER_QTY), "عكس الاتجاه")
            record_trade_pnl(reversal_pnl)
            record_daily_trade_result(symbol, reversal_pnl > 0)
            date_txt, time_txt = now_ksa_text()
            send_telegram_message(
                f"🔄 تم إغلاق صفقة بسبب إشارة عكسية\n\n"
                f"الرمز: {symbol}\n"
                f"الفريم: {timeframe}\n"
                f"العقد السابق: {format_contract_label(existing['occ_symbol'])}\n"
                f"الاتجاه السابق: {existing['action']}\n"
                f"الاتجاه الجديد: {action}\n\n"
                f"التاريخ: {date_txt}\n"
                f"الساعة: {time_txt}"
            )
            time.sleep(1)
        else:
            return {'status': 'skipped', 'reason': 'same direction already open'}

    price = get_latest_price(symbol)
    if price is None:
        return {'status': 'error', 'reason': f'Could not fetch price for {symbol}'}

    expiry         = get_expiry(symbol, signal_time)
    strike         = round(price)
    occ_symbol     = build_occ_symbol(symbol, expiry, action, strike)
    contract_label = format_contract_label(occ_symbol)

    # ==============================
    # التعديل: حجم الصفقة الديناميكي بناءً على 50% من رصيد الكاش المتاح
    # ==============================
    order_qty = calculate_order_qty(occ_symbol)

    print(f"[OCC] {occ_symbol} | Price={price} | Strike={strike} | Expiry={expiry} | Qty={order_qty}")

    order = {
        "symbol"       : occ_symbol,
        "qty"          : order_qty,
        "side"         : "buy",
        "type"         : "market",
        "time_in_force": "day"
    }

    r = requests.post(f"{ALPACA_BASE}/v2/orders", json=order, headers=HEADERS, timeout=10)
    try:
        result = r.json()
    except:
        result = {}

    print(f"[Buy] Status={r.status_code} | Result={result}")

    filled_price = None
    order_id     = result.get('id')

    if r.status_code in [200, 201] and order_id:
        filled_price, order_status = wait_for_filled_price(order_id)
        date_txt, time_txt = now_ksa_text()
        expiry_type = "الجمعة" if symbol in ["META", "AVGO", "MSTR"] else "ثاني يوم"

        send_telegram_message(
            f"✅ تم شراء عقد\n\n"
            f"الرمز: {symbol}\n"
            f"النوع: {action}\n"
            f"الفريم: {timeframe}\n"
            f"العقد: {contract_label}\n"
            f"انتهاء العقد: {expiry} ({expiry_type})\n"
            f"الكمية: {order_qty}\n"
            f"سعر السهم وقت الإشارة: {price}\n"
            f"سعر العقد: {filled_price if filled_price else 'لم يتوفر بعد'}\n"
            f"الهدف النهائي: {TAKE_PROFIT_PCT:.0%}\n"
            f"وقف الخسارة الابتدائي: {STOP_LOSS_PCT:.0%}\n"
            f"وقف متحرك: عند +15% يُرفع الوقف لـ +15% | الهدف الأقصى +50%\n\n"
            f"التاريخ: {date_txt}\n"
            f"الساعة: {time_txt}"
        )

        t = threading.Thread(target=monitor_tp_sl, args=(symbol, occ_symbol, filled_price or price, order_qty, timeframe))
        t.daemon = True
        t.start()

    return {
        'status'        : 'success' if r.status_code in [200, 201] else 'error',
        'symbol'        : symbol,
        'action'        : action,
        'timeframe'     : timeframe,
        'price'         : price,
        'strike'        : strike,
        'expiry'        : str(expiry),
        'occ_symbol'    : occ_symbol,
        'contract_label': contract_label,
        'qty'           : order_qty,
        'filled_price'  : filled_price,
        'tp_pct'        : f"{TAKE_PROFIT_PCT*100}%",
        'sl_pct'        : f"{STOP_LOSS_PCT*100}%",
        'alpaca_code'   : r.status_code,
        'result'        : result
    }


@app.route('/')
def home():
    return (
        f'Options Bot ✅ | '
        f'Target={TAKE_PROFIT_PCT*100:.0f}% | '
        f'InitialSL={STOP_LOSS_PCT*100:.0f}% | '
        f'TrailStop=+15%→lock+15% | MaxTarget=50% | '
        f'PositionSize={POSITION_SIZE_PCT:.0%} of cash | '
        f'DailyGoal(SPY/QQQ)={DAILY_WIN_TARGET} wins or {DAILY_LOSS_LIMIT} losses | '
        f'KSA: 16:45-22:15 | '
        f'SPY/QQQ→ثاني يوم | META/AVGO/MSTR→الجمعة | '
        f'Confirm=Candle-Close | '
        f'TelegramCmds=/status /balance /pnl /close /breakeven /pause /resume /ping | '
        f'Telegram={"ON" if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID else "OFF"}'
    )


@app.route('/status')
def status():
    positions = get_open_positions()
    now_ksa   = datetime.datetime.now(KSA_TZ).strftime('%Y-%m-%d %H:%M:%S')
    with daily_trade_lock:
        daily_trade_copy = dict(daily_trade_state)
    return jsonify({
        'active_positions' : positions,
        'count'            : len(positions),
        'ksa_time'         : now_ksa,
        'telegram'         : 'ON' if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID else 'OFF',
        'daily_goal_spy_qqq': daily_trade_copy,
        'settings': {
            'symbols'           : ALLOWED_SYMBOLS,
            'timeframe'         : TIMEFRAME,
            'position_size_pct' : f"{POSITION_SIZE_PCT:.0%} من الكاش المتاح لكل صفقة",
            'daily_goal'        : f"{DAILY_WIN_TARGET} صفقة ناجحة أو {DAILY_LOSS_LIMIT} صفقات فاشلة على SPY/QQQ فقط",
            'final_target'      : f"{TAKE_PROFIT_PCT*100:.0f}%",
            'initial_stop_loss' : f"{STOP_LOSS_PCT*100:.0f}%  (يشتغل قبل تحقيق +15%؛ بعدها الوقف يُرفع لـ +15%)",
            'trailing_stop'     : f"عند تحقيق +{int(TRAIL_TRIGGER_PCT*100)}% يُرفع الوقف ويثبت عند +{int(TRAIL_LOCK_PCT*100)}%",
            'max_target'        : f"{int(TAKE_PROFIT_PCT*100)}%",
            'allowed_window_ksa': '16:45 - 22:15',
            'expiry_spy_qqq'    : 'ثاني يوم عمل',
            'expiry_meta_avgo_mstr': 'الجمعة القادمة',
            'confirmation_mode' : 'التأكيد يتم داخل مؤشر TradingView (إغلاق فوق/تحت شمعة الإشارة) قبل إرسال التنبيه'
        }
    })


@app.route('/test_telegram')
def test_telegram():
    ok = send_telegram_message("✅ اختبار تيلجرام: البوت يعمل بنجاح")
    return jsonify({'telegram_sent': ok})


@app.route('/test_spy_call')
def test_spy_call():
    now = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    return jsonify(place_option_order('SPY', 'CALL', TIMEFRAME, now))


@app.route('/test_spy_put')
def test_spy_put():
    now = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    return jsonify(place_option_order('SPY', 'PUT', TIMEFRAME, now))


@app.route('/test_qqq_call')
def test_qqq_call():
    now = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    return jsonify(place_option_order('QQQ', 'CALL', TIMEFRAME, now))


@app.route('/test_qqq_put')
def test_qqq_put():
    now = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    return jsonify(place_option_order('QQQ', 'PUT', TIMEFRAME, now))


@app.route('/test_meta_call')
def test_meta_call():
    now = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    return jsonify(place_option_order('META', 'CALL', TIMEFRAME, now))


@app.route('/test_avgo_call')
def test_avgo_call():
    now = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    return jsonify(place_option_order('AVGO', 'CALL', TIMEFRAME, now))


@app.route('/test_mstr_call')
def test_mstr_call():
    now = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    return jsonify(place_option_order('MSTR', 'CALL', TIMEFRAME, now))


@app.route('/test_mstr_put')
def test_mstr_put():
    now = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    return jsonify(place_option_order('MSTR', 'PUT', TIMEFRAME, now))


# ══════════════════════════════════════════════════════════
# التعديل: أوامر تيلجرام التفاعلية
# /status /balance /pnl /close /breakeven /pause /resume /ping
# ══════════════════════════════════════════════════════════

def commands_list_text():
    return (
        "📋 الأوامر المتاحة:\n\n"
        "/status — الصفقات المفتوحة\n"
        "/balance — رصيد الحساب\n"
        "/pnl — الربح والخسارة (اليوم)\n"
        "/close — إغلاق كل الصفقات\n"
        "/breakeven — بيع براس المال\n"
        "/pause — إيقاف استقبال إشارات جديدة\n"
        "/resume — استئناف استقبال الإشارات\n"
        "/ping — حالة البوت"
    )


def handle_telegram_command(command_text):
    """يعالج أمر نصي وارد من تيلجرام ويرجّع نص الرد المناسب."""
    command = command_text.strip().lower().split('@')[0]  # يدعم أوامر مثل /status@BotName
    date_txt, time_txt = now_ksa_text()

    if command == '/status':
        positions = get_open_positions()
        if not positions:
            return f"📊 لا توجد صفقات مفتوحة حاليًا\n\nالتاريخ: {date_txt}\nالساعة: {time_txt}"
        lines = ["📊 الصفقات المفتوحة:\n"]
        for symbol, pos in positions.items():
            lines.append(
                f"• {pos['contract_label']}\n"
                f"  الدخول: {pos['avg_entry_price']} | الحالي: {pos['current_price']}\n"
                f"  الربح/الخسارة: {pos['unrealized_plpc']:.2%}\n"
            )
        return "\n".join(lines) + f"\nالتاريخ: {date_txt}\nالساعة: {time_txt}"

    elif command == '/balance':
        account = get_account_info()
        if not account:
            return "⚠️ تعذّر جلب بيانات الحساب من Alpaca"
        return (
            f"💰 رصيد الحساب\n\n"
            f"القيمة الإجمالية: ${float(account.get('portfolio_value', 0)):,.2f}\n"
            f"النقد المتاح: ${float(account.get('cash', 0)):,.2f}\n"
            f"القوة الشرائية: ${float(account.get('buying_power', 0)):,.2f}\n\n"
            f"التاريخ: {date_txt}\nالساعة: {time_txt}"
        )

    elif command == '/pnl':
        with daily_pnl_lock:
            today = get_ksa_date_str()
            if daily_pnl['date'] != today:
                total, wins, losses = 0.0, 0, 0
            else:
                total, wins, losses = daily_pnl['total'], daily_pnl['wins'], daily_pnl['losses']
        with daily_trade_lock:
            goal_today = get_ksa_date_str()
            if daily_trade_state['date'] != goal_today:
                goal_wins, goal_losses, goal_stopped = 0, 0, False
            else:
                goal_wins, goal_losses, goal_stopped = daily_trade_state['wins'], daily_trade_state['losses'], daily_trade_state['stopped']
        emoji = "💰" if total >= 0 else "💸"
        goal_line = f"\n\n🎯 هدف SPY/QQQ: {goal_wins}/{DAILY_WIN_TARGET} ناجحة، {goal_losses}/{DAILY_LOSS_LIMIT} فاشلة" + (" (متوقف)" if goal_stopped else "")
        return (
            f"{emoji} ربح/خسارة اليوم\n\n"
            f"الصافي: ${total:+.2f}\n"
            f"صفقات رابحة: {wins}\n"
            f"صفقات خاسرة: {losses}"
            f"{goal_line}\n\n"
            f"التاريخ: {date_txt}\nالساعة: {time_txt}"
        )

    elif command == '/close':
        closed = close_all_open_positions()
        if not closed:
            return f"📭 لا توجد صفقات مفتوحة لإغلاقها\n\nالتاريخ: {date_txt}\nالساعة: {time_txt}"
        lines = ["🔴 تم إغلاق كل الصفقات:\n"]
        for c in closed:
            lines.append(f"• {c['contract']} → ${c['pnl']:+.2f}")
        return "\n".join(lines) + f"\n\nالتاريخ: {date_txt}\nالساعة: {time_txt}"

    elif command == '/breakeven':
        closed, skipped = breakeven_all_open_positions()
        lines = []
        if closed:
            lines.append("🟢 تم البيع براس المال أو أعلى:")
            for c in closed:
                lines.append(f"• {c['contract']} → ${c['pnl']:+.2f}")
        if skipped:
            lines.append("\n⏭️ تم تجاهل (لسا تحت رأس المال):")
            for s in skipped:
                lines.append(f"• {s['contract']} ({s['pl_pct']:.2%})")
        if not closed and not skipped:
            return f"📭 لا توجد صفقات مفتوحة\n\nالتاريخ: {date_txt}\nالساعة: {time_txt}"
        return "\n".join(lines) + f"\n\nالتاريخ: {date_txt}\nالساعة: {time_txt}"

    elif command == '/pause':
        with bot_paused_lock:
            globals()['bot_paused'] = True
        return f"⏸️ تم إيقاف استقبال إشارات جديدة مؤقتًا\n(الصفقات المفتوحة حاليًا تبقى تحت المراقبة العادية)\n\nالتاريخ: {date_txt}\nالساعة: {time_txt}"

    elif command == '/resume':
        with bot_paused_lock:
            globals()['bot_paused'] = False
        return f"▶️ تم استئناف استقبال الإشارات\n\nالتاريخ: {date_txt}\nالساعة: {time_txt}"

    elif command == '/ping':
        with bot_paused_lock:
            state = "متوقف مؤقتًا ⏸️" if bot_paused else "شغال ✅"
        return f"🏓 البوت شغال\nالحالة: {state}\n\nالتاريخ: {date_txt}\nالساعة: {time_txt}"

    elif command in ('/start', '/help'):
        return commands_list_text()

    else:
        return "❓ أمر غير معروف\n\n" + commands_list_text()


@app.route('/telegram_webhook', methods=['POST'])
def telegram_webhook():
    try:
        update  = request.get_json(force=True, silent=True) or {}
        message = update.get('message') or update.get('edited_message') or {}
        chat_id = str(message.get('chat', {}).get('id', ''))
        text    = message.get('text', '')

        # أمان: تجاهل أي رسالة ما جاية من نفس المحادثة المصرّح بها فقط
        if not text.startswith('/') or not TELEGRAM_CHAT_ID or chat_id != str(TELEGRAM_CHAT_ID):
            return jsonify({'ok': True}), 200

        reply = handle_telegram_command(text)
        send_telegram_message(reply)
        return jsonify({'ok': True}), 200
    except Exception as e:
        print(f"[Telegram Webhook Error] {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/setup_telegram_commands')
def setup_telegram_commands():
    """يسجّل قائمة الأوامر عند تيلجرام (تظهر كمنيو تلقائي عند كتابة '/' في المحادثة). تُستدعى مرة واحدة فقط."""
    try:
        if not TELEGRAM_TOKEN:
            return jsonify({'ok': False, 'error': 'TELEGRAM_TOKEN missing'}), 400
        commands = [
            {'command': 'status',    'description': 'الصفقات المفتوحة'},
            {'command': 'balance',   'description': 'رصيد الحساب'},
            {'command': 'pnl',       'description': 'الربح والخسارة (اليوم)'},
            {'command': 'close',     'description': 'إغلاق كل الصفقات'},
            {'command': 'breakeven', 'description': 'بيع براس المال'},
            {'command': 'pause',     'description': 'إيقاف استقبال إشارات جديدة'},
            {'command': 'resume',    'description': 'استئناف استقبال الإشارات'},
            {'command': 'ping',      'description': 'حالة البوت'},
        ]
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setMyCommands",
            json={'commands': commands},
            timeout=10
        )
        return jsonify({'telegram_response': r.json()}), r.status_code
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/setup_telegram_webhook')
def setup_telegram_webhook():
    """يسجّل رابط /telegram_webhook عند تيلجرام تلقائيًا (تُستدعى مرة واحدة فقط بعد كل نشر جديد للسيرفر)."""
    try:
        if not TELEGRAM_TOKEN:
            return jsonify({'ok': False, 'error': 'TELEGRAM_TOKEN missing'}), 400
        webhook_url = request.url_root.rstrip('/') + '/telegram_webhook'
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook",
            params={'url': webhook_url},
            timeout=10
        )
        return jsonify({'telegram_response': r.json(), 'webhook_url': webhook_url}), r.status_code
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


def _process_signal_async(symbol, action, timeframe, signal_time):
    """تنفيذ الصفقة في الخلفية بعد الرد الفوري على TradingView"""
    try:
        result = place_option_order(symbol, action, timeframe, signal_time)
        print(f"[Async Result] {symbol} {action} -> {result.get('status')}")
    except Exception as e:
        print(f"[Async Error] {symbol} {action}: {e}")


@app.route('/webhook', methods=['POST'])
def webhook():
    """يرد على TradingView فورًا (أقل من ثانية) وينفّذ الصفقة في الخلفية.
    يمنع رسائل 'timed out' وإعادة الإرسال المكرر."""
    try:
        data        = request.get_json(force=True, silent=True) or {}
        print(f"[Webhook] Received: {data}")
        action      = data.get('action', '').upper()
        symbol      = data.get('symbol', 'SPY').upper()
        timeframe   = data.get('timeframe', TIMEFRAME)
        signal_time = data.get('time', None)

        if action not in ['CALL', 'PUT']:
            return jsonify({'status': 'error', 'reason': 'الإشارة غير صحيحة'}), 400

        # حماية من التكرار: إعادة إرسال TradingView لنفس الإشارة تُتجاهل فورًا
        if is_duplicate_signal(symbol, action):
            print(f"[Dedup] Duplicate {action} {symbol} ignored")
            return jsonify({'status': 'ignored', 'reason': 'إشارة مكررة (إعادة إرسال)'}), 200

        # رد فوري + معالجة بالخلفية
        t = threading.Thread(target=_process_signal_async, args=(symbol, action, timeframe, signal_time))
        t.daemon = True
        t.start()
        return jsonify({'status': 'accepted', 'message': f'{action} {symbol} received — processing'}), 200
    except Exception as e:
        print(f"[Webhook Error] {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
