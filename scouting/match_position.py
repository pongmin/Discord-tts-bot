"""
Match-V5 participant 데이터에서 라인/포지션을 정규화하는 순수 함수들.
DB에도, 분석 로직에도 같은 기준을 쓰기 위해 따로 뺌.
"""

VALID_POSITIONS = {"TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"}

UNKNOWN_POSITION = "UNKNOWN"


def _normalized(value: str | None) -> str:
    return (value or "").strip().upper()


def derive_canonical_role(participant: dict) -> str:
    """
    teamPosition(라이엇이 매치 후 계산한 값)을 최우선으로 쓰고,
    비어 있으면 individualPosition, 그마저 없으면 lane/role 조합으로 추정함.
    다섯 포지션 중 어디에도 못 맞추면 UNKNOWN.
    """
    team_position = _normalized(participant.get("teamPosition"))

    if team_position in VALID_POSITIONS:
        return team_position

    individual_position = _normalized(participant.get("individualPosition"))

    if individual_position in VALID_POSITIONS:
        return individual_position

    lane = _normalized(participant.get("lane"))
    role = _normalized(participant.get("role"))

    if lane == "TOP":
        return "TOP"

    if lane == "JUNGLE":
        return "JUNGLE"

    if lane in ("MIDDLE", "MID"):
        return "MIDDLE"

    if lane == "BOTTOM":
        if role == "DUO_SUPPORT":
            return "UTILITY"
        return "BOTTOM"

    return UNKNOWN_POSITION


def is_role_mismatch(participant: dict) -> bool:
    """
    라이엇이 매긴 teamPosition과 individualPosition이 둘 다 유효한 값인데 서로 다르면
    포지션이 어긋난 경기로 표시함(원딜/서폿이 바뀐 경우 등).
    """
    team_position = _normalized(participant.get("teamPosition"))
    individual_position = _normalized(participant.get("individualPosition"))

    if team_position not in VALID_POSITIONS or individual_position not in VALID_POSITIONS:
        return False

    return team_position != individual_position
