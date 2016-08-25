# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

from builds import BuildCache
from worker import Crashes, PhoneWorker

class AutophoneOptions(object):
    """Encapsulate the command line and ini file options used to configure
    Autophone. Each attribute is initialized to an 'empty' value which also
    is of the same type as the final option value so that the appropriate
    getters can be determined."""
    def __init__(self):
        # command line options
        self.ipaddr = ''
        self.port = -1
        self.credentials_file = ''
        self.devicescfg = ''
        self.logfile = ''
        self.loglevel = ''
        self.test_path = ''
        self.minidump_stackwalk = ''
        self.emailcfg = ''
        self.phonedash_url = ''
        self.webserver_url = ''
        self.enable_pulse = False
        self.pulse_durable_queue = True
        self.pulse_jobactions_exchange = ''
        self.cache_dir = ''
        self.override_build_dir = ''
        self.allow_duplicate_jobs = False
        self.repos = []
        self.buildtypes = []
        self.lifo = False
        self.build_cache_port = -1
        self.verbose = False
        self.treeherder_url = ''
        self.treeherder_client_id = ''
        self.treeherder_secret = ''
        self.treeherder_tier = 0
        self.treeherder_retry_wait = 0
        self.reboot_on_error = False
        self.maximum_heartbeat = 0
        self.usbwatchdog_appname = ''
        self.usbwatchdog_poll_interval = 0
        self.device_test_root = ''
        # Sensitive options should not be output to the logs
        self.phonedash_user = ''
        self.phonedash_password = ''
        self.pulse_user = ''
        self.pulse_password = ''
        self.s3_upload_bucket = ''
        self.aws_access_key_id = ''
        self.aws_access_key = ''

        # ini options
        self.build_cache_size = BuildCache.MAX_NUM_BUILDS
        self.build_cache_expires = BuildCache.EXPIRE_AFTER_DAYS
        self.device_ready_retry_wait = PhoneWorker.DEVICE_READY_RETRY_WAIT
        self.device_ready_retry_attempts = PhoneWorker.DEVICE_READY_RETRY_ATTEMPTS
        self.device_battery_min = PhoneWorker.DEVICE_BATTERY_MIN
        self.device_battery_max = PhoneWorker.DEVICE_BATTERY_MAX
        self.phone_retry_limit = PhoneWorker.DEVICE_READY_RETRY_ATTEMPTS
        self.phone_retry_wait = PhoneWorker.DEVICE_READY_RETRY_WAIT
        self.phone_max_reboots = PhoneWorker.PHONE_MAX_REBOOTS
        self.phone_ping_interval = PhoneWorker.PHONE_PING_INTERVAL
        self.phone_command_queue_timeout = PhoneWorker.PHONE_COMMAND_QUEUE_TIMEOUT
        self.phone_crash_window = Crashes.CRASH_WINDOW
        self.phone_crash_limit = Crashes.CRASH_LIMIT
        # other
        self.debug = 3

    def __str__(self):
        # Do not publish sensitive information
        whitelist = ('ipaddr',
                     'port',
                     'devicescfg',
                     'logfile',
                     'loglevel',
                     'test_path',
                     'minidump_stackwalk',
                     'emailcfg',
                     'phonedash_url',
                     'webserver_url',
                     'enable_pulse',
                     'pulse_durable_queue',
                     'pulse_jobactions_exchange',
                     'cache_dir',
                     'override_build_dir',
                     'allow_duplicate_jobs',
                     'repos',
                     'buildtypes',
                     'lifo',
                     'build_cache_port',
                     'verbose',
                     'treeherder_url',
                     'treeherder_tier',
                     'treeherder_retry_wait',
                     'reboot_on_error',
                     'maximum_heartbeat',
                     'device_test_root',
                     'build_cache_size',
                     'build_cache_expires',
                     'device_ready_retry_wait',
                     'device_ready_retry_attempts',
                     'device_battery_min',
                     'device_battery_max',
                     'phone_retry_limit',
                     'phone_retry_wait',
                     'phone_max_reboots',
                     'phone_ping_interval',
                     'phone_command_queue_timeout',
                     'phone_crash_window',
                     'phone_crash_limit',
                     'debug')
        d = {}
        for attr in whitelist:
            d[attr] = getattr(self, attr)
        return '%s' % d

    def __repr__(self):
        return self.__str__()
