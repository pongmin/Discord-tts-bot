"""
Data Dragon 챔피언 ID <-> 이름 매핑.

Riot API(riot_api.py)와는 별개의 정적 데이터 소스임 - API 키가 필요 없고
레이트 리밋도 없어서, Riot API 클라이언트나 그 안의 429 처리 로직을 거치지
않고 http_session만 공유해서 직접 호출함.

data/ddragon/에 버전+로케일별 champion.json을 캐시하고, meta.json에
"지금 쓰는 버전/로케일"을 기록함. 챔피언 ID는 패치가 바뀌어도 안 바뀌지만
이름/키는 바뀔 수 있으므로(개명 등) 캐시를 항상 버전과 함께 저장/로드함.
기본 로케일은 ko_KR(디스코드 봇이 한글로 챔피언 이름을 보여주기 위함)이고,
캐시 파일명에 로케일을 포함해서 다른 로케일로 미리 받아둔 캐시를 실수로
재사용하지 않게 함.

champion_name()/champion_id()는 디스크 캐시만 읽고 절대 네트워크 요청을
하지 않음. 새 패치 반영은 refresh_champion_data()를 명시적으로 호출해야만
일어남 - 매 호출마다 최신 버전인지 자동으로 확인하지 않음.
"""

import argparse
import asyncio
import json
from pathlib import Path

from http_session import get_session

# Repo root's shared data/ dir (also used by scouting_db, raw_match_store,
# champion_emoji, ban_report_assets), not this package's own folder.
DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "ddragon"
META_PATH = DATA_DIR / "meta.json"

VERSIONS_URL = "https://ddragon.leagueoflegends.com/api/versions.json"
CHAMPION_URL_TEMPLATE = "https://ddragon.leagueoflegends.com/cdn/{version}/data/{locale}/champion.json"

DEFAULT_LOCALE = "ko_KR"


class ChampionDataError(Exception):
    """Data Dragon 관련 오류의 기본 클래스"""


class ChampionDataNotLoadedError(ChampionDataError):
    """캐시가 아직 없어서 champion_name/champion_id를 쓸 수 없는 상태"""


# 프로세스 내 인메모리 캐시. 디스크 캐시가 있어도 매 호출마다 다시 읽지 않기 위함.
_id_to_name: dict[int, str] | None = None
_name_to_id: dict[str, int] | None = None
_id_to_image_key: dict[int, str] | None = None
_loaded_version: str | None = None
_loaded_locale: str | None = None


def _champion_file_path(version: str, locale: str) -> Path:
    return DATA_DIR / f"champion_{version}_{locale}.json"


def _build_maps(champion_data: dict) -> tuple[dict[int, str], dict[str, int], dict[int, str]]:
    id_to_name: dict[int, str] = {}
    name_to_id: dict[str, int] = {}
    id_to_image_key: dict[int, str] = {}

    for entry in champion_data["data"].values():
        champion_id_value = int(entry["key"])
        name = entry["name"]

        id_to_name[champion_id_value] = name
        name_to_id[name] = champion_id_value
        # entry["id"] is Data Dragon's own (always-English) sprite/image key,
        # e.g. "MonkeyKing" - independent of the champion.json locale, so it's
        # safe to read from whichever locale we happened to fetch.
        id_to_image_key[champion_id_value] = entry["id"]

    return id_to_name, name_to_id, id_to_image_key


def _load_meta() -> dict | None:
    if not META_PATH.exists():
        return None

    return json.loads(META_PATH.read_text(encoding="utf-8"))


def _save_meta(version: str, locale: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    META_PATH.write_text(json.dumps({"version": version, "locale": locale}), encoding="utf-8")


def _load_from_disk() -> bool:
    """
    디스크 캐시(meta.json + champion_<version>_<locale>.json)가 온전히 있으면
    인메모리 맵을 채우고 True를 반환함. 없거나 일부만 있으면 False (네트워크
    요청은 하지 않음). meta.json에 로케일이 없는 옛 캐시(로케일별 파일명을
    쓰기 전에 받아둔 것)는 더 이상 신뢰하지 않고 새로 받으라고 False를 반환함.
    """
    global _id_to_name, _name_to_id, _id_to_image_key, _loaded_version, _loaded_locale

    meta = _load_meta()

    if meta is None:
        return False

    version = meta.get("version")
    locale = meta.get("locale")

    if not version or not locale:
        return False

    champion_path = _champion_file_path(version, locale)

    if not champion_path.exists():
        return False

    champion_data = json.loads(champion_path.read_text(encoding="utf-8"))
    _id_to_name, _name_to_id, _id_to_image_key = _build_maps(champion_data)
    _loaded_version = version
    _loaded_locale = locale

    return True


def _ensure_loaded() -> None:
    if _id_to_name is not None:
        return

    if not _load_from_disk():
        raise ChampionDataNotLoadedError(
            "챔피언 데이터 캐시가 없음. champion_data.refresh_champion_data()를 먼저 실행해줘 "
            "(또는 `python -m riot.champion_data`)."
        )


async def _fetch_latest_version() -> str:
    session = get_session()

    async with session.get(VERSIONS_URL) as response:
        response.raise_for_status()
        versions = await response.json()

    if not versions:
        raise ChampionDataError("Data Dragon 버전 목록이 비어 있음")

    return versions[0]


async def _fetch_champion_json(version: str, locale: str) -> dict:
    session = get_session()
    url = CHAMPION_URL_TEMPLATE.format(version=version, locale=locale)

    async with session.get(url) as response:
        response.raise_for_status()
        return await response.json()


async def refresh_champion_data(force: bool = False, locale: str = DEFAULT_LOCALE) -> str:
    """
    최신 Data Dragon 버전을 확인하고, 그 버전+로케일의 champion.json이 로컬에
    아직 없거나 force=True면 새로 받아서
    data/ddragon/champion_<version>_<locale>.json으로 저장함. 이미 캐시돼
    있으면(force가 아닌 한) 재요청 없이 그 파일을 그대로 씀 - 최신 버전이
    이전에 이미 받아둔 버전과 같다면 이번 호출로 인한 추가 요청은
    versions.json 하나뿐임.

    반환값은 이번에 로드된(캐시로 쓰이게 된) 버전 문자열.
    """
    global _id_to_name, _name_to_id, _id_to_image_key, _loaded_version, _loaded_locale

    latest_version = await _fetch_latest_version()
    champion_path = _champion_file_path(latest_version, locale)

    if force or not champion_path.exists():
        champion_data = await _fetch_champion_json(latest_version, locale)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        champion_path.write_text(json.dumps(champion_data), encoding="utf-8")
    else:
        champion_data = json.loads(champion_path.read_text(encoding="utf-8"))

    _save_meta(latest_version, locale)
    _id_to_name, _name_to_id, _id_to_image_key = _build_maps(champion_data)
    _loaded_version = latest_version
    _loaded_locale = locale

    return latest_version


def champion_name(id: int) -> str | None:
    """
    챔피언 ID로 (캐시된 로케일의, 기본 한글) 이름을 찾음. 캐시에 없는 ID면
    None (없는 챔피언이 아니라 캐시가 오래됐을 수도 있으니, 호출 쪽에서
    필요하면 refresh를 고려할 것).
    """
    _ensure_loaded()
    return _id_to_name.get(id)


def champion_id(name: str) -> int | None:
    """
    챔피언 이름(캐시된 로케일의 Data Dragon "name" 필드, 기본 한글 - 예: "아리")으로
    ID를 찾음. 개명된 챔피언은 캐시된 버전 시점의 이름 기준으로만 찾아짐.
    """
    _ensure_loaded()
    return _name_to_id.get(name)


def champion_image_key(id: int) -> str | None:
    """
    챔피언 ID로 Data Dragon 스퀘어 아이콘 URL에 쓰는 (항상 영문) 이미지 키를
    찾음(예: 266 -> "Aatrox"). 아이콘 URL은
    f"https://ddragon.leagueoflegends.com/cdn/{version}/img/champion/{key}.png".
    """
    _ensure_loaded()
    return _id_to_image_key.get(id)


def known_champion_ids() -> list[int]:
    """현재 캐시에 있는 챔피언 ID 전체(champion_emoji.py의 동기화 대상 목록으로 씀)."""
    _ensure_loaded()
    return list(_id_to_name)


def current_version() -> str | None:
    _ensure_loaded()
    return _loaded_version


def current_locale() -> str | None:
    _ensure_loaded()
    return _loaded_locale


def _main() -> None:
    parser = argparse.ArgumentParser(description="Data Dragon 챔피언 데이터 캐시 갱신")
    parser.add_argument(
        "--force", action="store_true",
        help="최신 버전이 이미 캐시돼 있어도 champion.json을 다시 받음"
    )
    parser.add_argument(
        "--locale", default=DEFAULT_LOCALE,
        help=f"Data Dragon 로케일 (기본 {DEFAULT_LOCALE})"
    )
    args = parser.parse_args()

    async def _run():
        from http_session import close_session

        try:
            version = await refresh_champion_data(force=args.force, locale=args.locale)
            print(f"버전 {version} ({args.locale}) 캐시 완료 (챔피언 {len(_id_to_name)}개)")
        finally:
            await close_session()

    asyncio.run(_run())


if __name__ == "__main__":
    _main()
