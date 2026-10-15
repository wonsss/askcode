"""메뉴 막대 앱이 읽는 상태 한 덩어리(JSON). 앱은 화면만 맡고, 판단은 여기 한곳에서 한다.

기본은 Claude를 호출하지 않는다(비용 0). check_claude=True일 때만 1토큰 호출로 키·크레딧을 확인한다.
"""

from __future__ import annotations

import time
from pathlib import Path

from askcode import host
from askcode.ops import latest_backup


def _midnight() -> float:
    t = time.localtime()
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, t.tm_wday, t.tm_yday, t.tm_isdst))


def collect(settings, repo_name: str, web_base: str, port: int = host.CONSOLE_PORT,
            check_claude: bool = False) -> dict:
    from askcode.console.data import ROUTE_KO, ConsoleData

    jobs = host.job_states(settings.data_dir / "logs")
    out: dict = {
        "generated_at": time.time(),
        "jobs": jobs,
        "installed": any(j["state"] != "off" for j in jobs.values()),
        "console_url": f"http://127.0.0.1:{port}",
        "auth": "api_key" if settings.anthropic_api_key else "missing",
    }
    if settings.db_path.exists():
        data = ConsoleData(settings.db_path, repo_name, web_base, Path("eval/results"))
        today, week = _midnight(), time.time() - 7 * 86400
        rows_week = data.rows(source="all", since=week)
        slack_today = [r for r in rows_week if r.source == "slack" and r.ts >= today]
        out["today"] = {"questions": len(slack_today),
                        "cost": round(sum(r.cost for r in rows_week if r.ts >= today), 4)}
        out["week"] = {"questions": sum(1 for r in rows_week if r.source == "slack"),
                       "cost": round(sum(r.cost for r in rows_week), 4)}
        out["check"] = len(data.needs_check())
        out["recent"] = [{"id": r.id, "question": r.question, "route": ROUTE_KO.get(r.route or "", r.route or ""),
                          "confidence": r.confidence, "ts": r.ts, "cost": round(r.cost, 4),
                          "flags": r.check_reasons}
                         for r in data.rows(source="slack")[:6]]
        data.conn.close()
    else:
        out.update({"today": {"questions": 0, "cost": 0}, "week": {"questions": 0, "cost": 0}, "check": 0, "recent": []})
    b = latest_backup(settings.backup_dir)
    out["last_backup"] = ({"name": b.name, "age_hours": round((time.time() - b.stat().st_mtime) / 3600, 1),
                           "offsite": bool(settings.backup_upload_cmd)} if b else None)
    if check_claude:
        from askcode.doctor import claude_preflight

        problem = claude_preflight(settings)
        out["claude"] = {"ok": problem is None, "problem": problem}
    return out
