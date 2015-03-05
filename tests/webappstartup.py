# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import datetime
import os
import re
import sys
from time import sleep

from adb import ADBError
from perftest import PerfTest
from phonetest import PhoneTestResult

class WebappStartupTest(PerfTest):
    def __init__(self, phone, options, config_file=None,
                 enable_unittests=False, test_devices_repos={},
                 chunk=1):
        PerfTest.__init__(self, phone, options,
                          config_file=config_file,
                          enable_unittests=enable_unittests,
                          test_devices_repos=test_devices_repos,
                          chunk=chunk)
        self.webappstartup_name = None

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
        if self.webappstartup_name:
            self.dm.uninstall_app(self.webappstartup_name)
        PerfTest.teardown_job(self)

    def run_job(self):

        self.testname = 'webappstartup'

        if self.check_results(self.testname):
            # We already have good results for this test and build.
            # No need to test it again.
            self.message = 'Already have results for this test.'
            self.update_status(message=self.message)
            self.test_result.status = PhoneTestResult.USERCANCEL
            self.test_result.add_failure(self.name, 'TEST_UNEXPECTED_FAIL',
                                         self.message)
            return
        self.loggerdeco.info('Running test for %d iterations' %
                             self._iterations)

        # success == False indicates that none of the attempts
        # were successful in getting any measurement. This is
        # typically due to a regression in the brower which should
        # be reported.
        success = False
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
                    continue

                measurement = self.runtest()
                if not measurement:
                    self.test_result.add_failure(
                        self.name,
                        'TEST_UNEXPECTED_FAIL',
                        'Failed to get uncached measurement.')
                    continue
                dataset[-1]['uncached'] = measurement
                success = True

                measurement = self.runtest()
                if not measurement:
                    self.test_result.add_failure(
                        self.name,
                        'TEST_UNEXPECTED_FAIL',
                        'Failed to get cached measurement.')
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

            # If we have not gotten a single measurement at this point,
            # just bail and report the failure rather than wasting time
            # continuing more attempts.
            if success:
                self.test_result.add_pass(self.name)
            else:
                self.loggerdeco.info(
                    'Failed to get measurements for test %s after %d/%d attempt '
                    'of %d iterations' % (self.testname, attempt,
                                          self.stderrp_attempts,
                                          self._iterations))
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

        if success:
            self.test_result.status = PhoneTestResult.SUCCESS
        else:
            self.worker_subprocess.mailer.send(
                'Webappstartup test failed for Build %s %s on Phone %s' %
                (self.build.tree, self.build.id, self.phone.id),
                'No measurements were detected for test webappstartup.\n\n'
                'Repository: %s\n'
                'Build Id:   %s\n'
                'Revision:   %s\n' %
                (self.build.tree, self.build.id, self.build.revision))
            self.message = 'No measurements detected.'
            self.update_status(message=self.message)
            self.test_result.status = PhoneTestResult.BUSTED
            self.test_result.add_failure(self.name, 'TEST_UNEXPECTED_FAIL',
                                         self.message)

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

        moz_env = {'MOZ_CRASHREPORTER_NO_REPORT': '1'}
        # moz_env is expected to be a dictionary of environment variables:
        # Fennec itself will set them when launched
        for (env_count, (env_key, env_val)) in enumerate(moz_env.iteritems()):
            extras["env" + str(env_count)] = env_key + "=" + env_val

        # Additional command line arguments that fennec will read and use (e.g.
        # with a custom profile)
        #extra_args = ['--profile', self.profile_path]
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
        re_start_time = re.compile('%s .*(Gecko|fennec)' %
                                   logcat_prefix)
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

