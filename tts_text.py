# =========================
# TTS 텍스트 전처리
# =========================
import re

MAX_TTS_LENGTH = 50

# 이 위치 뒤에 공백이 있을 때만 단어 단위로 자름
MIN_WORD_CUT = 20


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


# 2벌식 키보드 행
_KB_ROWS = [
    "ㅂㅈㄷㄱㅅㅛㅕㅑㅐㅔ",
    "ㅁㄴㅇㄹㅎㅗㅓㅏㅣ",
    "ㅋㅌㅊㅍㅠㅜㅡ",
]

_KB_INDEX = {
    ch: (r, c)
    for r, row in enumerate(_KB_ROWS)
    for c, ch in enumerate(row)
}

# 2벌식에서 자음/모음이 배치된 키
_KO_CONSONANT_KEYS = "qwertasdfgzxcvQWERT"
_KO_VOWEL_KEYS = "yuiophjklbnmOP"

# 2벌식으로 한글을 치면 (초성)(중성)(종성?)이 반복되는 모양이 됨.
# 영어 단어는 대부분 이 구조로 떨어지지 않음.
_HANGUL_KEY_RE = re.compile(
    f"(?:[{_KO_CONSONANT_KEYS}][{_KO_VOWEL_KEYS}]{{1,2}}[{_KO_CONSONANT_KEYS}]{{0,2}})+"
)

_COMMON_EN = {
    "the", "you", "and", "lol", "lmao", "ok", "okay", "bro", "wtf", "gg",
    "omg", "hi", "hello", "yes", "no", "plz", "please", "thanks", "sorry",
    "what", "why", "how", "when", "who", "this", "that", "really", "nice",
}

_JAMO_RUN_RE = re.compile(r"[ㄱ-ㅎㅏ-ㅣ]+")
_EN_TOKEN_RE = re.compile(r"[A-Za-z]{5,}")
_WS_RE = re.compile(r"\s+")


def _keyboard_stats(text: str) -> tuple[int, float]:
    """
    자모 개수와, 자모가 키보드상 옆으로 연속해서 눌린 비율을 한 번에 계산.
    예: ㅁㄴㅇㄹ 같은 건 인접 입력이 많아서 난타 가능성이 높음.
    """
    jamo = [_KB_INDEX[c] for c in text if c in _KB_INDEX]

    if len(jamo) < 4:
        return len(jamo), 0.0

    adj = sum(
        1
        for (ra, ca), (rb, cb) in zip(jamo, jamo[1:])
        if ra == rb and abs(ca - cb) == 1
    )

    return len(jamo), adj / (len(jamo) - 1)


def _unique_ratio(text: str) -> float:
    """
    문자열에서 서로 다른 글자의 비율.
    너무 낮으면 도배/반복 가능성이 있음.
    """
    if not text:
        return 1.0

    return len(set(text)) / len(text)


def _looks_like_wrong_ime(text: str) -> bool:
    """
    영어 상태에서 한글을 치려다 난타처럼 된 경우를 일부 감지.
    예: rkskekfk, dkssud 같은 입력.
    """
    for tok in _EN_TOKEN_RE.findall(text):
        lower_tok = tok.lower()

        if lower_tok in _COMMON_EN:
            continue

        # 2벌식 음절 구조로 떨어지지 않으면 그냥 영어로 봄
        if not _HANGUL_KEY_RE.fullmatch(tok):
            continue

        vowel_ratio = sum(ch in "aeiou" for ch in lower_tok) / len(tok)

        if vowel_ratio < 0.2:
            return True

    return False


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


_EMOTION_MIX_RE = re.compile(r"[ㅋㅎㅠㅜ]+")


def _is_emotion_jamo_run(run: str) -> bool:
    """
    정상적인 감정/채팅 표현으로 볼 수 있는 자모 반복은 허용.
    예:
    ㅋㅋㅋㅋ
    ㅎㅎㅎㅎ
    ㅠㅠㅠㅠ
    ㅜㅜㅜㅜ
    ㄷㄷㄷ
    ㅉㅉㅉ
    ㅇㅇ
    ㄴㄴ
    """
    if not run:
        return False

    # 같은 글자 반복
    if len(set(run)) == 1:
        ch = run[0]

        # 웃음/울음/감탄/리액션으로 자주 쓰는 것들
        if ch in "ㅋㅎㅠㅜㄷㅉㅇㄴ":
            return True

    # 섞인 감정 표현도 허용
    # 예: ㅋㅋㅎㅎ, ㅠㅜㅠㅜ, ㅋㅋㅠㅠ
    if _EMOTION_MIX_RE.fullmatch(run):
        return True

    return False


def is_gibberish_korean(text: str) -> bool:
    """
    한글/자모 난타를 판정.
    단, 정상적인 감정 표현은 차단하지 않음.
    """
    text = text.strip()

    if len(text) < 6:
        return False

    compact = _WS_RE.sub("", text)

    if not compact:
        return False

    # 자모 연속 검사
    # 단, 정상 감정 표현은 허용
    jamo_runs = _JAMO_RUN_RE.findall(compact)
    suspicious_jamo_len = 0
    only_emotion = bool(jamo_runs)

    for run in jamo_runs:
        if _is_emotion_jamo_run(run):
            continue

        only_emotion = False

        # 예: ㅁㄴㅇㄹ, ㅏㅣㅓㅏ, ㅂㅈㄷㄱ 같은 난타
        if len(run) >= 4:
            return True

        suspicious_jamo_len += len(run)

    # 감정 표현을 제외하고도 자모가 많이 섞여 있으면 난타 가능성
    if suspicious_jamo_len >= 6:
        return True

    jamo_count, adjacency = _keyboard_stats(compact)

    # 키보드상 인접한 자모가 많이 이어지면 난타 가능성
    # 단, 전체 자모 run이 전부 감정 표현이면 허용
    if jamo_count >= 4 and adjacency >= 0.6 and not only_emotion:
        return True

    # 같은 문자 반복이 너무 많으면 난타/도배 가능성
    # 단, 감정 표현은 reduce_laughter()가 이미 줄였으므로 과하게 막지 않음
    if len(compact) >= 12 and _unique_ratio(compact) < 0.3 and not only_emotion:
        return True

    # 영어 상태에서 한글을 잘못 친 경우
    if _looks_like_wrong_ime(text):
        return True

    return False


_CUSTOM_EMOJI_RE = re.compile(r"<a?:\w+:\d+>")
_URL_RE = re.compile(r"https?://\S+")
_MENTION_RE = re.compile(r"<@!?\d+>")
_CHANNEL_RE = re.compile(r"<#\d+>")
_ROLE_RE = re.compile(r"<@&\d+>")
_SEMICOLON_RE = re.compile(r";{2,}")
_EXCLAIM_RE = re.compile(r"!{3,}")
_SYMBOL_RE = re.compile(r"[~@#$%^&*_=+`|\\/<>{}\[\]]+")


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
    if not force and is_gibberish_korean(text):
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
