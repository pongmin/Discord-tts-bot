import asyncio
import json
import random
import time
from pathlib import Path

from http_session import get_session


LANE_FILE = Path(__file__).resolve().parent / "lane_champions.json"

LANE_DISPLAY = {
    "top": "탑",
    "jungle": "정글",
    "mid": "미드",
    "adc": "원딜",
    "support": "서폿"
}

DAMAGE_DISPLAY = {
    "ad": "AD",
    "ap": "AP",
    "tank": "탱커"
}

_lane_champions = None


def load_lane_champions() -> dict:
    """
    챔피언 목록은 자주 안 바뀌므로 한 번만 읽어서 재사용.
    """
    global _lane_champions

    if _lane_champions is None:
        if not LANE_FILE.exists():
            raise FileNotFoundError("lane_champions.json 파일을 찾을 수 없습니다.")

        with open(LANE_FILE, "r", encoding="utf-8") as f:
            _lane_champions = json.load(f)

    return _lane_champions


def pick_random_champion(lane: str, damage_type: str | None = None) -> tuple[str, str]:
    lane_champions = load_lane_champions()

    if lane not in lane_champions:
        raise ValueError("지원하지 않는 라인입니다.")

    lane_pool = lane_champions[lane]

    if damage_type is not None:
        if damage_type not in lane_pool:
            raise ValueError("지원하지 않는 타입입니다.")

        champions = lane_pool[damage_type]

        if not champions:
            raise ValueError("해당 조건에 등록된 챔피언이 없습니다.")

        return random.choice(champions), damage_type

    combined_pool = [
        (champion, dtype)
        for dtype, champions in lane_pool.items()
        for champion in champions
    ]

    if not combined_pool:
        raise ValueError("해당 라인에 등록된 챔피언이 없습니다.")

    return random.choice(combined_pool)


# =========================
# Data Dragon 캐시
# =========================

VERSIONS_URL = "https://ddragon.leagueoflegends.com/api/versions.json"

# 패치는 2주에 한 번 정도라 자주 다시 받을 이유가 없음
_DDRAGON_TTL = 60 * 60 * 6

_ddragon_lock = asyncio.Lock()
_ddragon_fetched_at = 0.0
_ddragon_version: str | None = None
_ddragon_images: dict[str, str] = {}


async def _fetch_ddragon() -> None:
    global _ddragon_fetched_at, _ddragon_version, _ddragon_images

    session = get_session()

    async with session.get(VERSIONS_URL) as response:
        response.raise_for_status()
        versions = await response.json()

    version = versions[0]

    url = f"https://ddragon.leagueoflegends.com/cdn/{version}/data/ko_KR/champion.json"

    async with session.get(url) as response:
        response.raise_for_status()
        data = await response.json()

    # 매번 전체를 훑지 않도록 한글 이름 -> 이미지 URL로 미리 만들어 둠
    _ddragon_images = {
        champion["name"]: (
            "https://ddragon.leagueoflegends.com/cdn/"
            f"{version}/img/champion/{champion['image']['full']}"
        )
        for champion in data["data"].values()
    }

    _ddragon_version = version
    _ddragon_fetched_at = time.monotonic()


def _cache_is_fresh() -> bool:
    return (
        _ddragon_version is not None
        and time.monotonic() - _ddragon_fetched_at < _DDRAGON_TTL
    )


async def _ensure_ddragon() -> None:
    async with _ddragon_lock:
        # 락을 기다리는 사이에 다른 요청이 이미 채웠을 수 있음
        if _cache_is_fresh():
            return

        await _fetch_ddragon()


async def get_champion_image_url(champion_name_ko: str) -> str | None:
    if not _cache_is_fresh():
        await _ensure_ddragon()

    return _ddragon_images.get(champion_name_ko)
