# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""Light mode task definitions (Phase 1 locks in the first task)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TaskSpec:
    """A light-mode task that can be dispatched and accepted."""

    task_id: str
    title: str
    user_prompt: str
    agent_prompt: str
    success_markers: tuple[str, ...]
    expected_files: tuple[str, ...]
    acceptance_hints: tuple[str, ...]


FIRST_TASK = TaskSpec(
    task_id="thumbnail-py",
    title="Create an image thumbnail function + unit tests",
    user_prompt=(
        "Write me a Python function that takes an image path and produces a "
        "300px-wide thumbnail, plus unit tests confirming it works. Use the Pillow package."
    ),
    agent_prompt=(
        "You must use a terminal tool (cat) to actually create files in the current working directory:\n\n"
        "1. Use cat > thumbnail.py << 'EOF' to create thumbnail.py, implementing an image "
        "thumbnail function (accepts a path and a width, uses Pillow, resizes proportionally, and saves the file).\n\n"
        "2. Use cat > test_thumbnail.py << 'EOF' to create the corresponding pytest tests "
        "(at least two cases; mocking is fine).\n\n"
        "3. Install Pillow, then run pytest test_thumbnail.py -q to confirm it passes.\n\n"
        "Only after you have actually created the files, run the tests, and they all pass, "
        "output on the **last line**, exactly (no other text, prefix, or explanation):\n"
        "[[[THUMBNAIL_COMPLETE_20260722]]]\n\n"
        "If it fails, clearly explain why — do not output the marker above."
    ),
    success_markers=("[[[THUMBNAIL_COMPLETE_20260722]]]",),
    expected_files=("thumbnail.py", "test_thumbnail.py"),
    acceptance_hints=(
        "Please confirm /tmp/sandbox-test/thumbnail.py and test_thumbnail.py exist",
        "You can run pytest to verify",
        (
            "If Claude didn't actually create the files, next time explicitly say "
            "'please use cat to create the files'"
        ),
    ),
)


SECOND_TASK = TaskSpec(
    task_id="fastapi-health",
    title="Create a simple FastAPI health-check endpoint + tests",
    user_prompt=(
        "Create a simple FastAPI application with a /health endpoint that returns "
        "{'status': 'ok'}, and write pytest tests confirming the endpoint works correctly. "
        "Use FastAPI and httpx for testing."
    ),
    agent_prompt=(
        "You must use a terminal tool (cat) to actually create files in the current working directory:\n\n"
        "1. Use cat > main.py << 'EOF' to create a FastAPI app, containing:\n"
        "   from fastapi import FastAPI\n"
        "   app = FastAPI()\n"
        "   @app.get('/health')\n"
        "   def health():\n"
        "       return {'status': 'ok'}\n"
        "   EOF\n\n"
        "2. Create test_main.py and use httpx to test that the /health endpoint returns the correct response.\n\n"
        "3. Install the necessary packages, then run pytest test_main.py -q to confirm it passes.\n\n"
        "Only after you have actually created the files, installed the packages, run the tests, "
        "and they all pass, output on the **last line**, exactly (no other text):\n"
        "[[[FASTAPI_COMPLETE_20260722]]]\n\n"
        "If it fails, clearly explain why — do not output the marker above."
    ),
    success_markers=("[[[FASTAPI_COMPLETE_20260722]]]",),
    expected_files=("main.py", "test_main.py"),
    acceptance_hints=(
        "Please confirm main.py and test_main.py exist",
        "You can run pytest to verify",
        (
            "If Claude didn't actually create the files, explicitly instruct "
            "'please use cat to create the files'"
        ),
    ),
)

def get_task(task_id: str = "thumbnail-py") -> TaskSpec:
    """Look up a task by id; Phase 1 supports thumbnail-py and fastapi-health."""
    if task_id in ("thumbnail-py", "first", "default"):
        return FIRST_TASK
    if task_id in ("fastapi-health", "fastapi", "second"):
        return SECOND_TASK
    raise KeyError(f"unknown task {task_id!r}; supported: thumbnail-py, fastapi-health")
