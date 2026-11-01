from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from reclaim.assistant_setup import assistant_setup

# Read-only: the text the Settings page shows for connecting an AI assistant (run spec Phase C).
# There is deliberately no route that writes a client's config file.

router = APIRouter(prefix="/api/assistant")


class AssistantSetupOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: str
    args: list[str]
    installed: bool
    claude_desktop_json: str
    claude_code_command: str
    codex_toml: str


@router.get("/setup", response_model=AssistantSetupOut)
def setup() -> AssistantSetupOut:
    s = assistant_setup()
    return AssistantSetupOut(
        command=s.command,
        args=list(s.args),
        installed=s.installed,
        claude_desktop_json=s.claude_desktop_json,
        claude_code_command=s.claude_code_command,
        codex_toml=s.codex_toml,
    )
