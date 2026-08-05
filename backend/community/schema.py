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


# ─── Community v2.1: events / polls / bookmarks ───────────────────────────────
class EventIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=MAX_BODY_LEN)
    starts_at: str = Field(min_length=1, max_length=40)   # ISO-8601, coerced to UTC-Z server-side
    ends_at: Optional[str] = Field(default=None, max_length=40)
    timezone: Optional[str] = Field(default=None, max_length=64)   # IANA name, display only
    location: str = Field(default="", max_length=500)     # address or join URL
    host: str = Field(default="", max_length=200)
    channel_slug: str = Field(default="events", max_length=64)


class PollIn(BaseModel):
    channel_slug: str = Field(min_length=1, max_length=64)
    question: str = Field(min_length=1, max_length=300)
    options: List[str] = Field(min_length=2, max_length=6)


class VoteIn(BaseModel):
    option_id: int = Field(ge=1)


class BookmarkIn(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    url: str = Field(min_length=1, max_length=1000)
