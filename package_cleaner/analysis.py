# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Erick Woods
"""Compatibility import; implementation lives in the UI-independent core."""
import sys
from importlib import import_module
sys.modules[__name__] = import_module(".core.analysis", __package__)
