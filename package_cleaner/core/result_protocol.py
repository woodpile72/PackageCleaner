# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Erick Woods
"""Bounded, framed results on a dedicated local socket, never parsed from logs."""
import gzip
import hashlib
import json
import os
import socket
import struct
import threading

MAX_RESULT = 1024 * 1024
MAX_SNAPSHOT = 32 * 1024 * 1024


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def send_result(record, result):
    payload = dict(result)
    snapshot = payload.pop('final_dpkg_state', [])
    raw = encoded(snapshot)
    payload.update(protocol=1, attempt=record['transport']['attempt'],
                   approval_digest=record['transport']['approval_digest'],
                   snapshot_digest=hashlib.sha256(raw).hexdigest(), snapshot_records=len(snapshot))
    message = encoded(payload)
    packed = gzip.compress(raw, mtime=0)
    if len(message) > MAX_RESULT or len(packed) > MAX_SNAPSHOT:
        raise ValueError('Result exceeds supported protocol limits; reconciliation required.')
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(10)
        connection.connect(record['transport']['address'])
        for frame in (message, packed):
            connection.sendall(struct.pack('!I', len(frame)))
            connection.sendall(frame)


def receive_exact(connection, size):
    parts = []
    while size:
        chunk = connection.recv(min(size, 65536))
        if not chunk:
            raise ValueError('Incomplete helper result frame.')
        parts.append(chunk); size -= len(chunk)
    return b''.join(parts)


def receive_frame(connection, limit):
    length = struct.unpack('!I', receive_exact(connection, 4))[0]
    if length > limit:
        raise ValueError('Oversized helper result frame.')
    return receive_exact(connection, length)


class ResultReceiver:
    def __init__(self, address, expected_uid):
        self.socket = socket.socket(socket.AF_UNIX)
        self.socket.bind(address)
        self.socket.listen(1)
        self.socket.settimeout(0.1)
        self.expected_uid = expected_uid
        self.stop = threading.Event()
        self.payload = None
        self.snapshot = None
        self.packed = None
        self.error = None
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        try:
            while True:
                try:
                    connection, _ = self.socket.accept()
                    break
                except socket.timeout:
                    if self.stop.is_set():
                        return
            with connection:
                connection.settimeout(3)
                _, uid, _ = struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                if uid != self.expected_uid:
                    raise ValueError('Unexpected helper identity.')
                payload = json.loads(receive_frame(connection, MAX_RESULT))
                packed = receive_frame(connection, MAX_SNAPSHOT)
                # Bound expansion as well as compressed input.
                import io
                with gzip.GzipFile(fileobj=io.BytesIO(packed)) as stream:
                    raw = stream.read(MAX_SNAPSHOT + 1)
                if len(raw) > MAX_SNAPSHOT:
                    raise ValueError('Oversized expanded package snapshot.')
                snapshot = json.loads(raw)
                if not isinstance(payload, dict) or not isinstance(snapshot, list) or not all(isinstance(x, str) for x in snapshot):
                    raise ValueError('Malformed helper result or snapshot.')
                if payload.get('snapshot_digest') != hashlib.sha256(raw).hexdigest() or payload.get('snapshot_records') != len(snapshot):
                    raise ValueError('Helper snapshot checksum or count mismatch.')
                self.payload, self.snapshot, self.packed = payload, snapshot, packed
        except (OSError, ValueError, EOFError, struct.error) as exc:
            self.error = str(exc)

    def finish(self):
        self.stop.set()
        self.thread.join(timeout=4)
        self.socket.close()
        if self.thread.is_alive():
            self.error = 'Helper result reception did not finish.'


def verify_result(payload, snapshot, record, process_status):
    """Never infer success from exit status alone or merge into a fallback error."""
    if not isinstance(payload, dict):
        raise ValueError('No verified result received from the administrator helper.')
    transport = record['transport']
    if (payload.get('protocol') != 1 or payload.get('attempt') != transport['attempt']
            or payload.get('approval_digest') != transport['approval_digest']):
        raise ValueError('Helper result does not match this approved attempt.')
    status = payload.get('status')
    if status not in ('completed', 'incomplete', 'rejected', 'unknown'):
        raise ValueError('Invalid helper completion status.')
    expected = set(record['plan']['removed'])
    for key in ('removed', 'remaining', 'unknown'):
        values = payload.get(key)
        if not isinstance(values, list) or not all(isinstance(n, str) for n in values) or not set(values) <= expected:
            raise ValueError('Invalid package outcomes in helper result.')
    if status == 'completed':
        if (process_status != 0 or payload.get('exit_status') != 0
                or payload.get('execution_started') is not True
                or payload.get('validation') != 'passed: locked APT plan exactly matches approval'
                or payload.get('error') or payload.get('observation_error') or payload.get('unexpected_state_change')
                or payload['remaining'] or payload['unknown'] or set(payload['removed']) != expected):
            raise ValueError('Helper success was not confirmed by all completion checks.')
        observed = payload.get('final_state')
        if not isinstance(observed, dict) or set(observed) != expected or any(v not in ('not-installed', 'config-files') for v in observed.values()):
            raise ValueError('Final selected-package state does not match the approved removals.')
        def parsed(rows):
            result = {}
            for row in rows:
                fields = row.split('\t')
                if len(fields) != 3 or fields[0] in result:
                    raise ValueError('Invalid dpkg snapshot record.')
                result[fields[0]] = fields[1:]
            return result
        before, after = parsed(record['dpkg_state']), parsed(snapshot)
        if {n:v for n,v in before.items() if n not in expected} != {n:v for n,v in after.items() if n not in expected}:
            raise ValueError('Unapproved package-state changes require reconciliation.')
        for name in expected:
            if name in after:
                abbrev = after[name][1]
                if len(abbrev) != 3 or abbrev[1] not in ('n', 'c') or abbrev[2] != ' ':
                    raise ValueError('Approved package is not fully removed in the final snapshot.')
                wanted = 'config-files' if abbrev[1] == 'c' else 'not-installed'
                if observed[name] != wanted:
                    raise ValueError('Conflicting final-state observations.')
            elif observed[name] != 'not-installed':
                raise ValueError('Conflicting final-state observations.')
    return dict(payload, helper_verified=True)
