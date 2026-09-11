import os
from dataclasses import dataclass
from urllib.parse import quote

from http_session import get_session


# Riot ID -> PUUID 조회는 지역 라우팅(대륙) 값을 씀
ACCOUNT_ROUTE = "asia"

# 플랫폼(국가별 서버) 라우팅 값. 클래시/챔피언 숙련도/리그 API가 공통으로 씀.
PLATFORM_ROUTE = "kr"
CLASH_ROUTE = PLATFORM_ROUTE

# Match-V5도 ACCOUNT-V1과 같은 대륙 라우팅(지역) 값을 씀
MATCH_ROUTE = "asia"

ACCOUNT_BASE_URL = f"https://{ACCOUNT_ROUTE}.api.riotgames.com/riot/account/v1"
CLASH_BASE_URL = f"https://{CLASH_ROUTE}.api.riotgames.com/lol/clash/v1"
MATCH_BASE_URL = f"https://{MATCH_ROUTE}.api.riotgames.com/lol/match/v5"
MASTERY_BASE_URL = f"https://{PLATFORM_ROUTE}.api.riotgames.com/lol/champion-mastery/v4"
LEAGUE_BASE_URL = f"https://{PLATFORM_ROUTE}.api.riotgames.com/lol/league/v4"

# League-V4 entries/by-puuid가 반환하는 queueType 문자열 (큐 ID와는 별개 네임스페이스).
RANKED_SOLO_QUEUE_TYPE = "RANKED_SOLO_5x5"
RANKED_FLEX_QUEUE_TYPE = "RANKED_FLEX_SR"

# 큐 ID (공식 목록: https://static.developer.riotgames.com/docs/lol/queues.json)
#
# 블라인드 픽(과거 430)은 12.9 패치에서 소환사의 협곡에서 사라졌고 그 자리를
# 퀵플레이(490)가 대체함 - 지금 시점 기준 "일반 게임"은 드래프트(400)와
# 퀵플레이(490) 둘 뿐이라 430은 의도적으로 넣지 않음.
RANKED_SOLO_QUEUE_ID = 420
NORMAL_DRAFT_QUEUE_ID = 400
NORMAL_QUICKPLAY_QUEUE_ID = 490

# 랭크 자유. 스카우팅 수집 대상에서 명시적으로 제외함(ALLOWED_SCOUTING_QUEUE_IDS에 없음).
RANKED_FLEX_QUEUE_ID = 440

# 소환사의 협곡 클래시. Riot 공식 큐 목록(https://static.developer.riotgames.com/docs/lol/queues.json)에서
# "Summoner's Rift Clash games" 항목을 직접 확인한 값 = 700 (ARAM 클래시는 별도 720이며 대상 아님).
# 클래시는 실제 대회 드래프트에서 뭘 고르는지에 대한 그라운드 트루스라서 수집은 하되,
# feature/training 쪽 풀 계산(예: analyze_thickness.py의 --queues)에는 기본적으로 섞이지 않게
# ALLOWED_SCOUTING_QUEUE_IDS에는 넣어서 collect_matches.py로 수집은 가능하게 하고,
# analyze_thickness.py 쪽에서 --queues 인자로는 별도로 거부함(RANKED_FLEX_QUEUE_ID와 같은 방식).
CLASH_QUEUE_ID = 700

# scouting.collect_matches --queues / scouting.analyze_thickness --queues 가 허용하는 큐 목록.
# 아레나(1700)나 이벤트성 큐(URF 등)가 실수로/API 오동작으로 섞여 들어오는 걸
# 막기 위한 allow-list 역할도 함 - 여기 없는 큐 ID는 애초에 요청하지 않음.
ALLOWED_SCOUTING_QUEUE_IDS: dict[int, str] = {
    RANKED_SOLO_QUEUE_ID: "RANKED_SOLO",
    NORMAL_DRAFT_QUEUE_ID: "NORMAL_DRAFT",
    NORMAL_QUICKPLAY_QUEUE_ID: "NORMAL_QUICKPLAY",
    CLASH_QUEUE_ID: "CLASH",
}

# Match-V5의 by-puuid/ids 엔드포인트가 한 번에 허용하는 최대 count.
# Riot 공식 문서(Match-V5 by-puuid/ids) 기준 실측 상한이며, 이 프로세스에는
# 유효한 RIOT_API_KEY가 없어서 라이브 호출로 재확인하지는 못함.
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


@dataclass
class ChampionMastery:
    champion_id: int
    mastery_points: int
    mastery_level: int
    last_play_time: int | None


@dataclass
class LeagueEntry:
    queue_type: str
    tier: str
    division: str
    lp: int
    wins: int
    losses: int


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

async def get_match_ids_by_queue(puuid: str, queue_id: int, count: int = 200) -> list[str]:
    """
    최신 순으로 최대 count개의 매치 ID를 가져옴 (큐 하나 기준).
    Riot API가 by-puuid/ids 한 번 호출당 허용하는 count 상한(MATCH_IDS_PAGE_SIZE)이
    있어서, count가 그보다 크면 start를 옮겨가며 여러 번 나눠서 요청함.
    """
    match_ids: list[str] = []
    start = 0

    while len(match_ids) < count:
        remaining = count - len(match_ids)
        page_size = min(MATCH_IDS_PAGE_SIZE, remaining)

        url = (
            f"{MATCH_BASE_URL}/matches/by-puuid/{puuid}/ids"
            f"?start={start}&count={page_size}&queue={queue_id}"
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


# =========================
# Champion-Mastery-V4 / League-V4 (스냅샷 수집용)
# =========================

async def get_champion_masteries(puuid: str) -> list[ChampionMastery]:
    """
    해당 puuid의 전체 챔피언 숙련도를 반환함. 기록이 하나도 없으면 빈 리스트.
    """
    url = f"{MASTERY_BASE_URL}/champion-masteries/by-puuid/{puuid}"
    data = await _request_json(url)

    if not data:
        return []

    return [
        ChampionMastery(
            champion_id=entry["championId"],
            mastery_points=entry["championPoints"],
            mastery_level=entry["championLevel"],
            last_play_time=entry.get("lastPlayTime"),
        )
        for entry in data
    ]


async def get_league_entries(puuid: str) -> list[LeagueEntry]:
    """
    해당 puuid의 전체 리그 항목(솔로랭크/자유랭크 등)을 반환함.
    언랭이면 항목이 없어서 빈 리스트가 올 수 있음.
    """
    url = f"{LEAGUE_BASE_URL}/entries/by-puuid/{puuid}"
    data = await _request_json(url)

    if not data:
        return []

    return [
        LeagueEntry(
            queue_type=entry["queueType"],
            tier=entry["tier"],
            division=entry["rank"],
            lp=entry["leaguePoints"],
            wins=entry["wins"],
            losses=entry["losses"],
        )
        for entry in data
    ]
