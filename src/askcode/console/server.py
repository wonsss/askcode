"""웹 콘솔 서버(Starlette). 로컬 전용(127.0.0.1), 읽기 전용, 외부 리소스 없음."""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse
from starlette.routing import Route

from askcode.console import views
from askcode.console.data import ConsoleData, compare_runs

CSP = ("default-src 'none'; style-src 'unsafe-inline'; img-src data:; form-action 'self'; "
       "base-uri 'none'; frame-ancestors 'none'")
FILTER_KEYS = ("q", "domain", "route", "confidence", "feedback", "days", "source", "ops", "reason")


class SecurityHeaders(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"  # GitHub 링크로 콘솔 주소를 넘기지 않는다
        response.headers["Cache-Control"] = "no-store"
        return response


def _params(request: Request) -> dict:
    return {k: request.query_params[k] for k in FILTER_KEYS if request.query_params.get(k)}


def create_app(data: ConsoleData) -> Starlette:
    names = data.domain_names

    async def home(request: Request):
        recent = [r for r in data.rows("slack") if r.has_answer][:10]
        return HTMLResponse(views.home(data.stats(7), recent, len(data.needs_check()), names))

    async def search(request: Request):
        p = _params(request)
        source = p.get("source") or ("all" if p.get("ops") == "1" else "slack")
        rows = data.search(p.get("q", ""), source=source, domain=p.get("domain", ""), route=p.get("route", ""),
                           confidence=p.get("confidence", ""), feedback=p.get("feedback", ""),
                           days=int(p["days"]) if p.get("days", "").isdigit() else 0)
        return HTMLResponse(views.search_page({**p, "source": source}, rows, names))

    async def question(request: Request):
        row = data.get(request.path_params["qid"])
        if not row:
            return HTMLResponse(views.not_found("질문을 찾을 수 없어요"), status_code=404)
        cites = data.citations(row)
        view = request.query_params.get("view", "")
        return HTMLResponse(views.detail(row, view, cites, data.freshness(row, cites), data.latest_commit(),
                                         data.tool_steps(row.id) if view == "ops" else [],
                                         data.usage(row.id), data.feedback_events(row.id), names))

    async def check(request: Request):
        p = _params(request)
        return HTMLResponse(views.check_page(data.needs_check(p.get("source", "slack")), names, p))

    async def evals(request: Request):
        return HTMLResponse(views.eval_page(data.eval_runs()))

    async def eval_run(request: Request):
        run = data.eval_run(request.path_params["name"])
        if not run:
            return HTMLResponse(views.not_found("평가 회차를 찾을 수 없어요"), status_code=404)
        return HTMLResponse(views.eval_run_page(run))

    async def eval_compare(request: Request):
        a, b = data.eval_run(request.query_params.get("a", "")), data.eval_run(request.query_params.get("b", ""))
        if not (a and b):
            return HTMLResponse(views.not_found("비교할 회차를 찾을 수 없어요"), status_code=404)
        return HTMLResponse(views.compare_page(a, b, compare_runs(a, b)))

    routes = [
        Route("/", home), Route("/search", search), Route("/q/{qid}", question), Route("/check", check),
        Route("/eval", evals), Route("/eval/compare", eval_compare), Route("/eval/{name}", eval_run),
    ]
    return Starlette(routes=routes, middleware=[Middleware(SecurityHeaders)])
