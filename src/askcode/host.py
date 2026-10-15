"""맥북 호스트 모드: launchd로 봇·웹 콘솔을 띄우고(죽으면 다시 뜸), 켜져 있는 동안 잠자기를 막고,
매일 새벽 DB 백업을 돌린다. LLM 인증은 .env의 ASKCODE_LLM_PROVIDER가 정한다
(api=ANTHROPIC_API_KEY, subscription=로컬 claude CLI 구독 로그인 — 로컬 PoC 전용).

  askcode host on      등록하고 켠다
  askcode host off     끄고 등록을 지운다
  askcode host status  실행 여부·콘솔 주소·최근 로그
"""

from __future__ import annotations

import fcntl
import os
import plistlib
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

LABEL_PREFIX = "com.askcode"
CONSOLE_PORT = 8787


def acquire_serve_lock(data_dir: Path):
    """봇 중복 실행 방지. 잠금을 얻으면 파일 객체(프로세스가 끝날 때 풀림), 이미 실행 중이면 None."""
    data_dir.mkdir(parents=True, exist_ok=True)
    f = (data_dir / "serve.lock").open("w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        return None
    f.write(str(os.getpid()))
    f.flush()
    return f


@dataclass(frozen=True)
class Job:
    name: str          # bot | console | backup
    label: str
    args: list[str]
    keep_alive: bool
    calendar: dict | None = None  # 매일 실행 시각


def jobs(repo: Path, uv: str, port: int = CONSOLE_PORT) -> list[Job]:
    run = [uv, "run", "--project", str(repo), "askcode"]
    # caffeinate -i: 이 프로세스가 도는 동안 시스템 잠자기를 막는다(화면은 꺼져도 됨)
    return [
        Job("bot", f"{LABEL_PREFIX}.bot", ["/usr/bin/caffeinate", "-i", *run, "serve"], keep_alive=True),
        Job("console", f"{LABEL_PREFIX}.console", [*run, "console", "--port", str(port)], keep_alive=True),
        Job("backup", f"{LABEL_PREFIX}.backup",
            ["/bin/sh", "-c", f"{' '.join(run)} backup && {' '.join(run)} restore-check && {' '.join(run)} maintenance"],
            keep_alive=False, calendar={"Hour": 3, "Minute": 30}),
    ]


def plist_for(job: Job, repo: Path, log_dir: Path) -> dict:
    d = {
        "Label": job.label,
        "ProgramArguments": job.args,
        "WorkingDirectory": str(repo),  # .env를 여기서 읽는다
        "RunAtLoad": job.keep_alive,
        "StandardOutPath": str(log_dir / f"{job.name}.log"),
        "StandardErrorPath": str(log_dir / f"{job.name}.log"),
        # 셸 환경을 물려받지 않는다: 셸에 남은 ANTHROPIC_* 값이 .env의 봇 키를 덮지 않게
        "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin", "PYTHONUNBUFFERED": "1"},
    }
    if job.keep_alive:
        d["KeepAlive"] = True
        d["ThrottleInterval"] = 60  # 키·크레딧 문제로 바로 죽을 때 1분에 한 번만 다시 시도
    if job.calendar:
        d["StartCalendarInterval"] = job.calendar
    return d


def _agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def _loaded(label: str) -> bool:
    return _launchctl("print", f"{_domain()}/{label}").returncode == 0


def on(repo: Path, log_dir: Path, port: int = CONSOLE_PORT) -> list[str]:
    uv = shutil.which("uv") or "/opt/homebrew/bin/uv"
    log_dir.mkdir(parents=True, exist_ok=True)
    _agents_dir().mkdir(parents=True, exist_ok=True)
    done = []
    for job in jobs(repo.resolve(), uv, port):
        path = _agents_dir() / f"{job.label}.plist"
        path.write_bytes(plistlib.dumps(plist_for(job, repo.resolve(), log_dir.resolve())))
        if _loaded(job.label):
            _launchctl("bootout", f"{_domain()}/{job.label}")
        r = _launchctl("bootstrap", _domain(), str(path))
        if r.returncode != 0:
            raise RuntimeError(f"{job.label} 등록 실패: {r.stderr.strip()}")
        done.append(job.name)
    return done


def off() -> list[str]:
    done = []
    for name in ("bot", "console", "backup"):
        label = f"{LABEL_PREFIX}.{name}"
        if _loaded(label):
            _launchctl("bootout", f"{_domain()}/{label}")
            done.append(name)
        (_agents_dir() / f"{label}.plist").unlink(missing_ok=True)
    return done


def restart(name: str) -> bool:
    """등록된 작업 하나를 다시 띄운다(kickstart -k). 등록돼 있지 않으면 False."""
    label = f"{LABEL_PREFIX}.{name}"
    if not _loaded(label):
        return False
    return _launchctl("kickstart", "-k", f"{_domain()}/{label}").returncode == 0


def job_states(log_dir: Path) -> dict[str, dict]:
    """작업별 상태. state: running | waiting(재시작 대기·예약) | off"""
    out = {}
    for name in ("bot", "console", "backup"):
        label = f"{LABEL_PREFIX}.{name}"
        r = _launchctl("print", f"{_domain()}/{label}")
        pid, last_exit = None, None
        if r.returncode != 0:
            state = "off"
        else:
            info = dict(line.strip().split(" = ", 1) for line in r.stdout.splitlines() if " = " in line)
            pid = int(info["pid"]) if info.get("pid", "").isdigit() else None
            last_exit = info.get("last exit code")
            state = "running" if pid else "waiting"
        log = log_dir / f"{name}.log"
        last = ""
        if log.exists():
            lines = [x for x in log.read_text(encoding="utf-8", errors="replace").splitlines()[-200:]
                     if x.strip() and " INFO httpx" not in x]
            last = lines[-1][:200] if lines else ""
        out[name] = {"state": state, "pid": pid, "last_exit": last_exit, "last_log": last}
    return out


def status(log_dir: Path) -> list[tuple[str, str, str]]:
    """(이름, 상태 문장, 최근 로그 한 줄) — CLI 표시용"""
    out = []
    for name, j in job_states(log_dir).items():
        if j["state"] == "off":
            text = "꺼짐"
        elif j["state"] == "running":
            text = f"실행 중 (pid {j['pid']})"
        else:
            text = "대기 (매일 03:30)" if name == "backup" else f"재시작 대기 · 마지막 종료 {j['last_exit'] or '-'}"
        out.append((name, text, j["last_log"]))
    return out
