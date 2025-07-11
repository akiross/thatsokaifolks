#!/usr/bin/env python3
"""
Simple Telegram notification bot with HTTP endpoint.

Users can register topics via Telegram and get UUIDs.
External services can POST to /<uuid> to send notifications.
"""

import asyncio
import json
import logging
import os
import sqlite3
import uuid
from typing import Dict, Optional, List

import aiohttp
from aiohttp import web
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)


class TopicDatabase:
    def __init__(self, db_path: str = "telegram_topics.db"):
        self.db_path = db_path
        self.init_database()
    
    def init_database(self):
        """Initialize the SQLite database and create tables"""
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS topics (
                    topic_name TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Create index for faster lookups
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_user_id ON topics(user_id)
            ''')
            
            conn.commit()
            logger.info(f"Database initialized at {self.db_path}")
        finally:
            conn.close()
    
    def add_topic(self, topic_name: str, user_id: int, chat_id: int) -> bool:
        """Add a new topic to the database"""
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO topics (topic_name, user_id, chat_id)
                VALUES (?, ?, ?)
            ''', (topic_name, user_id, chat_id))
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False  # Topic name already exists globally
        finally:
            conn.close()
    
    def get_topic(self, topic_name: str) -> Optional[Dict]:
        """Get a topic by name"""
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT topic_name, user_id, chat_id, created_at
                FROM topics WHERE topic_name = ?
            ''', (topic_name,))
            row = cursor.fetchone()
            if row:
                return {
                    "topic_name": row[0],
                    "user_id": row[1],
                    "chat_id": row[2],
                    "created_at": row[3]
                }
            return None
        finally:
            conn.close()
    
    def get_user_topics(self, user_id: int) -> List[Dict]:
        """Get all topics for a user"""
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT topic_name, user_id, chat_id, created_at
                FROM topics WHERE user_id = ?
                ORDER BY created_at DESC
            ''', (user_id,))
            rows = cursor.fetchall()
            return [
                {
                    "topic_name": row[0],
                    "user_id": row[1],
                    "chat_id": row[2],
                    "created_at": row[3]
                }
                for row in rows
            ]
        finally:
            conn.close()
    
    def find_topic_by_name(self, user_id: int, topic_name: str) -> Optional[Dict]:
        """Find a topic by user ID and topic name"""
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT topic_name, user_id, chat_id, created_at
                FROM topics WHERE user_id = ? AND topic_name = ?
            ''', (user_id, topic_name))
            row = cursor.fetchone()
            if row:
                return {
                    "topic_name": row[0],
                    "user_id": row[1],
                    "chat_id": row[2],
                    "created_at": row[3]
                }
            return None
        finally:
            conn.close()
    
    def delete_topic(self, user_id: int, topic_name: str) -> bool:
        """Delete a topic by user ID and topic name"""
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()
            cursor.execute('''
                DELETE FROM topics WHERE user_id = ? AND topic_name = ?
            ''', (user_id, topic_name))
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()


# Global database instance
db = TopicDatabase()


class NotifierBot:
    def __init__(self, bot_token: str, webhook_port: int = 8080):
        self.bot_token = bot_token
        self.webhook_port = webhook_port
        self.app = Application.builder().token(bot_token).build()
        
        # Register command handlers
        self.app.add_handler(CommandHandler("start", self.start_command))
        self.app.add_handler(CommandHandler("help", self.help_command))
        self.app.add_handler(CommandHandler("register", self.register_command))
        self.app.add_handler(CommandHandler("unregister", self.unregister_command))
        self.app.add_handler(CommandHandler("list", self.list_topics_command))

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command"""
        welcome_msg = (
            "🤖 Welcome to the Notification Bot!\n\n"
            "Commands:\n"
            "• /register <topic_name> - Register a new topic and get a UUID\n"
            "• /unregister <topic_name> - Unregister a topic\n"
            "• /list - List your registered topics\n"
            "• /help - Show this help message\n\n"
            "After registering a topic, you'll get a UUID that others can use to send you notifications via HTTP POST."
        )
        await update.message.reply_text(welcome_msg)

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /help command"""
        await self.start_command(update, context)

    async def register_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /register <topic_name> command"""
        if not context.args:
            await update.message.reply_text("Please provide a topic name: /register <topic_name>")
            return

        topic_name = " ".join(context.args)
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id

        # Validate topic name (no special characters for URL safety)
        if not topic_name.replace("-", "").replace("_", "").isalnum():
            await update.message.reply_text("❌ Topic name can only contain letters, numbers, hyphens, and underscores.")
            return

        # Register topic (topic name is globally unique)
        if db.add_topic(topic_name, user_id, chat_id):
            await update.message.reply_text(
                f"✅ Topic '{topic_name}' registered!\n\n"
                f"🔗 Webhook endpoint: `/{topic_name}`\n\n"
                f"Others can now POST to: `http://your-server:8080/{topic_name}`",
                parse_mode='Markdown'
            )
        else:
            await update.message.reply_text(f"❌ Topic '{topic_name}' is already taken. Please choose a different name.")

    async def unregister_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /unregister <topic_name> command"""
        if not context.args:
            await update.message.reply_text("Please provide a topic name: /unregister <topic_name>")
            return

        topic_name = " ".join(context.args)
        user_id = update.effective_user.id

        # Delete topic from database
        if db.delete_topic(user_id, topic_name):
            await update.message.reply_text(f"✅ Topic '{topic_name}' unregistered!")
        else:
            await update.message.reply_text(f"❌ Topic '{topic_name}' not found!")

    async def list_topics_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /list command"""
        user_id = update.effective_user.id
        user_topics = db.get_user_topics(user_id)

        if not user_topics:
            await update.message.reply_text("📋 You have no registered topics.")
            return

        topic_list = []
        for topic in user_topics:
            topic_list.append(f"• `{topic['topic_name']}`")

        message = "📋 Your registered topics:\n\n" + "\n".join(topic_list)
        await update.message.reply_text(message, parse_mode='Markdown')


async def webhook_handler(request):
    """Handle HTTP POST requests to /<topic_name>"""
    topic_name = request.match_info.get('topic_name')
    
    logger.info(f"Webhook request received for topic: {topic_name}")
    logger.info(f"Content-Type: {request.content_type}")
    
    # Get topic from database
    topic_info = db.get_topic(topic_name)
    if not topic_info:
        return web.Response(status=404, text="Topic not found")

    try:
        # Get raw body first for debugging
        raw_body = await request.read()
        logger.info(f"Raw request body: {repr(raw_body)}")
        logger.info(f"Raw body length: {len(raw_body)}")
        
        # Get message from request body
        if request.content_type == 'application/json':
            try:
                # First try to clean control characters from the raw body
                import json
                clean_raw_body = ''.join(chr(b) if 32 <= b <= 126 or b in [9, 10, 13] else ' ' for b in raw_body)
                logger.info(f"Cleaned raw body: {repr(clean_raw_body)}")
                
                data = json.loads(clean_raw_body)
                message = data.get('message', str(data))
                logger.info(f"Parsed JSON data: {repr(data)}")
            except json.JSONDecodeError as e:
                logger.error(f"Failed to parse JSON from request even after cleaning: {e}")
                logger.info(f"Raw body that failed: {repr(raw_body)}")
                # Fallback: treat the entire body as text message
                message = raw_body.decode('utf-8', errors='replace')
                logger.info(f"Fallback: treating as text message: {repr(message)}")
        else:
            message = raw_body.decode('utf-8', errors='replace')
            logger.info(f"Decoded text message: {repr(message)}")

        if not message:
            return web.Response(status=400, text="No message provided")

        # Log the raw message for debugging
        logger.info(f"Received message for topic '{topic_name}': {repr(message)}")
        logger.info(f"Message length: {len(message)}, type: {type(message)}")

        # Get topic info
        chat_id = topic_info['chat_id']

        # Send notification via Telegram
        bot_token = os.getenv('TELEGRAM_BOT_TOKEN')
        telegram_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        
        # Check if message looks like JSON and format it appropriately
        formatted_message = message
        try:
            # Try to parse as JSON to validate and pretty-print
            import json
            logger.info(f"Attempting to parse as JSON: {message[:100]}...")
            parsed_json = json.loads(message)
            # Format as code block for better readability
            formatted_message = f"```json\n{json.dumps(parsed_json, indent=2)}\n```"
            logger.info("Successfully parsed and formatted as JSON")
        except (json.JSONDecodeError, TypeError) as e:
            # Not JSON or invalid JSON, escape potential markdown characters and remove control characters
            logger.info(f"JSON parsing failed: {e}. Treating as regular text.")
            # Remove control characters that cause JSON encoding issues
            clean_message = ''.join(char for char in message if ord(char) >= 32 or char in '\n\r\t')
            formatted_message = clean_message.replace('_', '\\_').replace('*', '\\*').replace('[', '\\[').replace(']', '\\]').replace('`', '\\`')
        
        notification_text = f"🔔 **{topic_name}**\n\n{formatted_message}"
        
        # Log the final message being sent to Telegram
        logger.info(f"Sending to Telegram: {repr(notification_text)}")
        logger.info(f"Final message length: {len(notification_text)}")
        
        async with aiohttp.ClientSession() as session:
            payload = {
                'chat_id': chat_id,
                'text': notification_text,
                'parse_mode': 'Markdown'
            }
            logger.info(f"Telegram payload: {repr(payload)}")
            async with session.post(telegram_url, json=payload) as resp:
                if resp.status == 200:
                    return web.Response(status=200, text="Notification sent")
                else:
                    logger.error(f"Failed to send Telegram message: {resp.status}")
                    return web.Response(status=500, text="Failed to send notification")

    except Exception as e:
        logger.error(f"Error processing webhook: {e}")
        return web.Response(status=500, text="Internal server error")


async def create_webhook_app():
    """Create the HTTP webhook application"""
    app = web.Application()
    app.router.add_post('/{topic_name}', webhook_handler)
    
    # Health check endpoint
    async def health_check(request):
        return web.Response(text="OK")
    
    app.router.add_get('/health', health_check)
    return app


async def main():
    """Main function to run both bot and webhook server"""
    bot_token = os.getenv('TELEGRAM_BOT_TOKEN')
    if not bot_token:
        logger.error("TELEGRAM_BOT_TOKEN environment variable is required")
        return

    webhook_port = int(os.getenv('WEBHOOK_PORT', '8080'))

    # Initialize bot
    notifier_bot = NotifierBot(bot_token, webhook_port)
    
    # Start webhook server
    webhook_app = await create_webhook_app()
    runner = web.AppRunner(webhook_app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', webhook_port)
    await site.start()
    
    logger.info(f"Webhook server started on port {webhook_port}")

    # Initialize and start the bot manually to avoid run_polling's event loop issues
    await notifier_bot.app.initialize()
    await notifier_bot.app.start()
    
    logger.info("Starting Telegram bot...")
    await notifier_bot.app.updater.start_polling(drop_pending_updates=True)
    
    try:
        # Keep the program running
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        await notifier_bot.app.updater.stop()
        await notifier_bot.app.stop()
        await notifier_bot.app.shutdown()
        await runner.cleanup()


if __name__ == '__main__':
    asyncio.run(main())