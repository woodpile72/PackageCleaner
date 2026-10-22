# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Erick Woods
import os
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from package_cleaner.terminal import local_date


class LocalDateTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {'TZ': 'America/Chicago'})
        self.environment.start()
        time.tzset()
        self.addCleanup(self.restore_timezone)
        self.now = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)

    def restore_timezone(self):
        self.environment.stop()
        time.tzset()

    def test_local_calendar_day_and_relative_age(self):
        self.assertEqual(local_date('2025-09-04T00:44:46.037218+00:00', self.now),
                         'Sep 3, 2025 (about 13 months ago)')

    def test_winter_timezone_and_utc_suffix(self):
        self.assertTrue(local_date('2026-01-02T05:30:00Z', self.now).startswith('Jan 1, 2026'))

    def test_naive_history_is_local(self):
        self.assertTrue(local_date('2025-09-04', self.now).startswith('Sep 4, 2025'))

    def test_unknown_and_invalid_dates(self):
        self.assertEqual(local_date(None, self.now), 'unknown')
        self.assertEqual(local_date('not a date', self.now), 'unknown')

    def test_future_date_does_not_claim_elapsed_use(self):
        self.assertIn('future date', local_date('2027-01-01', self.now))
        self.assertNotIn('ago', local_date('2027-01-01', self.now))
