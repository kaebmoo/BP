"""Check that /cancel routes correctly in the bot's handler chain.

Cannot live in tests/ — tests/conftest.py replaces `telegram` with MagicMocks,
so handler routing can't be exercised there. Run directly:

    .venv/bin/python -m scripts.check_bot_cancel_routing
"""
from datetime import datetime, timezone
from types import SimpleNamespace

from telegram import Chat, Message, MessageEntity, Update, User
from telegram.ext import CommandHandler, ConversationHandler, MessageHandler

from app.bot.main import build_application

CHAT_ID = 424242


def _update(text):
    # Telegram always tags a leading "/word" as a bot_command entity; without it
    # filters.COMMAND is False and CommandHandler never matches.
    entities = ([MessageEntity(MessageEntity.BOT_COMMAND, 0, len(text.split()[0]))]
                if text.startswith("/") else [])
    msg = Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=CHAT_ID, type=Chat.PRIVATE),
        from_user=User(id=CHAT_ID, first_name="T", is_bot=False),
        text=text,
        entities=entities,
    )
    # CommandHandler resolves "/cmd@botname" against the bot's username.
    msg.set_bot(SimpleNamespace(username="bp_test_bot"))
    return Update(update_id=1, message=msg)


def _first_match(handlers, update):
    """The handler PTB would actually run: first match wins within a group."""
    for h in handlers:
        if h.check_update(update) not in (None, False):
            return h
    return None


def main():
    handlers = build_application().handlers[0]
    unknown_cmd = next(h for h in handlers
                       if isinstance(h, MessageHandler) and str(h.filters) == "filters.COMMAND")

    # 1. Idle /cancel reaches the real cancel handler, not the unknown-command fallback.
    match = _first_match(handlers, _update("/cancel"))
    assert isinstance(match, CommandHandler) and "cancel" in match.commands, \
        f"idle /cancel routed to {type(match).__name__}, expected CommandHandler(cancel)"

    # 2. Mid-conversation /cancel still goes to that conversation's fallback.
    conv = next(h for h in handlers
                if isinstance(h, ConversationHandler)
                and any(isinstance(f, CommandHandler) and "cancel" in f.commands
                        for f in h.fallbacks))
    conv._conversations[(CHAT_ID, CHAT_ID)] = next(iter(conv.states))
    try:
        match = _first_match(handlers, _update("/cancel"))
        assert match is conv, \
            f"in-conversation /cancel routed to {type(match).__name__}, expected ConversationHandler"
    finally:
        conv._conversations.clear()

    # 3. A genuinely unknown command still hits the fallback.
    match = _first_match(handlers, _update("/satts"))
    assert match is unknown_cmd, \
        f"/satts routed to {type(match).__name__}, expected the unknown-command fallback"

    print("OK: idle /cancel -> cancel handler | in-conversation -> ConversationHandler | /satts -> fallback")


if __name__ == "__main__":
    main()
