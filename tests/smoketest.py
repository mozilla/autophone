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
                                        'phoneip': self.phone.ip, # XXX: not set!
                                        'buildid': self.build.id},
                                       '%(phoneid)s|%(phoneip)s|%(buildid)s|'
                                       '%(message)s')

        try:
            self.runtest()
        finally:
            self.logger = logger
            self.loggerdeco = loggerdeco

    def runtest(self):
        try:
            os.unlink('smoketest_pass')
        except OSError:
            pass
        try:
            os.unlink('smoketest_fail')
        except OSError:
            pass

        # Read our config file which gives us our number of
        # iterations and urls that we will be testing
        self.prepare_phone()

        appname = self.build.app_name

        # Clear logcat
        self.dm.recordLogcat()

        # Run test
        self.loggerdeco.debug('running fennec')
        self.run_fennec_with_profile(appname, 'about:fennec')

        self.loggerdeco.debug('analyzing logcat...')
        fennec_launched = self.analyze_logcat()
        start = datetime.datetime.now()
        while (not fennec_launched and (datetime.datetime.now() - start
                                        <= datetime.timedelta(seconds=60))):
            sleep(3)
            fennec_launched = self.analyze_logcat()

        if fennec_launched:
            self.loggerdeco.info('fennec successfully launched')
            file('smoketest_pass', 'w')
        else:
            self.loggerdeco.error('failed to launch fennec')
            file('smoketest_fail', 'w')

        self.loggerdeco.debug('killing fennec')
        # Get rid of the browser and session store files
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

    def analyze_logcat(self):
        buf = self.dm.get_logcat()
        got_start = False
        got_end = False

        for line in buf:
            if not got_start and 'Start proc org.mozilla.fennec' in line:
                got_start = True
            if not got_end and 'Throbber stop' in line:
                got_end = True
        return got_start and got_end

