# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import ConfigParser
import datetime
import glob
import logging
import os
import posixpath
import re
import sys
import shutil
import tempfile

from time import sleep

from mozprofile import FirefoxProfile

import utils
from autophonecrash import AutophoneCrashProcessor
from adb import ADBError
from logdecorator import LogDecorator
from phonestatus import PhoneStatus

# Set the logger globally in the file, but this must be reset when
# used in a child process.
logger = logging.getLogger()


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
                sleep(self.phonetest.options.phone_retry_wait)

    def clear(self):
        """Clears the device's logcat."""
        self.get()
        self._accumulated_logcat.extend(self._last_logcat)
        self._last_logcat = []
        self.phonetest.dm.clear_logcat()


class PhoneTest(object):
    # Use instances keyed on phoneid+':'config_file+':'+str(chunk)
    # to lookup tests.

    instances = {}

    @classmethod
    def lookup(cls, phoneid, config_file, chunk):
        key = '%s:%s:%s' % (phoneid, config_file, chunk)
        if key in PhoneTest.instances:
            return PhoneTest.instances[key]
        return None

    @classmethod
    def match(cls, tests=None, test_name=None, phoneid=None,
              config_file=None, chunk=None, job_guid=None,
              build_url=None):

        logger.debug('PhoneTest.match(tests: %s, test_name: %s, phoneid: %s, '
                     'config_file: %s, chunk: %s, job_guid: %s, '
                     'build_url: %s' % (tests, test_name, phoneid,
                                        config_file, chunk, job_guid,
                                        build_url))
        matches = []
        if not tests:
            tests = [PhoneTest.instances[key] for key in PhoneTest.instances.keys()]

        for test in tests:
            if test_name and test_name != test.name:
                continue

            if phoneid and phoneid != test.phone.id:
                continue

            if config_file and config_file != test.config_file:
                continue

            if chunk and chunk != test.chunk:
                continue

            if job_guid and job_guid != test.job_guid:
                continue

            if build_url:
                abi = test.phone.abi
                sdk = test.phone.sdk
                # First assume the test and build are compatible.
                incompatible_job = False
                # x86 devices can only test x86 builds and non-x86
                # devices can not test x86 builds.
                if abi == 'x86':
                    if 'x86' not in build_url:
                        incompatible_job = True
                else:
                    if 'x86' in build_url:
                        incompatible_job = True
                # If the build_url does not contain an sdk level, then
                # assume this is an build from before the split sdk
                # builds were first created. Otherwise the build_url
                # must match this device's supported sdk levels.
                if ('api-9' not in build_url and 'api-10' not in build_url and
                    'api-11' not in build_url):
                    pass
                elif sdk not in build_url:
                    incompatible_job  = True

                if incompatible_job:
                    continue

                # The test may be defined for multiple repositories.
                # We are interested if this particular build is
                # supported by this test. First assume it is
                # incompatible, and only accept it if the build_url is
                # from one of the supported repositories.
                if test.repos:
                    incompatible_job = True
                    for repo in test.repos:
                        if repo in build_url:
                            incompatible_job = False
                            break
                    if incompatible_job:
                        continue

            matches.append(test)

        logger.debug('PhoneTest.match = %s' % matches)

        return matches

    def __init__(self, dm=None, phone=None, options=None, config_file=None, chunk=1, repos=[]):
        # Ensure that repos is a list and that it is sorted in order
        # for comparisons with the tests loaded from the jobs database
        # to work.
        assert type(repos) == list, 'PhoneTest repos argument must be a list'
        repos.sort()
        self._add_instance(phone.id, config_file, chunk)
        # The default preferences and environment for running fennec
        # are set here in PhoneTest. Tests which subclass PhoneTest can
        # add or override these preferences during their
        # initialization.
        self._preferences = None
        self._environment = None
        self.config_file = config_file
        self.cfg = ConfigParser.ConfigParser()
        # Make the values in the config file case-sensitive
        self.cfg.optionxform = str
        self.cfg.read(self.config_file)
        self.enable_unittests = False
        self.chunk = chunk
        self.chunks = 1
        self.update_status_cb = None
        self.dm = dm
        self.phone = phone
        self.worker_subprocess = None
        self.options = options
        self.loggerdeco = LogDecorator(logger,
                                       {'phoneid': self.phone.id},
                                       '%(phoneid)s|%(message)s')
        self.loggerdeco_original = None
        self.dm_logger_original = None
        self.loggerdeco.info('init autophone.phonetest')
        self._base_device_path = ''
        self.profile_path = '/data/local/tmp/profile'
        self.repos = repos
        self._log = None
        # Treeherder related items.
        self._job_name = None
        self._job_symbol = None
        self._group_name = None
        self._group_symbol = None
        self.test_result = PhoneTestResult()
        self.message = None
        # A unique consistent guid is necessary for identifying the
        # test job in treeherder. The test job_guid is updated when a
        # test is added to the pending jobs/tests in the jobs
        # database.
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
        # Instrument running time
        self.start_time = None
        self.stop_time = None
        # Perform initial configuration. For tests which do not
        # specify all config options, reasonable defaults will be
        # chosen.

        # [paths]
        self.autophone_directory = os.path.dirname(os.path.abspath(sys.argv[0]))
        self._paths = {}
        self._paths['dest'] = posixpath.join(self.base_device_path,
                                             self.__class__.__name__)
        try:
            sources = self.cfg.get('paths', 'sources').split()
            self._paths['sources'] = []
            for source in sources:
                if not source.endswith('/'):
                    source += '/'
                self._paths['sources'].append(source)
        except (ConfigParser.NoSectionError, ConfigParser.NoOptionError):
            self._paths['sources'] = [
                os.path.join(self.autophone_directory, 'files/base/')]
        try:
            self._paths['dest'] = self.cfg.get('paths', 'dest')
            if not self._paths['dest'].endswith('/'):
                self._paths['dest'] += '/'
        except (ConfigParser.NoSectionError, ConfigParser.NoOptionError):
            pass
        try:
            self._paths['profile'] = self.cfg.get('paths', 'profile')
            if not self._paths['profile'].endswith('/'):
                self._paths['profile'] += '/'
        except (ConfigParser.NoSectionError, ConfigParser.NoOptionError):
            pass
        if 'profile' in self._paths:
            self.profile_path = self._paths['profile']
        # _pushes = {'sourcepath' : 'destpath', ...}
        self._pushes = {}
        for source in self._paths['sources']:
            for push in glob.glob(source + '*'):
                if push.endswith('~') or push.endswith('.bak'):
                    continue
                push_dest = posixpath.join(self._paths['dest'],
                                           os.path.basename(push))
                self._pushes[push] = push_dest
        self._initialize_url = os.path.join('file://', self._paths['dest'],
                                            'initialize_profile.html')
        # [tests]
        self._tests = {}
        try:
            for t in self.cfg.items('tests'):
                self._tests[t[0]] = t[1]
        except ConfigParser.NoSectionError:
            self._tests['blank'] = 'blank.html'

        self.loggerdeco.info('PhoneTest: Connected.')

    def __str__(self):
        return '%s(%s, config_file=%s, chunk=%s)' % (type(self).__name__,
                                                     self.phone,
                                                     self.config_file,
                                                     self.chunk)

    def __repr__(self):
        return self.__str__()

    def _add_instance(self, phoneid, config_file, chunk):
        key = '%s:%s:%s' % (phoneid, config_file, chunk)
        assert key not in PhoneTest.instances, 'Duplicate PhoneTest %s' % key
        PhoneTest.instances[key] = self

    def remove(self):
        key = '%s:%s:%s' % (self.phone.id, self.config_file, self.chunk)
        if key in PhoneTest.instances:
            del PhoneTest.instances[key]

    @property
    def preferences(self):
        # https://dxr.mozilla.org/mozilla-central/source/mobile/android/app/mobile.js
        # https://dxr.mozilla.org/mozilla-central/source/browser/app/profile/firefox.js
        # https://dxr.mozilla.org/mozilla-central/source/addon-sdk/source/test/preferences/no-connections.json

        if not self._preferences:
            self._preferences = {
                'app.update.auto': False,
                'app.update.certs.1.commonName': '',
                'app.update.certs.2.commonName': '',
                'app.update.enabled': False,
                'app.update.staging.enabled': False,
                'app.update.url': '',
                'app.update.url.android':  '',
                'app.update.url.override': '',
                'beacon.enabled': False,
                'browser.EULA.override': True,
                'browser.aboutHomeSnippets.updateUrl': '',
                'browser.firstrun.show.localepicker': False,
                'browser.firstrun.show.uidiscovery': False,
                'browser.newtab.url': '',
                'browser.newtabpage.directory.ping': '',
                'browser.newtabpage.directory.source': 'data:application/json,{"dummy":1}',
                'browser.safebrowsing.downloads.enabled': False,
                'browser.safebrowsing.downloads.remote.enabled': False,
                'browser.safebrowsing.enabled': False,
                'browser.safebrowsing.gethashURL': '',
                'browser.safebrowsing.malware.enabled': False,
                'browser.safebrowsing.malware.reportURL': '',
                'browser.safebrowsing.updateURL': '',
                'browser.search.countryCode': 'US',
                'browser.search.geoSpecificDefaults': False,
                'browser.search.geoip.url': '',
                'browser.search.isUS': True,
                'browser.search.suggest.enabled': False,
                'browser.search.update': False,
                'browser.selfsupport.url': '',
                'browser.sessionstore.resume_from_crash': False,
                'browser.snippets.enabled': False,
                'browser.snippets.firstrunHomepage.enabled': False,
                'browser.snippets.syncPromo.enabled': False,
                'browser.snippets.updateUrl': '',
                'browser.tiles.reportURL': '',
                'browser.trackingprotection.gethashURL': '',
                'browser.trackingprotection.updateURL': '',
                'browser.warnOnQuit': False,
                'browser.webapps.apkFactoryUrl': '',
                'browser.webapps.checkForUpdates': 0,
                'browser.webapps.updateCheckUrl': '',
                'datareporting.healthreport.documentServerURI': '',
                'datareporting.healthreport.service.enabled': False,
                'datareporting.healthreport.uploadEnabled': False,
                'datareporting.policy.dataSubmissionEnabled': False,
                'datareporting.policy.dataSubmissionPolicyBypassAcceptance': True,
                'dom.ipc.plugins.flash.subprocess.crashreporter.enabled': False,
                'extensions.blocklist.enabled': False,
                'extensions.blocklist.interval': 172800,
                'extensions.blocklist.url': '',
                'extensions.getAddons.cache.enabled': False,
                'extensions.update.background.url': '',
                'extensions.update.enabled': False,
                'extensions.update.url': '',
                'extensions.webservice.discoverURL': '',
                'general.useragent.updates.enabled': False,
                'geo.wifi.scan': False,
                'geo.wifi.uri': '',
                'media.autoplay.enabled': True,
                'media.gmp-gmpopenh264.autoupdate': False,
                'media.gmp-manager.cert.checkAttributes': False,
                'media.gmp-manager.cert.requireBuiltIn': False,
                'media.gmp-manager.certs.1.commonName': '',
                'media.gmp-manager.certs.2.commonName': '',
                'media.gmp-manager.url': '',
                'media.gmp-manager.url.override': '',
                'plugin.state.flash': 2,
                'shell.checkDefaultClient': False,
                'toolkit.telemetry.enabled': False,
                'toolkit.telemetry.notifiedOptOut': 999,
                'toolkit.telemetry.prompted': 999,
                'toolkit.telemetry.rejected': True,
                'toolkit.telemetry.server': '',
                'toolkit.telemetry.unified': False,
                'urlclassifier.updateinterval': 172800,
                'webapprt.app_update_interval': 172800,
                'xpinstall.signatures.required': False,
                }
            if self.cfg.has_section('preferences'):
                overrides = self.cfg.options('preferences')
                for name in overrides:
                    value = self.cfg.get('preferences', name)
                    if value.lower() == 'true':
                        value = True
                    elif value.lower() == 'false':
                        value = False
                    elif re.match('\d+$', value):
                        value = int(value)
                    self._preferences[name] = value
        return self._preferences

    @property
    def environment(self):
        if not self._environment:
            # https://developer.mozilla.org/en-US/docs/Environment_variables_affecting_crash_reporting
            self._environment = {
                'MOZ_CRASHREPORTER': '1',
                'MOZ_CRASHREPORTER_SHUTDOWN': '1',
                'MOZ_DISABLE_NONLOCAL_CONNECTIONS': '1',
                'NO_EM_RESTART': '1',
                #'NSPR_LOG_MODULES': 'all:5',
            }
            if self.cfg.has_section('environment'):
                overrides = self.cfg.options('environment')
                for name in overrides:
                    value = self.cfg.get('environment', name)
                    if value.lower() == 'true':
                        value = True
                    elif value.lower() == 'false':
                        value = False
                    elif re.match('\d+$', value):
                        value = int(value)
                    self._environment[name] = value
        return self._environment

    @property
    def name_suffix(self):
        return  '-%s' % self.chunk if self.chunks > 1 else ''

    @property
    def name(self):
        return 'autophone-%s%s' % (self.__class__.__name__, self.name_suffix)

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
                sleep(self.options.phone_retry_wait)

        if not success:
            raise e

        self.loggerdeco.debug('base_device_path is %s' % self._base_device_path)

        return self._base_device_path

    @property
    def job_url(self):
        if not self.options.treeherder_url:
            return None
        job_url = '%s/#/jobs?filter-searchStr=autophone&exclusion_profile=false&repo=%s&revision=%s'
        return job_url % (self.options.treeherder_url,
                          self.build.tree,
                          os.path.basename(self.build.revision))

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

    def get_test_package_names(self):
        """Return a set of test package names which need to be downloaded
        along with the build in order to run the test. This set will
        be passed to the BuildCache.get() method. Normally, this will
        only need to be set for UnitTests.

        See https://bugzilla.mozilla.org/show_bug.cgi?id=1158276
            https://bugzilla.mozilla.org/show_bug.cgi?id=917999
        """
        return set()

    def generate_guid(self):
        self.job_guid = utils.generate_guid()

    def get_buildername(self, tree):
        return "%s %s opt %s" % (
            self.phone.platform, tree, self.name)

    def handle_test_interrupt(self, reason, test_result):
        self.test_failure(self.name, 'TEST-UNEXPECTED-FAIL', reason, test_result)

    def test_pass(self, testpath):
        self.test_result.add_pass(testpath)

    def test_failure(self, testpath, status, message, testresult_status):
        self.message = message
        self.update_status(message=message)
        self.test_result.add_failure(testpath, status, message, testresult_status)

    def handle_crashes(self):
        if not self.crash_processor:
            return

        for error in self.crash_processor.get_errors(self.build.symbols,
                                                     self.options.minidump_stackwalk,
                                                     clean=False):
            if error['reason'] == 'java-exception':
                self.test_failure(
                    self.name, 'PROCESS-CRASH',
                    error['signature'],
                    PhoneTestResult.EXCEPTION)
            elif error['reason'] == 'PROFILE-ERROR':
                self.test_failure(
                    self.name,
                    error['reason'],
                    error['signature'],
                    PhoneTestResult.TESTFAILED)
            elif error['reason'] == 'PROCESS-CRASH':
                self.loggerdeco.info("PROCESS-CRASH | %s | "
                                     "application crashed [%s]" % (self.name,
                                                                   error['signature']))
                self.loggerdeco.info(error['stackwalk_output'])
                self.loggerdeco.info(error['stackwalk_errors'])

                self.test_failure(self.name,
                                  error['reason'],
                                  'application crashed [%s]' % error['signature'],
                                  PhoneTestResult.TESTFAILED)
            else:
                self.loggerdeco.warning('Unknown error reason: %s' % error['reason'])

    def create_profile(self, custom_addons=[], custom_prefs=None, root=True):
        # Create, install and initialize the profile to be
        # used in the test.

        temp_addons = ['quitter.xpi']
        temp_addons.extend(custom_addons)
        addons = ['%s/xpi/%s' % (os.getcwd(), addon) for addon in temp_addons]

        # make sure firefox isn't running when we try to
        # install the profile.

        self.dm.pkill(self.build.app_name, root=root)
        if isinstance(custom_prefs, dict):
            prefs = dict(self.preferences.items() + custom_prefs.items())
        else:
            prefs = self.preferences
        profile = FirefoxProfile(preferences=prefs, addons=addons)
        if not self.install_profile(profile):
            return False

        success = False
        for attempt in range(1, self.options.phone_retry_limit+1):
            self.loggerdeco.debug('Attempt %d Initializing profile' % attempt)
            self.run_fennec_with_profile(self.build.app_name, self._initialize_url)

            if self.wait_for_fennec():
                success = True
                break
            sleep(self.options.phone_retry_wait)

        if not success:
            msg = 'Aborting Test - Failure initializing profile.'
            self.loggerdeco.error(msg)

        return success

    def wait_for_fennec(self, max_wait_time=60, wait_time=5,
                        kill_wait_time=20, root=True):
        # Wait for up to a max_wait_time seconds for fennec to close
        # itself in response to the quitter request. Check that fennec
        # is still running every wait_time seconds. If fennec doesn't
        # close on its own, attempt up to 3 times to kill fennec, waiting
        # kill_wait_time seconds between attempts.
        # Return True if fennec exits on its own, False if it needs to be killed.
        # Re-raise the last exception if fennec can not be killed.
        max_wait_attempts = max_wait_time / wait_time
        for wait_attempt in range(1, max_wait_attempts+1):
            if not self.dm.process_exist(self.build.app_name):
                return True
            sleep(wait_time)
        self.loggerdeco.debug('killing fennec')
        max_killattempts = 3
        for kill_attempt in range(1, max_killattempts+1):
            try:
                self.dm.pkill(self.build.app_name, root=root)
                break
            except ADBError:
                self.loggerdeco.exception('Attempt %d to kill fennec failed' %
                                          kill_attempt)
                if kill_attempt == max_killattempts:
                    raise
                sleep(kill_wait_time)
        return False

    def install_local_pages(self):
        success = False
        for attempt in range(1, self.options.phone_retry_limit+1):
            self.loggerdeco.debug('Attempt %d Installing local pages' % attempt)
            try:
                self.dm.rm(self._paths['dest'], recursive=True, force=True)
                self.dm.mkdir(self._paths['dest'], parents=True)
                for push_source in self._pushes:
                    push_dest = self._pushes[push_source]
                    if os.path.isdir(push_source):
                        self.dm.push(push_source, push_dest)
                    else:
                        self.dm.push(push_source, push_dest)
                success = True
                break
            except ADBError:
                self.loggerdeco.exception('Attempt %d Installing local pages' % attempt)
                sleep(self.options.phone_retry_wait)

        if not success:
            self.loggerdeco.error('Failure installing local pages')

        return success

    def is_fennec_running(self, appname):
        for attempt in range(1, self.options.phone_retry_limit+1):
            try:
                return self.dm.process_exist(appname)
            except ADBError:
                self.loggerdeco.exception('Attempt %d is fennec running' % attempt)
                if attempt == self.options.phone_retry_limit:
                    raise
                sleep(self.options.phone_retry_wait)

    def setup_job(self):
        self.start_time = datetime.datetime.now()
        self.stop_time = self.start_time
        # Clear the Treeherder job details.
        self.job_details = []
        self.worker_subprocess.treeherder.submit_running(
            self.phone.id,
            self.build.url,
            self.build.tree,
            self.build.revision_hash,
            tests=[self])

        self.loggerdeco_original = self.loggerdeco
        # self.dm._logger can raise ADBTimeoutError due to the
        # property dm therefore place it after the initialization.
        self.dm_logger_original = self.dm._logger
        self.loggerdeco = LogDecorator(logger,
                                       {'phoneid': self.phone.id,
                                        'buildid': self.build.id,
                                        'test': self.name},
                                       '%(phoneid)s|%(buildid)s|%(test)s|'
                                       '%(message)s')
        self.dm._logger = self.loggerdeco
        self.loggerdeco.debug('PhoneTest.setup_job')
        if self._log:
            os.unlink(self._log)
        self._log = None
        self.upload_dir = tempfile.mkdtemp()
        self.crash_processor = AutophoneCrashProcessor(self.dm,
                                                       self.profile_path,
                                                       self.upload_dir)
        self.crash_processor.clear()
        self.test_result = PhoneTestResult()
        if not self.worker_subprocess.is_disabled():
            self.update_status(phone_status=PhoneStatus.WORKING,
                               message='Setting up %s' % self.name)

    def run_job(self):
        raise NotImplementedError

    def teardown_job(self):
        self.loggerdeco.debug('PhoneTest.teardown_job')
        self.stop_time = datetime.datetime.now()
        self.loggerdeco.info('Test %s elapsed time: %s' % (
            self.name, self.stop_time - self.start_time))
        try:
            if self.worker_subprocess.is_ok():
                # Do not attempt to process crashes if the device is
                # in an error state.
                self.handle_crashes()
        except Exception, e:
            self.loggerdeco.exception('Exception during crash processing')
            self.test_failure(
                self.name, 'TEST-UNEXPECTED-FAIL',
                'Exception %s during crash processing' % e,
                PhoneTestResult.EXCEPTION)
        try:
            if (self.worker_subprocess.is_disabled() and
                self.test_result.status != PhoneTestResult.USERCANCEL):
                # The worker was disabled while running one test of a job.
                # Record the cancellation on any remaining tests in that job.
                self.test_failure(self.name, 'TEST_UNEXPECTED_FAIL',
                                  'The worker was disabled.',
                                  PhoneTestResult.USERCANCEL)
            self.worker_subprocess.treeherder.submit_complete(
                self.phone.id,
                self.build.url,
                self.build.tree,
                self.build.revision_hash,
                tests=[self])
        except:
            self.loggerdeco.exception('Exception tearing down job')
        finally:
            if self.upload_dir and os.path.exists(self.upload_dir):
                shutil.rmtree(self.upload_dir)
            self.upload_dir = None

        if (logger.getEffectiveLevel() == logging.DEBUG and self._log and
            os.path.exists(self._log)):
            self.loggerdeco.debug(40 * '=')
            try:
                logfilehandle = open(self._log)
                self.loggerdeco.debug(logfilehandle.read())
                logfilehandle.close()
            except Exception:
                self.loggerdeco.exception('Exception %s loading log')
            self.loggerdeco.debug(40 * '-')
        # Reset the tests' volatile members in order to prevent them
        # from being reused after a test has completed.
        self.test_result = PhoneTestResult()
        self.message = None
        self.job_guid = None
        self.job_details = []
        self.submit_timestamp = None
        self.start_timestamp = None
        self.end_timestamp = None
        self.upload_dir = None
        self.start_time = None
        self.stop_time = None
        self._log = None
        self.logcat = Logcat(self)
        if self.loggerdeco_original:
            self.loggerdeco = self.loggerdeco_original
        if self.dm_logger_original:
            self.dm._logger = self.dm_logger_original

    def update_status(self, phone_status=None, message=None):
        if self.update_status_cb:
            self.update_status_cb(build=self.build,
                                  phone_status=phone_status,
                                  message=message)

    def install_profile(self, profile=None, root=True):
        if not profile:
            profile = FirefoxProfile()

        profile_path_parent = os.path.split(self.profile_path)[0]
        success = False
        for attempt in range(1, self.options.phone_retry_limit+1):
            try:
                self.loggerdeco.debug('Attempt %d installing profile' % attempt)
                self.dm.rm(self.profile_path, recursive=True,
                           force=True, root=root)
                self.dm.chmod(profile_path_parent, root=root)
                self.dm.mkdir(self.profile_path, root=root)
                self.dm.chmod(self.profile_path, root=root)
                self.dm.push(profile.profile, self.profile_path)
                self.dm.chmod(self.profile_path, recursive=True, root=root)
                success = True
                break
            except ADBError:
                self.loggerdeco.exception('Attempt %d Exception installing '
                                          'profile to %s' % (
                                              attempt, self.profile_path))
                sleep(self.options.phone_retry_wait)

        if not success:
            self.loggerdeco.error('Failure installing profile to %s' % self.profile_path)

        return success

    def run_fennec_with_profile(self, appname, url, extra_args=[]):
        self.loggerdeco.debug('run_fennec_with_profile: %s %s %s' %
                              (appname, url, extra_args))
        local_extra_args = ['-profile', self.profile_path]
        local_extra_args.extend(extra_args)

        try:
            self.dm.pkill(appname, root=True)
            self.dm.launch_fennec(appname,
                                 intent="android.intent.action.VIEW",
                                 moz_env=self.environment,
                                 extra_args=local_extra_args,
                                 url=url,
                                 wait=False,
                                 fail_if_running=False)
        except:
            self.loggerdeco.exception('run_fennec_with_profile: Exception:')
            raise

    def remove_sessionstore_files(self, root=True):
        self.dm.rm(self.profile_path + '/sessionstore.js',
                   force=True,
                   root=root)
        self.dm.rm(self.profile_path + '/sessionstore.bak',
                   force=True,
                   root=root)

    @property
    def fennec_crashed(self, root=True):
        """
        Perform a quick check for crashes by checking
        self.profile_path/minidumps for dump files.

        """
        if self.dm.exists(os.path.join(self.profile_path, 'minidumps', '*.dmp'),
                          root=root):
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
        self.status = PhoneTestResult.SUCCESS
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

    def add_failure(self, testpath, test_status, text, testresult_status):
        if testresult_status:
            self.status = testresult_status
        self.failures.append({
            "test": testpath,
            "status": test_status,
            "text": text})
