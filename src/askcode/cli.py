"""AskCode CLI.

  askcode sync [--force] [--no-external]   레포 동기화 + 코드·구조·문서 인덱싱 + 노션·슬랙 수집
  askcode ask "질문" [--image 화면.png]     슬랙 없이 파이프라인 실행 (스크린샷은 메모리에서만 읽음)
  askcode eval [--ids q01,q02] [--limit N]  골든 세트 평가 → eval/results/
  askcode serve                            슬랙 봇 실행 (주기적 동기화 포함)
  askcode stats                            감사 로그 요약
  askcode console [--port 8787]            웹 콘솔 (로컬 전용, 읽기 전용, API 비용 없음)
  askcode doctor [--online]                환경 점검 (도구·설정·인덱스, --online이면 연결까지)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from askcode.app import build_container

SYNC_INTERVAL_SEC = 600


def cmd_sync(force: bool, external: bool) -> None:
    c = build_container()
    r = c.refresh(external=external, force=force)
    print(f"{c.repo.name} @ {r.commit[:7]} (이전 {r.previous[:7] if r.previous else '-'}) · 변경 파일 {r.changed_files}개 · "
          f"{'재인덱싱' if r.reindexed else '변경 없음'}")
    if r.stats:
        print(json.dumps(r.stats, ensure_ascii=False, indent=2))


def _ensure_index(c) -> None:
    if c.store.file_count(c.repo.name) == 0:
        c.refresh()


def _read_image(path: str):
    from askcode.pipeline.screenshot import ALLOWED_TYPES, MAX_BYTES

    media = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
             ".gif": "image/gif"}.get(Path(path).suffix.lower())
    data = Path(path).read_bytes()
    if media not in ALLOWED_TYPES or len(data) > MAX_BYTES:
        sys.exit("--image는 png·jpg·webp·gif, 5MB 이하만 됩니다.")
    return data, media


def cmd_ask(question: str, domains: str | None, image: str | None = None) -> None:
    from askcode.pipeline.screenshot import extract_screen
    from askcode.slack.blocks import CONFIDENCE_LABEL

    c = build_container()
    _ensure_index(c)
    forced = None if domains is None else [d for d in domains.split(",") if d]

    async def run():
        shot = None
        if image:
            data, media = _read_image(image)
            shot = await extract_screen(c.llm, data, media)  # 이미지는 저장하지 않고 문구·배치만 넘긴다
            print(f"📷 화면 문구 {len(shot.texts)}개 · 배치 {len(shot.layout)}줄 · 추출 ${shot.cost_usd:.4f}")
        return await c.service.answer(question, source="cli", forced_domains=forced, screenshot=shot)

    a = asyncio.run(run())
    if a.status != "answer":
        print(f"[{a.status}] {a.body}")
        for did, name in a.options:
            print(f"  - {did}: {name}   (askcode ask \"{question}\" --domains {did})")
        print(f"\n비용 ${a.cost_usd:.4f}")
        return
    scope = ",".join(a.scope.domains) if a.scope and a.scope.domains else "전체"
    print(f"[{CONFIDENCE_LABEL[a.confidence]}] main@{a.commit[:7]} · 범위 {scope} · 경로 {a.route} · "
          f"${a.cost_usd:.4f} · {a.duration_ms / 1000:.1f}s · 도구 {a.tool_calls}회\n")
    print(a.body)
    print("\n근거")
    for x in a.citations:
        loc = f"{x.path}/" if x.start == 0 else f"{x.path}:{x.start}-{x.end}"
        print(f"  ✅ [{x.source}] {loc} {x.note}")
    for x in a.unverified:
        loc = f"{x.path}/" if x.start == 0 else f"{x.path}:{x.start}-{x.end}"
        print(f"  ⚠️ {loc} ({x.reason})")
    if a.attachment:
        filename, content = a.attachment
        flow = a.attachment_kind == "flow"
        out = Path(c.settings.data_dir) / ("diagrams" if flow else "wireframes") / filename
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(content, encoding="utf-8")
        print(f"\n{'🧭 흐름도' if flow else '🖼 변경안 (원본 스크린샷과 비교)' if a.comparison else '🖼 와이어프레임'}: {out}")
    elif a.route == "wireframe":
        print("\n⚠️ 와이어프레임 HTML이 없습니다")
    if a.followups:
        print("\n이어서 물어보기")
        for q in a.followups:
            print(f"  - {q}")
    if a.config_dependent:
        print("\n⚙ 일부 값은 설정·환경값에 따라 달라질 수 있음")
    if a.error:
        print(f"\n오류: {a.error}", file=sys.stderr)


def cmd_eval(ids: str | None, limit: int | None, concurrency: int) -> None:
    from askcode.evaluation import load_golden, run_eval

    c = build_container()
    _ensure_index(c)
    golden = load_golden(Path("eval/golden.yaml"))
    if ids:
        wanted = set(ids.split(","))
        golden = [g for g in golden if g["id"] in wanted]
    if limit:
        golden = golden[:limit]
    commit = c.store.get_repo_commit(c.repo.name) or "unknown"
    _, summary, md = asyncio.run(run_eval(c.service, c.llm, golden, Path("eval/results"), commit, concurrency))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\n결과표: {md}")


def cmd_serve() -> None:
    from askcode.slack.app import build_app, serve

    c = build_container()
    s = c.settings
    if not (s.slack_bot_token and s.slack_app_token):
        sys.exit("ASKCODE_SLACK_BOT_TOKEN, ASKCODE_SLACK_APP_TOKEN이 필요합니다.")
    from askcode.doctor import claude_preflight

    problem = claude_preflight(s)  # 키·크레딧 문제를 첫 질문이 아니라 시작할 때 알린다
    if problem:
        sys.exit(f"Claude를 호출할 수 없어 봇을 시작하지 않았어요: {problem}")
    c.refresh()

    async def periodic_sync() -> None:
        while True:
            await asyncio.sleep(SYNC_INTERVAL_SEC)
            try:
                await asyncio.to_thread(c.refresh)
            except Exception:
                logging.exception("periodic sync failed")

    async def main() -> None:
        app = build_app(s.slack_bot_token.get_secret_value(), c.service, c.store, s.escalation_channel, c.llm)
        asyncio.create_task(periodic_sync())
        await serve(app, s.slack_app_token.get_secret_value())

    asyncio.run(main())


def cmd_stats() -> None:
    c = build_container()
    row = c.store.execute(
        "SELECT COUNT(*) n, AVG(cost_usd) cost, SUM(cost_usd) total, AVG(duration_ms) ms, AVG(num_turns) turns, "
        "SUM(confidence='high') hi, SUM(confidence='medium') mid, SUM(confidence='low') lo FROM query_results"
    ).fetchone()
    routes = c.store.execute("SELECT route, COUNT(*) n, AVG(cost_usd) cost FROM query_results GROUP BY route").fetchall()
    fb = c.store.execute("SELECT value, COUNT(*) n FROM feedback GROUP BY value").fetchall()
    miss = c.store.execute("SELECT COUNT(*) n FROM tool_calls WHERE tool='widen_scope'").fetchone()["n"]
    print(f"질문 {row['n']}건 · 평균 ${row['cost'] or 0:.4f} · 합계 ${row['total'] or 0:.4f} · "
          f"{(row['ms'] or 0) / 1000:.1f}s · 대화 왕복 {row['turns'] or 0:.1f}회")
    print("경로별 " + ", ".join(f"{r['route']} {r['n']}건(평균 ${r['cost'] or 0:.4f})" for r in routes))
    print(f"확신도 높음 {row['hi'] or 0} / 보통 {row['mid'] or 0} / 낮음 {row['lo'] or 0} · 범위 확장(범위 미스) {miss}회")
    print("피드백 " + (", ".join(f"{r['value']} {r['n']}" for r in fb) or "없음"))


def cmd_console(port: int) -> None:
    """웹 콘솔. Claude API를 쓰지 않으므로 API 키 없이도 뜬다. 로컬(127.0.0.1)에서만 연다."""
    import uvicorn

    from askcode.config import load_repos
    from askcode.console.data import ConsoleData
    from askcode.console.server import create_app
    from askcode.pipeline.scoping import load_domain_map
    from askcode.settings import Settings

    s = Settings()
    repo = load_repos(s.config_dir)[0]
    names = {d.id: d.name for d in load_domain_map(s.config_dir).domains}
    data = ConsoleData(s.db_path, repo.name, repo.web_base, Path("eval/results"), names)
    print(f"AskCode 콘솔: http://127.0.0.1:{port}  (로컬 전용, 읽기 전용, 종료는 Ctrl+C)")
    uvicorn.run(create_app(data), host="127.0.0.1", port=port, log_level="warning")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="askcode")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("sync")
    sp.add_argument("--force", action="store_true")
    sp.add_argument("--no-external", action="store_true", help="노션·슬랙 수집 생략")
    ask = sub.add_parser("ask")
    ask.add_argument("question")
    ask.add_argument("--domains", help="스코핑 없이 범위 지정 (쉼표 구분, 빈 값이면 전체)")
    ask.add_argument("--image", help="화면 스크린샷 경로 (변경 요청이면 변경안 목업)")
    ev = sub.add_parser("eval")
    ev.add_argument("--ids")
    ev.add_argument("--limit", type=int)
    ev.add_argument("--concurrency", type=int, default=2)
    sub.add_parser("serve")
    sub.add_parser("stats")
    con = sub.add_parser("console", help="웹 콘솔 (로컬 전용, 읽기 전용)")
    con.add_argument("--port", type=int, default=8787)
    dr = sub.add_parser("doctor")
    dr.add_argument("--online", action="store_true", help="슬랙·Claude·GitHub·노션 연결까지 확인")
    args = parser.parse_args()
    if args.cmd == "doctor":
        from askcode.doctor import run

        sys.exit(run(args.online))
    if args.cmd == "sync":
        cmd_sync(args.force, not args.no_external)
    elif args.cmd == "ask":
        cmd_ask(args.question, args.domains, args.image)
    elif args.cmd == "eval":
        cmd_eval(args.ids, args.limit, args.concurrency)
    elif args.cmd == "serve":
        cmd_serve()
    elif args.cmd == "console":
        cmd_console(args.port)
    elif args.cmd == "stats":
        cmd_stats()


if __name__ == "__main__":
    main()
