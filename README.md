Discord TTS Bot

친구 서버에서 사용하기 위해 만든 Discord TTS 봇입니다.

기능
채팅 내용을 음성으로 읽기 (TTS)
ㅋㅋ, ㅠㅠ 등 감정 표현 자연스럽게 처리
커스텀 이모지 / 특수 문자 처리
키워드 자동 반응 (확률 + 쿨타임)
슬래시 명령어 지원 (/join, /leave, /skip 등)
설치 방법
pip install -r requirements.txt
python main.py
환경 설정

프로젝트 폴더에 .env 파일을 만들고 아래 내용을 추가하세요:

DISCORD_TOKEN=your_token_here
서버 설정

main.py에서 사용할 서버 ID를 추가해야 합니다:

GUILD_IDS = [
    123456789012345678,  # 여기에 본인 서버 ID 추가
]
