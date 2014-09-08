# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import datetime
import logging
import os
from time import sleep

from mozprofile import FirefoxProfile

from logdecorator import LogDecorator
from phonetest import PhoneTest

class SmokeTest(PhoneTest):

    def run_job(self):
        logger = self.logger
        loggerdeco = self.loggerdeco
        self.logger = logging.getLogger('autophone.worker.subprocess.test')
        self.loggerdeco = LogDecorator(self.logger,
                                       {'phoneid': self.phone.id,
                                        'phoneip': self.dm.get_ip_address(),
                                        'buildid': self.build.id},
                                       '%(phoneid)s|%(phoneip)s|%(buildid)s|'
                                       '%(message)s')

        try:
            self.runtest()
        finally:
            self.logger = logger
            self.loggerdeco = loggerdeco

    def runtest(self):
        self.update_status(message='Running smoketest')
        pass_file = 'smoketest-%s-pass' % self.phone.id
        fail_file = 'smoketest-%s-fail' % self.phone.id

        try:
            os.unlink(pass_file)
        except OSError:
            pass
        try:
            os.unlink(fail_file)
        except OSError:
            pass

        # Read our config file which gives us our number of
        # iterations and urls that we will be testing
        self.prepare_phone()

        # Clear logcat
        self.dm.clear_logcat()

        # Run test
        self.loggerdeco.debug('running fennec')
        self.run_fennec_with_profile(self.build.app_name, 'about:fennec')

        fennec_launched = self.dm.process_exist(self.build.app_name)
        found_throbber = False
        start = datetime.datetime.now()
        while (not fennec_launched and (datetime.datetime.now() - start
                                        <= datetime.timedelta(seconds=60))):
            sleep(3)
            fennec_launched = self.dm.process_exist(self.build.app_name)

        if fennec_launched:
            found_throbber = self.check_throbber()
            while (not found_throbber and (datetime.datetime.now() - start
                                           <= datetime.timedelta(seconds=60))):
                sleep(3)
                found_throbber = self.check_throbber()

        if not fennec_launched:
            self.loggerdeco.error('smoketest: fail - failed to launch fennec')
            file(fail_file, 'w')
        elif not found_throbber:
            self.loggerdeco.error('smoketest: fail - failed to find Throbber')
            file(fail_file, 'w')
        else:
            self.loggerdeco.info('smoketest: pass - fennec successfully launched')
            file(pass_file, 'w')

        if fennec_launched:
            self.loggerdeco.debug('killing fennec')
            self.dm.pkill(self.build.app_name)

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
        buf = self.dm.get_logcat(filter_specs=['*:V'])

        for line in buf:
            line = line.strip()
            self.loggerdeco.debug('check_throbber: %s' % line)
            if 'Throbber stop' in line:
                return True
        return False

