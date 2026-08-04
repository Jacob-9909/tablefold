"""Introspection contract.

An introspector's only job is to produce a faithful :class:`PhysicalSchema`.
It performs no inference and no folding — everything downstream reads the same
IR whether it came from a live connection or a DDL file on disk.
"""

from __future__ import annotations

from typing import Protocol

from tablefold.ir import PhysicalSchema


class Introspector(Protocol):
    def introspect(self) -> PhysicalSchema:
        """Return the physical schema this source describes."""
        ...
