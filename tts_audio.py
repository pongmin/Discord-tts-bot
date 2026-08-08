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
