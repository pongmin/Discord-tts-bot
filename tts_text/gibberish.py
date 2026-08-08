# =========================
# 한글/자모 난타(도배) 감지 및 제거
# =========================
import re

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

# 여러 종류가 섞여도 감정 표현으로 인정 (예: ㅋㅋㅎㅎ, ㅠㅜㅠㅜ)
_MIXABLE_EMOTION_JAMO = set("ㅋㅎㅠㅜ")

# 같은 글자만 반복될 때만 감정 표현으로 인정
# ㄱㄱㄱㄱ(가자/go), ㄹㄹㄹㄹ, ㅁㅁㅁㅁ, ㅍㅍㅍㅍ, ㅂㅂㅂㅂ(bye), ㅅㅅㅅㅅ(고마워)도 흔한 채팅 표현이라 허용
_REPEAT_ONLY_EMOTION_JAMO = set("ㄷㅉㅇㄴㄱㄹㅁㅍㅂㅅ")


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


def _is_emotion_jamo_run(run: str) -> bool:
    """
    정상적인 감정/채팅 표현으로 볼 수 있는 자모 반복은 허용.
    예: ㅋㅋㅋㅋ, ㅎㅎㅎㅎ, ㅠㅜㅠㅜ, ㄷㄷㄷ, ㅉㅉㅉ, ㅇㅇ, ㄴㄴ
    """
    if not run:
        return False

    chars = set(run)

    if chars <= _MIXABLE_EMOTION_JAMO:
        return True

    return len(chars) == 1 and chars <= _REPEAT_ONLY_EMOTION_JAMO


def strip_gibberish_jamo(text: str) -> str:
    """
    ㅁㄴㅇㄹ처럼 4글자 이상 이어지는 자모 난타 덩어리만 지우고,
    나머지 멀쩡한 문장은 그대로 살림.
    예: "ㅁㄴㅇㄹ 진짜 웃기다" -> " 진짜 웃기다"
    """
    def _replace(match: re.Match) -> str:
        run = match.group(0)

        if _is_emotion_jamo_run(run):
            return run

        if len(run) >= 4:
            return " "

        return run

    return _JAMO_RUN_RE.sub(_replace, text)


def is_gibberish_korean(text: str) -> bool:
    """
    자모 난타(도배)를 판정.
    단, 정상적인 감정 표현은 차단하지 않음.
    """
    text = text.strip()

    if len(text) < 6:
        return False

    compact = _WS_RE.sub("", text)

    if not compact:
        return False

    # strip_gibberish_jamo가 4글자 이상 덩어리는 이미 지웠으므로,
    # 여기서는 4글자 미만으로 쪼개져 살아남은 자모가 여기저기 섞여 있는 경우만 봄
    # 예: "ㅁㄴㅇ 안녕 ㅂㅈㄷ"
    jamo_runs = _JAMO_RUN_RE.findall(compact)
    suspicious_runs = [run for run in jamo_runs if not _is_emotion_jamo_run(run)]

    if sum(len(run) for run in suspicious_runs) >= 6:
        return True

    # 영어 상태에서 한글을 잘못 친 경우
    if _looks_like_wrong_ime(text):
        return True

    return False
