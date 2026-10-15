"""스크린샷 역추적. 이미지에서 화면 문구를 읽어(개인정보는 가림) 코드 검색 질문으로 바꾼다.

이미지는 저장하지 않고, 추출한 문구만 질문 맥락으로 쓴다.
"""

from __future__ import annotations

import json
import re

from askcode.llm.complete import LLM

ALLOWED_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
MAX_BYTES = 5 * 1024 * 1024

EXTRACT_PROMPT = """\
이 앱/웹 화면 스크린샷에서 UI에 보이는 문구를 그대로 추출하세요.
- 버튼, 탭, 제목, 안내 문구, 빈 상태 메시지처럼 코드에 하드코딩됐을 법한 고정 문구를 우선합니다.
- 사람 이름, 전화번호, 이메일, 주소, 계정 정보처럼 사용자 데이터로 보이는 값은 "[가림]"으로 바꿉니다.
- 아래 JSON만 출력합니다.
{"screen_texts": ["문구1", "문구2", ...], "description": "어떤 화면인지 한 문장"}"""


async def extract_screen(llm: LLM, image: bytes, media_type: str) -> tuple[list[str], str, float]:
    if media_type not in ALLOWED_TYPES or len(image) > MAX_BYTES:
        return [], "", 0.0
    comp = await llm.vision(EXTRACT_PROMPT, image, media_type)
    m = re.search(r"\{.*\}", comp.text, re.S)
    data = json.loads(m.group(0)) if m else {}
    texts = [t for t in (data.get("screen_texts") or []) if isinstance(t, str) and t.strip() and "[가림]" not in t][:20]
    return texts, str(data.get("description") or "")[:200], 0.0


def screenshot_context(texts: list[str], description: str) -> str:
    quoted = ", ".join(f'"{t}"' for t in texts[:15])
    return (f"사용자가 화면 스크린샷을 첨부함. 화면 설명: {description or '-'}. 화면 문구: {quoted}. "
            "이 문구를 code_search로 찾아 해당 화면 코드를 특정한 뒤 답할 것.")
