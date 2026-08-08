# =========================
# 유니코드 이모지 제거
# =========================
import re

_EMOJI_RE = re.compile(
    "["
    "\U0001F1E6-\U0001F1FF"  # 국기
    "\U0001F300-\U0001F5FF"  # 기호 & 그림
    "\U0001F600-\U0001F64F"  # 얼굴 이모지
    "\U0001F680-\U0001F6FF"  # 교통/지도
    "\U0001F700-\U0001F77F"
    "\U0001F780-\U0001F7FF"
    "\U0001F800-\U0001F8FF"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FAFF"
    "\U00002700-\U000027BF"
    "\U00002600-\U000026FF"
    "\U0000200D"             # ZWJ
    "\U0000FE0F"             # variation selector
    "]+",
    flags=re.UNICODE
)


def remove_unicode_emojis(text: str) -> str:
    return _EMOJI_RE.sub(" ", text)
