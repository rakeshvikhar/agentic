"""
A2A Orchestrator — calls the deployed Helpdesk Hosted Agent via A2A protocol
=============================================================================
Demonstrates the Agent-to-Agent (A2A) pattern:
  - This orchestrator receives a user request
  - It forwards it to the deployed helpdesk hosted agent via HTTP (Responses protocol)
  - The helpdesk agent handles tool calls internally via MCP
  - The orchestrator collects and returns the final answer

The Responses protocol (POST /responses) IS the A2A transport in Foundry.
Each hosted agent's endpoint acts as an A2A peer.

Run:
    python orchestrator.py --issue "VPN not working" --email "bob@corp.com"
    python orchestrator.py --issue "Need new monitor" --email "alice@corp.com"
"""
import argparse
import os
import re

from dotenv import load_dotenv
from openai import OpenAI
from azure.identity import DefaultAzureCredential

load_dotenv()

AGENT_ENDPOINT = os.environ.get(
    "HELPDESK_AGENT_ENDPOINT",
    "http://localhost:8088",   # default: local dev server
)
FOUNDRY_API_KEY = os.environ.get("AZURE_AI_PROJECT_KEY", "")


def _make_openai_client() -> OpenAI:
    """
    Build an OpenAI client pointed at the agent's responses endpoint.

    Local:    base_url = http://localhost:8088
    Deployed: base_url = .../endpoint/protocols/openai
              auth     = Bearer token (Entra ID)
    """
    base = AGENT_ENDPOINT.rstrip("/")
    if "localhost" in base:
        return OpenAI(api_key="local", base_url=base)

    # Deployed: base URL is the openai-compatible sub-path
    base_url = base + "/protocols/openai"
    # Get Entra ID token
    try:
        token = DefaultAzureCredential().get_token("https://cognitiveservices.azure.com/.default").token
    except Exception:
        token = FOUNDRY_API_KEY  # fallback

    return OpenAI(
        api_key=token,
        base_url=base_url,
        default_headers={
            "api-key": FOUNDRY_API_KEY,
            "responses-protocol-version": "1.0.0",
        },
        default_query={"api-version": "v1"},
    )


def call_helpdesk_agent(message: str, thread_id: str | None = None) -> tuple[str, str]:
    """
    Call the hosted helpdesk agent via the Responses protocol (A2A).
    Returns (reply_text, response_id).
    Retries on session_not_ready (container cold-start).
    """
    import time
    client = _make_openai_client()
    kwargs: dict = {"model": "", "input": message}
    if thread_id:
        kwargs["previous_response_id"] = thread_id

    for attempt in range(5):
        try:
            resp = client.responses.create(**kwargs)
            text = getattr(resp, "output_text", "") or _extract_text(getattr(resp, "output", []))
            rid  = getattr(resp, "id", "") or getattr(resp, "response_id", "")
            return text, rid
        except Exception as e:
            if "session_not_ready" in str(e) and attempt < 4:
                wait = 20 * (attempt + 1)
                print(f"  Container cold-starting — retrying in {wait}s (attempt {attempt+1}/5)...")
                time.sleep(wait)
            else:
                raise


def _extract_text(output: list) -> str:
    """Extract assistant text from Responses protocol output array."""
    parts = []
    for item in output:
        if item.get("type") == "message" and item.get("role") == "assistant":
            for c in item.get("content", []):
                if c.get("type") == "output_text":
                    parts.append(c.get("text", ""))
    return "\n".join(parts)


def _parse_response(data: dict) -> tuple[str, str]:
    """Extract (text, response_id) from a Responses protocol response dict."""
    text = data.get("output_text") or _extract_text(data.get("output", []))
    rid  = data.get("id") or data.get("response_id", "")
    return text, rid


def run_helpdesk_session(user_issue: str, user_email: str):
    """
    Multi-turn A2A session:
      Turn 1: Send the issue → agent creates ticket and diagnoses
      Turn 2: Request resolution summary → agent closes and notifies
    """
    sep = "=" * 64
    print(f"\n{sep}")
    print(f"  A2A HELPDESK SESSION")
    print(f"  Target agent : {AGENT_ENDPOINT}")
    print(f"  User         : {user_email}")
    print(f"  Issue        : {user_issue}")
    print(sep)

    # ── Turn 1: Report the issue ───────────────────────────────────────────────
    print("\n[Turn 1] Sending issue to helpdesk agent via A2A...")
    msg1 = f"Hi, I'm {user_email}. Issue: {user_issue}"
    reply1, resp_id = call_helpdesk_agent(msg1)
    print(f"\n  AGENT:\n  {reply1}\n")

    ticket_id = _extract_ticket_id(reply1)
    print(f"  Detected ticket: {ticket_id}")

    # ── Turn 2: Request wrap-up (multi-turn, same thread) ─────────────────────
    print("[Turn 2] Requesting resolution and notification (same thread)...")
    msg2 = (
        f"Please finalise ticket {ticket_id}: "
        "close it with the resolution and send a notification email to the user."
    )
    reply2, _ = call_helpdesk_agent(msg2, thread_id=resp_id)
    print(f"\n  AGENT:\n  {reply2}\n")

    print(sep)
    print("  A2A SESSION COMPLETE")
    print(sep)


def _extract_ticket_id(text: str) -> str:
    match = re.search(r"TKT-[A-Z0-9]{6}", text)
    return match.group(0) if match else "TKT-UNKNOWN"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="A2A orchestrator for the helpdesk hosted agent")
    parser.add_argument("--issue", default="My VPN keeps disconnecting and I cannot reach internal tools.")
    parser.add_argument("--email", default="alice@corp.com")
    args = parser.parse_args()

    run_helpdesk_session(user_issue=args.issue, user_email=args.email)
