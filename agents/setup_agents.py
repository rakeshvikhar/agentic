"""
Creates 4 helpdesk Prompt Agents in Azure AI Foundry using azure-ai-projects v2.2.0.

Each agent is defined as a PromptAgentDefinition with MCPTool pointing at our
Azure Function App (or local server during dev).

Run once:  python agents/setup_agents.py
Agent names are saved to agents/agents.json for use by run.py.
"""
import json
import os
import sys

from azure.ai.projects.models import MCPTool, PromptAgentDefinition
from dotenv import load_dotenv

# Allow importing auth.py from same directory
sys.path.insert(0, os.path.dirname(__file__))
from auth import get_project_client

load_dotenv()

MODEL          = os.environ.get("AZURE_OPENAI_MODEL", "gpt-4o")
MCP_SERVER_URL = os.environ.get("MCP_SERVER_URL", "http://localhost:7071/api/mcp")
MCP_SERVER_KEY = os.environ.get("MCP_SERVER_KEY", "")

AGENTS_JSON = os.path.join(os.path.dirname(__file__), "agents.json")

# Agent names must be alphanumeric + hyphens, max 63 chars
AGENT_NAMES = {
    "orchestrator": "helpdesk-orchestrator",
    "tech_support":  "helpdesk-tech-support",
    "asset_mgmt":    "helpdesk-asset-mgmt",
    "notifier":      "helpdesk-notifier",
}


def build_mcp_tool() -> MCPTool:
    headers = {}
    if MCP_SERVER_KEY:
        headers["x-functions-key"] = MCP_SERVER_KEY
    return MCPTool(
        server_label="helpdesk-tools",
        server_url=MCP_SERVER_URL,
        headers=headers if headers else None,
    )


AGENT_DEFINITIONS = {
    "orchestrator": {
        "instructions": """You are the IT Helpdesk Orchestrator.
Your responsibilities:
1. Understand the user's IT issue clearly.
2. Use create_ticket to log the issue (always assign a priority: low/medium/high/critical).
3. Tell the user their ticket ID.
4. Categorise the issue:
   - Hardware or physical equipment (laptop, monitor, keyboard) -> Asset Management
   - Software, network, VPN, access, password -> Tech Support
5. Once resolved, confirm the outcome to the user.
Be concise and professional. Always confirm the ticket ID first.""",
    },
    "tech_support": {
        "instructions": """You are the Tech Support specialist.
Your responsibilities:
1. Receive a ticket_id and issue description.
2. Use get_ticket_status to review the full issue details.
3. Use assign_ticket to assign to "Tech Support Team".
4. Diagnose step-by-step; use log_event for each diagnostic step taken.
5. Resolve the issue and use close_ticket with a clear resolution note.
Be technical, thorough, and log every action.""",
    },
    "asset_mgmt": {
        "instructions": """You are the Asset Management specialist.
Your responsibilities:
1. Receive a ticket_id and hardware-related request.
2. Use get_ticket_status to review the issue.
3. Use assign_ticket to assign to "Asset Management Team".
4. Use get_asset_info to check assets (IDs: A001, A002, A003, A004).
5. Log findings with log_event.
6. Close the ticket with close_ticket and a clear resolution.
Always verify asset availability before promising replacements.""",
    },
    "notifier": {
        "instructions": """You are the Notification specialist.
Your responsibilities:
1. Receive a ticket_id, recipient email, and resolution summary.
2. Use send_notification to send a friendly update to the user.
3. Include: ticket ID, what was resolved, and any follow-up steps.
Keep messages brief and professional. Never invent ticket details.""",
    },
}


def create_or_update_agent(client, role: str) -> str:
    """Create a new version of the agent, or create it fresh."""
    name       = AGENT_NAMES[role]
    defn       = AGENT_DEFINITIONS[role]
    mcp_tool   = build_mcp_tool()

    prompt_def = PromptAgentDefinition(
        model=MODEL,
        instructions=defn["instructions"],
        tools=[mcp_tool],
    )

    try:
        result = client.agents.create_version(name, definition=prompt_def)
        print(f"  {role:20s} -> {name}  (version: {result.get('version', 'v1')})")
        return name
    except Exception as e:
        print(f"  [ERROR] {role}: {e}")
        raise


def setup_agents():
    print(f"Connecting to: {os.environ['AZURE_AI_PROJECT_ENDPOINT'][:60]}...")
    print(f"Model        : {MODEL}")
    print(f"MCP server   : {MCP_SERVER_URL}\n")

    client = get_project_client()

    names = {}
    for role in ["orchestrator", "tech_support", "asset_mgmt", "notifier"]:
        name = create_or_update_agent(client, role)
        names[role] = name

    with open(AGENTS_JSON, "w") as f:
        json.dump(names, f, indent=2)

    print(f"\nAgent names saved to {AGENTS_JSON}")
    print("Run 'python agents/run.py' to start a helpdesk session.")


if __name__ == "__main__":
    setup_agents()
