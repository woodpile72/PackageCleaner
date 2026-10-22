# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Erick Woods
import gzip
import io
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from package_cleaner.service_activity import access_logs, discover_apache_logs, listening, log_time
from package_cleaner.recommendations import Evidence, evidence_summary, potential_clue


NOW = datetime(2026, 10, 20, tzinfo=timezone.utc)


def record(date='13/Oct/2026:22:59:21 -0500', status=404):
    return f'private-client - - [{date}] "GET /private/path?token=secret HTTP/1.1" {status} 12 "private-referrer" "private-agent"\n'


class ServiceActivityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_rotation_recent_404_and_old_success_are_distinct(self):
        current = self.root / 'access.log'; current.write_text('')
        rotated = self.root / 'access.log.1'; rotated.write_text(record() * 8)
        older = self.root / 'access.log.2.gz'
        with gzip.open(older, 'wt') as stream:
            stream.write(record('01/Jan/2024:12:00:00 +0000', 200))
        data = access_logs([current, rotated, older], now=NOW)
        self.assertEqual(data['recent_request_records'], 8)
        self.assertEqual(data['recent_status_counts'], {'404': 8})
        self.assertEqual(data['recent_successful_responses'], 0)
        self.assertEqual(data['last_observed_request'], '2026-10-14T03:59:21+00:00')
        self.assertEqual(data['last_successful_response'], '2024-01-01T12:00:00+00:00')
        self.assertFalse(data['coverage_complete'])
        self.assertNotIn('private-client', str(data))
        self.assertNotIn('token=secret', str(data))

    def test_current_successful_response_is_activity_not_human_use(self):
        path = self.root / 'access.log'; path.write_text(record(status=200))
        data = access_logs([path], now=NOW)
        evidence = Evidence(server_observations={'application_activity': data})
        self.assertTrue(evidence.activity)
        self.assertEqual(data['recent_successful_responses'], 1)
        self.assertIn('do not prove human use', evidence_summary(evidence))

    def test_lifecycle_running_and_enabled_never_become_usage(self):
        evidence = Evidence(running=True, services=[{'active': 'active', 'enabled': 'enabled', 'journal_entry': True}])
        self.assertTrue(evidence.operational)
        self.assertFalse(evidence.activity)
        self.assertIn('Running does not establish', evidence_summary(evidence))
        evidence.running = False
        evidence.services[0]['active'] = 'inactive'
        self.assertFalse(evidence.activity)
        self.assertIn('not application-usage evidence', evidence_summary(evidence))

    def test_future_and_malformed_records_cannot_create_usage_dates(self):
        path = self.root / 'access.log'
        path.write_text(record('13/Oct/2099:22:59:21 -0500') + 'service started successfully\n' +
                        record('99/Oct/2026:22:59:21 -0500'))
        data = access_logs([path], now=NOW)
        self.assertIsNone(data['last_observed_request'])
        self.assertEqual(data['future_records'], 1)
        self.assertEqual(data['unparsed_records'], 2)

    def test_empty_and_missing_logs_do_not_establish_non_use(self):
        empty = self.root / 'access.log'; empty.write_text('')
        missing = self.root / 'missing.log'
        data = access_logs([empty, missing], now=NOW)
        self.assertEqual(data['unreadable_files'], [str(missing)])
        self.assertIsNone(data['last_observed_request'])
        self.assertFalse(data['coverage_complete'])

    def test_permission_failure_reported_without_escalation(self):
        path = self.root / 'access.log'; path.write_text(record())
        with patch('package_cleaner.service_activity.open_inspection', side_effect=PermissionError):
            data = access_logs([path], now=NOW)
        self.assertEqual(data['unreadable_files'], [str(path)])
        self.assertFalse(data['request_records'])

    def test_corrupt_gzip_and_size_limits_are_visible(self):
        path = self.root / 'access.log.gz'; path.write_bytes(b'not gzip')
        self.assertEqual(access_logs([path])['unreadable_files'], [str(path)])
        plain = self.root / 'access.log'; plain.write_text(record() * 4)
        data = access_logs([plain], now=NOW, max_bytes=20)
        self.assertTrue(data['truncated'])
        self.assertEqual(data['request_records'], 0)

    def test_include_custom_paths_rotation_and_pipes(self):
        conf = self.root / 'apache2.conf'
        conf.write_text('IncludeOptional sites/*.conf\nCustomLog "|do-not-execute" combined\n')
        sites = self.root / 'sites'; sites.mkdir()
        log = self.root / 'custom.log'; log.write_text(record())
        rotated = self.root / 'custom.log.1'; rotated.write_text(record())
        (sites / 'site.conf').write_text(f'CustomLog "{log}" combined\n')
        paths, warnings = discover_apache_logs(conf, self.root / 'logs')
        self.assertIn(log, paths)
        self.assertIn(rotated, paths)
        self.assertTrue(any('piped' in text for text in warnings))

    def test_listener_without_ownership_cannot_be_attributed_by_port(self):
        with patch('package_cleaner.service_activity.command', return_value=(0, 'LISTEN 0 511 *:80 *:*', '')):
            result = listening(['apache2'])
        self.assertEqual(result['state'], 'unknown')
        with patch('package_cleaner.service_activity.command', side_effect=[(0, 'LISTEN 0 511 *:80 *:* users:(("apache2",pid=10,fd=4))', ''), (0, '', '')]):
            result = listening(['apache2'])
        self.assertEqual(result['state'], 'listening')
        self.assertEqual(result['transports'], ['TCP'])

    def test_last_observed_uses_timestamp_not_rotation_name_or_mtime(self):
        first = self.root / 'access.log'; first.write_text(record('01/Jan/2024:12:00:00 +0000'))
        last = self.root / 'access.log.99'; last.write_text(record())
        data = access_logs([last, first], now=NOW)
        self.assertEqual(data['last_observed_request'], '2026-10-14T03:59:21+00:00')


if __name__ == '__main__':
    unittest.main()
