# =========================
# TTS 큐 처리
# =========================
import asyncio
import io
import os
from dataclasses import dataclass

import discord

from tts_audio import make_tts_audio
from tts_settings import USER_TTS_SETTINGS
from tts_text import clean_tts_text

MAX_QUEUE_SIZE = 15
FFMPEG_PATH = os.getenv("FFMPEG_PATH", "ffmpeg")

# after 콜백이 끝내 안 불리는 경우에도 큐가 영구히 멈추지 않도록
MAX_PLAYBACK_SECONDS = 60

tts_queues = {}   # guild_id -> asyncio.Queue
tts_workers = {}  # guild_id -> asyncio.Task
tts_pending = {}  # guild_id -> (TTSRequest, asyncio.Task) - 재생 중에 미리 생성해 둔 다음 메시지


@dataclass
class TTSRequest:
    author_id: int
    author_name: str
    text: str


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
        print(f"[{guild.id}] 큐가 가득 참 - 메시지 드랍: {text!r}")
        return

    worker = tts_workers.get(guild.id)

    if worker is None or worker.done():
        tts_workers[guild.id] = asyncio.create_task(_tts_worker(guild))


def clear_guild_queue(guild_id: int) -> int:
    """
    대기 중인 메시지와, 재생 중에 미리 생성해 두던 다음 메시지까지 전부 비움.
    미리 생성 중이던 항목은 아직 재생 전이므로 여기서 취소해야 실수로 재생되지 않음.
    """
    cleared = 0
    queue = tts_queues.get(guild_id)

    while queue is not None and not queue.empty():
        queue.get_nowait()
        cleared += 1

    pending = tts_pending.pop(guild_id, None)

    if pending is not None:
        _, gen_task = pending
        gen_task.cancel()
        cleared += 1

    return cleared


async def _generate_audio(request: TTSRequest):
    """
    유저 TTS 설정에 따라 오디오를 생성. gTTS가 가끔 일시적인 네트워크 오류를 던지므로 한 번 더 시도.
    끝내 실패하면 None을 돌려주고, 호출부는 이 메시지를 건너뛰고 다음 메시지로 진행함.
    """
    setting = USER_TTS_SETTINGS.get(request.author_id, {})
    engine = setting.get("engine", "gtts")
    voice = setting.get("voice", "Kim")

    for attempt in range(2):
        try:
            audio = await make_tts_audio(request.text, engine=engine, voice=voice)
            return audio, engine, voice

        except Exception as e:
            if attempt == 0:
                print(f"TTS 생성 실패, 재시도: {e!r} text={request.text!r}")
                await asyncio.sleep(0.5)
                continue

            print(f"TTS ERROR: {e!r} text={request.text!r}")
            return None


async def _play(voice_client: discord.VoiceClient, audio: bytes):
    # FFmpegPCMAudio.__init__이 subprocess.Popen을 동기로 호출하므로,
    # 그대로 두면 프로세스 생성이 느려질 때(백신 스캔 등) 이벤트 루프 전체가 멈춰서
    # 그동안 들어온 메시지가 한꺼번에 밀렸다가 재생되는 현상이 생김
    source = await asyncio.to_thread(
        discord.FFmpegPCMAudio,
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
    현재 메시지를 재생하는 동안 다음 메시지 하나를 미리 생성해 둬서(prefetch) 재생 사이 공백을 줄임.
    """
    guild_id = guild.id
    queue = tts_queues[guild_id]

    try:
        while True:
            prefetched = tts_pending.pop(guild_id, None)

            if prefetched is not None:
                request, gen_task = prefetched
            else:
                try:
                    request = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return

                gen_task = asyncio.create_task(_generate_audio(request))

            # 음성 채널에 없으면 굳이 음성을 기다리지 않음
            if guild.voice_client is None:
                print(f"봇이 음성채널에 없음 - 메시지 드랍: {request.text!r}")
                gen_task.cancel()
                continue

            try:
                result = await gen_task

                if result is None:
                    continue

                audio, engine, voice = result

                # 음성 생성을 기다리는 사이에 봇이 나갔을 수 있으므로 재생 직전에 확인
                voice_client = guild.voice_client

                if voice_client is None or not voice_client.is_connected():
                    print(f"봇이 음성채널에 없음 - 메시지 드랍: {request.text!r}")
                    continue

                # 지금 재생하는 동안 다음 메시지 하나만 미리 생성 (한 개만 미리 당김)
                try:
                    next_request = queue.get_nowait()
                    tts_pending[guild_id] = (
                        next_request,
                        asyncio.create_task(_generate_audio(next_request))
                    )
                except asyncio.QueueEmpty:
                    pass

                print(
                    f"TTS 재생: {request.author_name}: {request.text} "
                    f"[engine={engine}, voice={voice if engine == 'se' else '-'}]"
                )

                await _play(voice_client, audio)
                await asyncio.sleep(0.05)

            except Exception as e:
                print(f"TTS ERROR: {e!r} text={request.text!r}")

    finally:
        pending = tts_pending.pop(guild_id, None)

        if pending is not None:
            _, gen_task = pending
            gen_task.cancel()

        if tts_workers.get(guild_id) is asyncio.current_task():
            del tts_workers[guild_id]
