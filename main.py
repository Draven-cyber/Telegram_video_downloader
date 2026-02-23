import os
import asyncio
import logging
from flask import Flask, render_template, request, send_file, jsonify, session
from telethon import TelegramClient, events
from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument
import hashlib
import time
import threading
import queue
import uuid

# Configure logging
logging.basicConfig(level=logging.DEBUG)

app = Flask(__name__)
app.secret_key = os.urandom(24).hex()

# Your Telegram API credentials
API_ID = '29145458'
API_HASH = '00b32d6c9f385662edfed86f047b4116'

# Store active clients and download tasks
clients = {}
download_tasks = {}
download_queue = queue.Queue()

class TelegramDownloader:
    def __init__(self, session_name):
        self.session_name = session_name
        self.client = TelegramClient(f'sessions/{session_name}', API_ID, API_HASH)
        self.is_connected = False
        
    async def connect(self):
        await self.client.connect()
        self.is_connected = True
        
    async def disconnect(self):
        await self.client.disconnect()
        self.is_connected = False
        
    async def get_message(self, message_link):
        """Extract message info from Telegram link"""
        try:
            # Parse the message link
            # Format: https://t.me/username/123 or https://t.me/c/123456789/123
            parts = message_link.split('/')
            
            if 'c' in parts:  # Private channel
                chat_id = int(parts[-2])
                message_id = int(parts[-1])
                entity = await self.client.get_entity(int(f'-100{chat_id}'))
            else:  # Public chat
                chat_username = parts[-2]
                message_id = int(parts[-1])
                entity = await self.client.get_entity(chat_username)
            
            # Get the message
            message = await self.client.get_messages(entity, ids=message_id)
            return message
        except Exception as e:
            logging.error(f"Error getting message: {e}")
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
                    for attr in message.media.document.attributes:
                        if hasattr(attr, 'file_name'):
                            filename = attr.file_name
                            break
                    else:
                        # Generate filename based on MIME type
                        mime_type = message.media.document.mime_type
                        ext = mime_type.split('/')[-1] if mime_type else 'bin'
                        filename = f"document_{message.id}.{ext}"
                else:
                    return None, "Unsupported media type"
                
                # Download path
                download_path = f"downloads/{download_id}_{filename}"
                
                # Download the media
                path = await message.download_media(file=download_path)
                
                if path:
                    return path, filename
                else:
                    return None, "Download failed"
            else:
                return None, "No media found in message"
        except Exception as e:
            logging.error(f"Download error: {e}")
            return None, str(e)

# Background worker for handling downloads
def download_worker():
    """Background thread to process downloads"""
    while True:
        try:
            task = download_queue.get(timeout=1)
            if task:
                download_id, message_link = task
                
                # Create new downloader instance
                downloader = TelegramDownloader(f"session_{download_id}")
                
                # Run async download
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                
                async def process_download():
                    try:
                        await downloader.connect()
                        
                        # Check if already logged in
                        if not await downloader.client.is_user_authorized():
                            download_tasks[download_id] = {
                                'status': 'error',
                                'message': 'Please log in to Telegram first'
                            }
                            return
                        
                        download_tasks[download_id] = {'status': 'processing', 'progress': 0}
                        
                        # Get message
                        message = await downloader.get_message(message_link)
                        
                        if not message:
                            download_tasks[download_id] = {
                                'status': 'error',
                                'message': 'Message not found or inaccessible'
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
                        else:
                            download_tasks[download_id] = {
                                'status': 'error',
                                'message': filename  # filename contains error message here
                            }
                            
                    except Exception as e:
                        download_tasks[download_id] = {
                            'status': 'error',
                            'message': str(e)
                        }
                    finally:
                        await downloader.disconnect()
                
                loop.run_until_complete(process_download())
                loop.close()
                
        except queue.Empty:
            pass
        except Exception as e:
            logging.error(f"Worker error: {e}")

# Start background worker thread
worker_thread = threading.Thread(target=download_worker, daemon=True)
worker_thread.start()

@app.route('/')
def index():
    """Main page"""
    return render_template('index.html')

@app.route('/login', methods=['POST'])
def login():
    """Handle Telegram login"""
    try:
        phone = request.json.get('phone')
        if not phone:
            return jsonify({'success': False, 'error': 'Phone number required'})
        
        # Create session ID
        session_id = str(uuid.uuid4())
        session['telegram_session'] = session_id
        
        # Create client
        client = TelegramClient(f'sessions/{session_id}', API_ID, API_HASH)
        clients[session_id] = client
        
        # Start login process
        async def send_code():
            await client.connect()
            if not await client.is_user_authorized():
                await client.send_code_request(phone)
                return {'success': True, 'phone_code_hash': 'sent'}
            else:
                return {'success': True, 'already_logged_in': True}
        
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(send_code())
        loop.close()
        
        return jsonify({'success': True, 'session_id': session_id})
        
    except Exception as e:
        logging.error(f"Login error: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/verify', methods=['POST'])
def verify():
    """Verify login code"""
    try:
        code = request.json.get('code')
        session_id = session.get('telegram_session')
        
        if not session_id or session_id not in clients:
            return jsonify({'success': False, 'error': 'No active session'})
        
        client = clients[session_id]
        
        async def verify_code():
            try:
                await client.sign_in(code=code)
                return {'success': True}
            except Exception as e:
                return {'success': False, 'error': str(e)}
        
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(verify_code())
        loop.close()
        
        return jsonify(result)
        
    except Exception as e:
        logging.error(f"Verification error: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/download', methods=['POST'])
def download():
    """Start download process"""
    try:
        message_link = request.json.get('link')
        session_id = session.get('telegram_session')
        
        if not session_id:
            return jsonify({'success': False, 'error': 'Please log in first'})
        
        if not message_link:
            return jsonify({'success': False, 'error': 'Message link required'})
        
        # Generate download ID
        download_id = hashlib.md5(f"{message_link}{time.time()}".encode()).hexdigest()[:8]
        
        # Add to queue
        download_queue.put((download_id, message_link))
        download_tasks[download_id] = {'status': 'queued', 'progress': 0}
        
        return jsonify({
            'success': True,
            'download_id': download_id,
            'message': 'Download started'
        })
        
    except Exception as e:
        logging.error(f"Download request error: {e}")
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
            return send_file(
                task['filepath'],
                as_attachment=True,
                download_name=task['filename']
            )
    
    return "File not found", 404

@app.route('/logout')
def logout():
    """Logout from Telegram"""
    session_id = session.get('telegram_session')
    if session_id and session_id in clients:
        client = clients[session_id]
        
        async def logout_client():
            await client.disconnect()
            if session_id in clients:
                del clients[session_id]
        
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(logout_client())
        loop.close()
    
    session.clear()
    return jsonify({'success': True})

# Create necessary directories
os.makedirs('sessions', exist_ok=True)
os.makedirs('downloads', exist_ok=True)
os.makedirs('templates', exist_ok=True)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
