"""Versioned text rules shared by indexing, queries and evidence validation."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

import jieba

NORMALIZATION_VERSION = "nfc-unicode-whitespace-v1"


def canonical_text(value: str) -> str:
    """Canonical records use NFC but retain their original layout whitespace."""
    return unicodedata.normalize("NFC", value)


@dataclass(frozen=True)
class NormalizedText:
    text: str
    spans: tuple[tuple[int, int], ...]

    def source_span(self, start: int, end: int) -> tuple[int, int]:
        if not (0 <= start < end <= len(self.spans)):
            raise ValueError("引文区间越界")
        return self.spans[start][0], self.spans[end - 1][1]


def normalize_with_spans(canonical: str) -> NormalizedText:
    """Map normalized code points to a *NFC canonical* source, never UTF-16 offsets."""
    if canonical_text(canonical) != canonical:
        raise ValueError("证据坐标必须绑定 NFC 正式文本")
    output: list[str] = []
    spans: list[tuple[int, int]] = []
    index = 0
    while index < len(canonical):
        start = index
        char = canonical[index]
        index += 1
        if char.isspace():
            while index < len(canonical) and canonical[index].isspace():
                index += 1
            if output and index < len(canonical):
                output.append(" ")
                spans.append((start, index))
        else:
            output.append(char)
            spans.append((start, index))
    return NormalizedText("".join(output), tuple(spans))


def normalize(value: str) -> str:
    return normalize_with_spans(canonical_text(value)).text


class EvidenceError(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def locate_quote(
    canonical: str, quote: str, span_hint: tuple[int, int] | list[int] | None = None
) -> tuple[int, int]:
    haystack = normalize_with_spans(canonical)
    needle = normalize(quote)
    if not needle:
        raise EvidenceError("empty_quote")
    candidates: list[tuple[int, int]] = []
    offset = 0
    while (found := haystack.text.find(needle, offset)) != -1:
        candidates.append(haystack.source_span(found, found + len(needle)))
        offset = found + 1
    if not candidates:
        raise EvidenceError("normalized_quote_mismatch")
    if span_hint is not None:
        if len(span_hint) != 2 or not (0 <= span_hint[0] < span_hint[1] <= len(canonical)):
            raise EvidenceError("span_out_of_bounds")
        candidates = [span for span in candidates if span[0] >= span_hint[0] and span[1] <= span_hint[1]]
        if not candidates:
            raise EvidenceError("span_hint_mismatch")
    if len(candidates) != 1:
        raise EvidenceError("ambiguous_quote")
    return candidates[0]


@lru_cache(maxsize=1)
def tokenizer() -> jieba.Tokenizer:
    # One immutable dictionary configuration; aliases belong to the index, not add_word().
    instance = jieba.Tokenizer()
    instance.initialize()
    return instance


@lru_cache(maxsize=1)
def lexical_fingerprint() -> str:
    instance = tokenizer()
    with instance.get_dict_file() as dictionary:
        dictionary_hash = hashlib.sha256(dictionary.read()).hexdigest()
    config = {
        "normalization": NORMALIZATION_VERSION,
        "unicode": unicodedata.unidata_version,
        "jieba": importlib.metadata.version("jieba"),
        "dictionary": dictionary_hash,
        "hmm": False,
        "keyword_case": "casefold",
        "phrase_case": "sensitive",
    }
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()


def tokens(value: str, *, search: bool = True) -> list[str]:
    text = normalize(value).casefold()
    pieces = tokenizer().cut_for_search(text, HMM=False) if search else tokenizer().cut(text, HMM=False)
    return [piece for piece in pieces if piece.strip() and any(char.isalnum() for char in piece)]


def index_text(value: str) -> str:
    return " ".join(tokens(value))


def compile_match(value: str, mode: Literal["any", "all"] = "any") -> str | None:
    if mode not in {"any", "all"}:
        raise ValueError("未知关键词模式")
    words = list(dict.fromkeys(tokens(value, search=mode == "any")))
    if not words:
        return None
    literals = ['"' + word.replace('"', '""') + '"' for word in words]
    return (" OR " if mode == "any" else " AND ").join(literals)
