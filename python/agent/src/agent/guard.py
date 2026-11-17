"""Low-cost screening for instruction-like content crossing into the model."""

import re
import time
from dataclasses import dataclass


_INJECTION_PATTERNS = (
    re.compile(
        r"\b(?:ignore|disregard|forget|override|bypass)\b.{0,80}"
        r"\b(?:previous|prior|above|system|developer|policy|instructions?|rules?)\b",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"\b(?:reveal|print|expose|show|dump|exfiltrate)\b.{0,80}"
        r"\b(?:system prompt|credentials?|secrets?|private data)\b",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"\b(?:you are now|act as|pretend to be)\b.{0,60}"
        r"\b(?:admin|developer|system|unrestricted|root)\b",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"\b(?:call|invoke|execute|use)\b.{0,60}"
        r"\b(?:tool|function|mcp)\b.{0,60}\b(?:without|bypass|skip|ignore)\b",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"\b(?:refund|cancel|book|purchase|transfer)\b.{0,80}"
        r"\b(?:without|skip|bypass)\b.{0,40}\b(?:approval|confirmation|policy)\b",
        re.IGNORECASE | re.DOTALL,
    ),
)


@dataclass(frozen=True)
class GuardDecision:
    blocked: bool
    reason: str | None
    elapsed_ms: float


def inspect_untrusted_content(text: str) -> GuardDecision:
    """Block common instruction-override patterns without sending them to the model."""
    started = time.perf_counter()
    reason = next(
        (
            "instruction-like content matched a local screening rule"
            for pattern in _INJECTION_PATTERNS
            if pattern.search(text)
        ),
        None,
    )
    return GuardDecision(
        blocked=reason is not None,
        reason=reason,
        elapsed_ms=(time.perf_counter() - started) * 1000,
    )
