"""
Helpdesk Hosted Agent — LangGraph + MCP
========================================
A LangGraph ReAct agent deployed as a Foundry Hosted Agent.
It connects to the helpdesk MCP server (Azure Function App) for all tool calls.

Local test:
    python main.py
    curl -X POST http://localhost:8088/responses \
         -H "Content-Type: application/json" \
         -d '{"input": "My VPN is disconnecting", "stream": false}'

Deploy to Foundry:
    python deploy.py
"""
import asyncio
import logging
import os

from dotenv import load_dotenv
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent
from langgraph.checkpoint.memory import MemorySaver

from langchain_azure_ai.agents.hosting._responses_host import ResponsesHostServer

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger(__name__)


def _load_kv_secrets() -> None:
    """Pull secrets from Azure Key Vault into os.environ when KEY_VAULT_URL is set."""
    kv_url = os.environ.get("KEY_VAULT_URL")
    if not kv_url:
        return
    try:
        from azure.identity import DefaultAzureCredential
        from azure.keyvault.secrets import SecretClient
        client = SecretClient(vault_url=kv_url, credential=DefaultAzureCredential())
        mapping = {
            "AZURE_AI_PROJECT_KEY": "foundry-api-key",
            "MCP_SERVER_KEY":       "mcp-function-key",
        }
        for env_var, secret_name in mapping.items():
            try:
                os.environ[env_var] = client.get_secret(secret_name).value
                log.info("Loaded secret %s from Key Vault", secret_name)
            except Exception as e:
                log.warning("Could not load KV secret %s: %s", secret_name, e)
    except Exception as e:
        log.warning("Key Vault unavailable, using env vars: %s", e)


_load_kv_secrets()

# ── Environment ────────────────────────────────────────────────────────────────
FOUNDRY_ENDPOINT = (
    os.environ.get("FOUNDRY_PROJECT_ENDPOINT")
    or os.environ.get("AZURE_AI_PROJECT_ENDPOINT")
    or os.environ["FOUNDRY_PROJECT_ENDPOINT"]  # raise clearly if both missing
)
MODEL_NAME        = os.environ.get("AZURE_AI_MODEL_DEPLOYMENT_NAME", "gpt-5.4")
MCP_SERVER_URL    = os.environ["MCP_SERVER_URL"]
MCP_SERVER_KEY    = os.environ.get("MCP_SERVER_KEY", "")
PORT              = int(os.environ.get("PORT", "8088"))

SYSTEM_PROMPT = """You are an IT Helpdesk Assistant with access to a ticket management system.

You can:
- Create tickets for reported issues (use create_ticket)
- Check ticket status (use get_ticket_status)
- Look up IT assets like laptops, monitors (use get_asset_info — IDs: A001-A004)
- Assign tickets to specialist teams (use assign_ticket)
- Log diagnostic steps (use log_event)
- Close resolved tickets (use close_ticket)
- Send email notifications to users (use send_notification)
- List all tickets (use list_all_tickets)

Always create a ticket first, share the ticket ID with the user, then work through the issue.
Be concise, professional, and log every diagnostic action you take."""


def _make_model() -> ChatOpenAI:
    base_url = FOUNDRY_ENDPOINT.rstrip("/") + "/openai/v1"
    api_key  = os.environ.get("AZURE_AI_PROJECT_KEY", "placeholder")
    return ChatOpenAI(model=MODEL_NAME, base_url=base_url, api_key=api_key, temperature=0.2)


def _make_graph(tools: list) -> "CompiledStateGraph":
    return create_react_agent(
        _make_model(),
        tools=tools,
        prompt=SYSTEM_PROMPT,
        checkpointer=MemorySaver(),
    )


async def _load_mcp_tools() -> list:
    mcp_headers = {"x-functions-key": MCP_SERVER_KEY} if MCP_SERVER_KEY else {}
    mcp_client = MultiServerMCPClient({
        "helpdesk": {
            "transport": "streamable_http",
            "url":       MCP_SERVER_URL,
            "headers":   mcp_headers or None,
        }
    })
    try:
        tools = await asyncio.wait_for(mcp_client.get_tools(), timeout=30.0)
        log.info("Loaded %d MCP tools: %s", len(tools), [t.name for t in tools])
        return tools
    except Exception as exc:
        log.warning("MCP tools unavailable: %s", exc)
        return []


async def main_async():
    """
    Start server immediately with a no-tool placeholder graph so /readiness
    responds fast (cold-start), then swap in the MCP-equipped graph in the
    background once tools are loaded.
    """
    app_insights = os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING")

    # ── Step 1: Build minimal graph (no MCP tools) — fast ──────────────────────
    log.info("Building placeholder graph (no MCP tools)...")
    placeholder_graph = _make_graph([])
    server = ResponsesHostServer(
        placeholder_graph,
        applicationinsights_connection_string=app_insights,
    )

    # ── Step 2: Load MCP tools in background and swap graph once ready ──────────
    async def upgrade_to_mcp():
        tools = await _load_mcp_tools()
        if tools:
            real_graph = _make_graph(tools)
            server._graph = real_graph
            log.info("Graph upgraded: MCP tools now active (%d tools)", len(tools))
        else:
            log.warning("Running without MCP tools.")

    asyncio.create_task(upgrade_to_mcp())

    # ── Step 3: Run server — /readiness is available immediately ───────────────
    log.info("Starting server on port %d", PORT)
    await server.run_async(port=PORT)


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
