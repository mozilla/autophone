# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import datetime
import glob
import json
import logging
import os
import time

from logdecorator import LogDecorator
from adb_android import ADBAndroid as ADBDevice
from adb import ADBError
from mozprofile import FirefoxProfile
from options import *

class PhoneTestMessage(object):

    IDLE = 'IDLE'
    INSTALLING = 'INSTALLING BUILD'
    WORKING = 'WORKING'
    REBOOTING = 'REBOOTING'
    DISCONNECTED = 'DISCONNECTED'  # temporary error
    DISABLED = 'DISABLED'  # permanent error

    class JsonEncoder(json.JSONEncoder):
        def default(self, obj):
            if isinstance(obj, PhoneTestMessage):
                return { 'phoneid': obj.phoneid, 'online': obj.online,
                         'msg': obj.msg, 'timestamp': obj.timestamp }
            return json.JSONEncoder.default(self, obj)


    def __init__(self, phoneid, status, current_build=None, current_repo=None,
                 msg=None):
        self.phoneid = phoneid
        self.status = status
        self.current_build = current_build
        self.current_repo = current_repo
        self.msg = msg
        self.timestamp = datetime.datetime.now().replace(microsecond=0)

    def __str__(self):
        s = '<%s> %s (%s)' % (self.timestamp.isoformat(), self.phoneid,
                              self.status)
        if self.msg:
            s += ': %s' % self.msg
        return s

    def short_desc(self):
        s = self.status
        if self.msg:
            s += ': %s' % self.msg
        return s


class PhoneTest(object):

    """
    The initialization function. It takes and stores all the information
    related to contacting this phone.
    Params:
    phoneid = ID of phone, to be used in log messages and reporting
    serial = serial number for adb style interfaces
    ip = phone's IP address (where sutagent running if it is running)
    sutcmdport = cmd port of sutagent if it is running
    sutdataport = data port of sutagent if it is running
    machinetype = pretty name of machine type - i.e. galaxy_nexus, droid_pro etc
    osver = version string of phone OS
    TODO: Add in connection data here for programmable power so we can add a
    powercycle method to this class.
    """
    def __init__(self, phone_cfg, user_cfg, config_file=None, status_cb=None,
                 enable_unittests=False, test_devices_repos={}):
        self.config_file = config_file
        self.enable_unittests = enable_unittests
        self.test_devices_repos = test_devices_repos
        self.status_cb = status_cb
        self.phone_cfg = phone_cfg
        self.user_cfg = user_cfg
        self.status = None
        self.logger = logging.getLogger('autophone.phonetest')
        self.loggerdeco = LogDecorator(self.logger,
                                       {'phoneid': self.phone_cfg['phoneid'],
                                        'pid': os.getpid()},
                                       '%(phoneid)s|%(pid)s|%(message)s')
        self.loggerdeco.info('init autophone.phonetest')
        self._base_device_path = ''
        self.profile_path = '/data/local/tmp/profile'
        self._dm = None

    def _check_device(self):
        for attempt in range(self.user_cfg[PHONE_RETRY_LIMIT]):
            output = self._dm.get_state()
            if output == 'device':
                break
            self.loggerdeco.warning(
                'PhoneTest:_check_device Attempt: %d: %s' %
                (attempt, output))
            time.sleep(self.user_cfg[PHONE_RETRY_WAIT])
        if output != 'device':
            raise ADBError('PhoneTest:_check_device: Failed')

    @property
    def dm(self):
        if not self._dm:
            self.loggerdeco.info('PhoneTest: Connecting to %s...' % self.phone_cfg['phoneid'])
            self._dm = ADBDevice(device_serial=self.phone_cfg['serial'],
                                 log_level=self.user_cfg['debug'],
                                 logger_name='autophone.phonetest.adb',
                                 device_ready_retry_wait=self.user_cfg[DEVICE_READY_RETRY_WAIT],
                                 device_ready_retry_attempts=self.user_cfg[DEVICE_READY_RETRY_ATTEMPTS])
            # Override mozlog.logger
            self._dm._logger = self.loggerdeco
            self.loggerdeco.info('PhoneTest: Connected.')
        self._check_device()
        return self._dm

    @property
    def base_device_path(self):
        if self._base_device_path:
            return self._base_device_path
        success = False
        e = None
        for attempt in range(self.user_cfg[PHONE_RETRY_LIMIT]):
            self._base_device_path = self.dm.test_root + '/autophone'
            self.loggerdeco.debug('Attempt %d creating base device path %s' % (attempt, self._base_device_path))
            try:
                if not self.dm.is_dir(self._base_device_path):
                    self.dm.mkdir(self._base_device_path, parents=True)
                success = True
                break
            except ADBError:
                self.loggerdeco.exception('Attempt %d creating base device path %s' % (attempt, self._base_device_path))
                time.sleep(self.user_cfg[PHONE_RETRY_WAIT])

        if not success:
            raise e

        self.loggerdeco.debug('base_device_path is %s' % self._base_device_path)

        return self._base_device_path

    def runjob(self, build_metadata, worker_subprocess):
        raise NotImplementedError

    def set_dm_debug(self, level):
        self.user_cfg['debug'] = level
        if self._dm:
            self._dm.log_level = level

    """
    sets the status
    Params:
    online = boolean True of False
    msg = the message of status
    """
    def set_status(self, status=PhoneTestMessage.WORKING, msg=None):
        self.status = PhoneTestMessage(self.phone_cfg['phoneid'], status,
                                       self.current_build,
                                       self.current_repo, msg)
        if self.status_cb:
            self.status_cb(self.status)

    def install_profile(self, profile=None):
        if not profile:
            profile = FirefoxProfile()

        profile_path_parent = os.path.split(self.profile_path)[0]
        success = False
        for attempt in range(self.user_cfg[PHONE_RETRY_LIMIT]):
            try:
                self.loggerdeco.debug('Attempt %d installing profile' % attempt)
                if self.dm.is_dir(self.profile_path, root=True):
                    self.dm.rm(self.profile_path, recursive=True, root=True)
                self.dm.chmod(profile_path_parent, root=True)
                self.dm.mkdir(self.profile_path, root=True)
                self.dm.chmod(self.profile_path, root=True)
                self.dm.push(profile.profile, self.profile_path)
                self.dm.chmod(self.profile_path, recursive=True, root=True)
                success = True
                break
            except ADBError:
                self.loggerdeco.exception('Attempt %d Exception installing profile to %s' % (attempt, self.profile_path))
                time.sleep(self.user_cfg[PHONE_RETRY_WAIT])

        if not success:
            self.loggerdeco.error('Failure installing profile to %s' % self.profile_path)

        return success

    def run_fennec_with_profile(self, appname, url):
        self.loggerdeco.debug('run_fennec_with_profile: %s %s' % (appname, url))
        try:
            self.dm.pkill(appname, root=True)
            self.dm.launch_fennec(appname,
                                 intent="android.intent.action.VIEW",
                                 moz_env={'MOZ_CRASHREPORTER_NO_REPORT': '1'},
                                 extra_args=['--profile', self.profile_path],
                                 url=url,
                                 wait=False,
                                 fail_if_running=False)
        except:
            self.loggerdeco.exception('run_fennec_with_profile: Exception:')
            raise

    def remove_sessionstore_files(self):
        self.dm.rm(self.profile_path + '/sessionstore.js', force=True)
        self.dm.rm(self.profile_path + '/sessionstore.bak', force=True)

    def check_for_crashes(self):
        """
        Perform a quick check for crashes by checking
        self.profile_path/minidumps for dump files.

        TODO: Should use mozbase/mozcrash with symbols and minidump_stackwalk
        to process and report crashes.
        """
        if glob.glob(os.path.join(self.profile_path, 'minidumps', '*.dmp')):
            return True
        return False
