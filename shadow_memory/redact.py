"""Zero-dependency secret redactor.

Every piece of text that enters the shadow memory (facts, evidence, session
digests, inbox archives) must pass through :func:`redact` first.  The patterns
here are intentionally conservative: false positives (over-redaction) are
cheap, leaking a live credential is not.
"""

from __future__ import annotations

import re
from typing import List, Tuple

MASK = "[REDACTED]"


# --- individual pattern table -------------------------------------------------
# order matters: the most specific shapes run first so that e.g. an OpenAI key
# is not first eaten by the generic bearer-token rule.

_PATTERNS: List[Tuple[str, "re.Pattern[str]", str]] = [
    # PEM private key blocks (RSA / EC / OPENSSH / PGP / generic)
    (
        "private_key_block",
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----.*?-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----",
            re.S,
        ),
        "[REDACTED_PRIVATE_KEY]",
    ),
    # OpenAI / Anthropic / generic sk- style keys
    ("openai_sk", re.compile(r"\bsk-(?:ant-|proj-|live-|test-)?[A-Za-z0-9_\-]{16,}\b"), "sk-[REDACTED]"),
    # GitHub classic + fine grained + app tokens
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), "gh?_[REDACTED]"),
    ("github_fine_grained", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), "github_pat_[REDACTED]"),
    # AWS access key id / secret
    ("aws_akid", re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA|ANVA|AIPA)[A-Z0-9]{16}\b"), "AKIA[REDACTED]"),
    # Google API key
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), "AIza[REDACTED]"),
    # Slack tokens
    ("slack_token", re.compile(r"\bxox[abprs]-[0-9A-Za-z\-]{10,}\b"), "xox-[REDACTED]"),
    # Stripe
    ("stripe_key", re.compile(r"\b(?:sk|pk|rk)_(?:live|test)_[0-9A-Za-z]{10,}\b"), "[REDACTED_STRIPE_KEY]"),
    # JWT
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b"),
        "[REDACTED_JWT]",
    ),
    # Generic assignment:  password = "..."  / api_key: '...'  / token=...
    (
        "secret_assignment",
        re.compile(
            r"(?i)\b(api[_-]?key|secret[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|"
            r"password|passwd|pwd|private[_-]?key|session[_-]?token|refresh[_-]?token|token|"
            r"secret|credential|credentials)\b"
            r"(\s*[:=]\s*)"
            r"(['\"]?)([^\s'\"，,;]{6,})\3"
        ),
        r"\1\2 [REDACTED]",
    ),
    # Bearer <token>
    ("bearer", re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-+/=]{12,}"), r"\1 [REDACTED]"),
    # 身份证（18 位，含 X）
    (
        "cn_id_card",
        re.compile(r"(?<![0-9A-Za-z])[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?![0-9A-Za-z])"),
        "[REDACTED_ID_CARD]",
    ),
    # 中国大陆手机号（允许 +86 / 空格 / 连字符分隔）
    (
        "cn_mobile",
        re.compile(r"(?<!\d)(?:\+?86[-\s]?)?1[3-9]\d(?:[-\s]?\d{4}){2}(?!\d)"),
        "[REDACTED_MOBILE]",
    ),
    # 通用手机号（国际格式，谨慎：11 位以上连续数字）
    (
        "generic_phone",
        re.compile(r"(?<![\d.\-/])\+?\d{1,3}[-\s]\(?\d{2,4}\)?[-\s]\d{3,4}[-\s]\d{3,4}(?![\d.\-/])"),
        "[REDACTED_PHONE]",
    ),
    # 邮箱
    (
        "email",
        re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,24}\b"),
        "[REDACTED_EMAIL]",
    ),
    # QQ 号（5-11 位，仅在带 QQ 前缀时触发，避免误伤普通数字）
    ("qq_number", re.compile(r"(?i)\bqq\s*[:：]?\s*\d{5,11}\b"), "QQ: [REDACTED]"),
    # 微信 ID（wxid_ 前缀）
    ("wxid", re.compile(r"\bwxid_[A-Za-z0-9_\-]{4,}\b"), "[REDACTED_WXID]"),
]


# --- helpers ----------------------------------------------------------------

def _compile_path_patterns() -> List["re.Pattern[str]"]:
    return []


def find_secrets(text: str) -> List[Tuple[str, int, int]]:
    """Return ``[(rule_name, start, end), ...]`` for every match (debug/testing)."""
    hits: List[Tuple[str, int, int]] = []
    if not text:
        return hits
    for name, rx, _repl in _PATTERNS:
        for m in rx.finditer(text):
            hits.append((name, m.start(), m.end()))
    hits.sort(key=lambda h: h[1])
    return hits


def redact(text: str) -> str:
    """Return ``text`` with every recognised secret replaced by a mask."""
    if not text:
        return text
    out = text
    for _name, rx, repl in _PATTERNS:
        try:
            out = rx.sub(repl, out)
        except re.error:  # pragma: no cover - patterns are static
            continue
    return out


def redact_dict(obj):
    """Recursively redact every string value inside a JSON-ish structure."""
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, list):
        return [redact_dict(v) for v in obj]
    if isinstance(obj, tuple):
        return tuple(redact_dict(v) for v in obj)
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            # keys that unambiguously name a secret get masked wholesale
            out[k] = MASK if _is_secret_key(k) and isinstance(v, str) and v else redact_dict(v)
        return out
    return obj


_SECRET_KEY_WORDS = (
    "password", "passwd", "pwd", "secret", "token", "api_key", "apikey",
    "private_key", "access_key", "credential",
)


def _is_secret_key(key) -> bool:
    if not isinstance(key, str):
        return False
    low = key.lower().replace("-", "_")
    return any(w in low for w in _SECRET_KEY_WORDS)


def contains_secret(text: str) -> bool:
    """Cheap boolean used by the store to assert nothing leaked."""
    return bool(find_secrets(text))