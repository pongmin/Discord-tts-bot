"""Read cached presentation metadata for the Discord ban report; never fetch data."""

import json
from pathlib import Path
import re
from urllib.parse import quote
import zlib

from scouting.ban_algorithm import Recommendation
from scouting.ban_report_ui import PlayerPresentation
from scouting.raw_match_store import load_raw_match
from riot.riot_api import PLATFORM_ROUTE, RANKED_SOLO_QUEUE_TYPE
from scouting.scouting_repo import ScoutingRepo


# Repo root's shared data/ dir, not this package's own folder.
DDRAGON_META_PATH = Path(__file__).resolve().parent.parent / "data" / "ddragon" / "meta.json"
# Verified against Riot's versions.json and profile-icon documentation on
# 2026-09-11. Prefer the already cached version; Match-V5 gameVersion is not a
# Data Dragon version and must not be converted by guessing a patch suffix.
# https://ddragon.leagueoflegends.com/api/versions.json
# https://developer.riotgames.com/docs/lol#data-dragon_other
FALLBACK_DDRAGON_VERSION = "16.18.1"
ICON_MATCH_LOOKBACK = 5  # Cosmetic lookup only; never limits analysis DB reads.
OPGG_REGIONS = {
    "KR": "kr", "JP1": "jp", "NA1": "na", "EUW1": "euw", "EUN1": "eune",
    "BR1": "br", "LA1": "lan", "LA2": "las", "OC1": "oce", "TR1": "tr",
    "RU": "ru", "PH2": "ph", "SG2": "sg", "TH2": "th", "TW2": "tw", "VN2": "vn",
}


def _ddragon_version() -> str:
    try:
        meta = json.loads(DDRAGON_META_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return FALLBACK_DDRAGON_VERSION
    version = meta.get("version") if isinstance(meta, dict) else None
    if isinstance(version, str) and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        return version
    return FALLBACK_DDRAGON_VERSION


def _profile_icon_id(data: dict) -> int | None:
    for key in ("profileIconId", "profileIcon", "profile_icon_id"):
        value = data.get(key)
        if type(value) is int and value >= 0:
            return value
    return None


def _cached_profile_icon(
    repo: ScoutingRepo, player_id: int, player: dict, raw_cache: dict,
) -> int | None:
    icon_id = _profile_icon_id(player)
    if icon_id is not None or not player.get("puuid"):
        return icon_id

    matches = repo.get_all_matches(player_id, queue_ids=(420, 400))
    recent = sorted(matches, key=lambda row: row["game_end"] or row["game_start"], reverse=True)
    for match in recent[:ICON_MATCH_LOOKBACK]:
        match_id = match["match_id"]
        if match_id not in raw_cache:
            try:
                raw_cache[match_id] = load_raw_match(match_id)
            except (OSError, ValueError, EOFError, zlib.error):
                # A missing or damaged cosmetic source must not reject an
                # otherwise valid recommendation from the existing DB data.
                raw_cache[match_id] = None
        raw = raw_cache[match_id]
        info = raw.get("info") if isinstance(raw, dict) else None
        participants = info.get("participants") if isinstance(info, dict) else None
        if not isinstance(participants, list):
            continue
        for participant in participants:
            if not isinstance(participant, dict) or participant.get("puuid") != player["puuid"]:
                continue
            icon_id = _profile_icon_id(participant)
            if icon_id is not None:
                return icon_id
    return None


def _opgg_url(player: dict) -> str | None:
    game_name, tag_line = player.get("game_name"), player.get("tag_line")
    platform = str(player.get("platform") or PLATFORM_ROUTE).upper()
    region = OPGG_REGIONS.get(platform)
    if not game_name or not tag_line or region is None:
        return None
    # OP.GG's Riot-ID path uses a hyphen between separately escaped components.
    # quote(..., safe="") preserves Unicode/spaces safely inside the path.
    return f"https://op.gg/lol/summoners/{region}/{quote(game_name, safe='')}-{quote(tag_line, safe='')}"


def load_player_presentations(
    repo: ScoutingRepo, result: Recommendation,
) -> dict[int, PlayerPresentation]:
    """Use the recommendation's cutoff repo and optional cached cosmetic assets.

    Run with the recommendation's worker-thread repo. Rank snapshots and own
    matches use the same cutoff, and no API request or storage mutation occurs.
    """
    version = _ddragon_version()
    presentations = {}
    raw_cache = {}
    for model in result.players:
        player_row = repo.get_player(model.player_id)
        player = dict(player_row) if player_row is not None else {}
        rank_row = repo.get_latest_rank_snapshot(model.player_id, RANKED_SOLO_QUEUE_TYPE)
        rank = dict(rank_row) if rank_row is not None else {}
        icon_id = _cached_profile_icon(repo, model.player_id, player, raw_cache)
        icon_url = (
            f"https://ddragon.leagueoflegends.com/cdn/{version}/img/profileicon/{icon_id}.png"
            if icon_id is not None else None
        )
        presentations[model.player_id] = PlayerPresentation(
            tier=rank.get("tier"), division=rank.get("division"), lp=rank.get("lp"),
            profile_icon_url=icon_url, opgg_url=_opgg_url(player),
        )
    return presentations
