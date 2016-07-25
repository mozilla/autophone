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
from utils import geometric_mean, host

logger = logging.getLogger()


class RoboTest(PerfTest):
    def __init__(self, dm=None, phone=None, options=None,
                 config_file=None, chunk=1, repos=[]):

        PerfTest.__init__(self, dm=dm, phone=phone, options=options,
                          config_file=config_file, chunk=chunk, repos=repos)

        self.enable_unittests = True
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
                test_url = ('am instrument -w -e deviceroot %s %s' %
                            (self._paths['dest'],
                             self.cfg.get('settings', 'tcheck_args')))

                self.loggerdeco.debug(
                    'test_location: %s, test_name: %s, test_path: %s, '
                    'test: %s, adb args: %s' %
                    (test_location, test_name, test_path,
                     self._tests[test_name], test_url))
                self._test_args["%s-%s" % (test_location, test_name)] = test_url

    @property
    def name(self):
        return 'autophone-talos%s' % self.name_suffix

    def create_profile(self):
        retVal = PerfTest.create_profile(self)

        config_file = os.path.join(self.build.dir, 'robotium.config')
        with open(config_file, 'w') as fHandle:
            fHandle.write("profile=%s\n" % self.profile_path)

            remoteLog = self._paths['dest'] + "/tcheck3.log"
            fHandle.write("logfile=%s\n" % remoteLog)
            fHandle.write("host=%s\n" % self.options.webserver_url)
            fHandle.write("rawhost=%s\n" % self.options.webserver_url)
            envstr = ""
            delim = ""
            # This is not foolproof and the ideal solution would be to have
            # one env/line instead of a single string
            env_vars = {
                'MOZ_CRASHREPORTER': 1,
                'MOZ_CRASHREPORTER_NO_REPORT': 1,
                'MOZ_CRASHREPORTER_SHUTDOWN': 1,
                'NO_EM_RESTART': 1,
                'MOZ_DISABLE_SWITCHBOARD': '1'
            }
#TODO: disabled until we have a single device and we can tweak the test for
#      network access
#                        'MOZ_DISABLE_NONLOCAL_CONNECTIONS': 1}
            for item in env_vars:
                envstr += "%s%s=%s" % (delim, item, env_vars[item])
                delim = ","
            fHandle.write("envvars=%s\n" % envstr)

        self.dm.push(config_file, self._paths['dest'])
        return retVal

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
            self.loggerdeco = self.loggerdeco.clone(
                extradict={'phoneid': self.phone.id,
                           'buildid': self.build.id,
                           'testname': testname},
                extraformat='%(phoneid)s|%(buildid)s|%(testname)s|%(message)s')
            self.dm._logger = self.loggerdeco
            self.loggerdeco.info('Running test (%d/%d) for %d iterations' %
                                 (testnum, testcount, self._iterations))

            command = None

            failures = 0
            measurements = []
            # We allow 1 failure, the second failure will abort the test.
            while (len(measurements) < self._iterations and failures < 2):
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
                                   (testnum, testcount, len(measurements), test_args))

                if not self.create_profile():
                    self.test_failure(test_args,
                                      'TEST_UNEXPECTED_FAIL',
                                      'Failed to create profile',
                                      PhoneTestResult.TESTFAILED)
                    break

                measurement = self.runtest(test_args)
                if measurement is None:
                    failures += 1
                else:
                    measurements.append(measurement)
                    self.test_pass(test_args)

            count = len(measurements)
            if count == self._iterations:
                # values are stored in an array, we need to submit one value though
                summary = geometric_mean(measurements[1:])
                phsuite = PerfherderSuite(name="tcheck3",
                                          value=summary,
                                          subtests=[{'name': 'tcheck3',
                                                    'value': summary}])
                self.perfherder_artifact = PerfherderArtifact(suites=[phsuite])
            else:
                if count == 0:
                    self.loggerdeco.info(
                        'No measurements for test %s after '
                        '%d iterations' % (testname, self._iterations))
                    self.worker_subprocess.mailer.send(
                        '%s %s failed for Build %s %s on %s %s' %
                        (self.__class__.__name__,
                         testname,
                         self.build.tree,
                         self.build.id,
                         host(),
                         self.phone.id),
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
                    self.test_failure(self.name, 'TEST_UNEXPECTED_FAIL',
                                      'No measurements detected. %s != %s' % (
                                          len(measurements), self._iterations),
                                      PhoneTestResult.BUSTED)
                else:
                    # If we do not have enough, then we failed.
                    self.loggerdeco.info(
                        'Failed to get enough measurements for test %s after '
                        '%d iterations' % (testname, self._iterations))
                    self.test_failure(self.name, 'TEST_UNEXPECTED_FAIL',
                                      'Not enough measurements collected %s != %s' % (
                                          len(measurements), self._iterations),
                                      PhoneTestResult.TESTFAILED)

                    self.loggerdeco.debug('publishing results')
                break

        return is_test_completed

    def runtest(self, test_args):
        # Clear logcat
        self.logcat.clear()

        try:
            self.dm.uninstall_app('org.mozilla.roboexample.test')
            robocop_apk_path = os.path.join(self.build.dir, 'robocop.apk')
            self.dm.install_app(robocop_apk_path)
        except:
            self.loggerdeco.exception('robotest.py:runtest: \
                                       Exception installing robocop.apk.')
            return {}

        # Run test
        self.dm.shell_output(test_args)

        # Get results - do this now so we don't have as much to
        # parse in logcat.
        return self.analyze_logcat()

    def analyze_logcat(self):
        """
        __start_report12.853116__end_report

        We will parse the above line and pull out 12.853116
        """
        self.loggerdeco.debug('analyzing logcat')

        re_data = re.compile('.*__start_report([0-9\.]+)__end_report.*')

        attempt = 1
        max_time = 90  # maximum time to wait for completeness score
        wait_time = 3  # time to wait between attempts
        max_attempts = max_time / wait_time

        results = None
        while attempt <= max_attempts and results is None:
            buf = self.logcat.get()
            for line in buf:
                match = re_data.match(line)
                if match:
                    numbers = match.group(1)
                    if numbers:
                        results = float(numbers)

            if self.fennec_crashed:
                # If fennec crashed, don't bother looking for pageload metric
                break
            if results is None:
                sleep(wait_time)
                attempt += 1

        if results is None:
            self.loggerdeco.info('Unable to find pageload metric')

        self.loggerdeco.info("returning from logcat analyze with: %s" %
                             results)
        return results
