"""
Multi-agent IT Helpdesk session via Azure AI Foundry.

Tool mode (set via .env or --tools flag):
  function  (default) -- client-side function calling; works with any model,
                         no public MCP server needed. Great for local dev.
  mcp                 -- server-side MCP; requires MCP_SERVER_URL to be a
                         publicly reachable URL (Azure Function App).

Usage:
    python agents/run.py
    python agents/run.py --issue "My monitor stopped working" --email "bob@corp.com"
    python agents/run.py --tools mcp   (requires deployed Azure Function)
"""
import argparse
import json
import os
import re
import sys
from typing import Any

from openai import OpenAI
from dotenv import load_dotenv
from azure.ai.projects.models import MCPTool

# Local ticket store for function-calling mode
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mcp_server"))
sys.path.insert(0, os.path.dirname(__file__))
from auth import get_project_client, get_openai_client
import ticket_store as ts

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"), override=True)

MODEL          = os.environ.get("AZURE_OPENAI_MODEL", "gpt-4o")
MCP_SERVER_URL = os.environ.get("MCP_SERVER_URL", "http://localhost:7071/api/mcp")
MCP_SERVER_KEY = os.environ.get("MCP_SERVER_KEY", "")

# ── Agent instructions ─────────────────────────────────────────────────────────
INSTRUCTIONS = {
    "orchestrator": (
        "You are the IT Helpdesk Orchestrator. "
        "1) Use create_ticket to log the issue (priority: low/medium/high/critical). "
        "2) Tell the user their ticket ID. "
        "3) Categorise: hardware/equipment -> Asset Mgmt; software/VPN/network -> Tech Support. "
        "Be concise and always confirm the ticket ID."
    ),
    "tech_support": (
        "You are the Tech Support specialist. "
        "1) Use get_ticket_status to read the issue. "
        "2) Use assign_ticket to assign to 'Tech Support Team'. "
        "3) Use log_event for each diagnostic step. "
        "4) Use close_ticket with a clear resolution."
    ),
    "asset_mgmt": (
        "You are the Asset Management specialist. "
        "1) Use get_ticket_status to read the issue. "
        "2) Use assign_ticket to assign to 'Asset Management Team'. "
        "3) Use get_asset_info to check assets (IDs: A001-A004). "
        "4) Log with log_event. Close with close_ticket."
    ),
    "notifier": (
        "You are the Notification specialist. "
        "Use send_notification to send a brief friendly resolution email. "
        "Include: ticket ID, resolution, follow-up steps."
    ),
}

# ── Function tool schema (for function-calling mode) ──────────────────────────
FUNCTION_TOOLS = [
    {
        "type": "function",
        "name": "create_ticket",
        "description": "Create a new IT helpdesk ticket",
        "parameters": {
            "type": "object",
            "properties": {
                "title":       {"type": "string"},
                "description": {"type": "string"},
                "priority":    {"type": "string", "enum": ["low", "medium", "high", "critical"]},
            },
            "required": ["title", "description", "priority"],
        },
    },
    {
        "type": "function",
        "name": "get_ticket_status",
        "description": "Get current status and details of a ticket",
        "parameters": {
            "type": "object",
            "properties": {"ticket_id": {"type": "string"}},
            "required": ["ticket_id"],
        },
    },
    {
        "type": "function",
        "name": "get_asset_info",
        "description": "Look up an IT asset by its ID (A001, A002, A003, A004)",
        "parameters": {
            "type": "object",
            "properties": {"asset_id": {"type": "string"}},
            "required": ["asset_id"],
        },
    },
    {
        "type": "function",
        "name": "assign_ticket",
        "description": "Assign a ticket to a support agent",
        "parameters": {
            "type": "object",
            "properties": {
                "ticket_id":  {"type": "string"},
                "agent_name": {"type": "string"},
            },
            "required": ["ticket_id", "agent_name"],
        },
    },
    {
        "type": "function",
        "name": "close_ticket",
        "description": "Close a ticket with a resolution note",
        "parameters": {
            "type": "object",
            "properties": {
                "ticket_id":  {"type": "string"},
                "resolution": {"type": "string"},
            },
            "required": ["ticket_id", "resolution"],
        },
    },
    {
        "type": "function",
        "name": "log_event",
        "description": "Log a diagnostic step or note to a ticket",
        "parameters": {
            "type": "object",
            "properties": {
                "ticket_id": {"type": "string"},
                "event":     {"type": "string"},
            },
            "required": ["ticket_id", "event"],
        },
    },
    {
        "type": "function",
        "name": "send_notification",
        "description": "Send an email notification to a user",
        "parameters": {
            "type": "object",
            "properties": {
                "recipient": {"type": "string"},
                "subject":   {"type": "string"},
                "message":   {"type": "string"},
            },
            "required": ["recipient", "subject", "message"],
        },
    },
]

# Local function dispatch (function-calling mode)
TOOL_DISPATCH = {
    "create_ticket":     ts.create_ticket,
    "get_ticket_status": ts.get_ticket_status,
    "get_asset_info":    ts.get_asset_info,
    "assign_ticket":     ts.assign_ticket,
    "close_ticket":      ts.close_ticket,
    "log_event":         ts.log_event,
    "send_notification": ts.send_notification,
}


def _dispatch_tool(name: str, args: dict) -> str:
    fn = TOOL_DISPATCH.get(name)
    if not fn:
        return json.dumps({"error": f"Unknown tool: {name}"})
    result = fn(**args)
    print(f"      [tool: {name}] -> {json.dumps(result)[:120]}")
    return json.dumps(result)


# ── Agent invocation ───────────────────────────────────────────────────────────

def run_agent_function_mode(
    oc: OpenAI,
    role: str,
    user_message: str,
    prev_response_id: str | None = None,
) -> tuple[str, str]:
    """
    Invoke an agent using client-side function calling.
    Handles the tool-call loop: model calls a function -> we dispatch locally -> continue.
    """
    kwargs: dict[str, Any] = {
        "model":        MODEL,
        "instructions": INSTRUCTIONS[role],
        "input":        user_message,
        "tools":        FUNCTION_TOOLS,
    }
    if prev_response_id:
        kwargs["previous_response_id"] = prev_response_id

    while True:
        resp = oc.responses.create(**kwargs)

        # Collect any function calls in this response
        tool_calls = [
            item for item in resp.output
            if getattr(item, "type", "") == "function_call"
        ]

        if not tool_calls:
            # No pending tool calls — we have the final answer
            return resp.output_text or "", resp.id

        # Execute each tool call and feed results back
        tool_results = []
        for tc in tool_calls:
            args = json.loads(tc.arguments) if isinstance(tc.arguments, str) else tc.arguments
            result_str = _dispatch_tool(tc.name, args)
            tool_results.append({
                "type":            "function_call_output",
                "call_id":         tc.call_id,
                "output":          result_str,
            })

        # Next iteration: continue the same response with tool outputs
        kwargs = {
            "model":               MODEL,
            "previous_response_id": resp.id,
            "input":               tool_results,
            "tools":               FUNCTION_TOOLS,
            "instructions":        INSTRUCTIONS[role],
        }


def run_agent_mcp_mode(
    oc: OpenAI,
    role: str,
    user_message: str,
    prev_response_id: str | None = None,
) -> tuple[str, str]:
    """
    Invoke an agent using server-side MCP (Azure Function App must be deployed).
    """
    mcp_headers = {}
    if MCP_SERVER_KEY:
        mcp_headers["x-functions-key"] = MCP_SERVER_KEY

    mcp_tool = MCPTool(
        server_label="helpdesk-tools",
        server_url=MCP_SERVER_URL,
        headers=mcp_headers if mcp_headers else None,
        require_approval="never",
    ).as_dict()

    kwargs: dict[str, Any] = {
        "model":        MODEL,
        "instructions": INSTRUCTIONS[role],
        "input":        user_message,
        "tools":        [mcp_tool],
    }
    if prev_response_id:
        kwargs["previous_response_id"] = prev_response_id

    resp = oc.responses.create(**kwargs)
    return resp.output_text or "", resp.id


# ── Orchestration ──────────────────────────────────────────────────────────────

def helpdesk_session(user_issue: str, user_email: str, tools_mode: str = "function"):
    project_client = get_project_client()
    oc             = get_openai_client(project_client)

    run_agent = run_agent_function_mode if tools_mode == "function" else run_agent_mcp_mode

    sep = "=" * 64
    print(f"\n{sep}")
    print(f"  IT HELPDESK SESSION  [tools: {tools_mode}]")
    print(f"  User : {user_email}")
    print(f"  Issue: {user_issue}")
    print(sep)

    # Step 1: Orchestrator triages + creates ticket
    print("\n[Step 1] Orchestrator -- triage & ticket creation")
    orch_msg = f"New helpdesk request from {user_email}:\n{user_issue}"
    orch_reply, orch_resp_id = run_agent(oc, "orchestrator", orch_msg)
    print(f"\n  ORCHESTRATOR:\n  {orch_reply}\n")

    ticket_id = _extract_ticket_id(orch_reply)
    print(f"  Ticket: {ticket_id}")

    # Step 2: Route to specialist
    asset_keywords = {"laptop", "monitor", "keyboard", "asset", "equipment", "device", "hardware", "screen"}
    is_asset = any(kw in user_issue.lower() for kw in asset_keywords)
    specialist     = "asset_mgmt" if is_asset else "tech_support"
    spec_label     = "Asset Management" if is_asset else "Tech Support"

    print(f"\n[Step 2] --> {spec_label}")
    spec_msg = f"Handle ticket {ticket_id}. User: {user_email}. Issue: {user_issue}"
    spec_reply, _ = run_agent(oc, specialist, spec_msg)
    print(f"\n  {spec_label.upper()}:\n  {spec_reply}\n")

    # Step 3: Notifier
    print("[Step 3] Notifier -- resolution email")
    notif_msg = (
        f"Send resolution to {user_email}. "
        f"Ticket: {ticket_id}. Resolution: {spec_reply[:250]}"
    )
    notif_reply, _ = run_agent(oc, "notifier", notif_msg)
    print(f"\n  NOTIFIER:\n  {notif_reply}\n")

    # Step 4: Orchestrator closes
    print("[Step 4] Orchestrator -- session close")
    wrap_msg = (
        f"Ticket {ticket_id} resolved by {spec_label}. "
        f"Notification sent to {user_email}. Confirm completion to user."
    )
    final_reply, _ = run_agent(oc, "orchestrator", wrap_msg, orch_resp_id)
    print(f"\n  ORCHESTRATOR (final):\n  {final_reply}\n")
    print(sep)
    print("  SESSION COMPLETE")
    print(sep)


def _extract_ticket_id(text: str) -> str:
    match = re.search(r"TKT-[A-Z0-9]{6}", text)
    return match.group(0) if match else "TKT-UNKNOWN"


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--issue", default="My VPN keeps disconnecting and I cannot access internal tools.")
    parser.add_argument("--email", default="alice@corp.com")
    parser.add_argument("--tools", choices=["function", "mcp"], default="function",
                        help="function=client-side (local dev), mcp=server-side (requires deployed Azure Function)")
    args = parser.parse_args()

    helpdesk_session(user_issue=args.issue, user_email=args.email, tools_mode=args.tools)
