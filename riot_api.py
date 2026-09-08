import os
from dataclasses import dataclass
from urllib.parse import quote

from http_session import get_session


# Riot ID -> PUUID 조회는 지역 라우팅(대륙) 값을 씀
ACCOUNT_ROUTE = "asia"

# 클래시는 플랫폼(국가별 서버) 라우팅 값을 씀
CLASH_ROUTE = "kr"

# Match-V5도 ACCOUNT-V1과 같은 대륙 라우팅(지역) 값을 씀
MATCH_ROUTE = "asia"

ACCOUNT_BASE_URL = f"https://{ACCOUNT_ROUTE}.api.riotgames.com/riot/account/v1"
CLASH_BASE_URL = f"https://{CLASH_ROUTE}.api.riotgames.com/lol/clash/v1"
MATCH_BASE_URL = f"https://{MATCH_ROUTE}.api.riotgames.com/lol/match/v5"

# 큐 ID: 솔로/듀오 랭크만 수집 대상. 자유랭크(440)는 수집하지 않음
RANKED_SOLO_QUEUE_ID = 420

# Match-V5의 by-puuid/ids 엔드포인트가 한 번에 허용하는 최대 count
MATCH_IDS_PAGE_SIZE = 100


class RiotApiError(Exception):
    """Riot API 관련 오류의 기본 클래스"""


class InvalidRiotIdError(RiotApiError):
    pass


class PlayerNotFoundError(RiotApiError):
    pass


class NotInClashError(RiotApiError):
    pass


class MatchNotFoundError(RiotApiError):
    pass


class InvalidApiKeyError(RiotApiError):
    pass


class RateLimitedError(RiotApiError):
    status_code = 429

    def __init__(self, retry_after: int | None = None):
        self.retry_after = retry_after
        super().__init__("Riot API 요청 한도를 초과했습니다.")


class RiotServerError(RiotApiError):
    def __init__(self, message: str, status_code: int | None = None):
        self.status_code = status_code
        super().__init__(message)


@dataclass
class RiotAccount:
    puuid: str
    game_name: str
    tag_line: str


@dataclass
class ClashPlayer:
    puuid: str
    position: str
    role: str = "MEMBER"


def _get_api_key() -> str:
    api_key = os.getenv("RIOT_API_KEY")

    if not api_key:
        raise InvalidApiKeyError("RIOT_API_KEY 환경 변수가 설정되지 않았습니다.")

    return api_key


def parse_riot_id(riot_id: str) -> tuple[str, str]:
    riot_id = riot_id.strip()

    if "#" not in riot_id:
        raise InvalidRiotIdError("Riot ID 형식이 올바르지 않습니다. 예: 이름#태그")

    game_name, tag_line = riot_id.split("#", 1)
    game_name = game_name.strip()
    tag_line = tag_line.strip()

    if not game_name or not tag_line:
        raise InvalidRiotIdError("Riot ID 형식이 올바르지 않습니다. 예: 이름#태그")

    return game_name, tag_line


async def _request_json(url: str):
    session = get_session()
    headers = {"X-Riot-Token": _get_api_key()}

    async with session.get(url, headers=headers) as response:
        if response.status == 200:
            return await response.json()

        if response.status in (401, 403):
            raise InvalidApiKeyError("Riot API 키가 유효하지 않거나 만료되었습니다.")

        if response.status == 404:
            return None

        if response.status == 429:
            retry_after = response.headers.get("Retry-After")
            raise RateLimitedError(int(retry_after) if retry_after else None)

        if response.status >= 500:
            raise RiotServerError(
                f"Riot API 서버 오류가 발생했습니다. (HTTP {response.status})",
                status_code=response.status
            )

        raise RiotApiError(f"Riot API 요청이 실패했습니다. (HTTP {response.status})")


async def get_account_by_riot_id(game_name: str, tag_line: str) -> RiotAccount:
    url = f"{ACCOUNT_BASE_URL}/accounts/by-riot-id/{quote(game_name)}/{quote(tag_line)}"
    data = await _request_json(url)

    if data is None:
        raise PlayerNotFoundError(f"'{game_name}#{tag_line}' 계정을 찾을 수 없습니다.")

    return RiotAccount(puuid=data["puuid"], game_name=data["gameName"], tag_line=data["tagLine"])


async def get_account_by_puuid(puuid: str) -> RiotAccount:
    url = f"{ACCOUNT_BASE_URL}/accounts/by-puuid/{puuid}"
    data = await _request_json(url)

    if data is None:
        raise PlayerNotFoundError("PUUID에 해당하는 계정을 찾을 수 없습니다.")

    return RiotAccount(
        puuid=data["puuid"],
        game_name=data.get("gameName", "?"),
        tag_line=data.get("tagLine", "?")
    )


async def get_clash_team_id(puuid: str) -> str:
    url = f"{CLASH_BASE_URL}/players/by-puuid/{puuid}"
    data = await _request_json(url)

    if not data:
        raise NotInClashError("클래시에 등록되지 않은 플레이어입니다.")

    return data[0]["teamId"]


async def get_clash_team(team_id: str) -> list[ClashPlayer]:
    url = f"{CLASH_BASE_URL}/teams/{team_id}"
    data = await _request_json(url)

    if not data or not data.get("players"):
        raise NotInClashError("클래시 팀 정보를 찾을 수 없습니다.")

    return [
        ClashPlayer(
            puuid=player["puuid"],
            position=player.get("position") or "UNSELECTED",
            role=player.get("role") or "MEMBER"
        )
        for player in data["players"]
    ]


# =========================
# Match-V5 (솔로 랭크 전용)
# =========================

async def get_solo_queue_match_ids(puuid: str, count: int = 200) -> list[str]:
    """
    최신 순으로 최대 count개의 솔로 랭크(큐 420) 매치 ID를 가져옴.
    Riot API가 by-puuid/ids 한 번 호출당 허용하는 count 상한이 있어서
    MATCH_IDS_PAGE_SIZE 단위로 나눠서 요청함.
    """
    match_ids: list[str] = []
    start = 0

    while len(match_ids) < count:
        remaining = count - len(match_ids)
        page_size = min(MATCH_IDS_PAGE_SIZE, remaining)

        url = (
            f"{MATCH_BASE_URL}/matches/by-puuid/{puuid}/ids"
            f"?start={start}&count={page_size}&queue={RANKED_SOLO_QUEUE_ID}"
        )
        page = await _request_json(url)

        if not page:
            break

        match_ids.extend(page)

        # 요청한 것보다 적게 왔다는 건 그 플레이어의 기록이 거기서 끝났다는 뜻
        if len(page) < page_size:
            break

        start += page_size

    return match_ids


async def get_match_by_id(match_id: str) -> dict:
    """
    Match-V5 상세 응답을 가공 없이 그대로 반환함.
    필드를 버리지 않고 그대로 저장해야 나중에 다시 계산할 수 있기 때문.
    """
    url = f"{MATCH_BASE_URL}/matches/{match_id}"
    data = await _request_json(url)

    if data is None:
        raise MatchNotFoundError(f"매치를 찾을 수 없습니다: {match_id}")

    return data
