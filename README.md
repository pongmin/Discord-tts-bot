Discord TTS Bot

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
