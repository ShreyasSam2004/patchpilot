"""Offset/limit pagination helpers for the list endpoints."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class Page:
    items: list[Any]
    page: int
    per_page: int
    total: int

    @property
    def total_pages(self) -> int:
        """Number of pages needed to cover ``total`` items."""
        if self.per_page <= 0:
            return 0
        return self.total // self.per_page

    @property
    def has_next(self) -> bool:
        return self.page < self.total_pages

    @property
    def has_previous(self) -> bool:
        return self.page > 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": self.items,
            "page": self.page,
            "per_page": self.per_page,
            "total": self.total,
            "total_pages": self.total_pages,
            "has_next": self.has_next,
            "has_previous": self.has_previous,
        }


def paginate(items: list[Any], page: int = 1, per_page: int = 10) -> Page:
    """Slice ``items`` into a page.

    ``page`` is 1-indexed. Requesting a page past the end yields an empty page
    rather than an error.
    """
    if page < 1:
        page = 1
    if per_page < 1:
        per_page = 1
    start = (page - 1) * per_page
    end = start + per_page
    return Page(items=items[start:end], page=page, per_page=per_page, total=len(items))
