# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

from urlparse import urlparse

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
        self.devicescfg = ''
        self.logfile = ''
        self.loglevel = ''
        self.test_path = ''
        self.emailcfg = ''
        self.enable_pulse = False
        self.enable_unittests = False
        self.cache_dir = ''
        self.override_build_dir = ''
        self.repos = []
        self.buildtypes = []
        self.build_cache_port = -1
        self.verbose = False
        self.treeherder_url = ''
        self.treeherder_credentials_path = ''
        self.treeherder_retries = 0
        self.treeherder_retry_wait = 0
        self._treeherder_protocol = ''
        self._treeherder_server = ''
        #self.treeherder_credentials = {} # computed
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

    def _parse_treeherder_url(self):
        p = urlparse(self.treeherder_url)
        self._treeherder_protocol = p.scheme
        self._treeherder_server = p.netloc

    @property
    def treeherder_protocol(self):
        if self.treeherder_url and not self._treeherder_protocol:
            self._parse_treeherder_url()
        return self._treeherder_protocol

    @property
    def treeherder_server(self):
        if self.treeherder_url and not self._treeherder_server:
            self._parse_treeherder_url()
        return self._treeherder_server

    def __str__(self):
        return '%s' % self.__dict__
