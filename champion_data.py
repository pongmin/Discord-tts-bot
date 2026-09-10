"""
Data Dragon 챔피언 ID <-> 이름 매핑.

Riot API(riot_api.py)와는 별개의 정적 데이터 소스임 - API 키가 필요 없고
레이트 리밋도 없어서, Riot API 클라이언트나 그 안의 429 처리 로직을 거치지
않고 http_session만 공유해서 직접 호출함.

data/ddragon/에 버전별 champion.json을 캐시하고, meta.json에 "지금 쓰는
버전"을 기록함. 챔피언 ID는 패치가 바뀌어도 안 바뀌지만 이름/키는 바뀔 수
있으므로(개명 등) 캐시를 항상 버전과 함께 저장/로드함.

champion_name()/champion_id()는 디스크 캐시만 읽고 절대 네트워크 요청을
하지 않음. 새 패치 반영은 refresh_champion_data()를 명시적으로 호출해야만
일어남 - 매 호출마다 최신 버전인지 자동으로 확인하지 않음.
"""

import argparse
import asyncio
import json
from pathlib import Path

from http_session import get_session

DATA_DIR = Path(__file__).resolve().parent / "data" / "ddragon"
META_PATH = DATA_DIR / "meta.json"

VERSIONS_URL = "https://ddragon.leagueoflegends.com/api/versions.json"
CHAMPION_URL_TEMPLATE = "https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/champion.json"


class ChampionDataError(Exception):
    """Data Dragon 관련 오류의 기본 클래스"""


class ChampionDataNotLoadedError(ChampionDataError):
    """캐시가 아직 없어서 champion_name/champion_id를 쓸 수 없는 상태"""


# 프로세스 내 인메모리 캐시. 디스크 캐시가 있어도 매 호출마다 다시 읽지 않기 위함.
_id_to_name: dict[int, str] | None = None
_name_to_id: dict[str, int] | None = None
_loaded_version: str | None = None


def _champion_file_path(version: str) -> Path:
    return DATA_DIR / f"champion_{version}.json"


def _build_maps(champion_data: dict) -> tuple[dict[int, str], dict[str, int]]:
    id_to_name: dict[int, str] = {}
    name_to_id: dict[str, int] = {}

    for entry in champion_data["data"].values():
        champion_id_value = int(entry["key"])
        name = entry["name"]

        id_to_name[champion_id_value] = name
        name_to_id[name] = champion_id_value

    return id_to_name, name_to_id


def _load_meta() -> dict | None:
    if not META_PATH.exists():
        return None

    return json.loads(META_PATH.read_text(encoding="utf-8"))


def _save_meta(version: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    META_PATH.write_text(json.dumps({"version": version}), encoding="utf-8")


def _load_from_disk() -> bool:
    """
    디스크 캐시(meta.json + champion_<version>.json)가 온전히 있으면 인메모리
    맵을 채우고 True를 반환함. 없거나 일부만 있으면 False (네트워크 요청은 하지 않음).
    """
    global _id_to_name, _name_to_id, _loaded_version

    meta = _load_meta()

    if meta is None:
        return False

    version = meta.get("version")

    if not version:
        return False

    champion_path = _champion_file_path(version)

    if not champion_path.exists():
        return False

    champion_data = json.loads(champion_path.read_text(encoding="utf-8"))
    _id_to_name, _name_to_id = _build_maps(champion_data)
    _loaded_version = version

    return True


def _ensure_loaded() -> None:
    if _id_to_name is not None:
        return

    if not _load_from_disk():
        raise ChampionDataNotLoadedError(
            "챔피언 데이터 캐시가 없음. champion_data.refresh_champion_data()를 먼저 실행해줘 "
            "(또는 `python champion_data.py`)."
        )


async def _fetch_latest_version() -> str:
    session = get_session()

    async with session.get(VERSIONS_URL) as response:
        response.raise_for_status()
        versions = await response.json()

    if not versions:
        raise ChampionDataError("Data Dragon 버전 목록이 비어 있음")

    return versions[0]


async def _fetch_champion_json(version: str) -> dict:
    session = get_session()
    url = CHAMPION_URL_TEMPLATE.format(version=version)

    async with session.get(url) as response:
        response.raise_for_status()
        return await response.json()


async def refresh_champion_data(force: bool = False) -> str:
    """
    최신 Data Dragon 버전을 확인하고, 그 버전의 champion.json이 로컬에 아직
    없거나 force=True면 새로 받아서 data/ddragon/champion_<version>.json으로
    저장함. 이미 캐시돼 있으면(force가 아닌 한) 재요청 없이 그 파일을 그대로 씀 -
    최신 버전이 이전에 이미 받아둔 버전과 같다면 이번 호출로 인한 추가 요청은
    versions.json 하나뿐임.

    반환값은 이번에 로드된(캐시로 쓰이게 된) 버전 문자열.
    """
    global _id_to_name, _name_to_id, _loaded_version

    latest_version = await _fetch_latest_version()
    champion_path = _champion_file_path(latest_version)

    if force or not champion_path.exists():
        champion_data = await _fetch_champion_json(latest_version)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        champion_path.write_text(json.dumps(champion_data), encoding="utf-8")
    else:
        champion_data = json.loads(champion_path.read_text(encoding="utf-8"))

    _save_meta(latest_version)
    _id_to_name, _name_to_id = _build_maps(champion_data)
    _loaded_version = latest_version

    return latest_version


def champion_name(id: int) -> str | None:
    """
    챔피언 ID로 이름을 찾음. 캐시에 없는 ID면 None (없는 챔피언이 아니라
    캐시가 오래됐을 수도 있으니, 호출 쪽에서 필요하면 refresh를 고려할 것).
    """
    _ensure_loaded()
    return _id_to_name.get(id)


def champion_id(name: str) -> int | None:
    """
    챔피언 이름(Data Dragon의 "name" 필드, 예: "Aurelion Sol")으로 ID를 찾음.
    개명된 챔피언은 캐시된 버전 시점의 이름 기준으로만 찾아짐.
    """
    _ensure_loaded()
    return _name_to_id.get(name)


def current_version() -> str | None:
    _ensure_loaded()
    return _loaded_version


def _main() -> None:
    parser = argparse.ArgumentParser(description="Data Dragon 챔피언 데이터 캐시 갱신")
    parser.add_argument(
        "--force", action="store_true",
        help="최신 버전이 이미 캐시돼 있어도 champion.json을 다시 받음"
    )
    args = parser.parse_args()

    async def _run():
        from http_session import close_session

        try:
            version = await refresh_champion_data(force=args.force)
            print(f"버전 {version} 캐시 완료 (챔피언 {len(_id_to_name)}개)")
        finally:
            await close_session()

    asyncio.run(_run())


if __name__ == "__main__":
    _main()
