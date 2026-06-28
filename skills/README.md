# Skill packs

Drop-in Python files that add new tools to Maahi.

## Where they live

```
~/.maahi/skills/<your_skill>.py
```

Override with `$MAAHI_SKILLS_DIR` for a different location.

## Shape

Each pack exports a module-level `TOOLS` tuple:

```python
from maahi.tools.registry import Tool

def my_tool(arg: str) -> dict:
    return {"ok": True, "result": ...}

TOOLS = (
    Tool(
        name="my_tool",
        description="One-line summary the brain sees.",
        func=my_tool,
        arg_schema={"arg": "str: what it is"},
    ),
)
```

## Rules

- Files starting with `_` are ignored (use for shared helper modules).
- A pack that fails to import is logged and skipped — never crashes Maahi.
- You **cannot** override a built-in tool — built-ins always win.
- Tool names must be unique across packs; first-loaded wins on collisions.

## Try it

```bash
mkdir -p ~/.maahi/skills
cp skills/example.py.template ~/.maahi/skills/example.py
# restart Maahi
```

Then say *"Hey Maahi, what time is it in Tokyo?"* or *"Hey Maahi, roll a die."*

## Listing what's loaded

```bash
curl -s http://127.0.0.1:7421/api/skills | jq '.tools[] | {name, description}'
```
