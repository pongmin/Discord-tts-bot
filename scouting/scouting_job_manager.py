"""스카우팅 수집(계정 조회 -> 경기 수집 -> DB 저장 -> 후속 계산) 백그라운드 job의
작고 범용적인 관리 레이어.

Discord 슬래시 커맨드는 이 모듈을 통해 job을 등록만 하고 즉시 반환해야 함 -
실제 오래 걸리는 작업(Riot API 조회, DB 기록, 추천 계산 등)은 봇 프로세스
안의 asyncio background task로 계속 실행되고, interaction의 15분 토큰
수명과는 완전히 분리됨. 결과/실패 통지는 이 모듈이 아니라 각 job의 runner가
직접 채널 메시지 등으로 처리함 - 이 모듈은 Discord나 밴 추천을 전혀 모름.

/banrecommend 전용이 아니라 "팀(선수 목록) 단위 스카우팅 수집 job"을 다루는
범용 레이어로 설계함. 나중에 Clash 상대 팀 prefetch에도 그대로 재사용할 생각
(이번 변경에서 prefetch 자체는 구현하지 않음).

영속 큐나 별도 서버는 범위 밖. 프로세스가 재시작되면 실행 중이던 job은
그냥 사라짐 - 단, 수집 자체는 이미 scouting.db에 즉시/점진적으로 저장되므로
재시작 후 다시 실행해도 이미 받은 경기는 캐시로 재사용됨(job 자체의 자동
복구는 이번 범위 밖).
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
import logging
import time

logger = logging.getLogger(__name__)

# 팀 job마다 각자 새 sqlite connection을 엶(scouting_db.get_connection()) -
# 수집/DB 기록은 메인 이벤트 루프 스레드에서, 추천 계산 중 읽기 전용 조회는
# (기존 코드가 이미 그렇게 짜여 있었음) asyncio.to_thread로 별도 OS 스레드에서
# 새 connection을 열어 돈다. 이 두 가지가 서로 다른 팀 job 사이에서 겹치면
# 진짜 문제가 생김:
#   - 두 job이 동시에 쓰기 트랜잭션을 열면 SQLite가 "database is locked"를
#     던질 수 있고, 전부 한 이벤트 루프 스레드에서 협조적으로 돌아가는 코루틴
#     끼리는 한쪽이 커밋 안 된 트랜잭션을 쥔 채 대기 중일 때 다른 쪽이 락을
#     기다리며 블로킹돼도 먼저 쥔 쪽이 스케줄될 기회가 없어 busy_timeout으로도
#     안 풀리는 사실상의 교착 상태가 됨.
#   - to_thread로 돌아가는 읽기 스레드는 진짜 별도 OS 스레드라서, 그 사이에
#     다른 job이 메인 스레드에서 쓰기를 하면 진짜 동시 접근이 됨.
# sqlite connection을 스레드 간에 공유하거나 무리하게 thread-safety를 깨는
# 방식으로 "해결"하지 않기로 했으므로, 안전한 해결책은 각 job의 DB 접근
# 전체(수집 쓰기 + 이후 추천 계산의 읽기 스레드까지)를 팀 job 사이에서
# 통째로 직렬화하는 것. Riot API 요청 자체의 429 처리는 이미 riot_api.py에
# 있고 이 락으로 대체되는 게 아님 - 이 락은 그 위에 "한 번에 한 팀만
# scouting.db에 접근한다"는 순서만 강제함. 다른 팀 job이 거부되는 건 아니고,
# 이 락 앞에서 대기했다가 순서대로 진행됨.
scouting_db_lock = asyncio.Lock()


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class ScoutingJob:
    """하나의 백그라운드 스카우팅 수집 job.

    key: 중복 방지용 식별자(예: 팀 5명의 (role, game_name, tag_line) 튜플).
         같은 key로 이미 QUEUED/RUNNING인 job이 있으면 새 job을 만들지 않음.
    kind: "banrecommend" 등 job 종류 문자열. 이 모듈은 값의 의미를 모르고
          그냥 보관/노출만 함.
    players: 이 job이 다루는 선수/역할 목록. 이 모듈은 내용을 해석하지 않음.
    """
    key: tuple
    kind: str
    user_id: int
    guild_id: int | None
    channel_id: int
    players: tuple
    status: JobStatus = JobStatus.QUEUED
    stage: str = "대기 중"
    created_at: float = field(default_factory=time.monotonic)
    task: asyncio.Task | None = field(default=None, repr=False)
    error: str | None = None

    def set_stage(self, stage: str) -> None:
        self.stage = stage


class ScoutingJobManager:
    """key별로 활성 job을 최대 하나만 유지함(같은 팀 중복 수집 방지)."""

    def __init__(self) -> None:
        self._jobs: dict[tuple, ScoutingJob] = {}

    def get(self, key: tuple) -> ScoutingJob | None:
        return self._jobs.get(key)

    def is_active(self, key: tuple) -> bool:
        job = self._jobs.get(key)
        return job is not None and job.status in (JobStatus.QUEUED, JobStatus.RUNNING)

    def start(
        self,
        key: tuple,
        kind: str,
        user_id: int,
        guild_id: int | None,
        channel_id: int,
        players: tuple,
        runner: Callable[[ScoutingJob], Awaitable[None]],
    ) -> tuple[ScoutingJob, bool]:
        """key에 대한 job을 스케줄하고 (job, created)를 반환함.

        이미 같은 key로 QUEUED/RUNNING인 job이 있으면 새로 만들지 않고
        (기존 job, False)를 반환함 - 호출자는 이 경우 "이미 같은 팀을 분석
        중입니다" 같은 안내만 하면 됨. 이 메서드는 중간에 await하지 않으므로
        (동기 함수), 확인과 등록 사이에 다른 코루틴이 끼어들 수 없어 경쟁
        상태 없이 안전하게 중복을 막음.
        """
        existing = self._jobs.get(key)

        if existing is not None and existing.status in (JobStatus.QUEUED, JobStatus.RUNNING):
            return existing, False

        job = ScoutingJob(
            key=key, kind=kind, user_id=user_id, guild_id=guild_id,
            channel_id=channel_id, players=players,
        )
        self._jobs[key] = job

        async def _run() -> None:
            job.status = JobStatus.RUNNING

            try:
                await runner(job)
                job.status = JobStatus.COMPLETED
            except asyncio.CancelledError:
                job.status = JobStatus.FAILED
                job.error = "취소됨"
                raise
            except Exception as exc:
                # runner가 실패를 사용자에게 알리는 것까지 책임짐(채널 메시지 등).
                # 여기서는 상태 기록/로깅만 함 - 이 모듈은 알림 방법을 모름.
                logger.exception("Scouting job %r failed at stage %r", key, job.stage)
                job.status = JobStatus.FAILED
                job.error = str(exc)

        job.task = asyncio.create_task(_run())
        return job, True


# 봇 프로세스 전체에서 공유하는 싱글턴. 영속 큐/별도 서버는 이번 범위 밖.
scouting_jobs = ScoutingJobManager()
