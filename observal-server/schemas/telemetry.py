from datetime import datetime

from pydantic import BaseModel


class ToolCallEvent(BaseModel):
    mcp_server_id: str
    tool_name: str
    input_params: str = ""
    response: str = ""
    latency_ms: int = 0
    status: str = "success"
    user_action: str = ""
    session_id: str = ""
    ide: str = ""


class AgentInteractionEvent(BaseModel):
    agent_id: str
    session_id: str = ""
    tool_calls: int = 0
    user_action: str = ""
    latency_ms: int = 0
    ide: str = ""


class TelemetryBatch(BaseModel):
    tool_calls: list[ToolCallEvent] = []
    agent_interactions: list[AgentInteractionEvent] = []


class TelemetryStatusResponse(BaseModel):
    tool_call_events: int
    agent_interaction_events: int
    status: str
