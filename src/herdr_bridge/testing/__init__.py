# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""herdr_bridge.testing — FakeHerdrServer contract-test double.

Lets downstream consumers run contract tests without a real herdr server.
"""

from herdr_bridge.testing._server import FakeApiError, FakeHerdrServer, Handler

__all__ = ["FakeApiError", "FakeHerdrServer", "Handler"]
