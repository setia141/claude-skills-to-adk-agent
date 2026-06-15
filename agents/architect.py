"""
agents/architect.py — Architect Agent.
Produces an ADR from parsed skills + user's agent intent.
Also provides run_adr_chat() for the conversational ADR refinement interface.
"""

import json
import logging
import re
from agents.claude_sdk import ask, ask_json, load_prompt

log = logging.getLogger(__name__)

ARCHITECT_SYSTEM = load_prompt("architect")
ADR_CHAT_SYSTEM  = load_prompt("adr_chat")


def run_architect(parsed_skills: list[dict],
                  agent_intent: str = "",
                  adk_choice: str = "Google ADK") -> dict:
    """
    Run the Architect Agent.

    Args:
        parsed_skills: List of parsed skill dicts.
        agent_intent:  User's description of what they want the agent to do.
        adk_choice:    Target ADK framework name.
    """
    log.info(f"[architect] Analyzing {len(parsed_skills)} skills, intent='{agent_intent[:60]}'")

    user = (
        f"## Parsed Skills\n{json.dumps(parsed_skills, indent=2)}\n\n"
        f"## Agent Intent (what the user wants this agent to do)\n"
        f"{agent_intent or 'Not specified — infer from skills.'}\n\n"
        f"## Target ADK\n{adk_choice}"
    )

    adr = ask_json(ARCHITECT_SYSTEM, user, tag="architect")
    log.info(f"[architect] topology={adr['decisions']['topology']['choice']}, "
             f"confidence={adr['confidence']}")
    return adr


def run_adr_chat(
    current_adr: dict,
    conversation: list[dict],
    user_message: str,
) -> dict:
    """
    One turn of the ADR conversational interface.

    Two-phase: Claude responds first (may ask follow-up questions), and only
    includes a <proposed_adr> block when it has enough information to commit
    to a change. The caller decides whether to apply it.

    Args:
        current_adr:  Current ADR dict shown in the UI.
        conversation: Prior turns [{role, content}]. Capped to last 10.
        user_message: Developer's new message.

    Returns:
        {
            "response":     str,        # conversational reply to display
            "proposed_adr": dict|None,  # complete new ADR, or None
        }
    """
    history = conversation[-10:] if conversation else []

    history_str = ""
    if history:
        lines = []
        for turn in history:
            label = "Claude" if turn["role"] == "assistant" else "Developer"
            lines.append(f"{label}: {turn['content']}")
        history_str = "## Conversation History\n" + "\n\n".join(lines) + "\n\n"

    user_content = (
        f"## Current ADR\n{json.dumps(current_adr, indent=2)}\n\n"
        + history_str
        + f"## Developer's Message\n{user_message}"
    )

    raw = ask(ADR_CHAT_SYSTEM, user_content, tag="adr_chat")

    # Extract <response> block
    resp_match = re.search(r"<response>(.*?)</response>", raw, re.DOTALL)
    response_text = resp_match.group(1).strip() if resp_match else raw.strip()

    # Extract optional <proposed_adr> block
    proposed_adr = None
    adr_match = re.search(r"<proposed_adr>(.*?)</proposed_adr>", raw, re.DOTALL)
    if adr_match:
        raw_json = adr_match.group(1).strip()
        # Strip markdown code fences if Claude wrapped the JSON
        raw_json = re.sub(r"^```[a-z]*\n?", "", raw_json, flags=re.MULTILINE)
        raw_json = re.sub(r"\n?```\s*$", "", raw_json, flags=re.MULTILINE)
        try:
            proposed_adr = json.loads(raw_json)
        except Exception as e:
            log.warning(f"[adr_chat] proposed_adr JSON parse failed: {e}")
            response_text += (
                "\n\n(I tried to propose changes but produced invalid JSON. "
                "Please ask me to try again or be more specific.)"
            )

    log.info(f"[adr_chat] proposed={'yes' if proposed_adr else 'no'}, "
             f"response_len={len(response_text)}")
    return {"response": response_text, "proposed_adr": proposed_adr}
