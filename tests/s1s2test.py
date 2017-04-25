# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import ConfigParser
import os
import re
import urlparse

from time import sleep

import utils

from perftest import PerfTest, PerfherderArtifact, PerfherderSuite, PerfherderOptions
from phonetest import TreeherderStatus, TestStatus


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

        self.loggerdeco.debug('S1S2Test: %s', self.__dict__)

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
            self.add_failure(
                self.name, TestStatus.TEST_UNEXPECTED_FAIL,
                'Aborting test - Could not install local pages on phone.',
                TreeherderStatus.EXCEPTION)
            return is_test_completed

        if not self.create_profile():
            self.add_failure(
                self.name, TestStatus.TEST_UNEXPECTED_FAIL,
                'Aborting test - Could not run Fennec.',
                TreeherderStatus.BUSTED)
            return is_test_completed

        perfherder_options = PerfherderOptions(self.perfherder_options,
                                               repo=self.build.tree)
        is_test_completed = True
        testcount = len(self._urls.keys())
        for testnum, (testname, url) in enumerate(self._urls.iteritems(), 1):
            self.loggerdeco = self.loggerdeco.clone(
                extradict={'phoneid': self.phone.id,
                           'buildid': self.build.id,
                           'testname': testname},
                extraformat='S1S2TestJob|%(phoneid)s|%(buildid)s|%(testname)s|%(message)s')
            self.dm._logger = self.loggerdeco
            self.loggerdeco.info('Running test (%d/%d) for %d iterations',
                                 testnum, testcount, self._iterations)

            command = None
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

                iteration = 0
                dataset = []
                for iteration in range(1, self._iterations+1):
                    # Calling svc power stayon true will turn on the
                    # display for at least some devices if it has
                    # turned off.
                    self.dm.power_on()
                    command = self.worker_subprocess.process_autophone_cmd(
                        test=self, require_ip_address=url.startswith('http'))
                    if command['interrupt']:
                        self.handle_test_interrupt(command['reason'],
                                                   command['test_result'])
                        break
                    self.update_status(message='Attempt %d/%d for Test %d/%d, '
                                       'run %d, for url %s' %
                                       (attempt, self.stderrp_attempts,
                                        testnum, testcount, iteration, url))

                    if not self.create_profile():
                        self.add_failure(
                            self.name,
                            TestStatus.TEST_UNEXPECTED_FAIL,
                            'Failed to create profile',
                            TreeherderStatus.TESTFAILED)
                        continue

                    measurement = self.runtest(url)
                    if not measurement:
                        self.loggerdeco.warning(
                            '%s %s Attempt %s Failed to get uncached measurement.',
                            testname, url, attempt)
                        continue

                    self.add_pass(url)
                    dataset.append({'uncached': measurement})

                    measurement = self.runtest(url)
                    if not measurement:
                        self.loggerdeco.warning(
                            '%s %s Attempt %s Failed to get cached measurement.',
                            testname, url, attempt)
                        continue

                    self.add_pass(url)
                    dataset[-1]['cached'] = measurement

                    if self.is_stderr_below_threshold(
                            ('throbberstart',
                             'throbberstop'),
                            dataset,
                            self.stderrp_accept):
                        self.loggerdeco.info(
                            'Accepted test (%d/%d) after %d of %d iterations',
                            testnum, testcount, iteration, self._iterations)
                        break

                if command and command['interrupt']:
                    break
                measurements = len(dataset)
                if measurements > 0 and self._iterations != measurements:
                    self.add_failure(
                        self.name,
                        TestStatus.TEST_UNEXPECTED_FAIL,
                        'Failed to get all measurements',
                        TreeherderStatus.TESTFAILED)
                elif measurements == 0:
                    # If we have not gotten a single measurement at this point,
                    # just bail and report the failure rather than wasting time
                    # continuing more attempts.
                    self.add_failure(
                        self.name, TestStatus.TEST_UNEXPECTED_FAIL,
                        'No measurements detected.',
                        TreeherderStatus.BUSTED)
                    self.loggerdeco.info(
                        'Failed to get measurements for test %s after %d/%d attempt '
                        'of %d iterations', testname, attempt,
                        self.stderrp_attempts, self._iterations)
                    self.worker_subprocess.mailer.send(
                        '%s %s failed for Build %s %s on %s %s' %
                        (self.__class__.__name__, testname, self.build.tree,
                         self.build.id, utils.host(), self.phone.id),
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
                         self.build.changeset))
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
                        'Rejected test (%d/%d) after %d/%d iterations',
                        testnum, testcount, iteration, self._iterations)

                self.loggerdeco.debug('publishing results')

                perfherder_values = {'geometric_mean': 0}
                metric_keys = ['throbberstart', 'throbberstop', 'throbbertime']
                cache_names = {'uncached': 'first', 'cached': 'second'}
                cache_keys = cache_names.keys()

                for metric_key in metric_keys:
                    perfherder_values[metric_key] = {'geometric_mean': 0}
                    for cache_key in cache_keys:
                        perfherder_values[metric_key][cache_key] = {'median': 0, 'values': []}

                for datapoint in dataset:
                    for cache_key in datapoint:
                        starttime = datapoint[cache_key]['starttime']
                        throbberstart = datapoint[cache_key]['throbberstart']
                        throbberstop = datapoint[cache_key]['throbberstop']
                        self.report_results(
                            starttime=starttime,
                            tstrt=throbberstart,
                            tstop=throbberstop,
                            testname=testname,
                            cache_enabled=(cache_key == 'cached'),
                            rejected=rejected)
                        perfherder_values['throbberstart'][cache_key]['values'].append(
                            throbberstart - starttime)
                        perfherder_values['throbberstop'][cache_key]['values'].append(
                            throbberstop - starttime)
                        perfherder_values['throbbertime'][cache_key]['values'].append(
                            throbberstop - throbberstart)

                test_values = []
                for metric_key in metric_keys:
                    for cache_key in cache_keys:
                        perfherder_values[metric_key][cache_key]['median'] = utils.median(
                            perfherder_values[metric_key][cache_key]['values'])
                    perfherder_values[metric_key]['geometric_mean'] = utils.geometric_mean(
                        [perfherder_values[metric_key]['uncached']['median'],
                         perfherder_values[metric_key]['cached']['median']])
                    test_values.append(perfherder_values[metric_key]['geometric_mean'])

                perfherder_suite = PerfherderSuite(name=testname,
                                                   value=utils.geometric_mean(test_values),
                                                   options=perfherder_options)
                for metric_key in metric_keys:
                    for cache_key in cache_keys:
                        cache_name = cache_names[cache_key]
                        subtest_name = "%s %s" % (metric_key, cache_name)
                        perfherder_suite.add_subtest(
                            subtest_name,
                            perfherder_values[metric_key][cache_key]['median'],
                            options=perfherder_options)

                self.perfherder_artifact = PerfherderArtifact()
                self.perfherder_artifact.add_suite(perfherder_suite)
                self.loggerdeco.debug("PerfherderArtifact: %s", self.perfherder_artifact)

                if not rejected:
                    break

            if command and command['interrupt']:
                break

        return is_test_completed

    def runtest(self, url):
        # Clear logcat
        self.worker_subprocess.logcat.clear()

        # Run test
        self.run_fennec_with_profile(self.build.app_name, url)

        # Get results - do this now so we don't have as much to
        # parse in logcat.
        starttime, throbberstart, throbberstop = self.analyze_logcat()

        self.wait_for_fennec()
        self.handle_crashes()

        # Ensure we succeeded - no 0's reported
        datapoint = {}
        if throbberstart and throbberstop:
            datapoint['starttime'] = starttime
            datapoint['throbberstart'] = throbberstart
            datapoint['throbberstop'] = throbberstop
            datapoint['throbbertime'] = throbberstop - throbberstart
        return datapoint

    def analyze_logcat(self):
        self.loggerdeco.debug('analyzing logcat')

        logcat_prefix = r'\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}'
        application_start = r'..GeckoApplication.*zerdatime (\d+) - (Fennec )?application start'
        throbber_prefix = r'..GeckoToolbarDisplayLayout.*zerdatime (\d+) -'
        re_start_time = re.compile(r'%s %s' % (logcat_prefix, application_start))
        re_throbber_start_time = re.compile('%s %s (Throbber|page load) start' %
                                            (logcat_prefix, throbber_prefix))
        re_throbber_stop_time = re.compile('%s %s (Throbber|page load) stop' %
                                           (logcat_prefix, throbber_prefix))

        start_time = 0
        throbber_start_time = 0
        throbber_stop_time = 0

        attempt = 1
        max_time = 90 # maximum time to wait for throbbers
        wait_time = 3 # time to wait between attempts
        max_attempts = max_time / wait_time
        success = False
        while not success and attempt <= max_attempts:
            buf = self.worker_subprocess.logcat.get()
            for line in buf:
                self.loggerdeco.debug('analyze_logcat: %s', line)

                if not start_time:
                    match = re_start_time.match(line)
                    if match:
                        start_time = int(match.group(1))
                        self.loggerdeco.info(
                            'analyze_logcat: new start_time: %s',
                            start_time)
                    continue # line

                # We want the first throbberstart and throbberstop
                # after the start_time.
                if not throbber_start_time:
                    match = re_throbber_start_time.match(line)
                    if match:
                        throbber_start_time = int(match.group(1))
                        self.loggerdeco.info(
                            'analyze_logcat: throbber_start_time: %s',
                            throbber_start_time)
                    continue # line

                match = re_throbber_stop_time.match(line)
                if match:
                    throbber_stop_time = int(match.group(1))
                    self.loggerdeco.info(
                        'analyze_logcat: throbber_stop_time: %s',
                        throbber_stop_time)
                    break # line

            if self.handle_crashes():
                # If fennec crashed, don't bother looking for the Throbbers
                self.loggerdeco.warning('analyze_logcat: fennec crashed.')
                break # attempt

            if start_time and throbber_start_time and throbber_stop_time:
                self.loggerdeco.debug(
                    'analyze_logcat: got measurements '
                    'start_time: %s, throbber start: %s, throbber stop: %s',
                    start_time, throbber_start_time, throbber_stop_time)
                success = True
            else:
                self.loggerdeco.debug(
                    'analyze_logcat: attempt %s, start_time: %s, '
                    'throbber_start_time: %s, throbber_stop_time: %s' %
                    (attempt, start_time, throbber_start_time, throbber_stop_time))
                sleep(wait_time)
                attempt += 1

        if not success:
            self.loggerdeco.warning(
                'analyze_logcat: failed to get measurements '
                'start_time: %s, throbber start: %s, throbber stop: %s',
                start_time, throbber_start_time, throbber_stop_time)
            start_time = throbber_start_time = throbber_stop_time = 0

        return (start_time, throbber_start_time, throbber_stop_time)
