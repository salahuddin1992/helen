"""
Poll REST endpoints.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user_id, get_db
from app.core.exceptions import ForbiddenError, NotFoundError, ValidationError
from app.services.poll_service import PollService

router = APIRouter(prefix="/polls", tags=["polls"])


class PollCreate(BaseModel):
    channel_id: str
    question: str = Field(..., min_length=1, max_length=500)
    options: list[str] = Field(..., min_length=2, max_length=12)
    is_multi_choice: bool = False
    is_anonymous: bool = False
    closes_at: datetime | None = None
    message_id: str | None = None


class PollVoteRequest(BaseModel):
    option_ids: list[str] = Field(..., min_length=1, max_length=12)


class PollOptionResponse(BaseModel):
    id: str
    position: int
    text: str

    class Config:
        from_attributes = True


class PollResponse(BaseModel):
    id: str
    channel_id: str
    creator_id: str
    question: str
    is_multi_choice: bool
    is_anonymous: bool
    status: str
    closes_at: datetime | None = None
    created_at: datetime
    options: list[PollOptionResponse]

    class Config:
        from_attributes = True


def _to_response(poll) -> PollResponse:
    return PollResponse(
        id=poll.id,
        channel_id=poll.channel_id,
        creator_id=poll.creator_id,
        question=poll.question,
        is_multi_choice=poll.is_multi_choice,
        is_anonymous=poll.is_anonymous,
        status=poll.status,
        closes_at=poll.closes_at,
        created_at=poll.created_at,
        options=[PollOptionResponse.model_validate(o) for o in poll.options],
    )


@router.post("", response_model=PollResponse, status_code=201)
async def create_poll(
    body: PollCreate,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    try:
        poll = await PollService.create(
            db,
            creator_id=user_id,
            channel_id=body.channel_id,
            question=body.question,
            options=body.options,
            is_multi_choice=body.is_multi_choice,
            is_anonymous=body.is_anonymous,
            closes_at=body.closes_at,
            message_id=body.message_id,
        )
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ForbiddenError as e:
        raise HTTPException(status_code=403, detail=str(e))
    return _to_response(poll)


@router.get("/{poll_id}", response_model=PollResponse)
async def get_poll(
    poll_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    try:
        poll = await PollService.get(db, poll_id, user_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Poll not found")
    except ForbiddenError as e:
        raise HTTPException(status_code=403, detail=str(e))
    return _to_response(poll)


@router.get("/{poll_id}/results")
async def get_poll_results(
    poll_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await PollService.results(db, poll_id, user_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Poll not found")
    except ForbiddenError as e:
        raise HTTPException(status_code=403, detail=str(e))


@router.post("/{poll_id}/vote")
async def vote(
    poll_id: str,
    body: PollVoteRequest,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    try:
        poll = await PollService.vote(db, poll_id, user_id, body.option_ids)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Poll not found")
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ForbiddenError as e:
        raise HTTPException(status_code=403, detail=str(e))
    return await PollService.results(db, poll_id, user_id)


@router.delete("/{poll_id}/vote", status_code=204)
async def retract_vote(
    poll_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    await PollService.retract(db, poll_id, user_id)
    return None


@router.post("/{poll_id}/close", response_model=PollResponse)
async def close_poll(
    poll_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    try:
        poll = await PollService.close(db, poll_id, user_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Poll not found")
    except ForbiddenError as e:
        raise HTTPException(status_code=403, detail=str(e))
    return _to_response(poll)


channel_polls_router = APIRouter(prefix="/channels", tags=["polls"])


@channel_polls_router.get("/{channel_id}/polls")
async def list_channel_polls(
    channel_id: str,
    status: str | None = Query(None, pattern=r"^(open|closed)$"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    try:
        polls, total = await PollService.list_for_channel(
            db, channel_id, user_id, status=status, limit=limit, offset=offset
        )
    except ForbiddenError as e:
        raise HTTPException(status_code=403, detail=str(e))
    return {
        "polls": [_to_response(p) for p in polls],
        "total": total,
    }
