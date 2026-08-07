# SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""The per-user actionable inbox.

Endpoints call ``sources``; ``sources`` calls ``deliver``; ``deliver`` writes in
the caller's transaction. Nothing in here goes through the event bus, which
cannot promise delivery — see ``delivery.py``.
"""

from services.inbox.delivery import (
    deliver,
    deliver_one,
    mark_read,
    mark_unread,
    record_event,
    resolve,
    supersede,
)
from services.inbox.registry import SPECS, KindSpec, Subject, spec_for
from services.inbox.visibility import filter_visible, visible_to

__all__ = [
    "SPECS",
    "KindSpec",
    "Subject",
    "deliver",
    "deliver_one",
    "filter_visible",
    "mark_read",
    "mark_unread",
    "record_event",
    "resolve",
    "spec_for",
    "supersede",
    "visible_to",
]
