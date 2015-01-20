# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import ConfigParser
import logging
import os
import shutil
import tempfile
import time

from mozprofile import FirefoxProfile

from adb import ADBError
from adb_android import ADBAndroid as ADBDevice
from logdecorator import LogDecorator
from phonestatus import PhoneStatus


class Logcat(object):
    def __init__(self, phonetest):
        self.phonetest = phonetest
        self._accumulated_logcat = []
        self._last_logcat = []

    def get(self, full=False):
        """Return the contents of logcat as list of strings.

        :param full: optional boolean which defaults to False. If full
                     is False, then get() will only return logcat
                     output since the last call to clear(). If
                     full is True, then get() will return all
                     logcat output since the test was initialized or
                     teardown_job was last called.

        """
        for attempt in range(1, self.phonetest.options.phone_retry_limit+1):
            try:
                self._last_logcat = [x.strip() for x in
                                     self.phonetest.dm.get_logcat(
                                         filter_specs=['*:V']
                                     )]
                output = []
                if full:
                    output.extend(self._accumulated_logcat)
                output.extend(self._last_logcat)
                return output
            except ADBError:
                self.phonetest.loggerdeco.exception('Attempt %d get logcat' % attempt)
                if attempt == self.phonetest.options.phone_retry_limit:
                    raise
                time.sleep(self.phonetest.options.phone_retry_wait)

    def clear(self):
        """Clears the device's logcat."""
        self.get()
        self._accumulated_logcat.extend(self._last_logcat)
        self._last_logcat = []
        self.phonetest.dm.clear_logcat()


class PhoneTest(object):
    def __init__(self, phone, options, config_file=None,
                 enable_unittests=False, test_devices_repos={},
                 chunk=1):
        self.config_file = config_file
        self.cfg = ConfigParser.RawConfigParser()
        self.cfg.read(self.config_file)
        self.enable_unittests = enable_unittests
        self.test_devices_repos = test_devices_repos
        self.chunk = chunk
        self.chunks = 1
        self.update_status_cb = None
        self.phone = phone
        self.worker_subprocess = None
        self.options = options
        self.logger = logging.getLogger('autophone.phonetest')
        self.loggerdeco = LogDecorator(self.logger,
                                       {'phoneid': self.phone.id,
                                        'pid': os.getpid()},
                                       '%(phoneid)s|%(pid)s|%(message)s')
        self.logger_original = None
        self.loggerdeco_original = None
        self.dm_logger_original = None
        self.loggerdeco.info('init autophone.phonetest')
        self._base_device_path = ''
        self.profile_path = '/data/local/tmp/profile'
        self._dm = None
        self._repos = None
        self._log = None
        # Treeherder related items.
        self._job_name = None
        self._job_symbol = None
        self._group_name = None
        self._group_symbol = None
        self.test_result = PhoneTestResult()
        self.message = None
        self.job_guid = None
        self.job_details = []
        self.submit_timestamp = None
        self.start_timestamp = None
        self.end_timestamp = None
        self.logcat = Logcat(self)
        self.loggerdeco.debug('PhoneTest: %s, cfg sections: %s' % (self.__dict__, self.cfg.sections()))
        if not self.cfg.sections():
            self.loggerdeco.warning('Test configuration not found. '
                                    'Will use defaults.')
        # upload_dir will hold ANR traces, tombstones and other files
        # pulled from the device.
        self.upload_dir = None
        # crash_processor is an instance of AutophoneCrashProcessor that
        # is used by non-unittests to process device errors and crashes.
        self.crash_processor = None

    def _check_device(self):
        for attempt in range(1, self.options.phone_retry_limit+1):
            output = self._dm.get_state()
            if output == 'device':
                break
            self.loggerdeco.warning(
                'PhoneTest:_check_device Attempt: %d: %s' %
                (attempt, output))
            time.sleep(self.options.phone_retry_wait)
        if output != 'device':
            raise ADBError('PhoneTest:_check_device: Failed')

    @property
    def name_suffix(self):
        return  '-%s' % self.chunk if self.chunk > 1 else ''

    @property
    def name(self):
        return 'autophone-%s%s' % (self.__class__.__name__, self.name_suffix)

    @property
    def dm(self):
        if not self._dm:
            self.loggerdeco.info('PhoneTest: Connecting to %s...' % self.phone.id)
            self._dm = ADBDevice(device=self.phone.serial,
                                 logger_name='autophone.phonetest.adb',
                                 device_ready_retry_wait=self.options.device_ready_retry_wait,
                                 device_ready_retry_attempts=self.options.device_ready_retry_attempts,
                                 verbose=self.options.verbose)
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
        for attempt in range(1, self.options.phone_retry_limit+1):
            self._base_device_path = self.dm.test_root + '/autophone'
            self.loggerdeco.debug('Attempt %d creating base device path %s' % (
                attempt, self._base_device_path))
            try:
                if not self.dm.is_dir(self._base_device_path):
                    self.dm.mkdir(self._base_device_path, parents=True)
                success = True
                break
            except ADBError:
                self.loggerdeco.exception('Attempt %d creating base device '
                                          'path %s' % (
                                              attempt, self._base_device_path))
                time.sleep(self.options.phone_retry_wait)

        if not success:
            raise e

        self.loggerdeco.debug('base_device_path is %s' % self._base_device_path)

        return self._base_device_path

    @property
    def job_name(self):
        if not self.options.treeherder_url:
            return None
        if not self._job_name:
            self._job_name = self.cfg.get('treeherder', 'job_name')
        return self._job_name

    @property
    def job_symbol(self):
        if not self.options.treeherder_url:
            return None
        if not self._job_symbol:
            self._job_symbol = self.cfg.get('treeherder', 'job_symbol')
            if self.chunks > 1:
                self._job_symbol = "%s%s" %(self._job_symbol, self.chunk)
        return self._job_symbol

    @property
    def group_name(self):
        if not self.options.treeherder_url:
            return None
        if not self._group_name:
            self._group_name = self.cfg.get('treeherder', 'group_name')
        return self._group_name

    @property
    def group_symbol(self):
        if not self.options.treeherder_url:
            return None
        if not self._group_symbol:
            self._group_symbol = self.cfg.get('treeherder', 'group_symbol')
        return self._group_symbol

    @property
    def build(self):
        return self.worker_subprocess.build

    @property
    def buildername(self):
        return "%s %s opt test %s-%s" % (
            self.phone.platform, self.build.tree, self.job_name, self.job_symbol)

    def handle_crashes(self):
        if not self.crash_processor:
            return

        for error in self.crash_processor.get_errors(self.build.symbols,
                                                     self.options.minidump_stackwalk,
                                                     clean=False):
            if error['reason'] == 'java-exception':
                self.test_result.add_failure(self.name,
                                             'PROCESS-CRASH',
                                             error['signature'])
            elif error['reason'] == 'PROFILE-ERROR':
                self.test_result.add_failure(self.name,
                                             error['reason'],
                                             error['signature'])
            elif error['reason'] == 'PROCESS-CRASH':
                self.loggerdeco.info("PROCESS-CRASH | %s | "
                                     "application crashed [%s]" % (self.name,
                                                                   error['signature']))
                self.loggerdeco.info(error['stackwalk_output'])
                self.loggerdeco.info(error['stackwalk_errors'])

                self.test_result.add_failure(self.name,
                                             error['reason'],
                                             'application crashed [%s]' % error['signature'])
            else:
                self.loggerdeco.warning('Unknown error reason: %s' % error['reason'])

    def setup_job(self):
        self.logger_original = self.logger
        self.loggerdeco_original = self.loggerdeco
        self.dm_logger_original = self.dm._logger
        # Clear the log file if we are submitting logs to Treeherder.
        if (self.worker_subprocess.options.treeherder_url and
            self.worker_subprocess.build.revision_hash and
            self.worker_subprocess.s3_bucket):
            self.worker_subprocess.initialize_log_filehandler()
        self.worker_subprocess.treeherder.submit_running(tests=[self])

        self.logger = logging.getLogger('autophone.worker.subprocess.test')
        self.loggerdeco = LogDecorator(self.logger,
                                       {'phoneid': self.phone.id,
                                        'pid': os.getpid(),
                                        'buildid': self.build.id,
                                        'test': self.name},
                                       '%(phoneid)s|%(pid)s|%(buildid)s|%(test)s|'
                                       '%(message)s')
        self.dm._logger = self.loggerdeco
        self.loggerdeco.debug('PhoneTest.setup_job')
        if self._log:
            os.unlink(self._log)
        self._log = None
        self.upload_dir = tempfile.mkdtemp()
        self.test_result = PhoneTestResult()

    def run_job(self):
        raise NotImplementedError

    def teardown_job(self):
        self.loggerdeco.debug('PhoneTest.teardown_job')
        try:
            self.handle_crashes()
            self.worker_subprocess.treeherder.submit_complete(test=self)
        finally:
            if self.upload_dir and os.path.exists(self.upload_dir):
                shutil.rmtree(self.upload_dir)
            self.upload_dir = None

        if self.logger.getEffectiveLevel() == logging.DEBUG and self._log:
            self.loggerdeco.debug(40 * '=')
            logfilehandle = open(self._log)
            self.loggerdeco.debug(logfilehandle.read())
            logfilehandle.close()
            self.loggerdeco.debug(40 * '-')
        self._log = None
        self.logcat = Logcat(self)
        if self.logger_original:
            self.logger = self.logger_original
        if self.loggerdeco_original:
            self.loggerdeco = self.loggerdeco_original
        if self.dm_logger_original:
            self.dm._logger = self.dm_logger_original

    def set_dm_debug(self, level):
        self.options.debug = level
        if self._dm:
            self._dm.log_level = level

    def update_status(self, phone_status=PhoneStatus.WORKING, message=None):
        if self.update_status_cb:
            self.update_status_cb(phone_status=phone_status,
                                  build=self.build,
                                  message=message)

    def install_profile(self, profile=None):
        if not profile:
            profile = FirefoxProfile()

        profile_path_parent = os.path.split(self.profile_path)[0]
        success = False
        for attempt in range(1, self.options.phone_retry_limit+1):
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
                self.loggerdeco.exception('Attempt %d Exception installing '
                                          'profile to %s' % (
                                              attempt, self.profile_path))
                time.sleep(self.options.phone_retry_wait)

        if not success:
            self.loggerdeco.error('Failure installing profile to %s' % self.profile_path)

        return success

    def run_fennec_with_profile(self, appname, url):
        self.loggerdeco.debug('run_fennec_with_profile: %s %s' % (appname, url))
        try:
            self.dm.pkill(appname, root=True)
            self.dm.launch_fennec(appname,
                                 intent="android.intent.action.VIEW",
                                 moz_env={'MOZ_CRASHREPORTER_NO_REPORT': '1',
                                          'MOZ_CRASHREPORTER': '1'},
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

    @property
    def fennec_crashed(self):
        """
        Perform a quick check for crashes by checking
        self.profile_path/minidumps for dump files.

        """
        if self.dm.exists(os.path.join(self.profile_path, 'minidumps', '*.dmp')):
            self.loggerdeco.info('fennec crashed')
            return True
        return False

class PhoneTestResult(object):
    """PhoneTestResult encapsulates the data format used by logparser
    so we can have a uniform approach to recording test results between
    native Autophone tests and Unit tests.
    """
    #SKIPPED = 'skipped'
    BUSTED = 'busted'
    EXCEPTION = 'exception'
    TESTFAILED = 'testfailed'
    UNKNOWN = 'unknown'
    USERCANCEL = 'usercancel'
    RETRY = 'retry'
    SUCCESS = 'success'

    def __init__(self):
        self.status = None
        self.passes = []
        self.failures = []
        self.todo = 0

    def __str__(self):
        return "PhoneTestResult: passes: %s, failures: %s" % (self.passes, self.failures)

    @property
    def passed(self):
        return len(self.passes)

    @property
    def failed(self):
        return len(self.failures)

    def add_pass(self, testpath):
        self.passes.append(testpath)

    def add_failure(self, testpath, status, text):
        self.failures.append({
            "test": testpath,
            "status": status,
            "text": text})
