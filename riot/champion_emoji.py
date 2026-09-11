"""
챔피언 스퀘어 아이콘을 Discord 애플리케이션 이모지로 등록/조회.

길드별 이모지가 아니라 봇 애플리케이션 소유 이모지(discord.py 2.5+의
create_application_emoji/fetch_application_emojis)를 씀 - 봇이 들어간 어떤
서버에서도 길드 이모지 슬롯을 소모하지 않고 바로 쓸 수 있어서, 리포트
텍스트 안에 <:champ_103:...> 형태로 인라인 아이콘을 넣을 수 있음.

이미지 자체는 Data Dragon(champion_data.py가 이미 갱신해 둔 캐시)에서
받아오고, 한 번 이모지로 올리면 그 뒤로는 계속 유효하므로(패치가 바뀌어도
이미 올라간 이미지는 안 바뀜) data/discord_emojis.json에 champion_id ->
{name, id}만 캐시해 둠. 새 챔피언이 나오면 sync_champion_emojis()를 다시
돌려야 새로 추가됨 - champion_data.py처럼 매 호출마다 자동으로 확인하지
않음.

emoji_markup()은 디스크 캐시만 읽고 절대 네트워크 요청이나 Discord API
호출을 하지 않음 - 리포트 렌더링 경로에서 안전하게 동기적으로 쓸 수 있음.
"""

import asyncio
import json
import logging
from pathlib import Path

import discord

from riot import champion_data
from http_session import get_session

logger = logging.getLogger(__name__)

# Repo root's shared data/ dir, not this package's own folder.
CACHE_PATH = Path(__file__).resolve().parent.parent / "data" / "discord_emojis.json"
EMOJI_NAME_PREFIX = "champ_"
CHAMPION_IMAGE_URL_TEMPLATE = "https://ddragon.leagueoflegends.com/cdn/{version}/img/champion/{image_key}.png"
# 업로드 사이 대기(초). Discord 이모지 생성에도 레이트 리밋이 있어서, 첫
# 동기화 때 수백 개를 한꺼번에 몰아 보내지 않기 위한 보수적 값 - discord.py
# 자체도 429를 재시도하지만, 애초에 덜 유발하는 게 안전함.
UPLOAD_DELAY_SECONDS = 1.0


class ChampionEmojiError(Exception):
    """챔피언 이모지 관련 오류의 기본 클래스"""


class ChampionEmojiNotLoadedError(ChampionEmojiError):
    """캐시가 아직 없어서 emoji_markup을 쓸 수 없는 상태"""


# champion_id -> {"name": str, "id": int}. 프로세스 내 인메모리 캐시.
_cache: dict[int, dict] | None = None


def _emoji_name(champion_id: int) -> str:
    return f"{EMOJI_NAME_PREFIX}{champion_id}"


def _load_from_disk() -> bool:
    global _cache

    if not CACHE_PATH.exists():
        return False

    try:
        raw = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False

    _cache = {int(champion_id): entry for champion_id, entry in raw.items()}
    return True


def _save_to_disk() -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(
        json.dumps({str(champion_id): entry for champion_id, entry in _cache.items()}, ensure_ascii=False),
        encoding="utf-8",
    )


def _ensure_loaded() -> None:
    if _cache is not None:
        return

    if not _load_from_disk():
        raise ChampionEmojiNotLoadedError(
            "챔피언 이모지 캐시가 없음. champion_emoji.sync_champion_emojis(bot)를 먼저 실행해줘."
        )


def emoji_markup(champion_id: int) -> str | None:
    """
    "<:champ_103:1234567890>" 형태의 인라인 이모지 마크업을 반환함. 캐시가
    없거나 그 챔피언이 아직 업로드되지 않았으면 None을 반환함(호출 쪽이
    아이콘 없이 텍스트만 보여주게 함 - 캐시 상태 때문에 리포트 자체가
    실패하면 안 됨).
    """
    _ensure_loaded()
    entry = _cache.get(champion_id)

    if entry is None:
        return None

    return f"<:{entry['name']}:{entry['id']}>"


async def _fetch_champion_image(version: str, image_key: str) -> bytes:
    session = get_session()
    url = CHAMPION_IMAGE_URL_TEMPLATE.format(version=version, image_key=image_key)

    async with session.get(url) as response:
        response.raise_for_status()
        return await response.read()


async def sync_champion_emojis(bot: discord.Client, force: bool = False) -> dict:
    """
    champion_data 캐시(먼저 refresh_champion_data()로 준비돼 있어야 함)에 있는
    챔피언마다 애플리케이션 이모지가 있는지 확인하고, 없으면 Data Dragon
    스퀘어 아이콘을 받아서 새로 등록함.

    이미 로컬 캐시에 있는 챔피언은 다시 올리지 않고, 로컬 캐시에 없어도
    Discord에 이미 등록돼 있으면(fetch_application_emojis로 확인, 이름
    규칙 champ_<id>로 매칭) 재사용함 - 로컬 캐시 파일이 사라져도 중복
    업로드하지 않기 위함. force=True면 전부 다시 올림.

    반환값은 {"created": n, "reused": n, "total": n} 요약.
    """
    global _cache

    version = champion_data.current_version()  # 캐시 없으면 ChampionDataNotLoadedError
    existing_by_name = {emoji.name: emoji for emoji in await bot.fetch_application_emojis()}

    if _cache is None:
        _load_from_disk()
    if _cache is None or force:
        _cache = {}

    created = reused = 0

    for champion_id in champion_data.known_champion_ids():
        if not force and champion_id in _cache:
            reused += 1
            continue

        name = _emoji_name(champion_id)
        remote = existing_by_name.get(name)

        if remote is not None and not force:
            _cache[champion_id] = {"name": remote.name, "id": remote.id}
            reused += 1
            continue

        image_key = champion_data.champion_image_key(champion_id)

        if image_key is None:
            continue

        try:
            image_bytes = await _fetch_champion_image(version, image_key)
            emoji = await bot.create_application_emoji(name=name, image=image_bytes)
        except discord.HTTPException as e:
            logger.warning(
                "챔피언 이모지 업로드 실패 (champion_id=%s, image_key=%s): %r",
                champion_id, image_key, e,
            )
            continue

        _cache[champion_id] = {"name": emoji.name, "id": emoji.id}
        created += 1
        await asyncio.sleep(UPLOAD_DELAY_SECONDS)

    _save_to_disk()

    return {"created": created, "reused": reused, "total": len(_cache)}
