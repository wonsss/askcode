"""AskCode CLI.

  askcode sync            레포 동기화 + 인덱싱
  askcode ask "질문"       슬랙 없이 파이프라인 실행
  askcode serve           슬랙 봇 실행 (주기적 동기화 포함)
  askcode stats           감사 로그 요약
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from askcode.app import build_container

SYNC_INTERVAL_SEC = 600


def cmd_sync() -> None:
    c = build_container()
    result, stats = c.refresh()
    print(f"{result.repo} @ {result.commit_sha[:7]} (이전 {result.previous_sha[:7] if result.previous_sha else '-'})")
    if result.changed_files:
        print(f"변경 파일 {len(result.changed_files)}개")
    if stats:
        print(f"추적 {stats.tracked} · 인덱싱 {stats.indexed} · 제외 {stats.excluded} · 바이너리/대용량 {stats.binary_or_large}")
    else:
        print("변경 없음")


def cmd_ask(question: str) -> None:
    from askcode.slack.blocks import CONFIDENCE_LABEL

    c = build_container()
    if c.store.file_count(c.repo.name) == 0:
        c.refresh()
    a = asyncio.run(c.service.answer(question, source="cli"))
    print(f"[{CONFIDENCE_LABEL[a.confidence]}] main@{a.commit[:7]}  ${a.cost_usd:.4f}  {a.duration_ms / 1000:.1f}s\n")
    print(a.body)
    print("\n근거")
    for x in a.citations:
        print(f"  ✅ {x.path}:{x.start}-{x.end} {x.note}")
    for x in a.unverified:
        print(f"  ⚠️ {x.path}:{x.start}-{x.end} ({x.reason})")
    if a.error:
        print(f"\n오류: {a.error}", file=sys.stderr)


def cmd_serve() -> None:
    from askcode.slack.app import build_app, serve

    c = build_container()
    s = c.settings
    if not (s.slack_bot_token and s.slack_app_token):
        sys.exit("ASKCODE_SLACK_BOT_TOKEN, ASKCODE_SLACK_APP_TOKEN이 필요합니다.")
    c.refresh()

    async def periodic_sync() -> None:
        while True:
            await asyncio.sleep(SYNC_INTERVAL_SEC)
            try:
                await asyncio.to_thread(c.refresh)
            except Exception:
                logging.exception("periodic sync failed")

    async def main() -> None:
        app = build_app(s.slack_bot_token.get_secret_value(), c.service, c.store, s.escalation_channel)
        asyncio.create_task(periodic_sync())
        await serve(app, s.slack_app_token.get_secret_value())

    asyncio.run(main())


def cmd_stats() -> None:
    c = build_container()
    row = c.store.execute(
        "SELECT COUNT(*) n, AVG(cost_usd) cost, AVG(duration_ms) ms, AVG(num_turns) turns, "
        "SUM(confidence='high') hi, SUM(confidence='medium') mid, SUM(confidence='low') lo FROM query_results"
    ).fetchone()
    fb = c.store.execute("SELECT value, COUNT(*) n FROM feedback GROUP BY value").fetchall()
    print(f"질문 {row['n']}건 · 평균 ${row['cost'] or 0:.4f} · {(row['ms'] or 0) / 1000:.1f}s · 도구 왕복 {row['turns'] or 0:.1f}회")
    print(f"확신도 높음 {row['hi'] or 0} / 보통 {row['mid'] or 0} / 낮음 {row['lo'] or 0}")
    print("피드백 " + (", ".join(f"{r['value']} {r['n']}" for r in fb) or "없음"))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="askcode")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sync")
    ask = sub.add_parser("ask")
    ask.add_argument("question")
    sub.add_parser("serve")
    sub.add_parser("stats")
    args = parser.parse_args()
    if args.cmd == "sync":
        cmd_sync()
    elif args.cmd == "ask":
        cmd_ask(args.question)
    elif args.cmd == "serve":
        cmd_serve()
    elif args.cmd == "stats":
        cmd_stats()


if __name__ == "__main__":
    main()
