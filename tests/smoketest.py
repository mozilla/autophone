# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import datetime
from time import sleep

from phonetest import PhoneTest, TreeherderStatus, TestStatus


class SmokeTest(PhoneTest):

    @property
    def name(self):
        return 'autophone-smoketest%s' % self.name_suffix

    def run_job(self):
        self.update_status(message='Running smoketest')

        # Clear logcat
        self.worker_subprocess.logcat.clear()

        is_test_completed = True

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

        # Run test
        self.loggerdeco.debug('running fennec')
        self.run_fennec_with_profile(self.build.app_name, 'about:fennec')

        command = None
        fennec_launched = self.dm.process_exist(self.build.app_name)
        found_throbber = False
        start = datetime.datetime.utcnow()
        while (not fennec_launched and (datetime.datetime.utcnow() - start
                                        <= datetime.timedelta(seconds=60))):
            command = self.worker_subprocess.process_autophone_cmd(test=self)
            if command['interrupt']:
                break
            sleep(3)
            fennec_launched = self.dm.process_exist(self.build.app_name)

        if fennec_launched:
            found_throbber = self.check_throbber()
            while (not found_throbber and (datetime.datetime.utcnow() - start
                                           <= datetime.timedelta(seconds=60))):
                command = self.worker_subprocess.process_autophone_cmd(test=self)
                if command['interrupt']:
                    break
                sleep(3)
                found_throbber = self.check_throbber()

        if command and command['interrupt']:
            is_test_completed = False
            self.handle_test_interrupt(command['reason'],
                                       command['test_result'])
        elif self.handle_crashes():
            pass # Handle the crash in teardown_job
        elif not fennec_launched:
            self.add_failure(
                self.name, TestStatus.TEST_UNEXPECTED_FAIL,
                'Failed to launch Fennec',
                TreeherderStatus.BUSTED)
        elif not found_throbber:
            self.add_failure(
                self.name, TestStatus.TEST_UNEXPECTED_FAIL,
                'Failed to find Throbber',
                TreeherderStatus.TESTFAILED)
        else:
            self.add_pass(self.name)

        if fennec_launched:
            self.loggerdeco.debug('killing fennec')
            self.dm.pkill(self.build.app_name, root=True)

        self.loggerdeco.debug('removing sessionstore files')
        self.remove_sessionstore_files()
        return is_test_completed

    def check_throbber(self):
        buf = self.worker_subprocess.logcat.get()

        for line in buf:
            line = line.strip()
            self.loggerdeco.debug('check_throbber: %s' % line)
            if 'Throbber stop' in line:
                return True
        return False

