import hashlib, pathlib, sys, os, zipfile, tempfile, json
sys.path.insert(0, r"C:\workspace\claude-ws\aifoundry\agents")
from dotenv import load_dotenv
load_dotenv(r"C:\workspace\claude-ws\aifoundry\.env", override=True)
from auth import get_project_client
from azure.ai.projects.models import (
    CreateAgentVersionFromCodeContent, CreateAgentVersionFromCodeMetadata,
    HostedAgentDefinition, CodeConfiguration, CodeDependencyResolution, ProtocolVersionRecord,
)
from azure.ai.projects._utils.utils import prepare_multipart_form_data

AGENT_DIR = pathlib.Path(r"C:\workspace\claude-ws\aifoundry\hosted_agent")

# Build zip
tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
tmp.close()
with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED) as zf:
    for f in [AGENT_DIR / "main.py", AGENT_DIR / "requirements.txt"]:
        zf.writestr(f.name, f.read_bytes())

zip_bytes = pathlib.Path(tmp.name).read_bytes()
sha256 = hashlib.sha256(zip_bytes).hexdigest()
print(f"Zip size: {len(zip_bytes)} bytes, sha256: {sha256[:20]}...")

zip_file = open(tmp.name, "rb")

definition = HostedAgentDefinition(
    cpu="1", memory="2Gi",
    code_configuration=CodeConfiguration(
        runtime="python_3_13",
        entry_point=["python", "main.py"],
        dependency_resolution=CodeDependencyResolution.REMOTE_BUILD,
    ),
    protocol_versions=[ProtocolVersionRecord(protocol="responses", version="1.0.0")],
    environment_variables={"AZURE_AI_MODEL_DEPLOYMENT_NAME": "gpt-5.4"},
)

content = CreateAgentVersionFromCodeContent(
    metadata=CreateAgentVersionFromCodeMetadata(description="test", definition=definition),
    code=zip_file,
)

body = content.as_dict()
print("\nas_dict() keys:", list(body.keys()))
print("code field type:", type(body.get("code")))
code_val = body.get("code")
if hasattr(code_val, "read"):
    pos = code_val.tell()
    data = code_val.read()
    code_val.seek(pos)
    print(f"code field is file-like, readable bytes: {len(data)}")
else:
    print("code field value:", repr(code_val)[:200])

files = prepare_multipart_form_data(body, ["code"], ["metadata"])
print("\nmultipart files parts:")
for part in files:
    name = part[0]
    val = part[1]
    if isinstance(val, tuple):
        fname, fval = val[0], val[1]
        if hasattr(fval, "read"):
            fval.seek(0)
            size = len(fval.read())
            fval.seek(0)
            print(f"  [{name}] filename={fname}, file size={size} bytes")
        else:
            print(f"  [{name}] filename={fname}, value={repr(fval)[:100]}")
    else:
        print(f"  [{name}] value={repr(val)[:200]}")

zip_file.close()
pathlib.Path(tmp.name).unlink()
