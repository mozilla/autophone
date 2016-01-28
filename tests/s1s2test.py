# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import ConfigParser
import datetime
import logging
import os
import re
import urlparse

from time import sleep

import utils

from perftest import PerfTest
from phonetest import PhoneTestResult

# Set the logger globally in the file, but this must be reset when
# used in a child process.
logger = logging.getLogger()


class S1S2Test(PerfTest):
    def __init__(self, dm=None, phone=None, options=None,
                 config_file=None, chunk=1, repos=[]):

        PerfTest.__init__(self, dm=dm, phone=phone, options=options,
                          config_file=config_file, chunk=chunk, repos=repos)

        # Map URLS - {urlname: url} - urlname serves as testname
        self._urls = {}
        config_vars = {'webserver_url': options.webserver_url}

        try:
            location_items = self.cfg.items('locations', False, config_vars)
        except ConfigParser.NoSectionError:
            location_items = [('local', None)]

        # Finialize test configuration
        for test_location, test_path in location_items:
            if test_location in config_vars:
                # Ignore the pseudo-options which result from passing
                # the config_vars for interpolation.
                continue
            for test_name in self._tests:
                if test_path:
                    test_url = urlparse.urljoin(test_path, self._tests[test_name])
                else:
                    test_url = 'file://' + os.path.join(self._paths['dest'],
                                                        self._tests[test_name])
                self.loggerdeco.debug(
                    'test_location: %s, test_name: %s, test_path: %s, '
                    'test: %s, test_url: %s' %
                    (test_location, test_name, test_path,
                     self._tests[test_name], test_url))
                self._urls["%s-%s" % (test_location, test_name)] = test_url

    @property
    def name(self):
        return 'autophone-s1s2%s' % self.name_suffix

    @property
    def phonedash_url(self):
        # For s1s2test, there are 4 different test names due to historical design.
        # We pick local-blank as the default.
        return self._phonedash_url('local-blank')

    def run_job(self):
        is_test_completed = False

        if not self.install_local_pages():
            self.test_failure(
                self.name, 'TEST_UNEXPECTED_FAIL',
                'Aborting test - Could not install local pages on phone.',
                PhoneTestResult.EXCEPTION)
            return is_test_completed

        if not self.create_profile():
            self.test_failure(
                self.name, 'TEST_UNEXPECTED_FAIL',
                'Aborting test - Could not run Fennec.',
                PhoneTestResult.BUSTED)
            return is_test_completed

        is_test_completed = True
        testcount = len(self._urls.keys())
        for testnum,(testname,url) in enumerate(self._urls.iteritems(), 1):
            if self.fennec_crashed:
                break
            self.loggerdeco = self.loggerdeco.clone(
                extradict={'phoneid': self.phone.id,
                           'buildid': self.build.id,
                           'testname': testname},
                extraformat='%(phoneid)s|%(buildid)s|%(testname)s|%(message)s')
            self.dm._logger = self.loggerdeco
            self.loggerdeco.info('Running test (%d/%d) for %d iterations' %
                                 (testnum, testcount, self._iterations))

            # success == False indicates that none of the attempts
            # were successful in getting any measurement. This is
            # typically due to a regression in the brower which should
            # be reported.
            success = False
            command = None
            for attempt in range(1, self.stderrp_attempts+1):
                if self.fennec_crashed:
                    break
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
                    # Calling svc power stayon true will turn on the
                    # display for at least some devices if it has
                    # turned off.
                    self.dm.power_on()
                    command = self.worker_subprocess.process_autophone_cmd(
                        test=self, require_ip_address=url.startswith('http'))
                    if command['interrupt']:
                        is_test_completed = False
                        self.handle_test_interrupt(command['reason'],
                                                   command['test_result'])
                        break
                    if self.fennec_crashed:
                        break
                    self.update_status(message='Attempt %d/%d for Test %d/%d, '
                                       'run %d, for url %s' %
                                       (attempt, self.stderrp_attempts,
                                        testnum, testcount, iteration, url))

                    dataset.append({})

                    if not self.create_profile():
                        self.test_failure(url,
                                          'TEST_UNEXPECTED_FAIL',
                                          'Failed to create profile',
                                          PhoneTestResult.TESTFAILED)
                        continue

                    measurement = self.runtest(url)
                    if measurement:
                        self.test_pass(url)
                    else:
                        self.test_failure(
                            url,
                            'TEST_UNEXPECTED_FAIL',
                            'Failed to get uncached measurement.',
                            PhoneTestResult.TESTFAILED)
                        continue
                    dataset[-1]['uncached'] = measurement
                    success = True

                    measurement = self.runtest(url)
                    if measurement:
                        self.test_pass(url)
                    else:
                        self.test_failure(
                            url,
                            'TEST_UNEXPECTED_FAIL',
                            'Failed to get cached measurement.',
                            PhoneTestResult.TESTFAILED)
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

                if command and command['interrupt']:
                    break
                if not success:
                    # If we have not gotten a single measurement at this point,
                    # just bail and report the failure rather than wasting time
                    # continuing more attempts.
                    self.loggerdeco.info(
                        'Failed to get measurements for test %s after %d/%d attempt '
                        'of %d iterations' % (testname, attempt,
                                              self.stderrp_attempts,
                                              self._iterations))
                    self.worker_subprocess.mailer.send(
                        '%s %s failed for Build %s %s on %s %s' %
                        (self.__class__.__name__, testname, self.build.tree,
                         self.build.id, utils.host(),  self.phone.id),
                        'No measurements were detected for test %s.\n\n'
                        'Job        %s\n'
                        'Host       %s\n'
                        'Phone      %s\n'
                        'Repository %s\n'
                        'Build      %s\n'
                        'Revision   %s\n' %
                        (testname,
                         self.job_url,
                         utils.host(),
                         self.phone.id,
                         self.build.tree,
                         self.build.id,
                         self.build.revision))
                    self.test_failure(self.name, 'TEST_UNEXPECTED_FAIL',
                                      'No measurements detected.',
                                      PhoneTestResult.BUSTED)
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

            if command and command['interrupt']:
                break
            elif not success:
                break

        return is_test_completed

    def runtest(self, url):
        # Clear logcat
        self.logcat.clear()

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

    def analyze_logcat(self):
        self.loggerdeco.debug('analyzing logcat')

        logcat_prefix = '(\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})'
        throbber_prefix = '..GeckoToolbarDisplayLayout.*zerdatime (\d+) - Throbber'
        re_base_time = re.compile('%s' % logcat_prefix)
        re_gecko_time = re.compile('%s .*([Gg]ecko)' % logcat_prefix)
        re_start_time = re.compile(
            '%s .*('
            'Start proc .*%s.* for activity %s|'
            'Fennec application start)' % (
                logcat_prefix, self.build.app_name, self.build.app_name))
        re_throbber_start_time = re.compile('%s %s start' %
                                            (logcat_prefix, throbber_prefix))
        re_throbber_stop_time = re.compile('%s %s stop' %
                                           (logcat_prefix, throbber_prefix))

        base_time = 0
        start_time = 0
        throbber_start_time = 0
        throbber_stop_time = 0
        start_time_reason = ''
        fennec_start = 'Fennec application start'

        attempt = 1
        max_time = 90 # maximum time to wait for throbbers
        wait_time = 3 # time to wait between attempts
        max_attempts = max_time / wait_time

        while (attempt <= max_attempts and (throbber_start_time == 0 or
                                            throbber_stop_time == 0)):
            buf = self.logcat.get()
            for line in buf:
                self.loggerdeco.debug('analyze_logcat: %s' % line)
                match = re_base_time.match(line)
                if match and not base_time:
                    base_time = match.group(1)
                    self.loggerdeco.info('analyze_logcat: base_time: %s' %
                                         base_time)
                # We want the Fennec application start message, or the
                # Start proc message or the first gecko related
                # message in order to determine the start_time which
                # will be used to convert the absolute time values
                # into values relative to the start of fennec.  See
                # https://bugzilla.mozilla.org/show_bug.cgi?id=1214810
                if not start_time:
                    match = re_gecko_time.match(line)
                    if match:
                        start_time = match.group(1)
                        start_time_reason = match.group(2)
                        self.loggerdeco.info(
                            'analyze_logcat: new start_time: %s %s' %
                            (start_time, start_time_reason))
                match = re_start_time.match(line)
                if match:
                    group1 = match.group(1)
                    group2 = match.group(2)
                    if not start_time:
                        start_time = group1
                        start_time_reason = group2
                        self.loggerdeco.info(
                            'analyze_logcat: new start_time: %s %s' %
                            (start_time, start_time_reason))
                    elif (fennec_start in group2 and
                          fennec_start not in start_time_reason):
                        # Only use the first if there are multiple
                        # fennec_start messages.
                        start_time = group1
                        start_time_reason = group2
                        self.loggerdeco.info(
                            'analyze_logcat: updated start_time: %s %s' %
                            (start_time, start_time_reason))
                    elif (fennec_start not in start_time_reason and
                          group2.startswith('Start proc')):
                        start_time = group1
                        start_time_reason = group2
                        self.loggerdeco.info(
                            'analyze_logcat: updated start_time: %s %s' %
                            (start_time, start_time_reason))
                    else:
                        self.loggerdeco.info(
                            'analyze_logcat: ignoring start_time: %s %s' %
                            (group1, group2))
                    continue
                # We want the first throbberstart and throbberstop
                # after the start_time.
                match = re_throbber_start_time.match(line)
                if match:
                    if throbber_start_time:
                        self.loggerdeco.warning(
                            'analyze_logcat: throbber_start_time: %s '
                            'missing throbber_stop. Resetting '
                            'throbber_start_time.' % throbber_start_time)
                    throbber_start_time = match.group(1)
                    self.loggerdeco.info(
                        'analyze_logcat: throbber_start_time: %s' %
                        throbber_start_time)
                    continue
                match = re_throbber_stop_time.match(line)
                if match and not throbber_stop_time:
                    throbber_stop_time = match.group(1)
                    self.loggerdeco.info(
                        'analyze_logcat: throbber_stop_time: %s' %
                        throbber_stop_time)
                    continue
                if start_time and throbber_start_time and throbber_stop_time:
                    break
            if self.fennec_crashed:
                # If fennec crashed, don't bother looking for the Throbbers
                self.loggerdeco.warning('analyze_logcat: fennec crashed.')
                break
            if (start_time == 0 or
                throbber_start_time == 0 or
                throbber_stop_time == 0):
                sleep(wait_time)
                attempt += 1
        if throbber_start_time and throbber_stop_time == 0:
            self.loggerdeco.warning('Unable to find Throbber stop')

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
