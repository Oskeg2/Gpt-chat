import asyncio
import os
from collections import defaultdict

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message
from openai import AsyncOpenAI

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
MODEL = "gpt-4o-mini"  # можно заменить на другую модель
SYSTEM_PROMPT = "Ты дружелюбный помощник. Отвечай кратко и по делу."
MAX_HISTORY = 20  # сколько последних сообщений помнить

bot = Bot(TELEGRAM_TOKEN)
dp = Dispatcher()
client = AsyncOpenAI(api_key=OPENAI_API_KEY)

# история диалога для каждого чата
history = defaultdict(list)


@dp.message(Command("start"))
async def start(message: Message):
    await message.answer("Привет! Напиши мне что-нибудь. /reset — очистить память.")


@dp.message(Command("reset"))
async def reset(message: Message):
    history[message.chat.id].clear()
    await message.answer("Память очищена.")


@dp.message(F.text)
async def chat(message: Message):
    msgs = history[message.chat.id]
    msgs.append({"role": "user", "content": message.text})
    del msgs[:-MAX_HISTORY]

    await bot.send_chat_action(message.chat.id, "typing")
    try:
        response = await client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "system", "content": SYSTEM_PROMPT}] + msgs,
        )
        answer = response.choices[0].message.content
    except Exception as e:
        await message.answer(f"Ошибка: {e}")
        return

    msgs.append({"role": "assistant", "content": answer})
    # лимит Telegram — 4096 символов на сообщение
    for i in range(0, len(answer), 4000):
        await message.answer(answer[i:i + 4000])


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
