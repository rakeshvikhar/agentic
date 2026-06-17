"""
Deploy the Helpdesk Orchestrator Agent to Azure AI Foundry.

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

AGENT_NAME = "helpdesk-orchestrator-agent"
AGENT_DIR  = pathlib.Path(__file__).parent


def get_env_vars() -> dict[str, str]:
    """Environment variables injected into the remote container."""
    endpoint = os.environ["AZURE_AI_PROJECT_ENDPOINT"]
    return {
        "AZURE_AI_MODEL_DEPLOYMENT_NAME": os.environ.get("AZURE_OPENAI_MODEL", "gpt-5.4"),
        "AZURE_AI_PROJECT_KEY":           os.environ["AZURE_AI_PROJECT_KEY"],
        "FOUNDRY_PROJECT_ENDPOINT":       endpoint,
        "AZURE_AI_PROJECT_ENDPOINT":      endpoint,
        "HELPDESK_AGENT_ENDPOINT":        os.environ["HELPDESK_AGENT_ENDPOINT"],
        "KEY_VAULT_URL":                  "https://rakesh-kv-eastus2.vault.azure.net",
    }


def create_source_zip() -> tuple[str, str]:
    """Zip main.py + requirements.txt; return (zip_path, sha256_of_zip_bytes)."""
    files = [AGENT_DIR / "main.py", AGENT_DIR / "requirements.txt"]
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    tmp.close()
    with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            if f.exists():
                data = f.read_bytes()
                zf.writestr(f.name, data)
                print(f"  + {f.name}  ({len(data)/1024:.1f} KB)")
    zip_bytes = pathlib.Path(tmp.name).read_bytes()
    sha256 = hashlib.sha256(zip_bytes).hexdigest()
    print(f"  -> zip: {len(zip_bytes)/1024:.1f} KB  sha256={sha256[:16]}...")
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
            description="IT Helpdesk orchestrator — drives multi-turn A2A sessions with helpdesk-langgraph-agent",
            definition=definition,
        ),
        code=("agent.zip", zip_bytes_data, "application/zip"),
    )

    try:
        print("\nUploading to Foundry...")
        result = client.beta.agents.create_version_from_code(
            AGENT_NAME, content, code_zip_sha256=sha256,
        )
        version = getattr(result, "version", "?")
        print(f"\nDeployed successfully!")
        print(f"  Agent   : {AGENT_NAME}")
        print(f"  Version : {version}")

        endpoint_base = os.environ["AZURE_AI_PROJECT_ENDPOINT"].rstrip("/")
        agent_endpoint = f"{endpoint_base}/agents/{AGENT_NAME}/endpoint"
        print(f"  Endpoint: {agent_endpoint}")

        # Append to .env so orchestrator callers can reference it
        env_path = pathlib.Path(__file__).parent.parent / ".env"
        existing = env_path.read_text(encoding="utf-8")
        if "ORCHESTRATOR_AGENT_ENDPOINT" not in existing:
            with open(env_path, "a", encoding="utf-8") as f:
                f.write(f"\nORCHESTRATOR_AGENT_ENDPOINT={agent_endpoint}\n")
            print("  (ORCHESTRATOR_AGENT_ENDPOINT appended to .env)")

    except Exception as e:
        print(f"\nDeployment failed: {e}")
        print("\nTo test locally:")
        print("  python main.py")
        print('  curl -X POST http://localhost:8088/responses \\')
        print('       -H "Content-Type: application/json" \\')
        print("       -d '{\"input\": \"VPN down for alice@corp.com\", \"stream\": false}'")
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
    delete_agent() if args.delete else deploy()
