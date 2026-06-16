"""
Deploy the Helpdesk Hosted Agent to Azure AI Foundry.

Run:
    python deploy.py           # deploy / update
    python deploy.py --delete  # remove the agent
"""
import argparse
import hashlib
import os
import pathlib
import sys
import tempfile
import zipfile

from dotenv import load_dotenv

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agents"))
from auth import get_project_client

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"), override=True)

AGENT_NAME = "helpdesk-langgraph-agent"
AGENT_DIR  = pathlib.Path(__file__).parent


def get_env_vars() -> dict[str, str]:
    endpoint = os.environ["AZURE_AI_PROJECT_ENDPOINT"]
    return {
        "AZURE_AI_MODEL_DEPLOYMENT_NAME": os.environ.get("AZURE_OPENAI_MODEL", "gpt-5.4"),
        "MCP_SERVER_URL":                 os.environ["MCP_SERVER_URL"],
        "MCP_SERVER_KEY":                 os.environ.get("MCP_SERVER_KEY", ""),
        "AZURE_AI_PROJECT_KEY":           os.environ["AZURE_AI_PROJECT_KEY"],
        "FOUNDRY_PROJECT_ENDPOINT":       endpoint,
        "AZURE_AI_PROJECT_ENDPOINT":      endpoint,
    }


def create_source_zip() -> tuple[str, str]:
    """Zip source files; return (zip_path, sha256_hex_of_zip)."""
    files = [
        AGENT_DIR / "main.py",
        AGENT_DIR / "requirements.txt",
    ]
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    tmp.close()
    with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            if f.exists():
                data = f.read_bytes()
                zf.writestr(f.name, data)
                print(f"  + {f.name}  ({len(data)/1024:.1f} KB)")
    # SHA-256 must be of the zip file itself, not of individual files
    zip_bytes = pathlib.Path(tmp.name).read_bytes()
    sha256 = hashlib.sha256(zip_bytes).hexdigest()
    print(f"  -> {tmp.name}  ({len(zip_bytes)/1024:.1f} KB)  sha256={sha256[:16]}...")
    return tmp.name, sha256


def deploy():
    from azure.ai.projects.models import (
        CreateAgentVersionFromCodeContent,
        CreateAgentVersionFromCodeMetadata,
        HostedAgentDefinition,
        CodeConfiguration,
        CodeDependencyResolution,
        ProtocolVersionRecord,
    )

    client = get_project_client()
    print(f"Deploying '{AGENT_NAME}' to AI Foundry...\n")

    print("Packing source files:")
    zip_path, sha256 = create_source_zip()

    definition = HostedAgentDefinition(
        cpu="1",
        memory="2Gi",
        code_configuration=CodeConfiguration(
            runtime="python_3_13",
            entry_point=["python", "main.py"],
            dependency_resolution=CodeDependencyResolution.REMOTE_BUILD,
        ),
        protocol_versions=[
            ProtocolVersionRecord(protocol="responses", version="1.0.0"),
        ],
        environment_variables=get_env_vars(),
    )

    zip_bytes_data = pathlib.Path(zip_path).read_bytes()
    content = CreateAgentVersionFromCodeContent(
        metadata=CreateAgentVersionFromCodeMetadata(
            description="IT Helpdesk agent - LangGraph + MCP + Azure AI Foundry",
            definition=definition,
        ),
        code=("agent.zip", zip_bytes_data, "application/zip"),
    )

    try:
        print(f"\nUploading to Foundry...")
        result = client.beta.agents.create_version_from_code(
            AGENT_NAME,
            content,
            code_zip_sha256=sha256,
        )
        print(f"\nDeployed successfully!")
        print(f"  Agent   : {AGENT_NAME}")
        print(f"  Version : {getattr(result, 'version', result.get('version', 'v1') if hasattr(result, 'get') else '?')}")

        # Build endpoint URL
        endpoint_base = os.environ["AZURE_AI_PROJECT_ENDPOINT"].rstrip("/")
        agent_endpoint = f"{endpoint_base}/agents/{AGENT_NAME}/endpoint"
        print(f"  Endpoint: {agent_endpoint}")
        print(f"\nAdd to .env:")
        print(f"  HELPDESK_AGENT_ENDPOINT={agent_endpoint}")

        # Append to .env
        env_path = pathlib.Path(__file__).parent.parent / ".env"
        existing = env_path.read_text(encoding="utf-8")
        if "HELPDESK_AGENT_ENDPOINT" not in existing:
            with open(env_path, "a", encoding="utf-8") as f:
                f.write(f"\nHELPDESK_AGENT_ENDPOINT={agent_endpoint}\n")
            print("  (appended to ../.env)")

    except Exception as e:
        print(f"\nDeployment failed: {e}")
        if "not found" in str(e).lower() or "404" in str(e):
            print("\nThe Hosted Agent API may not be enabled for your region/subscription.")
            print("Check: https://learn.microsoft.com/azure/foundry/agents/quickstarts/quickstart-hosted-agent")
        print("\nTo test locally instead:")
        print("  python main.py")
        print("  python orchestrator.py --issue 'VPN down' --email 'bob@corp.com'")
    finally:
        pathlib.Path(zip_path).unlink(missing_ok=True)


def delete_agent():
    client = get_project_client()
    try:
        client.agents.delete_version(AGENT_NAME, version="*")
        print(f"Agent '{AGENT_NAME}' deleted.")
    except Exception as e:
        print(f"Delete failed: {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--delete", action="store_true")
    args = parser.parse_args()
    if args.delete:
        delete_agent()
    else:
        deploy()
