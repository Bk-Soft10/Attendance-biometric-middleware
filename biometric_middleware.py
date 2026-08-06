#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Biometric Middleware Script for Odoo Hybrid Integration
=======================================================

This script acts as a bridge between biometric devices and Odoo SH.
It connects to biometric devices on the local network, retrieves attendance
logs, and pushes them to the Odoo API endpoint.

Compatible Devices:
- iWeb F18 / ZKTeco F18
- BIOSENSE-T series
- All ZKTeco devices with TCP/IP support
- Any device supported by pyzk library

Usage:
    python biometric_middleware.py --config config.ini
    python biometric_middleware.py --host 192.168.2.12 --port 4370 --api-key YOUR_KEY

Requirements:
    pip install pyzk requests

Author: Custom Development
Version: 1.0.0
"""

import argparse
import configparser
import json
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional

import requests

# Timezone database: prefer the stdlib (Python 3.9+), fall back to pytz.
try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except ImportError:  # pragma: no cover
    ZoneInfo = None
try:
    import pytz  # optional fallback
except ImportError:  # pragma: no cover
    pytz = None

# Optional: pyzk for direct ZKTeco communication
try:
    from zk import ZK
    # from zk.const import ATTENDANCE_STATUS
    PYZK_AVAILABLE = True
except ImportError:
    PYZK_AVAILABLE = False
    print("Warning: pyzk not installed. Direct ZKTeco connection will not be available.")
    print("Install with: pip install pyzk")

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
    ]
)
logger = logging.getLogger('biometric_middleware')


# ---------------------------------------------------------------------------
# Shared helpers: strict punch mapping + timezone-aware UTC conversion
# ---------------------------------------------------------------------------
# Strict punch enum, identical to Odoo's biometric.log.punch_type selection.
VALID_PUNCH_TYPES = {'0', '1', '2', '3', '4', '5'}
UNDEFINED_PUNCH = '255'


def map_punch(value) -> str:
    """Map a raw device punch value to our strict enum.

  In pyzk ``Attendance.punch`` is the check-in/out type:
    ``'0'``=Check In, ``'1'``=Check Out, ``'2'``=Break Out, ``'3'``=Break In,
    ``'4'``=Overtime In, ``'5'``=Overtime Out.

  ``Attendance.status`` is the *verification mode* (fingerprint, card, …) —
  never pass that here. Anything unexpected maps to ``'255'`` (Undefined).
    """
    token = str(value).strip() if value is not None else ''
    return token if token in VALID_PUNCH_TYPES else UNDEFINED_PUNCH


# ZKTeco verify mode (pyzk Attendance.status / iclock ATTLOG column 4)
# -> Odoo auth_type. Mirrors controllers/iclock_api.py.
_VERIFY_AUTH_MAP = {
    '0': 'password',
    '1': 'fingerprint',
    '2': 'fingerprint',  # PIN
    '3': 'rfid',
    '4': 'rfid',
    '15': 'face',
}


def map_auth_type(verify_mode) -> str:
    """Map pyzk ``Attendance.status`` (verify mode) to Odoo ``auth_type``."""
    token = str(verify_mode).strip() if verify_mode is not None else '1'
    return _VERIFY_AUTH_MAP.get(token, 'fingerprint')


def map_verify_mode(verify_mode) -> str:
    """Map pyzk ``Attendance.status`` to Odoo ``verify_mode`` selection."""
    token = str(verify_mode).strip() if verify_mode is not None else '1'
    return token if token in {str(i) for i in range(11)} else '1'


def attendance_record_to_log(record, tz_name: Optional[str]) -> Dict:
    """Convert a pyzk ``Attendance`` row to our outbound log dict.

    pyzk field semantics (do NOT swap):
      - ``record.punch``  → attendance state (check-in/out, 0-5)
      - ``record.status`` → verification mode (fingerprint/card/face, …)
    """
    raw_punch = getattr(record, 'punch', None)
    raw_status = getattr(record, 'status', None)
    return {
        'pin': str(record.user_id),
        'timestamp': to_utc_iso(record.timestamp, tz_name),
        'punch': map_punch(raw_punch),
        'auth_type': map_auth_type(raw_status),
        'verify_mode': map_verify_mode(raw_status),
        'raw_punch': raw_punch,
        'raw_status': raw_status,
    }


def _get_zone(tz_name: Optional[str]):
    """Resolve a timezone name to a tzinfo object (zoneinfo or pytz)."""
    if not tz_name:
        return None
    if ZoneInfo is not None:
        try:
            return ZoneInfo(tz_name)
        except Exception:
            pass
    if pytz is not None:
        try:
            return pytz.timezone(tz_name)
        except Exception:
            pass
    return None


def to_utc_iso(naive_dt: datetime, tz_name: Optional[str]) -> str:
    """Convert a device-local datetime to a UTC ISO-8601 string.

    The result carries an explicit ``+00:00`` offset so Odoo stores the
    absolute instant as-is (no second localization). If ``naive_dt`` is already
    tz-aware it is just converted to UTC; if ``tz_name`` is unknown/unavailable
    the value is assumed to already be UTC (best effort, never crashes).
    """
    if naive_dt.tzinfo is not None:
        aware = naive_dt.astimezone(timezone.utc)
    else:
        zone = _get_zone(tz_name)
        if zone is None:
            aware = naive_dt.replace(tzinfo=timezone.utc)
        elif pytz is not None and hasattr(zone, 'localize'):
            # pytz requires localize() rather than replace(tzinfo=...)
            aware = zone.localize(naive_dt).astimezone(timezone.utc)
        else:
            aware = naive_dt.replace(tzinfo=zone).astimezone(timezone.utc)
    return aware.isoformat()


class OdooAPIClient:
    """Client for communicating with Odoo Biometric API"""
    
    def __init__(self, base_url: str, api_key: str, device_code: str = None,
                 timeout: int = 30, db: str = None):
        """
        Initialize Odoo API client
        
        Args:
            base_url: Odoo instance URL (e.g., https://your-company.odoo.com)
            api_key: Device API key from Odoo
            device_code: Device code for additional validation
            timeout: Request timeout in seconds
            db: Optional Odoo database name (required when the host serves more
                than one database, e.g. a shared domain without a dbfilter).
        """
        self.base_url = base_url.rstrip('/')
        self.api_key = api_key
        self.device_code = device_code
        self.timeout = timeout
        self.db = db or None
        self.session = requests.Session()
        self.session.headers.update({
            'X-API-Key': api_key,
            'Content-Type': 'application/json',
        })
    
    def _get_endpoint(self, path: str) -> str:
        """Build full API URL"""
        return f"{self.base_url}/biometric/api/{path}"

    def _params(self, extra: Dict = None) -> Dict:
        """Merge the optional ``db`` selector into request query params."""
        params = dict(extra or {})
        if self.db:
            params['db'] = self.db
        return params
    
    def test_connection(self) -> bool:
        """Test API connectivity and authentication"""
        try:
            response = self.session.get(
                self._get_endpoint('test'),
                params=self._params({'api_key': self.api_key}),
                timeout=self.timeout
            )
            if response.status_code == 200:
                data = response.json()
                if data.get('success'):
                    logger.info("API authentication successful: %s", data.get('message'))
                    return True
                else:
                    logger.error("API authentication failed: %s", data.get('message'))
                    return False
            else:
                logger.error("API test failed with status %s: %s", 
                           response.status_code, response.text)
                return False
        except requests.exceptions.ConnectionError:
            logger.error("Cannot connect to Odoo at %s", self.base_url)
            return False
        except Exception as e:
            logger.error("API test error: %s", str(e))
            return False
    
    def send_attendance(self, pin: str, timestamp: str, punch_type: str = '0', 
                       auth_type: str = 'fingerprint') -> Dict:
        """
        Send single attendance record to Odoo
        
        Args:
            pin: Employee biometric ID/PIN
            timestamp: Punch timestamp (ISO format or YYYY-MM-DD HH:MM:SS)
            punch_type: 0=Check In, 1=Check Out, etc.
            auth_type: Authentication method
            
        Returns:
            API response dict
        """
        params = {
            'api_key': self.api_key,
            'pin': pin,
            'timestamp': timestamp,
            'punch': punch_type,
            'auth_type': auth_type,
        }
        if self.device_code:
            params['device_code'] = self.device_code
        
        try:
            response = self.session.get(
                self._get_endpoint('attendance'),
                params=self._params(params),
                timeout=self.timeout
            )
            return response.json()
        except Exception as e:
            logger.error("Failed to send attendance: %s", str(e))
            return {'success': False, 'error': str(e)}
    
    def fetch_fleet(self) -> List[Dict]:
        """Discover all configured devices from Odoo (custom endpoint).

        Authenticates with the configured (master) device api_key and returns
        every active device with LAN connection details and its own api_key.
        """
        try:
            response = self.session.get(
                self._get_endpoint('devices'),
                params=self._params({'api_key': self.api_key}),
                timeout=self.timeout,
            )
            if response.status_code != 200:
                logger.error("Fleet discovery failed (HTTP %s): %s",
                             response.status_code, response.text[:300])
                return []
            raw = response.json()
            return (raw.get('data', {}) or {}).get('devices', [])
        except Exception as e:
            logger.error("Fleet discovery error: %s", str(e))
            return []

    def send_batch(self, logs: List[Dict], device_code: str = None,
                   api_key: str = None) -> Dict:
        """
        Send batch attendance records to Odoo via the custom endpoint.

        Args:
            logs: List of log dicts with keys: pin, timestamp, punch, auth_type
            device_code/api_key: optional per-device overrides (fleet mode)

        Returns:
            Normalized dict: {ok: bool, sent: int, failed: int, message: str}
        """
        key = api_key or self.api_key
        payload = {
            'api_key': key,
            'logs': logs,
        }
        code = device_code or self.device_code
        if code:
            payload['device_code'] = code

        try:
            response = self.session.post(
                self._get_endpoint('attendance'),
                json=payload,
                params=self._params(),
                headers={'X-API-Key': key},
                timeout=self.timeout
            )
            raw = response.json()
            data = raw.get('data', {}) if isinstance(raw, dict) else {}
            return {
                'ok': bool(raw.get('success')) if isinstance(raw, dict) else False,
                'sent': int(data.get('success', 0)),
                'failed': int(data.get('failed', 0)),
                'message': raw.get('message', '') if isinstance(raw, dict) else str(raw),
            }
        except Exception as e:
            logger.error("Failed to send batch: %s", str(e))
            return {'ok': False, 'sent': 0, 'failed': len(logs), 'message': str(e)}


class OdooJson2Client:
    """Client using the native Odoo 19 JSON-2 API (Mode 3, JSON-2 transport).

    Authenticates with a *user* API key as a bearer token and invokes the
    ``biometric.log.push_attendance`` model method:

        POST {base}/json/2/biometric.log/push_attendance
        Authorization: Bearer <USER_API_KEY>
        {"device_code": "...", "logs": [...]}

    Note: the user owning the API key must have HR Attendance create rights.
    """

    def __init__(self, base_url: str, api_key: str, device_code: str,
                 timeout: int = 30, db: str = None):
        self.base_url = base_url.rstrip('/')
        self.api_key = api_key
        self.device_code = device_code
        self.timeout = timeout
        self.db = db or None
        self.session = requests.Session()
        self.session.headers.update({
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
        })

    def _endpoint(self, model: str, method: str) -> str:
        url = f"{self.base_url}/json/2/{model}/{method}"
        if self.db:
            url += f"?db={self.db}"
        return url

    def test_connection(self) -> bool:
        # A cheap authenticated call: read the current user's name.
        try:
            response = self.session.post(
                self._endpoint('res.users', 'read'),
                json={'ids': [], 'fields': ['login']},
                timeout=self.timeout,
            )
            if response.status_code in (200, 422):
                # 200 = ok; 422 only means our args were off, but auth passed.
                logger.info("JSON-2 API reachable (status %s)", response.status_code)
                return response.status_code == 200 or 'bearer' not in response.text.lower()
            logger.error("JSON-2 auth failed (status %s): %s",
                         response.status_code, response.text[:300])
            return False
        except requests.exceptions.ConnectionError:
            logger.error("Cannot connect to Odoo at %s", self.base_url)
            return False
        except Exception as e:
            logger.error("JSON-2 test error: %s", str(e))
            return False

    def fetch_fleet(self) -> List[Dict]:
        """Discover all configured devices from Odoo (JSON-2 API)."""
        try:
            response = self.session.post(
                self._endpoint('biometric.device', 'get_sync_fleet'),
                json={},
                timeout=self.timeout,
            )
            if response.status_code != 200:
                logger.error("Fleet discovery failed (HTTP %s): %s",
                             response.status_code, response.text[:300])
                return []
            raw = response.json()
            return (raw or {}).get('devices', [])
        except Exception as e:
            logger.error("Fleet discovery error: %s", str(e))
            return []

    def send_batch(self, logs: List[Dict], device_code: str = None,
                   api_key: str = None) -> Dict:
        payload = {'device_code': device_code or self.device_code, 'logs': logs}
        try:
            response = self.session.post(
                self._endpoint('biometric.log', 'push_attendance'),
                json=payload,
                timeout=self.timeout,
            )
            if response.status_code != 200:
                return {'ok': False, 'sent': 0, 'failed': len(logs),
                        'message': f'HTTP {response.status_code}: {response.text[:300]}'}
            raw = response.json()
            return {
                'ok': bool(raw.get('success')),
                'sent': int(raw.get('created', 0)),
                'failed': int(raw.get('failed', 0)),
                'message': 'duplicates=%s' % raw.get('duplicates', 0),
            }
        except Exception as e:
            logger.error("Failed to send batch (JSON-2): %s", str(e))
            return {'ok': False, 'sent': 0, 'failed': len(logs), 'message': str(e)}


class BiosenseWebClient:
    """Pull attendance from a Chiyu BIOSENSE-T / WebPass-family device via HTTP.

    These devices do **not** speak the ZKTeco SDK (pyzk / port 4370). Port 2000
    on the Terminal Status screen is the SoMac *software* listen port (device
    pushes TO software). Our bridge instead polls the built-in web UI:

      http://<device_ip>/  → login (default admin/admin) → Access Log

    Access Log columns: User ID | Date | Time | IN/OUT
    IN  → punch '0' (Check In)
    OUT → punch '1' (Check Out)
    """

    # Candidate paths used by Chiyu firmware generations (2015+).
    _LOGIN_PATHS = ('/', '/index.htm', '/index.html', '/login.htm', '/Login.htm')
    _LOG_PATHS = (
        '/AccessLog.htm', '/accesslog.htm', '/AccLog.htm', '/acclog.htm',
        '/log.htm', '/Log.htm', '/AccessLog.html', '/cgi-bin/AccessLog',
    )
    _USER_LIST_PATHS = (
        '/Users.htm', '/users.htm', '/UserList.htm', '/userlist.htm',
        '/User.htm', '/user.htm',
    )
    # Chiyu firmware search/export form variants (AccLog / AccessLog pages).
    _LOG_SEARCH_POSTS = (
        {'Type': 'User', 'Sel': 'All', 'Search': 'Search'},
        {'Category': 'User', 'Selection': 'All', 'Search': 'Search'},
        {'type': 'user', 'sel': 'all', 'search': 'Search'},
        {'Export': 'TXT', 'Type': 'User', 'Selection': 'All'},
    )

    def __init__(self, host: str, port: int = 80, username: str = 'admin',
                 password: str = 'admin', timeout: int = 15,
                 tz_name: str = 'UTC'):
        self.host = host
        self.port = int(port or 80)
        self.username = username or 'admin'
        self.password = password or 'admin'
        self.timeout = timeout
        self.tz_name = tz_name or 'UTC'
        self.base_url = 'http://%s:%s' % (self.host, self.port)
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'BiometricBridge/1.0 (BIOSENSE)',
            'Accept': 'text/html,application/xhtml+xml,*/*',
        })
        self._logged_in = False
        # name (lower) → User ID, filled from the device Users page when needed.
        self._users_by_name: Dict[str, str] = {}

    def connect(self) -> bool:
        """Reach the device web UI and authenticate."""
        try:
            # Probe reachability first. Chiyu firmwares answer a bare GET / with
            # either 200 (no auth / form login) or 401 + WWW-Authenticate.
            probe = self.session.get(self.base_url + '/', timeout=self.timeout)
            challenge = probe.headers.get('WWW-Authenticate', '')
            logger.info(
                "BIOSENSE web UI reachable at %s (HTTP %s%s)",
                self.base_url, probe.status_code,
                ', auth: %s' % challenge if challenge else '',
            )
        except Exception as e:
            logger.error(
                "Cannot reach BIOSENSE web UI at %s: %s "
                "(check LAN, ping, and that Web Management Port is open)",
                self.base_url, e,
            )
            return False

        if self._try_login(probe):
            self._logged_in = True
            return True

        logger.error(
            "BIOSENSE login failed at %s with user '%s'. "
            "Check web_username / web_password on the device record. "
            "Chiyu defaults: admin/admin (administrator), user/user (operator), "
            "user0/user0 (user). Run with --probe-biosense %s to dump the raw "
            "HTTP handshake for diagnosis.",
            self.base_url, self.username, self.host,
        )
        return False

    def disconnect(self):
        self._logged_in = False
        try:
            self.session.close()
        except Exception:
            pass

    def _try_login(self, probe=None) -> bool:
        """Authenticate: HTTP Basic/Digest first, then HTML form variants.

        Chiyu built-in HTTP servers normally protect the UI with HTTP Basic
        auth (the browser's native popup), so that is tried before any form.
        """
        if self._try_http_auth(probe):
            return True
        if self._try_form_login():
            return True

        # Some older firmwares expose the log pages without authentication.
        for path in self._LOG_PATHS:
            try:
                resp = self.session.get(self.base_url + path, timeout=self.timeout)
                if resp.status_code == 200 and self._looks_like_access_log(resp.text):
                    logger.info(
                        "BIOSENSE Access Log reachable without login (%s)", path,
                    )
                    return True
            except Exception:
                continue
        return False

    def _try_http_auth(self, probe=None) -> bool:
        """Try HTTP Basic then Digest credentials against the device."""
        from requests.auth import HTTPBasicAuth, HTTPDigestAuth

        challenge = ''
        if probe is not None:
            challenge = (probe.headers.get('WWW-Authenticate') or '').lower()

        schemes = []
        if 'digest' in challenge:
            schemes = [('digest', HTTPDigestAuth), ('basic', HTTPBasicAuth)]
        else:
            schemes = [('basic', HTTPBasicAuth), ('digest', HTTPDigestAuth)]

        for scheme_name, auth_cls in schemes:
            auth = auth_cls(self.username, self.password)
            for path in ('/',) + self._LOG_PATHS:
                try:
                    resp = self.session.get(
                        self.base_url + path, auth=auth, timeout=self.timeout,
                    )
                except Exception as e:
                    logger.debug("BIOSENSE %s auth on %s failed: %s",
                                 scheme_name, path, e)
                    continue
                if resp.status_code == 401:
                    continue
                if resp.status_code == 200 and self._looks_authenticated(resp.text):
                    # Keep the credentials on the session for later requests.
                    self.session.auth = auth
                    logger.info("BIOSENSE login OK via HTTP %s auth (%s)",
                                scheme_name, path)
                    return True
        return False

    def _try_form_login(self) -> bool:
        """POST credentials to known Chiyu HTML login forms."""
        form_variants = [
            {'Username': self.username, 'Password': self.password},
            {'username': self.username, 'password': self.password},
            {'UserName': self.username, 'PassWord': self.password},
            {'userid': self.username, 'userpwd': self.password},
            {'user': self.username, 'pwd': self.password},
            {'login_user': self.username, 'login_pwd': self.password},
            {'ID': self.username, 'PWD': self.password},
        ]
        for path in self._LOGIN_PATHS:
            url = self.base_url + path
            for fields in form_variants:
                try:
                    resp = self.session.post(url, data=fields, timeout=self.timeout,
                                             allow_redirects=True)
                    if resp.status_code == 200 and self._looks_authenticated(resp.text):
                        logger.info("BIOSENSE login OK via POST %s fields=%s",
                                    path, list(fields.keys()))
                        return True
                    resp = self.session.get(url, params=fields, timeout=self.timeout)
                    if resp.status_code == 200 and self._looks_authenticated(resp.text):
                        logger.info("BIOSENSE login OK via GET %s", path)
                        return True
                except Exception as e:
                    logger.debug("BIOSENSE form login %s failed: %s", path, e)
        return False

    # Text that only appears when the device rejected the credentials.
    _REJECTION_MARKERS = (
        'unauthorized', 'password error', 'login failed', 'invalid password',
        'wrong password', 'authentication failed', 'access denied',
    )
    # Text that only appears once we are inside the management UI.
    _AUTH_MARKERS = (
        'access log', 'terminal status', 'main functions', 'first in last out',
        'log out', 'logout', 'remote control', 'auto-refresh log',
        'user administration', 'system log', 'add new user',
    )

    @classmethod
    def _looks_authenticated(cls, html: str) -> bool:
        """True when the response looks like an authenticated page.

        Chiyu web UIs are frameset-based: the top-level document is just a
        ``<frameset>`` shell whose menu/content live in child frames, so it
        contains none of the usual markers. Treat a frameset (or any page that
        is clearly not a login form) as authenticated — the caller confirms by
        actually fetching the Access Log.
        """
        if not html:
            return False
        low = html.lower()
        if any(bad in low for bad in cls._REJECTION_MARKERS):
            return False
        if any(marker in low for marker in cls._AUTH_MARKERS):
            return True
        # Frameset shell: real content is in the child frames.
        if '<frameset' in low or '<frame ' in low or '<iframe' in low:
            return True
        # A password field means we are still looking at the login form.
        if 'type="password"' in low or "type='password'" in low:
            return False
        return False

    def _frame_sources(self, html: str) -> List[str]:
        """Return frame/iframe sources referenced by a frameset shell."""
        if not html:
            return []
        return re.findall(
            r'<(?:i?frame)[^>]+src=["\']([^"\']+)["\']',
            html, flags=re.IGNORECASE,
        )

    def _absolute(self, href: str) -> str:
        if href.startswith('http'):
            return href
        if href.startswith('/'):
            return self.base_url + href
        return self.base_url + '/' + href

    @staticmethod
    def _looks_like_access_log(html: str) -> bool:
        if not html:
            return False
        low = html.lower()
        return (
            ('user id' in low or 'userid' in low or 'user_id' in low)
            and ('in/out' in low or '>in<' in low or '>out<' in low
                 or 'in out' in low)
        ) or ('access log' in low and '<table' in low)

    def get_attendance_logs(self, since: Optional[datetime] = None) -> List[Dict]:
        """Fetch Access Log rows and map them to our outbound log dicts."""
        self._load_user_directory()

        html = self._fetch_access_log_html()
        if not html:
            logger.error("BIOSENSE: could not retrieve Access Log HTML")
            return []

        rows = self._parse_access_log_html(html)
        if not rows:
            txt = self._fetch_access_log_txt()
            if txt:
                rows = self._parse_access_log_txt(txt)

        if not rows and html:
            self._log_unparsed_sample(html, source='HTML')

        logs = []
        skipped = 0
        for row in rows:
            ts = row.get('timestamp')
            pin = row.get('pin')
            if not pin or not ts:
                skipped += 1
                continue
            if since and ts < since:
                skipped += 1
                continue
            direction = (row.get('direction') or 'IN').upper()
            punch = '0' if direction.startswith('IN') else '1'
            auth = 'fingerprint'
            note = (row.get('note') or '').lower()
            if 'card' in note or 'rfid' in note or 'wiegand' in note:
                auth = 'rfid'
            elif 'password' in note or 'pin' in note:
                auth = 'password'
            logs.append({
                'pin': str(pin).strip(),
                'timestamp': to_utc_iso(ts, self.tz_name),
                'punch': punch,
                'auth_type': auth,
                'verify_mode': '1' if auth == 'fingerprint' else '3',
                'raw_punch': punch,
                'raw_status': direction,
            })

        logger.info(
            "BIOSENSE retrieved %s logs (%s skipped/filtered) from %s",
            len(logs), skipped, self.host,
        )
        return logs

    def _load_user_directory(self) -> None:
        """Build User Name → User ID map from the device Users page."""
        if self._users_by_name:
            return
        for path in self._USER_LIST_PATHS:
            try:
                resp = self.session.get(self.base_url + path, timeout=self.timeout)
                if resp.status_code != 200 or not resp.text:
                    continue
                cols: Optional[Dict[str, int]] = None
                for block in re.findall(
                    r'<tr[^>]*>(.*?)</tr>', resp.text,
                    flags=re.IGNORECASE | re.DOTALL,
                ):
                    cells = self._cells_from_tr_block(block)
                    texts = [c['text'] for c in cells]
                    if len(texts) < 2:
                        continue
                    header = self._header_map(texts)
                    if header:
                        cols = header
                        continue
                    if not cols:
                        continue
                    uid = self._pin_from_cells(cells, cols, roles=('user_id', 'card', 'employee_id'))
                    name = self._cell_text(cells, cols, 'name')
                    if uid and name and not self._is_placeholder(name):
                        self._users_by_name[name.strip().lower()] = uid
                if self._users_by_name:
                    logger.info(
                        "BIOSENSE loaded %s user name(s) from %s",
                        len(self._users_by_name), path,
                    )
                    return
            except Exception as e:
                logger.debug("BIOSENSE user list %s failed: %s", path, e)

    def _log_unparsed_sample(self, content: str, source: str = 'HTML') -> None:
        """Emit raw table rows when parsing produced nothing — aids field tuning."""
        if source.upper() == 'HTML':
            blocks = re.findall(
                r'<tr[^>]*>(.*?)</tr>', content, flags=re.IGNORECASE | re.DOTALL,
            )
            samples = []
            for block in blocks[:15]:
                cells = self._cells_from_tr_block(block)
                texts = [c['text'] for c in cells]
                if len(texts) >= 3:
                    samples.append(texts)
            if samples:
                logger.warning(
                    "BIOSENSE parsed 0 rows from %s — sample table rows: %s",
                    source, samples[:5],
                )
        else:
            lines = [ln.strip() for ln in content.splitlines() if ln.strip()][:8]
            if lines:
                logger.warning(
                    "BIOSENSE parsed 0 rows from %s — sample lines: %s",
                    source, lines,
                )

    def _fetch_access_log_html(self) -> Optional[str]:
        for path in self._LOG_PATHS:
            try:
                resp = self.session.get(self.base_url + path, timeout=self.timeout)
                if resp.status_code == 200 and (
                    self._looks_like_access_log(resp.text)
                    or '<table' in (resp.text or '').lower()
                ):
                    rows = self._parse_access_log_html(resp.text or '')
                    if rows:
                        logger.info("BIOSENSE Access Log page: %s (%s rows)", path, len(rows))
                        return resp.text
                    logger.debug("BIOSENSE %s returned a table but 0 parsed rows", path)
            except Exception as e:
                logger.debug("BIOSENSE log path %s failed: %s", path, e)

        # Some firmwares only populate the log table after a Search POST.
        for path in self._LOG_PATHS:
            for fields in self._LOG_SEARCH_POSTS:
                try:
                    resp = self.session.post(
                        self.base_url + path, data=fields, timeout=self.timeout,
                    )
                    if resp.status_code != 200 or not resp.text:
                        continue
                    rows = self._parse_access_log_html(resp.text)
                    if rows:
                        logger.info(
                            "BIOSENSE Access Log via POST %s fields=%s (%s rows)",
                            path, sorted(fields.keys()), len(rows),
                        )
                        return resp.text
                except Exception as e:
                    logger.debug("BIOSENSE POST %s failed: %s", path, e)

        # Return the first HTML page even when empty — TXT export / diagnostics.
        for path in self._LOG_PATHS:
            try:
                resp = self.session.get(self.base_url + path, timeout=self.timeout)
                if resp.status_code == 200 and (
                    self._looks_like_access_log(resp.text)
                    or '<table' in (resp.text or '').lower()
                ):
                    logger.info("BIOSENSE Access Log page: %s", path)
                    return resp.text
            except Exception:
                continue

        # Crawl the UI: the home page is usually a frameset shell, so follow
        # frames first, then any Access-Log-looking link inside them.
        try:
            home = self.session.get(self.base_url + '/', timeout=self.timeout)
            pages = [home.text or '']
            for src in self._frame_sources(home.text or ''):
                try:
                    frame = self.session.get(self._absolute(src), timeout=self.timeout)
                    logger.debug("BIOSENSE followed frame %s (HTTP %s)",
                                 src, frame.status_code)
                    if frame.status_code == 200:
                        if self._looks_like_access_log(frame.text):
                            logger.info("BIOSENSE Access Log inside frame: %s", src)
                            return frame.text
                        pages.append(frame.text or '')
                except Exception as e:
                    logger.debug("BIOSENSE frame %s failed: %s", src, e)

            seen = set()
            for page in pages:
                hrefs = re.findall(
                    r'(?:href|action)=["\']([^"\']*(?:access|log|acc)[^"\']*)["\']',
                    page, flags=re.IGNORECASE,
                )
                for href in hrefs:
                    if href.lower().startswith(('javascript:', 'mailto:', '#')):
                        continue
                    url = self._absolute(href)
                    if url in seen:
                        continue
                    seen.add(url)
                    try:
                        resp = self.session.get(url, timeout=self.timeout)
                    except Exception:
                        continue
                    if resp.status_code == 200 and self._looks_like_access_log(resp.text):
                        logger.info("BIOSENSE Access Log via menu link: %s", href)
                        return resp.text
        except Exception as e:
            logger.debug("BIOSENSE menu crawl failed: %s", e)
        return None

    def _fetch_access_log_txt(self) -> Optional[str]:
        """Try common Export-TXT endpoints used by Chiyu firmwares."""
        candidates = [
            ('/AccessLog.htm', {'Export': 'TXT', 'Type': 'User', 'Selection': 'All'}),
            ('/AccessLog.htm', {'export': 'txt', 'type': 'user', 'selection': 'all'}),
            ('/AccLog.htm', {'Export': 'TXT'}),
            ('/cgi-bin/AccessLog', {'format': 'txt'}),
        ]
        for path, data in candidates:
            try:
                resp = self.session.post(
                    self.base_url + path, data=data, timeout=self.timeout,
                )
                text = resp.text or ''
                if resp.status_code == 200 and text and (
                    '\t' in text or 'User ID' in text or 'IN' in text
                ):
                    logger.info("BIOSENSE Access Log TXT export via %s", path)
                    return text
            except Exception:
                continue
        return None

    # Cells the device uses to mean "no value here".
    _PLACEHOLDERS = ('', '-', '--', '---', '----', '-----', 'n/a', 'na', 'null',
                     'none', '0000000000', 'xxxx')

    # Header keywords → logical column, ordered by preference within a role.
    _HEADER_ROLES = (
        ('user_id', ('user id', 'userid', 'user_id', 'user no', 'id no',
                     'staff', 'enroll')),
        ('employee_id', ('employee id', 'employee no', 'emp no', 'empid')),
        ('card', ('card no', 'card number', 'cardno', 'card', 'badge')),
        ('name', ('user name', 'username', 'name')),
        ('date', ('date',)),
        ('time', ('time',)),
        ('direction', ('in/out', 'in out', 'inout', 'direction', 'status')),
        ('note', ('note', 'remark', 'mode', 'verify', 'method', 'door', 'reader')),
    )
    _HREF_ID_PATTERNS = (
        r'(?:UserID|userid|UserId|EnrollID|enroll)=["\']?(\d+)',
        r'(?:\?|&)(?:ID|Id|id)=["\']?(\d+)',
        r'UserRecord[^"\']*(?:ID|id)=["\']?(\d+)',
        r'ModifyUser[^"\']*(?:ID|id)=["\']?(\d+)',
    )

    @classmethod
    def _is_placeholder(cls, value: Optional[str]) -> bool:
        return (value or '').strip().lower() in cls._PLACEHOLDERS

    @classmethod
    def _header_map(cls, texts: List[str]) -> Optional[Dict[str, int]]:
        """Map column index → role when `texts` looks like a header row."""
        mapping: Dict[str, int] = {}
        for idx, cell in enumerate(texts):
            low = cell.strip().lower()
            if not low:
                continue
            for role, keywords in cls._HEADER_ROLES:
                if role in mapping:
                    continue
                if any(low == kw or kw in low for kw in keywords):
                    mapping[role] = idx
                    break
        # A real header identifies at least a time-ish column plus an identity
        # column; otherwise it is a data row that happens to contain words.
        has_identity = (
            'user_id' in mapping or 'card' in mapping or 'employee_id' in mapping
        )
        has_time = 'date' in mapping or 'time' in mapping
        return mapping if has_identity and has_time else None

    @classmethod
    def _href_pin(cls, cell_html: str) -> Optional[str]:
        for pat in cls._HREF_ID_PATTERNS:
            m = re.search(pat, cell_html or '', flags=re.IGNORECASE)
            if m:
                return m.group(1)
        return None

    @classmethod
    def _cells_from_tr_block(cls, block: str) -> List[Dict]:
        """Parse one table row into [{text, href_pin}, …]."""
        cells = re.findall(
            r'<t[dh][^>]*>(.*?)</t[dh]>', block,
            flags=re.IGNORECASE | re.DOTALL,
        )
        out = []
        for raw in cells:
            text = cls._strip_html(raw)
            out.append({
                'text': text.strip() if text is not None else '',
                'href_pin': cls._href_pin(raw),
            })
        return out

    @classmethod
    def _normalize_pin_value(cls, value: str) -> Optional[str]:
        if not value or cls._is_placeholder(value):
            return None
        m = re.match(r'^(\d+)\s*\(\d+\)$', value.strip())  # 176(1)
        if m:
            return m.group(1)
        m = re.match(r'^(\d{2,})$', value.strip())
        if m:
            return m.group(1)
        if re.match(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,15}$', value.strip()):
            return value.strip()
        return None

    @classmethod
    def _cell_text(cls, cells: List[Dict], cols: Dict[str, int], role: str) -> str:
        idx = cols.get(role)
        if idx is None or idx >= len(cells):
            return ''
        return cells[idx]['text']

    def _pin_from_cells(self, cells: List[Dict], cols: Dict[str, int],
                        roles: tuple = ('user_id', 'card', 'employee_id', 'name')) -> Optional[str]:
        """Resolve the best biometric pin from mapped columns."""
        for role in roles:
            idx = cols.get(role)
            if idx is None or idx >= len(cells):
                continue
            cell = cells[idx]
            if cell.get('href_pin'):
                return cell['href_pin']
            pin = self._normalize_pin_value(cell.get('text') or '')
            if pin:
                return pin

        name = self._cell_text(cells, cols, 'name')
        if name and not self._is_placeholder(name):
            mapped = self._users_by_name.get(name.strip().lower())
            if mapped:
                return mapped
            # Last resort: send the display name so Odoo can map by name.
            return name.strip()
        return None

    def _row_from_header(self, cells: List[Dict], cols: Dict[str, int]) -> Optional[Dict]:
        """Build a log row using column positions discovered in the header."""
        date_str = self._cell_text(cells, cols, 'date')
        time_str = self._cell_text(cells, cols, 'time')
        if date_str and not time_str:
            m = re.match(r'^(\S+)\s+(\d{1,2}:\d{2}(?::\d{2})?)$', date_str)
            if m:
                date_str, time_str = m.group(1), m.group(2)
        ts = self._parse_device_datetime(date_str, time_str) if date_str and time_str else None
        if not ts:
            return None

        pin = self._pin_from_cells(cells, cols)
        if not pin:
            logger.warning(
                "BIOSENSE: skipping log row with no usable user id: %s",
                [c['text'] for c in cells],
            )
            return None

        raw_dir = self._cell_text(cells, cols, 'direction').upper()
        direction = 'OUT' if raw_dir.startswith('OUT') else 'IN'

        note = self._cell_text(cells, cols, 'note')
        name = self._cell_text(cells, cols, 'name')
        if not note and name and name != pin:
            note = name

        return {
            'pin': pin,
            'timestamp': ts,
            'direction': direction,
            'note': note,
        }

    def _parse_access_log_html(self, html: str) -> List[Dict]:
        """Extract rows from HTML tables (User ID / Date / Time / IN|OUT)."""
        rows: List[Dict] = []
        cols: Optional[Dict[str, int]] = None
        tr_blocks = re.findall(
            r'<tr[^>]*>(.*?)</tr>', html, flags=re.IGNORECASE | re.DOTALL,
        )
        for block in tr_blocks:
            cells = self._cells_from_tr_block(block)
            texts = [c['text'] for c in cells]
            if len(texts) < 3:
                continue

            header = self._header_map(texts)
            if header:
                cols = header
                logger.debug("BIOSENSE Access Log columns: %s", cols)
                continue

            parsed = (self._row_from_header(cells, cols) if cols
                      else self._row_from_cells(texts))
            if parsed:
                rows.append(parsed)
        return rows

    def _parse_access_log_txt(self, text: str) -> List[Dict]:
        rows: List[Dict] = []
        cols: Optional[Dict[str, int]] = None
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = re.split(r'\t+', line)
            if len(parts) < 4:
                parts = re.split(r'[,;|]+', line)
            parts = [p.strip() for p in parts if p.strip()]
            if len(parts) < 4:
                continue
            header = self._header_map(parts)
            if header:
                cols = header
                continue
            if cols:
                pseudo = [{'text': p, 'href_pin': None} for p in parts]
                parsed = self._row_from_header(pseudo, cols)
            else:
                parsed = self._row_from_cells(parts)
            if parsed:
                rows.append(parsed)
        if not rows:
            self._log_unparsed_sample(text, source='TXT')
        return rows

    def _row_from_cells(self, texts: List[str]) -> Optional[Dict]:
        """Heuristically map a list of cell strings to pin/timestamp/direction."""
        direction = None
        for t in texts:
            up = t.upper().strip()
            if up in ('IN', 'OUT') or up.startswith('IN ') or up.startswith('OUT '):
                direction = 'IN' if up.startswith('IN') else 'OUT'
                break
        if not direction:
            return None

        # Date + time: look for recognizable patterns.
        date_str = None
        time_str = None
        for t in texts:
            if re.match(r'^\d{1,4}[/\-.]\d{1,2}[/\-.]\d{1,4}$', t):
                date_str = t
            elif re.match(r'^\d{1,2}:\d{2}(:\d{2})?$', t):
                time_str = t
        if not date_str or not time_str:
            # Combined datetime in one cell.
            for t in texts:
                m = re.match(
                    r'^(\d{1,4}[/\-.]\d{1,2}[/\-.]\d{1,4})\s+(\d{1,2}:\d{2}(?::\d{2})?)$',
                    t,
                )
                if m:
                    date_str, time_str = m.group(1), m.group(2)
                    break
        if not date_str or not time_str:
            return None

        ts = self._parse_device_datetime(date_str, time_str)
        if not ts:
            return None

        # PIN / User ID: prefer "176(1)" style (id + level), then other IDs.
        # Never take the leading serial "No." column alone when a better ID exists.
        pin = None
        candidates = []
        for idx, t in enumerate(texts):
            if t.upper().startswith(('IN', 'OUT')):
                continue
            if t == date_str or t == time_str:
                continue
            if self._is_placeholder(t):
                continue
            if re.match(r'^\d{1,4}[/\-.]\d{1,2}[/\-.]\d{1,4}$', t):
                continue
            if re.match(r'^\d{1,2}:\d{2}', t):
                continue
            m = re.match(r'^(\d+)\s*\(\d+\)$', t)  # 176(1)
            if m:
                candidates.append((0, m.group(1)))  # highest priority
                continue
            m = re.match(r'^(\d{2,})$', t)  # prefer multi-digit IDs over serial "1"
            if m:
                candidates.append((1, m.group(1)))
                continue
            # Alphanumeric IDs such as "A123". Bare single digits are excluded
            # on purpose: they are the row's serial "No.", not a user id.
            if (re.match(r'^[A-Za-z0-9_-]{1,16}$', t) and not t.isalpha()
                    and re.search(r'[A-Za-z]', t) and re.search(r'\d', t)):
                candidates.append((2, t))
        if candidates:
            candidates.sort(key=lambda x: x[0])
            pin = candidates[0][1]
        if not pin:
            # Heuristic path: try User Name / free-text identity columns.
            for t in texts:
                if self._is_placeholder(t):
                    continue
                if t.upper().startswith(('IN', 'OUT')):
                    continue
                if t == date_str or t == time_str:
                    continue
                if re.match(r'^\d{1,4}[/\-.]\d{1,2}[/\-.]\d{1,4}$', t):
                    continue
                if re.match(r'^\d{1,2}:\d{2}', t):
                    continue
                if re.match(r'^\d+$', t):
                    continue
                mapped = self._users_by_name.get(t.strip().lower())
                if mapped:
                    pin = mapped
                    break
                if len(t.strip()) >= 2 and not t.strip().lower().startswith(
                    ('finger', 'card', 'pass', 'accepted', 'reject'),
                ):
                    pin = t.strip()
                    break
        if not pin:
            return None

        note_parts = []
        for t in texts:
            if t in (pin, date_str, time_str):
                continue
            if t.upper().startswith(('IN', 'OUT')):
                continue
            if re.match(r'^\d+$', t):  # serial No. / door no.
                continue
            if re.match(r'^\d+\s*\(\d+\)$', t):  # user id with level
                continue
            note_parts.append(t)
        note = ' '.join(note_parts)

        return {
            'pin': pin,
            'timestamp': ts,
            'direction': direction,
            'note': note,
        }

    @staticmethod
    def _parse_device_datetime(date_str: str, time_str: str) -> Optional[datetime]:
        if len(time_str.split(':')) == 2:
            time_str = time_str + ':00'
        combos = [
            '%m/%d/%Y %H:%M:%S',
            '%d/%m/%Y %H:%M:%S',
            '%Y/%m/%d %H:%M:%S',
            '%Y-%m-%d %H:%M:%S',
            '%m-%d-%Y %H:%M:%S',
            '%d-%m-%Y %H:%M:%S',
            '%m/%d/%y %H:%M:%S',
            '%d/%m/%y %H:%M:%S',
        ]
        raw = '%s %s' % (date_str, time_str)
        for fmt in combos:
            try:
                return datetime.strptime(raw, fmt)
            except ValueError:
                continue
        return None

    @staticmethod
    def _strip_html(fragment: str) -> str:
        text = re.sub(r'<[^>]+>', ' ', fragment or '')
        text = re.sub(r'&nbsp;|&#160;', ' ', text, flags=re.IGNORECASE)
        text = re.sub(r'&amp;', '&', text, flags=re.IGNORECASE)
        text = re.sub(r'\s+', ' ', text)
        return text.strip()


class BiometricDevice:
    """Wrapper for biometric device communication"""
    
    def __init__(self, host: str, port: int = 4370, timeout: int = 5, 
                 device_type: str = 'auto', password: int = 0,
                 tz_name: str = 'UTC'):
        """
        Initialize device connection
        
        Args:
            host: Device IP address
            port: Device port (default 4370 for ZKTeco)
            timeout: Connection timeout
            device_type: 'f18', 'biosense_t', or 'auto'
            password: Device password if configured
            tz_name: Device clock timezone (device_tz from Odoo), used to
                     convert naive local punch timestamps to UTC.
        """
        self.host = host
        self.port = port
        self.timeout = timeout
        self.device_type = device_type
        self.password = password
        self.tz_name = tz_name or 'UTC'
        self.conn = None
        self.zk = None
    
    def connect(self) -> bool:
        """Connect to the biometric device."""
        if not PYZK_AVAILABLE:
            logger.error("pyzk library not available. Cannot connect to device.")
            return False

        # BIOSENSE-T screens often show port 2000 as the ADMS *server* listen
        # port — pyzk polls the ZK SDK port (4370 by default). Try both.
        ports_to_try = [self.port]
        if self.port != 4370:
            ports_to_try.append(4370)

        last_error = None
        for port in ports_to_try:
            try:
                self.zk = ZK(
                    self.host,
                    port=port,
                    timeout=self.timeout,
                    password=self.password,
                    force_udp=False,
                    ommit_ping=False,
                )
                self.conn = self.zk.connect()
                if port != self.port:
                    logger.warning(
                        "Connected to %s on port %s (configured port %s failed). "
                        "Update device_port in Odoo if %s is the permanent port.",
                        self.host, port, self.port, port,
                    )
                self.port = port
                logger.info("Connected to device at %s:%s", self.host, self.port)

                device_info = self.get_device_info()
                logger.info("Device: %s", json.dumps(device_info, indent=2, default=str))
                return True
            except Exception as e:
                last_error = e
                if port != ports_to_try[-1]:
                    logger.warning(
                        "Connection attempt %s:%s failed (%s); trying next port...",
                        self.host, port, e,
                    )
                else:
                    logger.error(
                        "Failed to connect to %s:%s - %s",
                        self.host, port, e,
                    )

        if self.device_type == 'biosense_t':
            logger.error(
                "Device type biosense_t must use BiosenseWebClient (HTTP), "
                "not pyzk. Check sync_device routing."
            )
        return False
    
    def disconnect(self):
        """Disconnect from device"""
        if self.conn:
            try:
                self.conn.disconnect()
                logger.info("Disconnected from device")
            except Exception as e:
                logger.warning("Error disconnecting: %s", str(e))
    
    def get_device_info(self) -> Dict:
        """Get device information"""
        if not self.conn:
            return {}
        try:
            return {
                ' firmware_version': self.conn.get_firmware_version(),
                'serial_number': self.conn.get_serialnumber(),
                'device_name': self.conn.get_device_name(),
                'platform': self.conn.get_platform(),
                'users': self.conn.get_users(),
            }
        except Exception as e:
            logger.warning("Could not get device info: %s", str(e))
            return {}
    
    def get_attendance_logs(self, since: Optional[datetime] = None) -> List[Dict]:
        """
        Retrieve attendance logs from device
        
        Args:
            since: Only get logs after this datetime
            
        Returns:
            List of log dictionaries
        """
        if not self.conn:
            logger.error("Not connected to device")
            return []
        
        try:
            logger.info("Fetching attendance logs from device...")
            attendance = self.conn.get_attendance()
            
            logs = []
            skipped = 0
            
            for record in attendance:
                # Skip records before 'since' date (compared in device-local time)
                if since and record.timestamp < since:
                    skipped += 1
                    continue

                entry = attendance_record_to_log(record, self.tz_name)
                if logger.isEnabledFor(logging.DEBUG) and len(logs) < 5:
                    logger.debug(
                        "Raw attendance: pin=%s raw_punch=%s raw_status=%s -> punch=%s auth=%s",
                        entry['pin'], entry['raw_punch'], entry['raw_status'],
                        entry['punch'], entry['auth_type'],
                    )
                logs.append(entry)
            
            logger.info("Retrieved %s logs (%s skipped)", len(logs), skipped)
            return logs
            
        except Exception as e:
            logger.error("Failed to get attendance: %s", str(e))
            return []
    
    def clear_attendance(self) -> bool:
        """Clear attendance records from device (use with caution!)"""
        if not self.conn:
            return False
        try:
            self.conn.clear_attendance()
            logger.info("Attendance records cleared from device")
            return True
        except Exception as e:
            logger.error("Failed to clear attendance: %s", str(e))
            return False


def apply_logging(log_conf: Dict, verbose: bool = False):
    """Apply the configured log level and (optionally) a rotating-free file sink.

    Console output is always available (stdout / journald under systemd). When
    ``[logging] file`` is set, a file handler is added so the bridge also keeps
    a persistent log on disk for later inspection.
    """
    log_conf = log_conf or {}
    level_name = str(log_conf.get('level', 'INFO')).upper()
    level = logging.DEBUG if verbose else getattr(logging, level_name, logging.INFO)
    logger.setLevel(level)

    log_file = (log_conf.get('file') or '').strip()
    if not log_file:
        return
    target = os.path.abspath(log_file)
    # Avoid stacking duplicate handlers on the same file (e.g. daemon restarts).
    for h in logger.handlers:
        if isinstance(h, logging.FileHandler) and getattr(h, 'baseFilename', None) == target:
            return
    try:
        fh = logging.FileHandler(target)
        fh.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
        logger.addHandler(fh)
        logger.info("Logging to file: %s", target)
    except OSError as e:
        logger.warning("Could not open log file %s: %s", target, e)


def load_config(config_path: str) -> Dict:
    """Load configuration from INI file"""
    config = configparser.ConfigParser()
    config.read(config_path)
    
    return {
        'odoo': {
            'url': config.get('odoo', 'url', fallback=''),
            'db': config.get('odoo', 'db', fallback=''),
            'api_key': config.get('odoo', 'api_key', fallback=''),
            'device_code': config.get('odoo', 'device_code', fallback=None),
            'timeout': config.getint('odoo', 'timeout', fallback=30),
            # 'custom' -> /biometric/api/attendance (device api_key)
            # 'json2'  -> Odoo 19 /json/2 API (user api_key as bearer)
            'transport': config.get('odoo', 'transport', fallback='custom'),
            # When true, the bridge pulls the device list (IP/port/code) from
            # Odoo and syncs them all - no [device] section needed.
            'auto_discover': config.getboolean('odoo', 'auto_discover', fallback=True),
        },
        'device': {
            'host': config.get('device', 'host', fallback=''),
            'port': config.getint('device', 'port', fallback=4370),
            'password': config.getint('device', 'password', fallback=0),
            'type': config.get('device', 'type', fallback='auto'),
        },
        'sync': {
            'interval': config.getint('sync', 'interval_minutes', fallback=5),
            'batch_size': config.getint('sync', 'batch_size', fallback=100),
            'clear_after_sync': config.getboolean('sync', 'clear_after_sync', fallback=False),
            'since_hours': config.getint('sync', 'since_hours', fallback=24),
            'retry_attempts': config.getint('sync', 'retry_attempts', fallback=3),
            'retry_delay': config.getint('sync', 'retry_delay_seconds', fallback=10),
        },
        'logging': {
            'level': config.get('logging', 'level', fallback='INFO'),
            'file': config.get('logging', 'file', fallback=''),
        }
    }


def create_sample_config(path: str):
    """Create a sample configuration file"""
    config = configparser.ConfigParser()
    
    config['odoo'] = {
        '# Odoo instance URL': '',
        'url': 'https://your-company.odoo.com',
        '# Database name. Required only if the host serves multiple databases': '',
        '# (no dbfilter). Leave blank for a single-DB / Odoo.sh instance.': '',
        'db': '',
        '# Transport: custom (device API key) or json2 (Odoo 19 JSON-2, user API key)': '',
        'transport': 'json2',
        '# For transport=custom: any active device API Key (Device form in Odoo).': '',
        '# For transport=json2: a USER API key (Preferences > Account Security).': '',
        'api_key': 'YOUR_API_KEY_HERE',
        '# auto_discover=true: pull the device list (IP/port/code) from Odoo and': '',
        '# sync every configured device. Leave the [device] section blank.': '',
        'auto_discover': 'true',
        '# device_code: only used when auto_discover=false (single-device mode)': '',
        'device_code': 'DEV0001',
        '# Request timeout in seconds': '',
        'timeout': '30',
    }

    config['device'] = {
        '# Only used when auto_discover=false. With auto_discover=true these': '',
        '# values come from Odoo automatically and this section is ignored.': '',
        'host': '',
        '# Device port (default 4370 for ZKTeco)': '',
        'port': '4370',
        '# Device password (0 if no password)': '',
        'password': '0',
        '# Device type: f18, biosense_t, or auto': '',
        'type': 'auto',
    }
    
    config['sync'] = {
        '# Sync interval in minutes': '',
        'interval_minutes': '5',
        '# Batch size for sending logs': '',
        'batch_size': '100',
        '# Clear device logs after successful sync (USE WITH CAUTION)': '',
        'clear_after_sync': 'false',
        '# Only fetch logs from last N hours': '',
        'since_hours': '24',
        '# Retry attempts on failure': '',
        'retry_attempts': '3',
        '# Delay between retries in seconds': '',
        'retry_delay_seconds': '10',
    }
    
    config['logging'] = {
        '# Log level: DEBUG, INFO, WARNING, ERROR': '',
        'level': 'INFO',
        '# Log file path (empty for stdout only)': '',
        'file': '',
    }
    
    with open(path, 'w') as f:
        config.write(f)
    
    print(f"Sample configuration created at: {path}")
    print("Please edit the file with your actual settings.")


def push_logs(odoo, logs: List[Dict], sync_config: Dict,
              device_code: str = None, api_key: str = None) -> tuple:
    """Push logs to Odoo in batches with retry. Returns (sent, failed)."""
    batch_size = sync_config.get('batch_size', 100)
    total_sent = 0
    total_failed = 0

    for i in range(0, len(logs), batch_size):
        batch = logs[i:i + batch_size]
        for attempt in range(sync_config.get('retry_attempts', 3)):
            result = odoo.send_batch(batch, device_code=device_code, api_key=api_key)
            if result.get('ok'):
                total_sent += result.get('sent', len(batch))
                total_failed += result.get('failed', 0)
                logger.info("  batch %s/%s: %s sent, %s failed (%s)",
                            i // batch_size + 1,
                            (len(logs) - 1) // batch_size + 1,
                            result.get('sent', len(batch)),
                            result.get('failed', 0),
                            result.get('message', ''))
                break
            logger.warning("  batch %s failed (attempt %s): %s",
                           i // batch_size + 1, attempt + 1,
                           result.get('message', 'Unknown error'))
            if attempt < sync_config.get('retry_attempts', 3) - 1:
                time.sleep(sync_config.get('retry_delay_seconds', 10))
        else:
            total_failed += len(batch)
    return total_sent, total_failed


def sync_device(dev_conf: Dict, odoo, sync_config: Dict) -> bool:
    """Pull attendance from a single device and push it to Odoo."""
    name = dev_conf.get('name') or dev_conf.get('device_code') or dev_conf.get('host')
    device_type = (dev_conf.get('device_type') or dev_conf.get('type') or 'auto').lower()
    logger.info("=== Syncing device: %s (%s:%s, type=%s) ===",
                name, dev_conf.get('host'), dev_conf.get('port'), device_type)

    # BIOSENSE-T / Chiyu: pull Access Logs over HTTP web UI (not pyzk).
    if device_type in ('biosense_t', 'biosense', 'chiyu'):
        web_port = int(dev_conf.get('port') or 80)
        # Guard against stale Odoo config still set to SoMac (2000) or ZK (4370).
        if web_port in (2000, 4370):
            logger.warning(
                "BIOSENSE %s has device_port=%s in Odoo — that is the SoMac/ZK "
                "port, not the web UI. Using HTTP port 80 instead. Update "
                "device_port to 80 on the device form.",
                name, web_port,
            )
            web_port = 80
        device = BiosenseWebClient(
            host=dev_conf['host'],
            port=web_port,
            username=dev_conf.get('web_username') or 'admin',
            password=dev_conf.get('web_password') or 'admin',
            tz_name=dev_conf.get('timezone') or dev_conf.get('device_tz') or 'UTC',
        )
    else:
        device = BiometricDevice(
            host=dev_conf['host'],
            port=int(dev_conf.get('port') or 4370),
            password=int(dev_conf.get('password') or 0),
            device_type=device_type,
            tz_name=dev_conf.get('timezone') or dev_conf.get('device_tz') or 'UTC',
        )

    if not device.connect():
        return False

    try:
        since = datetime.now() - timedelta(hours=sync_config.get('since_hours', 24))
        logs = device.get_attendance_logs(since=since)
        if not logs:
            logger.info("No new logs for %s", name)
            return True

        sent, failed = push_logs(
            odoo, logs, sync_config,
            device_code=dev_conf.get('device_code'),
            api_key=dev_conf.get('api_key'),
        )
        logger.info("Device %s: %s sent, %s failed", name, sent, failed)

        if sync_config.get('clear_after_sync', False) and failed == 0:
            clear_fn = getattr(device, 'clear_attendance', None)
            if clear_fn:
                logger.info("Clearing device attendance records for %s...", name)
                clear_fn()
            else:
                logger.info("Clear-after-sync skipped for %s (not supported)", name)
        return failed == 0
    finally:
        device.disconnect()


def run_once(odoo, config: Dict) -> bool:
    """A single sync cycle: fleet auto-discovery or a single configured device."""
    sync_config = config['sync']

    if config['odoo'].get('auto_discover'):
        logger.info("Auto-discovering devices from Odoo...")
        fleet = odoo.fetch_fleet()
        if not fleet:
            logger.warning("Odoo returned no devices to sync "
                           "(none active with an IP address).")
            return True

        # Only devices that actually have a LAN address are pollable.
        targets = []
        for dev in fleet:
            if not dev.get('host'):
                logger.warning("Skipping %s: no IP configured in Odoo",
                               dev.get('name'))
                continue
            targets.append(dev)
        if not targets:
            return True

        # Poll every device concurrently: a dead/timing-out device (e.g. the
        # BIOSENSE-T) must never block ingestion from the others (e.g. the ZK
        # F18). Each device uses its own TCP connection, so they are isolated.
        logger.info("Discovered %s device(s) to sync (parallel)", len(targets))
        overall = True
        max_workers = min(len(targets), 8)
        with ThreadPoolExecutor(max_workers=max_workers,
                                thread_name_prefix='bio-sync') as executor:
            future_to_dev = {
                executor.submit(sync_device, dev, odoo, sync_config): dev
                for dev in targets
            }
            for future in as_completed(future_to_dev):
                dev = future_to_dev[future]
                try:
                    overall = future.result() and overall
                except Exception as e:
                    logger.error("Error syncing %s: %s", dev.get('name'), e)
                    overall = False
        return overall

    dev_conf = dict(config['device'])
    dev_conf.setdefault('device_code', config['odoo'].get('device_code'))
    if not dev_conf.get('host'):
        logger.error("No device host configured and auto_discover is off.")
        return False
    return sync_device(dev_conf, odoo, sync_config)


def run_daemon(odoo, config: Dict):
    """Run continuous sync daemon."""
    interval = config['sync'].get('interval_minutes', 5) * 60
    logger.info("Starting sync daemon (interval: %s minutes)", interval // 60)

    while True:
        try:
            run_once(odoo, config)
        except Exception as e:
            logger.error("Sync cycle error: %s", str(e))
        logger.info("Waiting %s minutes until next sync...", interval // 60)
        time.sleep(interval)


def probe_biosense(host: str, username: str = 'admin', password: str = 'admin',
                   port: int = 80, timeout: int = 15):
    """Dump the BIOSENSE web handshake so login problems can be diagnosed.

    Prints, for each candidate URL: HTTP status, auth challenge, page title and
    a short body snippet — enough to tell whether the device wants Basic auth,
    a form login, or uses different page names than we expect.
    """
    from requests.auth import HTTPBasicAuth, HTTPDigestAuth

    base = 'http://%s:%s' % (host, port)
    print('=' * 70)
    print('BIOSENSE probe: %s (user=%r)' % (base, username))
    print('=' * 70)

    def describe(resp):
        title = ''
        m = re.search(r'<title[^>]*>(.*?)</title>', resp.text or '',
                      flags=re.IGNORECASE | re.DOTALL)
        if m:
            title = BiosenseWebClient._strip_html(m.group(1))[:80]
        challenge = resp.headers.get('WWW-Authenticate', '')
        print('    status=%s len=%s%s' % (
            resp.status_code, len(resp.content or b''),
            ' auth=%r' % challenge if challenge else ''))
        if title:
            print('    title=%r' % title)
        snippet = BiosenseWebClient._strip_html(resp.text or '')[:300]
        if snippet:
            print('    body=%r' % snippet)

    session = requests.Session()
    session.headers.update({'User-Agent': 'BiometricBridge/1.0 (probe)'})

    print('\n[1] Anonymous GET /')
    try:
        resp = session.get(base + '/', timeout=timeout)
        describe(resp)
    except Exception as e:
        print('    ERROR: %s' % e)
        print('\nDevice unreachable — check LAN/firewall and Web Management Port.')
        return

    for scheme_name, auth_cls in (('Basic', HTTPBasicAuth), ('Digest', HTTPDigestAuth)):
        print('\n[2] HTTP %s auth GET /' % scheme_name)
        try:
            resp = session.get(base + '/', auth=auth_cls(username, password),
                               timeout=timeout)
            describe(resp)
        except Exception as e:
            print('    ERROR: %s' % e)

    print('\n[3] Candidate Access Log pages (with Basic auth)')
    auth = HTTPBasicAuth(username, password)
    for path in BiosenseWebClient._LOG_PATHS:
        try:
            resp = session.get(base + path, auth=auth, timeout=timeout)
            print('  %s' % path)
            describe(resp)
        except Exception as e:
            print('  %s -> ERROR: %s' % (path, e))

    print('\n[4] Links found on the home page')
    try:
        home = session.get(base + '/', auth=auth, timeout=timeout)
        links = re.findall(r'(?:href|src|action)=["\']([^"\']+)["\']', home.text or '')
        for link in sorted(set(links))[:40]:
            print('  %s' % link)

        frames = re.findall(r'<(?:i?frame)[^>]+src=["\']([^"\']+)["\']',
                            home.text or '', flags=re.IGNORECASE)
        if frames:
            print('\n  Frames (following each):')
            for src in frames:
                url = src if src.startswith('http') else (
                    base + src if src.startswith('/') else base + '/' + src)
                print('   %s' % url)
                try:
                    fr = session.get(url, auth=auth, timeout=timeout)
                    describe(fr)
                    sub = re.findall(r'(?:href|action)=["\']([^"\']+)["\']',
                                     fr.text or '')
                    for link in sorted(set(sub))[:25]:
                        print('      link: %s' % link)
                except Exception as e:
                    print('      ERROR: %s' % e)
        forms = re.findall(r'<form[^>]*>', home.text or '', flags=re.IGNORECASE)
        if forms:
            print('\n  Forms:')
            for f in forms[:10]:
                print('   %s' % f[:200])
        inputs = re.findall(r'<input[^>]*>', home.text or '', flags=re.IGNORECASE)
        if inputs:
            print('\n  Inputs:')
            for i in inputs[:20]:
                print('   %s' % i[:200])
    except Exception as e:
        print('  ERROR: %s' % e)

    print('\n[5] Full client attempt')
    client = BiosenseWebClient(host, port=port, username=username,
                               password=password, timeout=timeout)
    if client.connect():
        raw = client._fetch_access_log_html()
        if raw:
            print('\n  Access Log table rows as the device sends them:')
            for block in re.findall(r'<tr[^>]*>(.*?)</tr>', raw,
                                    flags=re.IGNORECASE | re.DOTALL)[:12]:
                cells = re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', block,
                                   flags=re.IGNORECASE | re.DOTALL)
                print('   %s' % [BiosenseWebClient._strip_html(c).strip()
                                 for c in cells])
        logs = client.get_attendance_logs(since=None)
        print('\n  connect() OK — retrieved %s log(s)' % len(logs))
        for entry in logs[:5]:
            print('   %s' % entry)
    else:
        print('  connect() FAILED — send this whole output for tuning.')
    client.disconnect()


def main():
    parser = argparse.ArgumentParser(
        description='Biometric Middleware for Odoo Hybrid Integration',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Create sample configuration file
  %(prog)s --create-config config.ini

  # Run with configuration file
  %(prog)s --config config.ini

  # Run once with direct parameters
  %(prog)s --host 192.168.2.12 --api-key YOUR_KEY --url https://company.odoo.com --once

  # Run as daemon with 10-minute intervals
  %(prog)s --config config.ini --daemon
        """
    )
    
    parser.add_argument('--config', '-c', help='Configuration file path')
    parser.add_argument('--create-config', help='Create sample configuration file', metavar='PATH')
    parser.add_argument('--probe-biosense', metavar='HOST',
                        help='Diagnose a BIOSENSE/Chiyu device web UI and exit')
    parser.add_argument('--web-user', default='admin',
                        help='Web username for --probe-biosense (default: admin)')
    parser.add_argument('--web-password', default='admin',
                        help='Web password for --probe-biosense (default: admin)')
    parser.add_argument('--host', help='Device IP address')
    parser.add_argument('--port', type=int, default=4370, help='Device port (default: 4370)')
    parser.add_argument('--url', help='Odoo URL')
    parser.add_argument('--db', help='Odoo database name (for multi-DB hosts)')
    parser.add_argument('--api-key', help='API Key')
    parser.add_argument('--device-code', help='Device code')
    parser.add_argument('--transport', choices=['custom', 'json2'], default=None,
                        help='Odoo transport: custom endpoint or Odoo 19 JSON-2 API')
    parser.add_argument('--auto-discover', dest='auto_discover', action='store_true',
                        default=None, help='Pull the device list from Odoo and sync all devices')
    parser.add_argument('--no-auto-discover', dest='auto_discover', action='store_false',
                        help='Disable auto-discovery; use a single configured device')
    parser.add_argument('--once', action='store_true', help='Run once and exit')
    parser.add_argument('--daemon', action='store_true', help='Run as continuous daemon')
    parser.add_argument('--since-hours', type=int, default=24, help='Fetch logs since N hours ago')
    parser.add_argument('--clear-after-sync', action='store_true', 
                       help='Clear device logs after sync (CAUTION!)')
    parser.add_argument('--verbose', '-v', action='store_true', help='Verbose output')
    
    args = parser.parse_args()
    
    # Create sample config
    if args.create_config:
        create_sample_config(args.create_config)
        return

    # BIOSENSE diagnostics
    if args.probe_biosense:
        logger.setLevel(logging.DEBUG)
        probe_biosense(args.probe_biosense, args.web_user, args.web_password)
        return
    
    # Load configuration
    if args.config:
        if not os.path.exists(args.config):
            print(f"Configuration file not found: {args.config}")
            print("Create one with: python biometric_middleware.py --create-config config.ini")
            sys.exit(1)
        config = load_config(args.config)
        if args.transport:
            config['odoo']['transport'] = args.transport
        if args.auto_discover is not None:
            config['odoo']['auto_discover'] = args.auto_discover
        if args.db:
            config['odoo']['db'] = args.db
    else:
        # Use command line arguments. Default to single-device mode when a
        # --host is given, otherwise auto-discover.
        auto_discover = args.auto_discover
        if auto_discover is None:
            auto_discover = not bool(args.host)
        config = {
            'odoo': {
                'url': args.url or '',
                'db': args.db or '',
                'api_key': args.api_key or '',
                'device_code': args.device_code,
                'timeout': 30,
                'transport': args.transport or 'custom',
                'auto_discover': auto_discover,
            },
            'device': {
                'host': args.host or '',
                'port': args.port,
                'password': 0,
                'type': 'auto',
            },
            'sync': {
                'interval_minutes': 5,
                'batch_size': 100,
                'clear_after_sync': args.clear_after_sync,
                'since_hours': args.since_hours,
                'retry_attempts': 3,
                'retry_delay_seconds': 10,
            },
            'logging': {
                'level': 'DEBUG' if args.verbose else 'INFO',
                'file': '',
            }
        }
    
    # Configure logging (level + optional file sink) now that config is known.
    apply_logging(config.get('logging'), args.verbose)

    # Validate configuration
    odoo_config = config['odoo']
    if not odoo_config['url'] or not odoo_config['api_key']:
        print("Error: Odoo URL and API Key are required")
        print("Provide via config file or --url and --api-key arguments")
        sys.exit(1)

    auto_discover = odoo_config.get('auto_discover', True)
    if not auto_discover and not config['device'].get('host'):
        print("Error: Device host is required when auto-discovery is disabled")
        print("Provide via config [device] host, --host, or enable auto-discovery")
        sys.exit(1)

    # Initialize the Odoo client based on transport
    transport = odoo_config.get('transport', 'custom')
    if transport == 'json2':
        if not auto_discover and not odoo_config.get('device_code'):
            print("Error: device_code is required for JSON-2 single-device mode")
            sys.exit(1)
        logger.info("Using Odoo 19 JSON-2 transport (/json/2)")
        odoo = OdooJson2Client(
            base_url=odoo_config['url'],
            api_key=odoo_config['api_key'],
            device_code=odoo_config.get('device_code'),
            timeout=odoo_config.get('timeout', 30),
            db=odoo_config.get('db'),
        )
    else:
        logger.info("Using custom endpoint transport (/biometric/api/attendance)")
        odoo = OdooAPIClient(
            base_url=odoo_config['url'],
            api_key=odoo_config['api_key'],
            device_code=odoo_config.get('device_code'),
            timeout=odoo_config.get('timeout', 30),
            db=odoo_config.get('db'),
        )

    # Test Odoo connection
    logger.info("Testing Odoo API connection...")
    if not odoo.test_connection():
        print("Failed to connect to Odoo API. Please check your configuration.")
        sys.exit(1)

    if auto_discover:
        logger.info("Mode: fleet auto-discovery (all devices configured in Odoo)")
    else:
        logger.info("Mode: single device (%s)", config['device'].get('host'))

    # Run mode
    if args.once or (not args.daemon and not args.config):
        logger.info("Running single sync cycle...")
        success = run_once(odoo, config)
        sys.exit(0 if success else 1)
    else:
        run_daemon(odoo, config)


if __name__ == '__main__':
    main()
