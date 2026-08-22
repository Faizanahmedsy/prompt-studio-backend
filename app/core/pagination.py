"""One pagination contract for every list endpoint.

`Page` is generic so a route's `response_model=Page[ProjectRead]` still
documents the item shape in OpenAPI, and the client reads `items` / `total` /
`page` in exactly the same place on every list it fetches.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

MAX_PAGE_SIZE = 200
# `offset = (page - 1) * size` is computed in Python and handed to Postgres as a
# BIGINT. Unbounded, `?page=99999999999999999999` overflows int64 and turns every
# list endpoint into a 500 that any authenticated caller can fire at will.
MAX_PAGE = 1_000_000


class PageParams(BaseModel):
    page: int = Field(1, ge=1, le=MAX_PAGE)
    size: int = Field(25, ge=1, le=MAX_PAGE_SIZE)

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.size


async def page_params(
    page: Annotated[int, Query(ge=1, le=MAX_PAGE, description="1-based page number")] = 1,
    size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE, description="Items per page")] = 25,
) -> PageParams:
    return PageParams(page=page, size=size)


PageQuery = Annotated[PageParams, Depends(page_params)]


class Page[T](BaseModel):
    items: list[T]
    total: int
    page: int
    size: int
    pages: int
    has_next: bool
    has_prev: bool

    @classmethod
    def build(cls, items: Sequence[T], total: int, params: PageParams) -> "Page[T]":
        pages = math.ceil(total / params.size) if total else 0
        return cls(
            items=list(items),
            total=total,
            page=params.page,
            size=params.size,
            pages=pages,
            has_next=params.page < pages,
            has_prev=params.page > 1,
        )


@dataclass(frozen=True)
class PageResult[T]:
    """What a **service** returns from a paginated query.

    Deliberately not `Page[T]`. `Page` is a pydantic model, and subscripting a
    generic pydantic model builds a concrete schema for the parameter — which
    fails outright for a SQLAlchemy ORM class. On Python 3.14 the annotation is
    evaluated lazily (PEP 649) and the bomb never goes off; on 3.13 the module
    raises `PydanticSchemaGenerationError` at import, so the app started fine on
    a laptop and died in the container. A plain dataclass has no schema to
    generate and keeps the response model where it belongs — in the router.
    """

    items: Sequence[T]
    total: int
    params: PageParams


async def paginate[T](
    db: AsyncSession, statement: Select[tuple[T]], params: PageParams
) -> tuple[Sequence[T], int]:
    """Run a SELECT once for the window and once for the count.

    The count strips ORDER BY (`order_by(None)`): sorting a subquery that is only
    being counted is wasted work, and Postgres rejects an ORDER BY over a column
    that is not selected in some grouped shapes.
    """
    count_statement = select(func.count()).select_from(statement.order_by(None).subquery())
    total = int((await db.execute(count_statement)).scalar_one())
    if total == 0:
        return [], 0
    result = await db.execute(statement.offset(params.offset).limit(params.size))
    return result.scalars().unique().all(), total


async def paginated[T](
    db: AsyncSession, statement: Select[tuple[T]], params: PageParams
) -> PageResult[T]:
    """`paginate` plus the params, as one value a service can hand back."""
    items, total = await paginate(db, statement, params)
    return PageResult(items=items, total=total, params=params)
