 Here's what was built and how each piece maps to your requirements:

hosted_agent/ — full file summary

File	Framework	Purpose
hosted_agent/main.py	LangGraph + MCP	Primary hosted agent. MultiServerMCPClient loads tools from your Azure Function, create_react_agent runs the graph, ResponsesHostServer exposes /responses
hosted_agent/main_af.py	Agent Framework + MCP	Alternative using Microsoft's agent-framework library — simpler, less control
hosted_agent/orchestrator.py	A2A protocol	Calls the hosted agent over HTTP (Responses protocol = A2A transport). Multi-turn conversation with previous_response_id
hosted_agent/deploy.py	SDK	Deploys main.py to Foundry as a HostedAgentDefinition with CodeConfiguration
hosted_agent/agent.manifest.yaml	azd	For azd ai agent init / azd deploy path
How the three patterns connect:

orchestrator.py  ──[A2A / POST /responses]──►  main.py (LangGraph hosted agent)
                                                     │
                                                     └──[MCP / SSE]──► helpdesk-mcp-tools
                                                                          (Azure Function)
                                                                               │
                                                                               └──► Azure Table Storage
To run locally:

conda activate foundry_env
cd C:\workspace\claude-ws\aifoundry\hosted_agent

# Terminal 1 — start the hosted agent server
$env:PYTHONIOENCODING = "utf-8"
python main.py

# Terminal 2 — call it via A2A
python orchestrator.py --issue "Laptop screen flickering" --email "bob@corp.com"
To deploy to Foundry (once the HostedAgent API is GA in your region):

python deploy.py

--
$env:PYTHONIOENCODING="utf-8"
conda activate foundry_env
python hosted_agent/orchestrator_a2a.py --issue "Laptop won't connect to WiFi" --email "bob@corp.com"
---

=====================
run a2a orchestrator locally
=============================
# Terminal 1: start the hosted agent server
$env:PYTHONIOENCODING="utf-8"
conda activate foundry_env
cd C:\workspace\claude-ws\aifoundry\hosted_agent
python main.py
# Terminal 2: run the orchestrator against it
$env:PYTHONIOENCODING="utf-8"
conda activate foundry_env
cd C:\workspace\claude-ws\aifoundry
python hosted_agent/orchestrator.py --issue "VPN disconnecting" --email "alice@corp.com"
# or true A2A version:
python hosted_agent/orchestrator_a2a.py --issue "VPN disconnecting" --email "alice@corp.com"