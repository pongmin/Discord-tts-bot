# =========================
# 웃음/울음/리액션 반복 축약
# =========================
import re

# 웃음/울음은 2번만 반복돼도 표현으로 인정
_LAUGH_RE = re.compile(r"([ㅋㅎㅠㅜ])\1+")

# 감탄/리액션은 3번 이상부터 표현으로 인정
_REACTION_RE = re.compile(r"([ㄷㅉ])\1{2,}")


def _shrink_run(match: re.Match) -> str:
    chars = match.group(0)
    length = len(chars)

    if length <= 5:
        return chars

    # 5개까지는 그대로 두고, 넘치는 만큼은 절반만 남기되 최대 10개
    return chars[0] * min(5 + (length - 5) // 2, 10)


def reduce_laughter(text: str) -> str:
    """
    너무 긴 웃음/울음/리액션 표현을 적당히 줄임.
    예:
    ㅋㅋㅋㅋㅋㅋㅋㅋㅋㅋㅋㅋ -> ㅋㅋㅋㅋㅋㅋ
    ㅠㅠㅠㅠㅠㅠㅠㅠㅠㅠ -> ㅠㅠㅠㅠㅠ
    """
    text = _LAUGH_RE.sub(_shrink_run, text)
    text = _REACTION_RE.sub(_shrink_run, text)

    return text
