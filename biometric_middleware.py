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
    from zk.const import ATTENDANCE_STATUS
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
    """Map a raw device punch/status value to our strict enum.

    ``'0'``=Check In, ``'1'``=Check Out, ``'2'``=Break Out, ``'3'``=Break In,
    ``'4'``=Overtime In, ``'5'``=Overtime Out. Anything unexpected is mapped to
    ``'255'`` (Undefined) so it is still stored but never silently
    mis-classified as a real check-in/out.
    """
    token = str(value).strip() if value is not None else ''
    return token if token in VALID_PUNCH_TYPES else UNDEFINED_PUNCH


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
        """Connect to the biometric device"""
        if not PYZK_AVAILABLE:
            logger.error("pyzk library not available. Cannot connect to device.")
            return False
        
        try:
            self.zk = ZK(
                self.host,
                port=self.port,
                timeout=self.timeout,
                password=self.password,
                force_udp=False,
                ommit_ping=False
            )
            self.conn = self.zk.connect()
            logger.info("Connected to device at %s:%s", self.host, self.port)
            
            # Log device info
            device_info = self.get_device_info()
            logger.info("Device: %s", json.dumps(device_info, indent=2, default=str))
            return True
            
        except Exception as e:
            logger.error("Failed to connect to %s:%s - %s", self.host, self.port, str(e))
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

                logs.append({
                    'pin': str(record.user_id),
                    # Convert the naive device-local stamp to UTC using the
                    # device's own timezone *before* transmission.
                    'timestamp': to_utc_iso(record.timestamp, self.tz_name),
                    # Map the raw device status to our strict punch enum
                    # (unexpected values become '255' / Undefined).
                    'punch': map_punch(getattr(record, 'status', None)),
                    'auth_type': str(record.punch),
                })
            
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
    logger.info("=== Syncing device: %s (%s:%s) ===",
                name, dev_conf.get('host'), dev_conf.get('port'))

    device = BiometricDevice(
        host=dev_conf['host'],
        port=int(dev_conf.get('port') or 4370),
        password=int(dev_conf.get('password') or 0),
        device_type=dev_conf.get('device_type') or dev_conf.get('type') or 'auto',
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
            logger.info("Clearing device attendance records for %s...", name)
            device.clear_attendance()
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
