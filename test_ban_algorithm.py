"""Manual v1 demo: real Pongmin#3369 plus four synthetic teammates.

Run ``python test_ban_algorithm.py`` (optionally ``--db /path/to/scouting.db``).
The collected database is opened read-only and copied into memory. All fixture
writes happen there; this script makes no API calls or reads of .env. Inspect
the printed Pongmin diagnostics by eye; this is not an evaluation harness.
"""

import argparse
import math
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
from uuid import uuid4

from ban_algorithm import (
    BETA,
    arithmetic_mean,
    build_player_model,
    format_recommendation,
    geometric_mean,
    harmonic_mean,
    recommend_bans,
)
import scouting_db as db
from scouting_repo import ScoutingRepo


DAY_MS = 86_400_000


def copy_collected_database(path: Path, memory: sqlite3.Connection) -> None:
    if not path.is_file():
        raise ValueError(
            f"Collected database not found: {path}. Supply --db with an existing "
            "scouting.db containing Pongmin#3369, or collect that player's matches first."
        )
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as source:
        source.backup(memory)
    # Any schema migration also applies only to the temporary in-memory copy.
    db.init_db(memory)


def add_synthetic_companions(conn: sqlite3.Connection, cutoff: int) -> list[tuple[int, str]]:
    """Make deterministic observations, using unique IDs to isolate the fixtures."""
    profiles = (
        ("TOP", "EMERALD", ((122, "Darius"), (86, "Garen"), (58, "Renekton"), (54, "Malphite"))),
        ("JUNGLE", "PLATINUM", ((64, "LeeSin"), (104, "Graves"), (254, "Vi"), (32, "Amumu"))),
        ("MIDDLE", "DIAMOND", ((103, "Ahri"), (61, "Orianna"), (112, "Viktor"), (1, "Annie"))),
        ("UTILITY", "GOLD", ((412, "Thresh"), (111, "Nautilus"), (89, "Leona"), (117, "Lulu"))),
    )
    namespace = f"ban-demo-{uuid4().hex}"
    opponents = []
    # All four champions have observations and different win rates; a three-ban
    # set cannot exhaust a synthetic companion's entire observed pool.
    pick_sequence = [0] * 15 + [1] * 8 + [2] * 5 + [3] * 3
    win_counts = (11, 4, 2, 1)
    for role, tier, pool in profiles:
        player_id = db.get_or_create_player(
            conn, f"{namespace}-{role}", f"Synthetic {role}", "DEMO", seen_at=cutoff
        )
        peer_id = db.get_or_create_player(
            conn, f"{namespace}-{role}-peer", f"Synthetic {role} peer", "DEMO", seen_at=cutoff
        )
        db.insert_rank_snapshot(conn, player_id, "RANKED_SOLO_5x5", tier, "II", 50, 40, 30, cutoff)
        occurrences = [0] * len(pool)
        for game_number in range(len(pick_sequence)):
            # A coprime stride interleaves champion picks across dates.
            pick = pick_sequence[(game_number * 7) % len(pick_sequence)]
            win = occurrences[pick] < win_counts[pick]
            occurrences[pick] += 1
            game_end = cutoff - (game_number + 1) * DAY_MS
            match_id = f"{namespace}-{role}-{game_number}"
            db.upsert_match(
                conn,
                match_id,
                {
                    "game_start": game_end - 1_800_000,
                    "game_end": game_end,
                    "game_version": "demo",
                    "patch": "demo",
                    "queue_id": 400 if game_number % 4 == 0 else 420,
                    "game_duration": 1800,
                },
                raw_file_path="",
            )
            # A same-role opponent provides local empirical meta observations.
            peer_pick = (pick + 1 + game_number % 3) % len(pool)
            for participant_id, observed_player, champion, observed_win in (
                (1, player_id, pool[pick], win),
                (6, peer_id, pool[peer_pick], not win),
            ):
                champion_id, champion_name = champion
                db.upsert_player_match(
                    conn,
                    observed_player,
                    match_id,
                    {
                        "team_id": 100 if participant_id == 1 else 200,
                        "participant_id": participant_id,
                        "champion_id": champion_id,
                        "champion_name": champion_name,
                        "team_position": role,
                        "individual_position": role,
                        "canonical_role": role,
                        "role_mismatch": False,
                        "win": observed_win,
                        "kills": 5,
                        "deaths": 4,
                        "assists": 8,
                    },
                    patch="demo",
                )
        opponents.append((player_id, role))
    conn.commit()
    return opponents


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=db.DB_PATH, help="Existing collected database (read-only)")
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    print("[Pongmin sanity]", flush=True)
    try:
        cutoff = db.now_ms()
        with closing(sqlite3.connect(":memory:")) as conn:
            conn.row_factory = sqlite3.Row
            copy_collected_database(args.db, conn)
            with ScoutingRepo(cutoff_time=cutoff, conn=conn) as repo:
                pongmin_id = repo.get_player_id_by_riot_id("Pongmin", "3369")
                if pongmin_id is None:
                    raise ValueError(
                        "Pongmin#3369 is missing. Supply --db with that player's "
                        "collected database or collect their queue 420/400 matches first."
                    )
                pongmin = build_player_model(repo, pongmin_id, "BOTTOM")
                top = max(pongmin.champions.values(), key=lambda c: (c.p_final, -c.champion_id))
                print(f"  Top champion: {top.name}")
                print(f"  P_final(X): {top.p_final:.3f}")
                print(f"  T(X): {top.threat:.3f}")
                print(f"  r(empty): {pongmin.residual_ratio():.3f}")
                top_ban = frozenset({top.champion_id})
                print(f"  r({{X}}): {pongmin.residual_ratio(top_ban):.3f}")
                print(f"  observed_pool_exhausted(empty): {pongmin.observed_pool_exhausted()}")
                print(f"  observed_pool_exhausted({{X}}): {pongmin.observed_pool_exhausted(top_ban)}")
                print(
                    f"  BOTTOM, queues 420+400: {sum(c.games for c in pongmin.champions.values())} games, "
                    f"{len(pongmin.champions)} champions; baseline WR={pongmin.baseline_winrate:.3f}, "
                    f"baseline KDA={pongmin.kda_baseline:.3f}"
                )
                for champion in sorted(pongmin.champions.values(), key=lambda c: (-c.p_final, c.champion_id)):
                    print(
                        f"    {champion.name}: {champion.wins}/{champion.games} wins, "
                        f"P_personal={champion.p_personal:.3f}, P_meta={champion.p_meta:.3f}, "
                        f"P_final={champion.p_final:.3f}, WR_adj={champion.wr_adj:.3f}, "
                        f"KDA={champion.kda_champ:.3f}, KDA_adj={champion.kda_adj:.3f}, "
                        f"T_WR_only={math.exp(BETA * (champion.wr_adj - pongmin.baseline_winrate)):.3f}, "
                        f"T_with_KDA={champion.threat:.3f}"
                    )
                print("  Inspect these values by eye; no automatic Pongmin pass/fail assertion.\n", flush=True)
                companions = add_synthetic_companions(conn, cutoff)
                opponents = companions[:3] + [(pongmin_id, "BOTTOM")] + companions[3:]
                print("Team: collected Pongmin#3369 BOTTOM + synthetic TOP/JUNGLE/MIDDLE/UTILITY.\n", flush=True)
                result = recommend_bans(repo, opponents)
                print(format_recommendation(result))
                synthetic = result.players[0]
                entire_pool = frozenset(synthetic.champions)
                print("\n[Synthetic exhausted-pool illustration]")
                print(f"  {synthetic.label}/{synthetic.role}: ban all {len(entire_pool)} observed champions")
                print(f"  S={synthetic.strength(entire_pool):.3f}, r={synthetic.residual_ratio(entire_pool):.3f}")
                print(f"  observed_pool_exhausted={synthetic.observed_pool_exhausted(entire_pool)}")
                print("  This flag records an exhausted observed pool; it does not establish actual zero ability.")
                residuals, weights = (0, 1, 1, 1, 1), (0.2,) * 5
                print(
                    f"  Equal-weight team with r=(0, 1, 1, 1, 1): "
                    f"V_1={arithmetic_mean(residuals, weights):.3f}, "
                    f"V_0={geometric_mean(residuals, weights):.3f}, "
                    f"V_-1={harmonic_mean(residuals, weights):.3f}"
                )
    except (ValueError, sqlite3.Error) as exc:
        print(f"Manual demo could not complete: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
