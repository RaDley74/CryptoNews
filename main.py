import warnings
warnings.simplefilter(action='ignore', category=FutureWarning)

import sys
import io
import time
import sqlite3
import requests
from bs4 import BeautifulSoup
import urllib.parse
import json
import logging
import re
import os
from dotenv import load_dotenv

# !!! ЛЕЧЕНИЕ КОДИРОВКИ WINDOWS (ОБЯЗАТЕЛЬНО В НАЧАЛЕ) !!!
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

# Библиотека Groq
from groq import Groq 

# Загрузка переменных окружения из .env
load_dotenv()

# ================= НАСТРОЙКИ =================
# Пауза между постами (3 минуты).
DELAY_BETWEEN_POSTS = int(os.getenv("DELAY_BETWEEN_POSTS", 180))

# ВСТАВЬ СЮДА СВОИ КЛЮЧИ
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")

TARGET_URL = os.getenv("TARGET_URL", "https://crypto.news/news/")
MODEL_NAME = os.getenv("MODEL_NAME", "llama-3.3-70b-versatile")

# ================= ЛОГИРОВАНИЕ =================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("bot_log.txt", encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# Инициализация клиента Groq
try:
    client = Groq(api_key=GROQ_API_KEY)
except Exception as e:
    logger.critical(f"Ошибка инициализации Groq: {e}")
    sys.exit(1)

# ================= БАЗА ДАННЫХ =================
def init_db():
    try:
        conn = sqlite3.connect('posted_news.db')
        cursor = conn.cursor()
        cursor.execute('''CREATE TABLE IF NOT EXISTS posts (id INTEGER PRIMARY KEY, url TEXT UNIQUE)''')
        conn.commit()
        conn.close()
    except Exception as e:
        logger.critical(f"Ошибка БД: {e}")

def is_posted(url):
    try:
        conn = sqlite3.connect('posted_news.db')
        cursor = conn.cursor()
        cursor.execute('SELECT 1 FROM posts WHERE url = ?', (url,))
        res = cursor.fetchone()
        conn.close()
        return res is not None
    except: return True

def mark_as_posted(url):
    try:
        conn = sqlite3.connect('posted_news.db')
        cursor = conn.cursor()
        cursor.execute('INSERT OR IGNORE INTO posts (url) VALUES (?)', (url,))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Ошибка записи БД: {e}")

# ================= ПАРСИНГ =================
def get_latest_news():
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        logger.info(f"Сканирую сайт: {TARGET_URL}")
        response = requests.get(TARGET_URL, headers=headers, timeout=15)
        if response.status_code != 200: return []
        
        soup = BeautifulSoup(response.content, 'html.parser')
        news_links = []
        
        for a in soup.find_all('a', href=True):
            href = a['href']
            if href.startswith('/'): href = "https://crypto.news" + href
            if "crypto.news" not in href: continue
            
            bad_words = ['/category/', '/tag/', '/author/', '/page/', 'contact', 'about']
            if any(w in href for w in bad_words): continue
            if href.count('-') < 3: continue 
            
            if not is_posted(href):
                if href not in news_links:
                    news_links.append(href)
        
        return news_links[::-1]
    except Exception as e:
        logger.error(f"Ошибка парсинга: {e}")
        return []

def get_page_text(url):
    try:
        r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
        soup = BeautifulSoup(r.content, 'html.parser')
        content = soup.find('div', class_='post-content') or soup.find('article')
        if content:
            for s in content(["script", "style"]): s.decompose()
            return content.get_text(separator="\n", strip=True)
        return None
    except: return None

# ================= ЧИСТКА ТЕКСТА (ИСПРАВЛЕННАЯ) =================
def clean_html_for_telegram(text):
    if not text: return ""
    
    # 0. Убираем "мусорные" заголовки от AI
    bad_prefixes = [
        "Тело поста:", "Вступление:", "Заголовок:", "Заключение:", 
        "Body:", "Intro:", "Title:", "Conclusion:", "Структура поста:",
        "Детали:", "Реакция рынка:", "Что будет дальше:", "Мнение Pulse Flow:"
    ]
    for prefix in bad_prefixes:
        text = re.sub(fr'{prefix}\s*', '', text, flags=re.IGNORECASE)

    # 1. Заменяем структурные теги на переносы строк (ВАЖНО!)
    # <br> -> перенос
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
    # </p>, </div> -> двойной перенос (новый абзац)
    text = re.sub(r'</(p|div|h\d)>', '\n\n', text, flags=re.IGNORECASE)
    # </li> -> перенос
    text = re.sub(r'</li>', '\n', text, flags=re.IGNORECASE)

    # 2. Markdown -> HTML
    text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text) # Жирный
    text = re.sub(r'#{1,6}\s+(.*)', r'<b>\1</b>', text) # Markdown заголовки в жирный

    # 3. Чистим оставшиеся HTML теги (удаляем сами теги, но оставляем текст внутри)
    # Удаляем всё кроме разрешенных Telegram тегов (b, i, code, a, pre)
    # Но проще удалить структурные, так как мы их уже заменили на \n
    text = re.sub(r'</?(div|p|span|h\d|ul|ol|li|article|section)[^>]*>', '', text, flags=re.IGNORECASE)

    # 4. Финальная шлифовка
    # Убираем пробелы в начале/конце каждой строки
    text = re.sub(r'^[ \t]+|[ \t]+$', '', text, flags=re.MULTILINE)
    # Заменяем тройные+ переносы на двойные (чтобы не было огромных дыр)
    text = re.sub(r'\n{3,}', '\n\n', text)

    return text.strip()

def smart_truncate(text, max_length=4000):
    if len(text) <= max_length:
        return text
    truncated = text[:max_length]
    last_dot = truncated.rfind('.')
    if last_dot > max_length * 0.8:
        truncated = truncated[:last_dot+1]
    else:
        truncated = truncated.rsplit(' ', 1)[0]
    
    if truncated.count('<b>') > truncated.count('</b>'):
        truncated += '</b>'
    
    truncated += "..."
    return truncated

# ================= AI (GROQ) =================
def process_content_dynamic(text):
    if not text: return None, []
    
    logger.info(f"Анализирую новость ({MODEL_NAME})...")
    
    # ОБНОВЛЕННЫЙ ПРОМПТ
    system_prompt = """
    Ты — главный редактор Telegram-канала "Pulse Flow Crypto".
    
    Твоя задача: Написать пост на русском языке, АДАПТИРУЯ ЕГО ОБЪЕМ под важность события.
    
    ЛОГИКА ОБЪЕМА:
    1. 🟢 Локальная новость: 800 - 1200 символов (Кратко и по делу).
    2. 🔴 Глобальное событие: 2000 - 3500 символов (Глубокая аналитика).
    
    СТРУКТУРА ПОСТА:
    1. [Заголовок]: ⚡️ ЗАГОЛОВОК (Цепляющий, с эмодзи).
    2. [Текст]: Суть + подробности.
    3. [Вывод]: 👁 Мнение Crypto Pulse.
    
    ТРЕБОВАНИЯ:
    - Используй АБЗАЦЫ (отделяй пустой строкой).
    - HTML теги: <b>жирный</b>, <code>код</code>.
    - Тикеры через $ (например $BTC).
    
    В КОНЦЕ ОТВЕТА РАЗДЕЛИТЕЛЬ: |||
    После него напиши 1 ПРОМПТ ДЛЯ КАРТИНКИ на английском.
    
    ВАЖНО ДЛЯ КАРТИНКИ:
    - Придумай ВИЗУАЛЬНУЮ МЕТАФОРУ (например: "золотой бык", "цифровой замок", "ракета в неоне").
    - СТРОГО ЗАПРЕЩЕНО использовать слова: "text", "graph", "chart", "diagram", "letters", "percent".
    - Описывай ТОЛЬКО объект или атмосферу. Не проси нарисовать надписи.
    """

    safe_text = str(text)
    user_message = f"Текст новости:\n{safe_text[:7000]}" 

    for attempt in range(3):
        try:
            chat_completion = client.chat.completions.create(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message}
                ],
                model=MODEL_NAME,
                temperature=0.6,
                max_tokens=3000,
            )

            full_response = chat_completion.choices[0].message.content
            
            if "|||" in full_response:
                parts = full_response.split("|||")
                post_text = clean_html_for_telegram(parts[0].strip())
                image_prompt = parts[1].strip()
            else:
                post_text = clean_html_for_telegram(full_response)
                image_prompt = "Abstract futuristic crypto sphere, neon lights, 3d render"

            prompts = [image_prompt] if image_prompt else []
            if not prompts: prompts.append("Abstract blockchain background, blue neon, 3d render")
            
            post_text = smart_truncate(post_text, max_length=4000)
            return post_text, prompts

        except Exception as e:
            if "429" in str(e):
                logger.warning(f"⚠ Лимит Groq. Жду 20 сек...")
                time.sleep(20)
            elif "model_decommissioned" in str(e):
                logger.critical("❌ МОДЕЛЬ УСТАРЕЛА! Обнови MODEL_NAME.")
                return None, []
            else:
                logger.error(f"Ошибка AI: {e}")
                time.sleep(5)
                
    return None, []

# ================= ЗАГРУЗКА ФОТО (Pollinations) =================
def generate_image_urls(prompts):
    urls = []
    base_seed = int(time.time())
    
    # Жесткий стиль: запрещает текст, цифры и графики, делает красивое 3D
    forced_style = "futuristic 3d crypto art, isometric, unreal engine 5 render, cinematic lighting, no text, no numbers, no typography, no graphs, high detail, 8k, abstract masterpiece"

    for i, prompt in enumerate(prompts):
        clean_prompt = re.sub(r'[^\w\s,]', '', prompt) 
        full_prompt = urllib.parse.quote(f"{clean_prompt}, {forced_style}")
        
        seed = base_seed + i 
        url = f"https://image.pollinations.ai/prompt/{full_prompt}?width=1280&height=720&seed={seed}&nologo=true&model=flux"
        urls.append(url)
    return urls

# ================= ОТПРАВКА =================
def send_telegram(text, image_urls):
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    
    if image_urls:
        img_url = image_urls[0]
        final_text = f'<a href="{img_url}">&#8205;</a>{text}'
    else:
        final_text = text

    data = {
        'chat_id': TELEGRAM_CHANNEL_ID,
        'text': final_text,
        'parse_mode': 'HTML',
        'disable_web_page_preview': False 
    }

    try:
        r = requests.post(api_url, data=data)
        
        if r.status_code == 400:
            logger.warning(f"Ошибка TG (HTML): {r.text}. Шлю чистый текст...")
            clean_text = BeautifulSoup(text, "html.parser").get_text()
            if image_urls:
                clean_text += f"\n\n🖼 <a href='{image_urls[0]}'>Image</a>"
            
            requests.post(api_url, data={'chat_id': TELEGRAM_CHANNEL_ID, 'text': clean_text, 'parse_mode': 'HTML'})
            
        elif r.status_code == 200: 
            logger.info("✅ Пост опубликован!")
        else: 
            logger.error(f"Ошибка TG: {r.text}")
    except Exception as e: 
        logger.error(f"Сбой отправки: {e}")

# ================= ГЛАВНЫЙ ЦИКЛ =================
if __name__ == "__main__":
    init_db()
    logger.info(f"🚀 Бот запущен (Groq: {MODEL_NAME})")
    
    while True:
        try:
            links = get_latest_news()
            
            if not links:
                logger.info("Новых новостей пока нет.")
            
            for link in links:
                logger.info(f"▶ Обработка: {link}")
                
                text = get_page_text(link)
                if text:
                    post, prompts = process_content_dynamic(text)
                    if post:
                        img_urls = generate_image_urls(prompts)
                        send_telegram(post, img_urls)
                        mark_as_posted(link)
                        
                        logger.info(f"💤 Сплю {DELAY_BETWEEN_POSTS} сек...")
                        time.sleep(DELAY_BETWEEN_POSTS)
                    else:
                        logger.warning("AI вернул пустой ответ.")
                        mark_as_posted(link)
                else:
                    logger.warning("Не удалось получить текст.")
                    mark_as_posted(link)
            
            logger.info("Проверка через 10 минут...")
            time.sleep(600)
            
        except Exception as e:
            logger.critical(f"Глобальная ошибка: {e}")
            time.sleep(60)