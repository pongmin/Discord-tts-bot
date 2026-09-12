"""Saved teams: a name for five Riot IDs, so nobody retypes them every time.

A saved team is an alias and nothing more. It stores five "이름#태그" strings and
never any scouting or match data - resolving one just hands those five strings
to the existing commands, which then do the same account lookup, collection,
cache reuse and analysis they always do. Nothing here knows about scoring,
role fit, or bans.

The saved order carries no role meaning for /assignroles, which is the whole
point of that command. /banrecommend does need roles, so it reads the saved
order positionally (slot 1 = TOP ... slot 5 = SUPPORT); /teamregister and
/teamshow both say so, since that is the only place the order matters.
"""

import asyncio
from contextlib import closing
import logging

import discord
from discord import app_commands

from scouting.ban_commands import (
    BanCommandError, ROLE_INPUTS, ROLE_LABELS, parse_inputs,
)
from scouting import scouting_db as db


logger = logging.getLogger(__name__)
TEAM_SLOTS = ("player1", "player2", "player3", "player4", "player5")
TEAM_LABELS = {slot: f"선수 {index}" for index, slot in enumerate(TEAM_SLOTS, start=1)}
TEAM_NAME_MAX = 32
# What /banrecommend reads the saved order as. /assignroles ignores it.
TEAM_SLOT_ROLES = dict(zip(TEAM_SLOTS, ROLE_INPUTS))
GUILD_ONLY_ERROR = "저장된 팀은 서버에서만 쓸 수 있습니다. 서버 채널에서 실행해주세요."


def normalize_team_name(name: str) -> str:
    """Reject an unusable team name before it ever reaches the database."""
    name = (name or "").strip()
    if not name:
        raise BanCommandError("팀 이름을 입력해주세요.")
    if len(name) > TEAM_NAME_MAX:
        raise BanCommandError(f"팀 이름은 {TEAM_NAME_MAX}자 이하로 지어주세요.")
    return name


def _save_team_row(guild_id: int, team_name: str, players: list[str]) -> bool:
    with closing(db.get_connection()) as conn:
        db.init_db(conn)
        return db.save_team(conn, guild_id, team_name, players)


def _load_team_row(guild_id: int, team_name: str):
    with closing(db.get_connection()) as conn:
        db.init_db(conn)
        return db.get_team(conn, guild_id, team_name)


def _delete_team_row(guild_id: int, team_name: str) -> bool:
    with closing(db.get_connection()) as conn:
        db.init_db(conn)
        return db.delete_team(conn, guild_id, team_name)


async def resolve_team(
    guild_id: int | None, team_name: str, *,
    slots: tuple[str, ...], labels: dict[str, str],
) -> dict[str, str]:
    """A saved team name -> the five slot inputs the calling command expects.

    Validates on the way out as strictly as /teamregister validated on the way
    in: a team that somehow holds the wrong number of players, an unparseable
    Riot ID, or a duplicate account is reported as an actionable error rather
    than handed to collection.
    """
    if guild_id is None:
        raise BanCommandError(GUILD_ONLY_ERROR)
    team_name = normalize_team_name(team_name)
    row = await asyncio.to_thread(_load_team_row, guild_id, team_name)
    if row is None:
        raise BanCommandError(
            f"저장된 팀 '{team_name}'을(를) 찾을 수 없습니다. /teamregister로 먼저 등록해주세요."
        )
    players = db.team_players(row)
    if len(players) != len(slots):
        raise BanCommandError(
            f"저장된 팀 '{row['team_name']}'에 선수가 {len(players)}명입니다. "
            f"{len(slots)}명을 다시 등록해주세요(/teamregister)."
        )
    inputs = dict(zip(slots, players))
    # Reuse the command parser so a stored team is held to the same rules as
    # typed input; names can also have been re-registered on Riot's side.
    try:
        parse_inputs(inputs, slots=slots, labels=labels)
    except BanCommandError as exc:
        raise BanCommandError(
            f"저장된 팀 '{row['team_name']}'의 선수 정보가 올바르지 않습니다: {exc} "
            "/teamregister로 다시 등록해주세요."
        ) from exc
    return inputs


async def resolve_team_or_inputs(
    guild_id: int | None, team: str | None, provided: dict[str, str | None], *,
    slots: tuple[str, ...], labels: dict[str, str],
) -> dict[str, str]:
    """Either a saved team name or five typed Riot IDs - never both, never half.

    The five slots become optional on the commands that accept a team, so this
    is where "exactly one of the two input styles" is enforced instead.
    """
    typed = {slot: value for slot, value in provided.items() if value}
    if team and typed:
        raise BanCommandError(
            "팀 이름과 선수를 동시에 지정할 수 없습니다. 둘 중 하나만 입력해주세요."
        )
    if team:
        return await resolve_team(guild_id, team, slots=slots, labels=labels)
    if not typed:
        raise BanCommandError(
            "저장된 팀 이름(team) 또는 선수 5명을 입력해주세요."
        )
    missing = [labels[slot] for slot in slots if not provided.get(slot)]
    if missing:
        raise BanCommandError(
            f"선수 5명을 모두 입력하거나 저장된 팀을 쓰세요. 빠진 자리: {', '.join(missing)}"
        )
    return {slot: provided[slot] for slot in slots}


def _roster_lines(players: list[str]) -> str:
    return "\n".join(
        f"{index}. {player}  ({ROLE_LABELS[TEAM_SLOT_ROLES[slot]]})"
        for index, (slot, player) in enumerate(zip(TEAM_SLOTS, players), start=1)
    )


def setup_team_commands(bot):
    @bot.tree.command(
        name="teamregister",
        description="선수 5명을 팀 이름으로 저장합니다(같은 이름이면 덮어씁니다).",
    )
    @app_commands.describe(
        name=f"팀 이름 ({TEAM_NAME_MAX}자 이하, 서버별로 따로 저장됩니다)",
        player1="선수 1 (이름#태그) · /banrecommend에서는 TOP",
        player2="선수 2 (이름#태그) · /banrecommend에서는 JUNGLE",
        player3="선수 3 (이름#태그) · /banrecommend에서는 MID",
        player4="선수 4 (이름#태그) · /banrecommend에서는 BOTTOM",
        player5="선수 5 (이름#태그) · /banrecommend에서는 SUPPORT",
    )
    async def teamregister(
        interaction: discord.Interaction, name: str, player1: str, player2: str,
        player3: str, player4: str, player5: str,
    ):
        inputs = dict(zip(TEAM_SLOTS, (player1, player2, player3, player4, player5)))
        try:
            if interaction.guild_id is None:
                raise BanCommandError(GUILD_ONLY_ERROR)
            team_name = normalize_team_name(name)
            # Same validation as a typed command: five parseable, distinct IDs.
            parsed = parse_inputs(inputs, slots=TEAM_SLOTS, labels=TEAM_LABELS)
        except BanCommandError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return

        players = [f"{game_name}#{tag_line}" for _, game_name, tag_line in parsed]
        try:
            created = await asyncio.to_thread(
                _save_team_row, interaction.guild_id, team_name, players
            )
        except Exception:
            logger.exception("TEAMREGISTER failed to save %r", team_name)
            await interaction.response.send_message(
                "❌ 팀을 저장하지 못했습니다. 봇 로그를 확인해주세요.", ephemeral=True
            )
            return

        verb = "저장했습니다" if created else "덮어썼습니다"
        await interaction.response.send_message(
            (f"✅ 팀 **{team_name}**을(를) {verb}.\n{_roster_lines(players)}\n\n"
             "`/assignroles team:" f"{team_name}` 또는 `/banrecommend team:{team_name}` 으로 쓸 수 있습니다. "
             "포지션 표기는 /banrecommend에서만 쓰이고, /assignroles는 순서를 무시합니다.")[:1900],
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @bot.tree.command(name="teamshow", description="저장된 팀의 선수 5명을 보여줍니다.")
    @app_commands.describe(name="팀 이름")
    async def teamshow(interaction: discord.Interaction, name: str):
        try:
            if interaction.guild_id is None:
                raise BanCommandError(GUILD_ONLY_ERROR)
            team_name = normalize_team_name(name)
            row = await asyncio.to_thread(_load_team_row, interaction.guild_id, team_name)
            if row is None:
                raise BanCommandError(f"저장된 팀 '{team_name}'을(를) 찾을 수 없습니다.")
        except BanCommandError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return

        players = db.team_players(row)
        await interaction.response.send_message(
            (f"📋 팀 **{row['team_name']}** ({len(players)}명)\n{_roster_lines(players)}\n\n"
             "괄호 안 포지션은 /banrecommend에서만 쓰입니다. "
             "/assignroles는 순서와 무관하게 배치를 직접 계산합니다.")[:1900],
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @bot.tree.command(name="teamdelete", description="저장된 팀을 삭제합니다.")
    @app_commands.describe(name="팀 이름")
    async def teamdelete(interaction: discord.Interaction, name: str):
        try:
            if interaction.guild_id is None:
                raise BanCommandError(GUILD_ONLY_ERROR)
            team_name = normalize_team_name(name)
        except BanCommandError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return

        deleted = await asyncio.to_thread(_delete_team_row, interaction.guild_id, team_name)
        if not deleted:
            await interaction.response.send_message(
                f"❌ 저장된 팀 '{team_name}'을(를) 찾을 수 없습니다.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            f"🗑️ 팀 **{team_name}**을(를) 삭제했습니다.",
            allowed_mentions=discord.AllowedMentions.none(),
        )
