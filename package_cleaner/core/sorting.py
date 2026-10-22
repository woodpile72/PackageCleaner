# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Erick Woods
"""Pure ordering of discovered applications; never scans or changes a transaction."""
from dataclasses import dataclass
from datetime import datetime
from .recommendations import Recommendation

OPTIONS = {
    'review_priority': 'Suggested order',
    'name_asc': 'Name (A–Z)', 'name_desc': 'Name (Z–A)',
    'size_desc': 'Size (largest first)', 'size_asc': 'Size (smallest first)',
    'activity_asc': 'Last observed activity (oldest first)',
    'activity_desc': 'Last observed activity (newest first)',
}
DEFAULTS = {'large': 'size_desc', 'potential': 'size_desc', 'support': 'size_desc',
            'servers': 'name_asc', 'advanced': 'review_priority', 'search': 'name_asc'}


@dataclass(frozen=True)
class ObservedActivity:
    timestamp: float
    recorded_date: str
    source: str
    weak: bool


def observed_activity(item: Recommendation) -> ObservedActivity | None:
    evidence = item.evidence
    activity = (evidence.server_observations or {}).get('application_activity', {})
    # Prefer recorded application requests. File access is a fallback clue, never
    # a claim of execution; service startup/running/enabled dates are not usage.
    for value, source, weak in ((activity.get('last_observed_request'), 'Application request log', False),
                                (evidence.latest_file_access, 'Executable-file access (weak evidence)', True)):
        if not isinstance(value, str):
            continue
        try:
            observed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            # Historical naive timestamps follow the collector's local timezone.
            stamp = observed.astimezone().timestamp()
        except (ValueError, OverflowError, OSError):
            continue
        return ObservedActivity(stamp, value, source, weak)
    return None


def sort_options(items: list[Recommendation]) -> dict[str, str]:
    return {key: label for key, label in OPTIONS.items()
            if (key != 'review_priority' or any(item.component_assessment for item in items))
            and (not key.startswith('activity_') or any(observed_activity(item) for item in items))}


def sort_applications(items: list[Recommendation], order: str) -> list[Recommendation]:
    if order not in OPTIONS:
        raise ValueError('Unknown application sort order.')
    # Fixed ascending tie-breakers even when the primary order is descending.
    result = sorted(items, key=lambda item: (item.title.casefold(), item.package))
    if order == 'review_priority':
        return sorted(result,key=lambda item: -(item.component_assessment.priority if item.component_assessment else item.score))
    if order.startswith('name_'):
        return sorted(result, key=lambda item: item.title.casefold(), reverse=order == 'name_desc')
    if order.startswith('size_'):
        return sorted(result, key=lambda item: item.installed_bytes, reverse=order == 'size_desc')
    known, unknown = [], []
    for item in result:
        observation = observed_activity(item)
        (known if observation else unknown).append((item, observation))
    known.sort(key=lambda pair: pair[1].timestamp, reverse=order == 'activity_desc')
    return [item for item, _ in known + unknown]
