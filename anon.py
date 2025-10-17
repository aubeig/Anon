import logging
import os
import secrets
import string
import sqlite3
import re
import shutil
import time
import asyncio
import html
import json
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from telegram.constants import ParseMode
from git import Repo

# --- НАСТРОЙКИ ИЗ ПЕРЕМЕННЫХ ОКРУЖЕНИЯ ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME")
ADMIN_ID = int(os.environ.get("ADMIN_ID")) if os.environ.get("ADMIN_ID") else None
ADMIN_PASSWORD = "sirok228"  # Пароль для админки

# --- НАСТРОЙКИ ДЛЯ ХРАНЕНИЯ БД НА GITHUB ---
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO = os.environ.get("GITHUB_REPO")
DB_FILENAME = os.environ.get("DB_FILENAME", "data.db")
REPO_PATH = "/tmp/repo"
DB_PATH = os.path.join(REPO_PATH, DB_FILENAME)

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', 
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('bot.log', encoding='utf-8')
    ]
)

repo = None

# --- ФУНКЦИИ ДЛЯ РАБОТЫ С GIT ---

def setup_repo():
    """Клонирует репозиторий из GitHub во временную папку."""
    global repo
    remote_url = f"https://{GITHUB_TOKEN}@github.com/{GITHUB_REPO}.git"
    
    if os.path.exists(REPO_PATH):
        shutil.rmtree(REPO_PATH)
    
    max_retries = 3
    for attempt in range(max_retries):
        try:
            logging.info(f"Клонирование репозитория {GITHUB_REPO}... (попытка {attempt + 1})")
            repo = Repo.clone_from(remote_url, REPO_PATH)
            repo.config_writer().set_value("user", "name", "AnonBot").release()
            repo.config_writer().set_value("user", "email", "bot@render.com").release()
            logging.info("Репозиторий успешно склонирован и настроен.")
            return
        except Exception as e:
            logging.error(f"Ошибка при клонировании репозитория (попытка {attempt + 1}): {e}")
            if attempt < max_retries - 1:
                time.sleep(5)
            else:
                logging.critical("Не удалось склонировать репозиторий, создаю локальную БД")
                os.makedirs(REPO_PATH, exist_ok=True)

def push_db_to_github(commit_message):
    """Отправляет файл базы данных на GitHub."""
    if not repo:
        logging.error("Репозиторий не инициализирован, push невозможен.")
        return
    
    max_retries = 3
    for attempt in range(max_retries):
        try:
            repo.index.add([DB_PATH])
            if repo.is_dirty(index=True, working_tree=False):
                repo.index.commit(commit_message)
                origin = repo.remote(name='origin')
                origin.push()
                logging.info(f"База данных успешно отправлена на GitHub. Коммит: {commit_message}")
                return
            else:
                logging.info("Нет изменений в БД для отправки.")
                return
        except Exception as e:
            logging.error(f"Ошибка при отправке БД на GitHub (попытка {attempt + 1}): {e}")
            if attempt < max_retries - 1:
                time.sleep(5)

# --- ФУНКЦИИ ДЛЯ РАБОТЫ С БД ---

def init_db():
    """Создает таблицы, если их нет."""
    try:
        db_existed_before = os.path.exists(DB_PATH)
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # Таблица пользователей
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY, 
                username TEXT, 
                first_name TEXT, 
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Таблица ссылок
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS links (
                link_id TEXT PRIMARY KEY, 
                user_id INTEGER, 
                title TEXT, 
                description TEXT, 
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, 
                expires_at TIMESTAMP, 
                is_active BOOLEAN DEFAULT 1,
                FOREIGN KEY (user_id) REFERENCES users (user_id)
            )
        ''')
        
        # Таблица сообщений
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS messages (
                message_id INTEGER PRIMARY KEY AUTOINCREMENT, 
                link_id TEXT, 
                from_user_id INTEGER, 
                to_user_id INTEGER, 
                message_text TEXT, 
                message_type TEXT DEFAULT "text", 
                file_id TEXT,
                file_size INTEGER,
                file_name TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, 
                FOREIGN KEY (link_id) REFERENCES links (link_id)
            )
        ''')
        
        # Таблица ответов
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS replies (
                reply_id INTEGER PRIMARY KEY AUTOINCREMENT, 
                message_id INTEGER, 
                from_user_id INTEGER, 
                reply_text TEXT, 
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, 
                FOREIGN KEY (message_id) REFERENCES messages (message_id)
            )
        ''')
        
        conn.commit()
        conn.close()
        
        if not db_existed_before:
            logging.info("Файл БД не найден, создаю новый и отправляю на GitHub...")
            push_db_to_github("Initial commit: create database file")
    except Exception as e:
        logging.error(f"Ошибка при инициализации БД: {e}")

def run_query(query, params=(), commit=False, fetch=None):
    """Универсальная функция для выполнения запросов к БД."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            if commit:
                conn.commit()
            if fetch == "one":
                return cursor.fetchone()
            if fetch == "all":
                return cursor.fetchall()
            if commit:
                return cursor.lastrowid
    except sqlite3.Error as e:
        logging.error(f"Ошибка базы данных: {e}")
        return None

def save_user(user_id, username, first_name):
    run_query('INSERT OR IGNORE INTO users (user_id, username, first_name) VALUES (?, ?, ?)', 
              (user_id, username, first_name), commit=True)

def create_anon_link(user_id, title, description):
    link_id = ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(10))
    expires_at = datetime.now() + timedelta(days=365)  # Увеличил до 1 года
    run_query('INSERT INTO links (link_id, user_id, title, description, expires_at) VALUES (?, ?, ?, ?, ?)', 
              (link_id, user_id, title, description, expires_at), commit=True)
    push_db_to_github(f"Create link for user {user_id}")
    return link_id

def save_message(link_id, from_user_id, to_user_id, message_text, message_type='text', file_id=None, file_size=None, file_name=None):
    message_id = run_query('INSERT INTO messages (link_id, from_user_id, to_user_id, message_text, message_type, file_id, file_size, file_name) VALUES (?, ?, ?, ?, ?, ?, ?, ?)', 
                          (link_id, from_user_id, to_user_id, message_text, message_type, file_id, file_size, file_name), commit=True)
    push_db_to_github(f"Save message from {from_user_id} to {to_user_id}")
    return message_id

def save_reply(message_id, from_user_id, reply_text):
    run_query('INSERT INTO replies (message_id, from_user_id, reply_text) VALUES (?, ?, ?)', 
              (message_id, from_user_id, reply_text), commit=True)
    push_db_to_github(f"Save reply to message {message_id}")

# --- НОВЫЕ ФУНКЦИИ ДЛЯ РАСШИРЕННОГО ПРОСМОТРА ---

def get_link_info(link_id):
    return run_query('SELECT l.link_id, l.user_id, l.title, l.description, u.username FROM links l LEFT JOIN users u ON l.user_id = u.user_id WHERE l.link_id = ?', (link_id,), fetch="one")

def get_user_links(user_id):
    return run_query('SELECT link_id, title, description, created_at FROM links WHERE user_id = ? AND is_active = 1', (user_id,), fetch="all")

def get_user_messages_with_replies(user_id, limit=50):
    """Получает сообщения вместе с ответами"""
    return run_query('''
        SELECT m.message_id, m.message_text, m.message_type, m.file_id, m.file_size, m.file_name, 
               m.created_at, l.title as link_title, l.link_id,
               (SELECT COUNT(*) FROM replies r WHERE r.message_id = m.message_id) as reply_count
        FROM messages m 
        JOIN links l ON m.link_id = l.link_id 
        WHERE m.to_user_id = ? 
        ORDER BY m.created_at DESC LIMIT ?
    ''', (user_id, limit), fetch="all")

def get_message_replies(message_id):
    """Получает все ответы на сообщение"""
    return run_query('''
        SELECT r.reply_text, r.created_at, u.username, u.first_name
        FROM replies r
        LEFT JOIN users u ON r.from_user_id = u.user_id
        WHERE r.message_id = ?
        ORDER BY r.created_at ASC
    ''', (message_id,), fetch="all")

def get_full_history_for_admin(user_id):
    return run_query('''
        SELECT m.message_id, m.message_text, m.message_type, m.file_id, m.file_size, m.file_name,
               m.created_at, u_from.username as from_username, u_from.first_name as from_first_name,
               u_to.username as to_username, u_to.first_name as to_first_name,
               l.title as link_title
        FROM messages m 
        LEFT JOIN users u_from ON m.from_user_id = u_from.user_id 
        LEFT JOIN users u_to ON m.to_user_id = u_to.user_id
        LEFT JOIN links l ON m.link_id = l.link_id
        WHERE m.from_user_id = ? OR m.to_user_id = ? 
        ORDER BY m.created_at ASC
    ''', (user_id, user_id), fetch="all")

def get_all_users_for_admin():
    return run_query("SELECT user_id, username, first_name, created_at FROM users ORDER BY created_at DESC", fetch="all")

def get_admin_stats():
    stats = {}
    stats['users'] = run_query("SELECT COUNT(*) FROM users", fetch="one")[0]
    stats['links'] = run_query("SELECT COUNT(*) FROM links WHERE is_active = 1", fetch="one")[0]
    stats['messages'] = run_query("SELECT COUNT(*) FROM messages", fetch="one")[0]
    stats['replies'] = run_query("SELECT COUNT(*) FROM replies", fetch="one")[0]
    
    # Статистика по типам файлов
    stats['photos'] = run_query("SELECT COUNT(*) FROM messages WHERE message_type = 'photo'", fetch="one")[0]
    stats['videos'] = run_query("SELECT COUNT(*) FROM messages WHERE message_type = 'video'", fetch="one")[0]
    stats['documents'] = run_query("SELECT COUNT(*) FROM messages WHERE message_type = 'document'", fetch="one")[0]
    stats['voice'] = run_query("SELECT COUNT(*) FROM messages WHERE message_type = 'voice'", fetch="one")[0]
    
    return stats

def get_all_data_for_html():
    """Получает все данные для генерации HTML отчета"""
    data = {}
    
    # Основная статистика
    data['stats'] = get_admin_stats()
    
    # Пользователи
    data['users'] = run_query('''
        SELECT u.user_id, u.username, u.first_name, u.created_at,
               (SELECT COUNT(*) FROM links l WHERE l.user_id = u.user_id) as link_count,
               (SELECT COUNT(*) FROM messages m WHERE m.to_user_id = u.user_id) as received_messages,
               (SELECT COUNT(*) FROM messages m WHERE m.from_user_id = u.user_id) as sent_messages
        FROM users u
        ORDER BY u.created_at DESC
    ''', fetch="all")
    
    # Ссылки
    data['links'] = run_query('''
        SELECT l.link_id, l.title, l.description, l.created_at, l.expires_at,
               u.username, u.first_name,
               (SELECT COUNT(*) FROM messages m WHERE m.link_id = l.link_id) as message_count
        FROM links l
        LEFT JOIN users u ON l.user_id = u.user_id
        WHERE l.is_active = 1
        ORDER BY l.created_at DESC
    ''', fetch="all")
    
    # Последние сообщения
    data['recent_messages'] = run_query('''
        SELECT m.message_id, m.message_text, m.message_type, m.file_size, m.file_name, m.created_at,
               u_from.username as from_username, u_from.first_name as from_first_name,
               u_to.username as to_username, u_to.first_name as to_first_name,
               l.title as link_title
        FROM messages m
        LEFT JOIN users u_from ON m.from_user_id = u_from.user_id
        LEFT JOIN users u_to ON m.to_user_id = u_to.user_id
        LEFT JOIN links l ON m.link_id = l.link_id
        ORDER BY m.created_at DESC
        LIMIT 100
    ''', fetch="all")
    
    return data

def generate_html_report():
    """Генерирует красивый HTML отчет"""
    data = get_all_data_for_html()
    
    html_content = f'''
    <!DOCTYPE html>
    <html lang="ru">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Анонимный Бот - Панель Администратора</title>
        <style>
            * {{
                margin: 0;
                padding: 0;
                box-sizing: border-box;
            }}
            
            body {{
                font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                min-height: 100vh;
                padding: 20px;
            }}
            
            .container {{
                max-width: 1200px;
                margin: 0 auto;
            }}
            
            .header {{
                background: rgba(255, 255, 255, 0.1);
                backdrop-filter: blur(10px);
                padding: 30px;
                border-radius: 20px;
                margin-bottom: 30px;
                text-align: center;
                border: 1px solid rgba(255, 255, 255, 0.2);
            }}
            
            .header h1 {{
                color: white;
                font-size: 2.5em;
                margin-bottom: 10px;
                text-shadow: 2px 2px 4px rgba(0,0,0,0.3);
            }}
            
            .header .subtitle {{
                color: #e0e0ff;
                font-size: 1.2em;
            }}
            
            .stats-grid {{
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
                gap: 20px;
                margin-bottom: 30px;
            }}
            
            .stat-card {{
                background: rgba(255, 255, 255, 0.1);
                backdrop-filter: blur(10px);
                padding: 25px;
                border-radius: 15px;
                text-align: center;
                border: 1px solid rgba(255, 255, 255, 0.2);
                transition: transform 0.3s ease, box-shadow 0.3s ease;
            }}
            
            .stat-card:hover {{
                transform: translateY(-5px);
                box-shadow: 0 10px 25px rgba(0,0,0,0.2);
            }}
            
            .stat-card h3 {{
                color: #ffd700;
                font-size: 2.5em;
                margin-bottom: 10px;
                font-weight: bold;
            }}
            
            .stat-card p {{
                color: white;
                font-size: 0.9em;
                opacity: 0.9;
            }}
            
            .section {{
                background: rgba(255, 255, 255, 0.1);
                backdrop-filter: blur(10px);
                padding: 25px;
                border-radius: 15px;
                margin-bottom: 30px;
                border: 1px solid rgba(255, 255, 255, 0.2);
            }}
            
            .section h2 {{
                color: white;
                margin-bottom: 20px;
                font-size: 1.5em;
                border-bottom: 2px solid rgba(255,255,255,0.3);
                padding-bottom: 10px;
            }}
            
            table {{
                width: 100%;
                border-collapse: collapse;
                background: rgba(255, 255, 255, 0.05);
                border-radius: 10px;
                overflow: hidden;
            }}
            
            th, td {{
                padding: 12px 15px;
                text-align: left;
                border-bottom: 1px solid rgba(255,255,255,0.1);
            }}
            
            th {{
                background: rgba(255,255,255,0.1);
                color: #ffd700;
                font-weight: 600;
            }}
            
            td {{
                color: white;
            }}
            
            tr:hover {{
                background: rgba(255,255,255,0.05);
            }}
            
            .badge {{
                display: inline-block;
                padding: 3px 8px;
                border-radius: 12px;
                font-size: 0.8em;
                font-weight: bold;
            }}
            
            .badge-success {{
                background: #4CAF50;
                color: white;
            }}
            
            .badge-info {{
                background: #2196F3;
                color: white;
            }}
            
            .badge-warning {{
                background: #FF9800;
                color: white;
            }}
            
            .timestamp {{
                font-family: monospace;
                font-size: 0.85em;
                opacity: 0.8;
            }}
            
            @keyframes fadeIn {{
                from {{ opacity: 0; transform: translateY(20px); }}
                to {{ opacity: 1; transform: translateY(0); }}
            }}
            
            .fade-in {{
                animation: fadeIn 0.6s ease-out;
            }}
            
            .file-type {{
                display: inline-block;
                width: 20px;
                text-align: center;
                margin-right: 5px;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header fade-in">
                <h1>🛠️ Панель Администратора</h1>
                <div class="subtitle">Анонимный Бот - Полная статистика и мониторинг</div>
                <div class="timestamp">Отчет сгенерирован: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</div>
            </div>
            
            <div class="stats-grid">
                <div class="stat-card fade-in">
                    <h3>{data['stats']['users']}</h3>
                    <p>👥 Всего пользователей</p>
                </div>
                <div class="stat-card fade-in">
                    <h3>{data['stats']['links']}</h3>
                    <p>🔗 Активных ссылок</p>
                </div>
                <div class="stat-card fade-in">
                    <h3>{data['stats']['messages']}</h3>
                    <p>📨 Всего сообщений</p>
                </div>
                <div class="stat-card fade-in">
                    <h3>{data['stats']['replies']}</h3>
                    <p>💬 Ответов</p>
                </div>
            </div>
            
            <div class="stats-grid">
                <div class="stat-card fade-in">
                    <h3>{data['stats']['photos']}</h3>
                    <p>🖼️ Фотографий</p>
                </div>
                <div class="stat-card fade-in">
                    <h3>{data['stats']['videos']}</h3>
                    <p>🎥 Видео</p>
                </div>
                <div class="stat-card fade-in">
                    <h3>{data['stats']['documents']}</h3>
                    <p>📄 Документов</p>
                </div>
                <div class="stat-card fade-in">
                    <h3>{data['stats']['voice']}</h3>
                    <p>🎤 Голосовых</p>
                </div>
            </div>
            
            <div class="section fade-in">
                <h2>👥 Последние пользователи</h2>
                <table>
                    <thead>
                        <tr>
                            <th>ID</th>
                            <th>Username</th>
                            <th>Имя</th>
                            <th>Регистрация</th>
                            <th>Ссылки</th>
                            <th>Сообщения</th>
                        </tr>
                    </thead>
                    <tbody>
    '''
    
    for user in data['users'][:10]:
        html_content += f'''
                        <tr>
                            <td>{user[0]}</td>
                            <td>@{user[1] if user[1] else 'N/A'}</td>
                            <td>{html.escape(user[2]) if user[2] else 'N/A'}</td>
                            <td class="timestamp">{user[3]}</td>
                            <td><span class="badge badge-info">{user[4]}</span></td>
                            <td>📨 {user[5]} | 📤 {user[6]}</td>
                        </tr>
        '''
    
    html_content += '''
                    </tbody>
                </table>
            </div>
            
            <div class="section fade-in">
                <h2>🔗 Активные ссылки</h2>
                <table>
                    <thead>
                        <tr>
                            <th>ID</th>
                            <th>Название</th>
                            <th>Владелец</th>
                            <th>Создана</th>
                            <th>Истекает</th>
                            <th>Сообщения</th>
                        </tr>
                    </thead>
                    <tbody>
    '''
    
    for link in data['links'][:15]:
        html_content += f'''
                        <tr>
                            <td><code>{link[0]}</code></td>
                            <td>{html.escape(link[1])}</td>
                            <td>@{link[5] if link[5] else html.escape(link[6])}</td>
                            <td class="timestamp">{link[3]}</td>
                            <td class="timestamp">{link[4]}</td>
                            <td><span class="badge badge-success">{link[7]}</span></td>
                        </tr>
        '''
    
    html_content += '''
                    </tbody>
                </table>
            </div>
            
            <div class="section fade-in">
                <h2>📨 Последние сообщения</h2>
                <table>
                    <thead>
                        <tr>
                            <th>ID</th>
                            <th>Тип</th>
                            <th>От</th>
                            <th>Кому</th>
                            <th>Ссылка</th>
                            <th>Время</th>
                            <th>Размер</th>
                        </tr>
                    </thead>
                    <tbody>
    '''
    
    for msg in data['recent_messages'][:20]:
        msg_type_icon = {
            'text': '📝',
            'photo': '🖼️',
            'video': '🎥',
            'document': '📄',
            'voice': '🎤'
        }.get(msg[2], '📄')
        
        file_size = f"{msg[3] // 1024} KB" if msg[3] else '-'
        from_user = f"@{msg[6]}" if msg[6] else (html.escape(msg[7]) if msg[7] else f"ID:{msg[0]}")
        to_user = f"@{msg[8]}" if msg[8] else (html.escape(msg[9]) if msg[9] else f"ID:{msg[0]}")
        
        html_content += f'''
                        <tr>
                            <td>#{msg[0]}</td>
                            <td>{msg_type_icon} {msg[2]}</td>
                            <td>{from_user}</td>
                            <td>{to_user}</td>
                            <td>{html.escape(msg[10]) if msg[10] else 'N/A'}</td>
                            <td class="timestamp">{msg[5]}</td>
                            <td>{file_size}</td>
                        </tr>
        '''
    
    html_content += '''
                    </tbody>
                </table>
            </div>
            
            <div class="header" style="text-align: center; margin-top: 40px;">
                <div class="subtitle">🟣 Анонимный Бот | Создано с ❤️ для Sirok</div>
            </div>
        </div>
        
        <script>
            // Добавляем анимацию при прокрутке
            const observerOptions = {{
                threshold: 0.1
            }};
            
            const observer = new IntersectionObserver((entries) => {{
                entries.forEach(entry => {{
                    if (entry.isIntersecting) {{
                        entry.target.style.animation = 'fadeIn 0.6s ease-out forwards';
                    }}
                }});
            }}, observerOptions);
            
            document.querySelectorAll('.section, .stat-card').forEach(el => {{
                observer.observe(el);
            }});
        </script>
    </body>
    </html>
    '''
    
    return html_content

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

def escape_markdown(text: str) -> str:
    if not text: return ""
    return re.sub(r'([_*\[\]()~`>#+\-=|{}.!])', r'\\\1', str(text))

def format_as_quote(text: str) -> str:
    if not text: return ""
    return '\n'.join([f"> {line}" for line in escape_markdown(text).split('\n')])

def format_datetime(dt_string):
    """Форматирует дату-время с точностью до секунд"""
    if isinstance(dt_string, str):
        return dt_string
    return dt_string.strftime("%Y-%m-%d %H:%M:%S") if hasattr(dt_string, 'strftime') else str(dt_string)

# --- КЛАВИАТУРЫ ---

def main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🟣 | 𝙰𝚞𝚋𝚎𝟷𝚐", callback_data="main_menu")],
        [InlineKeyboardButton("🔗 Мои ссылки", callback_data="my_links")],
        [InlineKeyboardButton("➕ Создать ссылку", callback_data="create_link")],
        [InlineKeyboardButton("📨 Мои сообщения", callback_data="my_messages")]
    ])

def message_details_keyboard(message_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Ответить", callback_data=f"reply_{message_id}")],
        [InlineKeyboardButton("📋 Просмотреть ответы", callback_data=f"view_replies_{message_id}")],
        [InlineKeyboardButton("🔙 Назад", callback_data="my_messages")]
    ])

def admin_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton("📜 История переписки", callback_data="admin_history")],
        [InlineKeyboardButton("📊 HTML Отчет", callback_data="admin_html_report")],
        [InlineKeyboardButton("📢 Оповещение", callback_data="admin_broadcast")],
        [InlineKeyboardButton("🔙 Главное меню", callback_data="main_menu")]
    ])

def back_to_main_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Главное меню", callback_data="main_menu")]])

# --- ОСНОВНЫЕ ОБРАБОТЧИКИ ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user = update.effective_user
        save_user(user.id, user.username, user.first_name)
        
        if context.args:
            link_id = context.args[0]
            link_info = get_link_info(link_id)
            if link_info:
                context.user_data['current_link'] = link_id
                text = f"🔗 *Анонимная ссылка*\n\n📝 *{escape_markdown(link_info[2])}*\n📋 {escape_markdown(link_info[3])}\n\n✍️ Напишите анонимное сообщение или отправьте медиафайл\\."
                await update.message.reply_text(text, parse_mode='MarkdownV2', reply_markup=back_to_main_keyboard())
                return
        
        text = "👋 *Добро пожаловать в Анонимный Бот\\!*\n\nСоздавайте ссылки для получения анонимных сообщений и вопросов\\."
        await update.message.reply_text(text, reply_markup=main_keyboard(), parse_mode='MarkdownV2')
    except Exception as e:
        logging.error(f"Ошибка в команде start: {e}")
        await update.message.reply_text("❌ Произошла ошибка. Попробуйте позже.")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        query = update.callback_query
        await query.answer()
        user = query.from_user
        data = query.data
        parts = data.split('_')
        command = parts[0]
        is_admin = user.username == ADMIN_USERNAME or user.id == ADMIN_ID

        if command == "main":
            await query.edit_message_text("🎭 *Главное меню*", reply_markup=main_keyboard(), parse_mode='MarkdownV2')
        
        elif command == "my":
            if data == "my_links":
                links = get_user_links(user.id)
                if links:
                    text = "🔗 *Ваши анонимные ссылки:*\n\n"
                    for link in links:
                        link_url = f"https://t.me/{context.bot.username}?start={link[0]}"
                        created = format_datetime(link[3])
                        text += f"📝 *{escape_markdown(link[1])}*\n📋 {escape_markdown(link[2])}\n🔗 `{link_url}`\n🕒 `{created}`\n\n"
                    await query.edit_message_text(text, parse_mode='MarkdownV2', reply_markup=back_to_main_keyboard())
                else:
                    await query.edit_message_text("У вас пока нет созданных ссылок\\.", reply_markup=back_to_main_keyboard(), parse_mode='MarkdownV2')
            
            elif data == "my_messages":
                messages = get_user_messages_with_replies(user.id)
                if messages:
                    text = "📨 *Ваши последние сообщения:*\n\n"
                    for msg in messages:
                        msg_id, msg_text, msg_type, file_id, file_size, file_name, created, link_title, link_id, reply_count = msg
                        
                        # Иконка типа сообщения
                        type_icon = {"text": "📝", "photo": "🖼️", "video": "🎥", "document": "📄", "voice": "🎤"}.get(msg_type, "📄")
                        
                        preview = msg_text or f"*{msg_type}*"
                        if len(preview) > 50:
                            preview = preview[:50] + "..."
                            
                        created_str = format_datetime(created)
                        text += f"{type_icon} *{escape_markdown(link_title)}*\n{format_as_quote(preview)}\n🕒 `{created_str}` | 💬 Ответов: {reply_count}\n\n"
                    
                    await query.edit_message_text(text, parse_mode='MarkdownV2', reply_markup=back_to_main_keyboard())
                else:
                    await query.edit_message_text("У вас пока нет сообщений\\.", parse_mode='MarkdownV2', reply_markup=back_to_main_keyboard())
        
        elif command == "create":
            context.user_data['creating_link'] = True
            context.user_data['link_stage'] = 'title'
            await query.edit_message_text("📝 Введите *название* для вашей ссылки:", parse_mode='MarkdownV2', reply_markup=back_to_main_keyboard())
        
        elif command == "reply":
            message_id = int(data.replace("reply_", ""))
            context.user_data['replying_to'] = message_id
            await query.edit_message_text(f"✍️ Введите ваш ответ на сообщение \\#{message_id}:", parse_mode='MarkdownV2', reply_markup=back_to_main_keyboard())
        
        elif command == "view":
            if data.startswith("view_replies_"):
                message_id = int(data.replace("view_replies_", ""))
                replies = get_message_replies(message_id)
                if replies:
                    text = f"💬 *Ответы на сообщение \\#{message_id}:*\n\n"
                    for reply in replies:
                        reply_text, created, username, first_name = reply
                        sender = f"@{username}" if username else (first_name or "Аноним")
                        created_str = format_datetime(created)
                        text += f"👤 *{escape_markdown(sender)}* \\(`{created_str}`\\):\n{format_as_quote(reply_text)}\n\n"
                    await query.edit_message_text(text, parse_mode='MarkdownV2', reply_markup=message_details_keyboard(message_id))
                else:
                    await query.edit_message_text(f"💬 На сообщение \\#{message_id} пока нет ответов\\.", parse_mode='MarkdownV2', reply_markup=message_details_keyboard(message_id))

        # АДМИН ПАНЕЛЬ
        if is_admin:
            if command == "admin":
                if data == "admin_stats":
                    stats = get_admin_stats()
                    text = f"""📊 *Статистика бота:*

👥 *Пользователи:*
• Всего пользователей: {stats['users']}
• Активных ссылок: {stats['links']}

💌 *Сообщения:*
• Всего сообщений: {stats['messages']}
• Ответов: {stats['replies']}

📁 *Файлы:*
• Фотографий: {stats['photos']}
• Видео: {stats['videos']}
• Документов: {stats['documents']}
• Голосовых: {stats['voice']}"""
                    await query.edit_message_text(text, parse_mode='MarkdownV2', reply_markup=admin_keyboard())
                
                elif data == "admin_history":
                    users = get_all_users_for_admin()
                    if users:
                        kb = [[InlineKeyboardButton(f"👤 {u[1] or u[2] or f'ID: {u[0]}'}", callback_data=f"admin_view_user:{u[0]}")] for u in users[:20]]
                        kb.append([InlineKeyboardButton("🔙 Назад", callback_data="admin_panel")])
                        await query.edit_message_text("👥 *Выберите пользователя для просмотра истории:*", reply_markup=InlineKeyboardMarkup(kb))
                    else:
                        await query.edit_message_text("Пользователей не найдено\\.", parse_mode='MarkdownV2', reply_markup=admin_keyboard())
                
                elif data == "admin_html_report":
                    # Генерируем HTML отчет
                    html_content = generate_html_report()
                    
                    # Сохраняем временный файл
                    report_path = "/tmp/admin_report.html"
                    with open(report_path, 'w', encoding='utf-8') as f:
                        f.write(html_content)
                    
                    # Отправляем файл
                    with open(report_path, 'rb') as f:
                        await query.message.reply_document(
                            document=f,
                            filename=f"admin_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html",
                            caption="📊 *Полный HTML отчет администратора*\nОткройте файл в браузере для просмотра красивой статистики\\!",
                            parse_mode='MarkdownV2'
                        )
                    
                    await query.edit_message_text("📊 HTML отчет сгенерирован и отправлен\\!", parse_mode='MarkdownV2', reply_markup=admin_keyboard())
                
                elif data == "admin_broadcast":
                    context.user_data['broadcasting'] = True
                    await query.edit_message_text("📢 Введите сообщение для рассылки всем пользователям:", reply_markup=back_to_main_keyboard())
            
            elif command == "admin_view":
                if data.startswith("admin_view_user:"):
                    user_id = int(data.split(":")[1])
                    history = get_full_history_for_admin(user_id)
                    
                    if not history:
                        await query.edit_message_text("_История сообщений не найдена\\._", parse_mode='MarkdownV2', reply_markup=admin_keyboard())
                        return
                    
                    await query.edit_message_text(f"📜 *История переписки для пользователя ID {user_id}*\n*Всего сообщений: {len(history)}*", parse_mode='MarkdownV2')
                    
                    # Отправляем сообщения по частям
                    for i, msg in enumerate(history[:10]):  # Ограничиваем первые 10 сообщений
                        msg_id, msg_text, msg_type, file_id, file_size, file_name, created, from_user, from_name, to_user, to_name, link_title = msg
                        
                        created_str = format_datetime(created)
                        header = f"*#{i+1}* | 🕒 `{created_str}`\n"
                        header += f"*От:* {escape_markdown(from_user or from_name or 'Аноним')}\n"
                        header += f"*Кому:* {escape_markdown(to_user or to_name or 'Аноним')}\n"
                        header += f"*Ссылка:* {escape_markdown(link_title or 'N/A')}\n"
                        
                        if msg_type == 'text':
                            await query.message.reply_text(f"{header}\n{format_as_quote(msg_text)}", parse_mode='MarkdownV2')
                        else:
                            file_info = f"\n*Тип:* {msg_type}"
                            if file_size:
                                file_info += f" \\({(file_size or 0) // 1024} KB\\)"
                            if file_name:
                                file_info += f"\n*Файл:* {escape_markdown(file_name)}"
                            
                            caption = f"{header}{file_info}"
                            
                            try:
                                if msg_type == 'photo': 
                                    await query.message.reply_photo(file_id, caption=caption, parse_mode='MarkdownV2')
                                elif msg_type == 'video': 
                                    await query.message.reply_video(file_id, caption=caption, parse_mode='MarkdownV2')
                                elif msg_type == 'document': 
                                    await query.message.reply_document(file_id, caption=caption, parse_mode='MarkdownV2')
                                elif msg_type == 'voice': 
                                    await query.message.reply_voice(file_id, caption=caption, parse_mode='MarkdownV2')
                            except Exception as e:
                                await query.message.reply_text(f"{header}\n*Файл недоступен:* {escape_markdown(str(e))}", parse_mode='MarkdownV2')
                    
                    if len(history) > 10:
                        await query.message.reply_text(f"*... и ещё {len(history) - 10} сообщений*\n_Для полного просмотра используйте HTML отчет_", parse_mode='MarkdownV2')

    except Exception as e:
        logging.error(f"Ошибка в обработчике кнопок: {e}")
        try:
            await query.edit_message_text("❌ Произошла ошибка. Попробуйте позже.", reply_markup=main_keyboard())
        except:
            pass

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user = update.effective_user
        text = update.message.text
        save_user(user.id, user.username, user.first_name)
        is_admin = user.username == ADMIN_USERNAME or user.id == ADMIN_ID

        # Проверка пароля для админки
        if text == ADMIN_PASSWORD and is_admin:
            context.user_data['admin_authenticated'] = True
            await update.message.reply_text("✅ *Пароль принят! Добро пожаловать в админ-панель.*", 
                                          reply_markup=admin_keyboard(), parse_mode='MarkdownV2')
            return

        # Ответ на сообщение
        if 'replying_to' in context.user_data:
            msg_id = context.user_data.pop('replying_to')
            save_reply(msg_id, user.id, text)
            original_msg = run_query("SELECT m.from_user_id, m.message_text FROM messages m WHERE m.message_id = ?", (msg_id,), fetch="one")
            if original_msg:
                try:
                    reply_notification = f"💬 *Получен ответ на ваше сообщение:*\n{format_as_quote(original_msg[1])}\n\n*Ответ:*\n{format_as_quote(text)}"
                    await context.bot.send_message(original_msg[0], reply_notification, parse_mode='MarkdownV2')
                except Exception as e:
                    logging.error(f"Failed to send reply notification: {e}")
            await update.message.reply_text("✅ Ваш ответ отправлен анонимно!", reply_markup=main_keyboard())
            return

        # Создание ссылки
        if context.user_data.get('creating_link'):
            stage = context.user_data.get('link_stage')
            if stage == 'title':
                context.user_data['link_title'] = text
                context.user_data['link_stage'] = 'description'
                await update.message.reply_text("📋 Теперь введите *описание* для ссылки:", parse_mode='MarkdownV2')
            elif stage == 'description':
                title = context.user_data.pop('link_title')
                context.user_data.pop('creating_link')
                context.user_data.pop('link_stage')
                link_id = create_anon_link(user.id, title, text)
                link_url = f"https://t.me/{context.bot.username}?start={link_id}"
                await update.message.reply_text(f"✅ *Ссылка создана\\!*\n\n📝 *{escape_markdown(title)}*\n📋 {escape_markdown(text)}\n\n🔗 `{link_url}`\n\nПоделитесь ей, чтобы получать сообщения\\!", parse_mode='MarkdownV2', reply_markup=main_keyboard())
            return

        # Рассылка от админа
        if is_admin and context.user_data.get('broadcasting') and context.user_data.get('admin_authenticated'):
            context.user_data.pop('broadcasting')
            users = run_query("SELECT user_id FROM users", fetch="all")
            sent_count = 0
            if users:
                for u in users:
                    try:
                        await context.bot.send_message(u[0], text, parse_mode='MarkdownV2')
                        sent_count += 1
                    except Exception as e:
                        logging.warning(f"Broadcast failed for user {u[0]}: {e}")
            await update.message.reply_text(f"📢 Рассылка завершена. Отправлено {sent_count}/{len(users) if users else 0} пользователям.", reply_markup=admin_keyboard())
            return

        # Отправка анонимного сообщения
        if context.user_data.get('current_link'):
            link_id = context.user_data.pop('current_link')
            link_info = get_link_info(link_id)
            if link_info:
                msg_id = save_message(link_id, user.id, link_info[1], text)
                notification = f"📨 *Новое анонимное сообщение*\n\n{format_as_quote(text)}"
                try:
                    await context.bot.send_message(link_info[1], notification, parse_mode='MarkdownV2', reply_markup=message_details_keyboard(msg_id))
                except Exception as e:
                    logging.error(f"Failed to send message notification: {e}")
                
                # Уведомление админу
                admin_notification = f"📨 *Новое сообщение*\nОт: {escape_markdown(user.username or user.first_name or 'Аноним')} -> Кому: {escape_markdown(link_info[4] or 'Аноним')}\n\n{format_as_quote(text)}"
                await context.bot.send_message(ADMIN_ID, admin_notification, parse_mode='MarkdownV2')
                
                await update.message.reply_text("✅ Ваше сообщение отправлено анонимно!", reply_markup=main_keyboard())
            return

        await update.message.reply_text("Используйте кнопки для навигации.", reply_markup=main_keyboard())

    except Exception as e:
        logging.error(f"Ошибка в обработчике текста: {e}")
        await update.message.reply_text("❌ Произошла ошибка. Попробуйте позже.")

async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user = update.effective_user
        save_user(user.id, user.username, user.first_name)
        msg = update.message
        caption = msg.caption or ""
        file_id, msg_type, file_size, file_name = None, "unknown", None, None

        if msg.photo: 
            file_id, msg_type = msg.photo[-1].file_id, "photo"
            file_size = msg.photo[-1].file_size
        elif msg.video: 
            file_id, msg_type = msg.video.file_id, "video"
            file_size = msg.video.file_size
            file_name = msg.video.file_name
        elif msg.voice: 
            file_id, msg_type = msg.voice.file_id, "voice"
            file_size = msg.voice.file_size
        elif msg.document: 
            file_id, msg_type = msg.document.file_id, "document"
            file_size = msg.document.file_size
            file_name = msg.document.file_name

        if context.user_data.get('current_link') and file_id:
            link_id = context.user_data.pop('current_link')
            link_info = get_link_info(link_id)
            if link_info:
                msg_id = save_message(link_id, user.id, link_info[1], caption, msg_type, file_id, file_size, file_name)
                
                # Подготовка информации о файле
                file_info = ""
                if file_size:
                    file_info = f" \\({(file_size or 0) // 1024} KB\\)"
                if file_name:
                    file_info += f"\n📄 `{escape_markdown(file_name)}`"
                
                user_caption = f"📨 *Новый анонимный {msg_type}*{file_info}\n\n{format_as_quote(caption)}"
                admin_caption = f"📨 *Новый {msg_type}*\nОт: {escape_markdown(user.username or user.first_name or 'Аноним')} -> Кому: {escape_markdown(link_info[4] or 'Аноним')}{file_info}\n\n{format_as_quote(caption)}"
                
                try:
                    if msg_type == 'photo': 
                        await context.bot.send_photo(link_info[1], file_id, caption=user_caption, parse_mode='MarkdownV2', reply_markup=message_details_keyboard(msg_id))
                    elif msg_type == 'video': 
                        await context.bot.send_video(link_info[1], file_id, caption=user_caption, parse_mode='MarkdownV2', reply_markup=message_details_keyboard(msg_id))
                    elif msg_type == 'document': 
                        await context.bot.send_document(link_info[1], file_id, caption=user_caption, parse_mode='MarkdownV2', reply_markup=message_details_keyboard(msg_id))
                    elif msg_type == 'voice': 
                        await context.bot.send_voice(link_info[1], file_id, caption=user_caption, parse_mode='MarkdownV2', reply_markup=message_details_keyboard(msg_id))
                except Exception as e: 
                    logging.error(f"Failed to send media to user: {e}")

                try:
                    if msg_type in ['photo', 'video', 'document']:
                        if msg_type == 'photo': 
                            await context.bot.send_photo(ADMIN_ID, file_id, caption=admin_caption, parse_mode='MarkdownV2')
                        elif msg_type == 'video': 
                            await context.bot.send_video(ADMIN_ID, file_id, caption=admin_caption, parse_mode='MarkdownV2')
                        elif msg_type == 'document': 
                            await context.bot.send_document(ADMIN_ID, file_id, caption=admin_caption, parse_mode='MarkdownV2')
                    elif msg_type == 'voice':
                        await context.bot.send_voice(ADMIN_ID, file_id, caption=admin_caption, parse_mode='MarkdownV2')
                except Exception as e: 
                    logging.error(f"Failed to send media to admin: {e}")
                
                await update.message.reply_text("✅ Ваше медиа отправлено анонимно!", reply_markup=main_keyboard())

    except Exception as e:
        logging.error(f"Ошибка в обработчике медиа: {e}")
        await update.message.reply_text("❌ Произошла ошибка при отправке медиа.")

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user = update.effective_user
        if user.username == ADMIN_USERNAME or user.id == ADMIN_ID:
            if context.user_data.get('admin_authenticated'):
                await update.message.reply_text("🛠️ *Панель администратора*", reply_markup=admin_keyboard(), parse_mode='MarkdownV2')
            else:
                await update.message.reply_text("🔐 *Требуется пароль для доступа к админ-панели*\nВведите пароль:", parse_mode='MarkdownV2')
        else:
            await update.message.reply_text("⛔️ Доступ запрещен\\.", parse_mode='MarkdownV2')
    except Exception as e:
        logging.error(f"Ошибка в команде admin: {e}")

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик ошибок."""
    logging.error(f"Exception while handling an update: {context.error}", exc_info=context.error)

async def post_init(application: Application):
    """Функция, выполняемая после инициализации бота."""
    logging.info("Бот успешно инициализирован и готов к работе")

def main():
    if not all([BOT_TOKEN, ADMIN_ID, GITHUB_TOKEN, GITHUB_REPO, DB_FILENAME]):
        logging.critical("КРИТИЧЕСКАЯ ОШИБКА: Не установлены все переменные окружения")
        return
    
    # Настройка репозитория и БД
    setup_repo()
    init_db()
    
    # Создание приложения
    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    
    # Добавление обработчиков
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("admin", admin_panel))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    media_filters = filters.PHOTO | filters.VIDEO | filters.VOICE | filters.Document.ALL
    application.add_handler(MessageHandler(media_filters & ~filters.COMMAND, handle_media))
    
    # Добавление обработчика ошибок
    application.add_error_handler(error_handler)
    
    logging.info("Бот запускается...")
    
    try:
        application.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=False,  # Не удаляем pending updates чтобы не терять данные
            close_loop=False
        )
    except Exception as e:
        logging.critical(f"Критическая ошибка при запуске бота: {e}")

if __name__ == "__main__":
    main()
