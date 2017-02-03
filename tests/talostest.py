# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import ConfigParser
import os
import re
from time import sleep

from perftest import PerfTest, PerfherderArtifact, PerfherderSuite, PerfherderOptions
from phonetest import TreeherderStatus, TestStatus
from utils import median, geometric_mean, host


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
        tpargs = self.cfg.get('settings', 'tpargs')
        for test_location, test_path in location_items:
            if test_location in config_vars:
                # Ignore the pseudo-options which result from passing
                # the config_vars for interpolation.
                continue

            for test_name in self._tests:
                try:
                    manifest = self._tests[test_name]

                    if not os.path.exists(manifest):
                        self.loggerdeco.debug('ignoring manifest for %s', manifest)
                        continue

                    with open(manifest, 'r') as fHandle:
                        lines = fHandle.readlines()

                    manifest_path = os.path.dirname(manifest)
                    manifest_file = os.path.basename(manifest)
                    test_manifest_path = os.path.join(manifest_path, 'manifest')
                    test_manifest_file = "%s.%s" % (manifest_file, test_location)
                    test_manifest = os.path.join(test_manifest_path, test_manifest_file)

                    if not os.path.isdir(test_manifest_path):
                        os.mkdir(test_manifest_path)

                    if test_location == 'remote':
                        url_root = test_path
                    else:
                        url_root = 'file://' + self._paths['dest']

                    with open(test_manifest, 'w') as fHandle:
                        for line in lines:
                            fHandle.write(line.replace('%webserver%',
                                                       url_root))
                    dest_manifest = os.path.join(self._paths['dest'],
                                                 test_manifest_path,
                                                 test_manifest_file)
                    self._pushes[test_manifest] = dest_manifest

                    extra_args = "-tp file://%s %s" % (dest_manifest, tpargs)

                    self._test_args["%s-%s" % (test_location, test_name)] = extra_args

                    self.loggerdeco.debug(
                        'generating manifest: test_location: %s, test_path: %s, '
                        'test_name: %s, manifest: %s, extra_args: %s',
                        test_location, test_name, test_path, manifest,
                        extra_args)

                except Exception:
                    self.loggerdeco.exception(
                        'generating manifest: test_location: %s, test_path: %s, '
                        'test_name: %s, manifest: %s',
                        test_location, test_name, test_path, manifest)
                    raise

        self.loggerdeco.debug('TalosTest: %s', self.__dict__)


    @property
    def name(self):
        return 'autophone-talos%s' % self.name_suffix

    def run_job(self):
        is_test_completed = False
        custom_addons = ['pageloader.xpi']

        if not self.install_local_pages():
            self.add_failure(
                self.name, TestStatus.TEST_UNEXPECTED_FAIL,
                'Aborting test - Could not install local pages on phone.',
                TreeherderStatus.EXCEPTION)
            return is_test_completed

        if not self.create_profile(custom_addons=custom_addons):
            self.add_failure(
                self.name, TestStatus.TEST_UNEXPECTED_FAIL,
                'Aborting test - Could not run Fennec.',
                TreeherderStatus.BUSTED)
            return is_test_completed

        perfherder_options = PerfherderOptions(self.perfherder_options,
                                               self.build.tree)
        is_test_completed = True
        testcount = len(self._test_args.keys())
        test_items = enumerate(self._test_args.iteritems(), 1)
        for testnum, (testname, test_args) in test_items:
            self.loggerdeco = self.loggerdeco.clone(
                extradict={'phoneid': self.phone.id,
                           'buildid': self.build.id,
                           'testname': testname},
                extraformat='TalosTestJob|%(phoneid)s|%(buildid)s|%(testname)s|%(message)s')
            self.dm._logger = self.loggerdeco
            self.loggerdeco.info('Running test (%d/%d)',
                                 testnum, testcount)

            # success == False indicates that none of the attempts
            # were successful in getting any measurement. This is
            # typically due to a regression in the brower which should
            # be reported.
            success = False

            command = self.worker_subprocess.process_autophone_cmd(
                test=self, require_ip_address=testname.startswith('remote'))

            if command['interrupt']:
                is_test_completed = False
                self.handle_test_interrupt(command['reason'],
                                           command['test_result'])
                break

            self.update_status(message='Test %d/%d, for test_args %s' %
                               (testnum, testcount, test_args))

            if not self.create_profile(custom_addons=custom_addons):
                self.add_failure(
                    self.name,
                    TestStatus.TEST_UNEXPECTED_FAIL,
                    'Failed to create profile',
                    TreeherderStatus.TESTFAILED)
            else:
                measurement = self.runtest(test_args)
                if measurement:
                    if not self.perfherder_artifact:
                        self.perfherder_artifact = PerfherderArtifact()
                    suite = self.create_suite(measurement['pageload_metric'],
                                              testname,
                                              options=perfherder_options)
                    self.perfherder_artifact.add_suite(suite)
                    self.add_pass(test_args)
                    success = True
                else:
                    self.add_failure(
                        self.name,
                        TestStatus.TEST_UNEXPECTED_FAIL,
                        'Failed to get measurement.',
                        TreeherderStatus.TESTFAILED)

            if not success:
                # If we have not gotten a single measurement at this point,
                # just bail and report the failure rather than wasting time
                # continuing more attempts.
                self.loggerdeco.info(
                    'Failed to get measurements for test %s', testname)
                self.worker_subprocess.mailer.send(
                    '%s %s failed for Build %s %s on %s %s' %
                    (self.__class__.__name__, testname, self.build.tree,
                     self.build.id, host(), self.phone.id),
                    'No measurements were detected for test %s.\n\n'
                    'Job        %s\n'
                    'Host       %s\n'
                    'Phone      %s\n'
                    'Repository %s\n'
                    'Build      %s\n'
                    'Revision   %s\n' %
                    (testname,
                     self.job_url,
                     host(),
                     self.phone.id,
                     self.build.tree,
                     self.build.id,
                     self.build.changeset))
                self.add_failure(
                    self.name, TestStatus.TEST_UNEXPECTED_FAIL,
                    'No measurements detected.',
                    TreeherderStatus.BUSTED)

                self.loggerdeco.debug('publishing results')

            if command and command['interrupt']:
                break
            elif not success:
                break

        return is_test_completed

    def runtest(self, extra_args):
        # Clear logcat
        self.worker_subprocess.logcat.clear()

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

        re_page_data = re.compile(r'.*\|[0-9];([a-zA-Z0-9\.\/\-]+);([0-9;]+).*')
        re_end_report = re.compile(r'.*__end_tp_report.*')

        attempt = 1
        max_time = 180  # maximum time to wait for tp report
        wait_time = 3  # time to wait between attempts
        max_attempts = max_time / wait_time

        results = {}
        pageload_metric = {'summary': 0}
        while attempt <= max_attempts and pageload_metric['summary'] == 0:
            buf = self.worker_subprocess.logcat.get()
            for line in buf:
                self.loggerdeco.debug('analyze_logcat: %s', line)
                if re_end_report.match(line):
                    # calculate score
                    data = []
                    for page in results:
                        data.append(median(results[page]))
                        # median of each page, ignoring the first run
                        pageload_metric[page] = median(results[page][1:])
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

            if self.handle_crashes():
                # If fennec crashed, don't bother looking for pageload metric
                break
            if pageload_metric['summary'] == 0:
                sleep(wait_time)
                attempt += 1
        if pageload_metric['summary'] == 0:
            self.loggerdeco.warning('Unable to find pageload metric')

        return pageload_metric

    def create_suite(self, metric, testname, options=None):
        phsuite = PerfherderSuite(name=testname,
                                  value=metric['summary'],
                                  options=options)
        for p in metric:
            if p != 'summary':
                phsuite.add_subtest(p, metric[p], options=options)
        return phsuite
