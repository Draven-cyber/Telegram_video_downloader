import os
import asyncio
import logging
from flask import Flask, render_template, request, send_file, jsonify, session as flask_session
from flask_session import Session
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError, FloodWaitError
from telethon.sessions import StringSession
import hashlib
import time
import threading
import queue
import uuid
from functools import wraps
import secrets
from datetime import datetime, timedelta
import pymongo
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError
import certifi
import urllib.parse

# =============================================
# CONFIGURATION - Embedded Credentials
# =============================================
API_ID = '29145458'
API_HASH = '00b32d6c9f385662edfed86f047b4116'
MONGO_URI = 'mongodb+srv://rithika:Rithika25894@cluster1.zuujlbq.mongodb.net/'

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Security configuration
app.secret_key = secrets.token_hex(32)
app.config['SESSION_TYPE'] = 'mongodb'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)
app.config['SESSION_PERMANENT'] = True
app.config['SESSION_USE_SIGNER'] = True
app.config['SESSION_KEY_PREFIX'] = 'evo_tg:'

# MongoDB connection with proper encoding
password = urllib.parse.quote_plus('Rithika25894')
MONGO_URI_FULL = f'mongodb+srv://rithika:{password}@cluster1.zuujlbq.mongodb.net/'

# Connect to MongoDB
try:
    mongo_client = MongoClient(
        MONGO_URI_FULL,
        tlsCAFile=certifi.where(),
        maxPoolSize=50,
        minPoolSize=10,
        maxIdleTimeMS=45000,
        retryWrites=True,
        serverSelectionTimeoutMS=5000
    )
    
    # Test connection
    mongo_client.admin.command('ping')
    logger.info("✅ Successfully connected to MongoDB Atlas")
    
except Exception as e:
    logger.error(f"❌ Failed to connect to MongoDB: {e}")
    raise

# Create database and collections
db = mongo_client['telegram_downloader']
sessions_collection = db['telegram_sessions']
downloads_collection = db['downloads']
users_collection = db['users']
stats_collection = db['stats']

# Create indexes
sessions_collection.create_index('session_id', unique=True)
sessions_collection.create_index('created_at', expireAfterSeconds=86400)  # 24 hours
downloads_collection.create_index('download_id', unique=True)
downloads_collection.create_index('created_at', expireAfterSeconds=3600)  # 1 hour
users_collection.create_index('user_id', unique=True)
stats_collection.create_index('date')

# Thread-local storage for event loops
thread_local = threading.local()

def get_event_loop():
    """Get or create event loop for thread"""
    if not hasattr(thread_local, 'loop'):
        thread_local.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(thread_local.loop)
    return thread_local.loop

def async_handler(f):
    """Decorator for async Flask routes"""
    @wraps(f)
    def wrapped(*args, **kwargs):
        loop = get_event_loop()
        try:
            return loop.run_until_complete(f(*args, **kwargs))
        except RuntimeError:
            thread_local.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(thread_local.loop)
            return thread_local.loop.run_until_complete(f(*args, **kwargs))
    return wrapped

class TelegramDownloader:
    def __init__(self, session_id, user_id=None):
        self.session_id = session_id
        self.user_id = user_id or str(uuid.uuid4())
        self.client = None
        self.session_string = self._load_session()
        
    def _load_session(self):
        """Load session from MongoDB"""
        try:
            session_data = sessions_collection.find_one({'session_id': self.session_id})
            if session_data:
                return session_data.get('session_string')
        except Exception as e:
            logger.error(f"Error loading session: {e}")
        return None
    
    def _save_session(self, session_string):
        """Save session to MongoDB"""
        try:
            sessions_collection.update_one(
                {'session_id': self.session_id},
                {
                    '$set': {
                        'session_string': session_string,
                        'user_id': self.user_id,
                        'updated_at': datetime.utcnow()
                    },
                    '$setOnInsert': {
                        'created_at': datetime.utcnow()
                    }
                },
                upsert=True
            )
        except Exception as e:
            logger.error(f"Error saving session: {e}")
    
    async def init_client(self):
        """Initialize Telegram client with StringSession"""
        try:
            if self.session_string:
                self.client = TelegramClient(
                    StringSession(self.session_string),
                    int(API_ID),
                    API_HASH
                )
            else:
                self.client = TelegramClient(
                    StringSession(),
                    int(API_ID),
                    API_HASH
                )
        except Exception as e:
            logger.error(f"Error initializing client: {e}")
            raise
    
    async def connect(self):
        """Connect to Telegram"""
        try:
            if not self.client:
                await self.init_client()
            await self.client.connect()
        except Exception as e:
            logger.error(f"Connection error: {e}")
            raise
    
    async def disconnect(self):
        """Disconnect from Telegram"""
        try:
            if self.client and self.client.is_connected():
                await self.client.disconnect()
        except Exception as e:
            logger.error(f"Disconnect error: {e}")
    
    async def is_authorized(self):
        """Check if user is authorized"""
        try:
            return await self.client.is_user_authorized()
        except Exception as e:
            logger.error(f"Auth check error: {e}")
            return False
    
    async def send_code(self, phone):
        """Send verification code"""
        try:
            result = await self.client.send_code_request(phone)
            sessions_collection.update_one(
                {'session_id': self.session_id},
                {'$set': {'phone': phone}}
            )
            return result
        except FloodWaitError as e:
            raise Exception(f"Too many attempts. Please wait {e.seconds} seconds")
        except Exception as e:
            logger.error(f"Send code error: {e}")
            raise
    
    async def sign_in(self, phone, code):
        """Sign in with code"""
        try:
            result = await self.client.sign_in(phone, code)
            self.session_string = self.client.session.save()
            self._save_session(self.session_string)
            
            # Get user info
            me = await self.client.get_me()
            users_collection.update_one(
                {'user_id': self.user_id},
                {
                    '$set': {
                        'telegram_id': me.id,
                        'username': me.username,
                        'first_name': me.first_name,
                        'last_name': me.last_name,
                        'phone': phone,
                        'last_login': datetime.utcnow()
                    },
                    '$setOnInsert': {
                        'created_at': datetime.utcnow()
                    }
                },
                upsert=True
            )
            
            return result
        except SessionPasswordNeededError:
            raise Exception("Two-factor authentication is enabled")
        except Exception as e:
            logger.error(f"Sign in error: {e}")
            raise
    
    async def get_message(self, message_link):
        """Get message from Telegram link"""
        try:
            # Parse link
            link = message_link.rstrip('/')
            
            if 't.me/c/' in link:
                # Private channel
                parts = link.split('/')
                chat_id = int(parts[-2])
                message_id = int(parts[-1])
                entity = await self.client.get_entity(int(f'-100{chat_id}'))
            else:
                # Public chat
                parts = link.split('/')
                username = parts[-2]
                message_id = int(parts[-1])
                entity = await self.client.get_entity(username)
            
            # Get message
            message = await self.client.get_messages(entity, ids=message_id)
            return message
        except Exception as e:
            logger.error(f"Error getting message: {e}")
            return None
    
    async def download_media(self, message, download_id):
        """Download media from message"""
        try:
            if not (message and message.media):
                return None, "No media found in message"
            
            # Create downloads directory
            os.makedirs('downloads', exist_ok=True)
            
            # Generate filename
            filename = None
            file_size = 0
            mime_type = None
            
            if hasattr(message.media, 'photo'):
                filename = f"photo_{message.id}.jpg"
                if message.media.photo.sizes:
                    file_size = message.media.photo.sizes[-1].size
                mime_type = 'image/jpeg'
                
            elif hasattr(message.media, 'document'):
                for attr in message.media.document.attributes:
                    if hasattr(attr, 'file_name'):
                        filename = attr.file_name
                        break
                
                file_size = message.media.document.size
                mime_type = message.media.document.mime_type
                
                if not filename:
                    ext = mime_type.split('/')[-1] if mime_type else 'bin'
                    ext_map = {
                        'plain': 'txt',
                        'jpeg': 'jpg',
                        'mpeg': 'mp3',
                        'png': 'png',
                        'pdf': 'pdf',
                        'vnd.android.package-archive': 'apk',
                        'zip': 'zip',
                        'x-rar': 'rar',
                        'x-7z-compressed': '7z'
                    }
                    ext = ext_map.get(ext, ext)
                    filename = f"document_{message.id}.{ext}"
            
            if not filename:
                return None, "Could not determine filename"
            
            # Sanitize filename
            filename = "".join(c for c in filename if c.isalnum() or c in ' ._-()').strip()
            if not filename:
                filename = f"file_{message.id}.bin"
            
            # Download path
            timestamp = int(time.time())
            safe_filename = f"{timestamp}_{filename}"
            download_path = os.path.join('downloads', safe_filename)
            
            # Download media
            logger.info(f"Downloading to {download_path}")
            path = await message.download_media(file=download_path)
            
            if path and os.path.exists(path):
                actual_size = os.path.getsize(path)
                
                # Save to MongoDB
                download_record = {
                    'download_id': download_id,
                    'user_id': self.user_id,
                    'filename': filename,
                    'saved_as': safe_filename,
                    'file_size': actual_size,
                    'mime_type': mime_type,
                    'message_id': message.id,
                    'created_at': datetime.utcnow(),
                    'expires_at': datetime.utcnow() + timedelta(hours=1)
                }
                downloads_collection.insert_one(download_record)
                
                return path, filename
            else:
                return None, "Download failed"
                
        except Exception as e:
            logger.error(f"Download error: {e}")
            return None, str(e)

# Download Worker
class DownloadWorker:
    def __init__(self):
        self.queue = queue.Queue()
        self.tasks = {}
        self.worker_thread = threading.Thread(target=self._worker, daemon=True)
        self.worker_thread.start()
    
    def _worker(self):
        """Background worker thread"""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        while True:
            try:
                task = self.queue.get(timeout=1)
                if task:
                    download_id, message_link, session_id, user_id = task
                    
                    async def process():
                        downloader = None
                        try:
                            self.tasks[download_id] = {
                                'status': 'processing',
                                'progress': 10,
                                'message': 'Initializing...'
                            }
                            
                            downloader = TelegramDownloader(session_id, user_id)
                            await downloader.connect()
                            
                            if not await downloader.is_authorized():
                                self.tasks[download_id] = {
                                    'status': 'error',
                                    'message': 'Session expired. Please login again.'
                                }
                                return
                            
                            self.tasks[download_id] = {
                                'status': 'processing',
                                'progress': 30,
                                'message': 'Fetching message...'
                            }
                            
                            message = await downloader.get_message(message_link)
                            
                            if not message:
                                self.tasks[download_id] = {
                                    'status': 'error',
                                    'message': 'Message not found or inaccessible'
                                }
                                return
                            
                            self.tasks[download_id] = {
                                'status': 'downloading',
                                'progress': 50,
                                'message': 'Downloading media...'
                            }
                            
                            filepath, filename = await downloader.download_media(message, download_id)
                            
                            if filepath:
                                self.tasks[download_id] = {
                                    'status': 'completed',
                                    'progress': 100,
                                    'filepath': filepath,
                                    'filename': filename,
                                    'message': 'Download complete!'
                                }
                                
                                # Update stats
                                stats_collection.update_one(
                                    {'date': datetime.utcnow().strftime('%Y-%m-%d')},
                                    {
                                        '$inc': {
                                            'total_downloads': 1,
                                            'total_size': os.path.getsize(filepath)
                                        },
                                        '$setOnInsert': {'created_at': datetime.utcnow()}
                                    },
                                    upsert=True
                                )
                            else:
                                self.tasks[download_id] = {
                                    'status': 'error',
                                    'message': filename
                                }
                                
                        except Exception as e:
                            logger.error(f"Worker error: {e}")
                            self.tasks[download_id] = {
                                'status': 'error',
                                'message': str(e)
                            }
                        finally:
                            if downloader:
                                await downloader.disconnect()
                    
                    loop.run_until_complete(process())
                    
            except queue.Empty:
                pass
            except Exception as e:
                logger.error(f"Worker thread error: {e}")
    
    def add_task(self, download_id, message_link, session_id, user_id):
        """Add task to queue"""
        self.queue.put((download_id, message_link, session_id, user_id))
        self.tasks[download_id] = {
            'status': 'queued',
            'progress': 0,
            'message': 'Waiting in queue...'
        }
        return download_id
    
    def get_task(self, download_id):
        """Get task status"""
        return self.tasks.get(download_id)
    
    def cleanup_old_tasks(self):
        """Remove old completed tasks"""
        current_time = time.time()
        to_delete = []
        for task_id, task in self.tasks.items():
            if task['status'] in ['completed', 'error']:
                if current_time - task.get('timestamp', 0) > 3600:
                    to_delete.append(task_id)
        
        for task_id in to_delete:
            del self.tasks[task_id]

# Initialize worker
download_worker = DownloadWorker()

# =============================================
# ROUTES
# =============================================

@app.route('/')
def index():
    """Main page"""
    return render_template('index.html')

@app.route('/login', methods=['POST'])
@async_handler
async def login():
    """Handle Telegram login"""
    try:
        data = request.get_json()
        phone = data.get('phone')
        
        if not phone:
            return jsonify({'success': False, 'error': 'Phone number required'})
        
        # Create session
        session_id = str(uuid.uuid4())
        user_id = str(uuid.uuid4())
        
        flask_session['telegram_session'] = session_id
        flask_session['user_id'] = user_id
        flask_session.permanent = True
        
        # Initialize downloader
        downloader = TelegramDownloader(session_id, user_id)
        await downloader.connect()
        
        # Send code
        await downloader.send_code(phone)
        
        return jsonify({
            'success': True,
            'message': 'Verification code sent'
        })
        
    except Exception as e:
        logger.error(f"Login error: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/verify', methods=['POST'])
@async_handler
async def verify():
    """Verify login code"""
    try:
        data = request.get_json()
        code = data.get('code')
        phone = data.get('phone')
        
        session_id = flask_session.get('telegram_session')
        user_id = flask_session.get('user_id')
        
        if not session_id:
            return jsonify({'success': False, 'error': 'No active session'})
        
        downloader = TelegramDownloader(session_id, user_id)
        await downloader.connect()
        
        try:
            await downloader.sign_in(phone, code)
            return jsonify({'success': True})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)})
        
    except Exception as e:
        logger.error(f"Verification error: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/download', methods=['POST'])
def download():
    """Start download process"""
    try:
        data = request.get_json()
        message_link = data.get('link')
        
        session_id = flask_session.get('telegram_session')
        user_id = flask_session.get('user_id')
        
        if not session_id:
            return jsonify({'success': False, 'error': 'Please login first'})
        
        if not message_link or 't.me/' not in message_link:
            return jsonify({'success': False, 'error': 'Invalid Telegram link'})
        
        # Generate download ID
        download_id = hashlib.md5(
            f"{message_link}{time.time()}{user_id}".encode()
        ).hexdigest()[:8]
        
        # Add to queue
        download_worker.add_task(download_id, message_link, session_id, user_id)
        
        return jsonify({
            'success': True,
            'download_id': download_id,
            'message': 'Download started'
        })
        
    except Exception as e:
        logger.error(f"Download error: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/status/<download_id>')
def get_status(download_id):
    """Get download status"""
    task = download_worker.get_task(download_id)
    
    if task:
        if task['status'] == 'completed':
            return jsonify({
                'status': 'completed',
                'filename': task.get('filename'),
                'download_url': f'/file/{download_id}',
                'message': task.get('message')
            })
        elif task['status'] == 'error':
            return jsonify({
                'status': 'error',
                'message': task.get('message', 'Unknown error')
            })
        else:
            return jsonify({
                'status': task['status'],
                'progress': task.get('progress', 0),
                'message': task.get('message', 'Processing...')
            })
    else:
        return jsonify({'status': 'not_found'})

@app.route('/file/<download_id>')
def download_file(download_id):
    """Serve downloaded file"""
    try:
        download = downloads_collection.find_one({'download_id': download_id})
        
        if not download:
            return jsonify({'error': 'Download not found'}), 404
        
        filepath = os.path.join('downloads', download['saved_as'])
        
        if os.path.exists(filepath):
            return send_file(
                filepath,
                as_attachment=True,
                download_name=download['filename'],
                mimetype=download.get('mime_type', 'application/octet-stream')
            )
        else:
            return jsonify({'error': 'File not found'}), 404
            
    except Exception as e:
        logger.error(f"File download error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/logout', methods=['POST'])
@async_handler
async def logout():
    """Logout from Telegram"""
    try:
        session_id = flask_session.get('telegram_session')
        
        if session_id:
            sessions_collection.delete_one({'session_id': session_id})
        
        flask_session.clear()
        return jsonify({'success': True})
        
    except Exception as e:
        logger.error(f"Logout error: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/check-auth')
@async_handler
async def check_auth():
    """Check authentication status"""
    try:
        session_id = flask_session.get('telegram_session')
        user_id = flask_session.get('user_id')
        
        if not session_id or not user_id:
            return jsonify({'authenticated': False})
        
        session_data = sessions_collection.find_one({'session_id': session_id})
        if not session_data:
            return jsonify({'authenticated': False})
        
        downloader = TelegramDownloader(session_id, user_id)
        await downloader.connect()
        
        try:
            if await downloader.is_authorized():
                return jsonify({'authenticated': True})
            else:
                return jsonify({'authenticated': False})
        finally:
            await downloader.disconnect()
            
    except Exception as e:
        logger.error(f"Auth check error: {e}")
        return jsonify({'authenticated': False})

@app.route('/stats')
def get_stats():
    """Get download statistics"""
    try:
        today = datetime.utcnow().strftime('%Y-%m-%d')
        today_stats = stats_collection.find_one({'date': today})
        
        total_downloads = sum([
            s.get('total_downloads', 0) 
            for s in stats_collection.find({}, {'total_downloads': 1})
        ])
        
        total_size = sum([
            s.get('total_size', 0) 
            for s in stats_collection.find({}, {'total_size': 1})
        ])
        
        return jsonify({
            'today_downloads': today_stats.get('total_downloads', 0) if today_stats else 0,
            'total_downloads': total_downloads,
            'total_size_gb': round(total_size / (1024**3), 2)
        })
        
    except Exception as e:
        logger.error(f"Stats error: {e}")
        return jsonify({'error': str(e)}), 500

# Cleanup function
def cleanup_old_files():
    """Remove old downloaded files"""
    try:
        cutoff = time.time() - 3600  # 1 hour
        
        for filename in os.listdir('downloads'):
            filepath = os.path.join('downloads', filename)
            if os.path.isfile(filepath):
                file_time = os.path.getctime(filepath)
                if file_time < cutoff:
                    os.remove(filepath)
                    logger.info(f"Removed old file: {filename}")
        
        download_worker.cleanup_old_tasks()
        
    except Exception as e:
        logger.error(f"Cleanup error: {e}")
    
    threading.Timer(1800, cleanup_old_files).start()

# Create directories
os.makedirs('downloads', exist_ok=True)
os.makedirs('templates', exist_ok=True)

# Start cleanup
cleanup_thread = threading.Thread(target=cleanup_old_files, daemon=True)
cleanup_thread.start()

if __name__ == '__main__':
    app.run(
        host='0.0.0.0',
        port=5000,
        debug=True,
        threaded=True
    )
