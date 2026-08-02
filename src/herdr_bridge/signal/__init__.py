# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""Herdr Bridge Signal — cross-tower wake-up acceleration.

See docs/herdr-bridge-signal-design.md for the full design (v6). This package
adds a push-wake trigger on top of the existing 4+1 communication layers; it
never changes their behavior, and RemaGraph (Primary) remains the sole source
of truth for message content — this package only ever carries wake control
signals.
"""

from __future__ import annotations
