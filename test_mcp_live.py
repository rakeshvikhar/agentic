"""
Quick smoke test: calls the Azure AI Foundry model with our local MCPTool.
Run: python test_mcp_live.py
"""
import sys, os
sys.path.insert(0, "agents")
sys.path.insert(0, "mcp_server")

from dotenv import load_dotenv
load_dotenv(".env", override=True)

from auth import get_project_client, get_openai_client
from azure.ai.projects.models import MCPTool

client = get_project_client()
oc = get_openai_client(client)
model = os.environ["AZURE_OPENAI_MODEL"]
mcp_url = os.environ["MCP_SERVER_URL"]

print(f"Model   : {model}")
print(f"MCP URL : {mcp_url}")
print()

mcp_tool = MCPTool(server_label="helpdesk-tools", server_url=mcp_url).as_dict()

print("Calling model with MCPTool (create_ticket)...")
resp = oc.responses.create(
    model=model,
    input="Create a ticket: title is 'VPN drops every 20 min', description is 'VPN disconnects frequently', priority high.",
    tools=[mcp_tool],
)

print(f"Status : {resp.status}")
print(f"Reply  : {resp.output_text}")
print()
print("Output items:")
for item in resp.output:
    item_type = getattr(item, "type", type(item).__name__)
    print(f"  [{item_type}]", getattr(item, "text", getattr(item, "name", "")))
