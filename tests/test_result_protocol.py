# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Erick Woods
"""Exercise real framed communication with fake execution; never invoke APT."""
import copy
import gzip
import json
import socket
import struct
import tempfile
import unittest
from pathlib import Path
from package_cleaner.execution import launch
from package_cleaner.result_protocol import send_result, MAX_RESULT


class ResultProtocolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.record = {'plan': {'requested': ['app:amd64'], 'removed': ['app:amd64']},
                       'dpkg_state': ['app:amd64\t1\tii ', 'other:amd64\t1\tii ']}
        self.payload = dict(status='completed', validation='passed: locked APT plan exactly matches approval',
                            execution_started=True, exit_status=0, removed=['app:amd64'],
                            remaining=[], unknown=[], final_state={'app:amd64': 'not-installed'},
                            final_dpkg_state=['other:amd64\t1\tii '])

    def run_result(self, payload=None, code=0, output='Removing app...\n'):
        def runner(command, stream, record):
            stream.write(output)
            send_result(record, self.payload if payload is None else payload)
            return code
        return launch(self.record, self.tmp.name, runner=runner)

    def assert_unknown(self, result):
        self.assertEqual(result['status'], 'unknown')
        self.assertFalse(result['helper_verified'])
        self.assertTrue(result['reconciliation_required'])

    def test_success_does_not_retain_fallback_error(self):
        result = self.run_result()
        self.assertEqual(result['status'], 'completed')
        self.assertTrue(result['helper_verified'])
        self.assertNotIn('error', result)
        self.assertNotIn('final_dpkg_state', result)

    def test_exit_success_missing_result_is_unknown(self):
        self.assert_unknown(launch(self.record, self.tmp.name, runner=lambda *args: 0))

    def test_success_payload_nonzero_process_is_unknown(self):
        self.assert_unknown(self.run_result(code=1))

    def test_package_manager_failure_cannot_claim_success(self):
        self.payload['exit_status'] = 1
        self.assert_unknown(self.run_result())

    def test_unexpected_final_state(self):
        for change in ('other changed', 'still installed', 'conflicting observation'):
            payload = copy.deepcopy(self.payload)
            if change == 'other changed':
                payload['final_dpkg_state'] = ['other:amd64\t2\tii ']
            elif change == 'still installed':
                payload['final_dpkg_state'].append('app:amd64\t1\tii ')
            else:
                payload['final_state']['app:amd64'] = 'installed'
            self.assert_unknown(self.run_result(payload))

    def test_config_files_are_valid_removal(self):
        self.payload['final_state']['app:amd64'] = 'config-files'
        self.payload['final_dpkg_state'].append('app:amd64\t1\trc ')
        self.assertEqual(self.run_result()['status'], 'completed')

    def test_malformed_truncated_and_oversized_frames(self):
        for data in (struct.pack('!I', 1) + b'{', struct.pack('!I', 200) + b'partial', struct.pack('!I', MAX_RESULT + 1)):
            def runner(command, output, record):
                with socket.socket(socket.AF_UNIX) as connection:
                    connection.connect(record['transport']['address'])
                    connection.sendall(data)
                return 0
            self.assert_unknown(launch(self.record, self.tmp.name, runner=runner))

    def test_wrong_attempt(self):
        def runner(command, output, record):
            record = copy.deepcopy(record)
            record['transport']['attempt'] = 'another attempt'
            send_result(record, self.payload)
            return 0
        self.assert_unknown(launch(self.record, self.tmp.name, runner=runner))

    def test_interruption_without_result(self):
        def runner(*args):
            raise KeyboardInterrupt()
        self.assert_unknown(launch(self.record, self.tmp.name, runner=runner))

    def test_confirmed_interrupted_execution_is_incomplete(self):
        self.payload.update(status='incomplete', exit_status=1, error='Execution interrupted.')
        result = self.run_result(code=1)
        self.assertEqual(result['status'], 'incomplete')
        self.assertTrue(result['helper_verified'])

    def test_large_inventory_and_small_logs(self):
        rows = [f'package-{i}:amd64\t1.0\tii ' for i in range(12000)]
        self.record['dpkg_state'] += rows
        self.payload['final_dpkg_state'] += rows
        result = self.run_result()
        directory = Path(result['log_directory'])
        self.assertEqual(result['status'], 'completed')
        self.assertLess((directory / 'result.json').stat().st_size, 4096)
        self.assertLess((directory / 'execution.log').stat().st_size, 4096)
        self.assertEqual(json.loads(gzip.decompress((directory / 'after-dpkg.json.gz').read_bytes())), self.payload['final_dpkg_state'])
        self.assertEqual(json.loads(gzip.decompress((directory / 'approval.json.gz').read_bytes()))['dpkg_state'], self.record['dpkg_state'])
        self.assertNotIn('package-11999', (directory / 'execution.log').read_text())

    def test_large_stdout_is_compressed_not_used_as_protocol(self):
        output = 'PACKAGE_CLEANER_RESULT ' + json.dumps(self.payload) + '\n' + ('diagnostic output\n' * 50000)
        result = self.run_result(output=output)
        directory = Path(result['log_directory'])
        self.assertEqual(result['status'], 'completed')
        self.assertLess((directory / 'execution.log').stat().st_size, 20000)
        self.assertEqual(gzip.decompress((directory / 'package-manager.log.gz').read_bytes()).decode(), output)
        self.assertFalse((directory / 'output.pending').exists())

    def test_log_marker_alone_cannot_report_success(self):
        def runner(command, output, record):
            output.write('PACKAGE_CLEANER_RESULT ' + json.dumps(self.payload))
            return 0
        self.assert_unknown(launch(self.record, self.tmp.name, runner=runner))
