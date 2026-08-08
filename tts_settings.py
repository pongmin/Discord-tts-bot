# =========================
# 유저별 TTS 설정 저장/로드
# =========================
import json
import pathlib

BASE_DIR = pathlib.Path(__file__).resolve().parent
USER_TTS_SETTINGS_FILE = BASE_DIR / "user_tts_settings.json"


def load_user_tts_settings():
    if not USER_TTS_SETTINGS_FILE.exists():
        return {}

    try:
        with open(USER_TTS_SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        return {int(user_id): setting for user_id, setting in data.items()}

    except Exception as e:
        print("유저 TTS 설정 로드 실패:", repr(e))
        return {}


def save_user_tts_settings():
    try:
        with open(USER_TTS_SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(USER_TTS_SETTINGS, f, ensure_ascii=False, indent=4)

    except Exception as e:
        print("유저 TTS 설정 저장 실패:", repr(e))


USER_TTS_SETTINGS = load_user_tts_settings()
