"""
Deploy the Deep Agent to Azure AI Foundry.

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

# Load project-level .env first, then agent .env (agent wins)
ROOT = pathlib.Path(__file__).parent.parent
load_dotenv(ROOT / ".env")
load_dotenv(pathlib.Path(__file__).parent / ".env", override=True)

sys.path.insert(0, str(ROOT / "agents"))
from auth import get_project_client   # noqa: E402

AGENT_NAME = "deep-reasoning-agent"
AGENT_DIR = pathlib.Path(__file__).parent


def get_env_vars() -> dict[str, str]:
    """
    No API key is uploaded here. The deployed agent's managed identity
    pulls AZURE_OPENAI_API_KEY from Key Vault at startup (see main.py:_load_kv_secret).
    """
    return {
        "AZURE_OPENAI_ENDPOINT":    os.environ["AZURE_OPENAI_ENDPOINT"],
        "AZURE_OPENAI_DEPLOYMENT":  os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-5.4"),
        "FOUNDRY_PROJECT_ENDPOINT": os.environ.get(
            "FOUNDRY_PROJECT_ENDPOINT",
            os.environ.get("AZURE_AI_PROJECT_ENDPOINT", ""),
        ),
        "KEY_VAULT_URL":            os.environ["KEY_VAULT_URL"],
        "KEY_VAULT_SECRET_NAME":    os.environ.get("KEY_VAULT_SECRET_NAME", "azure-openai54-api-key"),
    }


def create_source_zip() -> tuple[str, str]:
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
    print(f"  -> {tmp.name}  ({len(zip_bytes)/1024:.1f} KB)  sha256={sha256[:16]}...")
    return tmp.name, sha256


def deploy():
    from azure.ai.projects.models import (
        CodeConfiguration,
        CodeDependencyResolution,
        CreateAgentVersionFromCodeContent,
        CreateAgentVersionFromCodeMetadata,
        HostedAgentDefinition,
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
            description="Deep Reasoning Agent — LangGraph + built-in tools",
            definition=definition,
        ),
        code=("agent.zip", zip_bytes_data, "application/zip"),
    )

    try:
        print("\nUploading to Foundry...")
        result = client.beta.agents.create_version_from_code(
            AGENT_NAME,
            content,
            code_zip_sha256=sha256,
        )
        version = getattr(result, "version", "?")
        print(f"\nDeployed successfully!")
        print(f"  Agent   : {AGENT_NAME}")
        print(f"  Version : {version}")
        endpoint_base = os.environ.get(
            "FOUNDRY_PROJECT_ENDPOINT",
            os.environ.get("AZURE_AI_PROJECT_ENDPOINT", ""),
        ).rstrip("/")
        agent_endpoint = f"{endpoint_base}/agents/{AGENT_NAME}/endpoint"
        print(f"  Endpoint: {agent_endpoint}")

        env_path = ROOT / ".env"
        existing = env_path.read_text(encoding="utf-8")
        key = "DEEP_AGENT_ENDPOINT"
        if key not in existing:
            with open(env_path, "a", encoding="utf-8") as f:
                f.write(f"\n{key}={agent_endpoint}\n")
            print(f"  (appended {key} to ../.env)")

    except Exception as e:
        print(f"\nDeployment failed: {e}")
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
