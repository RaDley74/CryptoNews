import warnings
warnings.simplefilter(action='ignore', category=FutureWarning)

import sys
import io
import time
import sqlite3
import requests
import urllib.parse
import json
import logging
import re
import os
import random
from datetime import datetime
from dotenv import load_dotenv
from prompts import SYSTEM_PROMPT

# --- SELENIUM IMPORTS ---
from selenium import webdriver
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

# !!! ЛЕЧЕНИЕ КОДИРОВКИ WINDOWS !!!
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

from groq import Groq 

try:
    load_dotenv()
except: pass

# ================= НАСТРОЙКИ =================
def get_env_int(key, default):
    try:
        val = os.getenv(key, str(default))
        val = re.sub(r'\D', '', val) 
        return int(val) if val else default
    except: return default

DELAY_BETWEEN_POSTS = get_env_int("DELAY_BETWEEN_POSTS", 180)
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")

TEST_MODE = os.getenv("TEST_MODE", "False").lower() in ('true', '1', 't')
TELEGRAM_ADMIN_ID = os.getenv("TELEGRAM_ADMIN_ID")

TARGET_URL = "https://finance.yahoo.com/topic/crypto/"
MODEL_NAME = os.getenv("MODEL_NAME", "llama-3.3-70b-versatile")

# ================= ЛОГИРОВАНИЕ =================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

try:
    if not GROQ_API_KEY:
        logger.warning("⚠ GROQ_API_KEY не найден!")
    client = Groq(api_key=GROQ_API_KEY)
except Exception as e:
    logger.critical(f"Ошибка Groq: {e}")
    sys.exit(1)

# ================= БАЗА ДАННЫХ =================
def init_db():
    try:
        conn = sqlite3.connect('posted_news.db')
        conn.execute('''CREATE TABLE IF NOT EXISTS posts (id INTEGER PRIMARY KEY, url TEXT UNIQUE)''')
        conn.commit()
        conn.close()
    except Exception as e: logger.critical(f"DB Error: {e}")

def is_posted(url):
    try:
        conn = sqlite3.connect('posted_news.db')
        res = conn.execute('SELECT 1 FROM posts WHERE url = ?', (url,)).fetchone()
        conn.close()
        return res is not None
    except: return True

def mark_as_posted(url):
    try:
        conn = sqlite3.connect('posted_news.db')
        conn.execute('INSERT OR IGNORE INTO posts (url) VALUES (?)', (url,))
        conn.commit()
        conn.close()
    except Exception as e: logger.error(f"DB Write Error: {e}")

# ================= ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =================
def clean_html_for_telegram(text):
    if not text: return ""
    text = re.sub(r'<p\s*[^>]*>', '', text, flags=re.IGNORECASE)
    text = re.sub(r'</p>', '\n\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<div[^>]*>', '', text, flags=re.IGNORECASE)
    text = re.sub(r'</div>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'<(?!(/?(b|strong|i|em|u|s|a|code|pre)))\w+[^>]*>', '', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

def parse_time_minutes(text):
    """Преобразует строку времени (5 minutes ago) в число минут."""
    if not text: return 999999
    text = text.lower()
    
    # Регулярки для поиска времени
    # Ищем "5 minutes ago", "1 hour ago"
    match = re.search(r'(\d+)\s+(minute|hour|day)', text)
    if match:
        val = int(match.group(1))
        unit = match.group(2)
        if 'minute' in unit: return val
        if 'hour' in unit: return val * 60
        if 'day' in unit: return val * 1440
    
    if 'yesterday' in text: return 1440
    if 'just now' in text: return 0
    
    return 999999 # Если время не нашли, считаем очень старым

# ================= SELENIUM =================
def get_driver():
    chrome_options = Options()
    chrome_options.add_argument("--headless=new") 
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    prefs = {"profile.managed_default_content_settings.images": 2}
    chrome_options.add_experimental_option("prefs", prefs)
    service = ChromeService(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=chrome_options)
    return driver

def handle_consent_popup(driver):
    try:
        if "consent" in driver.current_url:
            logger.info("🍪 Принимаю куки (GDPR)...")
            try:
                accept_btn = WebDriverWait(driver, 5).until(
                    EC.element_to_be_clickable((By.XPATH, "//button[contains(@name, 'agree') or contains(@value, 'agree') or contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'agree') or contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'accept') or contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'zustimmen')]"))
                )
                accept_btn.click()
                time.sleep(3) 
            except Exception as e:
                logger.warning(f"Ошибка кнопки куки: {e}")
    except Exception as e:
        logger.warning(f"Ошибка куки: {e}")


# ================= ПАРСИНГ (ТОЛЬКО MAIN CONTAINER) =================
def get_latest_news():
    driver = None
    links = [] 
    seen_urls = set()

    try:
        logger.info(f"🌐 Сканирую: {TARGET_URL}")
        driver = get_driver()
        driver.get(TARGET_URL)
        handle_consent_popup(driver)
        time.sleep(5)
        
        # Прокручиваем, чтобы подгрузить новости в контейнере
        # driver.execute_script("window.scrollTo(0, 1000);")
        time.sleep(2)
        
        try:
            # --- ГЛАВНОЕ ИЗМЕНЕНИЕ ---
            # Ищем конкретную секцию по классу mainContainer
            # Используем CSS селектор, так как он надежнее для составных классов
            container = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "section.mainContainer"))
            )
            logger.info("✅ Секция 'mainContainer' найдена!")
            
            # Ищем ссылки ТОЛЬКО внутри этого контейнера
            elements = container.find_elements(By.TAG_NAME, "a")
            logger.info(f"⚡ Ссылок внутри контейнера: {len(elements)}")
            
        except Exception as e:
            logger.error(f"❌ Не нашел секцию mainContainer. Возможно, Yahoo сменил верстку. Ошибка: {e}")
            return []

        for el in elements:
            try:
                url = el.get_attribute("href")
                if not url: continue
                
                # 1. Дубли
                if url in seen_urls: continue
                seen_urls.add(url)
                
                # 2. Фильтр мусора (реклама иногда бывает и внутри контейнера)
                # Игнорируем видео, котировки и т.д.
                if any(x in url for x in ['/video/', '/quote/', 'click.yahoo.com', 'beap.gemini.yahoo.com']):
                    continue
                
                # 3. Это должна быть новость
                if "/news/" in url or "/m/" in url or "/finance/" in url:
                    
                    # Фильтр коротких ссылок
                    if len(url) < 50: continue

                    # 4. Проверка БД
                    if is_posted(url): continue
                    
                    links.append(url)
                    logger.info(f"✅ НАЙДЕНА: {url}")

            except: continue
        
        # СОРТИРОВКА
        # В ленте Yahoo новости идут сверху вниз: [0] = Самая новая, [End] = Старая.
        # Мы хотим постить в хронологическом порядке (Старая -> Новая).
        
        # 1. Берем 20 самых верхних (это самые свежие на данный момент)
        top_20 = links[:20]
        
        # 2. Переворачиваем их. 
        # Теперь список идет от "Самой старой из свежих" к "Самой свежей"
        final_list = top_20[::-1]
        
        if final_list:
            logger.info(f"🔎 Готово к постингу: {len(final_list)} шт. Порядок публикации:")
            for i, link in enumerate(final_list, 1):
                logger.info(f"{i}. {link}")
        else:
            logger.warning("📭 Новых ссылок в контейнере не найдено (или все уже в базе).")

        return final_list

    except Exception as e:
        logger.error(f"Selenium Error: {e}")
        return []
    finally:
        if driver: 
            try: driver.quit()
            except: pass

def get_page_text(url):
    driver = None
    try:
        logger.info(f"📖 Читаю: {url}")
        driver = get_driver()
        driver.get(url)
        handle_consent_popup(driver)
        try:
            WebDriverWait(driver, 8).until(EC.presence_of_element_located((By.CLASS_NAME, "caas-body")))
            return driver.find_element(By.CLASS_NAME, "caas-body").text
        except:
            return driver.find_element(By.TAG_NAME, "article").text
    except Exception as e:
        logger.error(f"Текст не получен: {e}")
        return None
    finally:
        if driver: 
            try: driver.quit()
            except: pass

# ================= AI =================
def process_content_dynamic(text):
    if not text: return None, []
    logger.info(f"🧠 AI пишет пост...")
    
    user_message = f"Текст новости:\n{text[:6000]}" 

    try:
        chat_completion = client.chat.completions.create(
            messages=[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user_message}],
            model=MODEL_NAME, temperature=0.6, max_tokens=2000,
        )
        full_response = chat_completion.choices[0].message.content
        
        if "|||" in full_response:
            parts = full_response.split("|||")
            raw_post = parts[0].strip()
            prompt = parts[1].strip()
        else:
            raw_post = full_response
            prompt = "Bitcoin crypto finance"
            
        clean_post = clean_html_for_telegram(raw_post)
        return clean_post, [prompt]
        
    except Exception as e:
        logger.error(f"AI Error: {e}")
        return None, []

# ================= ОТПРАВКА =================
def generate_image_urls(prompts):
    urls = []
    base_seed = int(time.time())
    prompt = urllib.parse.quote(prompts[0])
    url = f"https://image.pollinations.ai/prompt/{prompt}?width=1280&height=720&seed={base_seed}&nologo=true&model=flux"
    urls.append(url)
    return urls

def send_telegram(text, image_urls):
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    
    chat_id = TELEGRAM_ADMIN_ID if TEST_MODE else TELEGRAM_CHANNEL_ID
    dest = "АДМИНУ" if TEST_MODE else "В КАНАЛ"

    if not chat_id:
        logger.error("❌ Не указан CHAT_ID")
        return

    final_text = f'<a href="{image_urls[0]}">&#8205;</a>{text}' if image_urls else text
    data = {'chat_id': chat_id, 'text': final_text, 'parse_mode': 'HTML', 'disable_web_page_preview': False}

    try:
        r = requests.post(api_url, data=data)
        if r.status_code == 400:
            logger.warning(f"⚠ Ошибка HTML. Шлю без тегов...")
            clean_text = text.replace('<b>', '').replace('</b>', '')
            data['text'] = clean_text + f"\n\n{image_urls[0] if image_urls else ''}"
            requests.post(api_url, data=data)
        elif r.status_code == 200: 
            logger.info(f"✅ Пост отправлен {dest}!")
    except Exception as e:
        logger.error(f"Send Error: {e}")

# ================= MAIN =================
if __name__ == "__main__":
    init_db()
    mode_str = "🛠 ТЕСТОВЫЙ" if TEST_MODE else "📢 ПРОДАКШН"
    logger.info(f"🚀 Бот запущен. Режим: {mode_str}")
    
    while True:
        try:
            links = get_latest_news()
            
            if not links:
                logger.info("📭 Нет новых новостей.")
            
            for link in links:
                logger.info(f"▶ {link}")
                text = get_page_text(link)
                
                if text and len(text) > 300:
                    post, prompts = process_content_dynamic(text)
                    if post:
                        img = generate_image_urls(prompts)
                        send_telegram(post, img)
                        mark_as_posted(link)
                        logger.info(f"💤 Сплю {DELAY_BETWEEN_POSTS} сек...")
                        time.sleep(DELAY_BETWEEN_POSTS)
                    else:
                        mark_as_posted(link)
                else:
                    logger.warning("Текст слишком короткий.")
                    mark_as_posted(link)
            
            logger.info("⏳ Жду 10 мин...")
            time.sleep(600)
            
        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.critical(f"Global Error: {e}")
            time.sleep(60)