import discord
import asyncio
from discord import app_commands
import random
from tts_voice import USER_TTS_SETTINGS, save_user_tts_settings

from champion_recommend import (
    pick_random_champion,
    get_champion_image_url,
    LANE_DISPLAY,
    DAMAGE_DISPLAY
)

def setup_commands(bot, tts_channels, save_tts_channels, tts_queues):

    @bot.tree.command(
        name="ping",
        description="봇 응답 테스트"
    )
    async def ping(interaction: discord.Interaction):
        await interaction.response.send_message("pong")


    @bot.tree.command(
        name="setchannel",
        description="현재 채널을 TTS 입력 채널로 설정합니다."
    )
    async def setchannel(interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message(
                "서버에서만 사용할 수 있음",
                ephemeral=True
            )
            return

        tts_channels[interaction.guild.id] = interaction.channel.id
        save_tts_channels()

        await interaction.response.send_message(
            f"이제 {interaction.channel.mention} 채널의 메시지를 TTS 입력으로 사용합니다."
        )


    @bot.tree.command(
        name="join",
        description="봇이 음성 채널에 참여합니다."
    )
    async def join(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        try:
            if interaction.guild is None:
                await interaction.followup.send("서버에서만 사용할 수 있음")
                return

            if not isinstance(interaction.user, discord.Member):
                await interaction.followup.send("서버 멤버 정보 확인 실패")
                return

            if interaction.user.voice is None:
                await interaction.followup.send("먼저 음성 채널에 들어가야 함")
                return

            channel = interaction.user.voice.channel
            voice_client = interaction.guild.voice_client

            if voice_client is None:
                await channel.connect()
                await interaction.followup.send(f"{channel.name}에 참여함")

            elif voice_client.channel != channel:
                await voice_client.move_to(channel)
                await interaction.followup.send(f"{channel.name}로 이동함")

            else:
                await interaction.followup.send("이미 그 채널에 있음")

        except Exception as e:
            print("JOIN ERROR:", repr(e))
            await interaction.followup.send(f"에러 발생: `{type(e).__name__}`")


    @bot.tree.command(
        name="leave",
        description="봇이 음성 채널에서 나갑니다."
    )
    async def leave(interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message(
                "서버에서만 사용할 수 있음",
                ephemeral=True
            )
            return

        voice_client = interaction.guild.voice_client

        if voice_client is not None:
            await voice_client.disconnect()
            await interaction.response.send_message("음성 채널에서 나감")

        else:
            await interaction.response.send_message(
                "이미 음성 채널에 없음",
                ephemeral=True
            )


    @bot.tree.command(
        name="skip",
        description="현재 재생 중인 TTS를 건너뜁니다."
    )
    async def skip(interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message(
                "서버에서만 사용할 수 있음",
                ephemeral=True
            )
            return

        voice_client = interaction.guild.voice_client

        if voice_client is not None and voice_client.is_playing():
            voice_client.stop()
            await interaction.response.send_message("스킵함", ephemeral=True)

        else:
            await interaction.response.send_message(
                "재생 중인 TTS가 없음",
                ephemeral=True
            )

    @bot.tree.command(
        name="voice",
        description="쓰지마세요 테스트중임 바꾸면 소리 안나옴"
    )
    async def voice(interaction: discord.Interaction, mode: str):

        user_id = interaction.user.id

        if mode == "기본":
            USER_TTS_SETTINGS[user_id] = {"engine": "gtts"}
            save_user_tts_settings()
            await interaction.response.send_message("gTTS로 설정됨")

        elif mode == "테스트":
            USER_TTS_SETTINGS[user_id] = {"engine": "se", "voice": "Kim"}
            save_user_tts_settings()
            await interaction.response.send_message("StreamElements 목소리로 설정됨")

        else:
            await interaction.response.send_message("옵션: 기본 / 테스트")

    @bot.tree.command(
        name="clearqueue",
        description="대기 중인 TTS 큐를 비웁니다."
    )
    async def clearqueue(interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message(
                "서버에서만 사용할 수 있음",
                ephemeral=True
            )
            return

        guild_id = interaction.guild.id
        cleared = 0

        if guild_id in tts_queues:
            while not tts_queues[guild_id].empty():
                try:
                    tts_queues[guild_id].get_nowait()
                    cleared += 1

                except asyncio.QueueEmpty:
                    break

        await interaction.response.send_message(
            f"큐 {cleared}개 비움",
            ephemeral=True
        )
    @bot.tree.command(
        name="추천",
        description="라인과 AD/AP 조건에 맞는 롤 챔피언을 랜덤 추천합니다."
    )
    
    @app_commands.describe(
        라인="추천받을 라인",
        딜타입="AD 또는 AP 선택. 비워두면 전체에서 랜덤 추천"
    )
    
    @app_commands.choices(
        라인=[
            app_commands.Choice(name="탑", value="top"),
            app_commands.Choice(name="정글", value="jungle"),
            app_commands.Choice(name="미드", value="mid"),
            app_commands.Choice(name="원딜", value="adc"),
            app_commands.Choice(name="서폿", value="support")
        ],
        딜타입=[
            app_commands.Choice(name="AD", value="ad"),
            app_commands.Choice(name="AP", value="ap")
        ]
    )
    async def recommend_champion(
        interaction: discord.Interaction,
        라인: app_commands.Choice[str],
        딜타입: app_commands.Choice[str] | None = None
    ):
        lane = 라인.value
        damage_type = 딜타입.value if 딜타입 is not None else None

        try:
            champion, picked_damage_type = pick_random_champion(lane, damage_type)

            lane_name = LANE_DISPLAY[lane]
            damage_name = DAMAGE_DISPLAY[picked_damage_type]
            image_url = await get_champion_image_url(champion)

            embed = discord.Embed(
                title="롤 챔피언 추천",
                description=f"**{lane_name} {damage_name} 추천 챔피언: {champion}**",
                color=0x5865F2
            )

            if image_url:
                embed.set_thumbnail(url=image_url)

            await interaction.response.send_message(embed=embed)

        except Exception as e:
            await interaction.response.send_message(
                f"챔피언 추천 중 오류가 났어: `{type(e).__name__}: {e}`",
                ephemeral=True
            )
    @bot.tree.context_menu(name="이모티콘 폭격")
    async def emoji_bomb(interaction: discord.Interaction, message: discord.Message):
        emojis = [
            "<:Do_The_DIH:1475889811591135292>",
            "<:IMG_2178:1279441447292112928>",
            "<:IMG_2316:1310910241688256594>",
            "<:729d1d80595a4cc50416be1e20008c55:1380475191007907920>",
            "<:IMG_1931:1225097138736726058>",
            "<:IMG_2317:1310910517841498133>",
            "<:emoji_19:1499620472516509807>",
            "<:GEOBUGI:1475890659952169103>",
            "<:emoji_34:1375121956952608830>",
            "<:IMG_2379:1332006874421395526>",
            "🥀","💔","❓","🧑‍🦽","👎","🤬","🤡"
        ]

        count = min(10, len(emojis))
        selected_emojis = random.sample(emojis, k=count)

        await interaction.response.defer(ephemeral=True)

        success = 0
        failed = 0

        for emoji in selected_emojis:
            try:
                await message.add_reaction(emoji)
                success += 1
                await asyncio.sleep(0.02)

            except Exception as e:
                print("REACTION ERROR:", repr(e))
                failed += 1

        await interaction.followup.send(
            f"이모티콘 {success}개 달았음" + (f" / 실패 {failed}개" if failed else ""),
            ephemeral=True
        )