"""토큰 → 비용(USD). 비용은 SDK 보고값이 아니라 여기서 토큰 × 공식 단가로 계산한다.

Agent SDK의 total_cost_usd는 실측에서 토큰 × 공식 단가보다 작게 나왔다(sonnet-5-5 약 15%, haiku-5-5는 더 크게).
토큰 수는 Console 로그와 정확히 일치하므로 토큰을 기준으로 삼는다.
캐시 쓰기는 5분 TTL(입력 단가 × 1.25), 캐시 읽기는 입력 단가 × 0.1로 본다.
"""

from __future__ import annotations

# 모델 → (입력, 출력) USD / 100만 토큰. 가격이 바뀌면 여기만 고친다.
PRICES: dict[str, tuple[float, float]] = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
}
CACHE_WRITE = 1.25
CACHE_READ = 0.10


def estimate_cost(model: str | None, input_tokens: int, output_tokens: int,
                  cache_read_tokens: int = 0, cache_write_tokens: int = 0) -> float | None:
    """단가표에 없는 모델이면 None (호출한 쪽이 SDK 보고값 등으로 대체)."""
    price = PRICES.get(model or "")
    if not price:
        return None
    pin, pout = price
    return ((input_tokens + cache_write_tokens * CACHE_WRITE + cache_read_tokens * CACHE_READ) * pin
            + output_tokens * pout) / 1_000_000
