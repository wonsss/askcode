"""웹 콘솔 서버(Starlette). 로컬 전용(127.0.0.1), 외부 리소스 없음.

감사 로그는 읽기 전용 연결로 읽는다. 유일한 쓰기는 확인 필요 처리 기록(resolutions 추가)이며,
resolver를 넘겼을 때만 켜지고 CSRF 토큰·같은 출처 확인을 통과해야 한다.
"""

from __future__ import annotations

import hmac
import secrets
from collections.abc import Callable
from urllib.parse import parse_qs

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse
from starlette.routing import Route

from askcode.console import views
from askcode.console.data import ConsoleData, compare_runs

CSP = ("default-src 'none'; style-src 'unsafe-inline'; img-src data:; form-action 'self'; "
       "base-uri 'none'; frame-ancestors 'none'")
FILTER_KEYS = ("q", "domain", "route", "confidence", "feedback", "days", "source", "ops", "reason", "resolved")


class SecurityHeaders(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"  # GitHub 링크로 콘솔 주소를 넘기지 않는다
        response.headers["Cache-Control"] = "no-store"
        return response


# Cloudflare Access가 인증한 사용자 이메일. 터널 밖(127.0.0.1)에서 연 경우엔 없다.
ACCESS_EMAIL_HEADER = "cf-access-authenticated-user-email"
Resolver = Callable[[str, str, str, bool], bool]  # (query_id, operator, note, reopen) -> 찾았는지


def _same_origin(request: Request) -> bool:
    """브라우저가 보낸 출처가 이 콘솔인지. Sec-Fetch-Site·Origin 중 있는 것으로 판단한다."""
    site = request.headers.get("sec-fetch-site")
    if site and site not in ("same-origin", "none"):
        return False
    origin = request.headers.get("origin")
    if origin and origin != "null":
        host = request.headers.get("host", "")
        return origin.split("://", 1)[-1] == host
    return True


def _params(request: Request) -> dict:
    return {k: request.query_params[k] for k in FILTER_KEYS if request.query_params.get(k)}


def create_app(data: ConsoleData, resolver: Resolver | None = None) -> Starlette:
    names = data.domain_names
    # 프로세스마다 새 비밀값으로 만든 토큰. 콘솔이 재시작되면 열려 있던 폼은 다시 불러와야 한다.
    csrf = hmac.new(secrets.token_bytes(32), b"askcode-console-resolve", "sha256").hexdigest() if resolver else ""

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
                                         data.usage(row.id), data.feedback_events(row.id), names, csrf))

    async def check(request: Request):
        p = _params(request)
        src = p.get("source", "slack")
        done = data.needs_check(src, resolved=True)
        rows = done if p.get("resolved") == "1" else data.needs_check(src)
        return HTMLResponse(views.check_page(rows, names, p, len(done), csrf))

    async def resolve(request: Request):
        if not resolver:
            return HTMLResponse(views.not_found("이 콘솔은 읽기 전용이에요"), status_code=403)
        if not _same_origin(request):
            return HTMLResponse(views.not_found("다른 사이트에서 온 요청은 받지 않아요"), status_code=403)
        body = await request.body()
        if len(body) > 4096:
            return HTMLResponse(views.not_found("요청이 너무 커요"), status_code=413)
        form = {k: v[0] for k, v in parse_qs(body.decode("utf-8", "replace")).items()}
        if not hmac.compare_digest(form.get("csrf", ""), csrf):
            return HTMLResponse(views.not_found("페이지가 오래됐어요. 새로고침 후 다시 눌러 주세요"), status_code=403)
        action = form.get("action", "")
        if action not in ("resolve", "reopen"):
            return HTMLResponse(views.not_found("알 수 없는 동작이에요"), status_code=400)
        qid = request.path_params["qid"]
        operator = request.headers.get(ACCESS_EMAIL_HEADER) or "console(local)"
        note = " ".join(form.get("note", "").split())[:200]
        if not resolver(qid, operator, note, action == "reopen"):
            return HTMLResponse(views.not_found("질문을 찾을 수 없어요"), status_code=404)
        back = form.get("back", "")
        # 돌아갈 곳은 콘솔 안의 /check만 허용한다(열린 리다이렉트 방지)
        target = back if back.startswith("/check") and "//" not in back else f"/q/{qid}"
        return RedirectResponse(target, status_code=303)

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
        Route("/q/{qid}/resolve", resolve, methods=["POST"]),
        Route("/eval", evals), Route("/eval/compare", eval_compare), Route("/eval/{name}", eval_run),
    ]
    return Starlette(routes=routes, middleware=[Middleware(SecurityHeaders)])
