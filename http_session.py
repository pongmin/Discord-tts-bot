import aiohttp

# 요청 하나가 멈춰도 큐/명령이 영구히 막히지 않도록 상한을 둠
DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=15)

_session: aiohttp.ClientSession | None = None


def get_session() -> aiohttp.ClientSession:
    """
    프로세스 전체에서 공유하는 aiohttp 세션.
    요청마다 세션을 새로 만들면 매번 TCP/TLS 핸드셰이크를 다시 함.
    """
    global _session

    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(timeout=DEFAULT_TIMEOUT)

    return _session


async def close_session() -> None:
    global _session

    if _session is not None and not _session.closed:
        await _session.close()

    _session = None
