import discord
import asyncio


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