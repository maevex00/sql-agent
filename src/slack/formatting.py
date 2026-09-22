"""Pure Slack Block Kit formatting -- no network, no Slack SDK import, fully
unit-testable. slack/app.py is the only module in this package that actually
talks to Slack; everything about *what* gets said is decided here so it can
be tested without a live workspace.
"""
from __future__ import annotations

import re
from typing import Any

from service import AnswerOutcome

_MENTION_RE = re.compile(r"<@[A-Z0-9]+>\s*")


def strip_mention(text: str) -> str:
    """Remove a leading Slack user-mention token (e.g. `<@U123ABC> `) from an
    app_mention event's text, leaving the actual question.
    """
    return _MENTION_RE.sub("", text).strip()


def format_outcome_blocks(question: str, outcome: AnswerOutcome) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Q:* {question}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": outcome.text}},
    ]
    if outcome.repair_log:
        log_text = "\n".join(f"- {line}" for line in outcome.repair_log)
        blocks.append(
            {"type": "context", "elements": [{"type": "mrkdwn", "text": f"_repairs applied:_\n{log_text}"}]}
        )
    return blocks
