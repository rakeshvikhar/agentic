import inspect
from langchain_azure_ai.agents.hosting._responses_host import ResponsesHostServer
src = inspect.getsource(ResponsesHostServer)
# Look for version-related strings
import re
for line in src.split('\n'):
    if 'version' in line.lower() or 'protocol' in line.lower() or 'header' in line.lower():
        print(line)
