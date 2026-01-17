import json
import os
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from flask import Flask, request, jsonify

# Flask app
app = Flask(__name__)

API_URL = "https://api.telegram.org/bot{token}/{method}"

def get_env_settings():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    hr_chat_id = os.environ.get("HR_CHAT_ID")
    # Render yoki boshqa joyda webhook URL ham kerak bo'ladi
    webhook_url = os.environ.get("WEBHOOK_URL") 
    
    if not token:
        print("TELEGRAM_BOT_TOKEN muhit o'zgaruvchisi sozlanmagan")
        sys.exit(1)
    if not hr_chat_id:
        print("HR_CHAT_ID muhit o'zgaruvchisi sozlanmagan")
        sys.exit(1)
        
    try:
        hr_chat_id_int = int(hr_chat_id)
    except ValueError:
        print("HR_CHAT_ID butun son bo'lishi kerak")
        sys.exit(1)
        
    return token, hr_chat_id_int, webhook_url

# ... (qolgan yordamchi funksiyalar va is_valid_* funksiyalari o'zgarishsiz qoladi, 
# faqat get_updates kerak bo'lmaydi chunki webhook ishlatamiz) ...

# Global o'zgaruvchilar
TOKEN, HR_CHAT_ID, WEBHOOK_URL = get_env_settings()
conv_manager = None

# ... (api_call, send_message va boshqa funksiyalar) ...



def api_call(token, method, params):
    url = API_URL.format(token=token, method=method)
    data = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(url, data=data)
    try:
        with urllib.request.urlopen(req) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        try:
            error_body = exc.read().decode("utf-8")
        except Exception:
            error_body = ""
        raise RuntimeError(f"HTTP {exc.code} {method}: {error_body}") from exc
    return json.loads(body)


def verify_configuration(token, hr_chat_id):
    me = api_call(token, "getMe", {})
    if not me.get("ok"):
        print("Bot token noto'g'ri yoki bot bloklangan.")
        sys.exit(1)
    chat_info = api_call(token, "getChat", {"chat_id": hr_chat_id})
    if not chat_info.get("ok"):
        print("HR_CHAT_ID noto'g'ri yoki bot ushbu chatga qo'shilmagan.")
        sys.exit(1)


def get_updates(token, offset=None, timeout=50):
    params = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset
    result = api_call(token, "getUpdates", params)
    if not result.get("ok"):
        return []
    return result.get("result", [])


def send_message(token, chat_id, text, reply_markup=None):
    params = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup is not None:
        params["reply_markup"] = json.dumps(reply_markup)
    api_call(token, "sendMessage", params)


def send_document(token, chat_id, document_file_id, caption=None):
    params = {"chat_id": chat_id, "document": document_file_id}
    if caption:
        params["caption"] = caption
    api_call(token, "sendDocument", params)


def send_photo(token, chat_id, photo_file_id, caption=None):
    params = {"chat_id": chat_id, "photo": photo_file_id}
    if caption:
        params["caption"] = caption
    api_call(token, "sendPhoto", params)


def is_digits_only(text):
    stripped = text.strip()
    if not stripped:
        return False
    for ch in stripped:
        if ch < "0" or ch > "9":
            return False
    return True


def is_valid_full_name(text):
    if not text:
        return False
    value = text.strip()
    if len(value) < 5:
        return False
    if is_digits_only(value):
        return False
    if " " not in value:
        return False
    return True


def is_valid_phone(text):
    if not text:
        return False
    value = text.strip()
    digits_count = 0
    for ch in value:
        if ch >= "0" and ch <= "9":
            digits_count += 1
        elif ch in "+ ()-":
            continue
        else:
            return False
    if digits_count < 7:
        return False
    return True


def is_valid_short_text(text):
    if not text:
        return False
    value = text.strip()
    if len(value) < 3:
        return False
    if is_digits_only(value):
        return False
    return True


def is_valid_experience_text(text):
    if not text:
        return False
    value = text.strip()
    if len(value) < 5:
        return False
    return True


class ConversationManager:
    def __init__(self):
        self.user_states = {}

    def start_conversation(self, token, chat_id, user_id):
        self.user_states[user_id] = {"step": "ask_name", "data": {}}
        send_message(
            token,
            chat_id,
            "Assalomu alaykum! Xususiy maktabimizdagi bo'sh ish o'rinlari uchun ariza topshirish botiga xush kelibsiz.\n\nIsm va familiyangizni kiriting.",
        )

    def handle_message(self, token, hr_chat_id, message):
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        from_user = message.get("from") or {}
        user_id = from_user.get("id")
        if chat_id is None or user_id is None:
            return

        text = message.get("text")
        if text == "/start":
            self.start_conversation(token, chat_id, user_id)
            return

        state = self.user_states.get(user_id)
        if state is None:
            self.start_conversation(token, chat_id, user_id)
            return

        step = state.get("step")
        data = state.get("data", {})

        if step == "ask_name":
            if not text or not is_valid_full_name(text):
                send_message(
                    token,
                    chat_id,
                    "Iltimos, ism va familiyangizni to'liq ko'rinishida yuboring.\nMasalan: Ali Valiyev",
                )
                return
            data["full_name"] = text.strip()
            state["step"] = "ask_phone"
            state["data"] = data
            send_message(token, chat_id, "Telefon raqamingizni kiriting. Masalan: +99890 1234567")
            return

        if step == "ask_phone":
            if not text or not is_valid_phone(text):
                send_message(
                    token,
                    chat_id,
                    "Iltimos, telefon raqamingizni to'g'ri formatda yuboring.\nMasalan: +99890 1234567",
                )
                return
            data["phone"] = text.strip()
            state["step"] = "ask_position"
            state["data"] = data
            send_message(
                token,
                chat_id,
                "Qaysi lavozimga ariza bermoqchisiz?\nMasalan: Matematika o'qituvchisi, Ingliz tili o'qituvchisi, Administrator va hokazo.",
            )
            return

        if step == "ask_position":
            if not text or not is_valid_short_text(text):
                send_message(
                    token,
                    chat_id,
                    "Iltimos, lavozim nomini aniqroq qilib yozing.\nMasalan: Matematika o'qituvchisi",
                )
                return
            data["position"] = text.strip()
            state["step"] = "ask_experience"
            state["data"] = data
            send_message(
                token,
                chat_id,
                "Ish tajribangiz haqida qisqacha yozing. Masalan: 3 yil maktab o'qituvchisi sifatida.",
            )
            return

        if step == "ask_experience":
            if not text or not is_valid_experience_text(text):
                send_message(
                    token,
                    chat_id,
                    "Iltimos, ish tajribangizni biroz batafsilroq yozing.\nMasalan: 3 yil maktab o'qituvchisi sifatida.",
                )
                return
            data["experience"] = text.strip()
            state["step"] = "ask_cv"
            state["data"] = data
            send_message(
                token,
                chat_id,
                "Rahmat. Endi rezyumeingizni (PDF fayl yoki rasm) yuboring.\nAgar hozircha rezyume yo'q bo'lsa, /skip deb yozing.",
            )
            return

        if step == "ask_cv":
            document = message.get("document")
            photos = message.get("photo")
            cv_info = None

            if document:
                cv_info = {"type": "document", "file_id": document.get("file_id")}
            elif photos:
                largest_photo = photos[-1]
                cv_info = {"type": "photo", "file_id": largest_photo.get("file_id")}
            elif text and text.strip().lower() == "/skip":
                cv_info = None

            if cv_info is None and not (text and text.strip().lower() == "/skip"):
                send_message(
                    token,
                    chat_id,
                    "Iltimos, rezyumeni fayl yoki rasm sifatida yuboring yoki /skip deb yozing.",
                )
                return

            data["cv"] = cv_info
            state["step"] = "completed"
            state["data"] = data

            self.send_application_to_hr(token, hr_chat_id, chat_id, data)
            self.user_states.pop(user_id, None)

            send_message(
                token,
                chat_id,
                "Rahmat! Arizangiz HR bo'limiga yuborildi. Tez orada siz bilan bog'lanamiz.",
            )
            return

        send_message(
            token,
            chat_id,
            "Yangi ariza boshlash uchun /start buyrug'ini yuboring.",
        )
        self.user_states.pop(user_id, None)

    def send_application_to_hr(self, token, hr_chat_id, applicant_chat_id, data):
        phone_raw = data.get("phone", "")
        phone_digits = []
        for ch in phone_raw:
            if (ch >= "0" and ch <= "9") or ch == "+":
                phone_digits.append(ch)
        phone_link = "".join(phone_digits) if phone_digits else phone_raw

        lines = [
            "Yangi ishga qabul arizasi:",
            "",
            f"Ism familiya: {data.get('full_name', '')}",
            f"Telefon: <a href=\"tel:{phone_link}\">{data.get('phone', '')}</a>",
            f"Lavozim: {data.get('position', '')}",
            f"Ish tajribasi: {data.get('experience', '')}",
            f"Telegram chat ID: {applicant_chat_id}",
        ]
        text = "\n".join(lines)
        send_message(token, hr_chat_id, text)

        cv_info = data.get("cv")
        if not cv_info:
            return
        file_id = cv_info.get("file_id")
        if not file_id:
            return
        caption = "Nomzodning rezyumesi."
        if cv_info.get("type") == "document":
            send_document(token, hr_chat_id, file_id, caption=caption)
        elif cv_info.get("type") == "photo":
            send_photo(token, hr_chat_id, file_id, caption=caption)


def set_webhook(token, webhook_url):
    params = {"url": webhook_url}
    result = api_call(token, "setWebhook", params)
    if result.get("ok"):
        print(f"Webhook o'rnatildi: {webhook_url}")
    else:
        print(f"Webhook o'rnatishda xato: {result}")

@app.route("/", methods=["GET"])
def index():
    return "Bot ishlamoqda!"

@app.route("/webhook", methods=["POST"])
def webhook():
    if request.headers.get("content-type") == "application/json":
        update = request.get_json()
        if update:
            message = update.get("message") or update.get("edited_message")
            if message:
                conv_manager.handle_message(TOKEN, HR_CHAT_ID, message)
        return "OK"
    return "OK"

if __name__ == "__main__":
    # Avval ConversationManager ni yaratamiz
    conv_manager = ConversationManager()
    
    # Agar WEBHOOK_URL berilgan bo'lsa, webhookni sozlaymiz
    if WEBHOOK_URL:
        try:
            # Biroz kutib turamiz, server to'liq ishga tushishi uchun
            time.sleep(1)
            set_webhook(TOKEN, f"{WEBHOOK_URL}/webhook")
        except Exception as e:
            print(f"Webhook o'rnatishda xatolik: {e}")
    
    # Flask serverni ishga tushiramiz
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)


