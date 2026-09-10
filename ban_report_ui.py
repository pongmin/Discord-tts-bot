"""Korean Discord presentation for the unchanged observed-pool recommendation.

This module formats existing model values only. It performs no collection, DB
reads, scoring, or recommendation searches; the text formatter remains separate.
"""

import asyncio
from dataclasses import dataclass
import logging
import math
import re

import discord

import champion_data
from ban_algorithm import BanImpact, PlayerModel, Recommendation


logger = logging.getLogger(__name__)
ROLE_ORDER = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY")
ROLE_LABELS = {"TOP": "TOP", "JUNGLE": "JUNGLE", "MIDDLE": "MID",
               "BOTTOM": "BOTTOM", "UTILITY": "SUPPORT"}
TIER_LABELS = {
    "IRON": "아이언", "BRONZE": "브론즈", "SILVER": "실버", "GOLD": "골드",
    "PLATINUM": "플래티넘", "EMERALD": "에메랄드", "DIAMOND": "다이아몬드",
    "MASTER": "마스터", "GRANDMASTER": "그랜드마스터", "CHALLENGER": "챌린저",
}
MAX_DISPLAY_CHAMPIONS = 8
EXHAUSTION_WARNING = (
    "한 선수의 관측 챔피언 풀이 모두 밴됩니다. 미관측 챔피언은 모델링하지 "
    "않으므로 실제 효과는 표시값보다 낮을 수 있습니다."
)


@dataclass(frozen=True)
class PlayerPresentation:
    tier: str | None = None
    division: str | None = None
    lp: int | None = None
    profile_icon_url: str | None = None
    opgg_url: str | None = None


def _units(text: str) -> int:
    # Discord counts supplementary characters as two UTF-16 units.
    return len(text.encode("utf-16-le")) // 2


def _clip(text: str, limit: int) -> str:
    if _units(text) <= limit:
        return text
    kept = []
    used = 0
    for char in text:
        size = _units(char)
        if used + size > limit - 1:
            break
        kept.append(char)
        used += size
    return "".join(kept).rstrip("\\") + "…"


def _display(text: str, limit: int = 80) -> str:
    text = " ".join(str(text).split())
    return _clip(discord.utils.escape_mentions(discord.utils.escape_markdown(text)), limit)


def _player_label(player: PlayerModel) -> str:
    # The algorithm has a numeric fallback for incomplete stored account names.
    return "이름 미확인 선수" if re.fullmatch(r"Player \d+", player.label) else _display(player.label)


def _champion_name(champion_id: int | None, fallback: str) -> str:
    """
    가능하면 Data Dragon 캐시(기본 한글, champion_data.py)에서 챔피언 이름을
    찾아 보여줌. 캐시가 아직 없거나(champion_data.refresh_champion_data()를
    아직 안 돌렸거나) 그 ID가 캐시에 없으면 DB에 저장된 이름(Riot 내부 영문
    키)으로 대체함 - 캐시 상태 때문에 리포트 자체가 실패하면 안 되므로 항상
    뭔가는 보여줌. champion_id를 모르는 호출(경고 문자열에서 정규식으로 뽑아낸
    이름 등)은 fallback만 그대로 씀.
    """
    korean_name = None

    if champion_id is not None:
        try:
            korean_name = champion_data.champion_name(champion_id)
        except champion_data.ChampionDataNotLoadedError:
            korean_name = None

    if korean_name:
        return _display(korean_name, 64)

    return "이름 미확인 챔피언" if str(fallback).isdigit() else _display(fallback, 64)


def _rank_text(presentation: PlayerPresentation) -> str:
    tier = (presentation.tier or "").upper()
    if tier not in TIER_LABELS:
        return "미배치 / 정보 없음"
    text = TIER_LABELS[tier]
    if tier not in {"MASTER", "GRANDMASTER", "CHALLENGER"}:
        division = {"I": "1", "II": "2", "III": "3", "IV": "4"}.get(presentation.division)
        if division:
            text += f" {division}"
    if presentation.lp is not None:
        text += f" · {presentation.lp}점"
    return text


def risk_label(threat: float) -> str:
    if threat >= 1.10:
        return "▲ 높음"
    if threat >= 0.90:
        return "보통"
    return "▼ 낮음"


def stability_label(result: Recommendation) -> str:
    sets = [set(result.best_by_rho[rho].bans) for rho in (1, 0, -1)]
    if sets[0] == sets[1] == sets[2]:
        return "높음"
    return "보통" if len(set.intersection(*sets)) >= 2 else "낮음"


def _warning_owner(result: Recommendation, warning: str) -> PlayerModel | None:
    for player in result.players:
        if warning.startswith((f"{player.label}/{player.role}:", f"{player.label}:")):
            return player
    return None


def _translated_warning(warning: str) -> str:
    if "no meta observations in the seen pool" in warning:
        return "보유 챔피언의 포지션별 비교 기록이 부족해 개인 픽 기록을 기준으로 분석했습니다."
    if "no recognized solo-queue tier at cutoff" in warning:
        return "확인된 솔로랭크 티어가 없어 선수 간 비중 계산에 기본값을 사용했습니다."
    if "role-filtered games available" in warning:
        match = re.search(r"only (\d+) role-filtered games", warning)
        count = match.group(1) if match else "적은 수의"
        return (f"해당 포지션 분석 경기가 {count}경기뿐이라 추천 신뢰도가 낮을 수 있습니다. "
                "추가 기록이 있다면 더 깊게 수집해 보세요.")
    if "An optimum exhausts" in warning:
        return EXHAUSTION_WARNING
    if "An also-consider set exhausts" in warning:
        return ("추가 고려 밴에 관측 챔피언 풀을 모두 소진하는 조합이 있습니다. "
                "미관측 챔피언은 모델링하지 않으므로 실제 효과는 표시값보다 낮을 수 있습니다.")
    if "sensitive to concentration assumption" in warning:
        return "선수별 주력 집중도를 반영하는 방식에 따라 추천 밴이 달라집니다. 안정성이 낮으므로 신중하게 선택하세요."
    if warning.startswith("Negative marginal contribution for "):
        name = warning.removeprefix("Negative marginal contribution for ").split(" (", 1)[0]
        return (f"{_champion_name(None, name)} 밴의 영향도가 음수입니다. "
                "다른 챔피언으로 픽이 옮겨가 오히려 상대 위험도가 높아질 수 있습니다.")
    # Future diagnostics must remain visible without leaking English debug IDs.
    # Their full source is kept in logs; current algorithm warnings are all above.
    logger.warning("Untranslated ban recommendation warning: %s", warning)
    return "추가 분석 경고가 있습니다. 추천 해석에 주의가 필요하며, 상세 내용은 봇 운영자에게 확인해 주세요."


def _warnings(result: Recommendation, player: PlayerModel | None) -> list[str]:
    all_warnings = list(dict.fromkeys([
        *result.warnings,
        *(warning for item in result.players for warning in item.warnings),
    ]))
    messages = []
    for warning in all_warnings:
        owner = _warning_owner(result, warning)
        if (player is None and owner is None) or (
            player is not None and owner is not None and owner.player_id == player.player_id
        ):
            messages.append(_translated_warning(warning))

    main_exhausted = {
        diagnostic.player_id for diagnostic in result.player_diagnostics
        if diagnostic.observed_pool_exhausted
    }
    main_exhausted.update(result.best_by_rho[0].exhausted_player_ids)
    for impact in result.recommended:
        main_exhausted.update(impact.exhausted_player_ids)
    compared_exhausted = {
        pid for search in result.best_by_rho.values() for pid in search.exhausted_player_ids
    }
    extra_exhausted = {
        pid for impact in result.also_consider for pid in impact.exhausted_player_ids
    }
    if player is None:
        if main_exhausted or compared_exhausted:
            messages.append(EXHAUSTION_WARNING)
        if extra_exhausted:
            messages.append(_translated_warning("An also-consider set exhausts"))
        if stability_label(result) == "낮음":
            messages.append(_translated_warning("sensitive to concentration assumption"))
        affected_roles = [ROLE_LABELS[item.role] for item in result.players
                          if any(_warning_owner(result, w) == item for w in all_warnings)]
        if affected_roles:
            messages.append(f"{' · '.join(affected_roles)} 선수의 상세 페이지에 데이터 관련 주의사항이 있습니다.")
    elif player.player_id in main_exhausted:
        messages.append("추천 밴을 모두 적용하면 이 선수의 관측 챔피언 풀이 소진됩니다. "
                        "미관측 챔피언은 반영하지 않아 실제 효과는 표시값보다 낮을 수 있습니다.")
    elif player.player_id in compared_exhausted | extra_exhausted:
        messages.append("비교한 밴 조합 또는 추가 고려 밴에서 이 선수의 관측 챔피언 풀이 소진됩니다. "
                        "미관측 챔피언은 반영하지 않아 효과가 과대평가될 수 있습니다.")
    # Equal known warnings are one caution; count future unrecognized notes so
    # several unrecognized diagnostics cannot disappear through deduplication.
    counts = {message: messages.count(message) for message in messages}
    return [f"{message} ({counts[message]}건)" if counts[message] > 1 and message.startswith("추가 분석")
            else message for message in dict.fromkeys(messages)]


def _add_blocks(embed: discord.Embed, name: str, blocks: list[str]) -> None:
    """Keep each field under 1024 UTF-16 units without dropping caution text."""
    chunks = []
    current = ""
    for block in blocks:
        if current and _units(current + "\n\n" + block) > 1024:
            chunks.append(current)
            current = ""
        # Presentation blocks are bounded above; this also supports long notes.
        while _units(block) > 1024:
            split = _clip(block, 1024)
            chunks.append(split[:-1])
            block = block[len(split) - 1:]
        current = current + "\n\n" + block if current else block
    if current:
        chunks.append(current)
    for index, value in enumerate(chunks):
        embed.add_field(name=name if index == 0 else f"{name} · 계속", value=value, inline=False)


def _embed(role: str, page_index: int) -> discord.Embed:
    embed = discord.Embed(title=f"{role} · 스카우팅 리포트", colour=0x5865F2)
    embed.set_footer(text=f"{page_index + 1} / 6 · 이전·다음 버튼으로 이동")
    return embed


def render_player_page(
    result: Recommendation, player: PlayerModel,
    presentation: PlayerPresentation, page_index: int,
) -> discord.Embed:
    embed = _embed(ROLE_LABELS[player.role], page_index)
    embed.description = f"**{_player_label(player)}**"
    if presentation.profile_icon_url:
        embed.set_thumbnail(url=presentation.profile_icon_url)
    champions = sorted(player.champions.values(), key=lambda champion: (-champion.p_final, champion.champion_id))
    count = sum(champion.games for champion in champions)
    embed.add_field(name="솔로랭크", value=_rank_text(presentation), inline=True)
    embed.add_field(name="포지션 분석 경기", value=f"{count:,}경기", inline=True)
    embed.add_field(name="포지션 승률", value=f"{player.baseline_winrate:.1%}", inline=True)
    top_one = champions[0].p_final if champions else 0
    top_three = math.fsum(champion.p_final for champion in champions[:3])
    embed.add_field(name="주력 집중도", value=f"최다 픽 {top_one:.1%}  ·  상위 3개 합 {top_three:.1%}", inline=False)
    blocks = [
        f"**{index}. {_champion_name(champion.champion_id, champion.name)}**\n"
        f"픽 비중 {champion.p_final:.1%} · 조정 승률 {champion.wr_adj:.1%}\n"
        f"위험도 {champion.threat:.2f} · {risk_label(champion.threat)}"
        for index, champion in enumerate(champions[:MAX_DISPLAY_CHAMPIONS], 1)
    ]
    if len(champions) > MAX_DISPLAY_CHAMPIONS:
        blocks.append(f"외 {len(champions) - MAX_DISPLAY_CHAMPIONS}개 · 집중도와 추천에는 전체 챔피언 반영")
    _add_blocks(embed, "챔피언 풀 · 픽 비중 순", blocks or ["관측된 챔피언이 없습니다."])
    _add_blocks(embed, "주의사항", [f"⚠ {message}" for message in _warnings(result, player)])
    return embed


def _affected_players(result: Recommendation, impact: BanImpact) -> list[PlayerModel]:
    return sorted((player for player in result.players if impact.champion_id in player.champions),
                  key=lambda player: ROLE_ORDER.index(player.role))


def _ban_line(result: Recommendation, impact: BanImpact, index: int) -> str:
    roles = " · ".join(ROLE_LABELS[player.role] for player in _affected_players(result, impact))
    return f"**{index}. {_champion_name(impact.champion_id, impact.name)}** — {roles}\n영향도 {impact.marginal:.1%}"


def _reason(result: Recommendation, impact: BanImpact) -> str:
    players = _affected_players(result, impact)
    if len(players) >= 2:
        shared = " · 공유 밴 효율이 높음" if impact.marginal > 0 else ""
        return f"{len(players)}개 포지션에 동시에 영향{shared}"
    models = [player.champions[impact.champion_id] for player in players]
    # These thresholds choose explanatory words only; no new score or ordering.
    if any(champion.p_final >= 0.5 for champion in models):
        return "주력 픽 의존도가 높음"
    if any(champion.p_final >= 0.2 and champion.threat >= 1.10 for champion in models):
        return "높은 픽 비중과 위험도"
    if any(champion.threat >= 1.10 for champion in models):
        return "해당 포지션에서 위험도가 높은 픽"
    return "전체 밴 조합에서 상대 챔피언 선택에 영향"


def render_summary_page(result: Recommendation) -> discord.Embed:
    embed = _embed("종합 밴 추천", 5)
    embed.description = "상대 5명의 포지션 기록을 바탕으로 분석한 밴 우선순위입니다."
    _add_blocks(embed, "추천 밴 1~3위", [_ban_line(result, impact, index)
                for index, impact in enumerate(result.recommended, 1)])
    embed.add_field(name="예상 상대 위협 감소", value=f"{result.threat_reduction:.1%}", inline=True)
    embed.add_field(name="추천 안정성", value=stability_label(result), inline=True)
    _add_blocks(embed, "왜 이 밴인가?", [f"{_champion_name(impact.champion_id, impact.name)} · {_reason(result, impact)}"
                for impact in result.recommended])
    _add_blocks(embed, "추가 고려 4~8위", [_ban_line(result, impact, index)
                for index, impact in enumerate(result.also_consider[:5], 4)] or ["추가 고려할 챔피언이 없습니다."])
    embed.add_field(name="영향도 안내", value="추천 밴은 각 밴을 제외했을 때의 차이입니다. "
                    "추가 고려는 추천 밴 3개에 각각 하나를 더했을 때의 효과이며, 합산하지 않습니다.", inline=False)
    _add_blocks(embed, "주의사항", [f"⚠ {message}" for message in _warnings(result, None)])
    return embed


class ScoutingReportView(discord.ui.View):
    def __init__(self, result: Recommendation, presentations: dict[int, PlayerPresentation],
                 *, owner_id: int, timeout: float = 600):
        super().__init__(timeout=timeout)
        self.result = result
        self.presentations = presentations
        self.owner_id = owner_id
        self.page_index = 0
        self.message: discord.Message | discord.InteractionMessage | None = None
        self._page_lock = asyncio.Lock()
        by_role = {player.role: player for player in result.players}
        self.players = tuple(by_role[role] for role in ROLE_ORDER)
        self.pages = [render_player_page(result, player, presentations.get(player.player_id, PlayerPresentation()), index)
                      for index, player in enumerate(self.players)]
        self.pages.append(render_summary_page(result))
        self._profile_button: discord.ui.Button | None = None
        self._sync_buttons()

    @property
    def current_embed(self) -> discord.Embed:
        return self.pages[self.page_index]

    def _sync_buttons(self) -> None:
        self.previous_page.disabled = self.page_index == 0
        self.next_page.disabled = self.page_index == 5
        if self._profile_button is not None:
            self.remove_item(self._profile_button)
            self._profile_button = None
        if self.page_index < 5:
            player = self.players[self.page_index]
            presentation = self.presentations.get(player.player_id, PlayerPresentation())
            if presentation.opgg_url:
                self._profile_button = discord.ui.Button(label="OP.GG 보기", url=presentation.opgg_url, row=0)
                self.add_item(self._profile_button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message("페이지 이동은 이 리포트를 요청한 사용자만 할 수 있습니다.", ephemeral=True)
        return False

    async def _move(self, interaction: discord.Interaction, offset: int) -> None:
        async with self._page_lock:
            self.page_index = max(0, min(5, self.page_index + offset))
            self._sync_buttons()
            await interaction.response.edit_message(content=None, embed=self.current_embed,
                                                    view=self, allowed_mentions=discord.AllowedMentions.none())
            if interaction.message is not None:
                # Component messages edit with bot authentication, avoiding the
                # original slash interaction's 15-minute token lifetime.
                self.message = interaction.message

    @discord.ui.button(label="이전", style=discord.ButtonStyle.secondary, row=0)
    async def previous_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._move(interaction, -1)

    @discord.ui.button(label="다음", style=discord.ButtonStyle.primary, row=0)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._move(interaction, 1)

    async def on_timeout(self) -> None:
        async with self._page_lock:
            self.previous_page.disabled = True
            self.next_page.disabled = True
            if self.message is not None:
                try:
                    message = self.message
                    if isinstance(message, discord.InteractionMessage):
                        # An expensive collection may leave the original
                        # webhook token expired even before this View times out.
                        # A channel partial message edits with the bot token.
                        message = discord.PartialMessage(channel=message.channel, id=message.id)
                    await message.edit(view=self)
                except discord.HTTPException:
                    # A deleted message or expired original interaction must
                    # not turn successful collection into a command failure.
                    logger.info("Could not disable expired scouting report controls", exc_info=True)
