import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends

from api.deps import get_current_user
from models.user import User
from schemas.telemetry import TelemetryBatch, TelemetryStatusResponse
from services.clickhouse import insert_agent_interaction, insert_tool_call, query_recent_events

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/telemetry", tags=["telemetry"])


@router.post("/events")
async def ingest_events(
    batch: TelemetryBatch,
    current_user: User = Depends(get_current_user),
):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    ingested = 0
    errors = 0

    for tc in batch.tool_calls:
        try:
            await insert_tool_call({
                "event_id": str(uuid.uuid4()),
                "timestamp": now,
                "mcp_server_id": tc.mcp_server_id,
                "tool_name": tc.tool_name,
                "input_params": tc.input_params,
                "response": tc.response,
                "latency_ms": tc.latency_ms,
                "status": tc.status,
                "user_action": tc.user_action,
                "session_id": tc.session_id,
                "user_id": str(current_user.id),
                "ide": tc.ide,
            })
            ingested += 1
        except Exception:
            errors += 1

    for ai in batch.agent_interactions:
        try:
            await insert_agent_interaction({
                "event_id": str(uuid.uuid4()),
                "timestamp": now,
                "agent_id": ai.agent_id,
                "session_id": ai.session_id,
                "tool_calls": ai.tool_calls,
                "user_action": ai.user_action,
                "latency_ms": ai.latency_ms,
                "user_id": str(current_user.id),
                "ide": ai.ide,
            })
            ingested += 1
        except Exception:
            errors += 1

    return {"ingested": ingested, "errors": errors}


@router.get("/status", response_model=TelemetryStatusResponse)
async def telemetry_status(current_user: User = Depends(get_current_user)):
    counts = await query_recent_events(60)
    return TelemetryStatusResponse(
        tool_call_events=counts["tool_call_events"],
        agent_interaction_events=counts["agent_interaction_events"],
        status="ok",
    )
