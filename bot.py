#!/usr/bin/env python3
import os
import sys
import subprocess
import threading
import time
import shutil
import zipfile
import tarfile
import sqlite3
import ast
import importlib
import importlib.util
import html as html_lib
import logging
from datetime import datetime

# Auto install required base packages
def install_requirements():
    requirements = ["pyTelegramBotAPI", "requests", "psutil"]
    for package in requirements:
        try:
            if package == "pyTelegramBotAPI":
                import telebot
            elif package == "psutil":
                import psutil
            elif package == "requests":
                import requests
        except ImportError:
            print(f"📦 Installing base dependency: {package}...")
            try:
                subprocess.check_call([sys.executable, "-m", "pip", "install", package])
            except Exception as e:
                print(f"❌ Failed to install {package}: {e}")

install_requirements()

import psutil
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton

# Setup logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# আপনার টেলিগ্রাম বট টোকেন
BOT_TOKEN = "8615086853:AAFsZVFxoP2T8XsA9Bv6qcIyHibEwo-UAJA"

# Application directories
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "metadata.db")
UPLOADS_DIR = os.path.join(DATA_DIR, "uploads")
LOGS_DIR = os.path.join(DATA_DIR, "logs")
TEMP_DIR = os.path.join(DATA_DIR, "temp")

for directory in [DATA_DIR, UPLOADS_DIR, LOGS_DIR, TEMP_DIR]:
    os.makedirs(directory, exist_ok=True)

START_TIME = datetime.utcnow()

# Database setup
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.row_factory = sqlite3.Row
db_lock = threading.Lock()

def init_db():
    with db_lock:
        cur = conn.cursor()
        cur.execute('''
            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT,
                filename TEXT,
                orig_name TEXT,
                path TEXT,
                uploaded_at TEXT,
                file_type TEXT,
                pid INTEGER,
                status TEXT DEFAULT 'Stopped'
            )
        ''')
        cur.execute('''
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id INTEGER,
                started_at TEXT,
                finished_at TEXT,
                pid INTEGER,
                log_path TEXT,
                exit_code INTEGER
            )
        ''')
        cur.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                joined_at TEXT,
                last_seen TEXT
            )
        ''')
        # Reset any stuck 'Running' statuses on server startup
        cur.execute("UPDATE files SET status='Stopped', pid=NULL WHERE status='Running'")
        conn.commit()

init_db()

# Database helpers
def add_file_record(user_id, username, filename, orig_name, path, file_type):
    with db_lock:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO files (user_id, username, filename, orig_name, path, uploaded_at, file_type) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, username, filename, orig_name, path, datetime.utcnow().isoformat(), file_type)
        )
        conn.commit()
        return cur.lastrowid

def list_user_files(user_id):
    with db_lock:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, filename, orig_name, uploaded_at, file_type, status, pid FROM files WHERE user_id=? ORDER BY id DESC",
            (user_id,)
        )
        return cur.fetchall()

def get_file_record(file_id):
    with db_lock:
        cur = conn.cursor()
        cur.execute("SELECT * FROM files WHERE id=?", (file_id,))
        return cur.fetchone()

def remove_file_record(file_id):
    with db_lock:
        cur = conn.cursor()
        cur.execute("DELETE FROM files WHERE id=?", (file_id,))
        conn.commit()

def record_run_start(file_id, pid, log_path):
    with db_lock:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO runs (file_id, started_at, pid, log_path) VALUES (?, ?, ?, ?)",
            (file_id, datetime.utcnow().isoformat(), pid, log_path)
        )
        conn.commit()
        return cur.lastrowid

def record_run_finish(run_id, exit_code):
    with db_lock:
        cur = conn.cursor()
        cur.execute(
            "UPDATE runs SET finished_at=?, exit_code=? WHERE id=?",
            (datetime.utcnow().isoformat(), exit_code, run_id)
        )
        conn.commit()

def update_file_status(file_id, pid, status):
    with db_lock:
        cur = conn.cursor()
        cur.execute(
            "UPDATE files SET pid=?, status=? WHERE id=?",
            (pid, status, file_id)
        )
        conn.commit()

# Process management
processes = {}
proc_lock = threading.Lock()

def get_system_load():
    try:
        cpu = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory().percent
        with proc_lock:
            proc_count = len(processes)
        return float(cpu), float(mem), int(proc_count)
    except Exception as e:
        logger.error(f"Error getting system load: {e}")
        return 0.0, 0.0, 0

def get_file_type(filename):
    if not filename:
        return "unknown"
    name = filename.lower()
    if name.endswith(".py"):
        return "python"
    if name.endswith(".js"):
        return "javascript"
    if name.endswith(".zip"):
        return "zip"
    if any(name.endswith(ext) for ext in [".tar", ".tar.gz", ".tgz"]):
        return "archive"
    return "unknown"

def extract_archive(file_path, extract_dir):
    try:
        if file_path.lower().endswith(".zip"):
            with zipfile.ZipFile(file_path, 'r') as zip_ref:
                zip_ref.extractall(extract_dir)
        elif any(file_path.lower().endswith(ext) for ext in [".tar.gz", ".tgz"]):
            with tarfile.open(file_path, 'r:gz') as tar_ref:
                tar_ref.extractall(extract_dir)
        elif file_path.lower().endswith(".tar"):
            with tarfile.open(file_path, 'r') as tar_ref:
                tar_ref.extractall(extract_dir)
        else:
            return False, "Unsupported archive format"
        return True, None
    except Exception as e:
        return False, str(e)

def find_main_file(directory):
    priority_files = [
        "main.py", "bot.py", "app.py", "server.py", "index.py", "script.py",
        "main.js", "bot.js", "app.js", "server.js", "index.js", "script.js"
    ]
    for file_name in priority_files:
        file_path = os.path.join(directory, file_name)
        if os.path.isfile(file_path):
            return file_path
    
    for root, _, files in os.walk(directory):
        for file_name in priority_files:
            if file_name in files:
                return os.path.join(root, file_name)
    
    for root, _, files in os.walk(directory):
        for file in files:
            if file.endswith(".py") or file.endswith(".js"):
                return os.path.join(root, file)
    return None

def install_requirements_from_file(requirements_path):
    try:
        if not os.path.exists(requirements_path):
            return False, "requirements.txt not found"
        
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", requirements_path],
            capture_output=True, text=True, timeout=300
        )
        if result.returncode == 0:
            return True, "Requirements installed successfully"
        return False, result.stderr[:200]
    except Exception as e:
        return False, str(e)

def extract_imports(file_path):
    imports = set()
    try:
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
        tree = ast.parse(content)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.add(alias.name.split('.')[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.level == 0:
                    imports.add(node.module.split('.')[0])
    except Exception:
        pass
    return imports

def install_missing_imports(imports):
    missing = []
    for module in imports:
        try:
            importlib.import_module(module)
        except ImportError:
            missing.append(module)
    
    if not missing:
        return True, "All modules already installed"
    
    # জনপ্রিয় মডিউলগুলোর সঠিক PIP প্যাকেজ নেম ম্যাপিং
    pip_name_map = {
        'telebot': 'pyTelegramBotAPI',
        'telegram': 'python-telegram-bot',
        'PIL': 'Pillow',
        'cv2': 'opencv-python',
        'Crypto': 'pycryptodome',
        'bs4': 'beautifulsoup4',
        'dotenv': 'python-dotenv',
        'yt_dlp': 'yt-dlp',
        'discord': 'discord.py',
        'aiogram': 'aiogram',
        'pyrogram': 'pyrogram',
        'tgcrypto': 'tgcrypto'
    }
    
    installed = []
    for module in missing:
        pkg = pip_name_map.get(module, module)
        try:
            subprocess.run([sys.executable, "-m", "pip", "install", pkg], capture_output=True, timeout=120)
            installed.append(pkg)
        except Exception:
            pass
    
    return True, f"Installed: {', '.join(installed) if installed else 'None'}"

# Telegram Bot Initializer
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# Main Keyboard Menu (সংশোধিত: আপডেট ও কন্টাক্ট বাটন বাদ দেওয়া হয়েছে)
def main_menu_kb():
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(KeyboardButton("📤 Upload File"), KeyboardButton("📁 My Files"))
    kb.add(KeyboardButton("⚡ Bot Speed"), KeyboardButton("📊 Statistics"))
    return kb

def file_actions_kb(file_id, is_running=False):
    kb = InlineKeyboardMarkup()
    if is_running:
        kb.row(
            InlineKeyboardButton("⏹ Stop", callback_data=f"stop:{file_id}"),
            InlineKeyboardButton("🔁 Restart", callback_data=f"restart:{file_id}")
        )
    else:
        kb.row(
            InlineKeyboardButton("▶️ Start", callback_data=f"start:{file_id}"),
            InlineKeyboardButton("🔁 Restart", callback_data=f"restart:{file_id}")
        )
    kb.row(
        InlineKeyboardButton("🗑 Delete", callback_data=f"delete:{file_id}"),
        InlineKeyboardButton("📄 Logs", callback_data=f"logs:{file_id}")
    )
    kb.row(InlineKeyboardButton("⬅️ Back", callback_data="back_to_files"))
    return kb

# Handlers
@bot.message_handler(commands=['start', 'help'])
def start_handler(message):
    user = message.from_user
    user_id = user.id
    
    with db_lock:
        cur = conn.cursor()
        cur.execute(
            "INSERT OR REPLACE INTO users (user_id, username, joined_at, last_seen) VALUES (?, ?, ?, ?)",
            (user_id, user.username or "", datetime.utcnow().isoformat(), datetime.utcnow().isoformat())
        )
        conn.commit()
    
    welcome_text = f"""
🔥 <b>24x7 Unlimited Hosting Server</b>

👋 Welcome <b>{html_lib.escape(user.first_name or 'User')}</b>
🆔 Your ID: <code>{user_id}</code>
📂 Limit: <b>Unlimited Bots</b>

🤖 <b>Features:</b>
• Host Python (.py) & NodeJS (.js) scripts
• Auto-install requirements & dependencies
• 24/7 Background Run
• Live Real-time logs

👇 নীচের বাটনগুলো ব্যবহার করে ফাইল আপলোড ও কন্ট্রোল করুন!
    """
    bot.send_message(message.chat.id, welcome_text, reply_markup=main_menu_kb())

@bot.message_handler(func=lambda m: m.text == "⚡ Bot Speed")
def speed_handler(message):
    cpu, memory, processes_count = get_system_load()
    uptime_td = datetime.utcnow() - START_TIME
    days = uptime_td.days
    hours, remainder = divmod(uptime_td.seconds, 3600)
    minutes, _ = divmod(remainder, 60)
    
    bot.send_message(
        message.chat.id,
        f"⚡ <b>System Status</b>\n\n"
        f"• CPU Usage: <code>{cpu:.1f}%</code>\n"
        f"• RAM Usage: <code>{memory:.1f}%</code>\n"
        f"• Active Bots Running: <code>{processes_count}</code>\n"
        f"• Max Limit: <b>Unlimited 🚀</b>\n"
        f"• Server Uptime: <code>{days}d {hours}h {minutes}m</code>"
    )

@bot.message_handler(func=lambda m: m.text == "📊 Statistics")
def stats_handler(message):
    with db_lock:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(DISTINCT user_id) FROM users")
        user_count = cur.fetchone()[0] or 0
        cur.execute("SELECT COUNT(*) FROM files")
        file_count = cur.fetchone()[0] or 0
        cur.execute("SELECT COUNT(*) FROM files WHERE status='Running'")
        running_count = cur.fetchone()[0] or 0
    
    cpu, memory, _ = get_system_load()
    stats_text = f"""
📊 <b>Bot Hosting Statistics</b>

👥 Total Registered Users: <code>{user_count}</code>
📁 Total Hosted Files: <code>{file_count}</code>
🚀 Currently Active Bots: <code>{running_count}</code>
⚡ Host CPU: <code>{cpu:.1f}%</code>
💾 Host Memory: <code>{memory:.1f}%</code>
    """
    bot.send_message(message.chat.id, stats_text)

@bot.message_handler(func=lambda m: m.text == "📁 My Files")
def my_files_handler(message):
    send_files_list(message.chat.id, message.from_user.id)

def send_files_list(chat_id, user_id):
    files = list_user_files(user_id)
    if not files:
        bot.send_message(chat_id, "📁 <b>Your Files</b>\n\nকোনো ফাইল আপলোড করা হয়নি।")
        return
    
    text = "📁 <b>Your Hosted Files</b>\n\nম্যানেজ করতে ফাইলের নামের ওপর ক্লিক করুন:"
    kb = InlineKeyboardMarkup()
    
    for file in files:
        file_id, _, orig_name, _, file_type, status, _ = file
        emoji = "🟢" if status == "Running" else "🔴"
        button_text = f"{emoji} {orig_name} ({file_type})"
        kb.add(InlineKeyboardButton(button_text, callback_data=f"manage:{file_id}"))
    
    bot.send_message(chat_id, text, reply_markup=kb)

@bot.message_handler(func=lambda m: m.text == "📤 Upload File")
def upload_handler(message):
    bot.send_message(
        message.chat.id, 
        "📤 <b>Upload Script / Bot</b>\n\n"
        "আমাকে আপনার পাইথন (<code>.py</code>), জাভাস্ক্রিপ্ট (<code>.js</code>) অথবা পুরো প্রজেক্টের <code>.ZIP</code> ফাইল পাঠান।"
    )

# Document upload worker in background thread
def process_upload_file(message):
    user = message.from_user
    user_id = user.id
    
    try:
        status_msg = bot.reply_to(message, "⏳ ফাইল ডাউনলোড হচ্ছে...")
        file_info = bot.get_file(message.document.file_id)
        file_bytes = bot.download_file(file_info.file_path)
    except Exception as e:
        bot.reply_to(message, f"❌ ফাইল ডাউনলোড ব্যর্থ: {str(e)}")
        return
    
    original_filename = message.document.file_name or "unknown.py"
    file_type = get_file_type(original_filename)
    
    user_dir = os.path.join(UPLOADS_DIR, str(user_id))
    os.makedirs(user_dir, exist_ok=True)
    
    safe_filename = f"{int(time.time())}_{original_filename}"
    file_path = os.path.join(user_dir, safe_filename)
    
    try:
        with open(file_path, 'wb') as f:
            f.write(file_bytes)
    except Exception as e:
        bot.reply_to(message, f"❌ সেভ করতে ব্যর্থ: {str(e)}")
        return
    
    final_path = file_path
    extracted_dir = None
    
    if file_type in ["zip", "archive"]:
        bot.edit_message_text("📦 Archive Extract করা হচ্ছে...", chat_id=message.chat.id, message_id=status_msg.message_id)
        extracted_dir = os.path.join(TEMP_DIR, f"proj_{user_id}_{int(time.time())}")
        os.makedirs(extracted_dir, exist_ok=True)
        
        success, error = extract_archive(file_path, extracted_dir)
        if not success:
            bot.edit_message_text(f"❌ জিপ ফাইল আনপ্যাক করতে সমস্যা: {error}", chat_id=message.chat.id, message_id=status_msg.message_id)
            return
        
        main_file = find_main_file(extracted_dir)
        if not main_file:
            bot.edit_message_text("❌ Zip ফাইলে কোনো মেইন (main.py / bot.py / index.js) ফাইল পাওয়া যায়নি।", chat_id=message.chat.id, message_id=status_msg.message_id)
            return
        
        final_path = extracted_dir
        file_type = get_file_type(main_file)
    
    file_id = add_file_record(user_id, user.username or "", safe_filename, original_filename, final_path, file_type)
    bot.edit_message_text("🚀 ফাইল আপলোড সফল! বট স্টার্ট করা হচ্ছে...", chat_id=message.chat.id, message_id=status_msg.message_id)
    
    # Auto-start in background
    start_file_process(file_id, message.chat.id)

@bot.message_handler(content_types=['document'])
def document_handler(message):
    threading.Thread(target=process_upload_file, args=(message,), daemon=True).start()

# Process executor (Multi-threaded & Non-blocking)
def start_file_process(file_id, chat_id):
    def run_worker():
        file_record = get_file_record(file_id)
        if not file_record:
            bot.send_message(chat_id, "❌ ফাইল পাওয়া যায়নি।")
            return
        
        file_path = file_record["path"]
        original_name = file_record["orig_name"]
        
        target_file = None
        working_dir = None
        
        if os.path.isdir(file_path):
            main_file = find_main_file(file_path)
            if not main_file:
                bot.send_message(chat_id, "❌ ফোল্ডারে এক্সিকিউটেবল ফাইল পাওয়া যায়নি।")
                return
            target_file = main_file
            working_dir = file_path
        else:
            if not os.path.exists(file_path):
                bot.send_message(chat_id, "❌ স্ক্রিপ্ট ফাইলটি পাওয়া যায়নি।")
                return
            target_file = file_path
            working_dir = os.path.dirname(file_path)
        
        ext = os.path.splitext(target_file)[1].lower()
        
        # Dependency auto-installation
        if ext == ".py":
            req_path = os.path.join(working_dir, "requirements.txt")
            if os.path.exists(req_path):
                bot.send_message(chat_id, "📦 <code>requirements.txt</code> প্যাকেজ ইনস্টল হচ্ছে...")
                install_requirements_from_file(req_path)
            
            imports = extract_imports(target_file)
            if imports:
                install_missing_imports(imports)
        
        # Command setup
        if ext == ".py":
            cmd = [sys.executable, "-u", target_file]
        elif ext == ".js":
            cmd = ["node", target_file]
        else:
            bot.send_message(chat_id, f"❌ Unsupported file format: {ext}")
            return
        
        log_filename = f"file_{file_id}_{int(time.time())}.log"
        log_path = os.path.join(LOGS_DIR, log_filename)
        
        try:
            log_file = open(log_path, 'w', encoding='utf-8')
            process = subprocess.Popen(
                cmd,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                cwd=working_dir,
                text=True
            )
            
            run_id = record_run_start(file_id, process.pid, log_path)
            update_file_status(file_id, process.pid, "Running")
            
            with proc_lock:
                processes[file_id] = {
                    'process': process,
                    'run_id': run_id,
                    'log_path': log_path,
                    'log_file': log_file
                }
            
            bot.send_message(
                chat_id, 
                f"✅ <b>{html_lib.escape(original_name)}</b> চালু হয়েছে!\n"
                f"📝 <b>PID:</b> <code>{process.pid}</code>\n"
                f"📊 <b>Status:</b> 🟢 24/7 Running"
            )
            
            def monitor_proc():
                exit_code = process.wait()
                try:
                    log_file.close()
                except Exception:
                    pass
                
                update_file_status(file_id, None, "Stopped")
                record_run_finish(run_id, exit_code)
                with proc_lock:
                    processes.pop(file_id, None)
                
                if exit_code != 0:
                    try:
                        bot.send_message(
                            chat_id, 
                            f"⚠️ <b>{html_lib.escape(original_name)}</b> বন্ধ হয়ে গেছে!\n"
                            f"Exit Code: <code>{exit_code}</code>\n"
                            f"কারণ দেখতে 'Logs' চেক করুন।"
                        )
                    except Exception:
                        pass

            threading.Thread(target=monitor_proc, daemon=True).start()
            
        except Exception as e:
            logger.error(f"Failed to start: {e}")
            bot.send_message(chat_id, f"❌ প্রসেস স্টার্ট করতে ব্যর্থ: {str(e)}")

    threading.Thread(target=run_worker, daemon=True).start()

def stop_file_process(file_id):
    with proc_lock:
        if file_id in processes:
            proc_data = processes[file_id]
            process = proc_data['process']
            try:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
            except Exception as e:
                logger.error(f"Error terminating: {e}")
            
            try:
                proc_data['log_file'].close()
            except Exception:
                pass
            
            processes.pop(file_id, None)
    
    update_file_status(file_id, None, "Stopped")

def get_file_logs(file_id, lines=50):
    try:
        with proc_lock:
            if file_id in processes:
                log_path = processes[file_id]['log_path']
                if os.path.exists(log_path):
                    with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
                        content = f.readlines()
                    return ''.join(content[-lines:]) if content else "No logs recorded yet."
        
        with db_lock:
            cur = conn.cursor()
            cur.execute("SELECT log_path FROM runs WHERE file_id=? ORDER BY id DESC LIMIT 1", (file_id,))
            row = cur.fetchone()
            if row and row[0] and os.path.exists(row[0]):
                with open(row[0], 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.readlines()
                return ''.join(content[-lines:]) if content else "No logs found."
        
        return "No log file found."
    except Exception as e:
        return f"Error reading logs: {str(e)}"

# Callback query handler
@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    data = call.data
    chat_id = call.message.chat.id
    user_id = call.from_user.id
    
    if data == "back_to_files":
        try:
            bot.delete_message(chat_id, call.message.message_id)
        except Exception:
            pass
        send_files_list(chat_id, user_id)
        return
    
    try:
        if data.startswith("manage:"):
            file_id = int(data.split(":")[1])
            show_file_management(chat_id, file_id, user_id, call.message.message_id)
        
        elif data.startswith("start:"):
            file_id = int(data.split(":")[1])
            bot.answer_callback_query(call.id, "বট স্টার্ট হচ্ছে...")
            start_file_process(file_id, chat_id)
            time.sleep(1)
            show_file_management(chat_id, file_id, user_id, call.message.message_id)
        
        elif data.startswith("stop:"):
            file_id = int(data.split(":")[1])
            bot.answer_callback_query(call.id, "বট বন্ধ করা হচ্ছে...")
            stop_file_process(file_id)
            time.sleep(1)
            show_file_management(chat_id, file_id, user_id, call.message.message_id)
        
        elif data.startswith("restart:"):
            file_id = int(data.split(":")[1])
            bot.answer_callback_query(call.id, "রিস্টার্ট হচ্ছে...")
            stop_file_process(file_id)
            time.sleep(1)
            start_file_process(file_id, chat_id)
            time.sleep(1)
            show_file_management(chat_id, file_id, user_id, call.message.message_id)
        
        elif data.startswith("delete:"):
            file_id = int(data.split(":")[1])
            bot.answer_callback_query(call.id, "মুছে ফেলা হচ্ছে...")
            file_record = get_file_record(file_id)
            if file_record:
                stop_file_process(file_id)
                file_path = file_record["path"]
                try:
                    if os.path.isdir(file_path):
                        shutil.rmtree(file_path, ignore_errors=True)
                    elif os.path.exists(file_path):
                        os.remove(file_path)
                except Exception:
                    pass
                remove_file_record(file_id)
            
            bot.send_message(chat_id, "🗑 ফাইলটি সম্পূর্ণ মুছে ফেলা হয়েছে।")
            send_files_list(chat_id, user_id)
        
        elif data.startswith("logs:"):
            file_id = int(data.split(":")[1])
            bot.answer_callback_query(call.id, "লগ লোড হচ্ছে...")
            logs = get_file_logs(file_id)
            file_record = get_file_record(file_id)
            file_name = file_record["orig_name"] if file_record else "Script"
            
            if len(logs) > 3500:
                logs = logs[-3500:]
                logs = "... (পূর্বের লগ কাটা হয়েছে) ...\n" + logs
            
            log_text = f"📄 <b>Logs for {html_lib.escape(file_name)}:</b>\n\n<pre>{html_lib.escape(logs)}</pre>"
            bot.send_message(chat_id, log_text)
            
    except Exception as e:
        bot.answer_callback_query(call.id, "সমস্যা হয়েছে!")
        logger.error(f"Callback error: {e}")

def show_file_management(chat_id, file_id, user_id, message_id=None):
    file_record = get_file_record(file_id)
    if not file_record:
        bot.send_message(chat_id, "❌ ফাইলটি পাওয়া যায়নি।")
        return
    
    if file_record["user_id"] != user_id:
        bot.send_message(chat_id, "❌ অ্যাক্সেস ডিনাইড!")
        return
    
    with proc_lock:
        is_running = file_id in processes
    
    status_text = "🟢 Running" if is_running else "🔴 Stopped"
    pid_text = f"\n<b>PID:</b> <code>{file_record['pid']}</code>" if file_record['pid'] and is_running else ""
    
    text = f"""
⚙️ <b>File Management Dashboard</b>

📁 <b>File Name:</b> {html_lib.escape(file_record['orig_name'])}
📊 <b>Engine:</b> {file_record['file_type'].upper()}
📈 <b>Status:</b> {status_text}{pid_text}
⏰ <b>Uploaded:</b> {file_record['uploaded_at'][:16]}
    """
    
    kb = file_actions_kb(file_id, is_running)
    
    if message_id:
        try:
            bot.edit_message_text(text, chat_id, message_id, reply_markup=kb)
        except Exception:
            bot.send_message(chat_id, text, reply_markup=kb)
    else:
        bot.send_message(chat_id, text, reply_markup=kb)

# Bot polling loop
def start_bot():
    logger.info("Running Bot 🚀🚀🚀successfully...")
    while True:
        try:
            bot.infinity_polling(timeout=60, long_polling_timeout=50)
        except Exception as e:
            logger.error(f"Polling error: {e}")
            time.sleep(3)

if __name__ == "__main__":
    start_bot()
