"""Korean Discord presentation for /assignroles.

Formats an existing RoleAssignmentResult and nothing else: no collection, no DB
reads, no scoring. The 5x5 view is deliberately a second embed rather than a
page of its own - the recommendation is the answer, each player's fit for the
four roles they did NOT get is the evidence, and both should be visible without
a click.

Everything here is a percentage. Raw E and its factors are developer numbers and
live in `format_assignment` only.
"""

import discord

from scouting.role_assignment import (
    ROLE_ORDER, Assignment, RoleAssignmentResult, personal_fit_percent,
    team_fit_percent,
)


ROLE_LABELS = {"TOP": "TOP", "JUNGLE": "JUNGLE", "MIDDLE": "MID",
               "BOTTOM": "BOTTOM", "UTILITY": "SUPPORT"}
ROLE_WIDTH = max(len(label) for label in ROLE_LABELS.values())
EMBED_COLOR = 0x3BA55D
DIAGNOSTIC_COLOR = 0x4F545C
# Within this many points of team fit, the best and second-best assignments are
# close enough that the choice between them is not really supported by the data.
# A percentage rather than a raw score gap: Score is a sum of W * E, whose scale
# moves with the group's rank, so no fixed gap in score units means one thing.
CLOSE_CALL_PERCENT = 1.0
FIELD_LIMIT = 1024


def _personal_percent(percent: float) -> str:
    """Whole percent, except where rounding would read as a flat zero."""
    if percent and percent < 0.5:
        return "<1%"
    return f"{percent:.0f}%"


def _team_percent(percent: float) -> str:
    """One decimal: the gap between two good assignments is often under 1pp."""
    if percent and percent < 0.05:
        return "<0.1%"
    return f"{percent:.1f}%"


def _assignment_lines(result: RoleAssignmentResult, assignment: Assignment,
                      compare: Assignment | None = None) -> str:
    """One line per seat, with the player's own fit for it as a percentage.

    개인 적합도 is E normalized against that player's best role, so 100% means
    "this is their main" and 44% means "this is a real step down for them" -
    raw E is kept for the details matrix, where the factors behind it are also
    visible.
    """
    other = {fit.role: fit.player_id for fit in compare.fits} if compare else {}
    lines = [f"팀 적합도 **{_team_percent(team_fit_percent(result, assignment))}**"]
    for fit in assignment.fits:
        # ↔ marks the roles where this assignment differs from the other one,
        # which is the whole reason the runner-up is worth showing.
        moved = "↔ " if other and other.get(fit.role) != fit.player_id else ""
        unplayed = " · 무기록" if not fit.observed else ""
        lines.append(
            f"{moved}**{ROLE_LABELS[fit.role]}** — {fit.label} "
            f"· 개인 적합도 {_personal_percent(personal_fit_percent(result, fit))} "
            f"({fit.games}경기{unplayed})"
        )
    return "\n".join(lines)[:FIELD_LIMIT]


def _score_text(result: RoleAssignmentResult) -> str:
    """The two assignments as team fit, best pinned at 100% by construction."""
    runner_up = team_fit_percent(result, result.runner_up)
    lines = [
        f"최적 배치 팀 적합도 **{_team_percent(team_fit_percent(result, result.best))}**",
        f"차선 배치 팀 적합도 {_team_percent(runner_up)}",
        f"적합도 차이 **{_team_percent(100.0 - runner_up)}p**",
    ]
    if 100.0 - runner_up < CLOSE_CALL_PERCENT:
        lines.append("두 배치의 적합도가 거의 같습니다. 취향대로 골라도 무방합니다.")
    else:
        lines.append("팀 적합도는 배치 전체 점수를 최적 배치 대비 비율로 나타낸 값입니다.")
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
        name="추천 배치", value=_assignment_lines(result, result.best), inline=False
    )
    embed.add_field(name="적합도", value=_score_text(result), inline=False)
    embed.add_field(
        name="차선 배치",
        value=_assignment_lines(result, result.runner_up, result.best),
        inline=False,
    )
    notes = _notes(result)
    if notes:
        embed.add_field(name="참고", value=notes, inline=False)
    return embed


def render_matrix_embed(result: RoleAssignmentResult) -> discord.Embed:
    """The full 5x5 view: every player against every role, as personal fit %.

    Deliberately percentages, plus the one number personal fit cannot carry:
    W, the player's own strength, which is what lets the assignment prefer
    putting the stronger player where the fit is worth more. The factors behind
    E - F, R, C, B, N_eff and the raw game counts - stay in
    `format_assignment`, the plain-text developer dump, because they answer a
    model-debugging question rather than the "could this player have gone
    somewhere else" question this embed is for.
    """
    embed = discord.Embed(
        title="🧪 선수별 포지션 적합도",
        description=("각 선수의 개인 적합도입니다. 본인의 최적 포지션이 100%이고, "
                     "나머지는 그에 대한 비율입니다.\n"
                     "실력은 랭크·최근 폼·KDA를 합친 10점 만점 종합 점수이며, "
                     "배치 계산에는 쓰이지 않습니다.\n"
                     "✅ 는 추천 배치에서 실제로 맡는 자리입니다."),
        color=DIAGNOSTIC_COLOR,
    )
    assigned = {fit.player_id: fit.role for fit in result.best.fits}
    for player_id in result.player_ids:
        # display_skill is a 0-100 composite; one digit either side of the
        # point reads as a rating, which is what was asked for, and 100 is
        # the only value that needs three characters.
        lines = [f"실력: {result.strengths[player_id].display_skill / 10:.1f}"]
        for role in ROLE_ORDER:
            fit = result.matrix[(player_id, role)]
            mark = "✅" if assigned[player_id] == role else "  "
            percent = _personal_percent(personal_fit_percent(result, fit))
            lines.append(
                f"{mark} {ROLE_LABELS[role]:<{ROLE_WIDTH}} {percent:>5}"
            )
        body = "\n".join(lines)
        embed.add_field(
            name=f"{result.labels[player_id]} · 전체 승률 {result.baselines[player_id]:.1%}"[:256],
            value=f"```\n{body}\n```"[:FIELD_LIMIT],
            inline=False,
        )
    return embed
