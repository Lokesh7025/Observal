# SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Observal telemetry store: a single-writer DuckDB service behind HTTP.

Run with ``python -m telemetry_store``. The API and worker never open the
DuckDB file directly; they talk to this service through
``services.telemetry``.
"""
