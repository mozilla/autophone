# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import ConfigParser
import datetime
import os
import re
import sys
import tempfile
from time import sleep

from mozprofile import FirefoxProfile

from adb import ADBError
from perftest import PerfTest
from phonetest import PhoneTestResult

class WebappStartupTest(PerfTest):
    def __init__(self, dm=None, phone=None, options=None,
                 config_file=None, chunk=1, repos=[]):
        PerfTest.__init__(self, dm=dm, phone=phone, options=options,
                          config_file=config_file, chunk=chunk, repos=repos)
        self.webappstartup_name = None
        # Enable the consoleservice for logcat in release builds.
        self.preferences["consoleservice.logcat"] = True

    @property
    def name(self):
        return 'autophone-webapp%s' % self.name_suffix

    @property
    def phonedash_url(self):
        # For webappstartup, Phonedash records the test name as
        # webappstartup.
        return self._phonedash_url('webappstartup')

    def setup_job(self):
        PerfTest.setup_job(self)
        # [paths]
        autophone_directory = os.path.dirname(os.path.abspath(sys.argv[0]))
        self._paths = {}
        self._paths['webappstartup_apk'] = os.path.join(autophone_directory,
                                                        'tests/webapp-startup-test.apk')
        # Try to get the webappstartup package name if it is already installed.
        self.get_webappstartup_name()
        # Perform an initial install of the webapp to confirm it
        # can be installed.
        if not self.install_webappstartup():
            raise Exception('Unable to install %s' % self._paths['webappstartup_apk'])

    def teardown_job(self):
        # We must tear down the PerfTest before uninstalling
        # webappstartup, because we need the profile and minidumps
        # directory for crash processing.
        PerfTest.teardown_job(self)
        if self.webappstartup_name:
            try:
                # Make certain to catch any exceptions in order
                # to ensure that the test teardown completes.
                self.dm.uninstall_app(self.webappstartup_name)
            except:
                self.loggerdeco.exception('Exception uninstalling %s' %
                                          self.webappstartup_name)

    def create_profile(self, custom_prefs=None, root=True):
        # Create, install and initialize the profile to be
        # used in the test.

        self.loggerdeco.debug('create_profile: custom_prefs: %s' % custom_prefs)

        # Start without Fennec or the webapp running.
        self.kill_webappstartup()

        # Pull the profiles.ini file when it first exists so we can
        # determine the profile path.
        app_dir = '/data/data/%s' % self.build.app_name
        remote_profiles_ini_path = '%s/files/mozilla/profiles.ini' % app_dir
        # Do not use recursive chmod in order not to have to process
        # any unnecessary files or directories.
        path = app_dir
        for path_component in ('', 'files', 'mozilla'):
            path = os.path.join(path, path_component)
            self.dm.mkdir(path, parents=True, root=root)
            self.dm.chmod(path, root=root)

        # Run webappstartup once to create the initial profile.
        # Pull the ini file and parse it to get the profile path.
        self.run_webappstartup()
        max_attempts = 60
        for attempt in range(1, max_attempts):
            self.loggerdeco.debug('create_profile: '
                                  'Attempt %d checking for %s' %
                                  (attempt, remote_profiles_ini_path))
            if self.dm.is_file(remote_profiles_ini_path, root=root):
                break
            sleep(1)

        local_profiles_ini_file = tempfile.NamedTemporaryFile(
            suffix='.ini',
            prefix='profile',
            delete=False)
        local_profiles_ini_file.close()

        self.profile_path = None
        attempt = 0
        while attempt < max_attempts and not self.profile_path:
            attempt += 1
            sleep(1)
            self.loggerdeco.debug('create_profile: '
                                  'Attempt %d parsing %s' %
                                  (attempt, remote_profiles_ini_path))
            self.dm.pull(remote_profiles_ini_path, local_profiles_ini_file.name)

            cfg = ConfigParser.RawConfigParser()
            if not cfg.read(local_profiles_ini_file.name):
                continue
            for section in cfg.sections():
                if not section.startswith('Profile'):
                    continue
                if cfg.get(section, 'Name') != 'webapp0':
                    continue
                try:
                    remote_profile_path = cfg.get(section, 'Path')
                    if cfg.get(section, 'IsRelative') == '1':
                        self.profile_path = '%s/files/mozilla/%s' % (
                            app_dir, remote_profile_path)
                    else:
                        self.profile_path = remote_profile_path
                    break
                except ConfigParser.NoOptionError:
                    pass

        os.unlink(local_profiles_ini_file.name)

        self.loggerdeco.info('create_profile: profile_path: %s' %
                             self.profile_path)
        if not self.profile_path:
            self.kill_webappstartup()
            return False

        # Sleep for 10 seconds to allow initialization to complete.
        # Other approaches such as checking for changing directory
        # contents do not work. 5 seconds is too short.
        sleep(10)
        self.kill_webappstartup()

        # Need to update the crash processor's profile path since it
        # changes each time.
        self.crash_processor.remote_profile_dir = self.profile_path

        if isinstance(custom_prefs, dict):
            prefs = dict(self.preferences.items() + custom_prefs.items())
        else:
            prefs = self.preferences
        profile = FirefoxProfile(preferences=prefs, addons='%s/xpi/quitter.xpi' %
                                 os.getcwd())
        if not self.install_profile(profile):
            return False

        return True

    def run_job(self):

        self.testname = 'webappstartup'

        self.loggerdeco.info('Running test for %d iterations' %
                             self._iterations)

        # success == False indicates that none of the attempts
        # were successful in getting any measurement. This is
        # typically due to a regression in the brower which should
        # be reported.
        success = False
        command = None
        is_test_completed = True
        for attempt in range(1, self.stderrp_attempts+1):
            # dataset is a list of the measurements made for the
            # iterations for this test.
            #
            # An empty item in the dataset list represents a
            # failure to obtain any measurement for that
            # iteration.
            #
            # It is possible for an item in the dataset to have an
            # uncached value and not have a corresponding cached
            # value if the cached test failed to record the
            # values.

            dataset = []
            for iteration in range(1, self._iterations+1):
                command = self.worker_subprocess.process_autophone_cmd(test=self)
                if command['interrupt']:
                    is_test_completed = False
                    self.handle_test_interrupt(command['reason'],
                                               command['test_result'])
                    break

                self.update_status(message='Attempt %d/%d for Test %s, '
                                   'run %d' %
                                   (attempt, self.stderrp_attempts,
                                    self.testname, iteration))

                dataset.append({})

                if not self.install_webappstartup():
                    self.update_status(message='Attempt %d/%d for Test %s, '
                                       'run %d failed to install webappstartup' %
                                       (attempt, self.stderrp_attempts,
                                        self.testname, iteration))
                    self.test_failure(
                        self.name,
                        'TEST_UNEXPECTED_FAIL',
                        'Failed to get uncached measurement.',
                        PhoneTestResult.TESTFAILED)
                    continue

                if not self.create_profile():
                    self.test_failure(self.name,
                                      'TEST_UNEXPECTED_FAIL',
                                      'Failed to create profile',
                                      PhoneTestResult.TESTFAILED)
                    continue

                measurement = self.runtest()
                if measurement:
                    self.test_pass(self.name)
                else:
                    self.test_failure(
                        self.name,
                        'TEST_UNEXPECTED_FAIL',
                        'Failed to get uncached measurement.',
                        PhoneTestResult.TESTFAILED)
                    continue

                dataset[-1]['uncached'] = measurement
                success = True

                measurement = self.runtest()
                if measurement:
                    self.test_pass(self.name)
                else:
                    self.test_failure(
                        self.name,
                        'TEST_UNEXPECTED_FAIL',
                        'Failed to get cached measurement.',
                        PhoneTestResult.TESTFAILED)
                    continue

                dataset[-1]['cached'] = measurement

                if self.is_stderr_below_threshold(
                        ('chrome_time',
                         'startup_time'),
                        dataset,
                        self.stderrp_accept):
                    self.loggerdeco.info(
                        'Accepted test %s after %d of %d iterations' %
                        (self.testname, iteration, self._iterations))
                    break

            if command and command['interrupt']:
                break
            if not success:
                # If we have not gotten a single measurement at this point,
                # just bail and report the failure rather than wasting time
                # continuing more attempts.
                self.loggerdeco.info(
                    'Failed to get measurements for test %s after %d/%d attempt '
                    'of %d iterations' % (self.testname, attempt,
                                          self.stderrp_attempts,
                                          self._iterations))
                self.worker_subprocess.mailer.send(
                    'Webappstartup test failed for Build %s %s on Phone %s' %
                    (self.build.tree, self.build.id, self.phone.id),
                    'No measurements were detected for test webappstartup.\n\n'
                    'Job        %s\n'
                    'Phone      %s\n'
                    'Repository %s\n'
                    'Build      %s\n'
                    'Revision   %s\n' %
                    (self.job_url,
                     self.phone.id,
                     self.build.tree,
                     self.build.id,
                     self.build.revision))
                self.test_failure(self.name, 'TEST_UNEXPECTED_FAIL',
                                  'No measurements detected.',
                                  PhoneTestResult.BUSTED)
                break

            if self.is_stderr_below_threshold(
                    ('chrome_time',
                     'startup_time'),
                    dataset,
                    self.stderrp_reject):
                rejected = False
            else:
                rejected = True
                self.loggerdeco.info(
                    'Rejected test %s after %d/%d iterations' %
                    (self.testname, iteration, self._iterations))

            self.loggerdeco.debug('publishing results')

            for datapoint in dataset:
                for cachekey in datapoint:
                    self.report_results(
                        starttime=datapoint[cachekey]['starttime'],
                        tstrt=datapoint[cachekey]['chrome_time'],
                        tstop=datapoint[cachekey]['startup_time'],
                        testname=self.testname,
                        cache_enabled=(cachekey=='cached'),
                        rejected=rejected)
            if not rejected:
                break

        return is_test_completed

    def kill_webappstartup(self):
        re_webapp = re.compile(r'%s|%s|%s:%s.Webapp0' % (
            self.webappstartup_name[:75],
            self.build.app_name,
            self.build.app_name, self.build.app_name))

        procs = self.dm.get_process_list()
        pids = [proc[0] for proc in procs if re_webapp.match(proc[1])]
        if not pids:
            self.loggerdeco.debug('kill_webappstartup: no matching pids: %s' %
                                  re_webapp.pattern)
            return

        self.dm.kill(pids, sig=9, root=True)

    def run_webappstartup(self):
        self.kill_webappstartup()

        # am start -a android.intent.action.MAIN -n com.firefox.cli.apk.webappstartupperformancetestbclary.pc91282b8e6a542101851c47a670b9c8c/org.mozilla.android.synthapk.LauncherActivity 

        intent = 'android.intent.action.MAIN'
        activity_name = 'org.mozilla.android.synthapk.LauncherActivity'

        extras = {}

        # self.environment is expected to be a dictionary of environment variables:
        # Fennec itself will set them when launched
        for (env_count, (env_key, env_val)) in enumerate(self.environment.iteritems()):
            extras["env" + str(env_count)] = "%s=%s" % (env_key, env_val)

        # Additional command line arguments that fennec will read and use.
        extra_args = []
        if extra_args:
            extras['args'] = " ".join(extra_args)

        self.loggerdeco.debug('run_webappstartup: %s' % self.build.app_name)
        try:
            # need to kill the fennec process and the webapp process
            # org.mozilla.fennec:org.mozilla.fennec.Webapp0'

            self.kill_webappstartup()
            self.dm.launch_application(self.webappstartup_name,
                                       activity_name,
                                       intent,
                                       extras=extras,
                                       wait=False,
                                       fail_if_running=False)
        except:
            self.loggerdeco.exception('run_webappstartup: Exception:')
            raise

    def runtest(self):
        # Clear logcat
        self.logcat.clear()

        # Run test
        self.run_webappstartup()

        # Get results - do this now so we don't have as much to
        # parse in logcat.
        starttime, chrome_time, startup_time = self.analyze_logcat()
        self.wait_for_fennec(max_wait_time=0)

        # Need to check for and clear crashes here since
        # the profile directory will change for webappstartup tests
        # for each iteration.
        self.handle_crashes()
        self.crash_processor.delete_crash_dumps()
        # Ensure we succeeded - no 0's reported
        datapoint = {}
        if chrome_time and startup_time:
            datapoint['starttime'] = starttime
            datapoint['chrome_time'] = chrome_time
            datapoint['startup_time'] = startup_time
        return datapoint

    def wait_for_fennec(self, max_wait_time=60, wait_time=5,
                        kill_wait_time=20):
        # Wait for up to a max_wait_time seconds for fennec to close
        # itself in response to the quitter request. Check that fennec
        # is still running every wait_time seconds. If fennec doesn't
        # close on its own, attempt up to 3 times to kill fennec, waiting
        # kill_wait_time seconds between attempts.
        # Return True if fennec exits on its own, False if it needs to be killed.
        # Re-raise the last exception if fennec can not be killed.
        max_wait_attempts = max_wait_time / wait_time
        for wait_attempt in range(1, max_wait_attempts+1):
            if not self.is_webapp_running():
                return True
            sleep(wait_time)
        self.loggerdeco.debug('wait_for_fennec: killing fennec')
        max_killattempts = 3
        for kill_attempt in range(1, max_killattempts+1):
            try:
                self.kill_webappstartup()
                break
            except ADBError:
                self.loggerdeco.exception('Attempt %d to kill fennec failed' %
                                          kill_attempt)
                if kill_attempt == max_killattempts:
                    raise
                sleep(kill_wait_time)
        return False

    def get_webappstartup_name(self):
        output = self.dm.shell_output("pm list packages webappstartup")
        if not output:
            self.webappstartup_name = None
        else:
            re_webappname = re.compile(r'package:(.*)')
            match = re_webappname.match(output)
            if not match:
                self.loggerdeco.warning('Could not find webappstartup package in %s' % output)
            self.webappstartup_name = match.group(1)

    def install_webappstartup(self):
        success = False
        for attempt in range(1, self.options.phone_retry_limit+1):
            self.loggerdeco.debug('Attempt %d Installing webappstartup' % attempt)
            try:
                if self.webappstartup_name and self.dm.is_app_installed(self.webappstartup_name):
                    self.dm.uninstall_app(self.webappstartup_name)
                self.dm.install_app(self._paths['webappstartup_apk'])
                success = True
                break
            except ADBError:
                self.loggerdeco.exception('Attempt %d Installing webappstartup' % attempt)
                sleep(self.options.phone_retry_wait)

        if not success:
            self.loggerdeco.error('Failure installing webappstartup')
        else:
            self.get_webappstartup_name()
        return success

    def is_webapp_running(self):
        for attempt in range(1, self.options.phone_retry_limit+1):
            try:
                return (self.dm.process_exist(self.build.app_name) or
                        self.dm.process_exist(self.webappstartup_name) or
                        self.dm.process_exist('%s:%s.Webapp0' % (
                            self.build.app_name, self.build.app_name)))
            except ADBError:
                self.loggerdeco.exception('Attempt %d is fennec running' % attempt)
                if attempt == self.options.phone_retry_limit:
                    raise
                sleep(self.options.phone_retry_wait)

    def analyze_logcat(self):
        self.loggerdeco.debug('analyzing logcat')

        logcat_prefix = '(\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})'
        chrome_prefix = '..GeckoBrowser.*: zerdatime .* - browser chrome startup finished.'
        webapp_prefix = '..GeckoConsole.*WEBAPP STARTUP COMPLETE'
        re_base_time = re.compile('%s' % logcat_prefix)
        re_start_time = re.compile(
            '%s .*(Gecko|Start proc %s for activity %s)' % (
                logcat_prefix, self.webappstartup_name, self.webappstartup_name))
        re_chrome_time = re.compile('%s %s' %
                                    (logcat_prefix, chrome_prefix))
        re_startup_time = re.compile('%s %s' %
                                     (logcat_prefix, webapp_prefix))

        base_time = 0
        start_time = 0
        chrome_time = 0
        startup_time = 0

        attempt = 1
        max_time = 90 # maximum time to wait for WEBAPP STARTUP COMPLETE
        wait_time = 3 # time to wait between attempts
        max_attempts = max_time / wait_time

        while attempt <= max_attempts and startup_time == 0:
            buf = self.logcat.get()
            for line in buf:
                self.loggerdeco.debug('analyze_logcat: %s' % line)
                match = re_base_time.match(line)
                if match and not base_time:
                    base_time = match.group(1)
                    self.loggerdeco.debug('analyze_logcat: base_time: %s' % base_time)
                match = re_start_time.match(line)
                if match and not start_time:
                    start_time = match.group(1)
                    self.loggerdeco.debug('analyze_logcat: start_time: %s' % start_time)
                    continue
                match = re_chrome_time.match(line)
                if match and not chrome_time:
                    chrome_time = match.group(1)
                    self.loggerdeco.debug('analyze_logcat: chrome_time: %s' % chrome_time)
                    continue
                match = re_startup_time.match(line)
                if match and not startup_time:
                    startup_time = match.group(1)
                    self.loggerdeco.debug('analyze_logcat: startup_time: %s' % startup_time)
                    continue
                if start_time and startup_time:
                    break
            if self.fennec_crashed:
                break
            if start_time == 0 or chrome_time == 0 or startup_time == 0:
                sleep(wait_time)
                attempt += 1
        if chrome_time and startup_time == 0:
            self.loggerdeco.info('Unable to find WEBAPP STARTUP COMPLETE')

        # The captured time from the logcat lines is in the format
        # MM-DD HH:MM:SS.mmm. It is possible for the year to change
        # between the different times, so we need to make adjustments
        # if necessary. First, we assume the year does not change and
        # parse the dates as if they are in the current year. If
        # the dates violate the natural order start_time,
        # throbber_start_time, throbber_stop_time, we can adjust the
        # year.

        if base_time and start_time and chrome_time and startup_time:
            parse = lambda y, t: datetime.datetime.strptime('%4d-%s' % (y, t), '%Y-%m-%d %H:%M:%S.%f')
            year = datetime.datetime.now().year
            base_time = parse(year, base_time)
            start_time = parse(year, start_time)
            chrome_time = parse(year, chrome_time)
            startup_time = parse(year, startup_time)

            self.loggerdeco.debug('analyze_logcat: before year adjustment '
                                  'base: %s, start: %s, '
                                  'chrome time: %s '
                                  'startup time: %s' %
                                  (base_time, start_time,
                                   chrome_time, startup_time))

            if base_time > start_time:
                base_time.replace(year=year-1)
            elif start_time > chrome_time:
                base_time.replace(year=year-1)
                start_time.replace(year=year-1)
            elif chrome_time > startup_time:
                base_time.replace(year=year-1)
                start_time.replace(year=year-1)
                chrome_time.replace(year=year-1)

            self.loggerdeco.debug('analyze_logcat: after year adjustment '
                                  'base: %s, start: %s, '
                                  'chrome time: %s '
                                  'startup time: %s' %
                                  (base_time, start_time,
                                   chrome_time, startup_time))

            # Convert the times to milliseconds from the base time.
            convert = lambda t1, t2: round((t2 - t1).total_seconds() * 1000.0)

            start_time = convert(base_time, start_time)
            chrome_time = convert(base_time, chrome_time)
            startup_time = convert(base_time, startup_time)

            self.loggerdeco.debug('analyze_logcat: base: %s, start: %s, '
                                  'chrome time: %s, startup_time: %s ' %
                                  (base_time, start_time, chrome_time, startup_time))

            if start_time > chrome_time:
                self.loggerdeco.warning('analyze_logcat: inconsistent measurements: '
                                        'start_time: %s, '
                                        'chrome_time: %s' %
                                      (start_time, chrome_time))
                start_time = chrome_time = startup_time = 0
        else:
            self.loggerdeco.warning(
                'analyze_logcat: failed to get measurements '
                'start_time: %s, chrome_time: %s, startup_time: %s' % (
                    start_time, chrome_time, startup_time))
            start_time = chrome_time = startup_time = 0

        return (start_time, chrome_time, startup_time)

