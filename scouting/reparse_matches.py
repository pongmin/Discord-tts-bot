"""
로컬에 캐시된 원본 매치(data/raw_matches/*.json.gz)에서 player_matches를
네트워크 호출 없이 재생성하는 CLI.

용도:
- player_matches 스키마가 바뀌었거나(예: patch 컬럼 추가), canonical_role
  판정 로직이 바뀐 뒤에 기존 데이터를 다시 계산할 때.
- 과거에는 매치당 수집을 요청한 선수 1명만 저장했는데, 지금은 참가자 10명
  전원을 저장함 - 이미 받아둔 원본으로 나머지 9명을 채워 넣을 때도 이 스크립트로 함.

이미 있는 행은 최신 파생 로직으로 덮어쓸 뿐이고(ON CONFLICT DO UPDATE),
raw_matches/*.json.gz는 절대 건드리지 않으므로 몇 번을 다시 실행해도 안전함.

사용법:
    python -m scouting.reparse_matches
    python -m scouting.reparse_matches --limit 500   # 앞에서부터 일부만 (테스트용)
"""

import argparse

from scouting import scouting_db as db
from scouting.raw_match_store import RAW_MATCH_DIR, raw_match_path, load_raw_match
from scouting.match_collector import build_match_row, store_all_participants


def _iter_cached_match_ids():
    if not RAW_MATCH_DIR.exists():
        return

    for path in sorted(RAW_MATCH_DIR.glob("*.json.gz")):
        yield path.name.removesuffix(".json.gz")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="원본 매치 JSON에서 player_matches를 재생성함 (네트워크 호출 없음)"
    )
    parser.add_argument("--limit", type=int, default=None, help="앞에서부터 N개만 처리 (테스트용)")

    args = parser.parse_args()

    conn = db.get_connection()
    db.init_db(conn)

    try:
        match_ids = list(_iter_cached_match_ids())

        if args.limit is not None:
            match_ids = match_ids[: args.limit]

        total = len(match_ids)
        print(f"재생성 대상: {total}개 매치")

        if total == 0:
            print("data/raw_matches/에 저장된 원본이 없음. 먼저 `python -m scouting.collect_matches`로 수집해줘.")
            return

        for i, match_id in enumerate(match_ids, start=1):
            raw_data = load_raw_match(match_id)

            match_row = build_match_row(raw_data)
            db.upsert_match(conn, match_id, match_row, str(raw_match_path(match_id)))

            store_all_participants(conn, match_id, raw_data)

            conn.commit()

            if i % 200 == 0 or i == total:
                print(f"  {i}/{total} 처리함")

        print("완료.")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
