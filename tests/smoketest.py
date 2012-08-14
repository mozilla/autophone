# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import datetime
import os
from time import sleep

from mozprofile import FirefoxProfile

from phonetest import PhoneTest


class SmokeTest(PhoneTest):

    def __init__(self, phone_cfg, config_file=None, status_cb=None):
        PhoneTest.__init__(self, phone_cfg, config_file, status_cb)

    def runjob(self, job):
        try:
            os.unlink('smoketest_pass')
        except OSError:
            pass
        try:
            os.unlink('smoketest_fail')
        except OSError:
            pass

        if 'androidprocname' not in job or \
                'revision' not in job or 'blddate' not in job or \
                'bldtype' not in job or 'version' not in job:
            self.logger.error('Invalid job configuration: %s' % job)
            raise NameError('ERROR: Invalid job configuration: %s' % job)

        # Read our config file which gives us our number of
        # iterations and urls that we will be testing
        self.prepare_phone(job)

        intent = job['androidprocname'] + '/.App'

        # Clear logcat
        self.dm.recordLogcat()

        # Run test
        self.logger.debug('running fennec')
        self.run_fennec_with_profile(intent, 'about:fennec')

        self.logger.debug('analyzing logcat...')
        fennec_launched = self.analyze_logcat(job)
        start = datetime.datetime.now()
        while (not fennec_launched and (datetime.datetime.now() - start
                                        <= datetime.timedelta(seconds=60))):
            sleep(3)
            fennec_launched = self.analyze_logcat(job)

        if fennec_launched:
            self.logger.info('fennec successfully launched')
            file('smoketest_pass', 'w')
        else:
            self.logger.error('failed to launch fennec')
            file('smoketest_fail', 'w')

        self.logger.debug('killing fennec')
        # Get rid of the browser and session store files
        self.dm.killProcess(job['androidprocname'])

        self.logger.debug('removing sessionstore files')
        self.remove_sessionstore_files()

    def prepare_phone(self, job):
        prefs = { 'browser.firstrun.show.localepicker': False,
                  'browser.sessionstore.resume_from_crash': False,
                  'browser.firstrun.show.uidiscovery': False,
                  'shell.checkDefaultClient': False,
                  'browser.warnOnQuit': False,
                  'browser.EULA.override': True,
                  'toolkit.telemetry.prompted': 2 }
        profile = FirefoxProfile(preferences=prefs)
        self.install_profile(profile)
 
    def analyze_logcat(self, job):
        buf = self.dm.getLogcat()
        got_start = False
        got_end = False

        for line in buf:
            if not got_start and 'Start proc org.mozilla.fennec' in line:
                got_start = True
            if not got_end and 'Throbber stop' in line:
                got_end = True
        return got_start and got_end

