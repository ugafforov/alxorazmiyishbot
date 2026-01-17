import json
import os
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from flask import Flask, request, jsonify
import firebase_admin
from firebase_admin import credentials, firestore

# Flask app
app = Flask(__name__)

# Konfiguratsiya
API_URL = "https://api.telegram.org/bot{token}/{method}"

def get_env_settings():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    hr_chat_id = os.environ.get("HR_CHAT_ID")
    webhook_url = os.environ.get("WEBHOOK_URL")
    firebase_creds_json = os.environ.get("FIREBASE_CREDENTIALS")
    
    if not token:
        print("XATO: TELEGRAM_BOT_TOKEN topilmadi")
        sys.exit(1)
    if not hr_chat_id:
        print("XATO: HR_CHAT_ID topilmadi")
        sys.exit(1)
    if not firebase_creds_json:
        print("XATO: FIREBASE_CREDENTIALS topilmadi")
        sys.exit(1)
        
    return token, hr_chat_id, webhook_url, firebase_creds_json

TOKEN, HR_CHAT_ID, WEBHOOK_URL, FIREBASE_CREDS = get_env_settings()

# Firebase initialization
try:
    creds_dict = json.loads(FIREBASE_CREDS)
    cred = credentials.Certificate(creds_dict)
    firebase_admin.initialize_app(cred)
    db = firestore.client()
except Exception as e:
    print(f"Firebase initialization error: {e}")
    sys.exit(1)

# --- Yordamchi Funksiyalar ---

def api_call(method, params=None):
    """Telegram API ga so'rov yuborish"""
    url = API_URL.format(token=TOKEN, method=method)
    try:
        if params:
            data = urllib.parse.urlencode(params).encode("utf-8")
            req = urllib.request.Request(url, data=data)
        else:
            req = urllib.request.Request(url)
            
        with urllib.request.urlopen(req, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as e:
        print(f"API xatolik ({method}): {e}")
        return {"ok": False}

def send_msg(chat_id, text, reply_markup=None):
    params = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        params["reply_markup"] = json.dumps(reply_markup)
    return api_call("sendMessage", params)

# --- Validatsiya ---

def is_valid_name(text):
    if not text: return False
    parts = text.strip().split()
    return len(parts) >= 2 and len(text) >= 5

def is_valid_phone(text):
    if not text: return False
    digits = "".join(filter(str.isdigit, text))
    return len(digits) >= 7

# --- Suhbat Menejeri ---

class BotLogic:
    def __init__(self):
        self.states = {} # user_id -> {step, data}

    def handle_update(self, update):
        message = update.get("message")
        if not message: return
        
        chat_id = message["chat"]["id"]
        user_id = message["from"]["id"]
        text = message.get("text", "")

        # Komandalar
        if text == "/start":
            self.states[user_id] = {"step": "name", "data": {}}
            send_msg(chat_id, "<b>Assalomu alaykum!</b>\n\nIshga qabul botiga xush kelibsiz. Iltimos, ism va familiyangizni kiriting:")
            return

        state = self.states.get(user_id)
        if not state:
            send_msg(chat_id, "Iltimos, ariza boshlash uchun /start bosing.")
            return

        step = state["step"]
        
        if step == "name":
            if is_valid_name(text):
                state["data"]["name"] = text
                state["step"] = "phone"
                send_msg(chat_id, "Telefon raqamingizni yuboring:")
            else:
                send_msg(chat_id, "Iltimos, ism va familiyangizni to'liq yozing (Masalan: Ali Valiyev):")
        
        elif step == "phone":
            if is_valid_phone(text):
                state["data"]["phone"] = text
                state["step"] = "position"
                send_msg(chat_id, "Qaysi lavozimga topshirmoqchisiz?")
            else:
                send_msg(chat_id, "Telefon raqami xato. Iltimos qaytadan yuboring:")

        elif step == "position":
            if len(text) > 2:
                state["data"]["position"] = text
                state["step"] = "exp"
                send_msg(chat_id, "Ish tajribangiz haqida qisqacha ma'lumot bering:")
            else:
                send_msg(chat_id, "Lavozim nomini kiriting:")

        elif step == "exp":
            if len(text) > 5:
                state["data"]["exp"] = text
                state["step"] = "cv"
                send_msg(chat_id, "Rezyume (PDF yoki Rasm) yuboring yoki /skip bosing:")
            else:
                send_msg(chat_id, "Tajribangiz haqida batafsilroq yozing:")

        elif step == "cv":
            cv_file_id = None
            cv_type = None
            
            if message.get("document"):
                cv_file_id = message["document"]["file_id"]
                cv_type = "doc"
            elif message.get("photo"):
                cv_file_id = message["photo"][-1]["file_id"]
                cv_type = "photo"
            elif text == "/skip":
                pass
            else:
                send_msg(chat_id, "Iltimos, fayl yuboring yoki /skip bosing.")
                return

            # Firebase va HR ga yuborish
            self.finish_and_send(user_id, state["data"], cv_file_id, cv_type)
            send_msg(chat_id, "✅ <b>Rahmat!</b> Arizangiz HR bo'limiga yuborildi.")
            del self.states[user_id]

    def finish_and_send(self, user_id, data, file_id, f_type):
        # 1. Firestore'ga saqlash
        try:
            doc_ref = db.collection("applications").document()
            doc_ref.set({
                "user_id": user_id,
                "name": data["name"],
                "phone": data["phone"],
                "position": data["position"],
                "experience": data["exp"],
                "cv_file_id": file_id,
                "cv_type": f_type,
                "timestamp": firestore.SERVER_TIMESTAMP
            })
        except Exception as e:
            print(f"Firestore save error: {e}")

        # 2. HR ga Telegram orqali yuborish
        report = (
            f"<b>Yangi Ariza! (Firebase'ga saqlandi)</b>\n\n"
            f"👤 Nomzod: {data['name']}\n"
            f"📞 Tel: {data['phone']}\n"
            f"💼 Lavozim: {data['position']}\n"
            f"📝 Tajriba: {data['exp']}\n"
            f"🆔 ID: {user_id}"
        )
        send_msg(HR_CHAT_ID, report)
        
        if file_id:
            method = "sendDocument" if f_type == "doc" else "sendPhoto"
            param_key = "document" if f_type == "doc" else "photo"
            api_call(method, {"chat_id": HR_CHAT_ID, param_key: file_id, "caption": f"{data['name']} - Rezyume"})

bot_logic = BotLogic()

# --- Flask Yo'llari ---

@app.route("/", methods=["GET"])
def home():
    return "Bot with Firebase is active", 200

@app.route("/webhook", methods=["POST"])
def webhook():
    update = request.get_json()
    if update:
        try:
            bot_logic.handle_update(update)
        except Exception as e:
            print(f"Update error: {e}")
    return "OK", 200

if __name__ == "__main__":
    if WEBHOOK_URL:
        api_call("setWebhook", {"url": f"{WEBHOOK_URL}/webhook"})
    
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
