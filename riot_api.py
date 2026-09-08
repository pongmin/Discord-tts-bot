import os
from dataclasses import dataclass
from urllib.parse import quote

from http_session import get_session


# Riot ID -> PUUID 조회는 지역 라우팅(대륙) 값을 씀
ACCOUNT_ROUTE = "asia"

# 클래시는 플랫폼(국가별 서버) 라우팅 값을 씀
CLASH_ROUTE = "kr"

ACCOUNT_BASE_URL = f"https://{ACCOUNT_ROUTE}.api.riotgames.com/riot/account/v1"
CLASH_BASE_URL = f"https://{CLASH_ROUTE}.api.riotgames.com/lol/clash/v1"


class RiotApiError(Exception):
    """Riot API 관련 오류의 기본 클래스"""


class InvalidRiotIdError(RiotApiError):
    pass


class PlayerNotFoundError(RiotApiError):
    pass


class NotInClashError(RiotApiError):
    pass


class InvalidApiKeyError(RiotApiError):
    pass


class RateLimitedError(RiotApiError):
    def __init__(self, retry_after: int | None = None):
        self.retry_after = retry_after
        super().__init__("Riot API 요청 한도를 초과했습니다.")


class RiotServerError(RiotApiError):
    pass


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
            raise RiotServerError(f"Riot API 서버 오류가 발생했습니다. (HTTP {response.status})")

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
