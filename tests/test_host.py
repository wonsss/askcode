import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from askcode.host import acquire_serve_lock, jobs, plist_for


def test_serve_lock_allows_only_one(tmp_path):
    first = acquire_serve_lock(tmp_path)
    assert first is not None
    assert acquire_serve_lock(tmp_path) is None  # 두 번째 봇은 거절
    first.close()
    again = acquire_serve_lock(tmp_path)  # 첫 봇이 끝나면 다시 띄울 수 있다
    assert again is not None
    again.close()


def test_launchd_plists(tmp_path):
    repo, logs = Path("/repo/askcode"), tmp_path / "logs"
    js = {j.name: j for j in jobs(repo, "/opt/homebrew/bin/uv", 8787)}
    bot = plist_for(js["bot"], repo, logs)
    assert bot["ProgramArguments"][:2] == ["/usr/bin/caffeinate", "-i"]  # 켜진 동안 잠자기 방지
    assert bot["ProgramArguments"][-1] == "serve" and "--project" in bot["ProgramArguments"]
    assert bot["KeepAlive"] is True and bot["ThrottleInterval"] == 60 and bot["RunAtLoad"] is True
    assert bot["WorkingDirectory"] == str(repo)  # .env를 레포에서 읽는다
    assert not any(k.startswith("ANTHROPIC") for k in bot["EnvironmentVariables"])  # 셸 키가 끼어들지 않게
    console = plist_for(js["console"], repo, logs)
    assert console["ProgramArguments"][-3:] == ["console", "--port", "8787"] and "caffeinate" not in console["ProgramArguments"][0]
    backup = plist_for(js["backup"], repo, logs)
    assert backup["StartCalendarInterval"] == {"Hour": 3, "Minute": 30} and "KeepAlive" not in backup
    assert "backup &&" in backup["ProgramArguments"][-1] and "restore-check" in backup["ProgramArguments"][-1]


@pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("plutil"), reason="macOS 전용")
def test_plists_pass_plutil_lint(tmp_path):
    repo = Path("/repo/askcode")
    for job in jobs(repo, "/opt/homebrew/bin/uv"):
        f = tmp_path / f"{job.label}.plist"
        f.write_bytes(plistlib.dumps(plist_for(job, repo, tmp_path)))
        r = subprocess.run(["plutil", "-lint", str(f)], capture_output=True, text=True)
        assert r.returncode == 0, r.stdout
