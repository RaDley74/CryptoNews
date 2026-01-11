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
import hashlib
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

# === ЧЕРНЫЙ СПИСОК КАРТИНОК ===
# Если бот снова пришлет ошибку, скопируй MD5 хэш из логов и добавь сюда.
BLOCKED_IMAGE_HASHES = [
    "d41d8cd98f00b204e9800998ecf8427e", # Пустой файл
    # Сюда можно добавлять хэши заглушек "We Have Moved" если они пролезут через проверку размера
]

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
                    EC.element_to_be_clickable((By.XPATH, "//button[contains(@name, 'agree') or contains(@value, 'agree') or contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'agree')]"))
                )
                accept_btn.click()
                time.sleep(3) 
            except Exception as e:
                logger.warning(f"Ошибка кнопки куки: {e}")
    except Exception as e:
        logger.warning(f"Ошибка куки: {e}")

# ================= ПАРСИНГ =================
def get_latest_news(only_fresh=True):
    driver = None
    links = [] 
    seen_urls = set()

    try:
        logger.info(f"🌐 Сканирую: {TARGET_URL}")
        driver = get_driver()
        driver.get(TARGET_URL)
        handle_consent_popup(driver)
        time.sleep(5)
        
        try:
            container = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "section.mainContainer"))
            )
            logger.info("✅ Секция 'mainContainer' найдена!")
            elements = container.find_elements(By.TAG_NAME, "a")
            
        except Exception as e:
            logger.error(f"❌ Не нашел секцию mainContainer: {e}")
            return []

        for el in elements:
            try:
                url = el.get_attribute("href")
                if not url: continue
                if url in seen_urls: continue
                seen_urls.add(url)
                
                if any(x in url for x in ['/video/', '/quote/', 'click.yahoo.com', 'beap.gemini.yahoo.com']):
                    continue
                
                if "/news/" in url or "/m/" in url or "/finance/" in url:
                    if len(url) < 50: continue
                    links.append(url)

            except: continue
        
        top_20 = links[:20]
        
        if only_fresh:
            fresh_links = [link for link in top_20 if not is_posted(link)]
            final_list = fresh_links[::-1]
            
            if final_list:
                logger.info(f"🔎 Готово к постингу: {len(final_list)} шт.")
            else:
                logger.warning("📭 Новых ссылок нет.")
            return final_list
        else:
            return top_20

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
    length_instruction = (
        "ДОПОЛНИТЕЛЬНО: Оцени важность новости. "
        "Если это ВАЖНОЕ событие — напиши развернуто (3-4 абзаца). "
        "Если рядовое — 2-3 абзаца. Не делай пост короче 2-х абзацев."
    )

    try:
        chat_completion = client.chat.completions.create(
            messages=[{"role": "system", "content": f"{SYSTEM_PROMPT}\n\n{length_instruction}"}, {"role": "user", "content": user_message}],
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
        return clean_post, prompt
        
    except Exception as e:
        logger.error(f"AI Error: {e}")
        return None, ""

# ================= РАБОТА С КАРТИНКАМИ (НОВОЕ) =================
# ================= РАБОТА С КАРТИНКАМИ (ОБНОВЛЕНО) =================
def download_and_validate_image(prompt):
    """
    Генерирует ссылку, скачивает картинку для ПРОВЕРКИ, 
    но возвращает URL, чтобы отправить его через sendMessage.
    """
    base_seed = int(time.time())
    encoded_prompt = urllib.parse.quote(prompt)
    # Используем Flux модель, nologo=true
    url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1280&height=720&seed={base_seed}&nologo=true&model=flux"
    
    logger.info(f"🎨 Генерирую и проверяю картинку: {prompt[:50]}...")
    
    try:
        # Скачиваем с таймаутом ТОЛЬКО для проверки
        response = requests.get(url, timeout=60)
        
        if response.status_code != 200:
            logger.warning(f"⚠ API картинки вернул код {response.status_code}")
            return None

        image_data = response.content
        image_size = len(image_data)
        image_md5 = hashlib.md5(image_data).hexdigest()

        # 1. ПРОВЕРКА РАЗМЕРА
        if image_size < 60000: 
            logger.warning(f"⛔ Картинка слишком легкая ({image_size} байт). Скорее всего заглушка. Отменяю.")
            return None

        # 2. ПРОВЕРКА ХЭША (ЧЕРНЫЙ СПИСОК)
        if image_md5 in BLOCKED_IMAGE_HASHES:
            logger.warning(f"⛔ Картинка в черном списке (Hash: {image_md5}). Не отправляю.")
            return None

        logger.info(f"✅ Картинка валидна (Size: {image_size}). Возвращаю URL.")
        # ВОЗВРАЩАЕМ URL, а не байты
        return url

    except Exception as e:
        logger.error(f"Ошибка проверки картинки: {e}")
        return None

def send_telegram_post(text, image_url):
    """
    Отправляет пост через sendMessage.
    Если есть image_url, вставляет его как невидимую ссылку для превью.
    Это позволяет отправлять до 4096 символов текста.
    """
    chat_id = TELEGRAM_ADMIN_ID if TEST_MODE else TELEGRAM_CHANNEL_ID
    dest = "АДМИНУ" if TEST_MODE else "В КАНАЛ"

    if not chat_id:
        logger.error("❌ Не указан CHAT_ID")
        return

    try:
        # Формируем тело сообщения
        if image_url:
            # Вставляем невидимый символ &#8205; внутри ссылки. 
            # Телеграм распарсит это как превью картинки (Large Media Preview).
            final_text = f'<a href="{image_url}">&#8205;</a>{text}'
            disable_preview = False # Нужно включить превью, чтобы картинка появилась
        else:
            final_text = text
            disable_preview = True # Если картинки нет, отключаем превью (чтобы не тянулись ссылки из новостей)

        data = {
            'chat_id': chat_id, 
            'text': final_text, 
            'parse_mode': 'HTML',
            'disable_web_page_preview': disable_preview
        }
        
        api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        r = requests.post(api_url, data=data)

        # Обработка ошибок (например, если HTML кривой)
        if r.status_code == 400:
            logger.warning(f"⚠ Ошибка Telegram 400: {r.text}. Пробую без HTML (но тогда и без картинки)...")
            # Если ошибка в тегах, отправляем чистый текст без картинки
            clean_text_only = re.sub(r'<[^>]+>', '', text)
            data['text'] = clean_text_only
            data['parse_mode'] = None
            data['disable_web_page_preview'] = True
            r = requests.post(api_url, data=data)
        
        if r and r.status_code == 200:
            logger.info(f"✅ Пост успешно отправлен {dest}!")
        elif r:
            logger.error(f"Ошибка отправки: {r.text}")

    except Exception as e:
        logger.error(f"Send Error: {e}")

# ================= MAIN =================
# ================= MAIN =================
if __name__ == "__main__":
    init_db()
    
    # --- ПЕРВИЧНАЯ СИНХРОНИЗАЦИЯ (ДЛЯ НОВОГО СЕРВЕРА) ---
    try:
        conn = sqlite3.connect('posted_news.db')
        cursor = conn.cursor()
        cursor.execute('SELECT count(*) FROM posts')
        db_count = cursor.fetchone()[0]
        conn.close()

        if db_count == 0:
            logger.info("🆕 Обнаружена пустая база. Выполняю первичную настройку...")
            all_links = get_latest_news(only_fresh=False)
            
            if all_links:
                logger.info(f"📥 Сохраняю {len(all_links)} ссылок в базу...")
                for link in all_links:
                    mark_as_posted(link)
                
                links_to_release = all_links[:5] # 5 самых свежих
                logger.info(f"🔓 Освобождаю {len(links_to_release)} последних новостей для постинга...")
                
                conn = sqlite3.connect('posted_news.db')
                for link in links_to_release:
                    conn.execute('DELETE FROM posts WHERE url = ?', (link,))
                conn.commit()
                conn.close()
                logger.info("✅ Синхронизация завершена.")
    except Exception as e:
        logger.error(f"Ошибка инициализации: {e}")

    mode_str = "🛠 ТЕСТОВЫЙ" if TEST_MODE else "📢 ПРОДАКШН"
    logger.info(f"🚀 Бот запущен. Режим: {mode_str}")
    
    while True:
        try:
            links = get_latest_news()
            
            if not links:
                logger.info("📭 Нет новых новостей.")
            else:
                logger.info(f"📋 Очередь обработки ({len(links)} шт):")
                for i, l in enumerate(links, 1):
                    logger.info(f"   {i}. {l}")
            
            for link in links:
                logger.info(f"▶ Обработка: {link}")
                text = get_page_text(link)
                
                if text and len(text) > 300:
                    post, prompt_text = process_content_dynamic(text)
                    if post:
                        # 1. Проверяем картинку и получаем URL
                        image_url = download_and_validate_image(prompt_text)
                        
                        # 2. Если URL вернулся (проверка пройдена), отправляем с ссылкой
                        # Если None - отправится просто текст
                        if image_url is None:
                            logger.info("ℹ Отправляю пост БЕЗ картинки (сбой генерации или фильтр).")
                        
                        # 3. Отправляем в телеграм
                        send_telegram_post(post, image_url)
                        
                        mark_as_posted(link)
                        logger.info(f"💤 Сплю {DELAY_BETWEEN_POSTS} сек...")
                        time.sleep(DELAY_BETWEEN_POSTS)
                    else:
                        mark_as_posted(link)
                else:
                    logger.warning("Текст слишком короткий.")
                    mark_as_posted(link)
            
            logger.info("⏳ Жду 10 мин перед следующей проверкой...")
            time.sleep(600)
            
        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.critical(f"Global Error: {e}")
            time.sleep(60)