# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""herdr_bridge.acp: the ACP command plane (provisional/experimental additive; see BOUNDARIES.md).

The upstream alpha (acpx 0.12.0) hasn't stabilized yet, so this submodule is
excluded from the 0.x semver freeze. Its public interface still follows
additive-only evolution (design doc §4; ADR 0002).
"""

from __future__ import annotations

from herdr_bridge.acp.actions import AcpActions, connect
from herdr_bridge.acp.errors import (
    AcpAdapterError,
    AcpError,
    AcpSessionError,
    AcpTimeoutError,
    AcpTransportError,
    AcpVersionError,
)
from herdr_bridge.acp.models import (
    AcpAgentSpec,
    AcpEvent,
    AcpPolicy,
    AcpSessionInfo,
    PromptResult,
)
from herdr_bridge.acp.router import (
    AcpRouter,
    CentralTower,
    create_central_tower,
    create_herdr_router,
)

__all__ = [
    "AcpActions",
    "AcpAdapterError",
    "AcpAgentSpec",
    "AcpError",
    "AcpEvent",
    "AcpPolicy",
    "AcpRouter",
    "AcpSessionError",
    "AcpSessionInfo",
    "AcpTimeoutError",
    "AcpTransportError",
    "AcpVersionError",
    "CentralTower",
    "PromptResult",
    "connect",
    "create_central_tower",
    "create_herdr_router",
]
