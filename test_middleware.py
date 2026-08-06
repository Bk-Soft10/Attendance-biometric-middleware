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

    def test_status_is_verify_mode_not_punch(self):
        """Fingerprint verify mode (status=1) must NOT become Check Out."""
        self.assertEqual(bm.map_auth_type(1), 'fingerprint')
        self.assertEqual(bm.map_punch(0), '0')   # punch=0 → Check In
        self.assertEqual(bm.map_punch(1), '1')   # punch=1 → Check Out


class TestAttendanceRecordMapping(unittest.TestCase):
    def test_pyzk_fields_not_swapped(self):
        record = mock.Mock()
        record.user_id = '178'
        record.timestamp = bm.datetime(2026, 7, 16, 8, 0, 0)
        record.punch = 0          # Check In
        record.status = 1         # Fingerprint verify
        entry = bm.attendance_record_to_log(record, 'Asia/Riyadh')
        self.assertEqual(entry['punch'], '0')
        self.assertEqual(entry['auth_type'], 'fingerprint')
        self.assertEqual(entry['raw_punch'], 0)
        self.assertEqual(entry['raw_status'], 1)

    def test_check_out_with_rfid_verify(self):
        record = mock.Mock()
        record.user_id = '176'
        record.timestamp = bm.datetime(2026, 7, 15, 17, 58, 40)
        record.punch = 1          # Check Out
        record.status = 3         # RFID verify (was wrongly mapped as Break In)
        entry = bm.attendance_record_to_log(record, 'Asia/Riyadh')
        self.assertEqual(entry['punch'], '1')
        self.assertEqual(entry['auth_type'], 'rfid')


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


class TestBiosenseWebParsing(unittest.TestCase):
    SAMPLE_HTML = """
    <html><body><h1>Access Log</h1>
    <table>
      <tr><th>No.</th><th>User ID</th><th>User Name</th><th>Date</th>
          <th>Time</th><th>IN/OUT</th><th>Note</th></tr>
      <tr><td>1</td><td>176(1)</td><td>Ali</td><td>07/16/2026</td>
          <td>08:20:00</td><td>IN</td><td>Fingerprint</td></tr>
      <tr><td>2</td><td>176(1)</td><td>Ali</td><td>07/16/2026</td>
          <td>17:45:11</td><td>OUT</td><td>Card</td></tr>
      <tr><td>3</td><td>100</td><td></td><td>07/15/2026</td>
          <td>09:01</td><td>IN</td><td></td></tr>
    </table>
    </body></html>
    """

    def test_parse_html_maps_in_out_to_punch(self):
        client = bm.BiosenseWebClient('192.168.2.49', tz_name='Asia/Riyadh')
        rows = client._parse_access_log_html(self.SAMPLE_HTML)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]['pin'], '176')
        self.assertEqual(rows[0]['direction'], 'IN')
        self.assertEqual(rows[1]['direction'], 'OUT')
        self.assertEqual(rows[0]['timestamp'].hour, 8)

    def test_get_attendance_logs_converts_to_utc_and_punch(self):
        client = bm.BiosenseWebClient('192.168.2.49', tz_name='Asia/Riyadh')
        client._logged_in = True
        with mock.patch.object(client, '_fetch_access_log_html',
                               return_value=self.SAMPLE_HTML):
            logs = client.get_attendance_logs(since=None)
        self.assertEqual(len(logs), 3)
        self.assertEqual(logs[0]['punch'], '0')   # IN → Check In
        self.assertEqual(logs[1]['punch'], '1')   # OUT → Check Out
        self.assertEqual(logs[1]['auth_type'], 'rfid')
        self.assertIn('+00:00', logs[0]['timestamp'])
        # 08:20 Riyadh = 05:20 UTC
        self.assertTrue(logs[0]['timestamp'].startswith('2026-07-16T05:20:00'))

    def test_login_uses_http_basic_auth(self):
        """Device answering 401 must be retried with HTTP Basic credentials."""
        client = bm.BiosenseWebClient('192.168.2.49', username='user',
                                      password='secret')

        def fake_get(url, **kwargs):
            resp = mock.Mock()
            if kwargs.get('auth') is None:
                resp.status_code = 401
                resp.headers = {'WWW-Authenticate': 'Basic realm="BIOSENSE"'}
                resp.text = 'Unauthorized'
                resp.content = b''
                return resp
            resp.status_code = 200
            resp.headers = {}
            resp.text = '<html><body>Access Log</body></html>'
            resp.content = resp.text.encode()
            return resp

        client.session = mock.Mock()
        client.session.get.side_effect = fake_get
        self.assertTrue(client.connect())
        self.assertIsNotNone(client.session.auth)
        self.assertEqual(client.session.auth.username, 'user')
        self.assertEqual(client.session.auth.password, 'secret')

    def test_frameset_shell_counts_as_authenticated(self):
        """Chiyu UIs are frameset shells with none of the usual markers."""
        shell = ('<html><frameset cols="180,*">'
                 '<frame name="menu" src="menu.htm">'
                 '<frame name="main" src="status.htm">'
                 '</frameset></html>')
        self.assertTrue(bm.BiosenseWebClient._looks_authenticated(shell))

    def test_login_form_is_not_authenticated(self):
        form = ('<html><body><form><input type="text" name="Username">'
                '<input type="password" name="Password"></form></body></html>')
        self.assertFalse(bm.BiosenseWebClient._looks_authenticated(form))

    def test_rejection_page_is_not_authenticated(self):
        self.assertFalse(
            bm.BiosenseWebClient._looks_authenticated('<html>Unauthorized</html>'))

    def test_access_log_found_inside_frame(self):
        client = bm.BiosenseWebClient('192.168.2.49', tz_name='Asia/Riyadh')
        shell = '<html><frameset><frame src="menu.htm"></frameset></html>'

        def fake_get(url, **kwargs):
            resp = mock.Mock()
            resp.status_code = 200
            resp.headers = {}
            if url.endswith('menu.htm'):
                resp.text = self.SAMPLE_HTML
            elif url.endswith('/'):
                resp.text = shell
            else:
                resp.text = '<html>nothing</html>'
            resp.content = resp.text.encode()
            return resp

        client.session = mock.Mock()
        client.session.get.side_effect = fake_get
        html = client._fetch_access_log_html()
        self.assertIsNotNone(html)
        self.assertIn('Access Log', html)

    def test_login_fails_when_credentials_rejected(self):
        client = bm.BiosenseWebClient('192.168.2.49', username='x', password='y')

        def always_401(url, **kwargs):
            resp = mock.Mock()
            resp.status_code = 401
            resp.headers = {'WWW-Authenticate': 'Basic realm="BIOSENSE"'}
            resp.text = 'Unauthorized'
            resp.content = b''
            return resp

        client.session = mock.Mock()
        client.session.get.side_effect = always_401
        client.session.post.side_effect = always_401
        self.assertFalse(client.connect())

    def test_stale_port_redirect_in_sync(self):
        """device_port 2000/4370 must fall back to HTTP 80 for BIOSENSE."""
        odoo = mock.Mock()
        odoo.send_batch.return_value = {'ok': True, 'sent': 1, 'failed': 0, 'message': ''}
        fake = mock.Mock()
        fake.connect.return_value = True
        fake.get_attendance_logs.return_value = [
            {'pin': '1', 'timestamp': '2026-07-16T05:00:00+00:00', 'punch': '0'}]
        with mock.patch.object(bm, 'BiosenseWebClient', return_value=fake) as Ctor:
            ok = bm.sync_device(
                {'name': 'Back', 'host': '192.168.2.49', 'port': 2000,
                 'device_type': 'biosense_t', 'device_code': 'DEVX',
                 'timezone': 'Asia/Riyadh'},
                odoo, {'batch_size': 100, 'retry_attempts': 1, 'since_hours': 24},
            )
        self.assertTrue(ok)
        self.assertEqual(Ctor.call_args.kwargs['port'], 80)


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
