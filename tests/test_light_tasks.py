# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

import pytest

from herdr_bridge.light.tasks import FIRST_TASK, get_task


def test_first_task_has_required_fields():
    t = FIRST_TASK
    assert t.task_id == "thumbnail-py"
    assert "thumbnail" in t.title.lower()
    assert "thumbnail.py" in t.expected_files
    assert "test_thumbnail.py" in t.expected_files
    assert "THUMBNAIL_COMPLETE_20260722" in str(t.success_markers)
    assert t.user_prompt
    assert t.agent_prompt
    assert t.acceptance_hints


def test_get_task_aliases():
    assert get_task("thumbnail-py") is FIRST_TASK
    assert get_task("first") is FIRST_TASK
    assert get_task("default") is FIRST_TASK


def test_get_task_fastapi_health_aliases():
    from herdr_bridge.light.tasks import SECOND_TASK

    assert get_task("fastapi-health") is SECOND_TASK
    assert get_task("fastapi") is SECOND_TASK
    assert get_task("second") is SECOND_TASK


def test_get_task_unknown():
    with pytest.raises(KeyError, match="unknown task"):
        get_task("nonexistent")
