"""
Match-V5 원본 응답을 압축해서 영구 보관하는 저장소.

data/raw_matches/<match_id>.json.gz 로 저장하며, 한 번 저장된 파일은
바꾸지 않음(불변). 파생 데이터(SQLite)는 언제든 이 원본에서 다시 만들 수 있어야 함.
"""

import gzip
import json
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
RAW_MATCH_DIR = DATA_DIR / "raw_matches"


def raw_match_path(match_id: str) -> Path:
    return RAW_MATCH_DIR / f"{match_id}.json.gz"


def raw_match_exists(match_id: str) -> bool:
    return raw_match_path(match_id).exists()


def save_raw_match(match_id: str, match_data: dict) -> Path:
    """
    이미 저장돼 있으면 다시 쓰지 않음(불변 저장소).
    """
    path = raw_match_path(match_id)

    if path.exists():
        return path

    RAW_MATCH_DIR.mkdir(parents=True, exist_ok=True)

    # 쓰다가 중간에 죽어도 반쪽짜리 .gz가 남지 않도록 임시 파일에 쓰고 원자적으로 교체
    tmp_path = path.with_suffix(path.suffix + ".tmp")

    with gzip.open(tmp_path, "wt", encoding="utf-8") as f:
        json.dump(match_data, f, ensure_ascii=False)

    tmp_path.replace(path)

    return path


def load_raw_match(match_id: str) -> dict:
    path = raw_match_path(match_id)

    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)
