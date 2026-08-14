from __future__ import annotations

import math
import re
import time
from collections import defaultdict, deque
from collections.abc import Callable

from harnesslab.agentic.contracts import BudgetLimits, BudgetUsage


class BudgetExceeded(RuntimeError):
    pass


def estimate_tokens(text: str) -> int:
    """Conservative key-free estimator for preflight limits, including CJK text."""

    cjk = len(re.findall(r"[\u3400-\u9fff]", text))
    non_cjk = max(0, len(text) - cjk)
    return max(1, cjk + math.ceil(non_cjk / 4))


class BudgetLedger:
    def __init__(
        self,
        limits: BudgetLimits,
        *,
        input_price_per_million: float = 0.28,
        output_price_per_million: float = 0.42,
        tool_unit_price: float = 0.0001,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.limits = limits
        self.usage = BudgetUsage()
        self._clock = clock or time.monotonic
        self._started = self._clock()
        self._input_price = input_price_per_million
        self._output_price = output_price_per_million
        self._tool_price = tool_unit_price

    def charge_prompt(self, text: str, *, output_reserve: int = 0) -> int:
        tokens = estimate_tokens(text)
        next_input = self.usage.input_tokens + tokens
        next_output = self.usage.output_tokens + output_reserve
        if next_input > self.limits.max_input_tokens:
            raise BudgetExceeded(
                f"input token budget exceeded: {next_input}/{self.limits.max_input_tokens}"
            )
        if next_output > self.limits.max_output_tokens:
            raise BudgetExceeded(
                f"output token budget exceeded: {next_output}/{self.limits.max_output_tokens}"
            )
        self._assert_cost(next_input, next_output, self.usage.tool_cost_units)
        self.usage.input_tokens = next_input
        self.usage.output_tokens = next_output
        self._refresh_cost()
        return tokens

    def charge_tool(self, units: int) -> None:
        next_calls = self.usage.tool_calls + 1
        next_units = self.usage.tool_cost_units + units
        if next_calls > self.limits.max_tool_calls:
            raise BudgetExceeded(
                f"tool call budget exceeded: {next_calls}/{self.limits.max_tool_calls}"
            )
        self._assert_cost(self.usage.input_tokens, self.usage.output_tokens, next_units)
        self.usage.tool_calls = next_calls
        self.usage.tool_cost_units = next_units
        self._refresh_cost()
        self.check_wall_time()

    def check_wall_time(self) -> None:
        self.usage.elapsed_seconds = round(self._clock() - self._started, 6)
        if self.usage.elapsed_seconds > self.limits.max_wall_time_seconds:
            raise BudgetExceeded(
                "wall time budget exceeded: "
                f"{self.usage.elapsed_seconds:.3f}/{self.limits.max_wall_time_seconds:.3f}s"
            )

    def _assert_cost(self, input_tokens: int, output_tokens: int, tool_units: int) -> None:
        cost = self._cost(input_tokens, output_tokens, tool_units)
        if cost > self.limits.max_cost_usd:
            raise BudgetExceeded(
                f"cost budget exceeded: ${cost:.6f}/${self.limits.max_cost_usd:.6f}"
            )

    def _refresh_cost(self) -> None:
        self.usage.estimated_cost_usd = round(
            self._cost(
                self.usage.input_tokens,
                self.usage.output_tokens,
                self.usage.tool_cost_units,
            ),
            8,
        )
        self.usage.elapsed_seconds = round(self._clock() - self._started, 6)

    def _cost(self, input_tokens: int, output_tokens: int, tool_units: int) -> float:
        return (
            input_tokens / 1_000_000 * self._input_price
            + output_tokens / 1_000_000 * self._output_price
            + tool_units * self._tool_price
        )


class RateLimitExceeded(RuntimeError):
    pass


class SlidingWindowRateLimiter:
    def __init__(self, *, requests: int, window_seconds: float) -> None:
        self.requests = requests
        self.window_seconds = window_seconds
        self._entries: dict[str, deque[float]] = defaultdict(deque)

    def acquire(self, subject: str, *, now: float | None = None) -> int:
        timestamp = time.monotonic() if now is None else now
        entries = self._entries[subject]
        cutoff = timestamp - self.window_seconds
        while entries and entries[0] <= cutoff:
            entries.popleft()
        if len(entries) >= self.requests:
            retry_after = max(0.0, entries[0] + self.window_seconds - timestamp)
            raise RateLimitExceeded(f"rate limit exceeded; retry after {retry_after:.3f}s")
        entries.append(timestamp)
        return self.requests - len(entries)
