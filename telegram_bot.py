import json
import os
import sys
import time
import logging
import requests
import threading
import signal
from flask import Flask
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

    def call(self, method, params=None, files=None, timeout=10, max_retries=2):
        url = self.base_url + method

        # getUpdates uchun timeout'ni sozlash
        if method == "getUpdates":
            timeout = params.get("timeout", 30) + 5 if params else 35
        # sendMessage va boshqa methodlar uchun timeout'ni oshirish
        elif method in ["sendMessage", "sendPhoto", "sendDocument", "editMessageText"]:
            timeout = 20

        # Retry mexanizmi (getUpdates bundan mustasno)
        retries = max_retries if method != "getUpdates" else 0

        for attempt in range(retries + 1):
            try:
                response = self.session.post(url, data=params, files=files, timeout=timeout)
                response.raise_for_status()
                return response.json()
            except requests.exceptions.Timeout as e:
                if attempt < retries:
                    wait_time = 0.5 * (attempt + 1)  # 0.5s, 1s
                    logger.warning(f"API timeout ({method}), qayta urinilmoqda ({attempt + 1}/{retries + 1})...")
                    time.sleep(wait_time)
                else:
                    logger.error(f"API timeout ({method}) - barcha urinishlar muvaffaqiyatsiz: {e}")
                    return {"ok": False, "description": f"Timeout: {str(e)}"}
            except requests.exceptions.HTTPError as e:
                logger.error(f"API HTTP xatolik ({method}): {e}")
                try:
                    return response.json()
                except:
                    return {"ok": False, "description": str(e)}
            except requests.exceptions.ConnectionError as e:
                if attempt < retries:
                    wait_time = 0.5 * (attempt + 1)
                    logger.warning(f"API connection error ({method}), qayta urinilmoqda ({attempt + 1}/{retries + 1})...")
                    time.sleep(wait_time)
                else:
                    logger.error(f"API connection error ({method}): {e}")
                    return {"ok": False, "description": f"Connection error: {str(e)}"}
            except Exception as e:
                logger.error(f"API kutilmagan xatolik ({method}): {e}")
                return {"ok": False, "description": str(e)}

        return {"ok": False, "description": "Unknown error"}

    def send_message(self, chat_id, text, reply_markup=None):
        params = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML"
        }
        if reply_markup:
            params["reply_markup"] = json.dumps(reply_markup)

        result = self.call("sendMessage", params)

        # Xatolikni log qilish (call metodi allaqachon retry qiladi)
        if not result.get("ok"):
            logger.error(f"send_message xatolik: {result.get('description')} (chat_id: {chat_id})")

        return result

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

        # Retry mexanizmi (3 marta urinish)
        max_retries = 3
        for attempt in range(max_retries):
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
                logger.error(f"Firestore save error (urinish {attempt + 1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    time.sleep(1 * (attempt + 1))  # Exponential backoff: 1s, 2s, 3s
                else:
                    return False
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

    def get_recent_applications(self, limit=10, offset=0):
        if not self.db:
            return []
        try:
            # Firestore'da haqiqiy offset qimmat bo'lishi mumkin, 
            # lekin bu hajmdagi bot uchun limit(offset+limit) qilib keyin slice qilish yetarli
            query = self.db.collection("applications").order_by("timestamp", direction=firestore.Query.DESCENDING).limit(offset + limit)
            docs = query.stream()
            items = []
            for i, doc in enumerate(docs):
                if i < offset:
                    continue
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
            "uz_cyrl": [
                ["🏢 Бошқарув", "👨‍🏫 Ўқитувчи"],
                ["🧹 Тозалик ҳодими", "🛡 Хавфсизлик / Қўриқлаш"],
                ["💡 Бошқа лавозим"]
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
            "menu_about": {"uz": "🏫 Biz haqimizda", "uz_cyrl": "🏫 Биз ҳақимизда", "en": "🏫 About us", "ru": "🏫 О нас"},
            "menu_contact": {"uz": "💬 Biz bilan bog'lanish", "uz_cyrl": "💬 Биз билан боғланиш", "en": "💬 Contact us", "ru": "💬 Связаться"},
            "menu_location": {"uz": "📍 Manzilimiz", "uz_cyrl": "📍 Манзилимиз", "en": "📍 Our Location", "ru": "📍 Наш адрес"},
            "menu_jobs": {"uz": "💼 Bo'sh ish o'rinlari", "uz_cyrl": "💼 Бўш иш ўринлари", "en": "💼 Job vacancies", "ru": "💼 Вакансии"},
            "menu_lang": {"uz": "🌐 Tilni almashtirish", "uz_cyrl": "🌐 Тилни алмаштириш", "en": "🌐 Change language", "ru": "🌐 Сменить язык"},
            "back": {"uz": "⬅️ Orqaga", "uz_cyrl": "⬅️ Орқага", "en": "⬅️ Back", "ru": "⬅️ Назад"},
            "cancel": {"uz": "❌ Bekor qilish", "uz_cyrl": "❌ Бекор қилиш", "en": "❌ Cancel", "ru": "❌ Отмена"},
            "skip": {"uz": "O'tkazib yuborish", "uz_cyrl": "Ўтказиб юбориш", "en": "Skip", "ru": "Пропустить"},
            "send_contact": {"uz": "Kontaktni yuborish", "uz_cyrl": "Контактни юбориш", "en": "Send contact", "ru": "Отправить контакт"},
            "lang_uz": {"uz": "🇺🇿 Lotin", "uz_cyrl": "🇺🇿 Лотин", "en": "🇺🇿 Latin", "ru": "🇺🇿 Латиница"},
            "lang_uz_cyrl": {"uz": "🇺🇿 Kiril", "uz_cyrl": "🇺🇿 Кирил", "en": "🇺🇿 Cyrillic", "ru": "🇺🇿 Кириллица"},
            "lang_en": {"uz": "🇬🇧 ENG", "uz_cyrl": "🇬🇧 ENG", "en": "🇬🇧 ENG", "ru": "🇬🇧 ENG"},
            "lang_ru": {"uz": "🇷🇺 RUS", "uz_cyrl": "🇷🇺 RUS", "en": "🇷🇺 RUS", "ru": "🇷🇺 RUS"},
            "menu_admin": {"uz": "🔐 Admin", "uz_cyrl": "🔐 Админ", "en": "🔐 Admin", "ru": "🔐 Админ"},
            "admin_apps": {"uz": "📨 Arizalar", "uz_cyrl": "📨 Аризалар", "en": "📨 Applications", "ru": "📨 Заявки"},
            "admin_search": {"uz": "🔎 Lavozim bo'yicha qidirish", "uz_cyrl": "🔎 Лавозим бўйича қидириш", "en": "🔎 Search by position", "ru": "🔎 Поиск по должности"},
            "admin_stats": {"uz": "📊 Statistika (30 kun)", "uz_cyrl": "📊 Статистика (30 кун)", "en": "📊 Statistics (30 days)", "ru": "📊 Статистика (30 дней)"},
            "admin_back": {"uz": "⬅️ Orqaga", "uz_cyrl": "⬅️ Орқага", "en": "⬅️ Back", "ru": "⬅️ Назад"},
            "other_pos": {"uz": "💡 Boshqa lavozim", "uz_cyrl": "💡 Бошқа лавозим", "en": "💡 Other position", "ru": "💡 Другая должность"},
            
            # Messages
            "msg_welcome": {
                "uz": "<b>Assalomu alaykum!</b> 😊\n\nAl-Xorazmiy xususiy maktabiga xush kelibsiz! 🏫✨\n\nKerakli bo'limni tanlang: 👇",
                "uz_cyrl": "<b>Ассалому алайкум!</b> 😊\n\nАл-Хоразмий хусусий мактабига хуш келибсиз! 🏫✨\n\nКеракли бўлимни танланг: 👇",
                "en": "<b>Hello!</b> 😊\n\nWelcome to Al-Khwarizmi private school! 🏫✨\n\nPlease choose a section: 👇",
                "ru": "<b>Здравствуйте!</b> 😊\n\nДобро пожаловать в частную школу Аль-Хорезми! 🏫✨\n\nПожалуйста, выберите раздел: 👇"
            },
            "msg_about": {
                "uz": "<b>🏫 Al-Xorazmiy maktabi haqida:</b>\n\n"
                      "🎓 <b>Ta'lim:</b> 1-11 sinflar va maxsus tayyorlov kurslari.\n"
                      "🇺🇿 <b>Til:</b> O'zbek tili.\n"
                      "📚 <b>Chuqurlashtirilgan fanlar:</b> Ingliz tili, Matematika, IT va Arab tili.\n"
                      "🍱 <b>Oshxona:</b> 2 mahal bepul, halol va sifatli taomlar.\n"
                      "⏰ <b>Vaqt:</b> Darslar 8:30 – 17:00 (Shanba 14:00 gacha).\n"
                      "🗓 <b>Hafta:</b> 6 kunlik o'quv tizimi.",
                "uz_cyrl": "<b>🏫 Ал-Хоразмий мактаби ҳақида:</b>\n\n"
                           "🎓 <b>Таълим:</b> 1-11 синфлар ва махсус тайёрлов курслари.\n"
                           "🇺🇿 <b>Тил:</b> Ўзбек тили.\n"
                           "📚 <b>Чуқурлаштирилган фанлар:</b> Инглиз тили, Математика, IT ва Араб тили.\n"
                           "🍱 <b>Ошхона:</b> 2 маҳал бепул, ҳалол ва сифатли таомлар.\n"
                           "⏰ <b>Вақт:</b> Дарслар 8:30 – 17:00 (Шанба 14:00 гача).\n"
                           "🗓 <b>Ҳафта:</b> 6 кунлик ўқув тизими.",
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
                "uz_cyrl": "<b>📞 Биз билан боғланиш:</b>\n\n"
                           "☎️ <b>Телефон:</b> +998692100007\n"
                           "👨‍💻 <b>Telegram:</b> @Onlineeaz\n\n"
                           "Саволларингиз бўлса, қўнғироқ қилишингиз ёки adminга мурожаат қилишингиз мумкин. 😊",
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
                "uz_cyrl": "<b>📍 Манзилимиз:</b>\n\n"
                           "🇺🇿 Мактабимиз Наманган вилоятининг Наманган туманида жойлашган.\n\n"
                           "📍 <b>Мўлжал:</b>\n"
                           "Лола жаҳон бозоридан ўтганда, Қумқўрғон светофори олдида.\n\n"
                           "📍 <b>Локация:</b>\n"
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
                "uz_cyrl": "<b>Бўш иш ўринлари</b>\n\nИлтимос, исм ва фамилиянгизни киритинг:",
                "en": "<b>Job vacancies</b>\n\nPlease enter your first and last name:",
                "ru": "<b>Вакансии</b>\n\nПожалуйста, введите ваше имя и фамилию:"
            },
            "msg_ask_phone": {
                "uz": "Telefon raqamingizni yuboring (tugmani bosing):",
                "uz_cyrl": "Телефон рақамингизни юборинг (тугмани босинг):",
                "en": "Send your phone number (click the button):",
                "ru": "Отправьте свой номер телефона (нажмите кнопку):"
            },
            "msg_ask_position": {
                "uz": "Qaysi bo'limga topshirmoqchisiz? (Tanlang):",
                "uz_cyrl": "Қайси бўлимга топширмоқчисиз? (Танланг):",
                "en": "Which section are you applying for? (Choose):",
                "ru": "В какой раздел вы подаете заявку? (Выберите):"
            },
            "msg_ask_position_manual": {
                "uz": "Iltimos, mutaxassisligingiz yoki lavozim turini kiriting (Masalan: Matematika o'qituvchisi, Bosh buxgalter va h.k.):",
                "uz_cyrl": "Илтимос, мутахассислигингиз ёки лавозим турини киритинг (Масалан: Математика ўқитувчиси, Бош бухгалтер ва ҳ.к.):",
                "en": "Please enter your specialization or position type (Example: Math Teacher, Chief Accountant, etc.):",
                "ru": "Пожалуйста, введите вашу специализацию или тип должности (Например: Учитель математики, Главный бухгалтер и т. д.):"
            },
            "msg_ask_exp": {
                "uz": "Ish tajribangiz haqida qisqacha ma'lumot bering:",
                "uz_cyrl": "Иш тажрибангиз ҳақида қисқача маълумот беринг:",
                "en": "Provide brief information about your work experience:",
                "ru": "Кратко расскажите о своем опыте работы:"
            },
            "msg_ask_cv": {
                "uz": "Rezyume (PDF yoki Rasm) yuboring yoki 'O'tkazib yuborish' tugmasini bosing:",
                "uz_cyrl": "Резюме (PDF ёки Расм) юборинг ёки 'Ўтказиб юбориш' тугмасини босинг:",
                "en": "Send your resume (PDF or Image) or click 'Skip':",
                "ru": "Отправьте резюме (PDF или фото) или нажмите 'Пропустить':"
            },
            "msg_applied": {
                "uz": "✅ <b>Arizangiz HR bo'limiga yuborildi.</b> Siz bilan tez orada bog'lanamiz.",
                "uz_cyrl": "✅ <b>Аризангиз HR бўлимига юборилди.</b> Сиз билан тез орада боғланамиз.",
                "en": "✅ <b>Your application has been sent to the HR department.</b> We will contact you soon.",
                "ru": "✅ <b>Ваша заявка отправлена в отдел кадров.</b> Мы свяжемся с вами в ближайшее время."
            },
            "msg_canceled": {
                "uz": "Ariza topshirish bekor qilindi.",
                "uz_cyrl": "Ариза топшириш бекор қилинди.",
                "en": "Application canceled.",
                "ru": "Подача заявки отменена."
            },
            "msg_invalid_name": {
                "uz": "Iltimos, ism va familiyangizni to'liq yozing (Masalan: Ali Valiyev):",
                "uz_cyrl": "Илтимос, исм ва фамилиянгизни тўлиқ ёзинг (Масалан: Али Валиев):",
                "en": "Please write your full name (Example: Ali Valiyev):",
                "ru": "Пожалуйста, напишите свое полное имя (Например: Али Валиев):"
            },
            "msg_invalid_phone": {
                "uz": "Iltimos, telefon raqamingizni tugma orqali yuboring yoki yozing:",
                "uz_cyrl": "Илтимос, телефон рақамингизни тугма орқали юборинг ёки ёзинг:",
                "en": "Please send your phone number via button or type it:",
                "ru": "Пожалуйста, отправьте свой номер телефона через кнопку или напишите его:"
            },
            "msg_invalid_exp": {
                "uz": "Tajribangiz haqida batafsilroq yozing:",
                "uz_cyrl": "Тажрибангиз ҳақида батафсилроқ ёзинг:",
                "en": "Write more about your experience:",
                "ru": "Напишите подробнее о своем опыте:"
            },
            "msg_invalid_cv": {
                "uz": "Iltimos, fayl yuboring yoki tugmani bosing.",
                "uz_cyrl": "Илтимос, файл юборинг ёки тугмани босинг.",
                "en": "Please send a file or click the button.",
                "ru": "Пожалуйста, отправьте файл или нажмите кнопку."
            },
            "msg_select_lang": {
                "uz": "Tilni tanlang:",
                "uz_cyrl": "Тилни танланг:",
                "en": "Choose language:",
                "ru": "Выберите язык:"
            },
            "msg_lang_changed": {
                "uz": "✅ Til o'zgartirildi.",
                "uz_cyrl": "✅ Тил ўзгартирилди.",
                "en": "✅ Language changed.",
                "ru": "✅ Язык изменен."
            },
            "msg_choose_menu": {
                "uz": "Iltimos, pastdagi menyudan birini tanlang.",
                "uz_cyrl": "Илтимос, пастдаги менюдан бирини танланг.",
                "en": "Please choose from the menu below.",
                "ru": "Пожалуйста, выберите из меню ниже."
            },
            "admin_panel": {
                "uz": "Admin panel:",
                "uz_cyrl": "Админ панел:",
                "en": "Admin panel:",
                "ru": "Админ панель:"
            },
            "admin_search_ask": {
                "uz": "Lavozim nomini kiriting:",
                "uz_cyrl": "Лавозим номини киритинг:",
                "en": "Enter the position name:",
                "ru": "Введите название должности:"
            },
            "admin_no_results": {
                "uz": "Natija topilmadi.",
                "uz_cyrl": "Натижа топилмади.",
                "en": "No results found.",
                "ru": "Результатов не найдено."
            },
            "admin_no_apps": {
                "uz": "Hozircha arizalar topilmadi.",
                "uz_cyrl": "Ҳозирча аризалар топилмади.",
                "en": "No applications found yet.",
                "ru": "Заявок пока не найдено."
            },
            "admin_firebase_error": {
                "uz": "Firebase ulanmagan.",
                "uz_cyrl": "Firebase уланмаган.",
                "en": "Firebase not connected.",
                "ru": "Firebase не подключен."
            },
            "admin_app_details": {
                "uz": "<b>Ariza tafsiloti</b>",
                "uz_cyrl": "<b>Ариза тафсилоти</b>",
                "en": "<b>Application detail</b>",
                "ru": "<b>Детали заявки</b>"
            },
            "admin_stats_title": {
                "uz": "<b>Statistika (oxirgi {days} kun)</b>",
                "uz_cyrl": "<b>Статистика (охирги {days} кун)</b>",
                "en": "<b>Statistics (last {days} days)</b>",
                "ru": "<b>Статистика (за последние {days} дней)</b>"
            },
            "admin_total": {
                "uz": "Jami",
                "uz_cyrl": "Жами",
                "en": "Total",
                "ru": "Всего"
            },
            "admin_closed": {
                "uz": "Yopildi.",
                "uz_cyrl": "Ёпилди.",
                "en": "Closed.",
                "ru": "Закрыто."
            },
            "msg_stopped": {
                "uz": "👋 Bot to'xtatildi. Qaytadan boshlash uchun /start buyrug'ini yuboring.",
                "uz_cyrl": "👋 Бот тўхтатилди. Қайтадан бошлаш учун /start буйруғини юборинг.",
                "en": "👋 Bot stopped. Send /start to begin again.",
                "ru": "👋 Бот остановлен. Отправьте /start, чтобы начать снова."
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
                [{"text": self._label("lang_uz", lang)}, {"text": self._label("lang_uz_cyrl", lang)}],
                [{"text": self._label("lang_en", lang)}, {"text": self._label("lang_ru", lang)}],
                [{"text": self._label("back", lang)}],
            ],
            "resize_keyboard": True
        }

    def _welcome_lang_menu(self):
        """Birinchi marta bot ishga tushganda til tanlash menusi (creative)"""
        return {
            "keyboard": [
                [{"text": "🇺🇿 O'zbek (Lotin)"}],
                [{"text": "🇺🇿 Ўзбек (Кирил)"}],
                [{"text": "🇷🇺 Русский"}],
                [{"text": "🇬🇧 English"}],
            ],
            "resize_keyboard": True,
            "one_time_keyboard": True
        }

    def _admin_menu(self, lang="uz"):
        return {
            "keyboard": [
                [{"text": self._label("admin_apps", lang)}],
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
        # Callback query handling for pagination
        callback_query = update.get("callback_query")
        if callback_query:
            self._handle_callback(callback_query)
            return

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

            # Agar foydalanuvchi yangi bo'lsa (til tanlamagan), til tanlash menusini ko'rsatish
            if not lang:
                # Har uchala tilda til tanlash so'rovi (creative)
                welcome_msg = (
                    "🌟 <b>Al-Xorazmiy xususiy maktabi</b> 🏫\n\n"
                    "🌍 <i>Iltimos, tilni tanlang:</i>\n"
                    "🌍 <i>Пожалуйста, выберите язык:</i>\n"
                    "🌍 <i>Please select a language:</i>"
                )
                self.api.send_message(chat_id, welcome_msg, self._welcome_lang_menu())
                return

            # Agar til tanlangan bo'lsa, asosiy menyuni ko'rsatish
            self.api.send_message(chat_id, self._label("msg_welcome", lang), self._main_menu(lang, chat_id))
            return

        if text == "/stop":
            self.db.set_user_state(user_id, None)
            # Klaviaturani olib tashlash
            remove_kb = {"remove_keyboard": True}
            self.api.send_message(chat_id, self._label("msg_stopped", lang if lang else "uz"), remove_kb)
            return
        
        # Welcome lang menu'dan til tanlash (creative shaklda)
        if text in ["🇺🇿 O'zbek (Lotin)", "🇺🇿 Ўзбек (Кирил)", "🇷🇺 Русский", "🇬🇧 English"]:
            if text == "🇺🇿 O'zbek (Lotin)":
                new_lang = "uz"
            elif text == "🇺🇿 Ўзбек (Кирил)":
                new_lang = "uz_cyrl"
            elif text == "🇷🇺 Русский":
                new_lang = "ru"
            else:  # English
                new_lang = "en"

            self.db.set_user_lang(user_id, new_lang)
            # Til tanlangandan keyin xush kelibsiz xabarini ko'rsatish
            self.api.send_message(chat_id, self._label("msg_welcome", new_lang), self._main_menu(new_lang, chat_id))
            return

        action = self._action_from_text(text)

        if action == "menu_lang":
            self.api.send_message(chat_id, self._label("msg_select_lang", lang), self._lang_menu(lang))
            return

        if action in ["lang_uz", "lang_uz_cyrl", "lang_en", "lang_ru"]:
            if action == "lang_uz":
                new_lang = "uz"
            elif action == "lang_uz_cyrl":
                new_lang = "uz_cyrl"
            elif action == "lang_en":
                new_lang = "en"
            else:
                new_lang = "ru"
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
            if lang == "uz":
                msg = f"Siz <b>{text}</b> bo'limini tanladingiz.\n\nIltimos, endi aniq lavozim yoki mutaxassislikni yozing (Masalan: Matematika o'qituvchisi, Bosh buxgalter va h.k.):"
            elif lang == "uz_cyrl":
                msg = f"Сиз <b>{text}</b> бўлимини танладингиз.\n\nИлтимос, энди аниқ лавозим ёки мутахассисликни ёзинг (Масалан: Математика ўқитувчиси, Бош бухгалтер ва ҳ.к.):"
            elif lang == "en":
                msg = f"You selected the <b>{text}</b> section.\n\nPlease now enter the specific position or specialization (Example: Math Teacher, Chief Accountant, etc.):"
            elif lang == "ru":
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
            self._label("admin_apps", lang),
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

        if t == self._label("admin_apps", lang):
            self._send_recent_applications(chat_id, offset=0, lang=lang)
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

    def _handle_callback(self, cb):
        cb_id = cb.get("id")
        user_id = cb.get("from", {}).get("id")
        chat_id = cb.get("message", {}).get("chat", {}).get("id")
        msg_id = cb.get("message", {}).get("message_id")
        data = cb.get("data", "")
        lang = self.db.get_user_lang(user_id)

        # Answer callback to remove loading state
        self.api.call("answerCallbackQuery", {"callback_query_id": cb_id})

        if data.startswith("page_"):
            # Delete the navigation message to avoid clutter
            self.api.call("deleteMessage", {"chat_id": chat_id, "message_id": msg_id})
            
            offset = int(data.split("_")[1])
            self._send_recent_applications(chat_id, offset=offset, lang=lang)

    def _send_in_chunks(self, chat_id, text, reply_markup=None, max_len=3500, edit_msg_id=None):
        lines = (text or "").splitlines() or [""]
        buf = ""
        
        if edit_msg_id:
            params = {
                "chat_id": chat_id,
                "message_id": edit_msg_id,
                "text": text,
                "parse_mode": "HTML"
            }
            if reply_markup:
                params["reply_markup"] = json.dumps(reply_markup)
            self.api.call("editMessageText", params)
            return

        for line in lines:
            candidate = (buf + "\n" + line) if buf else line
            if len(candidate) > max_len and buf:
                self.api.send_message(chat_id, buf, reply_markup)
                buf = line
            else:
                buf = candidate
        if buf:
            self.api.send_message(chat_id, buf, reply_markup)

    def _send_recent_applications(self, chat_id, offset=0, limit=10, lang="uz", edit_msg_id=None):
        if not self.db.db:
            self.api.send_message(chat_id, self._label("admin_firebase_error", lang), self._admin_menu(lang))
            return
        
        items = self.db.get_recent_applications(limit=limit, offset=offset)
        if not items:
            if offset == 0:
                self.api.send_message(chat_id, self._label("admin_no_apps", lang), self._admin_menu(lang))
            else:
                # If no items on this page (e.g. deleted), go back
                self._send_recent_applications(chat_id, offset=max(0, offset-limit), limit=limit, lang=lang)
            return

        # Send header for the batch
        if offset == 0 and not edit_msg_id:
            self.api.send_message(chat_id, f"<b>{self._label('admin_apps', lang)}</b>", self._admin_menu(lang))

        # Send each application as a separate detailed message
        for i, item in enumerate(items, start=offset+1):
            self._send_single_application(chat_id, item, index=i, lang=lang)
            time.sleep(0.05) # Small delay to ensure order

        # Pagination navigation message
        kb = []
        nav_row = []
        if offset > 0:
            nav_row.append({"text": "⬅️ Oldingi", "callback_data": f"page_{max(0, offset-limit)}"})
        
        # Check if there might be more (simple heuristic: if we got 'limit' items, assume there's more)
        if len(items) == limit:
            nav_row.append({"text": "Keyingi ➡️", "callback_data": f"page_{offset+limit}"})
        
        if nav_row:
            kb.append(nav_row)
            markup = {"inline_keyboard": kb}
            self.api.send_message(chat_id, f"<i>Sahifa: {offset//limit + 1}</i>", markup)

    def _send_single_application(self, chat_id, item, index, lang="uz"):
        ts = self._fmt_ts(item.get("timestamp"))
        name = item.get("name") or "—"
        phone = item.get("phone") or "—"
        pos = item.get("position") or "—"
        exp = item.get("experience") or "—"
        cv_file_id = item.get("cv_file_id")
        cv_type = item.get("cv_type")
        
        clean_pos = pos.split(" ", 1)[-1] if " " in pos and any(e in pos for e in "🏢👨‍🏫🧹🛡💡") else pos

        # Format as requested in image
        caption = (
            f"{index}. 👤 {name}\n"
            f"   💼 {clean_pos}\n"
            f"   📞 {phone}\n"
            f"   📝 {exp}\n"
            f"   📅 {ts}"
        )

        if cv_file_id:
            method = "sendDocument" if cv_type == "doc" else "sendPhoto"
            param_key = "document" if cv_type == "doc" else "photo"
            self.api.call(method, {
                "chat_id": chat_id, 
                param_key: cv_file_id, 
                "caption": caption, 
                "parse_mode": "HTML"
            })
        else:
            self.api.send_message(chat_id, caption)

    def _send_applications_list(self, chat_id, items, title, lang="uz", edit_msg_id=None, reply_markup=None):
        # Used for search results - send as detailed messages too
        self.api.send_message(chat_id, f"<b>{title}</b>", self._admin_menu(lang))
        for i, item in enumerate(items, start=1):
            self._send_single_application(chat_id, item, index=i, lang=lang)
            time.sleep(0.05)

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
        pos = item.get("position") or "—"
        exp = item.get("experience") or "—"
        cv_file_id = item.get("cv_file_id")
        cv_type = item.get("cv_type")
        
        # Emojilarni tozalash
        clean_pos = pos.split(" ", 1)[-1] if " " in pos and any(e in pos for e in "🏢👨‍🏫🧹🛡💡") else pos

        # Localized labels
        header = "� Arizachi ma'lumotlari" if lang == "uz" else ("� Applicant Details" if lang == "en" else "� Данные заявителя")
        nomzod_lbl = "Nomzod" if lang == "uz" else ("Candidate" if lang == "en" else "Кандидат")
        tel_lbl = "Telefon" if lang == "uz" else ("Phone" if lang == "en" else "Телефон")
        lavozim_lbl = "Lavozim" if lang == "uz" else ("Position" if lang == "en" else "Должность")
        tajriba_lbl = "Tajriba" if lang == "uz" else ("Experience" if lang == "en" else "Опыт")
        sana_lbl = "Sana" if lang == "uz" else ("Date" if lang == "en" else "Дата")

        report = (
            f"<b>{header}</b>\n"
            f"⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n\n"
            f"👤 <b>{nomzod_lbl}:</b> {name}\n"
            f"📞 <b>{tel_lbl}:</b> {phone}\n"
            f"💼 <b>{lavozim_lbl}:</b> {clean_pos}\n"
            f"📝 <b>{tajriba_lbl}:</b> {exp}\n"
            f"🕒 <b>{sana_lbl}:</b> {ts}"
        )

        if cv_file_id:
            method = "sendDocument" if cv_type == "doc" else "sendPhoto"
            param_key = "document" if cv_type == "doc" else "photo"
            self.api.call(method, {
                "chat_id": chat_id, 
                param_key: cv_file_id, 
                "caption": report, 
                "parse_mode": "HTML",
                "reply_markup": json.dumps(self._admin_menu(lang))
            })
        else:
            self.api.send_message(chat_id, report, self._admin_menu(lang))

    def _send_stats(self, chat_id, days=30, lang="uz"):
        if not self.db.db:
            self.api.send_message(chat_id, self._label("admin_firebase_error", lang), self._admin_menu(lang))
            return
        
        # Loading message
        wait_msg = "📊 Ma'lumotlar tahlil qilinmoqda, iltimos kuting..." if lang == "uz" else \
                   ("📊 Analyzing data, please wait..." if lang == "en" else "📊 Данные анализируются, пожалуйста, подождите...")
        self.api.send_message(chat_id, wait_msg)
        
        stats = self.db.get_position_stats(days=days, limit=1000)
        total = stats.pop("_total", 0) if stats else 0
        
        if not stats or total == 0:
            no_data = "❌ Ushbu davr uchun ma'lumotlar mavjud emas." if lang == "uz" else \
                      ("❌ No data available for this period." if lang == "en" else "❌ Нет данных за этот период.")
            self.api.send_message(chat_id, no_data, self._admin_menu(lang))
            return
            
        sorted_items = sorted(stats.items(), key=lambda x: x[1], reverse=True)
        
        # Headers based on language
        title = f"<b>📊 {days} kunlik tahliliy hisobot</b>" if lang == "uz" else \
                (f"<b>📊 {days}-day Analytical Report</b>" if lang == "en" else f"<b>📊 Аналитический отчет за {days} дней</b>")
        
        summary_lbl = "📈 Umumiy ko'rsatkichlar" if lang == "uz" else ("📈 General Indicators" if lang == "en" else "📈 Общие показатели")
        total_apps_lbl = "Jami arizalar" if lang == "uz" else ("Total applications" if lang == "en" else "Всего заявок")
        avg_lbl = "Kunlik o'rtacha" if lang == "uz" else ("Daily average" if lang == "en" else "Среднесуточное")
        positions_lbl = "💼 Lavozimlar kesimida tahlil" if lang == "uz" else ("💼 Analysis by Positions" if lang == "en" else "💼 Анализ по должностям")
        
        avg_daily = round(total / days, 1)
        
        report = [
            title,
            "⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯",
            f"<b>{summary_lbl}:</b>",
            f"🔹 {total_apps_lbl}: <b>{total} ta</b>",
            f"🔹 {avg_lbl}: <b>{avg_daily} ta/kun</b>",
            "",
            f"<b>{positions_lbl}:</b>"
        ]
        
        # Progress bar helper
        def get_progress_bar(percent):
            filled_length = int(10 * percent / 100)
            bar = "🟢" * filled_length + "⚪" * (10 - filled_length)
            return bar

        for position, count in sorted_items:
            percent = (count / total) * 100
            bar = get_progress_bar(percent)
            # Emojilarni tozalash (agar bo'lsa)
            clean_pos = position.split(" ", 1)[-1] if " " in position and any(e in position for e in "🏢👨‍🏫🧹🛡💡") else position
            report.append(f"\n<b>{clean_pos}</b>")
            report.append(f"{bar}  {count} ta ({percent:.1f}%)")
            
        report.append("\n⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯")
        footer = "📅 Hisobot vaqti: " + datetime.now().strftime("%d.%m.%Y %H:%M")
        report.append(f"<i>{footer}</i>")
        
        self._send_in_chunks(chat_id, "\n".join(report), self._admin_menu(lang))

    def _clean_emoji(self, text):
        """Emojilarni olib tashlash (agar bor bo'lsa)"""
        if not text:
            return text
        # Oddiy emojilarni olib tashlash
        emoji_patterns = ["🏢", "👨‍🏫", "🧹", "🛡", "💡"]
        clean_text = text
        for emoji in emoji_patterns:
            clean_text = clean_text.replace(emoji, "")
        # Bosh va oxiridagi bo'sh joylarni olib tashlash
        return clean_text.strip()

    def _is_valid_name(self, text):
        if not text: return False
        parts = text.strip().split()
        return len(parts) >= 2 and len(text) >= 5

    def _is_valid_phone(self, text):
        if not text: return False
        digits = "".join(filter(str.isdigit, text))
        # O'zbekiston telefon raqamlari uchun minimum 9 raqam (masalan: 901234567)
        # Xalqaro format uchun 12 gacha raqam (masalan: 998901234567)
        return 9 <= len(digits) <= 15

    def _send_to_hr(self, user_id, data, file_id, f_type, saved_to_firebase):
        if not Config.HR_CHAT_ID:
            logger.warning("HR_CHAT_ID sozlanmagan, ariza yuborilmadi")
            return

        report = (
            f"<b>Yangi ariza</b>\n\n"
            f"👤 Nomzod: {data.get('name')}\n"
            f"📞 Tel: {data.get('phone')}\n"
            f"💼 Lavozim: {data.get('position')}\n"
            f"📝 Tajriba: {data.get('exp')}"
        )

        try:
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
        except Exception as e:
            logger.error(f"HR ga yuborishda xatolik: {e}")

def run_health_check():
    """Render uchun health check endpointini ishga tushirish"""
    app = Flask(__name__)

    @app.route('/')
    def health_check():
        return "Bot is running!", 200

    port = int(os.environ.get("PORT", 10000))
    # Flask loglarini kamaytirish
    import logging
    log = logging.getLogger('werkzeug')
    log.setLevel(logging.ERROR)
    
    app.run(host='0.0.0.0', port=port)

def run_polling():
    if not Config.validate():
        sys.exit(1)

    # Health check serverini alohida thread'da ishga tushirish
    health_thread = threading.Thread(target=run_health_check, daemon=True)
    health_thread.start()
    logger.info("Health check serveri ishga tushdi.")

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
        {"command": "stop", "description": "Botni to'xtatish"},
        {"command": "admin", "description": "Admin panel (faqat adminlar)"}
    ]
    result = api.call("setMyCommands", {"commands": commands})
    if result.get("ok"):
        logger.info("Bot komandalari o'rnatildi")
    else:
        logger.warning(f"Bot komandalari o'rnatilmadi: {result.get('description')}")

    # Bot description va short description o'rnatish
    description = (
        "🏫 Al-Xorazmiy xususiy maktabiga xush kelibsiz!\n\n"
        "✨ Bu bot orqali siz:\n"
        "📚 Maktab haqida to'liq ma'lumot olishingiz\n"
        "📍 Manzil va bog'lanish ma'lumotlarini ko'rishingiz\n"
        "💼 Bo'sh ish o'rinlariga ariza topshirishingiz mumkin\n\n"
        "🌍 4 tilda: O'zbek (Lotin), O'zbek (Kiril), English, Русский\n\n"
        "Anketani to'ldirish uchun START tugmasini bosing! 👇"
    )

    short_description = (
        "Al-Xorazmiy maktabiga ishga qabul qilish boti. "
        "Maktab haqida ma'lumot va bo'sh ish o'rinlariga ariza topshirish."
    )

    api.call("setMyDescription", {"description": description})
    api.call("setMyShortDescription", {"description": short_description})
    logger.info("Bot description o'rnatildi")

    # Bot profil rasmini o'rnatish (logo)
    import os
    logo_files = ["logo.png", "logo.jpg", "logo.jpeg", "school_logo.png", "school_logo.jpg"]
    logo_path = None

    for logo_file in logo_files:
        if os.path.exists(logo_file):
            logo_path = logo_file
            break

    if logo_path:
        try:
            with open(logo_path, "rb") as photo:
                files = {"photo": photo}
                result = api.call("setMyProfilePhoto", files=files)
                if result.get("ok"):
                    logger.info(f"Bot profil rasmi o'rnatildi: {logo_path}")
                else:
                    logger.warning(f"Bot profil rasmi o'rnatilmadi: {result.get('description')}")
        except Exception as e:
            logger.error(f"Bot profil rasmini o'rnatishda xatolik: {e}")
    else:
        logger.info("Logo fayli topilmadi. Bot profil rasmini o'rnatish uchun logo.png yoki logo.jpg faylini qo'shing.")

    # Kichik botlar uchun 5 worker yetarli
    executor = ThreadPoolExecutor(max_workers=5)
    retry_count = 0
    shutdown_flag = threading.Event()

    # Graceful shutdown handler
    def shutdown_handler(signum, frame):
        logger.info("To'xtatish signali qabul qilindi, bot to'xtatilmoqda...")
        shutdown_flag.set()

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    try:
        while not shutdown_flag.is_set():
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
    finally:
        logger.info("Bot to'xtatilmoqda, barcha threadlar yakunlanmoqda...")
        executor.shutdown(wait=True, cancel_futures=False)
        logger.info("Barcha threadlar yakunlandi.")

if __name__ == "__main__":
    try:
        run_polling()
    except KeyboardInterrupt:
        logger.info("Bot to'xtatildi.")
    except Exception as e:
        logger.critical(f"Bot kutilmaganda to'xtadi: {e}")
