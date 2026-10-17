"""
بوت تيليجرام لمتابعة مباريات برشلونة والاتحاد
- يرسل تنبيه قبل كل مباراة بيوم مع: اسم الخصم، وقت المباراة، صورة شعار الخصم
- يدعم عرض مباريات اليوم حتى لو كانت منتهية مع النتيجة النهائية والهدفين والفائز
- يدعم عرض المباريات المباشرة مع النتيجة الحالية
"""

import asyncio
import logging
import requests
from datetime import datetime, timedelta
from io import BytesIO

import pytz
from telegram import Update, Bot
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

from config import (
    TELEGRAM_BOT_TOKEN,
    FOOTBALL_API_KEY,
    TEAMS,
    TIMEZONE,
    CHECK_HOUR,
    CHECK_MINUTE,
)

# ─── إعداد اللوق ──────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── تخزين chat_ids المشتركين ──────────────────
SUBSCRIBERS_FILE = "subscribers.txt"


def load_subscribers() -> set:
    """تحميل قائمة المشتركين من الملف."""
    try:
        with open(SUBSCRIBERS_FILE, "r", encoding="utf-8") as f:
            return {line.strip() for line in f if line.strip()}
    except FileNotFoundError:
        return set()


def save_subscriber(chat_id: str):
    """حفظ مشترك جديد."""
    subs = load_subscribers()
    subs.add(chat_id)
    with open(SUBSCRIBERS_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(subs))


# ══════════════════════════════════════════════════
#  جلب المباريات من API-Football
# ══════════════════════════════════════════════════

def fetch_matches_by_date(date_str: str) -> list[tuple[dict, str, dict]]:
    """
    جلب كل مباريات تاريخ محدد وفلترتها محلياً للفرق المتابعة (برشلونة والاتحاد).
    هذه الطريقة تضمن التوافق مع الخطة المجانية Free Plan وتوفر عدد الطلبات (طلب واحد باليوم).
    يرجع قائمة من الأزواج: (fixture_dict, team_arabic_name, team_info_dict)
    """
    url = "https://v3.football.api-sports.io/fixtures"
    headers = {"x-apisports-key": FOOTBALL_API_KEY}
    params = {"date": date_str}

    try:
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        if data.get("errors"):
            logger.error(f"API errors: {data['errors']}")
            return []

        all_fixtures = data.get("response", [])
        tracked_ids = {info["id"]: (name, info) for name, info in TEAMS.items()}

        results = []
        for fixture in all_fixtures:
            home_id = fixture["teams"]["home"]["id"]
            away_id = fixture["teams"]["away"]["id"]

            if home_id in tracked_ids:
                team_ar, team_info = tracked_ids[home_id]
                results.append((fixture, team_ar, team_info))
            elif away_id in tracked_ids:
                team_ar, team_info = tracked_ids[away_id]
                results.append((fixture, team_ar, team_info))

        return results

    except requests.RequestException as e:
        logger.error(f"خطأ في الاتصال بـ API: {e}")
        return []


# ══════════════════════════════════════════════════
#  تنسيق رسالة المباراة
# ══════════════════════════════════════════════════

def format_match_message(
    match: dict,
    team_ar: str,
    team_info: dict,
    is_today: bool = False,
) -> tuple[str, str]:
    """
    تنسيق بيانات المباراة لرسالة تيليجرام.
    يدعم المباريات المنتهية، الجارية (مباشر)، أو القادمة.
    يرجع (نص_الرسالة, رابط_شعار_الخصم).
    """
    fixture = match["fixture"]
    teams = match["teams"]
    league = match["league"]
    goals = match.get("goals", {})
    status = fixture.get("status", {})
    status_short = status.get("short", "")

    home = teams["home"]
    away = teams["away"]

    # تحديد الخصم والملعب
    if home["id"] == team_info["id"]:
        opponent = away
        venue = "🏠 في الديار"
        team_goals = goals.get("home")
        opp_goals = goals.get("away")
    else:
        opponent = home
        venue = "✈️ خارج الديار"
        team_goals = goals.get("away")
        opp_goals = goals.get("home")

    # تحويل وقت المباراة للتوقيت المحلي
    tz = pytz.timezone(TIMEZONE)
    match_utc = datetime.fromisoformat(fixture["date"].replace("Z", "+00:00"))
    match_local = match_utc.astimezone(tz)
    time_str = match_local.strftime("%I:%M %p").lstrip("0")
    date_display = match_local.strftime("%Y/%m/%d")

    # بناء نص النتيجة والحالة
    if status_short in ["FT", "AET", "PEN"]:
        header = f"🏁 نتيجة مباراة {team_ar} (انتهت)"
        score_text = (
            f"⚽ النتيجة النهائية:\n"
            f"{home['name']} {goals.get('home', 0)} - {goals.get('away', 0)} {away['name']}\n"
        )
        if team_goals is not None and opp_goals is not None:
            if team_goals > opp_goals:
                score_text += f"🎉 فوز مستحق لـ {team_ar}! 👏🔥\n\n"
            elif team_goals < opp_goals:
                score_text += f"💔 خسارة {team_ar}\n\n"
            else:
                score_text += f"🤝 تعادل\n\n"
    elif status_short in ["1H", "HT", "2H", "ET", "BT", "P", "LIVE"]:
        header = f"🔴 مباراة {team_ar} (مباشر الآن!)"
        elapsed = status.get("elapsed", "")
        elapsed_str = f"⏱️ الدقيقة: {elapsed}'\n" if elapsed else ""
        score_text = (
            f"{elapsed_str}"
            f"⚽ النتيجة الحالية:\n"
            f"{home['name']} {goals.get('home', 0)} - {goals.get('away', 0)} {away['name']}\n\n"
        )
    elif status_short in ["PST", "CANC", "ABD"]:
        header = f"⚠️ مباراة {team_ar} (تأجلت / ألغيت)"
        score_text = f"الحالة: {status.get('long', 'تأجلت')}\n\n"
    else:
        day_label = "اليوم 📅" if is_today else "بكره 📅"
        header = f"⚽ مباراة {day_label}"
        score_text = ""

    # بناء الرسالة النهائية
    msg = (
        f"{'━' * 25}\n"
        f"{header}\n"
        f"{'━' * 25}\n\n"
        f"{team_info['emoji']} {team_ar}\n"
        f"🆚 {opponent['name']}\n\n"
        f"{score_text}"
        f"🏆 البطولة: {league['name']}\n"
        f"📍 {venue}\n"
        f"📅 التاريخ: {date_display}\n"
        f"⏰ الوقت: {time_str}\n"
        f"{'━' * 25}"
    )

    photo_url = opponent.get("logo", "")
    return msg, photo_url


# ══════════════════════════════════════════════════
#  إرسال الإشعارات
# ══════════════════════════════════════════════════

async def send_notification_to_chat(
    bot: Bot, chat_id: str, message: str, photo_url: str
):
    """إرسال إشعار مباراة مع صورة الخصم لمستخدم محدد."""
    try:
        if photo_url:
            try:
                img_resp = requests.get(photo_url, timeout=10)
                img_resp.raise_for_status()
                photo_bytes = BytesIO(img_resp.content)
                photo_bytes.name = "opponent_logo.png"

                await bot.send_photo(
                    chat_id=chat_id,
                    photo=photo_bytes,
                    caption=message,
                )
                return
            except Exception as img_err:
                logger.warning(f"فشل تحميل الصورة: {img_err}")

        # في حال عدم وجود صورة أو تعذر تحميلها، إرسال كنص
        await bot.send_message(chat_id=chat_id, text=message)

    except Exception as e:
        logger.error(f"فشل إرسال إشعار لـ {chat_id}: {e}")


# ══════════════════════════════════════════════════
#  أوامر البوت
# ══════════════════════════════════════════════════

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """أمر /start – تسجيل المستخدم وإرسال رسالة تترحيب."""
    chat_id = str(update.effective_chat.id)
    save_subscriber(chat_id)

    await update.message.reply_text(
        "⚽ أهلاً وسهلاً بك في بوت متابعة المباريات!\n\n"
        "تم تسجيلك بنجاح ✅\n\n"
        "🔔 يرسل البوت تلقائياً كل يوم الساعة 9 مساءً تنبيهاً إذا كانت هناك مباراة غداً لـ:\n"
        "🔵🔴 برشلونة\n"
        "🟡⚫ الاتحاد\n\n"
        "📌 الأوامر المتاحة:\n"
        "/today - مباريات اليوم ونتائجها\n"
        "/check - فحص مباريات الغد\n"
        "/help - معلومات المساعدة"
    )
    logger.info(f"مشترك جديد: {chat_id}")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """أمر /help – عرض المساعدة."""
    await update.message.reply_text(
        "📖 كيفية عمل البوت:\n\n"
        "1️⃣ يفحص البوت يومياً الساعة 9:00 مساءً.\n"
        "2️⃣ إذا كانت هناك مباراة غداً لبرشلونة أو الاتحاد، يرسل لك إشعاراً يحتوي على:\n"
        "   - اسم الخصم وشعاره 🖼️\n"
        "   - توقيت المباراة والبطولة ⏰\n"
        "3️⃣ أمر /today يعرض مباريات اليوم (سواء كانت قادمة أو جارية مباشرة أو منتهية مع النتيجة).\n"
        "4️⃣ أمر /check يفحص مباريات الغد فوراً."
    )


async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """أمر /check – فحص يدوي لمباريات الغد."""
    chat_id = str(update.effective_chat.id)
    save_subscriber(chat_id)

    await update.message.reply_text("🔍 جاري البحث عن مباريات غداً...")

    tz = pytz.timezone(TIMEZONE)
    tomorrow = (datetime.now(tz) + timedelta(days=1)).strftime("%Y-%m-%d")

    matches = fetch_matches_by_date(tomorrow)

    if matches:
        for match, team_ar, team_info in matches:
            msg, photo_url = format_match_message(match, team_ar, team_info, is_today=False)
            await send_notification_to_chat(context.bot, chat_id, msg, photo_url)
    else:
        await update.message.reply_text("😴 لا توجد مباريات غداً لبرشلونة أو الاتحاد.")


async def today_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """أمر /today – فحص مباريات اليوم ونتائجها."""
    chat_id = str(update.effective_chat.id)
    save_subscriber(chat_id)

    await update.message.reply_text("🔍 جاري البحث عن مباريات اليوم...")

    tz = pytz.timezone(TIMEZONE)
    today = datetime.now(tz).strftime("%Y-%m-%d")

    matches = fetch_matches_by_date(today)

    if matches:
        for match, team_ar, team_info in matches:
            msg, photo_url = format_match_message(match, team_ar, team_info, is_today=True)
            await send_notification_to_chat(context.bot, chat_id, msg, photo_url)
    else:
        await update.message.reply_text("😴 لا توجد مباريات اليوم لبرشلونة أو الاتحاد.")


# ══════════════════════════════════════════════════
#  الجدولة التلقائية
# ══════════════════════════════════════════════════

async def post_init(application: Application):
    """بعد تشغيل البوت – إعداد الجدولة التلقائية."""
    job_queue = application.job_queue

    tz = pytz.timezone(TIMEZONE)
    now = datetime.now(tz)
    first_run = now.replace(hour=CHECK_HOUR, minute=CHECK_MINUTE, second=0, microsecond=0)
    if first_run <= now:
        first_run += timedelta(days=1)

    job_queue.run_repeating(
        scheduled_check_job,
        interval=timedelta(days=1),
        first=first_run,
        name="daily_match_check",
    )
    logger.info(f"📅 تم جدولة الفحص اليومي الساعة {CHECK_HOUR}:{CHECK_MINUTE:02d}")
    logger.info("✅ البوت شغال وجاهز لاستقبال الأوامر!")


async def scheduled_check_job(context: ContextTypes.DEFAULT_TYPE):
    """وظيفة الجدولة التلقائية – فحص مباريات الغد وإرسالها للمشتركين."""
    logger.info("⏰ بدء الفحص التلقائي لمباريات غداً...")

    tz = pytz.timezone(TIMEZONE)
    tomorrow = (datetime.now(tz) + timedelta(days=1)).strftime("%Y-%m-%d")
    subscribers = load_subscribers()

    if not subscribers:
        logger.info("لا يوجد مشتركون مسجلون حالياً.")
        return

    matches = fetch_matches_by_date(tomorrow)

    if matches:
        for match, team_ar, team_info in matches:
            msg, photo_url = format_match_message(match, team_ar, team_info, is_today=False)
            for chat_id in subscribers:
                await send_notification_to_chat(context.bot, chat_id, msg, photo_url)
                logger.info(f"✅ تم إرسال إشعار {team_ar} لـ {chat_id}")

    logger.info("✅ انتهى الفحص التلقائي.")


import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler


class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write("⚽ Match Bot is Running!".encode("utf-8"))

    def log_message(self, format, *args):
        pass  # منع ملء اللوق بالـ health checks


def start_health_server():
    """تشغيل خادم ويب خفيف إذا كانت المنصة السحابية تتطلب PORT (مثل Render / Koyeb)."""
    port_str = os.environ.get("PORT")
    if port_str:
        try:
            port = int(port_str)
            server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            logger.info(f"🌐 خادم الـ Health Check يعمل على المنفذ {port}")
        except Exception as e:
            logger.warning(f"تعذر تشغيل خادم الـ Health Check: {e}")


def main():
    """نقطة البداية – تشغيل البوت."""
    logger.info("🚀 جاري تشغيل البوت...")

    # تشغيل خادم الويب السحابي إذا وُجد PORT
    start_health_server()

    # Python 3.14+: ضمان وجود event loop
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("check", check_command))
    app.add_handler(CommandHandler("today", today_command))

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
