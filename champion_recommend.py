import json
import random
from pathlib import Path

import aiohttp


LANE_FILE = Path("lane_champions.json")

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


def load_lane_champions() -> dict:
    if not LANE_FILE.exists():
        raise FileNotFoundError("lane_champions.json 파일을 찾을 수 없습니다.")

    with open(LANE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


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

    combined_pool = []

    for dtype, champions in lane_pool.items():
        for champion in champions:
            combined_pool.append((champion, dtype))

    if not combined_pool:
        raise ValueError("해당 라인에 등록된 챔피언이 없습니다.")

    return random.choice(combined_pool)


async def get_latest_lol_version() -> str:
    url = "https://ddragon.leagueoflegends.com/api/versions.json"

    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            response.raise_for_status()
            versions = await response.json()
            return versions[0]


async def get_champion_data_ko() -> dict:
    version = await get_latest_lol_version()
    url = f"https://ddragon.leagueoflegends.com/cdn/{version}/data/ko_KR/champion.json"

    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            response.raise_for_status()
            data = await response.json()
            return data["data"]


async def get_champion_image_url(champion_name_ko: str) -> str | None:
    version = await get_latest_lol_version()
    champion_data = await get_champion_data_ko()

    for champion_id, champion in champion_data.items():
        if champion["name"] == champion_name_ko:
            image_file = champion["image"]["full"]
            return f"https://ddragon.leagueoflegends.com/cdn/{version}/img/champion/{image_file}"

    return None