# =========================
# TTS 큐 처리
# =========================
import asyncio
import io
import os
import time
from dataclasses import dataclass, field

import discord

from tts.tts_audio import make_tts_audio
from tts.tts_settings import USER_TTS_SETTINGS
from tts.tts_text import clean_tts_text

MAX_QUEUE_SIZE = 15
FFMPEG_PATH = os.getenv("FFMPEG_PATH", "ffmpeg")

# after 콜백이 끝내 안 불리는 경우에도 큐가 영구히 멈추지 않도록
MAX_PLAYBACK_SECONDS = 60

# 이 값보다 오래 걸린 단계가 있으면 어디서 늦어졌는지 자세히 로그로 남김
SLOW_STAGE_THRESHOLD_SECONDS = 1.0

tts_queues = {}   # guild_id -> asyncio.Queue
tts_workers = {}  # guild_id -> asyncio.Task
tts_pending = {}  # guild_id -> (TTSRequest, asyncio.Task) - 재생 중에 미리 생성해 둔 다음 메시지


@dataclass
class TTSRequest:
    author_id: int
    author_name: str
    text: str
    received_at: float = field(default_factory=time.monotonic)
    queue_size_at_submit: int = 0


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

    request = TTSRequest(author_id, author_name, text)

    try:
        queue.put_nowait(request)
        request.queue_size_at_submit = queue.qsize()

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
    gen_start/gen_end는 이 메시지 하나의 생성이 얼마나 걸렸는지 진단하기 위한 타임스탬프.
    """
    setting = USER_TTS_SETTINGS.get(request.author_id, {})
    engine = setting.get("engine", "gtts")
    voice = setting.get("voice", "Kim")

    gen_start = time.monotonic()

    for attempt in range(2):
        attempt_start = time.monotonic()

        try:
            audio = await make_tts_audio(request.text, engine=engine, voice=voice)
            gen_end = time.monotonic()

            attempt_elapsed = gen_end - attempt_start
            if attempt_elapsed > SLOW_STAGE_THRESHOLD_SECONDS:
                print(
                    f"[SLOW TTS] gTTS 생성 지연: {attempt_elapsed:.2f}s "
                    f"(attempt={attempt + 1}, engine={engine}) text={request.text!r}"
                )

            return audio, engine, voice, gen_start, gen_end

        except Exception as e:
            attempt_elapsed = time.monotonic() - attempt_start
            print(
                f"[SLOW TTS] gTTS 생성 실패 ({attempt_elapsed:.2f}s 소요): {e!r} "
                f"text={request.text!r}"
            )

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
    spawn_start = time.monotonic()

    source = await asyncio.to_thread(
        discord.FFmpegPCMAudio,
        io.BytesIO(audio),
        pipe=True,
        executable=FFMPEG_PATH
    )

    spawn_elapsed = time.monotonic() - spawn_start
    if spawn_elapsed > SLOW_STAGE_THRESHOLD_SECONDS:
        print(f"[SLOW TTS] ffmpeg 프로세스 생성 지연: {spawn_elapsed:.2f}s")

    loop = asyncio.get_running_loop()
    done = asyncio.Event()

    def after_playing(error):
        if error:
            print("재생 오류:", error)

        loop.call_soon_threadsafe(done.set)

    playback_started = time.monotonic()
    voice_client.play(source, after=after_playing)

    try:
        await asyncio.wait_for(done.wait(), timeout=MAX_PLAYBACK_SECONDS)

    except asyncio.TimeoutError:
        print("재생 시간 초과 - 강제 중단")
        voice_client.stop()

    playback_finished = time.monotonic()
    return playback_started, playback_finished


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

            queued_wait = time.monotonic() - request.received_at
            if queued_wait > SLOW_STAGE_THRESHOLD_SECONDS:
                print(
                    f"[SLOW TTS] 큐 대기 지연: {queued_wait:.2f}s "
                    f"(제출 당시 큐 크기={request.queue_size_at_submit}) text={request.text!r}"
                )

            try:
                result = await gen_task

                if result is None:
                    continue

                audio, engine, voice, gen_start, gen_end = result

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

                playback_started, playback_finished = await _play(voice_client, audio)
                await asyncio.sleep(0.05)

                total_elapsed = playback_finished - request.received_at
                if total_elapsed > SLOW_STAGE_THRESHOLD_SECONDS:
                    print(
                        f"[SLOW TTS] 전체 지연 {total_elapsed:.2f}s 요약 text={request.text!r} | "
                        f"큐대기={gen_start - request.received_at:.2f}s "
                        f"생성={gen_end - gen_start:.2f}s "
                        f"생성후대기={playback_started - gen_end:.2f}s "
                        f"재생={playback_finished - playback_started:.2f}s | "
                        f"현재 큐 크기={queue.qsize()}"
                    )

            except Exception as e:
                print(f"TTS ERROR: {e!r} text={request.text!r}")

    finally:
        pending = tts_pending.pop(guild_id, None)

        if pending is not None:
            _, gen_task = pending
            gen_task.cancel()

        if tts_workers.get(guild_id) is asyncio.current_task():
            del tts_workers[guild_id]
