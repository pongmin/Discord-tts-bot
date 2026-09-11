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

폴더 구조

```text
main.py                 봇 진입점 (여기서 실행)
discord_commands.py     일반 슬래시 커맨드
http_session.py         공용 aiohttp 세션
tts/                    TTS 음성 생성·큐·유저 설정 (tts_text/ 전처리 포함)
riot/                   Riot API 클라이언트, Data Dragon 캐시, 챔피언 이모지·추천
scouting/               밴 추천 알고리즘·리포트, 매치/스냅샷 수집, scouting.db 접근
clash/                  클래시 조회 커맨드
tests/                  네트워크 없는 테스트 전체
data/                   런타임 캐시·DB (git 추적 안 함, 항상 루트에 위치)
```

CLI 스크립트는 패키지 안에 있으므로 루트에서 모듈로 실행합니다
(예: `python -m scouting.collect_matches "이름#태그"`).

밴 추천 v1 수동 확인:

Discord에서는 봇을 재시작해 기존 길드 커맨드 동기화가 완료되면 아래처럼 사용합니다.

```text
/banrecommend top:"이름#태그" jungle:"이름#태그" middle:"이름#태그" bottom:"Pongmin#3369" utility:"이름#태그" depth:normal
```

다섯 인자는 모두 필수이며 인자 위치가 분석 역할을 결정합니다. 계정을 조회한 뒤
`depth`는 선택 사항이며 기본값은 `normal`입니다. 기존 수집기로 SoloQ(420)와
Normal Draft(400)를 각각 아래 상한까지 조회합니다. 상한은 선수별·큐별로 적용됩니다.

| depth | 큐별 상한 | 캐시가 없을 때 선수당 예상 시간 |
| --- | ---: | --- |
| quick | 30경기 | 약 1–2분 |
| normal (기본) | 100경기 | 약 4–5분 |
| deep | 200경기 | 약 9분 |

상한과 시간은 튜닝하지 않은 임시 값입니다. normal/deep은 약 9분/200경기의
측정치에 기반하며 quick은 빠른 조회를 위한 대략적인 비율입니다. 5명을 수집하면
더 오래 걸릴 수 있습니다.

수집 시작 컷오프까지 저장된 해당 선수·큐의 경기 수가 상한 이상이면 그 큐는
API 조회 없이 건너뜁니다. 이 건수는 역할과 무관하게 계산합니다. 부족하면 선택한
상한을 기존 수집기에 전달하고, 이미 캐시된 매치 상세는 다시 받지 않습니다.
실제 제공되는 기록이 상한보다 적어도 추가 수집을 강제하지 않습니다.
분석은 선택한 깊이와 무관하게 DB의 모든 가용 기록을 읽으며, 기존 기록을 삭제하거나
읽기 상한을 적용하지 않습니다. 깊이를 올리면 부족한 큐만 추가 수집합니다.
솔로 랭크 스냅샷은 24시간 캐시하며, 없거나 오래됐으면 기존 League-V4 API로
갱신합니다. 언랭크 응답은 명시적으로 기록하며 알고리즘의 임시 가중치 경고가 표시됩니다.

상대 팀이 격전(Clash)에 등록돼 있다면 다섯 명을 일일이 적는 대신 한 명만
넘기면 됩니다.

```text
/clashban riot_id:"Pongmin#3369" depth:normal
```

`/clashban`은 `/clashlookup`과 똑같은 경로로 그 선수의 격전 팀 로스터를
조회해서(테스트용 `이름#MOCK` 라우팅 포함) CLASH-V1 포지션을 다섯 역할 슬롯에
매핑한 다음, 그대로 `/banrecommend`의 수집·추천·리포트 파이프라인에 넘깁니다.
밴 로직이나 수집 로직은 따로 만들지 않았고, 진행 상황 표시와 6페이지 리포트도
동일합니다. 로스터 조회는 백그라운드 job을 만들기 전에 끝내므로, 팀을 못 찾거나
로스터가 5명이 아니거나 포지션이 중복·미지정이면 job 없이 바로 한국어 오류로
중단합니다. 확정된 다섯 명으로 만든 중복 방지 키는 `/banrecommend`의 것과 같아서,
같은 팀을 두 커맨드로 동시에 분석하는 일도 막힙니다.

형식 오류·중복 계정·실제 조회/수집 실패는 오류로 표시하고 추천을 중단합니다.
역할별 기록이 1–19경기이면 임시 기준에 따른 신뢰도 안내를 `Recommendation.warnings`에
추가하고 정상적으로 추천합니다. 이 기준은 수집량을 자동으로 늘리지 않습니다.
0경기는 변경하지 않은 알고리즘에서 계산이 불가능하므로 기존 오류를 유지합니다.
결과는 같은 메시지 안의 이전·다음 버튼으로 이동하는 6페이지 한글 리포트입니다.
TOP → JUNGLE → MID → BOTTOM → SUPPORT → 종합 밴 추천 순서로 구성됩니다.
선수 페이지에는 솔로랭크, 포지션 경기 수·승률, 픽 비중 순 상위 챔피언 8종,
조정 승률·조정 KDA·위험도와 주력 집중도를 표시합니다. 나머지는 `외 N개`로 안내하되
분석에는 전체 기록을 사용합니다. 관측된 프로필 아이콘을 캐시에서 재사용하고,
선수에 맞는 OP.GG 링크를 제공합니다. 아이콘 캐시가 없으면 생략합니다.
종합 페이지에는 추천·추가 고려 밴과 영향도, 예상 상대 위협 감소,
추천 안정성 및 기존 데이터로 만든 짧은 설명을 표시합니다. 선수별 경고는
해당 상세 페이지에, 풀 소진과 추천 안정성 경고는 종합 페이지에 한글로 안내합니다.
페이지 이동은 요청한 사용자만 할 수 있고, 스카우팅 리포트는 오래 열어볼 수
있어야 하므로 버튼에 시간 제한이 없습니다(persistent view, `timeout=None`).
봇이 재시작돼도 예전 리포트의 이전·다음 버튼이 계속 동작합니다 - 각 버튼은
고정된 custom_id(`banreport:prev`/`banreport:next`)를 쓰고, `ban_reports`
테이블(scouting_db.py)에 그 메시지의 대상 팀·컷오프·현재 페이지만 저장해 둠.
재시작 후 첫 클릭에서는 `PersistentBanReportRouter`(ban_commands.py, 봇
시작 시 `bot.add_view()`로 한 번 등록됨)가 그 정보로 추천을 다시 계산해서
메시지를 갱신하고, 그다음부터는 일반 메시지처럼 바로 반응합니다 - 추천
결과 자체는 저장하지 않고 항상 원래 컷오프로 다시 계산하므로, 재시작 후에도
그 시점 그대로의 리포트를 보게 됩니다. Discord 표시 코드는 `ban_report_ui.py`와
`ban_report_assets.py`에 분리되어 있고, `format_recommendation()`은 로컬
수동 확인용으로 유지됩니다.

챔피언 이름은 Data Dragon 캐시(`champion_data.py`, 기본 로케일 ko_KR)를
champion_id로 조회해 한글로 표시하며, 봇 시작 시(`on_ready`) 한 번 자동으로
캐시를 받아둡니다. 캐시가 아직 없거나 그 버전에 없는 챔피언은 DB에 저장된
Riot 내부 영문 이름으로 조용히 대체되므로(리포트 자체는 실패하지 않음),
최신 챔피언을 바로 한글로 보고 싶으면 `python -m riot.champion_data --force`로
캐시를 갱신하세요.

각 챔피언 이름 앞에는 스퀘어 아이콘이 인라인 이모지로 붙습니다
(`champion_emoji.py`). 길드 이모지가 아니라 봇 애플리케이션 소유 이모지라서
(discord.py의 create_application_emoji/fetch_application_emojis) 봇이 들어간
어떤 서버에서도 바로 쓸 수 있고 길드 이모지 슬롯을 쓰지 않습니다. 봇 시작 시
챔피언 데이터 캐시 갱신 뒤 백그라운드 task로 동기화하며(최초 1회는 챔피언
수만큼 업로드해서 몇 분 걸릴 수 있음 - `on_ready`를 막지 않음), 이미 올라간
이모지는(로컬 캐시 `data/discord_emojis.json`이 없어도 Discord 쪽 이름
`champ_<id>`로 대조해) 재사용하므로 재시작마다 다시 올리지 않습니다. 아직
동기화 전이거나 업로드에 실패한 챔피언은 아이콘 없이 이름만 표시됩니다.

계정 조회·수집·추천 계산은 슬래시 커맨드 응답과 분리된 백그라운드 job으로
실행됩니다(`scouting_job_manager.py`). 명령을 실행하면 입력 형식만 즉시
확인하고 바로 "🔎 밴 추천 분석을 시작했습니다" 라고 답한 뒤, 완료되거나
실패하면 원래 interaction과 무관하게 같은 채널에 봇 계정으로 새 메시지를
보냅니다 - Discord interaction의 15분 토큰 수명에 더 이상 묶이지 않습니다.
같은 5인 팀(역할+Riot ID)으로 이미 수집 중이면 새 job을 만들지 않고
"이미 같은 팀을 분석 중입니다." 라고 안내합니다. 서로 다른 팀은 동시에
요청할 수 있지만, 팀 사이의 DB 접근(수집 기록 + 추천 계산의 읽기)은
`scouting_db_lock`으로 직렬화되어 한 번에 한 팀만 scouting.db에 접근하며,
Riot API 429 처리는 기존 `riot_api.py` 로직을 그대로 공유합니다. 처리 시간이
40분을 넘으면 중단 사실을 채널에 알립니다. 다시 실행하면 저장된 경기를
재사용하므로 수집을 이어갈 수 있습니다. 프로세스가 재시작되면 실행 중이던
job은 사라지지만, 이미 커밋된 경기 데이터는 남아 있어 다시 실행할 때
캐시로 재사용됩니다.

커맨드·화면·캐시 메타데이터의 네트워크 없는 테스트:

```powershell
python -m unittest discover -s tests -t . -v
```

로컬 수동 실행:

```powershell
python -m tests.test_ban_algorithm
# 다른 수집 DB를 사용할 경우:
python -m tests.test_ban_algorithm --db path/to/scouting.db
```

실제 `Pongmin#3369`의 BOTTOM 기록과 합성 선수 4명으로 추천 및 진단을
출력합니다. 원본 DB는 읽기 전용으로 열고, 합성 데이터는 메모리에만 넣습니다.
Pongmin의 픽 확률·Threat·주 챔피언 밴 전후 잔여 위협은 출력으로 직접 확인합니다.

일반 사용은 `ban_algorithm.recommend_bans(repo, opponents)`에
`ScoutingRepo(cutoff_time=scouting_db.now_ms())`와 선수 5명의
`[(player_id, canonical_role), ...]`을 전달합니다.
`format_recommendation(result)`로 추천 3개, 추가 후보 5개, 세 집계 방식의
독립 전수 탐색 결과를 출력할 수 있습니다. 선수별 해당 포지션의 수집 기록이
없거나 후보 합집합이 3종 미만이면 명시적인 오류를 반환합니다.

상수는 `ban_algorithm.py`에 고정되어 있습니다. 픽 확률은 경기 시작 날짜와
큐(420/400) 가중치를 쓰고, Threat의 승패·KDA 집계는 가중하지 않습니다. 메타는
자신이 참가한 경기의 다른 참가자 중 같은 포지션을 모든 패치에서 모읍니다.
다른 참가자의 전체 픽 수로 메타 픽률을 구해 15% 혼합합니다. v1.1에서는
개인이 관측하지 않은 챔피언들의 메타 질량을 별도 `Others` 확률로 보존합니다.
`P_others = 0.15 * (1 - sum(P_meta(c) for c in observed_pool))`이며,
일반적인 혼합에서는 관측 챔피언을 다시 정규화하지 않습니다.
메타 표본이 없거나 이 질량이 너무 작으면 `1e-6` 안전 질량을 추가하고
전체 분포를 정규화합니다. 이는 확률 추정치가 아닌 수치·모델 안전장치입니다.
솔로 랭크 가중치는 임시 티어 척도이며,
컷오프 시점의 랭크가 없으면 1을 부여하고 진단을 출력합니다.

위험도에는 승률에 더해 KDA를 약한 보조 신호로 사용합니다.
기준 KDA는 해당 포지션 전체 `(kills + assists) / max(1, deaths)`,
챔피언 KDA도 챔피언별 합계의 같은 비율입니다. 챔피언별 경기 수를 이용해
`KDA_adj = (games * KDA_champ + 8 * KDA_baseline) / (games + 8)`로 보정한 뒤,
기존 승률 지수에 `0.4 * log(max(KDA_adj, 1e-6) / max(KDA_baseline, 1e-6))`을
더합니다. 1경기 표본도 포함하며 기준에는 해당 챔피언의 기록도 포함됩니다.
기존 nullable 경기 자료의 누락된 K/D/A 수치는 0으로 처리합니다.
`python -m tests.test_ban_algorithm`에서 Pongmin의 챔피언별 KDA와 위험도 변경 전후를
비교할 수 있습니다. 수치 경계 사례를 포함한 전체 테스트는
`python -m unittest discover -s tests -t . -v`로 실행합니다.

`Others`는 위험도 `1.0`인 중립 사전값이며, 밴 후보나 추천 목록에는 들어가지
않습니다. 밴 후 비례 재분배에는 항상 남는 선택지로 포함됩니다.
관측 풀이 전부 밴되면 `S=1.0`, `r=1.0/S(empty)`이며,
`observed_pool_exhausted=True`는 미관측 챔피언 추정값에 의존한다는 뜻입니다.
풀 소진만으로 기하·조화 평균의 팀 위협이 0이 되지는 않습니다.
일부만 밴했을 때는 남은 챔피언의 Threat 차이로 효과가 결정되므로, 픽이
집중되어 있다는 사실만으로 주 챔피언 밴 효과가 보장되지는 않습니다.

**Champion Dependency (v1.2)**: Threat(성과)와 별개로, 밴이 그 선수의 익숙한
챔피언 풀을 얼마나 빼앗는지를 따로 계산합니다. 비례 재분배 모델만 쓰면,
모스트1이 위험도는 낮지만 압도적으로 많이 하는 챔피언인 경우 "밴하면 오히려
더 잘하는 챔피언으로 갈 테니 열어두는 게 낫다"는 결론이 나올 수 있는데,
Dependency는 이 편향을 성과와 독립적으로 보정합니다. 기존 성과 잔여
`r_perf(p,B) = S(p,B) / S(p,empty)`는 그대로 두고,
`q(p,B) = sum(P_personal(c) for c in B if c가 그 선수의 관측 풀에 있음)`,
`D(p,B) = exp(-0.35 * q(p,B))`(`0..1`로 clamp)를 곱한
`r(p,B) = r_perf(p,B) * D(p,B)`를 최종 잔여로 씁니다. 반드시 메타 혼합 전
`P_personal`을 쓰며(`P_final`은 메타 백오프로 실제 의존도와 달라질 수 있어서
안 씀), `Others`는 그 선수의 관측 풀이 아니므로 `q`에 절대 포함되지 않습니다.
`B`가 비어 있으면 `D=1`이라 `r=r_perf`와 같고, 관측 풀을 전부 밴해도(`D`가
`exp(-0.35)`까지만 내려가므로) 잔여가 0이 되지는 않습니다. 탐색·rho 집계·
marginal contribution은 전부 이 새 `r`을 그대로 쓰므로 최적 밴 결과에
실제로 반영됩니다.

후보 8종 pruning(`P_final * Threat` 상위 8개)만 쓰면 위험도는 낮지만
`P_personal`이 매우 높은 모스트가 애초에 탐색 후보에서 빠질 수 있어서,
선수별로 `P_personal` 상위 3종(`DEPENDENCY_CANDIDATES`)을 항상 후보에
추가로 포함시킵니다 - 그러지 않으면 Dependency가 있어도 그 챔피언 자체가
탐색되지 않아 무의미해집니다.

종합 페이지의 "왜 이 밴인가?"는 `ban_attribution.py`에서 계산합니다. 추천 밴
`c`마다 최적해 `B*`와 `B* - {c}`를 실제로 다시 평가해서, 선수 i별 기여를
로그 공간에서 분해합니다:

```
total_delta_i = w_i * (log r_i(B* - {c}) - log r_i(B*))
perf_delta_i  = w_i * (log r_perf_i(B* - {c}) - log r_perf_i(B*))
dep_delta_i   = w_i * (log D_i(B* - {c})      - log D_i(B*))
```

`r = r_perf * D`이므로 `total = perf + dep`이 정확히 성립하고, 다섯 명을
합하면 optimizer가 표시하는 marginal contribution과 같은 값(로그 스케일)이
됩니다. 여기에 단독 밴 효과(`solo_delta = -log V_0({c})`)와 추천 밴끼리의
조합 이득(`partner_gain`)을 더해, performance 중심 / dependency 중심 / 혼합 /
여러 선수 분산 / 한 선수 집중 / 낮은 픽률 고위험 대체픽 / 조합 시너지 /
단독은 약하지만 조합에서 강함 / 모스트지만 조합에서만 가치 있음 / 관측 풀
소진 / 추가 효과는 작지만 현재 조합에서 최선 중 최대 2개를 골라 한국어
문장으로 만듭니다. `P_personal >= 0.7` 같은 값은 이유 판정이 아니라 문장
표현(예: "사실상 원챔")을 고르는 재료로만 씁니다. 이 레이어는 설명 전용이라
`B*`·순서·점수는 전혀 바꾸지 않습니다. 선수 상세 페이지에는 챔피언별
숫자 표시를 추가하지 않았고, 기존 주력 집중도(Top1/Top3)를 그대로 씁니다.

Dependency 전용 테스트: `python -m unittest tests.test_ban_dependency -v`
(성과·Threat와 독립적인 페널티 검증, Others 제외, 탐색/marginal 반영,
극단값에서 NaN/inf 없음, 그리고 실제와 비슷한 수치로 재구성한 샤밀하#KR1/
BOTTOM 시나리오 - 모스트1 미스 포츈이 Dependency 도입 전에는 추천 3밴에
아예 들지 못하다가 도입 후 2위로 들어오는 것까지 확인합니다).
