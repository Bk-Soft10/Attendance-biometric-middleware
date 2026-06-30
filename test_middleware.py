#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Standalone tests for the biometric middleware (no Odoo / no device needed).

Run with:
    python3 -m unittest middleware/test_middleware.py -v
or:
    python3 middleware/test_middleware.py
"""
import unittest
from unittest import mock

import biometric_middleware as bm


def _fake_response(status=200, payload=None, text=''):
    resp = mock.Mock()
    resp.status_code = status
    resp.json.return_value = payload if payload is not None else {}
    resp.text = text
    return resp


class TestCustomClient(unittest.TestCase):
    def setUp(self):
        self.client = bm.OdooAPIClient('https://x.odoo.com', 'devkey', 'DEV0001')
        self.client.session = mock.Mock()

    def test_send_batch_normalizes_success(self):
        self.client.session.post.return_value = _fake_response(
            200, {'success': True, 'message': 'ok', 'data': {'success': 3, 'failed': 0}})
        res = self.client.send_batch([{'pin': '1'}])
        self.assertTrue(res['ok'])
        self.assertEqual(res['sent'], 3)
        self.assertEqual(res['failed'], 0)

    def test_send_batch_per_device_override(self):
        self.client.session.post.return_value = _fake_response(
            200, {'success': True, 'data': {'success': 1, 'failed': 0}})
        self.client.send_batch([{'pin': '1'}], device_code='DEV0002', api_key='otherkey')
        _, kwargs = self.client.session.post.call_args
        self.assertEqual(kwargs['json']['api_key'], 'otherkey')
        self.assertEqual(kwargs['json']['device_code'], 'DEV0002')
        self.assertEqual(kwargs['headers']['X-API-Key'], 'otherkey')

    def test_fetch_fleet(self):
        self.client.session.get.return_value = _fake_response(
            200, {'success': True, 'data': {'devices': [
                {'device_code': 'DEV0001', 'host': '192.168.2.12', 'port': 4370},
            ]}})
        fleet = self.client.fetch_fleet()
        self.assertEqual(len(fleet), 1)
        self.assertEqual(fleet[0]['host'], '192.168.2.12')

    def test_send_batch_handles_exception(self):
        self.client.session.post.side_effect = RuntimeError('boom')
        res = self.client.send_batch([{'pin': '1'}, {'pin': '2'}])
        self.assertFalse(res['ok'])
        self.assertEqual(res['failed'], 2)


class TestJson2Client(unittest.TestCase):
    def setUp(self):
        self.client = bm.OdooJson2Client('https://x.odoo.com', 'userkey', 'DEV0001')
        self.client.session = mock.Mock()

    def test_send_batch_normalizes(self):
        self.client.session.post.return_value = _fake_response(
            200, {'success': True, 'created': 5, 'failed': 0, 'duplicates': 2})
        res = self.client.send_batch([{'pin': '1'}], device_code='DEV0009')
        self.assertTrue(res['ok'])
        self.assertEqual(res['sent'], 5)
        self.assertIn('duplicates=2', res['message'])
        _, kwargs = self.client.session.post.call_args
        self.assertEqual(kwargs['json']['device_code'], 'DEV0009')

    def test_send_batch_http_error(self):
        self.client.session.post.return_value = _fake_response(403, text='forbidden')
        res = self.client.send_batch([{'pin': '1'}])
        self.assertFalse(res['ok'])
        self.assertIn('403', res['message'])

    def test_fetch_fleet(self):
        self.client.session.post.return_value = _fake_response(
            200, {'count': 2, 'devices': [{'device_code': 'A'}, {'device_code': 'B'}]})
        fleet = self.client.fetch_fleet()
        self.assertEqual([d['device_code'] for d in fleet], ['A', 'B'])


class TestPushLogs(unittest.TestCase):
    def test_batches_and_aggregates(self):
        odoo = mock.Mock()
        odoo.send_batch.return_value = {'ok': True, 'sent': 100, 'failed': 0, 'message': ''}
        logs = [{'pin': str(i)} for i in range(250)]
        sent, failed = bm.push_logs(odoo, logs, {'batch_size': 100, 'retry_attempts': 1})
        self.assertEqual(odoo.send_batch.call_count, 3)  # 100 + 100 + 50
        self.assertEqual(sent, 300)  # 3 batches * 100 (mock returns 100 each)
        self.assertEqual(failed, 0)

    def test_retry_then_fail(self):
        odoo = mock.Mock()
        odoo.send_batch.return_value = {'ok': False, 'sent': 0, 'failed': 1, 'message': 'err'}
        with mock.patch.object(bm.time, 'sleep'):
            sent, failed = bm.push_logs(odoo, [{'pin': '1'}],
                                        {'batch_size': 100, 'retry_attempts': 3})
        self.assertEqual(odoo.send_batch.call_count, 3)
        self.assertEqual(failed, 1)


class TestRunOnceFleet(unittest.TestCase):
    def test_fleet_mode_syncs_all_devices(self):
        odoo = mock.Mock()
        odoo.fetch_fleet.return_value = [
            {'name': 'A', 'device_code': 'DEV1', 'host': '10.0.0.1', 'port': 4370,
             'password': 0, 'api_key': 'k1'},
            {'name': 'B', 'device_code': 'DEV2', 'host': '10.0.0.2', 'port': 4370,
             'password': 0, 'api_key': 'k2'},
        ]
        odoo.send_batch.return_value = {'ok': True, 'sent': 1, 'failed': 0, 'message': ''}

        fake_dev = mock.Mock()
        fake_dev.connect.return_value = True
        fake_dev.get_attendance_logs.return_value = [
            {'pin': '1', 'timestamp': '2025-01-15 08:00:00', 'punch': '0'}]

        config = {'odoo': {'auto_discover': True},
                  'sync': {'batch_size': 100, 'retry_attempts': 1, 'since_hours': 24}}

        with mock.patch.object(bm, 'BiometricDevice', return_value=fake_dev):
            ok = bm.run_once(odoo, config)

        self.assertTrue(ok)
        self.assertEqual(fake_dev.connect.call_count, 2)
        self.assertEqual(odoo.send_batch.call_count, 2)
        # Each device pushed with its own device_code
        codes = {c.kwargs.get('device_code') for c in odoo.send_batch.call_args_list}
        self.assertEqual(codes, {'DEV1', 'DEV2'})

    def test_fleet_skips_device_without_host(self):
        odoo = mock.Mock()
        odoo.fetch_fleet.return_value = [{'name': 'NoIP', 'device_code': 'X', 'host': ''}]
        config = {'odoo': {'auto_discover': True},
                  'sync': {'batch_size': 100, 'retry_attempts': 1, 'since_hours': 24}}
        with mock.patch.object(bm, 'BiometricDevice') as MockDev:
            ok = bm.run_once(odoo, config)
        self.assertTrue(ok)
        MockDev.assert_not_called()


class TestPunchMapping(unittest.TestCase):
    def test_valid_punches_pass_through(self):
        for v in ('0', '1', '2', '3', '4', '5'):
            self.assertEqual(bm.map_punch(v), v)

    def test_integer_input_is_stringified(self):
        self.assertEqual(bm.map_punch(3), '3')

    def test_unexpected_values_map_to_255(self):
        for v in ('6', '99', 'x', '', None, '-1'):
            self.assertEqual(bm.map_punch(v), '255')


class TestTimezoneConversion(unittest.TestCase):
    def test_naive_local_converted_to_utc(self):
        # Riyadh is UTC+3 (no DST): 08:00 local -> 05:00 UTC
        out = bm.to_utc_iso(bm.datetime(2025, 1, 15, 8, 0, 0), 'Asia/Riyadh')
        self.assertTrue(out.startswith('2025-01-15T05:00:00'))
        self.assertIn('+00:00', out)

    def test_already_aware_is_normalized_to_utc(self):
        aware = bm.datetime(2025, 1, 15, 8, 0, 0,
                            tzinfo=bm.timezone(bm.timedelta(hours=3)))
        out = bm.to_utc_iso(aware, 'UTC')
        self.assertTrue(out.startswith('2025-01-15T05:00:00'))

    def test_unknown_tz_assumed_utc(self):
        out = bm.to_utc_iso(bm.datetime(2025, 1, 15, 8, 0, 0), 'Not/AZone')
        self.assertTrue(out.startswith('2025-01-15T08:00:00'))


class TestConfig(unittest.TestCase):
    def test_sample_config_roundtrip(self):
        import tempfile, os
        path = os.path.join(tempfile.mkdtemp(), 'config.ini')
        bm.create_sample_config(path)
        cfg = bm.load_config(path)
        self.assertIn('odoo', cfg)
        self.assertTrue(cfg['odoo']['transport'] in ('json2', 'custom'))
        self.assertIsInstance(cfg['odoo']['auto_discover'], bool)
        self.assertIsInstance(cfg['sync']['batch_size'], int)


if __name__ == '__main__':
    unittest.main(verbosity=2)
