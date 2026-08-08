# =========================
# gTTS용 공유 HTTP 세션
# =========================
"""
gTTS(write_to_fp -> stream())는 호출할 때마다 requests.Session()을 새로 만들어서
매번 TCP/TLS 핸드셰이크를 다시 함 (메시지당 약 150ms 손해).

site-packages의 gtts 패키지는 세션을 주입할 방법을 안 열어주므로 직접 수정하지 않고,
gtts.tts 모듈이 내부적으로 참조하는 requests.Session만 우리가 만든 프록시로
바꿔치기해서 항상 같은 공유 세션(=커넥션 풀)을 재사용하게 함.
전역 requests 모듈이 아니라 gtts.tts 모듈 안의 참조만 바꾸므로 프로젝트의
다른 requests 사용처(예: 다른 라이브러리)에는 영향이 없음.
"""
import asyncio

import gtts.tts as _gtts_tts
import requests

_session: requests.Session | None = None


class _NonClosingSessionHandle:
    """
    gtts.tts.stream()은 `with requests.Session() as s:`로 세션을 쓰는데,
    블록이 끝나면 s.close()가 호출되어 커넥션 풀이 매번 닫혀버림.
    __exit__에서 실제로 닫지 않도록 감싸서 커넥션이 메시지 사이에도 살아있게 함.
    """
    __slots__ = ("_session",)

    def __init__(self, session: requests.Session):
        self._session = session

    def __enter__(self) -> requests.Session:
        return self._session

    def __exit__(self, *exc_info) -> bool:
        return False

    def __getattr__(self, name):
        return getattr(self._session, name)


class _RequestsModuleProxy:
    """gtts.tts 모듈 안에서 requests.Session()만 가로채고, 나머지는 원래 requests로 위임."""

    def __init__(self, real_requests_module, session_factory):
        object.__setattr__(self, "_real", real_requests_module)
        object.__setattr__(self, "_session_factory", session_factory)

    def __getattr__(self, name):
        if name == "Session":
            return self._session_factory
        return getattr(self._real, name)


def install() -> None:
    """
    프로세스 시작 시 한 번 호출. gtts.tts의 requests 참조를 프록시로 바꿔서
    그 모듈이 만드는 Session이 항상 공유 세션을 가리키게 함.
    """
    global _session

    if isinstance(_gtts_tts.requests, _RequestsModuleProxy):
        return  # 이미 적용됨

    _session = requests.Session()

    def _session_factory(*args, **kwargs) -> _NonClosingSessionHandle:
        return _NonClosingSessionHandle(_session)

    _gtts_tts.requests = _RequestsModuleProxy(_gtts_tts.requests, _session_factory)


async def close_gtts_session() -> None:
    global _session

    def _close():
        global _session
        if _session is not None:
            _session.close()
        _session = None

    await asyncio.to_thread(_close)
