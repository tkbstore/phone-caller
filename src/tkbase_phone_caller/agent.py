"""LiveKit voice agent with OpenAI Realtime API.

This module defines the AI sales agent that handles phone conversations.
It uses OpenAI's Realtime API for low-latency speech-to-speech interaction.

Run modes:
    python -m tkbase_phone_caller.agent console   # Local mic/speaker test
    python -m tkbase_phone_caller.agent dev        # Development server
    python -m tkbase_phone_caller.agent start      # Production

Requires environment variables (in ~/.config/tkbase/livekit_credentials.env):
    LIVEKIT_URL=wss://xxx.livekit.cloud
    LIVEKIT_API_KEY=...
    LIVEKIT_API_SECRET=...
    OPENAI_API_KEY=sk-...
"""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv

from livekit.agents import Agent, AgentSession, AgentServer, JobContext
import livekit.agents as agents
from livekit.plugins import openai

from .prompts import build_sales_prompt

# Load credentials from .env in project root (primary)
# Falls back to ~/.config/tkbase/livekit_credentials.env
load_dotenv()  # loads .env from cwd
_FALLBACK_ENV = Path.home() / ".config" / "tkbase" / "livekit_credentials.env"
if _FALLBACK_ENV.exists():
    load_dotenv(_FALLBACK_ENV, override=False)


class SalesAgent(Agent):
    """AI sales agent for phone calls.

    Uses OpenAI Realtime API (gpt-4o-mini-realtime) for
    low-latency speech-to-speech conversation.

    Args:
        company_name: Company making the call.
        product_name: Product/service being offered.
        caller_name: Name to introduce as.
        purpose: Short description of call purpose.
        system_prompt: Full custom system prompt (overrides other args).
        voice: OpenAI Realtime voice ID.
    """

    def __init__(
        self,
        company_name: str = "",
        product_name: str = "",
        caller_name: str = "",
        purpose: str = "",
        system_prompt: str | None = None,
        voice: str = "alloy",
        model: str = "gpt-realtime-mini",
    ):
        if system_prompt is None:
            system_prompt = build_sales_prompt(
                purpose=purpose,
                company_name=company_name,
                product_name=product_name,
                caller_name=caller_name,
            )

        import os
        api_key = os.environ.get("OPENAI_API_KEY", "")

        llm = None
        if api_key:
            llm = openai.realtime.RealtimeModel(
                model=model,
                voice=voice,
                temperature=0.6,
            )

        super().__init__(
            instructions=system_prompt,
            llm=llm,
        )


# --- Server setup ---

server = AgentServer()


@server.rtc_session(agent_name="sales-agent")
async def sales_agent_session(ctx: JobContext) -> None:
    """Handle an inbound or outbound call session."""
    # Extract agent config from room metadata if available
    room = ctx.room
    metadata = room.metadata or ""

    # Default agent
    agent = SalesAgent()

    session = AgentSession()
    await session.start(agent=agent, room=room)

    # For inbound calls, greet immediately
    await session.generate_reply(
        instructions="相手に挨拶して、用件を簡潔に伝えてください。"
    )


def run() -> None:
    """Entry point for `python -m tkbase_phone_caller.agent`."""
    agents.cli.run_app(server)


if __name__ == "__main__":
    run()
