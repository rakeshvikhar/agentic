"""
Helpdesk Hosted Agent — Agent Framework + MCP  (alternative to main.py)
=========================================================================
Uses Microsoft's agent-framework library instead of LangGraph.
Simpler, less control but tightly integrated with Foundry.

Run locally:
    python main_af.py
"""
import logging
import os

from agent_framework import Agent
from agent_framework.foundry import FoundryChatClient
from agent_framework.tools.mcp import MCPTool
from agent_framework_foundry_hosting import ResponsesHostServer
from azure.core.credentials import AzureKeyCredential
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

FOUNDRY_ENDPOINT = os.environ["FOUNDRY_PROJECT_ENDPOINT"]
MODEL_NAME        = os.environ.get("AZURE_AI_MODEL_DEPLOYMENT_NAME", "gpt-5.2")
MCP_SERVER_URL    = os.environ["MCP_SERVER_URL"]
MCP_SERVER_KEY    = os.environ.get("MCP_SERVER_KEY", "")


def main():
    # ── Auth — key credential instead of DefaultAzureCredential ───────────────
    api_key    = os.environ["AZURE_AI_PROJECT_KEY"]
    credential = AzureKeyCredential(api_key)

    client = FoundryChatClient(
        project_endpoint=FOUNDRY_ENDPOINT,
        model=MODEL_NAME,
        credential=credential,
    )

    # ── MCP tool pointing at our Azure Function App ────────────────────────────
    mcp_headers = {"x-functions-key": MCP_SERVER_KEY} if MCP_SERVER_KEY else {}

    tools = []
    try:
        mcp_tool = MCPTool(
            server_label="helpdesk-tools",
            server_url=MCP_SERVER_URL,
            headers=mcp_headers,
            require_approval="never",
        )
        tools = [mcp_tool]
        log.info("Registered MCP tool: %s", MCP_SERVER_URL)
    except Exception as exc:
        log.warning("Could not register MCP tool: %s", exc)

    agent = Agent(
        client=client,
        instructions="""You are an IT Helpdesk Assistant.
Create a ticket for every issue (create_ticket), share the ticket ID, then resolve using the tools available:
get_ticket_status, get_asset_info (IDs: A001-A004), assign_ticket, log_event, close_ticket, send_notification.
Always log diagnostic steps and close the ticket when done.""",
        tools=tools,
        default_options={"store": False},
    )

    server = ResponsesHostServer(agent)
    log.info("Agent Framework server starting...")
    server.run()


if __name__ == "__main__":
    main()
