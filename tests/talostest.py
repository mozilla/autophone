# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import ConfigParser
import logging
import os
import re
from time import sleep

from perftest import PerfTest, PerfherderArtifact, PerfherderSuite
from phonetest import PhoneTestResult
from utils import median, geometric_mean

logger = logging.getLogger()


class TalosTest(PerfTest):
    def __init__(self, dm=None, phone=None, options=None,
                 config_file=None, chunk=1, repos=[]):

        PerfTest.__init__(self, dm=dm, phone=phone, options=options,
                          config_file=config_file, chunk=chunk, repos=repos)

        self._test_args = {}
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
                tpname = self._tests[test_name]
                tpargs = self.cfg.get('settings', 'tpargs')
                manifest_root = "file:%s" % self._paths['dest']
                tppath = os.path.join(manifest_root, tpname)
                extra_args = "-tp %s.develop %s" % (tppath, tpargs)

                try:
                    for source in self._paths['sources']:
                        fname = os.path.join(self.autophone_directory,
                                             source, tpname)
                        if not os.path.exists(fname):
                            continue

                        with open(fname, 'r') as fHandle:
                            lines = fHandle.readlines()

                        if test_location == 'remote':
                            manifest_root = options.webserver_url

                        manifest = "%s.develop" % fname
                        with open(manifest, 'w') as fHandle:
                            for line in lines:
                                fHandle.write(line.replace('%webserver%',
                                                           manifest_root))
                        self._pushes[manifest] = "%s.develop" % self._pushes[fname]

                except Exception:
                    pass

                self.loggerdeco.debug(
                    'test_location: %s, test_name: %s, test_path: %s, '
                    'test: %s, extra_args: %s' %
                    (test_location, test_name, test_path,
                     self._tests[test_name], extra_args))
                self._test_args["%s-%s" % (test_location, test_name)] = extra_args

    @property
    def name(self):
        return 'autophone-talos%s' % self.name_suffix

    def run_job(self):
        is_test_completed = False
        custom_addons = ['pageloader.xpi']

        if not self.install_local_pages():
            self.test_failure(
                self.name, 'TEST_UNEXPECTED_FAIL',
                'Aborting test - Could not install local pages on phone.',
                PhoneTestResult.EXCEPTION)
            return is_test_completed

        if not self.create_profile(custom_addons=custom_addons):
            self.test_failure(
                self.name, 'TEST_UNEXPECTED_FAIL',
                'Aborting test - Could not run Fennec.',
                PhoneTestResult.BUSTED)
            return is_test_completed

        is_test_completed = True
        testcount = len(self._test_args.keys())
        test_items = enumerate(self._test_args.iteritems(), 1)
        for testnum, (testname, test_args) in test_items:
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

            if self.fennec_crashed:
                break
            # dataset is a list of the measurements made for the
            # iterations for this test.
            #
            # An empty item in the dataset list represents a
            # failure to obtain any measurement for that
            # iteration.

            dataset = []
            for iteration in range(1, self._iterations+1):
                command = self.worker_subprocess.process_autophone_cmd(
                    test=self, require_ip_address=testname.startswith('remote'))
                if command['interrupt']:
                    is_test_completed = False
                    self.handle_test_interrupt(command['reason'],
                                               command['test_result'])
                    break
                if self.fennec_crashed:
                    break

                self.update_status(message='Test %d/%d, '
                                   'run %d, for test_args %s' %
                                   (testnum, testcount, iteration, test_args))

                if not self.create_profile(custom_addons=custom_addons):
                    self.test_failure(test_args,
                                      'TEST_UNEXPECTED_FAIL',
                                      'Failed to create profile',
                                      PhoneTestResult.TESTFAILED)
                    continue

                measurement = self.runtest(test_args)
                if measurement:
                    if not self.perfherder_artifact:
                        self.perfherder_artifact = PerfherderArtifact()
                    suite = self.create_suite(measurement['pageload_metric'],
                                             testname)
                    self.perfherder_artifact.add_suite(suite)
                    self.test_pass(test_args)
                else:
                    self.test_failure(
                        test_args,
                        'TEST_UNEXPECTED_FAIL',
                        'Failed to get measurement.',
                        PhoneTestResult.TESTFAILED)
                    continue
                success = True

            if not success:
                # If we have not gotten a single measurement at this point,
                # just bail and report the failure rather than wasting time
                # continuing more attempts.
                self.loggerdeco.info(
                    'Failed to get measurements for test %s after '
                    '%d iterations' % (testname, self._iterations))
                self.worker_subprocess.mailer.send(
                    '%s %s failed for Build %s %s on Phone %s' %
                    (self.__class__.__name__, testname, self.build.tree, self.build.id,
                     self.phone.id),
                    'No measurements were detected for test %s.\n\n'
                    'Job        %s\n'
                    'Phone      %s\n'
                    'Repository %s\n'
                    'Build      %s\n'
                    'Revision   %s\n' %
                    (testname,
                     self.job_url,
                     self.phone.id,
                     self.build.tree,
                     self.build.id,
                     self.build.revision))
                self.test_failure(self.name, 'TEST_UNEXPECTED_FAIL',
                                  'No measurements detected.',
                                  PhoneTestResult.BUSTED)

                self.loggerdeco.debug('publishing results')

            if command and command['interrupt']:
                break
            elif not success:
                break

        return is_test_completed

    def runtest(self, extra_args):
        # Clear logcat
        self.logcat.clear()

        # Run test
        self.run_fennec_with_profile(self.build.app_name, "",
                                     extra_args=extra_args.split(' '))

        # Get results - do this now so we don't have as much to
        # parse in logcat.
        pageload_metric = self.analyze_logcat()

        # Ensure we succeeded - no 0's reported
        datapoint = {}
        if pageload_metric['summary'] != 0:
            datapoint = {'pageload_metric': pageload_metric}
        return datapoint

    def analyze_logcat(self):
        """
I/GeckoDump( 2284): __start_tp_report
I/GeckoDump( 2284): _x_x_mozilla_page_load
I/GeckoDump( 2284): _x_x_mozilla_page_load_details
I/GeckoDump( 2284): |i|pagename|runs|
I/GeckoDump( 2284): |0;amazon.com/www.amazon.com/index.html;2386;1146
I/GeckoDump( 2284): |1;m.yahoo.co.jp/www.yahoo.co.jp/index.html;1724;901
I/GeckoDump( 2284): |2;m.accuweather.com/www.accuweather.com/index.html;228;231
I/GeckoDump( 2284): |3;m.yandex.ru/www.yandex.ru/index.html;6043;2984
I/GeckoDump( 2284): |4;m.wikipedia.com/en.m.wikipedia.org/index.html;734;385
I/GeckoDump( 2284): |5;m.espn.com/m.espn.go.com/index.html;576;419
I/GeckoDump( 2284): |6;m.bbc.co.uk/www.bbc.co.uk/mobile/index.html;349;229
I/GeckoDump( 2284): __end_tp_report
I/GeckoDump( 2284): __start_cc_report
I/GeckoDump( 2284): _x_x_mozilla_cycle_collect,3390
I/GeckoDump( 2284): __end_cc_report
I/GeckoDump( 2284): __startTimestamp1433438438092__endTimestamp

        We will parse the syntax here and build up a {name:[value,],} hash.
        Next we will compute the median value for each name.
        Finally we will report the geoemtric mean of all of the median values.
        """
        self.loggerdeco.debug('analyzing logcat')

        re_page_data = re.compile('.*\|[0-9];([a-zA-Z0-9\.\/\-]+);([0-9;]+).*')
        re_end_report = re.compile('.*__end_tp_report.*')

        attempt = 1
        max_time = 90  # maximum time to wait for completeness score
        wait_time = 3  # time to wait between attempts
        max_attempts = max_time / wait_time

        results = {}
        pageload_metric = {'summary': 0}
        while attempt <= max_attempts and pageload_metric['summary'] == 0:
            buf = self.logcat.get()
            for line in buf:
                self.loggerdeco.debug('analyze_logcat: %s' % line)
                if re_end_report.match(line):
                    # calculate score
                    data = []
                    for page in results:
                        data.append(median(results[page]))
                        pageload_metric[page] = median(results[page])
                    pageload_metric['summary'] = geometric_mean(data)
                    break

                match = re_page_data.match(line)
                if match:
                    page_name = match.group(1)
                    numbers = match.group(2)
                    if page_name and numbers:
                        page_name = page_name.split('/')[0]
                        numbers = [float(x) for x in numbers.split(';')]
                        results[page_name] = numbers

            if self.fennec_crashed:
                # If fennec crashed, don't bother looking for pageload metric
                break
            if pageload_metric['summary'] == 0:
                sleep(wait_time)
                attempt += 1
        if pageload_metric['summary'] == 0:
            self.loggerdeco.warning('Unable to find pageload metric')

        return pageload_metric

    def create_suite(self, metric, testname):
        phsuite = PerfherderSuite(name=testname,
                                  value=metric['summary'])
        for p in metric:
            if p != 'summary':
                phsuite.add_subtest(p, metric[p])
        return phsuite
