# --- START OF FILE main.py ---

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
from bs4 import BeautifulSoup  # !!! НУЖНО: pip install beautifulsoup4 lxml !!!

from prompts import SYSTEM_PROMPT
from groq import Groq 

# !!! ЛЕЧЕНИЕ КОДИРОВКИ WINDOWS !!!
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

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

# Хедеры для эмуляции браузера (чтобы Yahoo не блокировал)
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
    'Cache-Control': 'max-age=0',
    'Upgrade-Insecure-Requests': '1',
}

# === ЧЕРНЫЙ СПИСОК КАРТИНОК ===
BLOCKED_IMAGE_HASHES = [
    "d41d8cd98f00b204e9800998ecf8427e", 
    "2090a5dc21c32952cbf8496339752bd1"
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

# ================= ПАРСИНГ (REQUESTS + BS4) =================
def get_session():
    """Создает сессию с базовыми куками, если нужно"""
    session = requests.Session()
    session.headers.update(HEADERS)
    return session

def get_latest_news(only_fresh=True):
    links = [] 
    seen_urls = set()
    
    # Список URL для сканирования (основной + запасной)
    urls_to_scan = [TARGET_URL]
    session = get_session()

    for current_url in urls_to_scan:
        if len(links) > 0: break # Если нашли ссылки, выходим

        if current_url != urls_to_scan[0]: time.sleep(2)
        try:
            logger.info(f"🌐 Сканирую: {current_url}")
            response = session.get(current_url, timeout=15)
        
            if response.status_code != 200:
                logger.error(f"❌ Ошибка доступа к сайту: {response.status_code}")
                continue

            soup = BeautifulSoup(response.content, 'lxml')
            if soup.title:
                title_text = soup.title.string.strip()
                logger.info(f"📄 Заголовок: {title_text}")
                
                if any(x in title_text for x in ["Consent", "Datenschutzeinstellungen", "Privacy"]):
                    logger.warning("⚠ Попали на страницу согласия. Пробую следующий URL...")
                    continue
        
            # Yahoo Finance часто меняет верстку, ищем универсальный контейнер
            container = soup.select_one("#Fin-Stream") or \
                        soup.select_one("#mrt-node-Col1-1-Stream") or \
                        soup.select_one("div[id*='Stream']") or \
                        soup.select_one("section.mainContainer") or \
                        soup.select_one("#quoteNewsStream-0-Stream")
        
            if not container:
                # logger.warning("⚠ Контейнер не найден. Пробую искать через заголовки h3...")
                elements = soup.select("h3 a")
                if not elements:
                    # logger.warning("⚠ Заголовки не найдены. Сканирую ВСЕ ссылки на странице...")
                    elements = soup.find_all("a")
            else:
                elements = container.find_all("a")

            if not elements:
                logger.warning(f"⚠ Ссылки не найдены на {current_url}")
                continue

            for el in elements:
                try:
                    url = el.get('href')
                    if not url: continue
                
                    if url.startswith('/'):
                        url = "https://finance.yahoo.com" + url
                    url = url.split('?')[0]

                    if url in seen_urls: continue
                    seen_urls.add(url)
                
                    if any(x in url for x in ['/video/', '/quote/', 'click.yahoo.com', 'beap.gemini.yahoo.com', 'subscription']):
                        continue
                
                    if "/news/" in url or "/m/" in url or "/finance/" in url:
                        if len(url) < 40: continue 
                        links.append(url)

                except: continue

        except Exception as e:
            logger.error(f"Parsing Error on {current_url}: {e}")
            continue
    
    # Обработка результатов
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

def get_page_text(url):
    try:
        logger.info(f"📖 Читаю: {url}")
        session = get_session()
        response = session.get(url, timeout=15)
        
        if response.status_code != 200:
            logger.error(f"Не удалось открыть статью: {response.status_code}")
            return None

        soup = BeautifulSoup(response.content, 'lxml')
        
        if soup.title and soup.title.string:
            logger.info(f"📄 Заголовок статьи: {soup.title.string.strip()}")
        
        # Класс тела статьи на Yahoo Finance обычно .caas-body
        article_body = soup.select_one(".caas-body")
        
        # CoinTelegraph
        if not article_body:
            article_body = soup.select_one(".post-content")

        if not article_body:
            # Запасной вариант - искать тег article
            article_body = soup.find("article")

        if article_body:
            # Удаляем "Read more" кнопки и рекламу внутри текста
            for bad_tag in article_body.select("button, .caas-readmore, .caas-iframe, div[class*='ad-']"):
                bad_tag.decompose()
            
            text = article_body.get_text(separator="\n\n").strip()
            return text
        else:
            logger.warning("⚠ Не удалось найти текст статьи (нет .caas-body)")
            return None

    except Exception as e:
        logger.error(f"Текст не получен: {e}")
        return None

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
            prompt = "Bitcoin crypto finance abstract"
            
        clean_post = clean_html_for_telegram(raw_post)
        return clean_post, prompt
        
    except Exception as e:
        logger.error(f"AI Error: {e}")
        return None, ""

# ================= РАБОТА С КАРТИНКАМИ =================
def download_and_validate_image(prompt):
    base_seed = int(time.time())
    encoded_prompt = urllib.parse.quote(prompt)
    url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1280&height=720&seed={base_seed}&nologo=true&model=flux"
    
    logger.info(f"🎨 Генерирую и проверяю картинку: {prompt[:50]}...")
    
    try:
        response = requests.get(url, timeout=60)
        
        if response.status_code != 200:
            logger.warning(f"⚠ API картинки вернул код {response.status_code}")
            return None

        image_data = response.content
        image_md5 = hashlib.md5(image_data).hexdigest()

        if image_md5 in BLOCKED_IMAGE_HASHES:
            logger.warning(f"⛔ Картинка в черном списке. Не отправляю.")
            return "BLOCKED"

        logger.info(f"✅ Картинка валидна. Возвращаю URL.")
        return url

    except Exception as e:
        logger.error(f"Ошибка проверки картинки: {e}")
        return None

def notify_admin(message):
    """Отправляет уведомление администратору в Telegram"""
    if not TELEGRAM_ADMIN_ID or not TELEGRAM_BOT_TOKEN:
        return

    try:
        api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = {
            'chat_id': TELEGRAM_ADMIN_ID,
            'text': f"⚠️ <b>Уведомление от бота:</b>\n\n{str(message)[:4000]}",
            'parse_mode': 'HTML'
        }
        requests.post(api_url, data=data)
    except Exception as e:
        logger.error(f"Не удалось отправить уведомление админу: {e}")

def send_telegram_post(text, image_url):
    chat_id = TELEGRAM_ADMIN_ID if TEST_MODE else TELEGRAM_CHANNEL_ID
    dest = "АДМИНУ" if TEST_MODE else "В КАНАЛ"

    if not chat_id:
        logger.error("❌ Не указан CHAT_ID")
        return

    try:
        if image_url:
            final_text = f'<a href="{image_url}">&#8205;</a>{text}'
            disable_preview = False 
        else:
            final_text = text
            disable_preview = True 

        data = {
            'chat_id': chat_id, 
            'text': final_text, 
            'parse_mode': 'HTML',
            'disable_web_page_preview': disable_preview
        }
        
        api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        r = requests.post(api_url, data=data)

        if r.status_code == 400:
            logger.warning(f"⚠ Ошибка Telegram 400: {r.text}. Пробую без HTML...")
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
if __name__ == "__main__":
    init_db()
    
    # --- ПЕРВИЧНАЯ СИНХРОНИЗАЦИЯ ---
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
                
                # Оставляем пару новостей для теста/старта
                links_to_release = all_links[:3] 
                logger.info(f"🔓 Освобождаю {len(links_to_release)} последних новостей...")
                
                conn = sqlite3.connect('posted_news.db')
                for link in links_to_release:
                    conn.execute('DELETE FROM posts WHERE url = ?', (link,))
                conn.commit()
                conn.close()
                logger.info("✅ Синхронизация завершена.")
    except Exception as e:
        logger.error(f"Ошибка инициализации: {e}")

    mode_str = "🛠 ТЕСТОВЫЙ" if TEST_MODE else "📢 ПРОДАКШН"
    logger.info(f"🚀 Бот запущен (Requests Mode). Режим: {mode_str}")
    notify_admin(f"🚀 Бот успешно запущен.\nРежим: {mode_str}")
    
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
                        image_url = download_and_validate_image(prompt_text)
                        
                        if image_url == "BLOCKED":
                            logger.info("⏳ Картинка в черном списке. Откладываю пост на следующую попытку...")
                            continue

                        if image_url is None:
                            logger.info("ℹ Отправляю пост БЕЗ картинки.")
                        
                        send_telegram_post(post, image_url)
                        
                        mark_as_posted(link)
                        wait_time = DELAY_BETWEEN_POSTS + random.randint(10, 60)
                        logger.info(f"💤 Сплю {wait_time} сек...")
                        time.sleep(wait_time)
                    else:
                        mark_as_posted(link)
                else:
                    logger.warning("Текст слишком короткий или не найден.")
                    mark_as_posted(link)
            
            wait_time = 600 + random.randint(-120, 120)
            logger.info(f"⏳ Жду {wait_time // 60} мин ({wait_time} сек) перед следующей проверкой...")
            time.sleep(wait_time)
            
        except KeyboardInterrupt:
            break
        except Exception as e:
            error_msg = f"❌ Критическая ошибка:\n{e}"
            logger.critical(error_msg)
            notify_admin(error_msg)
            time.sleep(60)