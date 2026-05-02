import asyncio
import os
import time
import discord
import pathlib
import tempfile
from gtts import gTTS
from tts_text import clean_tts_text


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

async def make_tts_file(text: str, filename: str):
    def save():
        tts = gTTS(text=text, lang="ko")
        tts.save(filename)

    await asyncio.to_thread(save)


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
                    await make_tts_file(text, filename)

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

                    print(f"TTS 재생: {message.author}: {text}")

                    await done.wait()
                    await asyncio.sleep(0.03)

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