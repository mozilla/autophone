# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import ConfigParser
import datetime
import glob
import os
import posixpath
import re
import sys
from time import sleep

from mozprofile import FirefoxProfile

from adb import ADBError
from logdecorator import LogDecorator
from perftest import PerfTest
from phonestatus import TestResult

class S1S2Test(PerfTest):

    @property
    def phonedash_url(self):
        # For s1s2test, there are 4 different test names due to historical design.
        # We pick local-blank as the default.
        return self._phonedash_url('local-blank')

    def setup_job(self):
        PerfTest.setup_job(self)

        # [paths]
        autophone_directory = os.path.dirname(os.path.abspath(sys.argv[0]))
        self._paths = {}
        self._paths['sources'] = [os.path.join(autophone_directory, 'files/base/')]
        self._paths['dest'] = posixpath.join(self.base_device_path, 's1test/')
        try:
            try:
                sources = self.cfg.get('paths', 'sources').split()
                self._paths['sources'] = []
                for source in sources:
                    if not source.endswith('/'):
                        source += '/'
                    self._paths['sources'].append(source)
            except ConfigParser.NoOptionError:
                pass
            for opt in ('dest', 'profile'):
                try:
                    self._paths[opt] = self.cfg.get('paths', opt)
                    if not self._paths[opt].endswith('/'):
                        self._paths[opt] += '/'
                except ConfigParser.NoOptionError:
                    pass
        except ConfigParser.NoSectionError:
            pass
        if 'profile' in self._paths:
            self.profile_path = self._paths['profile']
        # _pushes = {'sourcepath' : 'destpath', ...}
        self._pushes = {}
        for source in self._paths['sources']:
            for push in glob.glob(source + '*'):
                if push.endswith('~') or push.endswith('.bak'):
                    continue
                push_dest = posixpath.join(self._paths['dest'], os.path.basename(push))
                self._pushes[push] = push_dest
        # [tests]
        self._tests = {}
        for t in self.cfg.items('tests'):
            self._tests[t[0]] = t[1]
        # Map URLS - {urlname: url} - urlname serves as testname
        self._urls = {}
        for test_location, test_path in self.cfg.items('locations'):
            for test_name in self._tests:
                if test_path:
                    test_url = test_path + self._tests[test_name]
                else:
                    test_url = 'file://' + self._paths['dest'] + self._tests[test_name]
                self._urls["%s-%s" % (test_location, test_name)] = test_url
        self._initialize_url = 'file://' + self._paths['dest'] + 'initialize_profile.html'

    def run_job(self):
        if not self.install_local_pages():
            message='Aborting test - Could not install local pages on phone.'
            self.update_status(message=message)
            self.result = TestResult.EXCEPTION
            self.message = message
            return

        if not self.create_profile():
            message='Aborting test - Could not run Fennec.'
            self.update_status(message=message)
            self.result = TestResult.EXCEPTION
            self.message = message
            return

        testcount = len(self._urls.keys())
        for testnum,(testname,url) in enumerate(self._urls.iteritems(), 1):
            self.loggerdeco = LogDecorator(self.logger,
                                           {'phoneid': self.phone.id,
                                            'pid': os.getpid(),
                                            'buildid': self.build.id,
                                            'testname': testname},
                                           '%(phoneid)s|%(pid)s|%(buildid)s|'
                                           '%(testname)s|%(message)s')
            self.dm._logger = self.loggerdeco
            if self.check_results(testname):
                # We already have good results for this test and build.
                # No need to test it again.
                self.loggerdeco.info('Skipping test (%d/%d) for %d iterations' %
                                     (testnum, testcount, self._iterations))
                continue
            self.loggerdeco.info('Running test (%d/%d) for %d iterations' %
                                 (testnum, testcount, self._iterations))

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
                    self.update_status(message='Attempt %d/%d for Test %d/%d, '
                                       'run %d, for url %s' %
                                       (attempt, self.stderrp_attempts,
                                        testnum, testcount, iteration, url))

                    dataset.append({})

                    if not self.create_profile():
                        continue

                    measurement = self.runtest(url)
                    if not measurement:
                        continue
                    dataset[-1]['uncached'] = measurement
                    success = True

                    measurement = self.runtest(url)
                    if not measurement:
                        continue
                    dataset[-1]['cached'] = measurement

                    if self.is_stderr_below_threshold(
                            ('throbberstart',
                             'throbberstop'),
                            dataset,
                            self.stderrp_accept):
                        self.loggerdeco.info(
                            'Accepted test (%d/%d) after %d of %d iterations' %
                            (testnum, testcount, iteration, self._iterations))
                        break

                # If we have not gotten a single measurement at this point,
                # just bail and report the failure rather than wasting time
                # continuing more attempts.
                if not success:
                    self.loggerdeco.info(
                        'Failed to get measurements for test %s after %d/%d attempt '
                        'of %d iterations' % (testname, attempt,
                                              self.stderrp_attempts,
                                              self._iterations))
                    break

                if self.is_stderr_below_threshold(
                        ('throbberstart',
                         'throbberstop'),
                        dataset,
                        self.stderrp_reject):
                    rejected = False
                else:
                    rejected = True
                    self.loggerdeco.info(
                        'Rejected test (%d/%d) after %d/%d iterations' %
                        (testnum, testcount, iteration, self._iterations))

                self.loggerdeco.debug('publishing results')

                for datapoint in dataset:
                    for cachekey in datapoint:
                        self.report_results(
                            starttime=datapoint[cachekey]['starttime'],
                            tstrt=datapoint[cachekey]['throbberstart'],
                            tstop=datapoint[cachekey]['throbberstop'],
                            testname=testname,
                            cache_enabled=(cachekey=='cached'),
                            rejected=rejected)
                if not rejected:
                    break

            if not success:
                self.worker_subprocess.mailer.send(
                    'S1S2Test %s failed for Build %s %s on Phone %s' %
                    (testname, self.build.tree, self.build.id,
                     self.phone.id),
                    'No measurements were detected for test %s.\n\n'
                    'Repository: %s\n'
                    'Build Id:   %s\n'
                    'Revision:   %s\n' %
                    (testname, self.build.tree, self.build.id, self.build.revision))
                self.result = TestResult.BUSTED
                self.message = 'No measurements detected.'
                break
        if not self.result:
            self.result = TestResult.SUCCESS

    def runtest(self, url):
        # Clear logcat
        self.dm.clear_logcat()

        # Run test
        self.run_fennec_with_profile(self.build.app_name, url)

        # Get results - do this now so we don't have as much to
        # parse in logcat.
        starttime, throbberstart, throbberstop = self.analyze_logcat()

        self.wait_for_fennec()

        # Ensure we succeeded - no 0's reported
        datapoint = {}
        if (throbberstart and throbberstop):
            datapoint['starttime'] = starttime
            datapoint['throbberstart'] = throbberstart
            datapoint['throbberstop'] = throbberstop
            datapoint['throbbertime'] = throbberstop - throbberstart
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
            if not self.dm.process_exist(self.build.app_name):
                return True
            sleep(wait_time)
        self.loggerdeco.debug('killing fennec')
        max_killattempts = 3
        for kill_attempt in range(1, max_killattempts+1):
            try:
                self.dm.pkill(self.build.app_name, root=True)
                break
            except ADBError:
                self.loggerdeco.exception('Attempt %d to kill fennec failed' %
                                          kill_attempt)
                if kill_attempt == max_killattempts:
                    raise
                sleep(kill_wait_time)
        return False

    def create_profile(self, custom_prefs=None):
        # Create, install and initialize the profile to be
        # used in the test.

        # make sure firefox isn't running when we try to
        # install the profile.

        self.dm.pkill(self.build.app_name, root=True)

        telemetry_prompt = 999
        if self.build.id < '20130103':
            telemetry_prompt = 2
        prefs = {
            'browser.firstrun.show.localepicker': False,
            'browser.sessionstore.resume_from_crash': False,
            'dom.ipc.plugins.flash.subprocess.crashreporter.enabled': False,
            'browser.firstrun.show.uidiscovery': False,
            'shell.checkDefaultClient': False,
            'browser.warnOnQuit': False,
            'browser.EULA.override': True,
            'toolkit.telemetry.prompted': telemetry_prompt,
            'toolkit.telemetry.notifiedOptOut': telemetry_prompt,
            'datareporting.healthreport.service.enabled': False,
            }
        if isinstance(custom_prefs, dict):
            prefs = dict(prefs.items() + custom_prefs.items())
        profile = FirefoxProfile(preferences=prefs, addons='%s/xpi/quitter.xpi' %
                                 os.getcwd())
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
            self.update_status(message=msg)

        return success

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

    def analyze_logcat(self):
        self.loggerdeco.debug('analyzing logcat')

        logcat_prefix = '(\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})'
        throbber_prefix = '..GeckoToolbarDisplayLayout.*zerdatime (\d+) - Throbber'
        re_base_time = re.compile('%s' % logcat_prefix)
        re_start_time = re.compile('%s .*(Gecko|fennec)' %
                                   logcat_prefix)
        re_throbber_start_time = re.compile('%s %s start' %
                                            (logcat_prefix, throbber_prefix))
        re_throbber_stop_time = re.compile('%s %s stop' %
                                           (logcat_prefix, throbber_prefix))

        base_time = 0
        start_time = 0
        throbber_start_time = 0
        throbber_stop_time = 0

        attempt = 1
        max_time = 90 # maximum time to wait for throbbers
        wait_time = 3 # time to wait between attempts
        max_attempts = max_time / wait_time

        while (attempt <= max_attempts and (throbber_start_time == 0 or
                                            throbber_stop_time == 0)):
            buf = self.get_logcat()
            for line in buf:
                self.loggerdeco.debug('analyze_logcat: %s' % line)
                match = re_base_time.match(line)
                if match and not base_time:
                    base_time = match.group(1)
                    self.loggerdeco.debug('analyze_logcat: base_time: %s' % base_time)
                # we want the first throbberstart and throbberstop.
                match = re_start_time.match(line)
                if match and not start_time:
                    start_time = match.group(1)
                    self.loggerdeco.debug('analyze_logcat: start_time: %s' % start_time)
                    continue
                match = re_throbber_start_time.match(line)
                if match and not throbber_start_time:
                    throbber_start_time = match.group(1)
                    self.loggerdeco.debug('analyze_logcat: throbber_start_time: %s' % throbber_start_time)
                    continue
                match = re_throbber_stop_time.match(line)
                if match and not throbber_stop_time:
                    throbber_stop_time = match.group(1)
                    self.loggerdeco.debug('analyze_logcat: throbber_stop_time: %s' % throbber_stop_time)
                    continue
                if start_time and throbber_start_time and throbber_stop_time:
                    break
            if (start_time == 0 or
                throbber_start_time == 0 or
                throbber_stop_time == 0):
                sleep(wait_time)
                attempt += 1
        if self.check_for_crashes():
            self.loggerdeco.info('fennec crashed')
            fennec_crashed = True
        else:
            fennec_crashed = False
        if throbber_start_time and throbber_stop_time == 0 and not fennec_crashed:
            self.loggerdeco.info('Unable to find Throbber stop')

        # The captured time from the logcat lines is in the format
        # MM-DD HH:MM:SS.mmm. It is possible for the year to change
        # between the different times, so we need to make adjustments
        # if necessary. First, we assume the year does not change and
        # parse the dates as if they are in the current year. If
        # the dates violate the natural order start_time,
        # throbber_start_time, throbber_stop_time, we can adjust the
        # year.

        if base_time and start_time and throbber_start_time and throbber_stop_time:
            parse = lambda y, t: datetime.datetime.strptime('%4d-%s' % (y, t), '%Y-%m-%d %H:%M:%S.%f')
            year = datetime.datetime.now().year
            base_time = parse(year, base_time)
            start_time = parse(year, start_time)
            throbber_start_time = parse(year, throbber_start_time)
            throbber_stop_time = parse(year, throbber_stop_time)

            self.loggerdeco.debug('analyze_logcat: before year adjustment '
                                  'base: %s, start: %s, '
                                  'throbber start: %s' %
                                  (base_time, start_time,
                                   throbber_start_time))

            if base_time > start_time:
                base_time.replace(year=year-1)
            elif start_time > throbber_start_time:
                base_time.replace(year=year-1)
                start_time.replace(year=year-1)
            elif throbber_start_time > throbber_stop_time:
                base_time.replace(year=year-1)
                start_time.replace(year=year-1)
                throbber_start_time.replace(year-1)

            self.loggerdeco.debug('analyze_logcat: after year adjustment '
                                  'base: %s, start: %s, '
                                  'throbber start: %s' %
                                  (base_time, start_time,
                                   throbber_start_time))

            # Convert the times to milliseconds from the base time.
            convert = lambda t1, t2: round((t2 - t1).total_seconds() * 1000.0)

            start_time = convert(base_time, start_time)
            throbber_start_time = convert(base_time, throbber_start_time)
            throbber_stop_time = convert(base_time, throbber_stop_time)

            self.loggerdeco.debug('analyze_logcat: base: %s, start: %s, '
                                  'throbber start: %s, throbber stop: %s, '
                                  'throbber time: %s ' %
                                  (base_time, start_time,
                                   throbber_start_time, throbber_stop_time,
                                   throbber_stop_time - throbber_start_time))

            if (start_time > throbber_start_time or
                start_time > throbber_stop_time or
                throbber_start_time > throbber_stop_time):
                self.loggerdeco.warning('analyze_logcat: inconsistent measurements: '
                                        'start: %s, '
                                        'throbber start: %s, throbber stop: %s ' %
                                      (start_time,
                                       throbber_start_time,
                                       throbber_stop_time))
                start_time = throbber_start_time = throbber_stop_time = 0
        else:
            self.loggerdeco.warning(
                'analyze_logcat: failed to get measurements '
                'start_time: %s, throbber start: %s, throbber stop: %s' % (
                    start_time, throbber_start_time, throbber_stop_time))
            start_time = throbber_start_time = throbber_stop_time = 0

        return (start_time, throbber_start_time, throbber_stop_time)


