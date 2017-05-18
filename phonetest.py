# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import ConfigParser
import datetime
import glob
import os
import posixpath
import urlparse
import re
import sys
import shutil
import tempfile

from time import sleep

from mozprofile import Profile

import utils
from autophonecrash import AutophoneCrashProcessor
from adb import ADBError
from logdecorator import LogDecorator
from phonestatus import PhoneStatus, TreeherderStatus, TestStatus

# Define the Adobe Flash Player package name as a constant for reuse.
FLASH_PACKAGE = 'com.adobe.flashplayer'

class PhoneTest(object):

    # Use instances keyed on phoneid+':'config_file+':'+str(chunk)
    # to lookup tests.

    instances = {}
    has_run_if_changed = False

    @classmethod
    def lookup(cls, phoneid, config_file, chunk):
        key = '%s:%s:%s' % (phoneid, config_file, chunk)
        if key in PhoneTest.instances:
            return PhoneTest.instances[key]
        return None

    @classmethod
    def match(cls, tests=None, test_name=None, phoneid=None,
              config_file=None, job_guid=None,
              repo=None, platform=None, build_type=None, build_abi=None, build_sdk=None,
              changeset_dirs=None):

        logger = utils.getLogger()
        logger.debug('PhoneTest.match(tests: %s, test_name: %s, phoneid: %s, '
                     'config_file: %s, job_guid: %s, '
                     'repo: %s, platform: %s, build_type: %s, '
                     'abi: %s, build_sdk: %s',
                     tests, test_name, phoneid,
                     config_file, job_guid,
                     repo, platform, build_type, build_abi, build_sdk)
        matches = []
        if not tests:
            tests = [PhoneTest.instances[key] for key in PhoneTest.instances.keys()]

        for test in tests:
            # If changeset_dirs is empty, we will run the tests anyway.
            # This is safer in terms of catching regressions and extra tests
            # being run are more likely to be noticed and fixed than tests
            # not being run that should have been.
            if hasattr(test, 'run_if_changed') and test.run_if_changed and changeset_dirs:
                matched = False
                for cd in changeset_dirs:
                    if matched:
                        break
                    for td in test.run_if_changed:
                        if cd == "" or cd.startswith(td):
                            logger.debug('PhoneTest.match: test %s dir %s '
                                         'matched changeset_dirs %s', test, td, cd)
                            matched = True
                            break
                if not matched:
                    continue

            if test_name and test_name != test.name and \
               "%s%s" % (test_name, test.name_suffix) != test.name:
                continue

            if phoneid and phoneid != test.phone.id:
                continue

            if config_file and config_file != test.config_file:
                continue

            if job_guid and job_guid != test.job_guid:
                continue

            if repo and test.repos and repo not in test.repos:
                continue

            if build_type and build_type not in test.buildtypes:
                continue

            if platform and platform not in test.platforms:
                continue

            if build_abi and build_abi not in test.phone.abi:
                # phone.abi may be of the form armeabi-v7a, arm64-v8a
                # or some form of x86. Test for inclusion rather than
                # exact matches to cover the possibilities.
                continue

            if build_sdk and build_sdk not in test.phone.supported_sdks:
                # We have extended build_sdk and the
                # phone.supported_sdks to be a comma-delimited list of
                # sdk values for phones whose minimum support has
                # changed as the builds have changed.
                sdk_found = False
                for sdk in build_sdk.split(','):
                    if sdk in test.phone.supported_sdks:
                        sdk_found = True
                        break
                if not sdk_found:
                    continue

            matches.append(test)

        logger.debug('PhoneTest.match = %s', matches)

        return matches

    def __init__(self, dm=None, phone=None, options=None, config_file=None, chunk=1, repos=[]):
        # The PhoneTest constructor may raise exceptions due to issues with
        # the device. Creators of PhoneTest objects are responsible
        # for catching these exceptions and cleaning up any previously
        # created tests for the device.
        #
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

        logger = utils.getLogger(name=self.phone.id)
        self.loggerdeco = LogDecorator(logger,
                                       {},
                                       '%(message)s')
        self.loggerdeco_original = None
        self.dm_logger_original = None
        # Test result
        self.status = TreeherderStatus.SUCCESS
        self.passed = 0
        self.failed = 0
        self.todo = 0

        self.repos = repos
        self.unittest_logpath = None
        # Treeherder related items.
        self._job_name = None
        self._job_symbol = None
        self._group_name = None
        self._group_symbol = None
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

        # base_device_path accesses the device to determine the
        # appropriate path and can therefore fail and raise an
        # exception which will not be caught in the PhoneTest
        # constructor. Creators of PhoneTest objects are responsible
        # for catching these exceptions and cleaning up any previously
        # created tests for the device.
        self._base_device_path = ''
        self.profile_path = '/data/local/tests/autophone/profile'
        if self.dm:
            self.profile_path = '%s/profile' % self.base_device_path
        self.autophone_directory = os.path.dirname(os.path.abspath(sys.argv[0]))
        self._paths = {}
        try:
            sources = self.cfg.get('paths', 'sources').split()
            self._paths['sources'] = []
            for source in sources:
                if not source.endswith('/'):
                    source += '/'
                self._paths['sources'].append(source)
        except (ConfigParser.NoSectionError, ConfigParser.NoOptionError):
            self._paths['sources'] = ['files/base/']
        try:
            self._paths['dest'] = self.cfg.get('paths', 'dest')
            if not self._paths['dest'].endswith('/'):
                self._paths['dest'] += '/'
        except (ConfigParser.NoSectionError, ConfigParser.NoOptionError):
            self._paths['dest'] = os.path.join(self.base_device_path,
                                               self.__class__.__name__)
        try:
            self._paths['profile'] = self.cfg.get('paths', 'profile')
            if not self._paths['profile'].endswith('/'):
                self._paths['profile'] += '/'
        except (ConfigParser.NoSectionError, ConfigParser.NoOptionError):
            pass
        self.run_if_changed = set()
        try:
            dirs = self.cfg.get('runtests', 'run_if_changed')
            self.run_if_changed = set([d.strip() for d in dirs.split(',')])
            if self.run_if_changed:
                PhoneTest.has_run_if_changed = True
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
                push_file_name = os.path.basename(push)
                push_dest = posixpath.join(self._paths['dest'],
                                           source,
                                           push_file_name)
                self._pushes[push] = push_dest
                if push_file_name == 'initialize_profile.html':
                    self._initialize_url = 'file://' + push_dest
        # [tests]
        self._tests = {}
        try:
            for t in self.cfg.items('tests'):
                self._tests[t[0]] = t[1]
        except ConfigParser.NoSectionError:
            self._tests['blank'] = 'blank.html'

        # [builds]
        self.buildtypes = []
        try:
            self.buildtypes = self.cfg.get('builds', 'buildtypes').split(' ')
        except (ConfigParser.NoSectionError, ConfigParser.NoOptionError):
            self.buildtypes = list(self.options.buildtypes)

        self.platforms = []
        try:
            self.platforms = self.cfg.get('builds', 'platforms').split(' ')
        except (ConfigParser.NoSectionError, ConfigParser.NoOptionError):
            self.platforms = self.options.platforms

        self.loggerdeco.info('PhoneTest: %s', self.__dict__)

    def __str__(self):
        return '%s(%s, config_file=%s, chunk=%s, buildtypes=%s)' % (
            type(self).__name__,
            self.phone,
            self.config_file,
            self.chunk,
            self.buildtypes)

    def __repr__(self):
        return self.__str__()

    def _add_instance(self, phoneid, config_file, chunk):
        key = '%s:%s:%s' % (phoneid, config_file, chunk)
        assert key not in PhoneTest.instances, 'Duplicate PhoneTest %s' % key
        PhoneTest.instances[key] = self

    def remove(self):
        key = '%s:%s:%s' % (self.phone.id, self.config_file, self.chunk)
        if key in PhoneTest.instances:
            had_run_if_changed = hasattr(PhoneTest.instances[key], 'run_if_changed') and \
                                 PhoneTest.instances[key].run_if_changed
            del PhoneTest.instances[key]
            if had_run_if_changed:
                PhoneTest.has_run_if_changed = False
                for key in PhoneTest.instances.keys():
                    if PhoneTest.instances[key].run_if_changed:
                        PhoneTest.has_run_if_changed = True
                        break

    def set_worker_subprocess(self, worker_subprocess):
        logger = utils.getLogger()
        self.loggerdeco = LogDecorator(logger,
                                       {},
                                       '%(message)s')

        self.loggerdeco_original = None
        self.dm_logger_original = None
        self.worker_subprocess = worker_subprocess
        self.dm = worker_subprocess.dm
        self.update_status_cb = worker_subprocess.update_status

    @property
    def preferences(self):
        # https://dxr.mozilla.org/mozilla-central/source/mobile/android/app/mobile.js
        # https://dxr.mozilla.org/mozilla-central/source/browser/app/profile/firefox.js
        # https://dxr.mozilla.org/mozilla-central/source/addon-sdk/source/test/preferences/no-connections.json
        # https://dxr.mozilla.org/mozilla-central/source/testing/profiles/prefs_general.js
        # https://dxr.mozilla.org/mozilla-central/source/testing/mozbase/mozprofile/mozprofile/profile.py

        if not self._preferences:
            self._preferences = {
                'app.support.baseURL': 'http://localhost/support-dummy/',
                'app.update.auto': False,
                'app.update.certs.1.commonName': '',
                'app.update.certs.2.commonName': '',
                'app.update.enabled': False,
                'app.update.staging.enabled': False,
                'app.update.url': 'http://localhost/app-dummy/update',
                'app.update.url.android': 'http://localhost/app-dummy/update',
                'beacon.enabled': False,
                'browser.EULA.override': True,
                'browser.aboutHomeSnippets.updateUrl': '',
                'browser.contentHandlers.types.0.uri': 'http://localhost/rss?url=%%s',
                'browser.contentHandlers.types.1.uri': 'http://localhost/rss?url=%%s',
                'browser.contentHandlers.types.2.uri': 'http://localhost/rss?url=%%s',
                'browser.contentHandlers.types.3.uri': 'http://localhost/rss?url=%%s',
                'browser.contentHandlers.types.4.uri': 'http://localhost/rss?url=%%s',
                'browser.contentHandlers.types.5.uri': 'http://localhost/rss?url=%%s',
                'browser.firstrun.show.localepicker': False,
                'browser.firstrun.show.uidiscovery': False,
                'browser.newtab.url': '',
                'browser.newtabpage.directory.ping': '',
                'browser.newtabpage.directory.source': 'data:application/json,{"dummy":1}',
                'browser.newtabpage.remote': False,
                'browser.push.warning.infoURL': 'http://localhost/alerts-dummy/infoURL',
                'browser.push.warning.migrationURL': 'http://localhost/alerts-dummy/migrationURL',
                'browser.safebrowsing.appRepURL': '',
                'browser.safebrowsing.downloads.enabled': False,
                'browser.safebrowsing.downloads.remote.enabled': False,
                'browser.safebrowsing.downloads.remote.url': 'http://localhost/safebrowsing-dummy/update',
                'browser.safebrowsing.enabled': False,
                'browser.safebrowsing.gethashURL': '',
                'browser.safebrowsing.malware.enabled': False,
                'browser.safebrowsing.malware.reportURL': '',
                'browser.safebrowsing.provider.google.appRepURL': 'http://localhost/safebrowsing-dummy/update',
                'browser.safebrowsing.provider.google.gethashURL': 'http://localhost/safebrowsing-dummy/gethash',
                'browser.safebrowsing.provider.google.updateURL': 'http://localhost/safebrowsing-dummy/update',
                'browser.safebrowsing.provider.mozilla.gethashURL': 'http://localhost/safebrowsing-dummy/gethash',
                'browser.safebrowsing.provider.mozilla.updateURL': 'http://localhost/safebrowsing-dummy/update',
                'browser.safebrowsing.updateURL': 'http://localhost/safebrowsing-dummy/update',
                'browser.search.countryCode': 'US',
                'browser.search.geoSpecificDefaults': False,
                'browser.search.geoip.url': '',
                'browser.search.isUS': True,
                'browser.search.suggest.enabled': False,
                'browser.search.update': False,
                'browser.selfsupport.url': 'https://localhost/selfsupport-dummy/',
                'browser.sessionstore.resume_from_crash': False,
                'browser.shell.checkDefaultBrowser': False,
                'browser.snippets.enabled': False,
                'browser.snippets.firstrunHomepage.enabled': False,
                'browser.snippets.syncPromo.enabled': False,
                'browser.snippets.updateUrl': '',
                'browser.tabs.warnOnClose': False,
                'browser.tiles.reportURL': 'http://localhost/tests/robocop/robocop_tiles.sjs',
                'browser.trackingprotection.gethashURL': '',
                'browser.trackingprotection.updateURL': '',
                'browser.translation.bing.authURL': 'http://localhost/browser/browser/components/translation/test/bing.sjs',
                'browser.translation.bing.translateArrayURL': 'http://localhost/browser/browser/components/translation/test/bing.sjs',
                'browser.translation.yandex.translateURLOverride': 'http://localhost/browser/browser/components/translation/test/yandex.sjs',
                'browser.uitour.pinnedTabUrl': 'http://localhost/uitour-dummy/pinnedTab',
                'browser.uitour.url': 'http://localhost/uitour-dummy/tour',
                'browser.urlbar.suggest.searches': False,
                'browser.urlbar.userMadeSearchSuggestionsChoice': True,
                'browser.warnOnQuit': False,
                'browser.webapps.apkFactoryUrl': '',
                'browser.webapps.checkForUpdates': 0,
                'browser.webapps.updateCheckUrl': '',
                'datareporting.healthreport.about.reportUrl': 'http://localhost/abouthealthreport/',
                'datareporting.healthreport.about.reportUrlUnified': 'http://localhost/abouthealthreport/v4/',
                'datareporting.healthreport.documentServerURI': 'http://localhost/healthreport/',
                'datareporting.healthreport.service.enabled': False,
                'datareporting.healthreport.uploadEnabled': False,
                'datareporting.policy.dataSubmissionEnabled': False,
                'datareporting.policy.dataSubmissionPolicyBypassAcceptance': True,
                'dom.ipc.plugins.flash.subprocess.crashreporter.enabled': False,
                'experiments.manifest.uri': 'http://localhost/experiments-dummy/manifest',
                'extensions.autoDisableScopes': 0, # By default don't disable add-ons from any scope
                'extensions.blocklist.enabled': False,
                'extensions.blocklist.interval': 172800,
                'extensions.blocklist.url': 'http://localhost/extensions-dummy/blocklistURL',
                'extensions.enabledScopes': 5,     # By default only load extensions all scopes except temporary.
                'extensions.getAddons.cache.enabled': False,
                'extensions.getAddons.get.url': 'http://localhost/extensions-dummy/repositoryGetURL',
                'extensions.getAddons.getWithPerformance.url': 'http://localhost/extensions-dummy/repositoryGetWithPerformanceURL',
                'extensions.getAddons.search.browseURL': 'http://localhost/extensions-dummy/repositoryBrowseURL',
                'extensions.getAddons.search.url': 'http://localhost/extensions-dummy/repositorySearchURL',
                'extensions.hotfix.url': 'http://localhost/extensions-dummy/hotfixURL',
                'extensions.installDistroAddons': False,
                'extensions.showMismatchUI': False,
                'extensions.startupScanScopes': 5, # And scan for changes at startup
                'extensions.systemAddon.update.url': 'data:application/xml,<updates></updates>',
                'extensions.update.autoUpdateDefault': False,
                'extensions.update.background.url': 'http://localhost/extensions-dummy/updateBackgroundURL',
                'extensions.update.enabled': False,
                'extensions.update.interval': 172800,
                'extensions.update.notifyUser': False,
                'extensions.update.url': 'http://localhost/extensions-dummy/updateURL',
                'extensions.webservice.discoverURL': 'http://localhost/extensions-dummy/discoveryURL',
                'general.useragent.updates.enabled': False,
                'geo.provider.testing': True,
                'geo.wifi.scan': False,
                'geo.wifi.uri': 'http://localhost/tests/dom/tests/mochitest/geolocation/network_geolocation.sjs',
                'identity.fxaccounts.auth.uri': 'https://localhost/fxa-dummy/',
                'identity.fxaccounts.remote.force_auth.uri': 'https://localhost/fxa-force-auth',
                'identity.fxaccounts.remote.signin.uri': 'https://localhost/fxa-signin',
                'identity.fxaccounts.remote.signup.uri': 'https://localhost/fxa-signup',
                'identity.fxaccounts.remote.webchannel.uri': 'https://localhost/',
                'identity.fxaccounts.settings.uri': 'https://localhost/fxa-settings',
                'identity.fxaccounts.skipDeviceRegistration': True,
                'media.autoplay.enabled': True,
                'media.gmp-gmpopenh264.autoupdate': False,
                'media.gmp-manager.cert.checkAttributes': False,
                'media.gmp-manager.cert.requireBuiltIn': False,
                'media.gmp-manager.certs.1.commonName': '',
                'media.gmp-manager.certs.2.commonName': '',
                'media.gmp-manager.secondsBetweenChecks': 172800,
                'media.gmp-manager.url': 'http://localhost/media-dummy/gmpmanager',
                'media.gmp-manager.url.override': 'data:application/xml,<updates></updates>',
                'plugin.state.flash': 2,
                'plugins.update.url': 'http://localhost/plugins-dummy/updateCheckURL',
                'privacy.trackingprotection.introURL': 'http://localhost/trackingprotection/tour',
                'security.notification_enable_delay': 0,
                'security.ssl.errorReporting.url': 'https://localhost/browser/browser/base/content/test/general/pinning_reports.sjs?succeed',
                'shell.checkDefaultClient': False,
                'toolkit.startup.max_resumed_crashes': -1,
                'toolkit.telemetry.cachedClientID': 'dddddddd-dddd-dddd-dddd-dddddddddddd', # https://dxr.mozilla.org/mozilla-central/source/toolkit/modules/ClientID.jsm#40
                'toolkit.telemetry.enabled': False,
                'toolkit.telemetry.notifiedOptOut': 999,
                'toolkit.telemetry.prompted': 999,
                'toolkit.telemetry.rejected': True,
                'toolkit.telemetry.server': 'https://localhost/telemetry-dummy/',
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
                    elif re.match(r'\d+$', value):
                        value = int(value)
                    self._preferences[name] = value
        return self._preferences

    @property
    def environment(self):
        if not self._environment:
            # https://developer.mozilla.org/en-US/docs/Environment_variables_affecting_crash_reporting
            self._environment = {
                'MOZ_CRASHREPORTER': '1',
                'MOZ_CRASHREPORTER_NO_REPORT': '1',
                'MOZ_CRASHREPORTER_SHUTDOWN': '1',
                'MOZ_DISABLE_NONLOCAL_CONNECTIONS': '1',
                'MOZ_IN_AUTOMATION': '1',
                'NO_EM_RESTART': '1',
                'MOZ_DISABLE_SWITCHBOARD': '1',
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
                    elif re.match(r'\d+$', value):
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
        for attempt in range(1, self.options.phone_retry_limit+1):
            try:
                self._base_device_path = self.dm.test_root + '/autophone'
                if not self.dm.is_dir(self._base_device_path, root=True):
                    self.loggerdeco.debug('Attempt %d creating base device path %s',
                                          attempt, self._base_device_path)
                    self.dm.mkdir(self._base_device_path, parents=True, root=True)
                    self.dm.chmod(self._base_device_path, recursive=True, root=True)
                success = True
                break
            except ADBError:
                self.loggerdeco.exception('Attempt %d creating base device '
                                          'path %s',
                                          attempt, self._base_device_path)
                sleep(self.options.phone_retry_wait)

        if not success:
            raise Exception('Failed to determine base_device_path')

        self.loggerdeco.debug('base_device_path is %s', self._base_device_path)

        return self._base_device_path

    @property
    def job_url(self):
        if not self.options.treeherder_url:
            return None
        job_url = ('%s/#/jobs?filter-searchStr=autophone&' +
                   'exclusion_profile=false&' +
                   'filter-tier=1&filter-tier=2&filter-tier=3&' +
                   'repo=%s&revision=%s')
        return job_url % (self.options.treeherder_url,
                          self.build.tree,
                          self.build.revision[:12])

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

    def handle_test_interrupt(self, reason, test_result):
        self.add_failure(self.name, TestStatus.TEST_UNEXPECTED_FAIL, reason, test_result)

    def add_pass(self, testpath):
        testpath = _normalize_testpath(testpath)
        self.passed += 1
        self.loggerdeco.info(' %s | %s | Ok.', TestStatus.TEST_PASS, testpath)

    def add_failure(self, testpath, test_status, text, testresult_status):
        """Report a test failure.

        :param testpath: A string identifying the test.
        :param test_status: A string identifying the type of test failure.
        :param text: A string decribing the failure.
        :param testresult_status: Test status to be reported to Treeherder.
            One of PhoneTest.{BUSTED,EXCEPTION,TESTFAILED,UNKNOWN,USERCANCEL}.
        """
        self.message = text
        self.update_status(message=text)
        testpath = _normalize_testpath(testpath)
        self.status = testresult_status
        self.failed += 1
        self.loggerdeco.info(' %s | %s | %s', test_status, testpath, text)

    def reset_result(self):
        self.status = TreeherderStatus.SUCCESS
        self.passed = 0
        self.failed = 0
        self.todo = 0

    def handle_crashes(self):
        """Detect if any crash dump files have been generated, process them and
        produce Treeherder compatible error messages, then clean up the dump
        files before returning True if a crash was found or False if there were
        no crashes detected.
        """
        if not self.crash_processor:
            return False

        errors = self.crash_processor.get_errors(self.build.symbols,
                                                 self.options.minidump_stackwalk,
                                                 clean=True)

        for error in errors:
            if error['reason'] == 'java-exception':
                self.add_failure(
                    self.name, TestStatus.PROCESS_CRASH,
                    error['signature'],
                    TreeherderStatus.TESTFAILED)
            elif error['reason'] == TestStatus.TEST_UNEXPECTED_FAIL:
                self.add_failure(
                    self.name,
                    error['reason'],
                    error['signature'],
                    TreeherderStatus.TESTFAILED)
            elif error['reason'] == TestStatus.PROCESS_CRASH:
                self.add_failure(
                    self.name,
                    error['reason'],
                    'application crashed [%s]' % error['signature'],
                    TreeherderStatus.TESTFAILED)
                self.loggerdeco.info(error['signature'])
                self.loggerdeco.info(error['stackwalk_output'])
                self.loggerdeco.info(error['stackwalk_errors'])
            else:
                self.loggerdeco.warning('Unknown error reason: %s', error['reason'])

        return len(errors) > 0

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
        profile = Profile(preferences=prefs, addons=addons)
        if not self.install_profile(profile):
            return False

        success = False
        for attempt in range(1, self.options.phone_retry_limit+1):
            self.loggerdeco.debug('Attempt %d Initializing profile', attempt)
            self.run_fennec_with_profile(self.build.app_name, self._initialize_url)

            if self.wait_for_fennec():
                success = True
                break
            sleep(self.options.phone_retry_wait)

        if not success or self.handle_crashes():
            self.add_failure(self.name, TestStatus.TEST_UNEXPECTED_FAIL,
                             'Failure initializing profile',
                             TreeherderStatus.TESTFAILED)

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
        max_killattempts = 3
        for kill_attempt in range(1, max_killattempts+1):
            try:
                self.loggerdeco.info('killing %s' % self.build.app_name)
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
            self.loggerdeco.debug('Attempt %d Installing local pages', attempt)
            try:
                self.dm.rm(self._paths['dest'], recursive=True, force=True, root=True)
                self.dm.mkdir(self._paths['dest'], parents=True, root=True)
                for push_source in self._pushes:
                    push_dest = self._pushes[push_source]
                    if os.path.isdir(push_source):
                        self.dm.push(push_source, push_dest)
                    else:
                        self.dm.push(push_source, push_dest)
                self.dm.chmod(self._paths['dest'], recursive=True, root=True)
                success = True
                break
            except ADBError:
                self.loggerdeco.exception('Attempt %d Installing local pages', attempt)
                sleep(self.options.phone_retry_wait)

        if not success:
            self.add_failure(self.name, TestStatus.TEST_UNEXPECTED_FAIL,
                             'Failure installing local pages',
                             TreeherderStatus.TESTFAILED)

        return success

    def is_fennec_running(self, appname):
        for attempt in range(1, self.options.phone_retry_limit+1):
            try:
                return self.dm.process_exist(appname)
            except ADBError:
                self.loggerdeco.exception('Attempt %d is fennec running', attempt)
                if attempt == self.options.phone_retry_limit:
                    raise
                sleep(self.options.phone_retry_wait)

    def setup_job(self):
        # Log the current full contents of logcat, then clear the
        # logcat buffers to help prevent the device's buffer from
        # over flowing during the test.
        self.worker_subprocess.log_step('Setup Test')
        self.start_time = datetime.datetime.utcnow()
        self.stop_time = self.start_time
        # Clear the Treeherder job details.
        self.job_details = []
        try:
            self.worker_subprocess.logcat.reset()
        except:
            self.loggerdeco.exception('Exception resetting logcat before test')
        self.worker_subprocess.treeherder.submit_running(
            self.phone.id,
            self.build.url,
            self.build.tree,
            self.build.revision,
            self.build.type,
            self.build.abi,
            self.build.platform,
            self.build.sdk,
            self.build.builder_type,
            tests=[self])

        self.loggerdeco_original = self.loggerdeco
        # self.dm._logger can raise ADBTimeoutError due to the
        # property dm therefore place it after the initialization.
        self.dm_logger_original = self.dm._logger
        logger = utils.getLogger()
        self.loggerdeco = LogDecorator(logger,
                                       {
                                           'repo': self.build.tree,
                                           'buildid': self.build.id,
                                           'buildtype': self.build.type,
                                           'sdk': self.phone.sdk,
                                           'platform': self.build.platform,
                                           'testname': self.name
                                       },
                                       'PhoneTestJob %(repo)s %(buildid)s %(buildtype)s %(sdk)s %(platform)s %(testname)s '
                                       '%(message)s')
        self.dm._logger = self.loggerdeco
        self.loggerdeco.info('PhoneTest starting job')
        if self.unittest_logpath:
            os.unlink(self.unittest_logpath)
        self.unittest_logpath = None
        self.upload_dir = tempfile.mkdtemp()
        self.crash_processor = AutophoneCrashProcessor(self.dm,
                                                       self.profile_path,
                                                       self.upload_dir,
                                                       self.build.app_name)
        self.crash_processor.clear()
        self.reset_result()
        if not self.worker_subprocess.is_disabled():
            self.update_status(phone_status=PhoneStatus.WORKING,
                               message='Setting up %s' % self.name)

    def run_job(self):
        raise NotImplementedError

    def teardown_job(self):
        self.loggerdeco.debug('PhoneTest.teardown_job')
        self.stop_time = datetime.datetime.utcnow()
        if self.stop_time and self.start_time:
            self.loggerdeco.info('Test %s elapsed time: %s',
                                 self.name, self.stop_time - self.start_time)
        try:
            if self.worker_subprocess.is_ok():
                # Do not attempt to process crashes if the device is
                # in an error state.
                self.handle_crashes()
        except Exception, e:
            self.loggerdeco.exception('Exception during crash processing')
            self.add_failure(
                self.name, TestStatus.TEST_UNEXPECTED_FAIL,
                'Exception %s during crash processing' % e,
                TreeherderStatus.EXCEPTION)
        if self.unittest_logpath and os.path.exists(self.unittest_logpath):
            self.worker_subprocess.log_step('Unittest Log')
            try:
                logfilehandle = open(self.unittest_logpath)
                for logline in logfilehandle.read().splitlines():
                    self.loggerdeco.info(logline)
                logfilehandle.close()
            except:
                self.loggerdeco.exception('Exception loading log %s', self.unittest_log)
            finally:
                os.unlink(self.unittest_logpath)
                self.unittest_logpath = None
        # Unit tests may include the logcat output already but not all of them do.
        self.worker_subprocess.log_step('Logcat')
        try:
            for logcat_line in self.worker_subprocess.logcat.get(full=True):
                self.loggerdeco.info("logcat: %s", logcat_line)
        except:
            self.loggerdeco.exception('Exception getting logcat')
        try:
            if self.worker_subprocess.is_disabled() and self.status != TreeherderStatus.USERCANCEL:
                # The worker was disabled while running one test of a job.
                # Record the cancellation on any remaining tests in that job.
                self.add_failure(
                    self.name, TestStatus.TEST_UNEXPECTED_FAIL,
                    'The worker was disabled.',
                    TreeherderStatus.USERCANCEL)
            self.loggerdeco.info('PhoneTest stopping job')
            self.worker_subprocess.flush_log()
            self.worker_subprocess.treeherder.submit_complete(
                self.phone.id,
                self.build.url,
                self.build.tree,
                self.build.revision,
                self.build.type,
                self.build.abi,
                self.build.platform,
                self.build.sdk,
                self.build.builder_type,
                tests=[self])
        except:
            self.loggerdeco.exception('Exception tearing down job')
        finally:
            if self.upload_dir and os.path.exists(self.upload_dir):
                shutil.rmtree(self.upload_dir)
            self.upload_dir = None

        # Reset the tests' volatile members in order to prevent them
        # from being reused after a test has completed.
        self.message = None
        self.job_guid = None
        self.job_details = []
        self.submit_timestamp = None
        self.start_timestamp = None
        self.end_timestamp = None
        self.upload_dir = None
        self.start_time = None
        self.stop_time = None
        self.unittest_logpath = None
        # Reset the logcat buffers to help prevent the device's buffer
        # from over flowing after the test.
        self.worker_subprocess.logcat.reset()
        if self.loggerdeco_original:
            self.loggerdeco = self.loggerdeco_original
            self.loggerdeco_original = None
        if self.dm_logger_original:
            self.dm._logger = self.dm_logger_original
            self.dm_logger_original = None
        self.reset_result()

    def update_status(self, phone_status=None, message=None):
        if self.update_status_cb:
            self.update_status_cb(build=self.build,
                                  phone_status=phone_status,
                                  message=message)

    def install_profile(self, profile=None, root=True):
        if not profile:
            profile = Profile()

        profile_path_parent = os.path.split(self.profile_path)[0]
        success = False
        for attempt in range(1, self.options.phone_retry_limit+1):
            try:
                self.loggerdeco.debug('Attempt %d installing profile', attempt)
                if self.dm.exists(self.profile_path, root=root):
                    # If the profile already exists, chmod it to make sure
                    # we have permission to delete it.
                    self.dm.chmod(self.profile_path, recursive=True, root=root)
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
            self.add_failure(self.name, TestStatus.TEST_UNEXPECTED_FAIL,
                             'Failure installing profile to %s' % self.profile_path,
                             TreeherderStatus.TESTFAILED)

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


def _normalize_testpath(testpath):
    parts = urlparse.urlparse(testpath)
    if testpath.startswith('http'):
        testpath = testpath.replace(parts.netloc, 'remote')
    return testpath
