"""Pydantic request models for the Community API (PRD §6)."""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field

MAX_BODY_LEN = 4000
MAX_MENTIONS = 20
MAX_ATTACHMENTS = 5


class MessageIn(BaseModel):
    body: str = Field(default="", max_length=MAX_BODY_LEN)
    parent_message_id: Optional[int] = None
    mention_user_ids: List[str] = Field(default_factory=list, max_length=MAX_MENTIONS)
    attachment_ids: List[str] = Field(default_factory=list, max_length=MAX_ATTACHMENTS)


class MessageEdit(BaseModel):
    body: str = Field(min_length=1, max_length=MAX_BODY_LEN)
    mention_user_ids: Optional[List[str]] = Field(default=None, max_length=MAX_MENTIONS)


class ReactionIn(BaseModel):
    emoji: str = Field(min_length=1, max_length=16)


class ReadIn(BaseModel):
    last_read_message_id: int = Field(ge=0)


class HandoffRedeem(BaseModel):
    token: str = Field(min_length=1, max_length=128)


class DmOpen(BaseModel):
    user_id: str = Field(min_length=1, max_length=64)


class DmMessageIn(BaseModel):
    body: str = Field(default="", max_length=MAX_BODY_LEN)
    attachment_ids: List[str] = Field(default_factory=list, max_length=MAX_ATTACHMENTS)
