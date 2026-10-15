"""골든 세트 평가 하네스.

단계별로 따로 측정해 실패 원인을 가른다.
- 행동: answer / clarify / refuse가 기대와 같은가
- 스코핑: 기대 도메인 중 하나 이상이 스코프에 들어갔는가 (강제·전체 범위면 통과)
- 근거 재현율: 기대 근거 파일 중 검증된 인용으로 나온 비율
- 정답: 소형 모델 판정 (기대 답의 필수 사실을 포함했는가)
- 비용·지연·도구 호출 수
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

from askcode.llm.complete import LLM
from askcode.pipeline.answer import Answer, AnswerService

JUDGE_PROMPT = """\
당신은 채점자입니다. 기획자 질문에 대한 봇 답변이 기대 답의 핵심 사실을 맞게 담았는지 판정합니다.
- 핵심 수치·조건·화면 방식이 맞으면 통과입니다. 표현이 달라도 됩니다.
- 기대 답과 모순되는 사실이 있거나 핵심을 빠뜨리면 실패입니다.
- 채점 메모가 있으면 따릅니다.
JSON만 출력: {{"pass": true|false, "reason": "100자 이내 한 문장"}}

질문: {question}
기대 답: {expected}
채점 메모: {notes}
봇 답변: {answer}"""


@dataclass
class ItemResult:
    id: str
    type: str
    question: str
    expected_behavior: str
    behavior: str
    behavior_ok: bool
    scope_ok: bool | None
    scope_domains: list[str]
    evidence_recall: float | None
    correct: bool | None
    judge_reason: str
    confidence: str
    route: str
    cost_usd: float
    duration_s: float
    tool_calls: int
    cited: list[str] = field(default_factory=list)
    error: str | None = None


def load_golden(path: Path) -> list[dict]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


async def judge(llm: LLM, item: dict, answer: Answer) -> tuple[bool | None, str]:
    try:
        comp = await llm.complete(JUDGE_PROMPT.format(
            question=item["question"], expected=item.get("answer", ""), notes=item.get("notes", "-"),
            answer=answer.body[:3000]), tier="small", max_tokens=600)
        m = re.search(r"\{.*\}", comp.text, re.S)
        d = json.loads(m.group(0)) if m else {}
        if not isinstance(d.get("pass"), bool):
            # 잘리거나 형식이 틀린 판정을 오답으로 세지 않는다
            return None, "판정 실패: 응답 형식"
        return d["pass"], str(d.get("reason", ""))[:200]
    except Exception as exc:
        return None, f"판정 실패: {type(exc).__name__}"


async def evaluate_item(service: AnswerService, llm: LLM | None, item: dict) -> ItemResult:
    started = time.monotonic()
    a = await service.answer(item["question"], source="eval")
    behavior = a.status  # answer | clarify | refuse | error(API 사용 불가)
    expected = item.get("expected_behavior", "answer")
    exp_domains = set(item.get("expected_domains") or [])
    scope_domains = a.scope.domains if a.scope else []
    scope_ok = None if not exp_domains or behavior != "answer" else (not scope_domains or bool(exp_domains & set(scope_domains)))
    exp_paths = {e["path"] for e in item.get("evidence") or []}
    cited = sorted({c.path for c in a.citations})
    recall = (len(exp_paths & set(cited)) / len(exp_paths)) if exp_paths and behavior == "answer" else None
    correct, reason = (None, "")
    if behavior == "answer" and expected == "answer":
        correct, reason = await judge(llm, item, a) if llm else (None, "판정 생략(LLM 없음)")
    elif behavior == expected:
        correct, reason = True, "기대 행동과 일치"
    else:
        correct, reason = False, f"기대 행동 {expected}, 실제 {behavior}"
    return ItemResult(
        id=item["id"], type=item.get("type", ""), question=item["question"], expected_behavior=expected,
        behavior=behavior, behavior_ok=behavior == expected, scope_ok=scope_ok, scope_domains=scope_domains,
        evidence_recall=recall, correct=correct, judge_reason=reason, confidence=a.confidence, route=a.route,
        cost_usd=a.cost_usd, duration_s=round(time.monotonic() - started, 1), tool_calls=a.tool_calls, cited=cited,
        error=a.error,
    )


def _pct(xs: list[bool | None]) -> str:
    vals = [x for x in xs if x is not None]
    return f"{100 * sum(vals) / len(vals):.0f}% ({sum(vals)}/{len(vals)})" if vals else "-"


def summarize(results: list[ItemResult]) -> dict:
    recalls = [r.evidence_recall for r in results if r.evidence_recall is not None]
    costs = [r.cost_usd for r in results]
    return {
        "items": len(results),
        "correct": _pct([r.correct for r in results]),
        "behavior": _pct([r.behavior_ok for r in results]),
        "scope_recall": _pct([r.scope_ok for r in results]),
        "evidence_recall": f"{100 * sum(recalls) / len(recalls):.0f}%" if recalls else "-",
        "avg_cost_usd": round(sum(costs) / len(costs), 4) if costs else 0,
        "total_cost_usd": round(sum(costs), 4),
        "avg_duration_s": round(sum(r.duration_s for r in results) / len(results), 1) if results else 0,
        "avg_tool_calls": round(sum(r.tool_calls for r in results) / len(results), 1) if results else 0,
        "high_confidence": sum(r.confidence == "high" for r in results),
    }


def render_markdown(results: list[ItemResult], summary: dict, commit: str) -> str:
    out = [f"# AskCode 평가 결과", "", f"- 기준 커밋: `{commit[:7]}`", f"- 문항: {summary['items']}개", "",
           "| 지표 | 값 |", "| --- | --- |",
           f"| 정답률 (판정) | {summary['correct']} |",
           f"| 행동 일치 (답변·되묻기·거절) | {summary['behavior']} |",
           f"| 스코핑 재현율 | {summary['scope_recall']} |",
           f"| 근거 재현율 (기대 근거 파일 인용 비율) | {summary['evidence_recall']} |",
           f"| 질문당 평균 비용 | ${summary['avg_cost_usd']} |",
           f"| 전체 비용 | ${summary['total_cost_usd']} |",
           f"| 평균 응답 시간 | {summary['avg_duration_s']}초 |",
           f"| 평균 도구 호출 | {summary['avg_tool_calls']}회 |", "",
           "## 유형별", "", "| 유형 | 정답률 | 평균 비용 |", "| --- | --- | --- |"]
    for t in sorted({r.type for r in results}):
        rs = [r for r in results if r.type == t]
        out.append(f"| {t} | {_pct([r.correct for r in rs])} | ${sum(r.cost_usd for r in rs) / len(rs):.4f} |")
    out += ["", "## 문항별", "", "| id | 유형 | 행동 | 정답 | 스코프 | 근거 재현 | 확신도 | 경로 | 비용 | 시간 | 메모 |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    mark = lambda v: "-" if v is None else ("✅" if v else "❌")  # noqa: E731
    for r in results:
        rec = "-" if r.evidence_recall is None else f"{100 * r.evidence_recall:.0f}%"
        out.append(f"| {r.id} | {r.type} | {r.behavior} | {mark(r.correct)} | {mark(r.scope_ok)} | {rec} | {r.confidence} | "
                   f"{r.route} | ${r.cost_usd:.4f} | {r.duration_s}s | {(r.judge_reason or r.error or '').replace('|', '/')[:80]} |")
    return "\n".join(out) + "\n"


async def run_eval(service: AnswerService, llm: LLM | None, golden: list[dict], out_dir: Path, commit: str,
                   concurrency: int = 2) -> tuple[list[ItemResult], dict, Path]:
    sem = asyncio.Semaphore(concurrency)

    async def one(item):
        async with sem:
            return await evaluate_item(service, llm, item)

    results = await asyncio.gather(*(one(i) for i in golden))
    summary = summarize(results)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    md = out_dir / f"eval-{stamp}.md"
    md.write_text(render_markdown(results, summary, commit), encoding="utf-8")
    (out_dir / f"eval-{stamp}.json").write_text(
        json.dumps({"summary": summary, "results": [asdict(r) for r in results]}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    return results, summary, md
