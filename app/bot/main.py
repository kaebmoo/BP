
import logging
import os
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, CommandHandler, ContextTypes, TypeHandler, CallbackQueryHandler
from telegram.error import NetworkError, TimedOut, TelegramError
from telegram.request import HTTPXRequest
from .handlers import (get_auth_handler, get_ocr_handler, get_manual_bp_handler,
                       get_profile_handler, get_delete_handler, get_edit_handler,
                       get_password_handler, get_deactivate_handler,
                       get_broadcast_handler, get_notification_settings_handler,
                       stats, help_command, unknown, unknown_command, cancel, bp_command,
                       language_command, language_callback,
                       settings_command, settings_callback, timezone_callback,
                       notification_callback)
from .payment_handlers import get_payment_handler, subscription_command
import warnings
from telegram.warnings import PTBUserWarning

# Suppress PTBUserWarning about CallbackQueryHandler in ConversationHandler
warnings.filterwarnings("ignore", category=PTBUserWarning, message=".*CallbackQueryHandler.*")

from .log_service import BotLogService

async def log_middleware(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Log all incoming updates with sensitive data masking.

    Text messages are classified by conversation state so that passwords,
    names, DOB, and other PII are never written to logs in clear text.
    """
    try:
        user = update.effective_user
        if not user:
            return

        msg_type = "unknown"
        content = ""

        if update.message:
            if update.message.text:
                text = update.message.text

                # Commands are safe to log as-is
                if text.startswith('/'):
                    msg_type = "command"
                    content = text
                else:
                    # Classify by conversation state stored in user_data
                    conv_state = _detect_conversation_state(context)
                    msg_type, content = _classify_text_input(text, conv_state)

            elif update.message.photo:
                msg_type = "photo"
                content = "User sent a photo"

            elif update.message.document:
                msg_type = "document"
                content = f"Document: {update.message.document.mime_type or 'unknown'}"

            elif update.message.contact:
                msg_type = "contact"
                # Mask phone number
                from .log_service import mask_phone
                phone = update.message.contact.phone_number or ""
                content = f"Contact: {mask_phone(phone)}"

        elif update.callback_query:
            msg_type = "callback"
            cb_data = update.callback_query.data or ""
            content = f"Data: {cb_data}"

        if content:
            BotLogService.log(
                telegram_id=user.id,
                direction="IN",
                message_type=msg_type,
                content=content
            )
    except Exception as e:
        logger.error(f"Logging Error: {e}")


# --- Conversation state detection for log masking ---

# These keys are set by ConversationHandler internally
_CONV_KEY_AUTH = "auth_conversation"
_CONV_KEY_OCR = "ocr_conversation"
_CONV_KEY_MANUAL = "manual_bp_conversation"

# State numbers (mirror handlers.py)
_STATE_AUTH_PASSWORD = 1
_STATE_REG_NAME = 2
_STATE_REG_DOB = 3
_STATE_REG_GENDER = 4
_STATE_REG_ROLE = 5
_STATE_REG_PASSWORD = 6


def _detect_conversation_state(context: ContextTypes.DEFAULT_TYPE) -> str:
    """
    Try to infer what the user is currently inputting based on
    ConversationHandler state stored in context.
    Returns a hint string: 'password', 'name', 'dob', 'gender', 'role', or 'general'.
    """
    try:
        # ConversationHandler stores state in context.user_data under special keys
        # but the actual key format depends on the handler name / entry.
        # Simpler approach: check user_data flags set by our handlers.
        ud = context.user_data or {}

        # If we're in registration/auth flow, check what step
        # Our handlers store 'register_lang' during auth flow
        if ud.get('_auth_state'):
            state = ud['_auth_state']
            if state == 'auth_password':
                return 'password'
            elif state == 'reg_name':
                return 'name'
            elif state == 'reg_dob':
                return 'dob'
            elif state == 'reg_gender':
                return 'gender'
            elif state == 'reg_role':
                return 'role'
            elif state == 'reg_password':
                return 'password'

    except Exception:
        pass

    return 'general'


def _classify_text_input(text: str, conv_state: str) -> tuple:
    """
    Return (msg_type, masked_content) based on detected conversation state.
    """
    from .log_service import mask_text, mask_name, mask_dob

    if conv_state == 'password':
        return ('text:password', mask_text(text))

    if conv_state == 'name':
        return ('text:name', mask_name(text))

    if conv_state == 'dob':
        return ('text:dob', mask_dob(text))

    if conv_state in ('gender', 'role'):
        # Gender/role are selection values, safe to log
        return (f'text:{conv_state}', text)

    # General text — apply pattern-based masking as safety net
    # Don't log raw text; it could be password for existing user login
    # Log length + first 2 chars only for debugging
    if len(text) <= 2:
        return ('text', mask_text(text))
    else:
        # For non-state text: show safely — mask if it looks like PII
        from .log_service import BotLogService
        masked = BotLogService._mask_text_patterns(text)
        return ('text', masked)

load_dotenv()

# Configure Logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# Suppress verbose httpx logging
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# Track connection state
IS_CONNECTION_LOST = False

async def connection_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Monitor successfully received updates to log reconnection."""
    global IS_CONNECTION_LOST
    if IS_CONNECTION_LOST:
        logger.info("Connection restored! Resuming update processing...")
        IS_CONNECTION_LOST = False

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log the error and handle connection issues gracefully."""
    global IS_CONNECTION_LOST

    if isinstance(context.error, NetworkError):
        if not IS_CONNECTION_LOST:
            logger.warning(f"Network Error detected: {context.error}. Retrying...")
            IS_CONNECTION_LOST = True
        return

    if isinstance(context.error, TimedOut):
        if not IS_CONNECTION_LOST:
            logger.warning("Request Timed Out. Retrying...")
            IS_CONNECTION_LOST = True
        return

    logger.error(msg="Exception while handling an update:", exc_info=context.error)


def build_application():
    """Build and configure the Telegram Application with all handlers.
    Returns the Application instance (not yet running).
    """
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN not found in environment variables.")

    # Configure connection timeouts via HTTPXRequest
    request = HTTPXRequest(
        connect_timeout=30.0,
        read_timeout=30.0,
        write_timeout=30.0
    )

    application = ApplicationBuilder().token(token).request(request).build()

    # Monitor connection state (Runs first)
    application.add_handler(TypeHandler(Update, connection_monitor), group=-1)

    # Log Middleware (Runs in separate group to ensure execution)
    application.add_handler(TypeHandler(Update, log_middleware), group=-5)

    # Add Error Handler
    application.add_error_handler(error_handler)

    # Auth & Registration Conversation
    application.add_handler(get_auth_handler())

    # Payment / Subscription
    application.add_handler(get_payment_handler())
    application.add_handler(CommandHandler("subscription", subscription_command))

    # OCR & Record Logic
    application.add_handler(get_ocr_handler())

    # ALL ConversationHandlers that accept TEXT input in their states
    # MUST be registered BEFORE manual_bp_handler, because manual_bp uses
    # a Regex entry_point (BP_TEXT_PATTERN) that matches values like "140 89 78"
    # which would intercept text input meant for other active conversations.
    application.add_handler(get_edit_handler())
    application.add_handler(get_delete_handler())
    application.add_handler(get_profile_handler())
    application.add_handler(get_password_handler())
    application.add_handler(get_deactivate_handler())
    application.add_handler(get_broadcast_handler())
    # Notification add-time conversation MUST precede the plain ^notif_
    # callback handler below so 'notif_add' reaches the conversation entry.
    application.add_handler(get_notification_settings_handler())

    # Manual BP Text Input (e.g., "130 90 65") — MUST be LAST ConversationHandler
    application.add_handler(get_manual_bp_handler())

    # Simple Commands
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CommandHandler("bp", bp_command))
    application.add_handler(CommandHandler("help", help_command))

    # Language
    application.add_handler(CommandHandler("language", language_command))
    application.add_handler(CallbackQueryHandler(language_callback, pattern='^lang_'))

    # Settings with Timezone
    application.add_handler(CommandHandler("settings", settings_command))
    application.add_handler(CallbackQueryHandler(settings_callback, pattern='^settings_'))
    application.add_handler(CallbackQueryHandler(timezone_callback, pattern='^tz_'))
    application.add_handler(CallbackQueryHandler(notification_callback, pattern='^notif_(menu|toggle|del:)'))

    # /cancel outside a conversation — inside one, the ConversationHandler
    # fallbacks above match first, so this only fires when nothing is active.
    application.add_handler(CommandHandler("cancel", cancel))

    # Fallback for unknown messages (Must be last)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown))
    # Fallback for mistyped commands not matched above (e.g. /satts)
    application.add_handler(MessageHandler(filters.COMMAND, unknown_command))

    return application


def run_polling():
    """Run the bot in long-polling mode (for VPS/local dev)."""
    application = build_application()
    print("Bot is running in polling mode... (Press Ctrl+C to stop)")
    application.run_polling(poll_interval=1.0, timeout=30)


def main():
    run_polling()


if __name__ == '__main__':
    main()
