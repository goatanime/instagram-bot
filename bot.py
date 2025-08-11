import os
import re
import logging
import sqlite3
import asyncio
import shutil
import tempfile
from datetime import datetime, timezone, timedelta
from typing import List, Tuple

import httpx
import yt_dlp
from telegram import (
    Update, InputMediaPhoto, InputMediaVideo, InlineKeyboardButton,
    InlineKeyboardMarkup
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters
)
from telegram.error import TelegramError

# --- Configuration ---

class Config:
    """Holds all application configuration."""
    # Your details have been hardcoded here for simplicity.
    BOT_TOKEN: str = "7259669876:AAGAYunh5Z7IdpQyXpg0mYbe84RX-UzW37g"
    SHORTENER_TOKEN: str = "0f40e7c1f77af23bfabbd4f2afcbeb59bc3b3636"
    ADMIN_ID: int = 7191595289 # Make sure this is an integer, not a string

    # Static settings
    DB_FILE: str = 'ig_users.db'
    # The cookie file is now generic for all platforms
    COOKIES_FILE: str = "cookies.txt"
    DOWNLOAD_DIR: str = tempfile.gettempdir()
    SHORTENER_API_URL: str = "https://shrinkearn.com/api"
    ACCESS_DURATION_HOURS: int = 24

    @staticmethod
    def validate():
        """Ensure critical values are set."""
        if not Config.BOT_TOKEN:
            raise ValueError("FATAL: BOT_TOKEN is not set in the Config class.")
        if not Config.ADMIN_ID:
            logging.warning("ADMIN_ID is not set. Admin notifications will be disabled.")

# --- Logging Setup ---

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)


# --- Database Management ---

class Database:
    """Handles all SQLite database operations."""
    def __init__(self, db_file: str):
        self._conn = sqlite3.connect(db_file, check_same_thread=False)
        self._cursor = self._conn.cursor()
        self._setup()

    def _setup(self):
        """Creates the users table if it doesn't exist."""
        self._cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                access_time TEXT NOT NULL
            )
        ''')
        self._conn.commit()

    def grant_access(self, user_id: int):
        """Grants access to a user."""
        now_utc = datetime.now(timezone.utc).isoformat()
        self._cursor.execute(
            "REPLACE INTO users (user_id, access_time) VALUES (?, ?)",
            (user_id, now_utc)
        )
        self._conn.commit()
        logger.info(f"Access granted for user_id: {user_id}")

    def has_valid_access(self, user_id: int) -> bool:
        """Checks if a user's access is still valid."""
        self._cursor.execute("SELECT access_time FROM users WHERE user_id=?", (user_id,))
        result = self._cursor.fetchone()
        if not result:
            return False
        try:
            access_time = datetime.fromisoformat(result[0]).replace(tzinfo=timezone.utc)
            now_utc = datetime.now(timezone.utc)
            return (now_utc - access_time) < timedelta(hours=Config.ACCESS_DURATION_HOURS)
        except (ValueError, TypeError) as e:
            logger.error(f"Error parsing access time for user {user_id}: {e}")
            return False

    def close(self):
        """Closes the database connection."""
        if self._conn:
            self._conn.close()
            logger.info("Database connection closed.")


# --- Media Downloader ---

class MediaDownloader:
    """Manages media downloads from multiple platforms using yt-dlp."""
    # Combined regex for Instagram, Facebook, and YouTube
    _URL_PATTERN = re.compile(
        r'https?://(?:www\.)?'
        r'(?:'
        r'instagram\.com/(?:p|reel|tv|stories|explore/tags)/[a-zA-Z0-9_.-]+(?:/[0-9]+)?|'
        r'instagram\.com/[a-zA-Z0-9_.-]+/?|'
        r'(?:m\.|web\.)?facebook\.com/(?:watch/?|reel/|[a-zA-Z0-9_.-]+/videos/|[a-zA-Z0-9_.-]+/posts/|video\.php\?v=)[0-9a-zA-Z_.-]+|'
        r'youtube\.com/(?:watch\?v=|shorts/)[a-zA-Z0-9_-]{11}|'
        r'youtu\.be/[a-zA-Z0-9_-]{11}'
        r')'
    )

    @staticmethod
    def is_valid_url(url: str) -> bool:
        return bool(MediaDownloader._URL_PATTERN.match(url))

    @staticmethod
    def _validate_cookies(file_path: str) -> bool:
        if not os.path.exists(file_path) or os.path.getsize(file_path) < 100:
            return False
        with open(file_path, 'r', encoding='utf-8') as f:
            return f.readline().strip().startswith(("# HTTP Cookie File", "# Netscape HTTP Cookie File"))

    def download_media(self, url: str, user_id: int) -> Tuple[List[str], str]:
        temp_dir = tempfile.mkdtemp(prefix=f"media_{user_id}_", dir=Config.DOWNLOAD_DIR)
        cookies_file = Config.COOKIES_FILE if self._validate_cookies(Config.COOKIES_FILE) else None
        if not cookies_file:
            logger.warning("Cookies file not found or invalid. Private content may fail.")

        ydl_opts = {
            'outtmpl': os.path.join(temp_dir, '%(title)s.%(ext)s'),
            'format': 'bestvideo[ext=mp4][height<=1080]+bestaudio[ext=m4a]/best[ext=mp4][height<=1080]/best',
            'cookiefile': cookies_file,
            'ignoreerrors': False, 'quiet': True, 'no_warnings': True,
            'retries': 3, 'fragment_retries': 3,
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
                'Accept-Language': 'en-US,en;q=0.9',
            },
            'postprocessors': [{'key': 'FFmpegVideoConvertor', 'preferedformat': 'mp4'}],
            'max_filesize': '50m', # Set max filesize to avoid Telegram limit issues
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.extract_info(url, download=True)
        except Exception as e:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise e

        downloaded_files = [os.path.join(temp_dir, f) for f in os.listdir(temp_dir)]
        if not downloaded_files:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise ValueError("yt-dlp completed but no files were found.")
        return downloaded_files, temp_dir


# --- Main Bot Class ---

class TelegramBot:
    """The main class for the Telegram Bot."""
    def __init__(self, config: Config):
        self.config = config
        self.db = Database(config.DB_FILE)
        self.downloader = MediaDownloader()
        self.application = Application.builder().token(config.BOT_TOKEN).build()
        self.http_client = httpx.AsyncClient(timeout=10.0)
        self._register_handlers()

    def _register_handlers(self):
        self.application.add_handler(CommandHandler("start", self.start_command))
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))
        self.application.add_handler(MessageHandler(filters.Document.ALL, self.handle_cookie_file))
        self.application.add_error_handler(self.error_handler)

    async def _generate_short_url(self, bot_username: str) -> str:
        deep_link = f"https://t.me/{bot_username}?start=shorte"
        if not self.config.SHORTENER_TOKEN:
            return deep_link
        params = {'api': self.config.SHORTENER_TOKEN, 'url': deep_link}
        try:
            response = await self.http_client.get(self.config.SHORTENER_API_URL, params=params)
            response.raise_for_status()
            data = response.json()
            if data.get('status') == 'success':
                return data['shortenedUrl']
            logger.error(f"Shortener API error: {data.get('message', 'Unknown error')}")
        except Exception as e:
            logger.error(f"Failed to generate short URL: {e}")
        return deep_link

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        logger.info(f"/start command from user {user.id} ({user.username})")
        start_message = (
            "🌟 **Welcome to the All-in-One Media Downloader!**\n\n"
            "Send me a link from Instagram, Facebook, or YouTube, and I'll download it for you.\n\n"
            "Supported content: Reels, Shorts, Videos, and Images."
        )
        if context.args and context.args[0] == "shorte":
            self.db.grant_access(user.id)
            await update.message.reply_text(
                f"🎉 **Premium Access Activated!**\n\nYour free access is valid for {self.config.ACCESS_DURATION_HOURS} hours.\n\n{start_message}",
                parse_mode='Markdown'
            )
        elif self.db.has_valid_access(user.id):
            await update.message.reply_text(f"✅ **Welcome back!**\nYour access is active.\n\n{start_message}", parse_mode='Markdown')
        else:
            bot_username = (await context.bot.get_me()).username
            short_url = await self._generate_short_url(bot_username)
            keyboard = [[InlineKeyboardButton("🔥 GET FREE ACCESS 🔥", url=short_url)]]
            await update.message.reply_text(
                f"🔒 **Premium Access Required**\n\n{start_message}\n\nFirst, click the button below to activate your free 24-hour access.",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode='Markdown'
            )

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        message_text = update.message.text.strip()

        if message_text == "9434" and user_id == self.config.ADMIN_ID:
            self.db.grant_access(user_id)
            await update.message.reply_text("✅ **Admin Bypass:** Access granted for 24 hours.")
            return

        if not self.downloader.is_valid_url(message_text):
            await update.message.reply_text("❌ **Invalid URL**\nPlease send a valid link from Instagram, Facebook, or YouTube.", parse_mode='Markdown')
            return
            
        if not self.db.has_valid_access(user_id):
            bot_username = (await context.bot.get_me()).username
            short_url = await self._generate_short_url(bot_username)
            keyboard = [[InlineKeyboardButton("⏳ RENEW ACCESS ⏳", url=short_url)]]
            await update.message.reply_text(
                "⏱️ **Your access has expired!**\nPlease renew your free access.",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode='Markdown'
            )
            return

        asyncio.create_task(self.process_download_task(update, context, message_text))

    async def process_download_task(self, update: Update, context: ContextTypes.DEFAULT_TYPE, url: str):
        user_id = update.effective_user.id
        msg = await update.message.reply_text("⏬ Downloading, please wait...")
        temp_dir = None
        try:
            files, temp_dir = await asyncio.to_thread(self.downloader.download_media, url, user_id)
            
            await context.bot.edit_message_text("✅ Download complete! Sending media...", chat_id=msg.chat_id, message_id=msg.message_id)
            await self._send_media(update, context, files)
            
            await context.bot.delete_message(chat_id=msg.chat_id, message_id=msg.message_id)

        except yt_dlp.utils.DownloadError as e:
            error_message = self._handle_download_error(e, user_id)
            await context.bot.edit_message_text(error_message, chat_id=msg.chat_id, message_id=msg.message_id, parse_mode='Markdown')
            if "login" in str(e).lower() or "cookies" in str(e).lower() or "restricted" in str(e).lower():
                await self._notify_admin(f"⚠️ A restricted video failed to download for user {user_id}. Cookies may be required.\n\n`{e}`")
        except Exception as e:
            logger.error(f"Unexpected error for user {user_id}: {e}", exc_info=True)
            await context.bot.edit_message_text("❌ An unexpected error occurred.", chat_id=msg.chat_id, message_id=msg.message_id)
        finally:
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)

    def _handle_download_error(self, e: Exception, user_id: int) -> str:
        err_str = str(e).lower()
        logger.warning(f"DownloadError for user {user_id}: {err_str}")
        
        if "sign in to confirm" in err_str or "not a bot" in err_str:
            return "🤖 **Bot-Check Failed**\nYouTube is asking to verify that you're not a bot. The admin needs to provide a `cookies.txt` file to solve this."
        if "login required" in err_str or "private" in err_str:
            return "🔒 **Private Content**\nThis content is private and requires a login session to download. The admin needs to provide a cookie file."
        if "age-restricted" in err_str or "18 years old" in err_str:
            return "🔞 **Age-Restricted Content**\nThis video can't be downloaded because it's marked as 18+. The admin needs to provide a logged-in session."
        if "file is larger than the 50.00mib limit" in err_str:
            return "📦 **File Too Large**\nThis video is larger than 50MB and cannot be sent on Telegram."
        if "429" in err_str or "too many requests" in err_str:
            return "⏳ **Rate Limited**\nThe service is limiting requests. Please try again later."
        if "unsupported url" in err_str:
            return "🔗 **Unsupported URL**\nThis type of link is not supported."
        
        return "❌ **Download Failed**\nAn unknown error occurred."

    async def _send_media(self, update: Update, context: ContextTypes.DEFAULT_TYPE, files: List[str]):
        bot_username = (await context.bot.get_me()).username
        caption = f"Downloaded via @{bot_username}"
        
        videos = sorted([f for f in files if f.lower().endswith(('.mp4', '.mov', '.webm'))])
        photos = sorted([f for f in files if f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp'))])

        all_media_paths = photos + videos

        if not all_media_paths:
            await update.message.reply_text("🤔 Could not find any supported media in the link.")
            return

        if len(all_media_paths) == 1:
            path = all_media_paths[0]
            with open(path, 'rb') as file:
                if path in photos:
                    await update.message.reply_photo(photo=file, caption=caption)
                else:
                    await update.message.reply_video(video=file, caption=caption, supports_streaming=True)
            return

        media_group = []
        file_handlers = []
        try:
            first_path = all_media_paths[0]
            first_file = open(first_path, 'rb')
            file_handlers.append(first_file)
            if first_path in photos:
                media_group.append(InputMediaPhoto(media=first_file, caption=caption))
            else:
                media_group.append(InputMediaVideo(media=first_file, caption=caption))
            
            for path in all_media_paths[1:]:
                file = open(path, 'rb')
                file_handlers.append(file)
                if path in photos:
                    media_group.append(InputMediaPhoto(media=file))
                else:
                    media_group.append(InputMediaVideo(media=file))

            for i in range(0, len(media_group), 10):
                await update.message.reply_media_group(media=media_group[i:i+10])

        finally:
            for f in file_handlers:
                f.close()

    async def handle_cookie_file(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != self.config.ADMIN_ID:
            return
        if not update.message.document or not update.message.document.file_name.endswith('.txt'):
            await update.message.reply_text("Please upload the cookie file as a `.txt` document.")
            return
        try:
            file = await context.bot.get_file(update.message.document.file_id)
            # The file should be named cookies.txt now
            await file.download_to_drive(self.config.COOKIES_FILE)
            if self.downloader._validate_cookies(self.config.COOKIES_FILE):
                await update.message.reply_text("✅ **Cookies updated successfully!** This will be used for all platforms.")
                logger.info(f"Cookies file updated by admin.")
            else:
                os.remove(self.config.COOKIES_FILE)
                await update.message.reply_text("❌ **Invalid Cookies File**.")
        except Exception as e:
            logger.error(f"Failed to update cookie file: {e}")
            await update.message.reply_text(f"❌ Error updating cookies: {e}")

    async def error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE):
        logger.error(f"Update {update} caused error: {context.error}", exc_info=context.error)

    async def post_init(self, application: Application):
        """Actions to run after initialization but before polling starts."""
        await self._notify_admin("🔔 Bot is starting up...")
        if not self.downloader._validate_cookies(self.config.COOKIES_FILE):
            await self._notify_admin("⚠️ **Warning:** `cookies.txt` is missing or invalid. Private or restricted content may fail to download.")

    async def _notify_admin(self, text: str):
        if not self.config.ADMIN_ID:
            return
        try:
            await self.application.bot.send_message(chat_id=self.config.ADMIN_ID, text=text, parse_mode='Markdown')
        except TelegramError as e:
            if "chat not found" in str(e).lower():
                logger.error(f"Failed to notify admin ({self.config.ADMIN_ID}): Chat not found. Please ensure the admin has started a chat with the bot by sending /start.")
            else:
                logger.error(f"Failed to send notification to admin {self.config.ADMIN_ID}: {e}")


def main():
    """The main entry point for the bot."""
    Config.validate()
    bot = TelegramBot(Config())

    bot.application.post_init = bot.post_init

    logger.info("All-in-One Media Downloader Bot is now running.")
    
    bot.application.run_polling()


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot shutdown initiated.")
    except ValueError as e:
        logger.critical(e)
