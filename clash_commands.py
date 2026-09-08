import asyncio

import discord
from discord import app_commands

from riot_api import (
    parse_riot_id,
    get_account_by_riot_id,
    get_account_by_puuid,
    get_clash_team_id,
    get_clash_team,
    RiotApiError,
    InvalidRiotIdError,
    PlayerNotFoundError,
    NotInClashError,
    InvalidApiKeyError,
    RateLimitedError,
    RiotServerError,
)
from mock_clash_data import (
    is_mock_tag_line,
    mock_get_team_id,
    mock_get_team,
    mock_get_account_by_puuid,
)


# 표시 순서 (CLASH-V1이 주는 position 값 그대로 사용)
POSITION_ORDER = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY", "UNSELECTED", "FILL"]


def _position_sort_key(player) -> int:
    try:
        return POSITION_ORDER.index(player.position)
    except ValueError:
        return len(POSITION_ORDER)


def setup_clash_commands(bot):

    @bot.tree.command(
        name="clashlookup",
        description="Riot ID로 현재 참가 중인 클래시 팀을 조회합니다."
    )
    @app_commands.describe(riot_id="예: Hide on bush#KR1")
    async def clashlookup(interaction: discord.Interaction, riot_id: str):
        await interaction.response.defer()

        try:
            game_name, tag_line = parse_riot_id(riot_id)

            # "이름#MOCK" 태그라인이면 실제 Riot API 대신 mock_clash_data로 라우팅.
            # 이후 정렬/조회/임베드 생성 로직은 실제 팀 조회와 완전히 동일한 경로를 탄다.
            if is_mock_tag_line(tag_line):
                team_id = await mock_get_team_id(game_name)
                players = await mock_get_team(team_id)
                resolve_account = mock_get_account_by_puuid
            else:
                account = await get_account_by_riot_id(game_name, tag_line)
                team_id = await get_clash_team_id(account.puuid)
                players = await get_clash_team(team_id)
                resolve_account = get_account_by_puuid

            # puuid -> 현재 Riot ID 는 병렬로 조회
            resolved_accounts = await asyncio.gather(
                *(resolve_account(player.puuid) for player in players),
                return_exceptions=True
            )

            rows = sorted(
                zip(players, resolved_accounts),
                key=lambda pair: _position_sort_key(pair[0])
            )

            lines = []
            for player, resolved in rows:
                if isinstance(resolved, Exception):
                    name = "알 수 없음"
                else:
                    name = f"{resolved.game_name}#{resolved.tag_line}"

                marker = " (C)" if player.role == "CAPTAIN" else ""
                lines.append(f"{player.position:<9}{name}{marker}")

            embed = discord.Embed(
                title="클래시 팀",
                description="```\n" + "\n".join(lines) + "\n```",
                color=0x5865F2
            )

            await interaction.followup.send(embed=embed)

        except InvalidRiotIdError as e:
            await interaction.followup.send(f"❌ {e}")

        except PlayerNotFoundError as e:
            await interaction.followup.send(f"❌ {e}")

        except NotInClashError as e:
            await interaction.followup.send(f"❌ {e}")

        except InvalidApiKeyError as e:
            await interaction.followup.send(f"❌ {e}")

        except RateLimitedError as e:
            retry_msg = f" ({e.retry_after}초 후 다시 시도해줘)" if e.retry_after else ""
            await interaction.followup.send(f"❌ Riot API 요청 한도를 초과했습니다.{retry_msg}")

        except RiotServerError as e:
            await interaction.followup.send(f"❌ {e}")

        except RiotApiError as e:
            await interaction.followup.send(f"❌ Riot API 오류: {e}")

        except Exception as e:
            print("CLASHLOOKUP ERROR:", repr(e))
            await interaction.followup.send(f"❌ 알 수 없는 오류가 발생했습니다: `{type(e).__name__}`")
