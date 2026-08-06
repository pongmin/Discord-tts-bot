import asyncio
import io
import os
import json
import pathlib
import urllib.parse
from dataclasses import dataclass

import discord
from gtts import gTTS

from http_session import get_session
from tts_text import clean_tts_text


BASE_DIR = pathlib.Path(__file__).resolve().parent
USER_TTS_SETTINGS_FILE = BASE_DIR / "user_tts_settings.json"


def load_user_tts_settings():
    if not USER_TTS_SETTINGS_FILE.exists():
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


USER_TTS_SETTINGS = load_user_tts_settings()

MAX_QUEUE_SIZE = 15
FFMPEG_PATH = os.getenv("FFMPEG_PATH", "ffmpeg")

# after 콜백이 끝내 안 불리는 경우에도 큐가 영구히 멈추지 않도록
MAX_PLAYBACK_SECONDS = 60

tts_queues = {}   # guild_id -> asyncio.Queue
tts_workers = {}  # guild_id -> asyncio.Task


@dataclass
class TTSRequest:
    author_id: int
    author_name: str
    text: str


# =========================
# TTS 오디오 생성
# =========================

async def make_tts_audio(text: str, engine: str = "gtts", voice: str = "Kim") -> bytes:
    """
    TTS 결과를 파일이 아니라 바이트로 돌려줌.
    ffmpeg에 그대로 파이프로 넣을 거라 임시 파일이 필요 없음.
    """
    if engine == "se":
        encoded = urllib.parse.quote(text)
        url = (
            "https://api.streamelements.com/kappa/v2/speech"
            f"?voice={voice}&text={encoded}"
        )

        async with get_session().get(url) as resp:
            # 에러 페이지를 mp3로 받아서 ffmpeg가 깨지는 걸 막음
            resp.raise_for_status()
            return await resp.read()

    def render() -> bytes:
        buffer = io.BytesIO()
        gTTS(text=text, lang="ko").write_to_fp(buffer)
        return buffer.getvalue()

    return await asyncio.to_thread(render)


# =========================
# TTS 큐 처리
# =========================

async def add_tts_queue(bot, message: discord.Message):
    if message.guild is None:
        return

    _submit(
        bot,
        message.guild,
        message.author.id,
        str(message.author),
        message.content
    )


async def add_bot_tts_queue(bot, guild: discord.Guild, text: str):
    _submit(bot, guild, bot.user.id, str(bot.user), text)


def _submit(bot, guild: discord.Guild, author_id: int, author_name: str, raw_text: str):
    text = clean_tts_text(raw_text)

    if not text:
        return

    queue = tts_queues.setdefault(
        guild.id,
        asyncio.Queue(maxsize=MAX_QUEUE_SIZE)
    )

    try:
        queue.put_nowait(TTSRequest(author_id, author_name, text))

    except asyncio.QueueFull:
        print(f"[{guild.id}] 큐가 가득 참 - 메시지 드랍")
        return

    worker = tts_workers.get(guild.id)

    if worker is None or worker.done():
        tts_workers[guild.id] = asyncio.create_task(_tts_worker(guild))


async def _play(voice_client: discord.VoiceClient, audio: bytes):
    source = discord.FFmpegPCMAudio(
        io.BytesIO(audio),
        pipe=True,
        executable=FFMPEG_PATH
    )

    loop = asyncio.get_running_loop()
    done = asyncio.Event()

    def after_playing(error):
        if error:
            print("재생 오류:", error)

        loop.call_soon_threadsafe(done.set)

    voice_client.play(source, after=after_playing)

    try:
        await asyncio.wait_for(done.wait(), timeout=MAX_PLAYBACK_SECONDS)

    except asyncio.TimeoutError:
        print("재생 시간 초과 - 강제 중단")
        voice_client.stop()


async def _tts_worker(guild: discord.Guild):
    """
    길드마다 하나만 도는 소비자. 큐가 비면 종료하고, 새 메시지가 오면 _submit()이 다시 띄움.
    """
    guild_id = guild.id
    queue = tts_queues[guild_id]

    try:
        while True:
            try:
                request = queue.get_nowait()

            except asyncio.QueueEmpty:
                return

            # 음성 채널에 없으면 굳이 음성을 만들지 않음
            if guild.voice_client is None:
                print("봇이 음성채널에 없음")
                continue

            try:
                setting = USER_TTS_SETTINGS.get(request.author_id, {})
                engine = setting.get("engine", "gtts")
                voice = setting.get("voice", "Kim")

                audio = await make_tts_audio(
                    request.text,
                    engine=engine,
                    voice=voice
                )

                # 음성 생성을 기다리는 사이에 봇이 나갔을 수 있으므로 재생 직전에 확인
                voice_client = guild.voice_client

                if voice_client is None or not voice_client.is_connected():
                    print("봇이 음성채널에 없음")
                    continue

                print(
                    f"TTS 재생: {request.author_name}: {request.text} "
                    f"[engine={engine}, voice={voice if engine == 'se' else '-'}]"
                )

                await _play(voice_client, audio)
                await asyncio.sleep(0.05)

            except Exception as e:
                print("TTS ERROR:", repr(e))

    finally:
        if tts_workers.get(guild_id) is asyncio.current_task():
            del tts_workers[guild_id]
