import json
import os
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
import firebase_admin
from firebase_admin import credentials, firestore

# Konfiguratsiya
API_URL = "https://api.telegram.org/bot{token}/{method}"

def get_env_settings():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    hr_chat_id = os.environ.get("HR_CHAT_ID")
    firebase_creds_json = os.environ.get("FIREBASE_CREDENTIALS")
    firebase_creds_file = os.environ.get("FIREBASE_CREDENTIALS_FILE")
    if not firebase_creds_json and firebase_creds_file:
        try:
            with open(firebase_creds_file, "r", encoding="utf-8") as f:
                firebase_creds_json = f.read()
        except Exception as e:
            print(f"XATO: FIREBASE_CREDENTIALS_FILE o'qilmadi: {e}")
            sys.exit(1)
    
    if not token:
        print("XATO: TELEGRAM_BOT_TOKEN topilmadi")
        sys.exit(1)
    if not hr_chat_id:
        print("XATO: HR_CHAT_ID topilmadi")
        sys.exit(1)
    if not firebase_creds_json:
        print("XATO: FIREBASE_CREDENTIALS yoki FIREBASE_CREDENTIALS_FILE topilmadi")
        sys.exit(1)
        
    return token, hr_chat_id, firebase_creds_json

TOKEN, HR_CHAT_ID, FIREBASE_CREDS = get_env_settings()

# Firebase initialization
# User provided Web Config (for reference):
# apiKey: "AIzaSyDMYfYXtXL2ENNTbrx1wu_Xkpb6rS1SwGo"
# authDomain: "alxorazmiyishbot.firebaseapp.com"
# projectId: "alxorazmiyishbot"
# storageBucket: "alxorazmiyishbot.firebasestorage.app"
# messagingSenderId: "131888596228"
# appId: "1:131888596228:web:65dc085d428b6afec43c51"

try:
    creds_dict = json.loads(FIREBASE_CREDS)
    cred = credentials.Certificate(creds_dict)
    firebase_admin.initialize_app(cred, {
        'projectId': 'alxorazmiyishbot',
        'storageBucket': 'alxorazmiyishbot.firebasestorage.app'
    })
    db = firestore.client()
    print("Firebase muvaffaqiyatli bog'landi: alxorazmiyishbot")
except Exception as e:
    print(f"Firebase initialization error: {e}")
    # Render'da xato bermasligi uchun sys.exit(1) ni olib tashladik, 
    # lekin db ishlamasligi mumkin
    db = None

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

        request_timeout = 60 if method == "getUpdates" else 10
        with urllib.request.urlopen(req, timeout=request_timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        print(f"API xatolik ({method}): HTTP {e.code} {e.reason}")
        return {"ok": False, "error_code": e.code, "description": body}
    except Exception as e:
        print(f"API xatolik ({method}): {e}")
        return {"ok": False, "description": str(e)}

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
        self.lang = {}
        self.positions = [
            ["Matematika o'qituvchisi", "Ingliz tili o'qituvchisi"],
            ["Ona tili va adabiyot", "Fizika o'qituvchisi"],
            ["Boshlang'ich sinf", "Administrator"],
            ["Boshqa lavozim"]
        ]
        self.labels = {
            "menu_about": {"uz": "🏫 Biz haqimizda", "en": "🏫 About us", "ru": "🏫 О нас"},
            "menu_contact": {"uz": "💬 Biz bilan bog'lanish", "en": "💬 Contact us", "ru": "💬 Связаться"},
            "menu_jobs": {"uz": "💼 Bo'sh ish o'rinlari", "en": "💼 Job vacancies", "ru": "💼 Вакансии"},
            "menu_lang": {"uz": "🌐 Tilni almashtirish", "en": "🌐 Change language", "ru": "🌐 Сменить язык"},
            "back": {"uz": "⬅️ Orqaga", "en": "⬅️ Back", "ru": "⬅️ Назад"},
            "cancel": {"uz": "❌ Bekor qilish", "en": "❌ Cancel", "ru": "❌ Отмена"},
            "skip": {"uz": "O'tkazib yuborish", "en": "Skip", "ru": "Пропустить"},
            "send_contact": {"uz": "Kontaktni yuborish", "en": "Send contact", "ru": "Отправить контакт"},
            "lang_uz": {"uz": "🇺🇿 UZ", "en": "🇺🇿 UZ", "ru": "🇺🇿 UZ"},
            "lang_en": {"uz": "🇬🇧 ENG", "en": "🇬🇧 ENG", "ru": "🇬🇧 ENG"},
            "lang_ru": {"uz": "🇷🇺 RUS", "en": "🇷🇺 RUS", "ru": "🇷🇺 RUS"},
        }

    def _lang(self, user_id):
        return self.lang.get(user_id, "uz")

    def _label(self, key, lang):
        return self.labels.get(key, {}).get(lang) or self.labels.get(key, {}).get("uz") or key

    def _main_menu(self, lang):
        return {
            "keyboard": [
                [{"text": self._label("menu_about", lang)}, {"text": self._label("menu_contact", lang)}],
                [{"text": self._label("menu_jobs", lang)}],
                [{"text": self._label("menu_lang", lang)}],
            ],
            "resize_keyboard": True
        }

    def _lang_menu(self, lang):
        return {
            "keyboard": [
                [{"text": self._label("lang_uz", lang)}, {"text": self._label("lang_en", lang)}, {"text": self._label("lang_ru", lang)}],
                [{"text": self._label("back", lang)}],
            ],
            "resize_keyboard": True
        }

    def _action_from_text(self, text):
        for action_key in ["menu_about", "menu_contact", "menu_jobs", "menu_lang", "back", "cancel", "skip", "send_contact", "lang_uz", "lang_en", "lang_ru"]:
            labels = self.labels.get(action_key, {})
            if text in labels.values():
                return action_key
        return None

    def handle_update(self, update):
        message = update.get("message")
        if not message: return
        
        chat_id = message["chat"]["id"]
        user_id = message["from"]["id"]
        text = message.get("text", "")
        contact = message.get("contact")
        lang = self._lang(user_id)

        if text in ["/start", "/menu"] or (isinstance(text, str) and text.startswith("/menu")) or text == "Menu":
            if user_id not in self.lang:
                self.lang[user_id] = "uz"
                lang = "uz"
            self.states[user_id] = None
            send_msg(chat_id, "Menu yangilanmoqda...", {"remove_keyboard": True})
            send_msg(chat_id, "<b>Assalomu alaykum!</b>\n\nKerakli bo'limni tanlang:", self._main_menu(lang))
            return
        
        action = self._action_from_text(text)

        if action == "menu_lang":
            send_msg(chat_id, "Tilni tanlang:", self._lang_menu(lang))
            return

        if action in ["lang_uz", "lang_en", "lang_ru"]:
            new_lang = "uz" if action == "lang_uz" else ("en" if action == "lang_en" else "ru")
            self.lang[user_id] = new_lang
            lang = new_lang
            send_msg(chat_id, "✅ Til o'zgartirildi.", self._main_menu(lang))
            return

        if action == "back":
            send_msg(chat_id, "Menu:", self._main_menu(lang))
            return

        # Asosiy Menyu tugmalarini qayta ishlash
        if not self.states.get(user_id):
            if action == "menu_about":
                msg = (
                    "<b>Biz haqimizda</b>\n\n"
                    "Al-Xorazmiy xususiy maktabi zamonaviy ta'lim va tarbiya uyg'unligini ta'minlaydi."
                )
                send_msg(chat_id, msg, self._main_menu(lang))
                return

            if action == "menu_contact":
                msg = (
                    "<b>Biz bilan bog'lanish</b>\n\n"
                    "Telefon: +998 90 XXX XX XX\n"
                    "Telegram: @u_gafforov\n"
                    "Email: info@alxorazmiy.uz"
                )
                send_msg(chat_id, msg, self._main_menu(lang))
                return

            if action == "menu_jobs":
                self.states[user_id] = {"step": "name", "data": {}, "mode": "job"}
                send_msg(chat_id, "<b>Bo'sh ish o'rinlari</b>\n\nIltimos, ism va familiyangizni kiriting:", {"remove_keyboard": True})
                return

        # Ariza topshirish flow'i (states mavjud bo'lsa)
        state = self.states.get(user_id)
        if not state:
            send_msg(chat_id, "Iltimos, pastdagi menyudan birini tanlang.", self._main_menu(lang))
            return

        if action == "cancel":
            self.states[user_id] = None
            send_msg(chat_id, "Ariza topshirish bekor qilindi.", self._main_menu(lang))
            return

        step = state["step"]
        
        if step == "name":
            if is_valid_name(text):
                state["data"]["name"] = text
                state["step"] = "phone"
                markup = {
                    "keyboard": [
                        [{"text": self._label("send_contact", lang), "request_contact": True}],
                        [{"text": self._label("cancel", lang)}]
                    ],
                    "resize_keyboard": True,
                    "one_time_keyboard": True
                }
                send_msg(chat_id, "Telefon raqamingizni yuboring (tugmani bosing):", markup)
            else:
                send_msg(chat_id, f"Iltimos, ism va familiyangizni to'liq yozing (Masalan: Ali Valiyev):\n\nBekor qilish uchun '{self._label('cancel', lang)}' tugmasini bosing.")
        
        elif step == "phone":
            phone_val = None
            if contact:
                phone_val = contact.get("phone_number")
            elif is_valid_phone(text):
                phone_val = text

            if phone_val:
                state["data"]["phone"] = phone_val
                state["step"] = "position"
                kb = [[{"text": p} for p in row] for row in self.positions]
                kb.append([{"text": self._label("cancel", lang)}])
                markup = {
                    "keyboard": kb,
                    "resize_keyboard": True
                }
                send_msg(chat_id, "Qaysi lavozimga topshirmoqchisiz? (Ro'yxatdan tanlang yoki yozing):", markup)
            else:
                send_msg(chat_id, "Iltimos, telefon raqamingizni tugma orqali yuboring yoki yozing:")

        elif step == "position":
            if len(text) > 2:
                state["data"]["position"] = text
                state["step"] = "exp"
                markup = {
                    "keyboard": [[{"text": self._label("cancel", lang)}]],
                    "resize_keyboard": True
                }
                send_msg(chat_id, "Ish tajribangiz haqida qisqacha ma'lumot bering:", markup)
            else:
                send_msg(chat_id, "Lavozim nomini kiriting:")

        elif step == "exp":
            if len(text) > 5:
                state["data"]["exp"] = text
                state["step"] = "cv"
                markup = {
                    "keyboard": [
                        [{"text": self._label("skip", lang)}],
                        [{"text": self._label("cancel", lang)}]
                    ],
                    "resize_keyboard": True,
                    "one_time_keyboard": True
                }
                send_msg(chat_id, "Rezyume (PDF yoki Rasm) yuboring yoki 'O'tkazib yuborish' tugmasini bosing:", markup)
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
            elif action == "skip" or text == "/skip":
                pass
            else:
                send_msg(chat_id, "Iltimos, fayl yuboring yoki tugmani bosing.")
                return

            # Firebase va HR ga yuborish
            self.finish_and_send(user_id, state["data"], cv_file_id, cv_type)
            send_msg(chat_id, "✅ <b>Rahmat!</b> Arizangiz HR bo'limiga yuborildi.", self._main_menu(lang))
            del self.states[user_id]

    def finish_and_send(self, user_id, data, file_id, f_type):
        # 1. Firestore'ga saqlash
        saved_to_firebase = False
        try:
            if db:
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
                saved_to_firebase = True
        except Exception as e:
            print(f"Firestore save error: {e}")

        # 2. HR ga Telegram orqali yuborish
        status_msg = "(Firebase'ga saqlandi)" if saved_to_firebase else "(Firebase'ga saqlanmadi!)"
        report = (
            f"<b>Yangi Ariza! {status_msg}</b>\n\n"
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

def run_polling():
    offset = 0
    print("Bot ishga tushdi. Yangilanishlar kutilmoqda (polling)...")
    api_call("deleteWebhook", {"drop_pending_updates": True})
    while True:
        result = api_call("getUpdates", {"timeout": 30, "offset": offset})
        if not result.get("ok"):
            if result.get("error_code") == 409:
                api_call("deleteWebhook", {"drop_pending_updates": True})
                time.sleep(5)
                continue
            time.sleep(2)
            continue

        updates = result.get("result") or []
        for upd in updates:
            try:
                update_id = upd.get("update_id")
                if isinstance(update_id, int):
                    offset = update_id + 1
                bot_logic.handle_update(upd)
            except Exception as e:
                print(f"Update error: {e}")
        time.sleep(0.2)

if __name__ == "__main__":
    run_polling()
