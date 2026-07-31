# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""Light mode — the command tower experience for occasional users (Phase 1).

Hides fleet / actor / rules complexity, providing:
- A single task definition (first task: thumbnail function + tests)
- Automatic agent selection, dispatch, wait, and acceptance reporting
- User-language error messages
"""

from herdr_bridge.light.commander import LightCommander, LightResult
from herdr_bridge.light.report import format_user_report
from herdr_bridge.light.tasks import FIRST_TASK, TaskSpec

__all__ = [
    "FIRST_TASK",
    "LightCommander",
    "LightResult",
    "TaskSpec",
    "format_user_report",
]
