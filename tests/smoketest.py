# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import datetime
from time import sleep

from mozprofile import FirefoxProfile

from autophonecrash import AutophoneCrashProcessor
from phonetest import PhoneTest, PhoneTestResult


class SmokeTest(PhoneTest):

    @property
    def name(self):
        return 'autophone-smoketest%s' % self.name_suffix

    def setup_job(self):
        PhoneTest.setup_job(self)
        self.crash_processor = AutophoneCrashProcessor(self.dm,
                                                       self.loggerdeco,
                                                       self.profile_path,
                                                       self.upload_dir)
        self.crash_processor.clear()

    def teardown_job(self):
        self.loggerdeco.debug('PerfTest.teardown_job')
        PhoneTest.teardown_job(self)

    def run_job(self):
        self.update_status(message='Running smoketest')

        # Read our config file which gives us our number of
        # iterations and urls that we will be testing
        self.prepare_phone()

        # Clear logcat
        self.logcat.clear()

        # Run test
        self.loggerdeco.debug('running fennec')
        self.run_fennec_with_profile(self.build.app_name, 'about:fennec')

        command = None
        fennec_launched = self.dm.process_exist(self.build.app_name)
        found_throbber = False
        start = datetime.datetime.now()
        while (not fennec_launched and (datetime.datetime.now() - start
                                        <= datetime.timedelta(seconds=60))):
            command = self.worker_subprocess.process_autophone_cmd(self)
            if command['interrupt']:
                break
            sleep(3)
            fennec_launched = self.dm.process_exist(self.build.app_name)

        if fennec_launched:
            found_throbber = self.check_throbber()
            while (not found_throbber and (datetime.datetime.now() - start
                                           <= datetime.timedelta(seconds=60))):
                command = self.worker_subprocess.process_autophone_cmd(self)
                if command['interrupt']:
                    break
                sleep(3)
                found_throbber = self.check_throbber()

        if command and command['interrupt']:
            self.handle_test_interrupt(command['reason'])
        if self.fennec_crashed:
            pass # Handle the crash in teardown_job
        elif not fennec_launched:
            self.test_result.status = PhoneTestResult.BUSTED
            self.message = 'Failed to launch Fennec'
            self.test_result.add_failure(self.name, 'TEST_UNEXPECTED_FAIL',
                                         self.message)
        elif not found_throbber:
            self.test_result.status = PhoneTestResult.TESTFAILED
            self.messaage = 'Failed to find Throbber'
            self.test_result.add_failure(self.name, 'TEST_UNEXPECTED_FAIL',
                                         self.message)
        else:
            self.test_result.add_pass(self.name)

        if fennec_launched:
            self.loggerdeco.debug('killing fennec')
            self.dm.pkill(self.build.app_name, root=True)

        self.loggerdeco.debug('removing sessionstore files')
        self.remove_sessionstore_files()

    def prepare_phone(self):
        prefs = { 'browser.firstrun.show.localepicker': False,
                  'browser.sessionstore.resume_from_crash': False,
                  'browser.firstrun.show.uidiscovery': False,
                  'shell.checkDefaultClient': False,
                  'browser.warnOnQuit': False,
                  'browser.EULA.override': True,
                  'toolkit.telemetry.prompted': 999,
                  'toolkit.telemetry.notifiedOptOut': 999 }
        profile = FirefoxProfile(preferences=prefs)
        self.install_profile(profile)

    def check_throbber(self):
        buf = self.logcat.get()

        for line in buf:
            line = line.strip()
            self.loggerdeco.debug('check_throbber: %s' % line)
            if 'Throbber stop' in line:
                return True
        return False

