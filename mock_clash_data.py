"""
클래시 조회 기능 테스트용 가짜 데이터.

실제 Riot API(riot_api.py)는 전혀 호출하지 않고, "이름#MOCK" 형태의 태그라인이
들어왔을 때만 clash_commands.py에서 이 모듈로 라우팅된다.
실제 API와 같은 반환 타입(RiotAccount, ClashPlayer)을 쓰기 때문에,
이후 임베드를 만드는 로직은 실제 팀을 조회했을 때와 완전히 동일한 경로를 탄다.
"""

from riot_api import RiotAccount, ClashPlayer, PlayerNotFoundError


MOCK_TAG_LINE = "MOCK"

# puuid -> RiotAccount. get_account_by_puuid()가 하는 일을 흉내냄.
_MOCK_ACCOUNTS: dict[str, RiotAccount] = {}

# gameName(소문자) -> {"team_id": ..., "players": [ClashPlayer, ...]}
_MOCK_TEAMS: dict[str, dict] = {}


def _register_team(trigger_name: str, team_id: str, roster: list[tuple[str, str, str, str]]) -> None:
    """
    roster 항목: (puuid, position, role, riot_id)
    일부러 포지션 순서와 다르게(등록 순서 그대로) 넣어서,
    임베드를 만들 때 포지션 정렬이 실제로 동작하는지 확인할 수 있게 함.
    """
    players = []

    for puuid, position, role, riot_id in roster:
        game_name, tag_line = riot_id.split("#", 1)
        _MOCK_ACCOUNTS[puuid] = RiotAccount(puuid=puuid, game_name=game_name, tag_line=tag_line)
        players.append(ClashPlayer(puuid=puuid, position=position, role=role))

    _MOCK_TEAMS[trigger_name.lower()] = {"team_id": team_id, "players": players}


# test1#MOCK: 기본 케이스. 명단 순서가 포지션 순서와 다르고, 주장은 미드.
_register_team(
    "test1",
    team_id="mock-team-1",
    roster=[
        ("mock-1-util", "UTILITY", "MEMBER", "Sup Anchor#KR1"),
        ("mock-1-top", "TOP", "MEMBER", "Iron Wall#KR1"),
        ("mock-1-mid", "MIDDLE", "CAPTAIN", "Mid#KR1"),
        ("mock-1-bot", "BOTTOM", "MEMBER", "Hyper Carry Machine#KR1"),
        ("mock-1-jg", "JUNGLE", "MEMBER", "Gank King#KR1"),
    ]
)

# test2#MOCK: 아주 짧은 이름과 아주 긴 이름을 섞음(같은 KR 서버 안에서만). 주장은 정글.
_register_team(
    "test2",
    team_id="mock-team-2",
    roster=[
        ("mock-2-bot", "BOTTOM", "MEMBER", "A#KR1"),
        ("mock-2-top", "TOP", "MEMBER", "ThisIsAVeryLongSummonerName#KR1"),
        ("mock-2-util", "UTILITY", "MEMBER", "힐러정글러#KR2"),
        ("mock-2-jg", "JUNGLE", "CAPTAIN", "Jungle Overlord Supreme#KR1"),
        ("mock-2-mid", "MIDDLE", "MEMBER", "Mid#KR1"),
    ]
)

# test3#MOCK: 포지션 미지정(UNSELECTED/FILL) 케이스 포함. 주장은 탑.
_register_team(
    "test3",
    team_id="mock-team-3",
    roster=[
        ("mock-3-mid", "MIDDLE", "MEMBER", "Roaming Mage#KR1"),
        ("mock-3-top", "TOP", "CAPTAIN", "Split Pusher#KR1"),
        ("mock-3-jg", "FILL", "MEMBER", "Flex Player#KR1"),
        ("mock-3-bot", "BOTTOM", "MEMBER", "Crit Machine#KR1"),
        ("mock-3-util", "UNSELECTED", "MEMBER", "Enchanter#KR1"),
    ]
)

# test4#MOCK: 명단이 완전히 역순이고, 주장이 서포터인 케이스.
_register_team(
    "test4",
    team_id="mock-team-4",
    roster=[
        ("mock-4-util", "UTILITY", "CAPTAIN", "Shot Caller#KR1"),
        ("mock-4-bot", "BOTTOM", "MEMBER", "역스윕단상완#KR1"),
        ("mock-4-mid", "MIDDLE", "MEMBER", "Vlad Only#KR1"),
        ("mock-4-jg", "JUNGLE", "MEMBER", "Invade Everything#KR1"),
        ("mock-4-top", "TOP", "MEMBER", "탑솔장인#KR1"),
    ]
)


def is_mock_tag_line(tag_line: str) -> bool:
    return tag_line.strip().upper() == MOCK_TAG_LINE


async def mock_get_team_id(game_name: str) -> str:
    team = _MOCK_TEAMS.get(game_name.strip().lower())

    if team is None:
        raise PlayerNotFoundError(f"등록된 mock 팀이 없습니다: '{game_name}#{MOCK_TAG_LINE}'")

    return team["team_id"]


async def mock_get_team(team_id: str) -> list[ClashPlayer]:
    for team in _MOCK_TEAMS.values():
        if team["team_id"] == team_id:
            return team["players"]

    raise PlayerNotFoundError(f"mock 팀을 찾을 수 없습니다: {team_id}")


async def mock_get_account_by_puuid(puuid: str) -> RiotAccount:
    account = _MOCK_ACCOUNTS.get(puuid)

    if account is None:
        raise PlayerNotFoundError(f"mock 계정을 찾을 수 없습니다: {puuid}")

    return account
