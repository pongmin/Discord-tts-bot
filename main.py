import discord
from discord.ext import commands
import logging
from dotenv import load_dotenv
import os
import json
import random
import time
import asyncio

from tts_voice import add_tts_queue, add_bot_tts_queue, tts_queues
from discord_commands import setup_commands


# =========================
# 기본 설정
# =========================

TTS_CHANNELS_FILE = "tts_channels.json"

GUILD_IDS = [
    1499995640288116838,
    1207668437497806928,
]

KEYWORD_REACTIONS = [
    {
        "keyword": "?",
        "responses": ["?"],
        "prob": 0.2
    },
    {
        "keyword": "볼때마다",
        "responses": ["죽어있네"],
        "prob": 0.2
    },
    {
        "keyword": ";;",
        "responses": [";;"],
        "prob": 0.2
    },
    {
        "keyword": "안",
        "responses": ["물어봄"],
        "prob": 0.2
    }
]

reaction_last_used = {}

MY_GUILDS = [discord.Object(id=g) for g in GUILD_IDS]


# =========================
# TTS 채널 저장 / 불러오기
# =========================

def load_tts_channels():
    if not os.path.exists(TTS_CHANNELS_FILE):
        return {}

    try:
        with open(TTS_CHANNELS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        return {
            int(guild_id): int(channel_id)
            for guild_id, channel_id in data.items()
        }

    except Exception as e:
        print("TTS 채널 파일 로드 실패:", repr(e))
        return {}


def save_tts_channels():
    try:
        with open(TTS_CHANNELS_FILE, "w", encoding="utf-8") as f:
            json.dump(tts_channels, f, ensure_ascii=False, indent=4)

    except Exception as e:
        print("TTS 채널 파일 저장 실패:", repr(e))


tts_channels = load_tts_channels()


# =========================
# 봇 초기화
# =========================

load_dotenv()

token = os.getenv("DISCORD_TOKEN")

if token is None:
    raise RuntimeError("DISCORD_TOKEN이 .env 파일에 없음")

handler = logging.FileHandler(
    filename="discord.log",
    encoding="utf-8",
    mode="w"
)

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix="#", intents=intents)

setup_commands(bot, tts_channels, save_tts_channels, tts_queues)

# =========================
# 자동 반응
# =========================

async def try_keyword_reaction(message: discord.Message):
    if message.guild is None:
        return

    content = message.content.strip()

    for rule in KEYWORD_REACTIONS:
        keyword = rule["keyword"]

        # 메시지 전체가 keyword와 완전히 같을 때만 반응
        if content != keyword:
            continue

        key = (message.guild.id, keyword)
        now = time.time()
        last = reaction_last_used.get(key, 0)

        if now - last < rule.get("cooldown", 10):
            continue

        if random.random() >= rule.get("prob", 0.1):
            continue

        response = random.choice(rule["responses"])
        reaction_last_used[key] = now

        # 채팅에도 보내기
        await message.channel.send(response)

        # 음성 채널에 있으면 TTS로도 읽기
        if message.guild.voice_client is not None:
            await add_bot_tts_queue(bot, message.guild, message.channel, response)

        break


# =========================
# 이벤트
# =========================

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user.name} - {bot.user.id}")

    if not getattr(bot, "synced", False):
        all_ok = True

        for guild in MY_GUILDS:
            try:
                bot.tree.copy_global_to(guild=guild)
                synced = await bot.tree.sync(guild=guild)
                print(f"Guild {guild.id} synced: {len(synced)}")

            except Exception as e:
                all_ok = False
                print(f"SYNC ERROR for {guild.id}:", repr(e))

        if all_ok:
            bot.synced = True


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    if message.guild is None:
        return

    guild_id = message.guild.id
    channel_id = message.channel.id

    if tts_channels.get(guild_id) == channel_id:
        voice_client = message.guild.voice_client

        if voice_client is None:
            print("봇이 음성채널에 없음")
        else:
            await add_tts_queue(bot, message)

    await try_keyword_reaction(message)

    await bot.process_commands(message)


@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot:
        return

    guild = member.guild
    voice_client = guild.voice_client

    if voice_client is None:
        return

    if before.channel == voice_client.channel and after.channel != voice_client.channel:
        human_count = sum(
            1
            for member_in_channel in voice_client.channel.members
            if not member_in_channel.bot
        )

        if human_count == 0:
            print(f"[{guild.id}] 혼자 남아서 자동 퇴장")
            await voice_client.disconnect()

# =========================
# 실행
# =========================

bot.run(token, log_handler=handler, log_level=logging.DEBUG)