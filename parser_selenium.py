import time
import os
import json
import asyncio
from datetime import datetime
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
from telegram import Bot
from telegram.constants import ParseMode

# === НАСТРОЙКИ ===
URL = 'https://zakupki.okmot.kg/popp/view/order/list.xhtml'
TELEGRAM_TOKEN = '7399516902:AAEShFpb9hs2dHVrSsHD5T7yQ74OYRhqX2Q'
CHAT_IDS = [377568546, 144731354, 817346567]
CHECK_INTERVAL = 180  # 3 минуты
SEEN_FILE = 'seen_tenders.json'

LOG_FILE = "log.txt"
ERROR_LOG_FILE = "errors.log"

# === ЛОГИРОВАНИЕ ===
def log(msg):
    timestamp = datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")
    full_msg = f"{timestamp} {msg}"
    print(full_msg)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(full_msg + "\n")

def log_error(msg):
    timestamp = datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")
    full_msg = f"{timestamp} {msg}"
    print(full_msg)
    with open(ERROR_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(full_msg + "\n")

# === ЗАГРУЗКА ID ПРОСМОТРЕННЫХ ===
def load_seen_ids():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, 'r') as f:
            return set(json.load(f))
    return set()

def save_seen_ids(seen_ids):
    with open(SEEN_FILE, 'w') as f:
        json.dump(list(seen_ids), f)

# === ПЕРЕВОД ТИПОВ ЗАКУПОК ===
def translate_type(type_text):
    t = type_text.lower()
    if t == "goods":
        return "Товары"
    if t == "services":
        return "Услуги"
    if t == "work":
        return "Работы"
    return type_text

# === УСТОЙЧИВЫЙ ПОИСК СТРОК ТЕНДЕРОВ ===
def find_tender_rows(driver):
    """
    Возвращает список <tr> с данными тендеров.
    Пробует несколько вариантов, чтобы не отваливаться при мелких изменениях верстки.
    """
    XPATHS = [
        # Самый надёжный вариант — чёт/нечёт
        "//tr[(contains(@class,'ui-datatable-odd') or contains(@class,'ui-datatable-even')) and not(contains(@class,'ui-datatable-empty-message'))]",
        # Иногда строка без odd/even, но с widget-content
        "//tbody/tr[contains(@class,'ui-widget-content') and not(contains(@class,'ui-datatable-empty-message'))]",
        # Через tbody с динамическим id
        "//tbody[starts-with(@id,'j_idt') and contains(@id,':table_data')]/tr[not(contains(@class,'ui-datatable-empty-message'))]"
    ]
    for xp in XPATHS:
        els = driver.find_elements(By.XPATH, xp)
        if els:
            return els
    return []

# === ОСНОВНАЯ ЛОГИКА ===
async def check_tenders():
    log("🔍 Открываем браузер...")

    seen_ids = load_seen_ids()
    new_seen_ids = set(seen_ids)

    options = webdriver.ChromeOptions()
    options.add_argument('--headless=new')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.add_argument('--disable-gpu')
    options.add_argument('--window-size=1920x1080')

    driver = webdriver.Chrome(service=Service(), options=options)
    driver.get(URL)

    sent_count = 0
    skipped_count = 0

    try:
        # === ЖДЁМ ИМЕННО СТРОКИ, А НЕ ПРОСТО ТАБЛИЦУ ===
        WebDriverWait(driver, 30).until(lambda d: len(find_tender_rows(d)) > 0)

        # Пытаемся принудительно выбрать 10 строк на страницу (если есть селектор)
        try:
            select = Select(driver.find_element(By.XPATH, "//select[contains(@class, 'ui-paginator-rpp-options')]"))
            # Если уже 10 — select_by_value('10') не навредит; если нет — переключит
            select.select_by_value("10")
            time.sleep(1.5)
        except Exception as e:
            log_error(f"⚠️ Не удалось выбрать '10 строк на странице': {e}")

        rows = find_tender_rows(driver)
        log(f"🔎 Найдено строк: {len(rows)}")

        # Отладочный дамп при нуле
        if not rows:
            driver.save_screenshot("debug.png")
            with open("page.html", "w", encoding="utf-8") as f:
                f.write(driver.page_source)
            log_error("⚠️ rows == 0, сохранены debug.png и page.html для анализа.")
            # Смысла дальше парсить нет
            return

        bot = Bot(token=TELEGRAM_TOKEN)

        for row in rows:
            cells = row.find_elements(By.TAG_NAME, "td")
            if len(cells) < 9:
                continue

            # ID
            tender_id = cells[0].text.strip().split("\n")[-1]

            # Организация
            org_lines = cells[1].text.strip().split('\n')
            organization = org_lines[-1] if len(org_lines) > 1 else (org_lines[0] if org_lines else "")

            # Тип
            try:
                tender_type_span = cells[2].find_element(By.TAG_NAME, "span").text.strip()
                tender_type_text = cells[2].text.replace(tender_type_span, "").strip()
            except Exception:
                tender_type_text = "Unknown"

            # Сумма
            try:
                # Часто внутри 2 строки: валюта/лейбл и цифра
                raw_lines = cells[6].text.strip().split('\n')
                if len(raw_lines) >= 2:
                    amount_line = raw_lines[1]
                else:
                    # fallback — берём всё, что есть
                    amount_line = cells[6].text.strip()
                amount_line = amount_line.replace(',', '').replace(' ', '')
                # Отрезаем возможные копейки (.00)
                amount = int(float(amount_line.split('.')[0])) if amount_line else 0
            except Exception:
                amount = 0
                log_error(f"⚠️ Ошибка парсинга суммы у ID {tender_id}: raw={cells[6].text.strip()}")

            # Название
            tender_name = cells[3].text.strip().replace('\n', ' ')

            # Дедлайн (обычно в 9-м столбце)
            deadline_lines = cells[8].text.strip().split('\n')
            deadline = deadline_lines[-1] if len(deadline_lines) > 1 else (deadline_lines[0] if deadline_lines else "")

            log(f"\n📝 Проверяем тендер:\n🆔 ID: {tender_id}\n📌 Название: {tender_name}\n📛 Организация: {organization}\n📦 Тип: {tender_type_text}\n💰 Сумма: {amount:,} сом\n🗓 Дедлайн: {deadline}")

            # Фильтры
            if tender_id in seen_ids:
                log("⏩ Пропущен: уже отправляли")
                skipped_count += 1
                continue

            if tender_type_text.lower() != 'goods':
                log("⛔ Пропущен: не 'товары'")
                skipped_count += 1
                continue

            if amount < 2000000:
                log("⛔ Пропущен: сумма < 2 млн")
                skipped_count += 1
                continue

            # Сообщение
            message = (
                f"📩 Найден тендер:\n"
                f"📌 Название: {tender_name}\n"
                f"📛 Организация: {organization}\n"
                f"📦 Тип: {translate_type(tender_type_text)}\n"
                f"💰 Сумма: {amount:,} сом\n"
                f"🗓 Срок подачи: {deadline}\n"
                f"🔗 https://zakupki.okmot.kg/popp/view/order/view.xhtml?id={tender_id}"
            )

            # Отправка
            try:
                for chat_id in CHAT_IDS:
                    await bot.send_message(chat_id=chat_id, text=message, parse_mode=ParseMode.HTML)
                log(f"📩 Отправлено: {tender_id}")
                sent_count += 1
            except Exception as e:
                log_error(f"⚠️ Ошибка отправки Telegram: {e}")

            new_seen_ids.add(tender_id)

    except TimeoutException:
        log_error("❌ Таблица не загрузилась вовремя.")
        save_seen_ids(new_seen_ids)
    except Exception as e:
        if "invalid session" in str(e).lower() or "disconnected" in str(e).lower():
            log_error("⚠️ Браузер отключился (спящий режим / сессия потеряна). Пропускаем цикл.")
            driver.quit()
            return
        log_error(f"❌ Ошибка в Selenium: {e}")
        save_seen_ids(new_seen_ids)
    finally:
        driver.quit()
        save_seen_ids(new_seen_ids)
        log(f"📊 Итого: отправлено: {sent_count}, пропущено: {skipped_count}")
        log(f"⏳ Ожидание {CHECK_INTERVAL // 60} минут...")

if __name__ == "__main__":
    while True:
        asyncio.run(check_tenders())
        time.sleep(CHECK_INTERVAL)
