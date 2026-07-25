from typing import Any

from telegram.ext import Application, CallbackContext, ExtBot, JobQueue

type UserData = dict[Any, Any]
type ChatData = dict[Any, Any]
type BotData = dict[Any, Any]
type TelegramContext = CallbackContext[ExtBot[None], UserData, ChatData, BotData]
type TelegramApplication = Application[
    ExtBot[None],
    TelegramContext,
    UserData,
    ChatData,
    BotData,
    JobQueue[TelegramContext],
]
