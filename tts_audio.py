# =========================
# TTS 오디오 생성
# =========================
import asyncio
import io
import urllib.parse

from gtts import gTTS

import gtts_session
from http_session import get_session

gtts_session.install()

# gTTS의 timeout 기본값은 None(무제한)이라, 구글 쪽 응답이 멈추면 이 요청을 처리하는
# 스레드가 영원히 안 풀림 -> 같은 길드의 TTS 워커 전체가 그 메시지 하나 때문에 멈춤.
# 소켓 레벨 타임아웃을 걸어서 아무리 길어도 이 시간 안에는 예외로 풀리게 함.
GTTS_REQUEST_TIMEOUT_SECONDS = 5.0


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
        gTTS(text=text, lang="ko", timeout=GTTS_REQUEST_TIMEOUT_SECONDS).write_to_fp(buffer)
        return buffer.getvalue()

    return await asyncio.to_thread(render)
