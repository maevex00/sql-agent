"""Glossary Layer: business terminology + metric catalog injected into the LLM
Query Planner's prompt.

Kept deliberately thin -- this is plain concatenation of two small Markdown
files, not retrieval. See ARCHITECTURE.md pipeline stage 2: the glossary
catalog for this demo is small enough to always include in full; a
retrieval/routing layer would be solving a problem this project doesn't have.
"""
from __future__ import annotations

from pathlib import Path

GLOSSARY_DIR = Path(__file__).parent.parent.parent / "glossary"


def load_glossary(glossary_dir: str | Path = GLOSSARY_DIR) -> str:
    """Concatenate every .md file in `glossary_dir`, sorted by filename for a
    stable, deterministic prompt (same glossary content -> same prompt text).
    """
    glossary_dir = Path(glossary_dir)
    parts = [p.read_text(encoding="utf-8") for p in sorted(glossary_dir.glob("*.md"))]
    return "\n\n---\n\n".join(parts)


ANSWER_SYSTEM_PROMPT = """Answer the user's question about what a business term or metric
means, using ONLY the glossary content provided below. If the glossary doesn't define the
term the user is asking about, say so plainly rather than guessing. Keep the answer to a
few sentences -- this is a definition lookup, not a report."""


def answer_metric_definition(question: str, glossary_text: str, *, model: str = "claude-sonnet-5", api_key: str | None = None) -> str:
    """Answer a METRIC_DEFINITION-intent question directly from the glossary,
    with no SQL involved. Grounded in `glossary_text` only, to avoid the LLM
    inventing a definition the glossary doesn't actually contain.

    Not covered by unit tests (live API call) -- see llm_planner.py /
    intent_router.py for the same pattern.
    """
    import os

    import anthropic

    client = anthropic.Anthropic(api_key=api_key or os.environ["ANTHROPIC_API_KEY"])
    response = client.messages.create(
        model=model,
        max_tokens=512,
        system=f"{ANSWER_SYSTEM_PROMPT}\n\n{glossary_text}",
        messages=[{"role": "user", "content": question}],
    )
    return next(block.text for block in response.content if block.type == "text")
