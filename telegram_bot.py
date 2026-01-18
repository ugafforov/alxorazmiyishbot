import json
import os
import sys
import time
import logging
import requests
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import firebase_admin
from firebase_admin import credentials, firestore
try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    load_dotenv = None

# Logging sozlamalari
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("TelegramBot")

# .env faylidan yuklash
if load_dotenv:
    load_dotenv(override=True)

class Config:
    TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
    HR_CHAT_ID = os.environ.get("HR_CHAT_ID")
    FIREBASE_CREDS_JSON = os.environ.get("FIREBASE_CREDENTIALS")
    FIREBASE_CREDS_FILE = os.environ.get("FIREBASE_CREDENTIALS_FILE") or "alxorazmiyishbot-firebase-adminsdk-fbsvc-b24fba48ab.json"

    @classmethod
    def validate(cls):
        if not cls.TOKEN:
            logger.error("TELEGRAM_BOT_TOKEN topilmadi")
            return False
        if not cls.HR_CHAT_ID:
            logger.error("HR_CHAT_ID topilmadi")
            return False
        return True

class TelegramAPI:
    def __init__(self, token):
        self.base_url = f"https://api.telegram.org/bot{token}/"
        self.session = requests.Session()

    def call(self, method, params=None, files=None, timeout=10):
        url = self.base_url + method
        try:
            if method == "getUpdates":
                timeout = params.get("timeout", 30) + 5 if params else 35
            
            response = self.session.post(url, data=params, files=files, timeout=timeout)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.HTTPError as e:
            logger.error(f"API HTTP xatolik ({method}): {e}")
            try:
                return response.json()
            except:
                return {"ok": False, "description": str(e)}
        except Exception as e:
            logger.error(f"API kutilmagan xatolik ({method}): {e}")
            return {"ok": False, "description": str(e)}

    def send_message(self, chat_id, text, reply_markup=None):
        params = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML"
        }
        if reply_markup:
            params["reply_markup"] = json.dumps(reply_markup)
        return self.call("sendMessage", params)

class FirestoreDB:
    def __init__(self):
        self.db = None
        self._user_states = {}
        self._user_langs = {}
        self._lock = threading.Lock()
        self.initialize()

    def initialize(self):
        try:
            if not firebase_admin._apps:
                creds_json = Config.FIREBASE_CREDS_JSON
                if not creds_json and os.path.exists(Config.FIREBASE_CREDS_FILE):
                    with open(Config.FIREBASE_CREDS_FILE, "r") as f:
                        creds_json = f.read()
                
                if creds_json:
                    creds_dict = json.loads(creds_json)
                    cred = credentials.Certificate(creds_dict)
                    firebase_admin.initialize_app(cred, {
                        'projectId': 'alxorazmiyishbot',
                        'storageBucket': 'alxorazmiyishbot.firebasestorage.app'
                    })
                    self.db = firestore.client()
                    logger.info("Firebase muvaffaqiyatli bog'landi")
                else:
                    logger.warning("Firebase credentials topilmadi, bot cheklangan rejimda ishlaydi")
        except Exception as e:
            logger.error(f"Firebase initialization error: {e}")

    def save_application(self, user_id, data, file_id, f_type):
        if not self.db: return False
        try:
            doc_ref = self.db.collection("applications").document()
            doc_ref.set({
                "user_id": user_id,
                "name": data.get("name"),
                "phone": data.get("phone"),
                "position": data.get("position"),
                "experience": data.get("exp"),
                "cv_file_id": file_id,
                "cv_type": f_type,
                "timestamp": firestore.SERVER_TIMESTAMP
            })
            return True
        except Exception as e:
            logger.error(f"Firestore save error: {e}")
            return False

    def get_user_state(self, user_id):
        user_id_str = str(user_id)
        with self._lock:
            if user_id_str in self._user_states:
                return self._user_states[user_id_str]
        
        if not self.db: return None
        try:
            doc = self.db.collection("user_states").document(user_id_str).get()
            state = doc.to_dict() if doc.exists else None
            with self._lock:
                self._user_states[user_id_str] = state
            return state
        except Exception as e:
            logger.error(f"Error getting user state: {e}")
            return None

    def set_user_state(self, user_id, state):
        user_id_str = str(user_id)
        with self._lock:
            self._user_states[user_id_str] = state
        
        if not self.db: return
        try:
            if state is None:
                self.db.collection("user_states").document(user_id_str).delete()
            else:
                self.db.collection("user_states").document(user_id_str).set(state)
        except Exception as e:
            logger.error(f"Error setting user state: {e}")

    def get_user_lang(self, user_id):
        user_id_str = str(user_id)
        with self._lock:
            if user_id_str in self._user_langs:
                return self._user_langs[user_id_str]
            
        if not self.db: return "uz"
        try:
            doc = self.db.collection("user_langs").document(user_id_str).get()
            lang = doc.to_dict().get("lang", "uz") if doc.exists else "uz"
            with self._lock:
                self._user_langs[user_id_str] = lang
            return lang
        except Exception as e:
            logger.error(f"Error getting user lang: {e}")
            return "uz"

    def set_user_lang(self, user_id, lang):
        user_id_str = str(user_id)
        with self._lock:
            self._user_langs[user_id_str] = lang
        
        if not self.db: return
        try:
            self.db.collection("user_langs").document(user_id_str).set({"lang": lang})
        except Exception as e:
            logger.error(f"Error setting user lang: {e}")

    def get_recent_applications(self, limit=10):
        if not self.db:
            return []
        try:
            query = self.db.collection("applications").order_by("timestamp", direction=firestore.Query.DESCENDING).limit(limit)
            docs = query.stream()
            items = []
            for doc in docs:
                data = doc.to_dict() or {}
                items.append({"id": doc.id, **data})
            return items
        except Exception as e:
            logger.error(f"Error getting recent applications: {e}")
            return []

    def get_application(self, doc_id):
        if not self.db:
            return None
        try:
            doc = self.db.collection("applications").document(str(doc_id)).get()
            if not doc.exists:
                return None
            data = doc.to_dict() or {}
            return {"id": doc.id, **data}
        except Exception as e:
            logger.error(f"Error getting application: {e}")
            return None

    def search_applications_by_position(self, query_text, limit=50, scan_limit=300):
        if not self.db:
            return []
        q = (query_text or "").strip().lower()
        if not q:
            return []
        try:
            query = self.db.collection("applications").order_by("timestamp", direction=firestore.Query.DESCENDING).limit(scan_limit)
            docs = query.stream()
            items = []
            for doc in docs:
                data = doc.to_dict() or {}
                position = str(data.get("position") or "")
                if q in position.lower():
                    items.append({"id": doc.id, **data})
                if len(items) >= limit:
                    break
            return items
        except Exception as e:
            logger.error(f"Error searching applications: {e}")
            return []

    def get_position_stats(self, days=30, limit=1000):
        if not self.db:
            return {}
        start = datetime.utcnow() - timedelta(days=days)
        try:
            query = (
                self.db.collection("applications")
                .where("timestamp", ">=", start)
                .order_by("timestamp", direction=firestore.Query.DESCENDING)
                .limit(limit)
            )
            docs = query.stream()
            stats = {}
            total = 0
            for doc in docs:
                data = doc.to_dict() or {}
                position = str(data.get("position") or "Noma'lum")
                stats[position] = stats.get(position, 0) + 1
                total += 1
            stats["_total"] = total
            return stats
        except Exception as e:
            logger.error(f"Error getting stats: {e}")
            return {}

class BotLogic:
    def __init__(self, api, db):
        self.api = api
        self.db = db
        self.positions = {
            "uz": [
                ["🏢 Boshqaruv", "👨‍🏫 O'qituvchi"],
                ["🧹 Tozalik hodimi", "🛡 Xavfsizlik / Qo'riqlash"],
                ["💡 Boshqa lavozim"]
            ],
            "en": [
                ["🏢 Management", "👨‍🏫 Teacher"],
                ["🧹 Cleaning staff", "🛡 Security"],
                ["💡 Other position"]
            ],
            "ru": [
                ["🏢 Управление", "👨‍🏫 Учитель"],
                ["🧹 Уборка", "🛡 Безопасность"],
                ["💡 Другая должность"]
            ]
        }
        self.labels = {
            "menu_about": {"uz": "🏫 Biz haqimizda", "en": "🏫 About us", "ru": "🏫 О нас"},
            "menu_contact": {"uz": "💬 Biz bilan bog'lanish", "en": "💬 Contact us", "ru": "💬 Связаться"},
            "menu_location": {"uz": "📍 Manzilimiz", "en": "📍 Our Location", "ru": "📍 Наш адрес"},
            "menu_jobs": {"uz": "💼 Bo'sh ish o'rinlari", "en": "💼 Job vacancies", "ru": "💼 Вакансии"},
            "menu_lang": {"uz": "🌐 Tilni almashtirish", "en": "🌐 Change language", "ru": "🌐 Сменить язык"},
            "back": {"uz": "⬅️ Orqaga", "en": "⬅️ Back", "ru": "⬅️ Назад"},
            "cancel": {"uz": "❌ Bekor qilish", "en": "❌ Cancel", "ru": "❌ Отмена"},
            "skip": {"uz": "O'tkazib yuborish", "en": "Skip", "ru": "Пропустить"},
            "send_contact": {"uz": "Kontaktni yuborish", "en": "Send contact", "ru": "Отправить контакт"},
            "lang_uz": {"uz": "🇺🇿 UZ", "en": "🇺🇿 UZ", "ru": "🇺🇿 UZ"},
            "lang_en": {"uz": "🇬🇧 ENG", "en": "🇬🇧 ENG", "ru": "🇬🇧 ENG"},
            "lang_ru": {"uz": "🇷🇺 RUS", "en": "🇷🇺 RUS", "ru": "🇷🇺 RUS"},
            "menu_admin": {"uz": "🔐 Admin", "en": "🔐 Admin", "ru": "🔐 Админ"},
            "admin_recent_10": {"uz": "📥 Oxirgi 10 ta", "en": "📥 Last 10", "ru": "📥 Последние 10"},
            "admin_recent_50": {"uz": "📥 Oxirgi 50 ta", "en": "📥 Last 50", "ru": "📥 Последние 50"},
            "admin_search": {"uz": "🔎 Lavozim bo‘yicha qidirish", "en": "🔎 Search by position", "ru": "🔎 Поиск по должности"},
            "admin_stats": {"uz": "📊 Statistika (30 kun)", "en": "📊 Statistics (30 days)", "ru": "📊 Статистика (30 дней)"},
            "admin_back": {"uz": "⬅️ Orqaga", "en": "⬅️ Back", "ru": "⬅️ Назад"},
            "other_pos": {"uz": "💡 Boshqa lavozim", "en": "💡 Other position", "ru": "💡 Другая должность"},
            
            # Messages
            "msg_welcome": {
                "uz": "<b>Assalomu alaykum!</b>\n\nKerakli bo'limni tanlang:",
                "en": "<b>Hello!</b>\n\nPlease choose a section:",
                "ru": "<b>Здравствуйте!</b>\n\nПожалуйста, выберите раздел:"
            },
            "msg_about": {
                "uz": "<b>🏫 Al-Xorazmiy maktabi haqida:</b>\n\n"
                      "🎓 <b>Ta'lim:</b> 1-11 sinflar va maxsus tayyorlov kurslari.\n"
                      "🇺🇿 <b>Til:</b> O'zbek tili.\n"
                      "📚 <b>Chuqurlashtirilgan fanlar:</b> Ingliz tili, Matematika, IT va Arab tili.\n"
                      "🍱 <b>Oshxona:</b> 2 mahal bepul, halol va sifatli taomlar.\n"
                      "⏰ <b>Vaqt:</b> Darslar 8:30 – 17:00 (Shanba 14:00 gacha).\n"
                      "🗓 <b>Hafta:</b> 6 kunlik o'quv tizimi.",
                "en": "<b>🏫 About Al-Khwarizmi School:</b>\n\n"
                      "🎓 <b>Education:</b> Grades 1-11 and preschool preparation.\n"
                      "🇺🇿 <b>Language:</b> Uzbek.\n"
                      "📚 <b>Advanced subjects:</b> English, Math, IT, and Arabic.\n"
                      "🍱 <b>Dining:</b> 2 free, Halal, and high-quality meals.\n"
                      "⏰ <b>Schedule:</b> 8:30 AM – 5:00 PM (Saturday until 2:00 PM).\n"
                      "🗓 <b>Week:</b> 6-day school week.",
                "ru": "<b>🏫 О школе Аль-Хорезми:</b>\n\n"
                      "🎓 <b>Обучение:</b> 1-11 классы и подготовительные курсы.\n"
                      "🇺🇿 <b>Язык:</b> Узбекский.\n"
                      "📚 <b>Углубленные предметы:</b> Английский, Математика, IT и Арабский язык.\n"
                      "🍱 <b>Питание:</b> 2-разовое бесплатное, Халяль и качественная еда.\n"
                      "⏰ <b>График:</b> 8:30 – 17:00 (Суббота до 14:00).\n"
                      "🗓 <b>Неделя:</b> 6-дневная учебная неделя."
            },
            "msg_contact": {
                "uz": "<b>📞 Biz bilan bog'lanish:</b>\n\n"
                      "☎️ <b>Telefon:</b> +998692100007\n"
                      "👨‍💻 <b>Telegram:</b> @Onlineeaz\n\n"
                      "Savollaringiz bo'lsa, qo'ng'iroq qilishingiz yoki adminga murojaat qilishingiz mumkin. 😊",
                "en": "<b>📞 Contact us:</b>\n\n"
                      "☎️ <b>Phone:</b> +998692100007\n"
                      "👨‍💻 <b>Telegram:</b> @Onlineeaz\n\n"
                      "If you have any questions, feel free to call or contact the admin. 😊",
                "ru": "<b>📞 Связаться с нами:</b>\n\n"
                      "☎️ <b>Телефон:</b> +998692100007\n"
                      "👨‍💻 <b>Telegram:</b> @Onlineeaz\n\n"
                      "Если у вас есть вопросы, вы можете позвонить или написать админу. 😊"
            },
            "msg_location": {
                "uz": "<b>📍 Manzilimiz:</b>\n\n"
                      "🇺🇿 Maktabimiz Namangan viloyatining Namangan tumanida joylashgan.\n\n"
                      "📍 <b>Mo'ljal:</b>\n"
                      "Lola jahon bozoridan o'tganda, Qumqo'rg'on svetofori oldida.\n\n"
                      "📍 <b>Lokatsiya:</b>\n"
                      "https://goo.gl/maps/T71FNWrrKkMFVmvU9",
                "en": "<b>📍 Our Location:</b>\n\n"
                      "🇺🇿 Our school is located in the Namangan district of the Namangan region.\n\n"
                      "📍 <b>Landmark:</b>\n"
                      "Past the Lola world market, near the Qumqorgon traffic light.\n\n"
                      "📍 <b>Location:</b>\n"
                      "https://goo.gl/maps/T71FNWrrKkMFVmvU9",
                "ru": "<b>📍 Наш адрес:</b>\n\n"
                      "🇺🇿 Наша школа находится в Наманганском районе Наманганской области.\n\n"
                      "📍 <b>Ориентир:</b>\n"
                      "После мирового рынка Лола, возле светофора Кумкурган.\n\n"
                      "📍 <b>Локация:</b>\n"
                      "https://goo.gl/maps/T71FNWrrKkMFVmvU9"
            },
            "msg_ask_name": {
                "uz": "<b>Bo'sh ish o'rinlari</b>\n\nIltimos, ism va familiyangizni kiriting:",
                "en": "<b>Job vacancies</b>\n\nPlease enter your first and last name:",
                "ru": "<b>Вакансии</b>\n\nПожалуйста, введите ваше имя и фамилию:"
            },
            "msg_ask_phone": {
                "uz": "Telefon raqamingizni yuboring (tugmani bosing):",
                "en": "Send your phone number (click the button):",
                "ru": "Отправьте свой номер телефона (нажмите кнопку):"
            },
            "msg_ask_position": {
                "uz": "Qaysi bo'limga topshirmoqchisiz? (Tanlang):",
                "en": "Which section are you applying for? (Choose):",
                "ru": "В какой раздел вы подаете заявку? (Выберите):"
            },
            "msg_ask_position_manual": {
                "uz": "Iltimos, mutaxassisligingiz yoki lavozim turini kiriting (Masalan: Matematika o'qituvchisi, Bosh buxgalter va h.k.):",
                "en": "Please enter your specialization or position type (Example: Math Teacher, Chief Accountant, etc.):",
                "ru": "Пожалуйста, введите вашу специализацию или тип должности (Например: Учитель математики, Главный бухгалтер и т. д.):"
            },
            "msg_ask_exp": {
                "uz": "Ish tajribangiz haqida qisqacha ma'lumot bering:",
                "en": "Provide brief information about your work experience:",
                "ru": "Кратко расскажите о своем опыте работы:"
            },
            "msg_ask_cv": {
                "uz": "Rezyume (PDF yoki Rasm) yuboring yoki 'O'tkazib yuborish' tugmasini bosing:",
                "en": "Send your resume (PDF or Image) or click 'Skip':",
                "ru": "Отправьте резюме (PDF или фото) или нажмите 'Пропустить':"
            },
            "msg_applied": {
                "uz": "✅ <b>Arizangiz HR bo'limiga yuborildi.</b> Siz bilan tez orada bog'lanamiz.",
                "en": "✅ <b>Your application has been sent to the HR department.</b> We will contact you soon.",
                "ru": "✅ <b>Ваша заявка отправлена в отдел кадров.</b> Мы свяжемся с вами в ближайшее время."
            },
            "msg_canceled": {
                "uz": "Ariza topshirish bekor qilindi.",
                "en": "Application canceled.",
                "ru": "Подача заявки отменена."
            },
            "msg_invalid_name": {
                "uz": "Iltimos, ism va familiyangizni to'liq yozing (Masalan: Ali Valiyev):",
                "en": "Please write your full name (Example: Ali Valiyev):",
                "ru": "Пожалуйста, напишите свое полное имя (Например: Али Валиев):"
            },
            "msg_invalid_phone": {
                "uz": "Iltimos, telefon raqamingizni tugma orqali yuboring yoki yozing:",
                "en": "Please send your phone number via button or type it:",
                "ru": "Пожалуйста, отправьте свой номер телефона через кнопку или напишите его:"
            },
            "msg_invalid_exp": {
                "uz": "Tajribangiz haqida batafsilroq yozing:",
                "en": "Write more about your experience:",
                "ru": "Напишите подробнее о своем опыте:"
            },
            "msg_invalid_cv": {
                "uz": "Iltimos, fayl yuboring yoki tugmani bosing.",
                "en": "Please send a file or click the button.",
                "ru": "Пожалуйста, отправьте файл или нажмите кнопку."
            },
            "msg_select_lang": {
                "uz": "Tilni tanlang:",
                "en": "Choose language:",
                "ru": "Выберите язык:"
            },
            "msg_lang_changed": {
                "uz": "✅ Til o'zgartirildi.",
                "en": "✅ Language changed.",
                "ru": "✅ Язык изменен."
            },
            "msg_choose_menu": {
                "uz": "Iltimos, pastdagi menyudan birini tanlang.",
                "en": "Please choose from the menu below.",
                "ru": "Пожалуйста, выберите из меню ниже."
            },
            "admin_panel": {
                "uz": "Admin panel:",
                "en": "Admin panel:",
                "ru": "Админ панель:"
            },
            "admin_search_ask": {
                "uz": "Lavozim nomini kiriting:",
                "en": "Enter the position name:",
                "ru": "Введите название должности:"
            },
            "admin_no_results": {
                "uz": "Natija topilmadi.",
                "en": "No results found.",
                "ru": "Результатов не найдено."
            },
            "admin_no_apps": {
                "uz": "Hozircha arizalar topilmadi.",
                "en": "No applications found yet.",
                "ru": "Заявок пока не найдено."
            },
            "admin_firebase_error": {
                "uz": "Firebase ulanmagan.",
                "en": "Firebase not connected.",
                "ru": "Firebase не подключен."
            },
            "admin_app_details": {
                "uz": "<b>Ariza tafsiloti</b>",
                "en": "<b>Application detail</b>",
                "ru": "<b>Детали заявки</b>"
            },
            "admin_stats_title": {
                "uz": "<b>Statistika (oxirgi {days} kun)</b>",
                "en": "<b>Statistics (last {days} days)</b>",
                "ru": "<b>Статистика (за последние {days} дней)</b>"
            },
            "admin_total": {
                "uz": "Jami",
                "en": "Total",
                "ru": "Всего"
            },
            "admin_closed": {
                "uz": "Yopildi.",
                "en": "Closed.",
                "ru": "Закрыто."
            }
        }

    def _label(self, key, lang):
        return self.labels.get(key, {}).get(lang) or self.labels.get(key, {}).get("uz") or key

    def _main_menu(self, lang, chat_id=None):
        is_hr = str(chat_id) == str(Config.HR_CHAT_ID) if chat_id and Config.HR_CHAT_ID else False
        
        # 1. Bo'sh ish o'rinlar (to'liq qator)
        # 2. Manzilimiz | Biz haqimizda
        # 3. Biz bilan bog'lanish (to'liq qator)
        # 4. Tilni almashtirish | Admin (agar admin bo'lsa)
        
        kb = [
            [{"text": self._label("menu_jobs", lang)}],
            [{"text": self._label("menu_location", lang)}, {"text": self._label("menu_about", lang)}],
            [{"text": self._label("menu_contact", lang)}]
        ]
        
        last_row = [{"text": self._label("menu_lang", lang)}]
        if is_hr:
            last_row.append({"text": self._label("menu_admin", lang)})
        kb.append(last_row)
            
        return {
            "keyboard": kb,
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

    def _admin_menu(self, lang="uz"):
        return {
            "keyboard": [
                [{"text": self._label("admin_recent_10", lang)}, {"text": self._label("admin_recent_50", lang)}],
                [{"text": self._label("admin_search", lang)}],
                [{"text": self._label("admin_stats", lang)}],
                [{"text": self._label("admin_back", lang)}],
            ],
            "resize_keyboard": True
        }

    def _action_from_text(self, text):
        if not text: return None
        for action_key, translations in self.labels.items():
            if text in translations.values():
                return action_key
        return None

    def handle_update(self, update):
        message = update.get("message")
        if not message: return
        
        chat_id = message["chat"]["id"]
        user_id = message["from"]["id"]
        text = message.get("text", "")
        contact = message.get("contact")
        
        lang = self.db.get_user_lang(user_id)
        state = self.db.get_user_state(user_id)
        is_hr_chat = str(chat_id) == str(Config.HR_CHAT_ID)

        if is_hr_chat:
            admin_handled = self._handle_admin(update, chat_id, user_id, text, state)
            if admin_handled:
                return

        if text in ["/start", "/menu"] or text == "Menu":
            self.db.set_user_state(user_id, None)
            self.api.send_message(chat_id, self._label("msg_welcome", lang), self._main_menu(lang, chat_id))
            return
        
        action = self._action_from_text(text)

        if action == "menu_lang":
            self.api.send_message(chat_id, self._label("msg_select_lang", lang), self._lang_menu(lang))
            return

        if action in ["lang_uz", "lang_en", "lang_ru"]:
            new_lang = "uz" if action == "lang_uz" else ("en" if action == "lang_en" else "ru")
            self.db.set_user_lang(user_id, new_lang)
            self.api.send_message(chat_id, self._label("msg_lang_changed", new_lang), self._main_menu(new_lang, chat_id))
            return

        if action == "back":
            self.api.send_message(chat_id, "Menu:", self._main_menu(lang, chat_id))
            return

        if not state:
            if action == "menu_about":
                self.api.send_message(chat_id, self._label("msg_about", lang), self._main_menu(lang, chat_id))
                return

            if action == "menu_contact":
                self.api.send_message(chat_id, self._label("msg_contact", lang), self._main_menu(lang, chat_id))
                return

            if action == "menu_location":
                self.api.send_message(chat_id, self._label("msg_location", lang), self._main_menu(lang, chat_id))
                return

            if action == "menu_jobs":
                self.db.set_user_state(user_id, {"step": "name", "data": {}, "mode": "job"})
                self.api.send_message(chat_id, self._label("msg_ask_name", lang), {"remove_keyboard": True})
                return
            
            # Agar hech qanday action bo'lmasa va state yo'q bo'lsa
            self.api.send_message(chat_id, self._label("msg_choose_menu", lang), self._main_menu(lang, chat_id))
            return

        if state and state.get("mode") == "admin":
            self.api.send_message(chat_id, self._label("admin_panel", lang), self._admin_menu(lang))
            return

        # Ariza topshirish flow'i
        if action == "cancel":
            self.db.set_user_state(user_id, None)
            self.api.send_message(chat_id, self._label("msg_canceled", lang), self._main_menu(lang, chat_id))
            return

        step = state.get("step")
        data = state.get("data", {})
        
        if step == "name":
            if self._is_valid_name(text):
                data["name"] = text
                state["step"] = "phone"
                state["data"] = data
                self.db.set_user_state(user_id, state)
                markup = {
                    "keyboard": [
                        [{"text": self._label("send_contact", lang), "request_contact": True}],
                        [{"text": self._label("cancel", lang)}]
                    ],
                    "resize_keyboard": True,
                    "one_time_keyboard": True
                }
                self.api.send_message(chat_id, self._label("msg_ask_phone", lang), markup)
            else:
                self.api.send_message(chat_id, f"{self._label('msg_invalid_name', lang)}\n\n{self._label('cancel', lang)}: '{self._label('cancel', lang)}'")
        
        elif step == "phone":
            phone_val = contact.get("phone_number") if contact else (text if self._is_valid_phone(text) else None)
            if phone_val:
                data["phone"] = phone_val
                state["step"] = "position"
                state["data"] = data
                self.db.set_user_state(user_id, state)
                kb = [[{"text": p} for p in row] for row in self.positions.get(lang, self.positions["uz"])]
                kb.append([{"text": self._label("cancel", lang)}])
                markup = {"keyboard": kb, "resize_keyboard": True}
                self.api.send_message(chat_id, self._label("msg_ask_position", lang), markup)
            else:
                self.api.send_message(chat_id, self._label("msg_invalid_phone", lang))

        elif step == "position":
            # Bo'lim tanlanganida
            data["category"] = text
            state["step"] = "position_manual"
            state["data"] = data
            self.db.set_user_state(user_id, state)
            
            # Kreativ xabar: tanlangan bo'limga qarab har xil so'rash
            msg = self._label("msg_ask_position_manual", lang)
            
            # Agar kreativlik qo'shmoqchi bo'lsak, bo'lim nomini xabarga qo'shamiz
            if "uz" in lang:
                msg = f"Siz <b>{text}</b> bo'limini tanladingiz.\n\nIltimos, endi aniq lavozim yoki mutaxassislikni yozing (Masalan: Matematika o'qituvchisi, Bosh buxgalter va h.k.):"
            elif "en" in lang:
                msg = f"You selected the <b>{text}</b> section.\n\nPlease now enter the specific position or specialization (Example: Math Teacher, Chief Accountant, etc.):"
            elif "ru" in lang:
                msg = f"Вы выбрали раздел <b>{text}</b>.\n\nТеперь введите конкретную должность или специализацию (Например: Учитель математики, Главный бухгалтер и т. д.):"

            markup = {"keyboard": [[{"text": self._label("cancel", lang)}]], "resize_keyboard": True}
            self.api.send_message(chat_id, msg, markup)

        elif step == "position_manual":
            if len(text) > 2:
                category = data.get("category", "")
                # Bo'lim va lavozimni birlashtirish (masalan: "O'qituvchi (Matematika)")
                # Agar "Boshqa lavozim" bo'lsa, faqat kiritilgan matnni olamiz
                other_label = self._label("other_pos", lang)
                if category == other_label:
                    data["position"] = text
                else:
                    # Emojilarni olib tashlash (toza ko'rinish uchun)
                    clean_cat = category.split(" ", 1)[-1] if " " in category else category
                    data["position"] = f"{clean_cat} ({text})"
                
                state["step"] = "exp"
                state["data"] = data
                self.db.set_user_state(user_id, state)
                markup = {"keyboard": [[{"text": self._label("cancel", lang)}]], "resize_keyboard": True}
                self.api.send_message(chat_id, self._label("msg_ask_exp", lang), markup)
            else:
                self.api.send_message(chat_id, self._label("msg_ask_position_manual", lang))

        elif step == "exp":
            if len(text) > 5:
                data["exp"] = text
                state["step"] = "cv"
                state["data"] = data
                self.db.set_user_state(user_id, state)
                markup = {
                    "keyboard": [[{"text": self._label("skip", lang)}], [{"text": self._label("cancel", lang)}]],
                    "resize_keyboard": True, "one_time_keyboard": True
                }
                self.api.send_message(chat_id, self._label("msg_ask_cv", lang), markup)
            else:
                self.api.send_message(chat_id, self._label("msg_invalid_exp", lang))

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
                self.api.send_message(chat_id, self._label("msg_invalid_cv", lang))
                return

            # Firebase va HR ga yuborish
            saved = self.db.save_application(user_id, data, cv_file_id, cv_type)
            self._send_to_hr(user_id, data, cv_file_id, cv_type, saved)
            
            self.api.send_message(chat_id, self._label("msg_applied", lang), self._main_menu(lang, chat_id))
            self.db.set_user_state(user_id, None)

    def _handle_admin(self, update, chat_id, user_id, text, state):
        t = (text or "").strip()
        lang = self.db.get_user_lang(user_id)
        
        admin_buttons = {
            self._label("admin_back", lang),
            self._label("admin_recent_10", lang),
            self._label("admin_recent_50", lang),
            self._label("admin_search", lang),
            self._label("admin_stats", lang),
        }
        
        # Check for admin menu action
        action = self._action_from_text(t)
        if action == "menu_admin":
             self.db.set_user_state(user_id, {"mode": "admin", "step": "menu"})
             self.api.send_message(chat_id, self._label("admin_panel", lang), self._admin_menu(lang))
             return True

        if t.startswith("/admin"):
            self.db.set_user_state(user_id, {"mode": "admin", "step": "menu"})
            self.api.send_message(chat_id, self._label("admin_panel", lang), self._admin_menu(lang))
            return True

        if t in admin_buttons and (not state or state.get("mode") != "admin"):
            self.db.set_user_state(user_id, {"mode": "admin", "step": "menu"})
            state = {"mode": "admin", "step": "menu"}

        if t.startswith("/a ") and (not state or state.get("mode") != "admin"):
            self.db.set_user_state(user_id, {"mode": "admin", "step": "menu"})
            state = {"mode": "admin", "step": "menu"}

        if not state or state.get("mode") != "admin":
            if t.startswith("/a "):
                doc_id = t[3:].strip()
                self._send_application_details(chat_id, doc_id, lang)
                return True
            return False

        if t == self._label("admin_back", lang):
            self.db.set_user_state(user_id, None)
            self.api.send_message(chat_id, self._label("msg_welcome", lang), self._main_menu(lang, chat_id))
            return True

        if t == self._label("admin_recent_10", lang):
            self._send_recent_applications(chat_id, limit=10, lang=lang)
            self.db.set_user_state(user_id, {"mode": "admin", "step": "menu"})
            return True

        if t == self._label("admin_recent_50", lang):
            self._send_recent_applications(chat_id, limit=50, lang=lang)
            self.db.set_user_state(user_id, {"mode": "admin", "step": "menu"})
            return True

        if t == self._label("admin_search", lang):
            self.db.set_user_state(user_id, {"mode": "admin", "step": "search_position"})
            self.api.send_message(chat_id, self._label("admin_search_ask", lang), self._admin_menu(lang))
            return True

        if t == self._label("admin_stats", lang):
            self._send_stats(chat_id, days=30, lang=lang)
            self.db.set_user_state(user_id, {"mode": "admin", "step": "menu"})
            return True

        if t.startswith("/a "):
            doc_id = t[3:].strip()
            self._send_application_details(chat_id, doc_id, lang)
            return True

        if state.get("step") == "search_position":
            results = self.db.search_applications_by_position(t, limit=50, scan_limit=300)
            if not self.db.db:
                self.api.send_message(chat_id, self._label("admin_firebase_error", lang), self._admin_menu(lang))
                self.db.set_user_state(user_id, {"mode": "admin", "step": "menu"})
                return True
            if not results:
                self.api.send_message(chat_id, self._label("admin_no_results", lang), self._admin_menu(lang))
                self.db.set_user_state(user_id, {"mode": "admin", "step": "menu"})
                return True
            self._send_applications_list(chat_id, results, title=f"{self._label('admin_search', lang)}: {t}", lang=lang)
            self.db.set_user_state(user_id, {"mode": "admin", "step": "menu"})
            return True

        return False

    def _fmt_ts(self, ts):
        if not ts:
            return "—"
        try:
            if hasattr(ts, "strftime"):
                return ts.strftime("%d.%m.%Y %H:%M")
        except Exception:
            pass
        return str(ts)

    def _send_in_chunks(self, chat_id, text, reply_markup=None, max_len=3500):
        lines = (text or "").splitlines() or [""]
        buf = ""
        for line in lines:
            candidate = (buf + "\n" + line) if buf else line
            if len(candidate) > max_len and buf:
                self.api.send_message(chat_id, buf, reply_markup)
                buf = line
            else:
                buf = candidate
        if buf:
            self.api.send_message(chat_id, buf, reply_markup)

    def _send_recent_applications(self, chat_id, limit=10, lang="uz"):
        if not self.db.db:
            self.api.send_message(chat_id, self._label("admin_firebase_error", lang), self._admin_menu(lang))
            return
        items = self.db.get_recent_applications(limit=limit)
        if not items:
            self.api.send_message(chat_id, self._label("admin_no_apps", lang), self._admin_menu(lang))
            return
        self._send_applications_list(chat_id, items, title=f"{self._label('admin_recent_10' if limit==10 else 'admin_recent_50', lang)}", lang=lang)

    def _send_applications_list(self, chat_id, items, title, lang="uz"):
        self.api.send_message(chat_id, f"<b>{title}</b>", self._admin_menu(lang))
        
        for i, item in enumerate(items, start=1):
            ts = self._fmt_ts(item.get("timestamp"))
            name = item.get("name") or "—"
            phone = item.get("phone") or "—"
            position = item.get("position") or "—"
            exp = item.get("experience") or "—"
            
            cv_file_id = item.get("cv_file_id")
            cv_type = item.get("cv_type")
            
            caption = (
                f"{i}. 👤 <b>{name}</b>\n"
                f"   💼 {position}\n"
                f"   📞 {phone}\n"
                f"   📝 {exp}\n"
                f"   📅 {ts}"
            )
            
            if cv_file_id:
                method = "sendDocument" if cv_type == "doc" else "sendPhoto"
                param_key = "document" if cv_type == "doc" else "photo"
                self.api.call(method, {"chat_id": chat_id, param_key: cv_file_id, "caption": caption, "parse_mode": "HTML"})
            else:
                self.api.send_message(chat_id, caption)

    def _send_application_details(self, chat_id, doc_id, lang="uz"):
        if not self.db.db:
            self.api.send_message(chat_id, self._label("admin_firebase_error", lang), self._admin_menu(lang))
            return
        item = self.db.get_application(doc_id)
        if not item:
            self.api.send_message(chat_id, self._label("admin_no_results", lang), self._admin_menu(lang))
            return
        ts = self._fmt_ts(item.get("timestamp"))
        name = item.get("name") or "—"
        phone = item.get("phone") or "—"
        position = item.get("position") or "—"
        exp = item.get("experience") or "—"
        user_id = item.get("user_id") or "—"
        cv_file_id = item.get("cv_file_id")
        cv_type = item.get("cv_type")
        
        # Localized fields for details
        sana_lbl = "🕒 Sana" if lang == "uz" else ("🕒 Date" if lang == "en" else "🕒 Дата")
        nomzod_lbl = "👤 Nomzod" if lang == "uz" else ("👤 Candidate" if lang == "en" else "👤 Кандидат")
        tel_lbl = "📞 Tel" if lang == "uz" else ("📞 Phone" if lang == "en" else "📞 Тел")
        lavozim_lbl = "💼 Lavozim" if lang == "uz" else ("💼 Position" if lang == "en" else "💼 Должность")
        tajriba_lbl = "📝 Tajriba" if lang == "uz" else ("📝 Experience" if lang == "en" else "📝 Опыт")
        rez_lbl = "Rezyume" if lang == "uz" else ("Resume" if lang == "en" else "Резюме")

        report = (
            f"{self._label('admin_app_details', lang)}\n\n"
            f"{sana_lbl}: {ts}\n"
            f"{nomzod_lbl}: {name}\n"
            f"{tel_lbl}: {phone}\n"
            f"{lavozim_lbl}: {position}\n"
            f"{tajriba_lbl}: {exp}\n"
            f"🆔 User ID: {user_id}\n"
            f"📄 Doc ID: <code>{item.get('id')}</code>"
        )
        self.api.send_message(chat_id, report, self._admin_menu(lang))
        if cv_file_id:
            method = "sendDocument" if cv_type == "doc" else "sendPhoto"
            param_key = "document" if cv_type == "doc" else "photo"
            self.api.call(method, {"chat_id": chat_id, param_key: cv_file_id, "caption": f"{name} - {rez_lbl}"})

    def _send_stats(self, chat_id, days=30, lang="uz"):
        if not self.db.db:
            self.api.send_message(chat_id, self._label("admin_firebase_error", lang), self._admin_menu(lang))
            return
        stats = self.db.get_position_stats(days=days, limit=1000)
        total = stats.pop("_total", 0) if stats else 0
        if not stats:
            self.api.send_message(chat_id, self._label("admin_no_results", lang), self._admin_menu(lang))
            return
        sorted_items = sorted(stats.items(), key=lambda x: x[1], reverse=True)
        title = self._label("admin_stats_title", lang).format(days=days)
        total_lbl = self._label("admin_total", lang)
        lines = [title, f"{total_lbl}: {total}", ""]
        for position, count in sorted_items:
            lines.append(f"- {position}: {count}")
        self._send_in_chunks(chat_id, "\n".join(lines), self._admin_menu(lang))

    def _is_valid_name(self, text):
        if not text: return False
        parts = text.strip().split()
        return len(parts) >= 2 and len(text) >= 5

    def _is_valid_phone(self, text):
        if not text: return False
        digits = "".join(filter(str.isdigit, text))
        return len(digits) >= 7

    def _send_to_hr(self, user_id, data, file_id, f_type, saved_to_firebase):
        report = (
            f"<b>Sizning arizangiz</b>\n\n"
            f"👤 Nomzod: {data.get('name')}\n"
            f"📞 Tel: {data.get('phone')}\n"
            f"💼 Lavozim: {data.get('position')}\n"
            f"📝 Tajriba: {data.get('exp')}"
        )
        
        if file_id:
            method = "sendDocument" if f_type == "doc" else "sendPhoto"
            param_key = "document" if f_type == "doc" else "photo"
            params = {
                "chat_id": Config.HR_CHAT_ID,
                param_key: file_id,
                "caption": report,
                "parse_mode": "HTML"
            }
            self.api.call(method, params)
        else:
            self.api.send_message(Config.HR_CHAT_ID, report)

def run_polling():
    if not Config.validate():
        sys.exit(1)

    api = TelegramAPI(Config.TOKEN)
    db = FirestoreDB()
    bot = BotLogic(api, db)
    
    offset = 0
    logger.info("Bot ishga tushdi. Yangilanishlar kutilmoqda (polling)...")
    
    # Webhookni o'chirish (polling rejimida ishlash uchun)
    api.call("deleteWebhook", {"drop_pending_updates": True})
    
    # Bot komandalarini o'rnatish
    commands = [
        {"command": "start", "description": "Botni ishga tushirish"},
        {"command": "menu", "description": "Asosiy menyu"},
        {"command": "admin", "description": "Admin panel (faqat adminlar)"}
    ]
    api.call("setMyCommands", {"commands": json.dumps(commands)})
    logger.info("Bot komandalari o'rnatildi")
    
    executor = ThreadPoolExecutor(max_workers=20)
    retry_count = 0
    
    while True:
        try:
            result = api.call("getUpdates", {"timeout": 30, "offset": offset})
            
            if not result.get("ok"):
                error_code = result.get("error_code")
                description = result.get("description", "")
                
                if error_code == 409: # Conflict
                    logger.warning("Conflict aniqlandi, webhook o'chirilmoqda...")
                    api.call("deleteWebhook", {"drop_pending_updates": True})
                    time.sleep(2)
                elif error_code == 401: # Unauthorized
                    logger.error("TOKEN noto'g'ri!")
                    break
                else:
                    logger.error(f"Polling xatosi: {description}")
                    time.sleep(2)
                continue

            updates = result.get("result") or []
            for upd in updates:
                update_id = upd.get("update_id")
                if isinstance(update_id, int):
                    offset = update_id + 1
                
                # Update'ni alohida thread'da qayta ishlash
                executor.submit(bot.handle_update, upd)
            
            retry_count = 0 
        except requests.exceptions.ConnectionError:
            retry_count += 1
            wait_time = min(retry_count * 2, 30)
            logger.warning(f"Internet aloqasi yo'q. {wait_time} soniyadan keyin qayta uriniladi...")
            time.sleep(wait_time)
        except Exception as e:
            logger.exception(f"Kutilmagan xatolik: {e}")
            time.sleep(2)

if __name__ == "__main__":
    try:
        run_polling()
    except KeyboardInterrupt:
        logger.info("Bot to'xtatildi.")
    except Exception as e:
        logger.critical(f"Bot kutilmaganda to'xtadi: {e}")
