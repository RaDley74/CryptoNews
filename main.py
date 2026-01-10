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
import random
from dotenv import load_dotenv

# !!! ЛЕЧЕНИЕ КОДИРОВКИ WINDOWS (ОБЯЗАТЕЛЬНО В НАЧАЛЕ) !!!
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

# Библиотека Groq
from groq import Groq 

# Загрузка переменных окружения из .env
load_dotenv()

# ================= НАСТРОЙКИ =================
DELAY_BETWEEN_POSTS = int(os.getenv("DELAY_BETWEEN_POSTS", 180))

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

# Инициализация Groq
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

# ================= ЧИСТКА ТЕКСТА (ИСПРАВЛЕННАЯ СТРУКТУРА) =================
def clean_html_for_telegram(text):
    if not text: return ""
    
    # 0. Удаляем мусорные заголовки от AI
    bad_prefixes = [
        "Тело поста:", "Вступление:", "Заголовок:", "Заключение:", 
        "Body:", "Intro:", "Title:", "Conclusion:", "Структура поста:",
        "Детали:", "Реакция рынка:", "Что будет дальше:", "Мнение Pulse Flow:"
    ]
    for prefix in bad_prefixes:
        text = re.sub(fr'{prefix}\s*', '', text, flags=re.IGNORECASE)

    # Удаляем китайские иероглифы (артефакты модели Llama)
    text = re.sub(r'[\u4e00-\u9fff]+', '', text)

    # 1. Сначала обрабатываем Markdown жирный шрифт **текст** -> <b>текст</b>
    text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)
    
    # 2. ГЛАВНОЕ: Превращаем теги абзацев в двойные переносы строк
    # <br> -> один перенос
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
    # </p> или </div> -> ДВА переноса (чтобы была пустая строка)
    text = re.sub(r'</(p|div|h\d)>', '\n\n', text, flags=re.IGNORECASE)
    # </li> -> один перенос
    text = re.sub(r'</li>', '\n', text, flags=re.IGNORECASE)

    # 3. Удаляем все открывающие теги блоков (они нам не нужны, мы уже обработали закрывающие)
    text = re.sub(r'<p[^>]*>', '', text, flags=re.IGNORECASE)
    text = re.sub(r'<div[^>]*>', '', text, flags=re.IGNORECASE)
    text = re.sub(r'<h\d[^>]*>', '', text, flags=re.IGNORECASE)

    # 4. Списки: делаем красиво
    text = re.sub(r'<li[^>]*>', '• ', text, flags=re.IGNORECASE)
    text = re.sub(r'<ul[^>]*>', '', text, flags=re.IGNORECASE)
    text = re.sub(r'</ul[^>]*>', '\n', text, flags=re.IGNORECASE)

    # 5. Чистим всё остальное (кроме поддерживаемых Telegram тегов: b, strong, i, em, code, pre, a)
    # Удаляем все теги, кроме разрешенных. 
    # В данном случае проще удалить всё, что похоже на тег и не является <b>, <a>, <code> и т.д.
    # Но регулярка выше уже сделала основную работу по структуре.
    
    # 6. Финальная зачистка пробелов и лишних переносов
    # Удаляем пробелы в начале и конце строк
    lines = [line.strip() for line in text.split('\n')]
    text = '\n'.join(lines)
    
    # Заменяем 3 и более переносов на 2 (чтобы не было огромных дыр)
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
    
    # !!! ПРОМПТ С ЖЕСТКИМ ТРЕБОВАНИЕМ HTML ТЕГОВ !!!
    system_prompt = """
    Ты — редактор крипто-канала "Crypto Pulse".
    
    Твоя задача:
    1. Написать пост на русском.
    2. НЕ использовать китайские иероглифы (строгий запрет).
    3. Придумать описание картинки на английском, которая ИЛЛЮСТРИРУЕТ ЭТУ НОВОСТЬ.
    
    ФОРМАТИРОВАНИЕ (СТРОГО):
    - Каждый абзац ОБЯЗАТЕЛЬНО оборачивай в тег <p>Текст абзаца</p>.
    - Заголовки выделяй <b>жирным</b> (не используй #).
    - Для важных слов используй <b>жирный</b>.
    - Тикеры пиши через $ (например $BTC).
    
    СТРУКТУРА ПОСТА:
    <p>⚡️ <b>ЗАГОЛОВОК</b></p>
    <p>Текст новости (суть).</p>
    <p>Детали и цифры.</p>
    
    В КОНЦЕ ОТВЕТА РАЗДЕЛИТЕЛЬ: |||
    После него напиши ПРОМПТ ДЛЯ КАРТИНКИ (на английском).
    
    ИНСТРУКЦИЯ ДЛЯ КАРТИНКИ:
    - Картинка должна быть УНИКАЛЬНОЙ и СТРОГО ПО ТЕМЕ НОВОСТИ.
    - ИЗБЕГАЙ ПРОСТЫХ ЛОГОТИПОВ. Используй метафоры и действия.
    - Опиши объекты, действие и настроение сцены.
    - НЕ ПИШИ стиль (render, 4k, realistic) - бот добавит сам.
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
                image_prompt = "Crypto blockchain node technology"

            prompts = [image_prompt] if image_prompt else []
            if not prompts: prompts.append("Bitcoin futuristic concept")
            
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

# ================= ЗАГРУЗКА ФОТО (СМЫСЛ + СТИЛЬ) =================
def generate_image_urls(prompts):
    urls = []
    base_seed = int(time.time())
    
    art_styles = [
        "cyberpunk city style, neon lights",
        "futuristic 3d render, unreal engine 5, isometric",
        "digital art, synthesizerwave colors",
        "cinematic lighting, dark noir atmosphere",
        "oil painting style, artistic masterpiece",
        "blueprint technical drawing style",
        "low poly 3d art, vibrant colors",
        "surreal dreamlike atmosphere",
        "photorealistic, highly detailed, 8k",
        "minimalist flat design, vector art",
        "abstract data visualization, complex geometric shapes",
        "retro comic book style, pop art",
        "double exposure, artistic photography"
    ]

    quality_filters = "high detail, 8k, no text, no typography"

    for i, prompt in enumerate(prompts):
        clean_prompt = re.sub(r'[^\w\s,]', '', prompt)
        
        chosen_style = random.choice(art_styles)
        
        # Сначала ЧТО, потом КАК
        final_query = f"{clean_prompt}, {chosen_style}, {quality_filters}"
        full_prompt = urllib.parse.quote(final_query)
        
        seed = base_seed + i 
        url = f"https://image.pollinations.ai/prompt/{full_prompt}?width=1280&height=720&seed={seed}&nologo=true&model=flux"
        urls.append(url)
        
        logger.info(f"🎨 Запрос картинки: {clean_prompt} | Стиль: {chosen_style}")
        
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
            # Попытка спасти текст, убрав теги, но оставив абзацы
            clean_text = text.replace('<b>', '').replace('</b>', '').replace('<code>', '').replace('</code>', '')
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