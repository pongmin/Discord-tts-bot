import asyncio
import os
import time
import discord
import pathlib
import tempfile
import json
import urllib.parse
import aiohttp

from tts_text import clean_tts_text


# 🔽 여기 추가
USER_TTS_SETTINGS_FILE = "user_tts_settings.json"


# 🔽 여기도 추가
def load_user_tts_settings():
    if not os.path.exists(USER_TTS_SETTINGS_FILE):
        return {}

    try:
        with open(USER_TTS_SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        return {int(user_id): setting for user_id, setting in data.items()}

    except Exception as e:
        print("유저 TTS 설정 로드 실패:", repr(e))
        return {}


def save_user_tts_settings():
    try:
        with open(USER_TTS_SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(USER_TTS_SETTINGS, f, ensure_ascii=False, indent=4)

    except Exception as e:
        print("유저 TTS 설정 저장 실패:", repr(e))


# 🔽 여기서 불러오기
USER_TTS_SETTINGS = load_user_tts_settings()

MAX_QUEUE_SIZE = 15
FFMPEG_PATH = os.getenv("FFMPEG_PATH", "ffmpeg")

TTS_DIR = pathlib.Path(tempfile.gettempdir()) / "discord_tts"
TTS_DIR.mkdir(exist_ok=True)

tts_queues = {}   # guild_id -> asyncio.Queue
tts_playing = {}  # guild_id -> bool
tts_locks = {}    # guild_id -> asyncio.Lock


# =========================
# TTS 파일 생성
# =========================

async def make_tts_file(text: str, filename: str, engine="gtts", voice="Kim"):

    if engine == "gtts":
        from gtts import gTTS

        def save():
            tts = gTTS(text=text, lang="ko")
            tts.save(filename)

        await asyncio.to_thread(save)

    elif engine == "se":
        encoded = urllib.parse.quote(text)
        url = f"https://api.streamelements.com/kappa/v2/speech?voice={voice}&text={encoded}"

        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                data = await resp.read()

        with open(filename, "wb") as f:
            f.write(data)


# =========================
# TTS 큐 처리
# =========================

async def add_tts_queue(bot, message: discord.Message):
    if message.guild is None:
        return

    guild_id = message.guild.id
    text = clean_tts_text(message.content)

    if not text:
        return

    queue = tts_queues.setdefault(
        guild_id,
        asyncio.Queue(maxsize=MAX_QUEUE_SIZE)
    )

    if queue.full():
        print(f"[{guild_id}] 큐가 가득 참 - 메시지 드랍")
        return

    await queue.put((message, text))

    lock = tts_locks.setdefault(guild_id, asyncio.Lock())

    if not tts_playing.get(guild_id, False):
        tts_playing[guild_id] = True
        asyncio.create_task(play_tts_queue(bot, message.guild, lock))


async def add_bot_tts_queue(bot, guild: discord.Guild, channel: discord.TextChannel, text: str):
    guild_id = guild.id

    text = clean_tts_text(text)
    if not text:
        return

    queue = tts_queues.setdefault(
        guild_id,
        asyncio.Queue(maxsize=MAX_QUEUE_SIZE)
    )

    if queue.full():
        print(f"[{guild_id}] 큐가 가득 참 - 봇 반응 TTS 드랍")
        return

    class BotTTSMessage:
        def __init__(self, guild, channel, text):
            self.guild = guild
            self.channel = channel
            self.content = text
            self.id = int(time.time() * 1000)
            self.author = bot.user

    fake_message = BotTTSMessage(guild, channel, text)

    await queue.put((fake_message, text))

    lock = tts_locks.setdefault(guild_id, asyncio.Lock())

    if not tts_playing.get(guild_id, False):
        tts_playing[guild_id] = True
        asyncio.create_task(play_tts_queue(bot, guild, lock))


async def play_tts_queue(bot, guild: discord.Guild, lock: asyncio.Lock):
    guild_id = guild.id

    async with lock:
        try:
            while guild_id in tts_queues and not tts_queues[guild_id].empty():
                message, text = await tts_queues[guild_id].get()

                voice_client = guild.voice_client

                if voice_client is None:
                    print("봇이 음성채널에 없음")
                    continue

                filename = str(TTS_DIR / f"tts_{guild_id}_{message.id}.mp3")

                try:
                    user_id = message.author.id

                    setting = USER_TTS_SETTINGS.get(
                        user_id,
                        {"engine": "gtts"}
                    )

                    await make_tts_file(
                        text,
                        filename,
                        engine=setting.get("engine", "gtts"),
                        voice=setting.get("voice", "Kim")
                    )

                    audio_source = discord.FFmpegPCMAudio(
                        filename,
                        executable=FFMPEG_PATH
                    )

                    done = asyncio.Event()

                    def after_playing(error):
                        if error:
                            print("재생 오류:", error)

                        bot.loop.call_soon_threadsafe(done.set)

                    voice_client.play(audio_source, after=after_playing)

                    print(
                        f"TTS 재생: {message.author}: {text} "
                        f"[engine={setting.get('engine', 'gtts')}, "
                        f"voice={setting.get('voice', '-')}]"
                    )

                    await done.wait()

                    await asyncio.sleep(0.05)

                except Exception as e:
                    print("TTS ERROR:", repr(e))

                finally:
                    try:
                        if os.path.exists(filename):
                            os.remove(filename)

                    except Exception as e:
                        print("파일 삭제 실패:", repr(e))

        finally:
            tts_playing[guild_id] = False

            if guild_id in tts_queues and not tts_queues[guild_id].empty():
                tts_playing[guild_id] = True
                asyncio.create_task(play_tts_queue(bot, guild, lock))