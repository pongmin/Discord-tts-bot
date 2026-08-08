# =========================
# TTS 텍스트 전처리 파이프라인
# =========================
import re

from .emoji import remove_unicode_emojis
from .gibberish import is_gibberish_korean, strip_gibberish_jamo
from .laughter import reduce_laughter

MAX_TTS_LENGTH = 50

# 이 위치 뒤에 공백이 있을 때만 단어 단위로 자름
MIN_WORD_CUT = 20

_CUSTOM_EMOJI_RE = re.compile(r"<a?:\w+:\d+>")
_URL_RE = re.compile(r"https?://\S+")
_MENTION_RE = re.compile(r"<@!?\d+>")
_CHANNEL_RE = re.compile(r"<#\d+>")
_ROLE_RE = re.compile(r"<@&\d+>")
_SEMICOLON_RE = re.compile(r";{2,}")
_EXCLAIM_RE = re.compile(r"!{3,}")
_SYMBOL_RE = re.compile(r"[~@#$%^&*_=+`|\\/<>{}\[\]]+")
_WS_RE = re.compile(r"\s+")


def clean_tts_text(text: str) -> str:
    text = text.strip()

    if not text:
        return ""

    # !로 시작하면 난타 필터 강제 통과
    # 예: !ㅁㄴㅇㄹ 이것도 읽게 만들고 싶을 때
    force = False

    if text.startswith("!"):
        force = True
        text = text[1:].strip()

        if not text:
            return ""

    if text == "?":
        text = "왓"

    # ㅋㅋㅋㅋ, ㅎㅎㅎㅎ, ㅠㅠㅠㅠ 같은 표현은 먼저 축약
    text = reduce_laughter(text)

    # 커스텀 이모티콘 제거
    text = _CUSTOM_EMOJI_RE.sub(" ", text)

    # 유니코드 이모지 제거
    text = remove_unicode_emojis(text)

    # URL 제거
    text = _URL_RE.sub(" 링크 ", text)

    # 멘션 단순화
    text = _MENTION_RE.sub(" 멘션 ", text)
    text = _CHANNEL_RE.sub(" 채널 ", text)
    text = _ROLE_RE.sub(" 역할 ", text)

    # ; 살리기
    text = _SEMICOLON_RE.sub(
        lambda m: " " + " ".join(["쎄미콜론"] * min(len(m.group(0)), 3)) + " ",
        text
    )

    # 느낌표는 살리되 과한 반복만 줄임. 예: 와!!!!!!!! -> 와!!
    text = _EXCLAIM_RE.sub("!!", text)

    # 읽을 수 없는 특수문자 제거
    text = _SYMBOL_RE.sub(" ", text)

    # 공백 정리
    text = _WS_RE.sub(" ", text).strip()

    if not text:
        return ""

    # 마크업을 다 걷어낸 뒤에 판정해야 이모티콘/링크/멘션이 난타 판정에 끼어들지 않음
    if not force:
        # 자모 난타 덩어리만 지우고, 나머지 문장은 살림
        # 예: "ㅁㄴㅇㄹ 진짜 웃기다" -> "진짜 웃기다"
        text = strip_gibberish_jamo(text)
        text = _WS_RE.sub(" ", text).strip()

        if not text:
            return ""

        # 남은 부분이 여전히 통째로 난타/도배면 그때만 전부 버림
        if is_gibberish_korean(text):
            return ""

    # 길이 제한
    if len(text) > MAX_TTS_LENGTH:
        cut = text[:MAX_TTS_LENGTH]
        last_space = cut.rfind(" ")

        # 영어/공백 있는 문장은 단어 중간에서 자르지 않도록 보정
        if last_space >= MIN_WORD_CUT:
            cut = cut[:last_space]

        text = cut

    return text
