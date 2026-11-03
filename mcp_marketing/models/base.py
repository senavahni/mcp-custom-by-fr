"""Base ORM: semua tabel default di schema `mcp`, dengan naming convention tetap."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, MetaData, Text, Uuid, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, mapped_column

NAMING = {
    "ix": "%(column_0_label)s_idx",
    "uq": "%(table_name)s_%(column_0_name)s_key",
    "ck": "%(table_name)s_%(constraint_name)s_check",
    "fk": "%(table_name)s_%(column_0_name)s_fkey",
    "pk": "%(table_name)s_pkey",
}


class Base(DeclarativeBase):
    metadata = MetaData(schema="mcp", naming_convention=NAMING)
    type_annotation_map = {
        dict[str, Any]: JSONB,
        list[Any]: JSONB,
        list[str]: ARRAY(Text),
        uuid.UUID: Uuid(as_uuid=True),
        datetime: DateTime(timezone=True),
    }


def uuid_pk() -> Any:
    return mapped_column(Uuid(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())


def ts_default() -> Any:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
