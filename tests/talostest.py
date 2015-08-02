# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import ConfigParser
import glob
import logging
import os
import posixpath
import re
import sys
from time import sleep

from mozprofile import FirefoxProfile

from adb import ADBError
from logdecorator import LogDecorator
from perftest import PerfTest
from phonetest import PhoneTestResult
from utils import median, geometric_mean

logger = logging.getLogger()


class TalosTest(PerfTest):
    def __init__(self, dm=None, phone=None, options=None,
                 config_file=None, chunk=1, repos=[]):

        PerfTest.__init__(self, dm=dm, phone=phone, options=options,
                          config_file=config_file, chunk=chunk, repos=repos)

        # [paths]
        autophone_directory = os.path.dirname(os.path.abspath(sys.argv[0]))
        self._paths = {}
        self._paths['dest'] = posixpath.join(self.base_device_path, '%stest/' %
                                             self.type)
        try:
            sources = self.cfg.get('paths', 'sources').split()
            self._paths['sources'] = []
            for source in sources:
                if not source.endswith('/'):
                    source += '/'
                self._paths['sources'].append(source)
        except (ConfigParser.NoSectionError, ConfigParser.NoOptionError):
            self._paths['sources'] = [
                os.path.join(autophone_directory, 'files/base/')]
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
        # [tests]
        self._tests = {}
        try:
            for t in self.cfg.items('tests'):
                self._tests[t[0]] = t[1]
        except ConfigParser.NoSectionError:
            self._tests['blank'] = 'blank.html'

        self._test_args = {}
        config_vars = {'webserver_url': options.webserver_url}

        try:
            location_items = self.cfg.items('locations', False, config_vars)
        except ConfigParser.NoSectionError:
            location_items = [('local', None)]

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
                        fname = os.path.join(autophone_directory,
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
        self._initialize_url = os.path.join('file://', self._paths['dest'],
                                            'initialize_profile.html')

    @property
    def type(self):
        return 'talos'

    @property
    def name(self):
        return 'autophone-%s%s' % (self.type, self.name_suffix)

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
        testcount = len(self._test_args.keys())
        test_items = enumerate(self._test_args.iteritems(), 1)
        for testnum, (testname, test_args) in test_items:
            if self.fennec_crashed:
                break
            self.loggerdeco = LogDecorator(logger,
                                           {'phoneid': self.phone.id,
                                            'buildid': self.build.id,
                                            'testname': testname},
                                           '%(phoneid)s|%(buildid)s|'
                                           '%(testname)s|%(message)s')
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

                dataset.append({})

                if not self.create_profile():
                    self.test_failure(test_args,
                                      'TEST_UNEXPECTED_FAIL',
                                      'Failed to create profile',
                                      PhoneTestResult.TESTFAILED)
                    continue

                measurement = self.runtest(test_args)
                if measurement:
                    self.test_pass(test_args)
                else:
                    self.test_failure(
                        test_args,
                        'TEST_UNEXPECTED_FAIL',
                        'Failed to get measurement.',
                        PhoneTestResult.TESTFAILED)
                    continue
                dataset[-1] = measurement
                success = True

            if not success:
                # If we have not gotten a single measurement at this point,
                # just bail and report the failure rather than wasting time
                # continuing more attempts.
                self.loggerdeco.info(
                    'Failed to get measurements for test %s after '
                    '%d iterations' % (testname, self._iterations))
                self.worker_subprocess.mailer.send(
                    '%sTest %s failed for Build %s %s on Phone %s' %
                    (self.type, testname, self.build.tree, self.build.id,
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

                for datapoint in dataset:
                    for cachekey in datapoint:
                        pass
                        #TODO: figure out results reporting
#                        self.report_results(results)

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

    def create_profile(self, custom_prefs=None, root=True):
        if not custom_prefs:
            custom_prefs = {}

        self.dm.pkill(self.build.app_name, root=root)
        if isinstance(custom_prefs, dict):
            prefs = dict(self.preferences.items() + custom_prefs.items())
        else:
            prefs = self.preferences
        profile = FirefoxProfile(preferences=prefs,
                                 addons=['%s/xpi/quitter.xpi' % os.getcwd(),
                                         '%s/xpi/pageloader.xpi' % os.getcwd()])
        if not self.install_profile(profile):
            return False

        success = False
        for attempt in range(1, self.options.phone_retry_limit+1):
            self.loggerdeco.debug('Attempt %d Initializing profile' % attempt)
            self.run_fennec_with_profile(self.build.app_name,
                                         self._initialize_url)
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
        # Return True if fennec exits on its own, False to kill it.
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
            self.loggerdeco.debug('Attempt %d Installing local pages' %
                                  attempt)
            try:
                self.dm.rm(self._paths['dest'], recursive=True, force=True)
                self.dm.mkdir(self._paths['dest'], parents=True)
                for push_source in self._pushes:
                    push_dest = self._pushes[push_source]
                    self.dm.push(push_source, push_dest)
                success = True
                break
            except ADBError:
                self.loggerdeco.exception('Attempt %d Installing local pages' %
                                          attempt)
                sleep(self.options.phone_retry_wait)

        if not success:
            self.loggerdeco.error('Failure installing local pages')

        return success

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
            self.loggerdeco.info('Unable to find pageload metric')

        return pageload_metric
