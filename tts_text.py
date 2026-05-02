# =========================
# TTS 텍스트 전처리
# =========================
import re

def reduce_laughter(text: str) -> str:
    """
    너무 긴 웃음/울음/리액션 표현을 적당히 줄임.
    예:
    ㅋㅋㅋㅋㅋㅋㅋㅋㅋㅋㅋㅋ -> ㅋㅋㅋㅋㅋㅋ
    ㅠㅠㅠㅠㅠㅠㅠㅠㅠㅠ -> ㅠㅠㅠㅠㅠ
    """
    def replacer(match):
        chars = match.group(0)
        length = len(chars)

        if length <= 5:
            return chars

        # 너무 긴 감정 표현은 절반 정도만 남김
        new_length = 5 + int((length-5) * 0.5)

        # 최소 5개, 최대 10개 정도로 제한
        new_length = max(5, min(new_length, 10))

        return chars[0] * new_length

    text = re.sub(r"ㅋ{2,}", replacer, text)
    text = re.sub(r"ㅎ{2,}", replacer, text)
    text = re.sub(r"ㅠ{2,}", replacer, text)
    text = re.sub(r"ㅜ{2,}", replacer, text)
    text = re.sub(r"ㄷ{3,}", replacer, text)
    text = re.sub(r"ㅉ{3,}", replacer, text)

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

_EN2KR_KEYS = set("rRseEfaqQtTdwWczxvgkoiOjpuPhynbml")

_COMMON_EN = {
    "the", "you", "and", "lol", "lmao", "ok", "okay", "bro", "wtf", "gg",
    "omg", "hi", "hello", "yes", "no", "plz", "please", "thanks", "sorry",
    "what", "why", "how", "when", "who", "this", "that", "really", "nice",
}


def _keyboard_adjacency_ratio(text: str) -> float:
    """
    자모가 키보드상 옆으로 연속해서 눌린 비율을 계산.
    예: ㅁㄴㅇㄹ 같은 건 인접 입력이 많아서 난타 가능성이 높음.
    """
    jamo = [c for c in text if c in _KB_INDEX]

    if len(jamo) < 4:
        return 0.0

    adj = 0

    for a, b in zip(jamo, jamo[1:]):
        ra, ca = _KB_INDEX[a]
        rb, cb = _KB_INDEX[b]

        if ra == rb and abs(ca - cb) == 1:
            adj += 1

    return adj / (len(jamo) - 1)


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
    tokens = re.findall(r"[A-Za-z]{5,}", text)

    if not tokens:
        return False

    for tok in tokens:
        lower_tok = tok.lower()

        if lower_tok in _COMMON_EN:
            continue

        if all(ch in _EN2KR_KEYS for ch in tok):
            vowel_ratio = sum(ch.lower() in "aeiou" for ch in tok) / len(tok)

            if vowel_ratio < 0.2:
                return True

    return False

def remove_unicode_emojis(text: str) -> str:
    emoji_pattern = re.compile(
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
    return emoji_pattern.sub(" ", text)


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
    if re.fullmatch(r"[ㅋㅎㅠㅜ]+", run):
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

    compact = re.sub(r"\s+", "", text)

    if not compact:
        return False

    # 자모 연속 검사
    # 단, 정상 감정 표현은 허용
    jamo_runs = re.findall(r"[ㄱ-ㅎㅏ-ㅣ]+", compact)
    suspicious_jamo_len = 0

    for run in jamo_runs:
        if _is_emotion_jamo_run(run):
            continue

        # 예: ㅁㄴㅇㄹ, ㅏㅣㅓㅏ, ㅂㅈㄷㄱ 같은 난타
        if len(run) >= 4:
            return True

        suspicious_jamo_len += len(run)

    # 감정 표현을 제외하고도 자모가 많이 섞여 있으면 난타 가능성
    if suspicious_jamo_len >= 6:
        return True

    jamo_count = sum(1 for c in compact if c in _KB_INDEX)

    # 키보드상 인접한 자모가 많이 이어지면 난타 가능성
    # 단, 전체 자모 run이 전부 감정 표현이면 허용
    if jamo_count >= 4 and _keyboard_adjacency_ratio(compact) >= 0.6:
        if jamo_runs and all(_is_emotion_jamo_run(run) for run in jamo_runs):
            return False

        return True

    # 같은 문자 반복이 너무 많으면 난타/도배 가능성
    # 단, 감정 표현은 reduce_laughter()가 이미 줄였으므로 과하게 막지 않음
    if len(compact) >= 12 and _unique_ratio(compact) < 0.3:
        if jamo_runs and all(_is_emotion_jamo_run(run) for run in jamo_runs):
            return False

        return True

    # 영어 상태에서 한글을 잘못 친 경우
    if _looks_like_wrong_ime(text):
        return True

    return False


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

    # 한글 난타 차단
    if not force and is_gibberish_korean(text):
        return ""
    # 커스텀 이모티콘 제거
    text = re.sub(r"<a?:\w+:\d+>", " ", text)

    # 유니코드 이모지 제거
    text = remove_unicode_emojis(text)

    # URL 제거
    text = re.sub(r"https?://\S+", " 링크 ", text)

    # 멘션 단순화
    text = re.sub(r"<@!?\d+>", " 멘션 ", text)
    text = re.sub(r"<#\d+>", " 채널 ", text)
    text = re.sub(r"<@&\d+>", " 역할 ", text)
    
    # ; 살리기
    text = re.sub(
    r";{2,}",
    lambda m: " " + " ".join(["쎄미콜론"] * min(len(m.group(0)), 3)) + " ",
    text
    )

    # 특수문자 과도 반복 제거
    # 느낌표는 살려둠. 예: 와!!! 같은 표현
    text = re.sub(r"[~@#$%^&*_=+`|\\/<>{}\[\]]+", " ", text)

    # 공백 정리
    text = re.sub(r"\s+", " ", text).strip()

    if not text:
        return ""

    # 길이 제한
    max_length = 50

    if len(text) > max_length:
        cut = text[:max_length]
        last_space = cut.rfind(" ")

        # 영어/공백 있는 문장은 단어 중간에서 자르지 않도록 보정
        if last_space >= 20:
            cut = cut[:last_space]

        text = cut

    return text
