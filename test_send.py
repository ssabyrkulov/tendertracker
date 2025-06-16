from telegram import Bot

TELEGRAM_TOKEN = '7399516902:AAEShFpb9hs2dHVrSsHD5T7yQ74OYRhqX2Q'
CHAT_IDS = [817346567]  # только новому другу

bot = Bot(token=TELEGRAM_TOKEN)

for chat_id in CHAT_IDS:
    try:
        bot.send_message(chat_id=chat_id, text="👋 Проверка связи: если ты видишь это — всё работает!")
        print(f"✅ Успешно отправлено: {chat_id}")
    except Exception as e:
        print(f"❌ Ошибка при отправке {chat_id}: {e}")
