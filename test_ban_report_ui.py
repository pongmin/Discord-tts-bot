"""Offline renderer and interaction tests; no Riot calls or Discord login."""

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import discord

from ban_algorithm import (
    BanImpact, ChampionModel, PlayerDiagnostic, PlayerModel, Recommendation,
    SearchResult,
)
from ban_report_ui import (
    PlayerPresentation, ScoutingReportView, render_player_page,
    render_summary_page, stability_label,
)


ROLES = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY")
VISIBLE_ROLES = ("TOP", "JUNGLE", "MID", "BOTTOM", "SUPPORT")
NAMES = {1: "잭스", 2: "케인", 3: "진", 4: "룰루", 5: "아리", 6: "가렌", 7: "애쉬", 8: "레오나"}


def champion(cid, share, threat=1.0, *, name=None, games=10, wins=6):
    return ChampionModel(cid, name or NAMES.get(cid, f"챔피언{cid}"), games, wins,
                         0.987654321, 0.123456789, share, 0.625, threat)


def recommendation():
    pools = ((1, 5, 6), (1, 2, 6), (1, 5, 8), (3, 7, 8), (4, 7, 8))
    players = tuple(
        PlayerModel(100 + index, f"Player{index}#KR1", role,
                    {cid: champion(cid, share) for cid, share in zip(pool, (0.6, 0.3, 0.1))},
                    0.6, 4.0)
        for index, (role, pool) in enumerate(zip(ROLES, pools))
    )

    def impact(cid, marginal):
        return BanImpact(cid, NAMES[cid],
                         tuple(f"{p.label}/{p.role}" for p in players if cid in p.champions),
                         marginal)

    return Recommendation(
        players, (0.2,) * 5, tuple(NAMES),
        {rho: SearchResult((1, 2, 3), 0.842, 56, 0, ()) for rho in (1, 0, -1)},
        (impact(1, 0.052), impact(2, 0.032), impact(3, 0.012)),
        tuple(impact(cid, 0.009 - cid / 10000) for cid in range(4, 9)),
        tuple(PlayerDiagnostic(p.player_id, p.label, p.role, 1.0, 1.0, False) for p in players),
        (),
    )


def presentation(index=0):
    return PlayerPresentation(
        tier="GOLD", division="II", lp=73,
        profile_icon_url=f"https://ddragon.leagueoflegends.com/cdn/15.1.1/img/profileicon/{index + 1}.png",
        opgg_url=f"https://www.op.gg/summoners/kr/Player{index}-KR1",
    )


def embed_text(embed):
    pieces = [embed.title or "", embed.description or ""]
    for field in embed.fields:
        pieces.extend((field.name, field.value))
    pieces.extend((embed.footer.text or "", embed.author.name or ""))
    return "\n".join(pieces)


def utf16_length(value):
    return len(value.encode("utf-16-le")) // 2


def button(view, label):
    return next(child for child in view.children if getattr(child, "label", None) == label)


class ReportRendererTests(unittest.TestCase):
    def setUp(self):
        self.result = recommendation()

    def assert_embed_limits(self, embed):
        self.assertLessEqual(utf16_length(embed.title or ""), 256)
        self.assertLessEqual(utf16_length(embed.description or ""), 4096)
        self.assertLessEqual(utf16_length(embed.footer.text or ""), 2048)
        self.assertLessEqual(len(embed.fields), 25)
        for field in embed.fields:
            self.assertLessEqual(utf16_length(field.name), 256)
            self.assertLessEqual(utf16_length(field.value), 1024)
        # Newlines introduced by this test are not part of the actual embed.
        payload = [embed.title or "", embed.description or "", embed.footer.text or "", embed.author.name or ""]
        payload.extend(text for field in embed.fields for text in (field.name, field.value))
        self.assertLessEqual(sum(map(utf16_length, payload)), 6000)

    def test_player_details_use_actual_role_games_and_pick_order(self):
        player = replace(self.result.players[0], champions={
            6: champion(6, 0.1, 2.0, games=5, wins=1),
            5: champion(5, 0.3, 1.0, games=15, wins=10),
            1: champion(1, 0.6, 0.1, games=30, wins=19),
        })
        embed = render_player_page(self.result, player, presentation(), 0)
        text = embed_text(embed)
        self.assertIn(player.label, text)
        self.assertIn("골드", text)
        self.assertRegex(text, r"골드\s+(?:II|2)")
        self.assertIn("73", text)
        self.assertRegex(text, r"50\s*경기")
        self.assertRegex(text, r"60(?:\.0)?%")
        # Candidate scores favor 가렌, 아리, 잭스; the UI must sort by p_final.
        self.assertLess(text.index("잭스"), text.index("아리"))
        self.assertLess(text.index("아리"), text.index("가렌"))
        self.assertIn("62.5%", text)
        self.assertIn("위험도", text)
        self.assertIn("주력 집중도", text)
        self.assertEqual(embed.thumbnail.url, presentation().profile_icon_url)
        for internal in ("Threat", "threat", "P_personal", "P_meta", "P_final", "candidate_score", "champion_id", "player_id", "0.987654321"):
            self.assertNotIn(internal, text)
        self.assert_embed_limits(embed)

    def test_danger_boundaries(self):
        for threat, expected in ((0.899999, "▼ 낮음"), (0.9, "보통"), (1.099999, "보통"), (1.1, "▲ 높음")):
            with self.subTest(threat=threat):
                player = replace(self.result.players[0], champions={1: champion(1, 1.0, threat)})
                text = embed_text(render_player_page(self.result, player, presentation(), 0))
                self.assertIn(expected, text)
                for other in {"▼ 낮음", "보통", "▲ 높음"} - {expected}:
                    self.assertNotIn(other, text)

    def test_concentration_uses_full_pool_before_display_truncation(self):
        shares = [0.4, 0.3, 0.1] + [0.002] * 100
        player = replace(self.result.players[0], champions={
            cid: champion(cid, share, name=f"챔피언{cid:03}")
            for cid, share in enumerate(shares, 1)
        })
        embed = render_player_page(self.result, player, presentation(), 0)
        text = embed_text(embed)
        self.assertRegex(text, r"40(?:\.0)?%")
        self.assertRegex(text, r"80(?:\.0)?%")
        self.assertIn("챔피언001", text)
        self.assertRegex(text, r"외\s*\d+개")
        self.assert_embed_limits(embed)

    def test_summary_reuses_order_percentages_and_hides_debug_data(self):
        before = deepcopy(self.result)
        embed = render_summary_page(self.result)
        text = embed_text(embed)
        self.assertLess(text.index("잭스"), text.index("케인"))
        self.assertLess(text.index("케인"), text.index("진"))
        self.assertIn("5.2%", text)
        self.assertIn("3.2%", text)
        self.assertIn("15.8%", text)
        self.assertIn("TOP · JUNGLE · MID", text)
        self.assertIn("추가 고려", text)
        for name in (NAMES[cid] for cid in range(4, 9)):
            self.assertIn(name, text)
        self.assertIn("추천 안정성", text)
        self.assertIn("왜 이 밴인가?", text)
        self.assertIn("3개 포지션", text)
        self.assertFalse(embed.thumbnail.url)
        for internal in ("B*", "rho", "V(B)", "[1]", "MIDDLE", "UTILITY", "marginal", "0.842", "0.052"):
            self.assertNotIn(internal, text)
        self.assertEqual(self.result, before)
        self.assert_embed_limits(embed)

    def test_stability_uses_all_three_set_intersections(self):
        for sets, expected in (
            (((1, 2, 3), (3, 2, 1), (2, 1, 3)), "높음"),
            (((1, 2, 3), (1, 2, 4), (1, 2, 5)), "보통"),
            (((1, 2, 3), (1, 2, 4), (1, 3, 4)), "낮음"),
        ):
            with self.subTest(sets=sets):
                result = replace(self.result, best_by_rho={
                    rho: replace(self.result.best_by_rho[rho], bans=bans)
                    for rho, bans in zip((1, 0, -1), sets)
                })
                self.assertEqual(stability_label(result), expected)

    def test_all_existing_warning_types_are_visible_in_korean(self):
        prefix = "Player0#KR1/TOP"
        warnings = (
            f"{prefix}: no meta observations in the seen pool; using personal probabilities.",
            "Player0#KR1: no recognized solo-queue tier at cutoff; placeholder rank weight = 1.",
            "An optimum exhausts an observed role champion pool: v1 assumes S=0 and r=0. With no unseen/Others bucket, this optimistic assumption can show 100% threat reduction under rho=0 or rho=-1.",
            "Negative marginal contribution for 잭스 (-0.000321): Threat/redistribution may redirect picks onto more threatening champions.",
            "An also-consider set exhausts an observed pool; its marginal value includes the optimistic v1 S=0 assumption (unseen/Others picks are not modeled).",
            "Recommendation is sensitive to concentration assumption — treat with caution.",
            f"{prefix}: only 2 role-filtered games available — recommendation may be less reliable. Consider a deeper --depth if more history exists.",
        )
        baseline = embed_text(render_summary_page(self.result)) + embed_text(
            render_player_page(self.result, self.result.players[0], presentation(), 0)
        )
        for warning in warnings:
            with self.subTest(warning=warning):
                result = replace(self.result, warnings=(warning,))
                embed = render_summary_page(result)
                player_embed = render_player_page(result, result.players[0], presentation(), 0)
                text = embed_text(embed) + embed_text(player_embed)
                self.assertNotEqual(text, baseline)
                self.assertIn("⚠", text)
                for english in ("no meta observations", "placeholder rank weight", "An optimum", "Negative marginal", "An also-consider", "Recommendation is sensitive", "role-filtered", "rho=", "S=0", "0.000321"):
                    self.assertNotIn(english, text)
                self.assert_embed_limits(embed)
                self.assert_embed_limits(player_embed)

    def test_exhaustion_diagnostics_are_shown_without_warning_strings(self):
        diagnostic = replace(self.result.player_diagnostics[0], observed_pool_exhausted=True)
        cases = (
            replace(self.result, player_diagnostics=(diagnostic,) + self.result.player_diagnostics[1:]),
            replace(self.result, also_consider=(replace(self.result.also_consider[0], exhausted_player_ids=(100,)),) + self.result.also_consider[1:]),
            replace(self.result, best_by_rho=self.result.best_by_rho | {
                1: replace(self.result.best_by_rho[1], exhausted_player_ids=(100,)),
            }),
        )
        for source, result in zip(("player diagnostic", "alternative ban", "comparison search"), cases):
            with self.subTest(source=source):
                text = embed_text(render_summary_page(result))
                self.assertIn("관측", text)
                self.assertIn("미관측 챔피언", text)
                self.assertIn("실제 효과", text)
                self.assertIn("⚠", text)

    def test_missing_profile_and_rank_metadata_is_readable(self):
        embed = render_player_page(self.result, self.result.players[0], PlayerPresentation(), 0)
        text = embed_text(embed)
        self.assertIn(self.result.players[0].label, text)
        self.assertTrue(any(label in text for label in ("정보 없음", "확인되지", "미배치", "미확인", "언랭크")))
        self.assertFalse(embed.thumbnail.url)
        self.assert_embed_limits(embed)

    def test_large_pools_and_astral_names_stay_within_discord_limits(self):
        champions = {
            cid: champion(cid, 1 / 250, name=f"챔피언{cid:03}" + "😀" * 110)
            for cid in range(1, 251)
        }
        player = replace(self.result.players[0], label="선수😀" * 100, champions=champions)
        result = replace(self.result, players=(player,) + self.result.players[1:])
        embed = render_player_page(result, player, presentation(), 0)
        self.assert_embed_limits(embed)
        self.assertRegex(embed_text(embed), r"외\s*\d+개")
        long_bans = tuple(replace(ban, name=ban.name + "😀" * 200) for ban in result.recommended)
        alternatives = tuple(replace(ban, name=ban.name + "😀" * 200) for ban in result.also_consider)
        self.assert_embed_limits(render_summary_page(replace(result, recommended=long_bans, also_consider=alternatives)))


class ScoutingReportViewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.result = recommendation()
        self.presentations = {p.player_id: presentation(i) for i, p in enumerate(self.result.players)}
        # Ordering must come from roles even if the caller's tuple is reordered.
        self.view = ScoutingReportView(replace(self.result, players=tuple(reversed(self.result.players))),
                                       self.presentations, owner_id=123, timeout=600)
        self.addCleanup(self.view.stop)

    def interaction(self, user_id=123):
        return SimpleNamespace(user=SimpleNamespace(id=user_id), message=None,
                               response=SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock()))

    async def test_six_pages_edit_same_message_and_change_profile_links(self):
        self.assertEqual(self.view.page_index, 0)
        self.assertTrue(button(self.view, "이전").disabled)
        interaction = self.interaction()
        for index, role in enumerate(VISIBLE_ROLES):
            text = embed_text(self.view.current_embed)
            self.assertIn(role, text)
            self.assertIn(self.result.players[index].label, text)
            self.assertEqual(button(self.view, "OP.GG 보기").url, presentation(index).opgg_url)
            self.assertEqual(self.view.current_embed.thumbnail.url, presentation(index).profile_icon_url)
            await button(self.view, "다음").callback(interaction)
            self.assertEqual(self.view.page_index, index + 1)
            self.assertIs(interaction.response.edit_message.await_args.kwargs["view"], self.view)
            self.assertEqual(interaction.response.edit_message.await_args.kwargs["embed"].to_dict(), self.view.current_embed.to_dict())
        self.assertIn("종합", embed_text(self.view.current_embed))
        self.assertFalse(self.view.current_embed.thumbnail.url)
        self.assertFalse(any(getattr(child, "url", None) for child in self.view.children))
        self.assertTrue(button(self.view, "다음").disabled)
        self.assertEqual(interaction.response.edit_message.await_count, 5)
        await button(self.view, "이전").callback(interaction)
        self.assertEqual(self.view.page_index, 4)
        self.assertEqual(button(self.view, "OP.GG 보기").url, presentation(4).opgg_url)

    async def test_other_users_cannot_turn_pages(self):
        interaction = self.interaction(user_id=999)
        self.assertFalse(await self.view.interaction_check(interaction))
        self.assertEqual(self.view.page_index, 0)
        interaction.response.send_message.assert_awaited_once()
        self.assertTrue(interaction.response.send_message.await_args.kwargs["ephemeral"])
        self.assertTrue(await self.view.interaction_check(self.interaction()))

    async def test_stale_boundary_clicks_cannot_leave_the_six_pages(self):
        interaction = self.interaction()
        await button(self.view, "이전").callback(interaction)
        self.assertEqual(self.view.page_index, 0)
        for _ in range(6):
            await button(self.view, "다음").callback(interaction)
        self.assertEqual(self.view.page_index, 5)
        for _ in range(6):
            await button(self.view, "이전").callback(interaction)
        self.assertEqual(self.view.page_index, 0)

    async def test_timeout_disables_navigation_and_handles_missing_message(self):
        self.view.message = SimpleNamespace(edit=AsyncMock())
        await self.view.on_timeout()
        self.assertTrue(button(self.view, "이전").disabled)
        self.assertTrue(button(self.view, "다음").disabled)
        self.view.message.edit.assert_awaited_once()
        no_message = ScoutingReportView(self.result, {}, owner_id=123)
        self.addCleanup(no_message.stop)
        await no_message.on_timeout()
        self.assertTrue(button(no_message, "이전").disabled)
        self.assertTrue(button(no_message, "다음").disabled)

    async def test_timeout_handles_deleted_discord_message(self):
        response = SimpleNamespace(status=404, reason="Not Found")
        self.view.message = SimpleNamespace(edit=AsyncMock(side_effect=discord.NotFound(response, {"message": "Unknown Message", "code": 10008})))
        await self.view.on_timeout()
        self.assertTrue(button(self.view, "다음").disabled)


if __name__ == "__main__":
    unittest.main()
