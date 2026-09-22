#!/usr/bin/env python
"""Slack Bolt app, Socket Mode -- no public URL/webhook needed, matching the
Feishu long-connection callback pattern this project's design was originally
modeled on.

Listens on `app_mention` (channel mentions) and direct messages, answers via
service.answer_question(), and replies in the same thread (`thread_ts`) so a
channel can hold multiple concurrent question threads without them
interleaving -- this is the "conversation context" mechanism; there is no
multi-turn refinement of a single QueryPlan across messages, since nothing
in the IR or repair loop supports that yet (would be new scope, not this
phase's job).

Requires SLACK_BOT_TOKEN, SLACK_APP_TOKEN, ANTHROPIC_API_KEY (DATABASE_URL
optional, see service.py). Not covered by the test suite -- needs a live
Slack app and workspace to exercise at all. What *is* tested: every pure
piece this module is thin glue around --
  - slack/formatting.py (Block Kit construction, mention stripping)
  - planner/repair.py's run_pipeline (orchestration, fake LLM callables)
  - report/analyzer.py (chart/summary orchestration, hand-built rows)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent.parent / ".env")
sys.path.insert(0, str(Path(__file__).parent.parent))

from joinplanner.sql_builder import SchemaCatalog  # noqa: E402
from planner.glossary import load_glossary  # noqa: E402
from service import answer_question  # noqa: E402
from slack.formatting import format_outcome_blocks, strip_mention  # noqa: E402

CORRELATION_PATH = Path(__file__).parent.parent.parent / "schema" / "correlation.json"

REQUIRED_ENV_VARS = ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "ANTHROPIC_API_KEY")


def build_app():
    from slack_bolt import App

    app = App(token=os.environ["SLACK_BOT_TOKEN"])
    schema = SchemaCatalog(CORRELATION_PATH)
    glossary_text = load_glossary()

    def handle_question(event: dict, say, client) -> None:
        question = strip_mention(event.get("text", ""))
        if not question:
            return
        thread_ts = event.get("thread_ts") or event.get("ts")

        outcome = answer_question(question, schema, glossary_text)
        blocks = format_outcome_blocks(question, outcome)
        say(blocks=blocks, text=outcome.text, thread_ts=thread_ts)

        if outcome.chart_path:
            client.files_upload_v2(
                channel=event["channel"],
                thread_ts=thread_ts,
                file=outcome.chart_path,
                title="result chart",
            )

    @app.event("app_mention")
    def on_mention(event, say, client):
        handle_question(event, say, client)

    @app.event("message")
    def on_direct_message(event, say, client):
        # Only plain DMs, and never react to the bot's own messages (avoids a
        # feedback loop where the bot's reply is itself treated as a question).
        if event.get("channel_type") == "im" and "bot_id" not in event and event.get("subtype") is None:
            handle_question(event, say, client)

    return app


def main() -> None:
    missing = [v for v in REQUIRED_ENV_VARS if v not in os.environ]
    if missing:
        print(f"Missing required environment variables: {', '.join(missing)}", file=sys.stderr)
        raise SystemExit(1)

    from slack_bolt.adapter.socket_mode import SocketModeHandler

    app = build_app()
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()


if __name__ == "__main__":
    main()
