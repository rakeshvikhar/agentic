"""
True A2A Orchestrator — uses A2APreviewTool so the LLM does the routing
========================================================================

Contrast with orchestrator.py (plain HTTP caller) vs this file:

  orchestrator.py         — YOUR Python code decides when/how to call the helpdesk
  orchestrator_a2a.py     — THE MODEL decides when to call the helpdesk agent as a tool

How it works
------------
1. We give the model an A2APreviewTool pointing at the helpdesk hosted agent.
2. The model sees "helpdesk_agent" as one of its tools.
3. When the user reports an issue, the model autonomously calls the helpdesk tool,
   gets back a response, and synthesises the final answer.
4. The Foundry platform handles the actual HTTP call to the sub-agent's /responses
   endpoint — we never write httpx.post() ourselves.

This is A2A: Agent (orchestrator) calls Agent (helpdesk) via the Responses protocol,
with the routing decision made by the LLM, not by hand-written control flow.

Requirements
------------
- The helpdesk hosted agent must be deployed and reachable.
- Set HELPDESK_AGENT_ENDPOINT in .env after running deploy.py.
- For local testing use http://localhost:8088 (run main.py first).

Run:
    python orchestrator_a2a.py --issue "VPN not working" --email "bob@corp.com"
"""
import argparse
import json
import os
import sys

from dotenv import load_dotenv
from azure.ai.projects.models import A2APreviewTool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agents"))
from auth import get_openai_client, get_project_client

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"), override=True)

MODEL = os.environ.get("AZURE_OPENAI_MODEL", "gpt-5.2")

# The deployed helpdesk hosted agent endpoint.
# After running deploy.py this will be something like:
#   https://<resource>.services.ai.azure.com/api/projects/<proj>/agents/<name>/endpoint
# For local dev: http://localhost:8088  (requires main.py to be running)
HELPDESK_AGENT_ENDPOINT = os.environ.get("HELPDESK_AGENT_ENDPOINT", "http://localhost:8088")

ORCHESTRATOR_INSTRUCTIONS = """
You are an IT support supervisor.

When a user reports an IT issue:
1. Use the helpdesk_agent tool to handle the full support workflow — it will
   create a ticket, diagnose the problem, and resolve it.
2. Once the helpdesk_agent responds, summarise the outcome for the user
   in plain language.

You do not solve technical problems yourself — you delegate to the helpdesk agent.
""".strip()


def build_a2a_tool() -> dict:
    """
    A2APreviewTool tells Foundry that 'helpdesk_agent' is another hosted agent.
    The platform routes calls to its /responses endpoint automatically.
    We never write the HTTP call ourselves — that's the point.
    """
    tool = A2APreviewTool(
        name="helpdesk_agent",
        description=(
            "IT helpdesk specialist agent. "
            "Call it with the user's issue and email. "
            "It creates tickets, diagnoses hardware/software problems, "
            "and sends notifications via MCP tools."
        ),
        base_url=HELPDESK_AGENT_ENDPOINT,
        # agent_card_path defaults to /.well-known/agent-card.json
        # The helpdesk agent must expose this if using strict A2A discovery.
        # For the Foundry Responses protocol, base_url alone is enough.
    )
    return tool.as_dict()


def run_a2a_session(user_issue: str, user_email: str):
    sep = "=" * 64
    print(f"\n{sep}")
    print("  TRUE A2A SESSION (LLM-driven routing)")
    print(f"  Helpdesk agent : {HELPDESK_AGENT_ENDPOINT}")
    print(f"  User           : {user_email}")
    print(f"  Issue          : {user_issue}")
    print(sep)

    project_client = get_project_client()
    oc = get_openai_client(project_client)

    a2a_tool = build_a2a_tool()
    print(f"\n[A2A tool definition]\n{json.dumps(a2a_tool, indent=2)}\n")

    user_message = (
        f"Hi, I'm {user_email}.\n"
        f"Issue: {user_issue}\n\n"
        "Please handle this end-to-end: create a ticket, diagnose the problem, "
        "and close it once resolved. Send me a notification."
    )

    print("[Calling orchestrator model — it will invoke helpdesk_agent via A2A...]")
    resp = oc.responses.create(
        model=MODEL,
        instructions=ORCHESTRATOR_INSTRUCTIONS,
        input=user_message,
        tools=[a2a_tool],
    )

    # Walk the output — the model may emit function_call + function_call_output
    # before the final message. Print each step so the A2A call is visible.
    for item in resp.output:
        item_type = getattr(item, "type", "")
        if item_type == "function_call":
            print(f"\n  [A2A CALL] model → helpdesk_agent")
            args = getattr(item, "arguments", "")
            try:
                print(f"  args: {json.dumps(json.loads(args), indent=4)}")
            except Exception:
                print(f"  args: {args}")
        elif item_type == "function_call_output":
            print(f"\n  [A2A RESPONSE] helpdesk_agent → model")
            out = getattr(item, "output", "")
            print(f"  {str(out)[:400]}")
        elif item_type == "message":
            role = getattr(item, "role", "")
            if role == "assistant":
                for c in getattr(item, "content", []):
                    if getattr(c, "type", "") == "output_text":
                        print(f"\n  ORCHESTRATOR FINAL ANSWER:\n  {c.text}")

    print(f"\n{sep}")
    print("  A2A SESSION COMPLETE")
    print(sep)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="True A2A orchestrator using A2APreviewTool")
    parser.add_argument("--issue", default="My VPN keeps disconnecting and I cannot reach internal tools.")
    parser.add_argument("--email", default="alice@corp.com")
    args = parser.parse_args()

    run_a2a_session(user_issue=args.issue, user_email=args.email)
