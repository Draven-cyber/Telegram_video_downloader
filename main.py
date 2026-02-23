import os
import asyncio
import logging
from flask import Flask, render_template, request, send_file, jsonify, session
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
import hashlib
import time
import threading
import queue
import uuid
from functools import wraps

# Configure logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.urandom(24).hex()
app.config['SESSION_TYPE'] = 'filesystem'

# Your Telegram API credentials
API_ID = '29145458'
API_HASH = '00b32d6c9f385662edfed86f047b4116'

# Store active clients and download tasks
clients = {}
download_tasks = {}
download_queue = queue.Queue()
loop_per_thread = {}

def async_handler(f):
    """Decorator to handle async functions in Flask routes"""
    @wraps(f)
    def wrapped(*args, **kwargs):
        # Get or create event loop for this thread
        thread_id = threading.get_ident()
        if thread_id not in loop_per_thread:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop_per_thread[thread_id] = loop
        else:
            loop = loop_per_thread[thread_id]
        
        # Run the async function
        return loop.run_until_complete(f(*args, **kwargs))
    return wrapped

class TelegramDownloader:
    def __init__(self, session_name):
        self.session_name = session_name
        self.client = None
        self.loop = None
        
    async def init_client(self):
        """Initialize the client with proper event loop"""
        self.loop = asyncio.get_event_loop()
        self.client = TelegramClient(f'sessions/{self.session_name}', API_ID, API_HASH, loop=self.loop)
        
    async def connect(self):
        """Connect to Telegram"""
        if not self.client:
            await self.init_client()
        await self.client.connect()
        
    async def disconnect(self):
        """Disconnect from Telegram"""
        if self.client and self.client.is_connected():
            await self.client.disconnect()
            
    async def is_authorized(self):
        """Check if user is authorized"""
        return await self.client.is_user_authorized()
        
    async def send_code(self, phone):
        """Send verification code"""
        return await self.client.send_code_request(phone)
        
    async def sign_in(self, phone, code):
        """Sign in with code"""
        return await self.client.sign_in(phone, code)
        
    async def get_message(self, message_link):
        """Extract message info from Telegram link"""
        try:
            # Parse the message link
            # Format: https://t.me/username/123 or https://t.me/c/123456789/123
            parts = message_link.split('/')
            
            # Remove empty strings from split
            parts = [p for p in parts if p]
            
            # Find the message ID (last part)
            message_id = int(parts[-1])
            
            # Determine if it's a private channel (has 'c' in the path)
            if 'c' in parts:
                # Private channel format: https://t.me/c/123456789/123
                chat_id_index = parts.index('c') + 1
                chat_id = int(parts[chat_id_index])
                # For private channels, chat_id needs to be in format -100xxxxxxxxx
                entity = await self.client.get_entity(int(f'-100{chat_id}'))
            else:
                # Public chat format: https://t.me/username/123
                chat_username = parts[-2]
                # Remove any 't.me' or 'telegram.me' prefixes
                if chat_username in ['t.me', 'telegram.me']:
                    chat_username = parts[-3]
                entity = await self.client.get_entity(chat_username)
            
            # Get the message
            message = await self.client.get_messages(entity, ids=message_id)
            return message
        except Exception as e:
            logger.error(f"Error getting message: {e}")
            return None
    
    async def download_media(self, message, download_id):
        """Download media from message"""
        try:
            if message and message.media:
                # Create downloads directory if it doesn't exist
                os.makedirs('downloads', exist_ok=True)
                
                # Generate filename
                if hasattr(message.media, 'photo'):
                    ext = '.jpg'
                    filename = f"photo_{message.id}{ext}"
                elif hasattr(message.media, 'document'):
                    # Try to get original filename
                    filename = None
                    for attr in message.media.document.attributes:
                        if hasattr(attr, 'file_name'):
                            filename = attr.file_name
                            break
                    
                    if not filename:
                        # Generate filename based on MIME type
                        mime_type = message.media.document.mime_type
                        if mime_type:
                            ext = mime_type.split('/')[-1]
                            if ext == 'plain':
                                ext = 'txt'
                            elif ext == 'jpeg':
                                ext = 'jpg'
                        else:
                            ext = 'bin'
                        filename = f"document_{message.id}.{ext}"
                else:
                    return None, "Unsupported media type"
                
                # Download path
                safe_filename = "".join([c for c in filename if c.isalnum() or c in ' ._-()']).rstrip()
                download_path = f"downloads/{download_id}_{safe_filename}"
                
                # Download the media
                path = await message.download_media(file=download_path)
                
                if path:
                    return path, safe_filename
                else:
                    return None, "Download failed"
            else:
                return None, "No media found in message"
        except Exception as e:
            logger.error(f"Download error: {e}")
            return None, str(e)

# Background worker for handling downloads
def download_worker():
    """Background thread to process downloads"""
    # Create event loop for this thread
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    while True:
        try:
            task = download_queue.get(timeout=1)
            if task:
                download_id, message_link, session_id = task
                
                async def process_download():
                    downloader = None
                    try:
                        # Create new downloader instance
                        downloader = TelegramDownloader(session_id)
                        await downloader.connect()
                        
                        # Check if already logged in
                        if not await downloader.is_authorized():
                            download_tasks[download_id] = {
                                'status': 'error',
                                'message': 'Session expired. Please log in again.'
                            }
                            return
                        
                        download_tasks[download_id] = {'status': 'processing', 'progress': 10}
                        
                        # Get message
                        message = await downloader.get_message(message_link)
                        
                        if not message:
                            download_tasks[download_id] = {
                                'status': 'error',
                                'message': 'Message not found or inaccessible. Make sure you have access to this channel/group.'
                            }
                            return
                        
                        download_tasks[download_id] = {'status': 'downloading', 'progress': 30}
                        
                        # Download media
                        filepath, filename = await downloader.download_media(message, download_id)
                        
                        if filepath:
                            download_tasks[download_id] = {
                                'status': 'completed',
                                'filepath': filepath,
                                'filename': filename,
                                'progress': 100
                            }
                            logger.info(f"Download completed: {filename}")
                        else:
                            download_tasks[download_id] = {
                                'status': 'error',
                                'message': filename  # filename contains error message here
                            }
                            
                    except Exception as e:
                        logger.error(f"Download error: {e}")
                        download_tasks[download_id] = {
                            'status': 'error',
                            'message': str(e)
                        }
                    finally:
                        if downloader:
                            await downloader.disconnect()
                
                # Run the async function
                loop.run_until_complete(process_download())
                
        except queue.Empty:
            pass
        except Exception as e:
            logger.error(f"Worker error: {e}")

# Start background worker thread
worker_thread = threading.Thread(target=download_worker, daemon=True)
worker_thread.start()

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
        
        # Create session ID
        session_id = str(uuid.uuid4())
        session['telegram_session'] = session_id
        
        # Create and connect client
        downloader = TelegramDownloader(session_id)
        await downloader.connect()
        clients[session_id] = downloader
        
        # Send code request
        result = await downloader.send_code(phone)
        
        return jsonify({
            'success': True,
            'message': 'Code sent successfully',
            'phone_code_hash': getattr(result, 'phone_code_hash', '')
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
        session_id = session.get('telegram_session')
        
        if not session_id or session_id not in clients:
            return jsonify({'success': False, 'error': 'No active session. Please start over.'})
        
        downloader = clients[session_id]
        
        try:
            await downloader.sign_in(phone, code)
            return jsonify({'success': True})
        except SessionPasswordNeededError:
            return jsonify({'success': False, 'error': 'Two-factor authentication enabled. This version doesn\'t support 2FA yet.'})
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
        session_id = session.get('telegram_session')
        
        if not session_id:
            return jsonify({'success': False, 'error': 'Please log in first'})
        
        if session_id not in clients:
            return jsonify({'success': False, 'error': 'Session expired. Please log in again.'})
        
        if not message_link:
            return jsonify({'success': False, 'error': 'Message link required'})
        
        # Validate link format
        if 't.me/' not in message_link:
            return jsonify({'success': False, 'error': 'Invalid Telegram link format'})
        
        # Generate download ID
        download_id = hashlib.md5(f"{message_link}{time.time()}".encode()).hexdigest()[:8]
        
        # Add to queue with session_id
        download_queue.put((download_id, message_link, session_id))
        download_tasks[download_id] = {'status': 'queued', 'progress': 0}
        
        return jsonify({
            'success': True,
            'download_id': download_id,
            'message': 'Download started'
        })
        
    except Exception as e:
        logger.error(f"Download request error: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/status/<download_id>')
def get_status(download_id):
    """Get download status"""
    if download_id in download_tasks:
        task = download_tasks[download_id]
        if task['status'] == 'completed':
            return jsonify({
                'status': 'completed',
                'filename': task.get('filename'),
                'download_url': f'/file/{download_id}'
            })
        elif task['status'] == 'error':
            return jsonify({
                'status': 'error',
                'message': task.get('message', 'Unknown error')
            })
        else:
            return jsonify({
                'status': task['status'],
                'progress': task.get('progress', 0)
            })
    else:
        return jsonify({'status': 'not_found'})

@app.route('/file/<download_id>')
def download_file(download_id):
    """Serve downloaded file"""
    if download_id in download_tasks:
        task = download_tasks[download_id]
        if task['status'] == 'completed' and os.path.exists(task['filepath']):
            try:
                return send_file(
                    task['filepath'],
                    as_attachment=True,
                    download_name=task['filename'],
                    mimetype='application/octet-stream'
                )
            except Exception as e:
                logger.error(f"File send error: {e}")
                return "Error sending file", 500
    
    return "File not found or expired", 404

@app.route('/logout')
@async_handler
async def logout():
    """Logout from Telegram"""
    session_id = session.get('telegram_session')
    if session_id and session_id in clients:
        try:
            downloader = clients[session_id]
            await downloader.disconnect()
            del clients[session_id]
        except Exception as e:
            logger.error(f"Logout error: {e}")
    
    # Clear session
    session.clear()
    
    # Clean up old downloads (optional)
    try:
        # Remove files older than 1 hour
        import time
        current_time = time.time()
        for filename in os.listdir('downloads'):
            filepath = os.path.join('downloads', filename)
            if os.path.isfile(filepath) and current_time - os.path.getctime(filepath) > 3600:
                os.remove(filepath)
    except Exception as e:
        logger.error(f"Cleanup error: {e}")
    
    return jsonify({'success': True})

@app.route('/check-auth')
@async_handler
async def check_auth():
    """Check if user is authenticated"""
    session_id = session.get('telegram_session')
    if session_id and session_id in clients:
        try:
            downloader = clients[session_id]
            if await downloader.is_authorized():
                return jsonify({'authenticated': True})
        except:
            pass
    
    return jsonify({'authenticated': False})

# Create necessary directories
os.makedirs('sessions', exist_ok=True)
os.makedirs('downloads', exist_ok=True)
os.makedirs('templates', exist_ok=True)

# Clean up old session files on startup
try:
    for filename in os.listdir('sessions'):
        if filename.endswith('.session'):
            os.remove(os.path.join('sessions', filename))
except:
    pass

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True, threaded=True)
