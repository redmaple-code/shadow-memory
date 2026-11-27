"""Zero-token structural dissection helpers shared by every collector.

Nothing here calls an LLM.  Everything is regex + string surgery, which keeps
cost at exactly zero tokens while still yielding a usable session card:
title, summary, last-round conclusion, next steps, touched source files.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

# --- text cleaning -----------------------------------------------------------

_CODE_FENCE_RX = re.compile(r"```.*?```|~~~.*?~~~", re.S)
_INLINE_CODE_RX = re.compile(r"`[^`\n]{0,200}`")
_URL_RX = re.compile(r"\b(?:https?|ftp|ftps|ws|wss|file)://[^\s<>\"'\)\]]+", re.I)
_MD_LINK_RX = re.compile(r"!?\[([^\]\n]{0,120})\]\(([^)\n]{0,300})\)")
_HTML_TAG_RX = re.compile(r"<[^>\n]{1,200}>")
_MD_EMPH_RX = re.compile(r"(\*\*|__|\*|_|~~)(?=\S)(.+?)(?<=\S)\1")
_HEADING_RX = re.compile(r"^\s{0,3}#{1,6}\s*", re.M)
_LIST_BULLET_RX = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s+", re.M)
_WS_RX = re.compile(r"[ \t\u3000]+")
_MULTI_NL_RX = re.compile(r"\n{3,}")

_SENTENCE_SPLIT_RX = re.compile(r"(?<=[。！？!?；;])|(?<=[.])\s+")

_NEXT_STEP_RX = re.compile(
    r"(?im)^[^\n]{0,40}?(?:下一步|接下来|后续|待办|TODO|Next steps?|Next up|remaining)\s*[:：]?\s*(.+)$"
)

_TITLE_NOISE_RX = re.compile(
    r"^(?:请|麻烦|帮我|帮忙|帮忙看下|给我|能否|可以)?\s*(?:你)?\s*"
)
_TITLE_STRIP_CHARS = "。．.,，;；:：!！?？~～、 \t\r\n\"'`“”‘’「」《》"


def strip_code(text: str) -> str:
    """Remove fenced + inline code so conclusions are prose, not source."""
    if not text:
        return ""
    text = _CODE_FENCE_RX.sub(" ", text)
    return _INLINE_CODE_RX.sub(" ", text)


def strip_urls(text: str) -> str:
    if not text:
        return ""
    text = _MD_LINK_RX.sub(r"\1", text)
    return _URL_RX.sub(" ", text)


def clean_prose(text: str, *, keep_lines: bool = False) -> str:
    """Strip markdown/urls/code and collapse whitespace."""
    if not text:
        return ""
    out = strip_code(text)
    out = strip_urls(out)
    out = _HTML_TAG_RX.sub(" ", out)
    out = _MD_EMPH_RX.sub(r"\2", out)
    out = _HEADING_RX.sub("", out)
    out = _LIST_BULLET_RX.sub("", out)
    out = out.replace("\r\n", "\n").replace("\r", "\n")
    if not keep_lines:
        out = out.replace("\n", " ")
    out = _WS_RX.sub(" ", out)
    out = _MULTI_NL_RX.sub("\n\n", out)
    return out.strip()


def first_sentences(text: str, max_chars: int = 400) -> str:
    """Take whole sentences up to ``max_chars`` — never cut mid-sentence."""
    text = clean_prose(text)
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    parts = [p for p in _SENTENCE_SPLIT_RX.split(text) if p and p.strip()]
    out = ""
    for p in parts:
        if len(out) + len(p) > max_chars and out:
            break
        out += p
        if len(out) >= max_chars:
            break
    if not out:
        out = text[:max_chars].rsplit(" ", 1)[0]
    return out.strip()


def clean_title(raw: str, *, max_chars: int = 60) -> str:
    """Turn a first user message into a short, readable session title."""
    if not raw:
        return ""
    text = clean_prose(raw, keep_lines=False)
    if not text:
        return ""
    # strip stacked politeness prefixes ("请帮我……" → "……")
    for _ in range(3):
        stripped = _TITLE_NOISE_RX.sub("", text).strip()
        if stripped == text:
            break
        text = stripped
    # cut at the first natural terminator when it leaves a usable title
    for sep in ("。", "！", "？", "；", ". ", "，", ",", "、", "\n"):
        idx = text.find(sep)
        if 6 <= idx <= max_chars:
            text = text[:idx]
            break
    text = text.strip(_TITLE_STRIP_CHARS)
    if len(text) > max_chars:
        # cut on a natural boundary, then append an ellipsis
        cut = text[:max_chars]
        for sep in ("，", ",", " ", "、", "/", "·"):
            pos = cut.rfind(sep)
            if pos >= max_chars // 2:
                cut = cut[:pos]
                break
        text = cut.strip(_TITLE_STRIP_CHARS) + "…"
    return text.strip()


def extract_next_steps(text: str, max_chars: int = 240) -> str:
    if not text:
        return ""
    found: List[str] = []
    for m in _NEXT_STEP_RX.finditer(strip_code(text)):
        line = clean_prose(m.group(1))
        line = line.strip(" -:：")
        if line:
            found.append(line)
        if len(found) >= 3:
            break
    joined = "；".join(dict.fromkeys(found))
    return joined[:max_chars]


# --- file path extraction ----------------------------------------------------

_WIN_ABS_RX = re.compile(r"[A-Za-z]:[\\/][^\s\"'<>|*?\r\n\t]{1,300}")
_POSIX_ABS_RX = re.compile(r"(?<![\w.:/~])/(?:[A-Za-z0-9_.@+\-]+/){1,12}[A-Za-z0-9_.@+\-]*")

_KNOWN_EXTS = (
    "py pyi ipy js mjs cjs ts tsx jsx md markdown rst txt json jsonl toml ini cfg conf "
    "yaml yml xml csv tsv sql sh bash zsh fish ps1 bat cmd go rs java kt kts c h hpp cc cpp "
    "cs rb php swift m mm vue svelte astro html htm css scss sass less styl "
    "lua r jl dart ex exs erl hs ml clj scala gradle cmake mk dockerfile lock env "
    "png jpg jpeg gif svg webp pdf zip tar gz xlsx docx pptx db sqlite sqlite3 parquet "
    "npy npz h5 hdf5 mat inp oiv vtk stl obj"
).split()

_EXT_ALT = "|".join(sorted(set(_KNOWN_EXTS), key=len, reverse=True))

_REL_PATH_RX = re.compile(
    r"(?<![\w/.:@\-])"
    r"((?:\.{1,2}[\\/])?(?:[\w.@+\-]+[\\/]){1,12}[\w.@+\-]+\.(?:" + _EXT_ALT + r"))"
    r"(?![\w])",
    re.I,
)
_BARE_FILE_RX = re.compile(
    r"(?<![\w/.:@\-])([\w][\w.@+\-]{0,80}\.(?:" + _EXT_ALT + r"))(?![\w])",
    re.I,
)

_TRAILING_PUNCT = " \t\r\n,.;:!?)]}\"'`，。；：！？、）】》"


def _is_noise_path(p: str) -> bool:
    low = p.lower()
    if low.startswith(("http://", "https://", "ftp://")):
        return True
    if "@" in p and "/" not in p:  # an email, not a path
        return True
    # IP:port or version-like tokens
    if re.fullmatch(r"\d+(?:\.\d+)+", p):
        return True
    if re.fullmatch(r"v?\d+\.\d+[\w.\-]*", p):
        return True
    # pure extension e.g. "py" leftovers
    if p.startswith(".") and p.count(".") == 1 and len(p) < 6:
        return True
    return False


def _norm_rel(path: str, cwd: Optional[str]) -> str:
    p = path.replace("\\", "/").rstrip(_TRAILING_PUNCT)
    if cwd:
        try:
            base = str(cwd).replace("\\", "/").rstrip("/")
            lowp, lowb = p.lower(), base.lower()
            if lowp.startswith(lowb + "/"):
                return p[len(base) + 1 :]
        except Exception:  # pragma: no cover
            pass
    # collapse a leading "./" — it carries no information
    while p.startswith("./"):
        p = p[2:]
    return p


def extract_paths(
    text: str,
    cwd: Optional[str] = None,
    *,
    limit: int = 20,
    strip_code_blocks: bool = False,
) -> List[str]:
    """Extract source-ish paths from free text, dropping URLs and noise.

    Patterns are applied in specificity order and every match is blanked out of
    the working buffer, so an absolute path cannot also be harvested as a
    relative one (``G:/p/src/main.py`` must not reappear as ``p/src/main.py``).
    """
    if not text:
        return []
    scan = strip_urls(text)
    if strip_code_blocks:
        scan = _CODE_FENCE_RX.sub(" ", scan)

    found: List[str] = []
    seen: set = set()

    def add(p: str) -> bool:
        norm = _norm_rel(p, cwd)
        if not norm or _is_noise_path(norm):
            return False
        key = norm.lower()
        if key in seen:
            return True
        seen.add(key)
        found.append(norm)
        return True

    buf = scan
    for rx in (_WIN_ABS_RX, _POSIX_ABS_RX, _REL_PATH_RX):
        if len(found) >= limit:
            break
        spans: List[tuple] = []
        for m in rx.finditer(buf):
            add(m.group(0))
            spans.append(m.span())
        # blank matched regions so later patterns cannot re-harvest fragments
        if spans:
            chars = list(buf)
            for start, end in spans:
                for i in range(start, min(end, len(chars))):
                    chars[i] = " "
            buf = "".join(chars)

    if len(found) < limit:
        for m in _BARE_FILE_RX.finditer(buf):
            add(m.group(1))
            if len(found) >= limit:
                break
    return found[:limit]


# --- message normalisation ---------------------------------------------------

def _content_text(content: Any) -> str:
    """Flatten Claude / Codex / ZCode style content payloads into plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: List[str] = []
        for item in content:
            if isinstance(item, str):
                chunks.append(item)
            elif isinstance(item, dict):
                t = item.get("type")
                if t in ("text", "input_text", "output_text", "summary_text", "reasoning_text"):
                    val = item.get("text") or item.get("content") or ""
                    if isinstance(val, str):
                        chunks.append(val)
                elif t == "tool_result":
                    val = item.get("content")
                    if isinstance(val, str):
                        chunks.append(val)
                elif isinstance(item.get("text"), str):
                    chunks.append(item["text"])
        return "\n".join(c for c in chunks if c)
    if isinstance(content, dict):
        return _content_text([content])
    return ""


def normalise_messages(messages: Iterable[Any]) -> List[Dict[str, Any]]:
    """Coerce heterogeneous records into ``{role, text, ts}`` dicts."""
    out: List[Dict[str, Any]] = []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role") or m.get("type") or ""
        role = {"human": "user", "ai": "assistant", "model": "assistant"}.get(role, role)
        text = m.get("text")
        if not isinstance(text, str) or not text:
            text = _content_text(m.get("content") if "content" in m else m.get("message"))
        if not isinstance(text, str):
            text = ""
        if not text.strip():
            continue
        out.append({"role": role, "text": text, "ts": m.get("ts") or m.get("time_created")})
    return out


def extract_digest(
    messages: Sequence[Dict[str, Any]],
    cwd: Optional[str] = None,
    *,
    fallback_title: str = "",
) -> Dict[str, Any]:
    """Produce the zero-token session card consumed by ``session_digest``."""
    msgs = normalise_messages(messages)
    if not msgs:
        return {
            "clean_title": clean_title(fallback_title),
            "summary": "",
            "outcome": "",
            "next_steps": "",
            "touched_files": [],
            "message_count": 0,
        }

    first_user = next((m["text"] for m in msgs if m["role"] == "user"), "")
    title = clean_title(first_user) or clean_title(fallback_title)

    assistants = [m["text"] for m in msgs if m["role"] == "assistant"]
    last_assistant = assistants[-1] if assistants else ""

    outcome = first_sentences(strip_code(last_assistant), 400)
    if not outcome and assistants:
        outcome = first_sentences(assistants[-2] if len(assistants) > 1 else assistants[-1], 400)

    # summary: first user ask + last conclusion, compact
    head = first_sentences(first_user, 160)
    summary_bits = [b for b in (head, outcome) if b]
    summary = " → ".join(summary_bits)[:600]

    next_steps = extract_next_steps(last_assistant)

    files: List[str] = []
    seen: set = set()
    # long transcripts: scanning every message would dominate collection time,
    # so sample the opening (what the task was about) + the tail (what changed)
    path_msgs = msgs if len(msgs) <= 60 else (msgs[:10] + msgs[-50:])
    for m in path_msgs:
        for p in extract_paths(m["text"], cwd, limit=40, strip_code_blocks=False):
            k = p.lower()
            if k not in seen:
                seen.add(k)
                files.append(p)
    files = files[:20]

    return {
        "clean_title": title,
        "summary": summary,
        "outcome": outcome,
        "next_steps": next_steps,
        "touched_files": files,
        "message_count": len(msgs),
    }


# --- jsonl helpers -----------------------------------------------------------

def iter_jsonl(path: os.PathLike | str) -> Iterable[Dict[str, Any]]:
    """Yield parsed JSON objects from a .jsonl file, skipping broken lines."""
    p = Path(path)
    try:
        with p.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if isinstance(obj, dict):
                    yield obj
    except OSError:
        return


def best_cwd(cwd: Optional[str], fallback: Optional[str] = None) -> str:
    cand = (cwd or fallback or "").strip()
    if not cand:
        return ""
    return os.path.abspath(cand)


def rel_to_cwd(path: str, cwd: Optional[str]) -> str:
    return _norm_rel(path, cwd)