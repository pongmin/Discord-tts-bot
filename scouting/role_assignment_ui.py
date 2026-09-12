"""Korean Discord presentation for /assignroles.

Formats an existing RoleAssignmentResult and nothing else: no collection, no DB
reads, no scoring. The diagnostic matrix is deliberately a second embed rather
than a page of its own - the recommendation is the answer, the 5x5 grid of
E/F/R/C is the evidence, and both should be visible without a click.
"""

import discord

from scouting.role_assignment import ROLE_ORDER, RoleAssignmentResult, RoleFit


ROLE_LABELS = {"TOP": "TOP", "JUNGLE": "JUNGLE", "MIDDLE": "MID",
               "BOTTOM": "BOTTOM", "UTILITY": "SUPPORT"}
ROLE_WIDTH = max(len(label) for label in ROLE_LABELS.values())
EMBED_COLOR = 0x3BA55D
DIAGNOSTIC_COLOR = 0x4F545C
# Below this the best and second-best assignments are close enough that the
# choice between them is not really supported by the data.
CLOSE_CALL_MARGIN = 0.10
FIELD_LIMIT = 1024


def _number(value: float) -> str:
    """Keep tiny fits readable instead of rendering every one of them as 0.000."""
    if value and abs(value) < 0.001:
        return f"{value:.1e}"
    return f"{value:.3f}"


def _assignment_lines(result: RoleAssignmentResult, fits: tuple[RoleFit, ...],
                      compare: tuple[RoleFit, ...] | None = None) -> str:
    other = {fit.role: fit.player_id for fit in compare} if compare else {}
    lines = []
    for fit in fits:
        # ↔ marks the roles where this assignment differs from the other one,
        # which is the whole reason the runner-up is worth showing.
        moved = "↔ " if other and other.get(fit.role) != fit.player_id else ""
        unplayed = " · 무기록" if not fit.observed else ""
        lines.append(
            f"{moved}**{ROLE_LABELS[fit.role]}** — {fit.label} "
            f"(E {_number(fit.fit)} · {fit.games}경기{unplayed})"
        )
    return "\n".join(lines)[:FIELD_LIMIT]


def _score_text(result: RoleAssignmentResult) -> str:
    lines = [
        f"최적 배치 점수 **{result.best.score:.3f}**",
        f"차선 배치 점수 {result.runner_up.score:.3f}",
        f"점수 차이 **{result.margin:.3f}**",
    ]
    if result.margin < CLOSE_CALL_MARGIN:
        lines.append("두 배치의 점수가 거의 같습니다. 취향대로 골라도 무방합니다.")
    else:
        lines.append("점수는 각 자리 적합도 E의 로그 합이며, 클수록 좋습니다.")
    return "\n".join(lines)[:FIELD_LIMIT]


def _notes(result: RoleAssignmentResult) -> str:
    notes = []
    unplayed = [fit for fit in result.best.fits if not fit.observed]
    for fit in unplayed:
        notes.append(
            f"{fit.label}: 수집된 기록에 {ROLE_LABELS[fit.role]} 경기가 없어 "
            "중립 추정값으로 계산했습니다."
        )
    thin = [fit for fit in result.best.fits if fit.observed and fit.games < 10]
    for fit in thin:
        notes.append(
            f"{fit.label}: {ROLE_LABELS[fit.role]} 기록이 {fit.games}경기뿐이라 "
            "신뢰도가 낮습니다."
        )
    return "\n".join(notes)[:FIELD_LIMIT]


def render_assignment_embed(result: RoleAssignmentResult) -> discord.Embed:
    embed = discord.Embed(
        title="🧭 역할 배치 추천",
        description="수집된 솔로 랭크·일반 드래프트 기록만으로 계산한 5인 포지션 배치입니다.",
        color=EMBED_COLOR,
    )
    embed.add_field(
        name="추천 배치", value=_assignment_lines(result, result.best.fits), inline=False
    )
    embed.add_field(name="점수", value=_score_text(result), inline=False)
    embed.add_field(
        name="차선 배치",
        value=_assignment_lines(result, result.runner_up.fits, result.best.fits),
        inline=False,
    )
    notes = _notes(result)
    if notes:
        embed.add_field(name="참고", value=notes, inline=False)
    return embed


def render_matrix_embed(result: RoleAssignmentResult) -> discord.Embed:
    """The full 5x5 diagnostic: every player against every role.

    E = F * R * C is shown with its three factors and the raw game count, so a
    surprising assignment can be traced to whichever factor drove it.
    """
    embed = discord.Embed(
        title="🧪 진단 · 5×5 역할 적합도",
        description=("E = F × R × C × B^0.4 · F 해당 포지션 비중 · R 포지션 승률 보정 · "
                     "C 관측 챔피언 풀 강도 · B 챔피언 폭 · N 유효 챔피언 수\n"
                     "✅ 는 추천 배치에서 실제로 맡는 자리입니다."),
        color=DIAGNOSTIC_COLOR,
    )
    assigned = {fit.player_id: fit.role for fit in result.best.fits}
    for player_id in result.player_ids:
        lines = []
        for role in ROLE_ORDER:
            fit = result.matrix[(player_id, role)]
            mark = "✅" if assigned[player_id] == role else "  "
            lines.append(
                f"{mark} {ROLE_LABELS[role]:<{ROLE_WIDTH}} "
                f"E {_number(fit.fit):>7} F {_number(fit.share):>7} "
                f"R {_number(fit.winrate_ratio):>7} C {_number(fit.strength):>7} "
                f"B {_number(fit.breadth):>7} N {fit.effective_pool:>5.2f} "
                f"{fit.games:>4}경기"
            )
        body = "\n".join(lines)
        embed.add_field(
            name=f"{result.labels[player_id]} · 전체 승률 {result.baselines[player_id]:.1%}"[:256],
            value=f"```\n{body}\n```"[:FIELD_LIMIT],
            inline=False,
        )
    return embed
