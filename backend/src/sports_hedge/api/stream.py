"""Read-only STREAM operator API. Status GET never calls providers."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from sports_hedge.application.stream.pin import StreamPinError
from sports_hedge.application.stream.runtime import get_stream_runtime
from sports_hedge.application.stream.status import StreamStatus

router = APIRouter(prefix="/stream", tags=["stream"])


class StreamSelectRequest(BaseModel):
    canonical_event_id: str = Field(min_length=1)


@router.get("/status", response_model=StreamStatus)
def stream_status() -> StreamStatus:
    return get_stream_runtime().status()


@router.post("/select", response_model=StreamStatus)
async def stream_select(request: StreamSelectRequest) -> StreamStatus:
    try:
        return await get_stream_runtime().select_fixture(request.canonical_event_id)
    except StreamPinError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.reason) from exc


@router.post("/remove", response_model=StreamStatus)
async def stream_remove() -> StreamStatus:
    return await get_stream_runtime().remove()


@router.post("/pause", response_model=StreamStatus)
async def stream_pause() -> StreamStatus:
    return await get_stream_runtime().pause()


@router.post("/resume", response_model=StreamStatus)
async def stream_resume() -> StreamStatus:
    return await get_stream_runtime().resume()
