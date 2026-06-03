import sys, json
sys.path.insert(0, "app")
from services.script_parser import ScriptParser

text = """Let's run a live payment. We set up an amount of fifty thousand dollars, select the currency, Sender ID, and Receiver ID, then select the US to UK corridor and hit Orchestrate."""

scenes = ScriptParser().parse(text, default_url="http://host.containers.internal:5173")
print(json.dumps(scenes, indent=2))
