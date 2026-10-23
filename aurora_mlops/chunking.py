"""Transcript chunking strategy for the RAG corpus.

Redacted transcripts are split on speaker-turn boundaries and packed into
chunks of at most `max_tokens` (A-TRN-04), carrying the last turn of the
previous chunk as overlap so a question and its answer are not separated.
A turn longer than the limit is split on sentence boundaries. Chunk IDs are
deterministic (ticket, index, redaction version), matching the contract's
chunk_id definition in contracts/schemas/transcript_chunks.yaml.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

TURN = re.compile(r"^(customer|agent|system)\s*:", re.IGNORECASE)
SENTENCE = re.compile(r"(?<=[.!?])\s+")
BOILERPLATE = re.compile(
    r"(thank you for contacting aurora.*|this message was sent from.*|sent from my \w+)$", re.IGNORECASE
)


def approx_tokens(text: str) -> int:
    """~4 characters per token; replace with the embedding model's tokenizer once chosen (A-TRN-02)."""
    return max(1, round(len(text) / 4))


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    ticket_id: str
    chunk_index: int
    text: str
    token_count: int


def split_turns(transcript: str) -> list[str]:
    turns: list[str] = []
    for line in transcript.splitlines():
        line = BOILERPLATE.sub("", line.strip()).strip()
        if not line:
            continue
        if TURN.match(line) or not turns:
            turns.append(line)
        else:
            turns[-1] += " " + line
    return turns


def _split_long(turn: str, max_tokens: int, count: Callable[[str], int]) -> list[str]:
    if count(turn) <= max_tokens:
        return [turn]
    parts, cur = [], ""
    for sentence in SENTENCE.split(turn):
        candidate = f"{cur} {sentence}".strip()
        if cur and count(candidate) > max_tokens:
            parts.append(cur)
            cur = sentence
        else:
            cur = candidate
    if cur:
        parts.append(cur)
    return parts


def chunk_id(ticket_id: str, index: int, redaction_version: str) -> str:
    return hashlib.sha256(f"{ticket_id}|{index}|{redaction_version}".encode()).hexdigest()[:32]


def chunk_transcript(
    ticket_id: str,
    transcript: str,
    redaction_version: str,
    max_tokens: int = 512,
    min_tokens: int = 50,
    overlap_turns: int = 1,
    count: Callable[[str], int] = approx_tokens,
) -> list[Chunk]:
    pieces: list[str] = [p for t in split_turns(transcript) for p in _split_long(t, max_tokens, count)]
    groups: list[list[str]] = []
    cur: list[str] = []
    for piece in pieces:
        if cur and count(" ".join([*cur, piece])) > max_tokens:
            groups.append(cur)
            carry = cur[-overlap_turns:] if overlap_turns else []
            cur = carry if count(" ".join([*carry, piece])) <= max_tokens else []
        cur.append(piece)
    if cur:
        # Fold a tiny tail into the previous chunk when it still fits.
        if groups and count(" ".join(cur)) < min_tokens and count(" ".join(groups[-1] + cur)) <= max_tokens:
            groups[-1].extend(p for p in cur if p not in groups[-1])
        else:
            groups.append(cur)
    return [
        Chunk(chunk_id(ticket_id, i, redaction_version), ticket_id, i, text, count(text))
        for i, text in enumerate(" ".join(g) for g in groups)
    ]


def chunk_corpus(tickets: Sequence[tuple[str, str]], redaction_version: str, **kwargs) -> list[Chunk]:
    return [c for tid, text in tickets for c in chunk_transcript(tid, text, redaction_version, **kwargs)]
