# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import ConfigParser
import datetime
import json
import os
import posixpath
import re
import urllib2
from time import sleep

from mozprofile import FirefoxProfile

from phonetest import PhoneTest

class S1S2Test(PhoneTest):

    def runjob(self, job, worker_subprocess):
        # Read our config file which gives us our number of
        # iterations and urls that we will be testing
        for cache_enabled in (False, True):
            self.runtest(job, worker_subprocess, cache_enabled)

    def runtest(self, job, worker_subprocess, cache_enabled):
        # Read our config file which gives us our number of
        # iterations and urls that we will be testing
        self.prepare_phone(job, cache_enabled)

        intent = job['androidprocname'] + '/.App'

        # Initialize profile
        self.logger.debug('initializing profile...')
        self.run_fennec_with_profile(intent, self._initialize_url)
        if not self.wait_for_fennec(job):
            self.logger.info('%s: Failed to initialize profile for build %s' %
                             (self.phone_cfg['phoneid'], job['buildid']))
            self.set_status(msg='Failed to initialize profile for build %s' % job['buildid'])
            return

        for testnum,(testname,url) in enumerate(self._urls.iteritems(), 1):
            self.logger.info('%s: Running test %s (%d/%d) for %s iterations' %
                             (self.phone_cfg['phoneid'], testname, testnum,
                              len(self._urls.keys()), self._iterations))
            for i in range(self._iterations):
                success = False
                attempt = 0
                while not success and attempt < 3:
                    # Set status
                    self.set_status(msg='Test %d/%d, run %s, attempt %s for url %s' %
                            (testnum, len(self._urls.keys()), i, attempt, url))

                    # Clear logcat
                    self.logger.debug('clearing logcat')
                    self.dm.recordLogcat()
                    self.logger.debug('logcat cleared')

                    # Get start time
                    try:
                        starttime = self.dm.getInfo('uptimemillis')['uptimemillis'][0]
                    except IndexError:
                        # uptimemillis is not supported in all implementations
                        # therefore we can not exclude such cases.
                        starttime = 0

                    # Run test
                    self.logger.debug('running fennec')
                    self.run_fennec_with_profile(intent, url)

                    # Get results - do this now so we don't have as much to
                    # parse in logcat.
                    throbberstart, throbberstop = self.analyze_logcat(job)

                    self.wait_for_fennec(job)

                    # Get rid of the browser and session store files
                    self.logger.debug('removing sessionstore files')
                    self.remove_sessionstore_files()

                    # Ensure we succeeded - no 0's reported
                    if (throbberstart and throbberstop):
                        success = True
                    else:
                        attempt = attempt + 1

                # Publish results
                self.logger.debug('%s throbbers after %d attempts' %
                                  ('successfully got' if success else 'failed to get', attempt))
                if success:
                    self.logger.debug('publishing results')
                    self.publish_results(starttime=int(starttime),
                                         tstrt=throbberstart,
                                         tstop=throbberstop,
                                         job=job,
                                         testname=testname,
                                         cache_enabled=cache_enabled)

    def wait_for_fennec(self, job, max_wait_time=60, wait_time=5, kill_wait_time=20):
        # Wait for up to a max_wait_time seconds for fennec to close
        # itself in response to the quitter request. Check that fennec
        # is still running every wait_time seconds. If fennec doesn't
        # close on its own, attempt up to 3 times to kill fennec, waiting
        # kill_wait_time seconds between attempts.
        # Return True if fennec exits on its own, False if it needs to be killed.
        # Re-raise the last exception if fennec can not be killed.
        max_wait_attempts = max_wait_time / wait_time
        for wait_attempt in range(max_wait_attempts):
            if not self.dm.processExist(job['androidprocname']):
                return True
            sleep(wait_time)
        self.logger.debug('killing fennec')
        max_killattempts = 3
        for kill_attempt in range(max_killattempts):
            try:
                self.dm.killProcess(job['androidprocname'])
                break
            except DMError:
                self.logger.info('Attempt %d to kill fennec failed' % kill_attempt)
                if kill_attempt == max_killattempts - 1:
                    raise
                sleep(kill_wait_time)
        return False

    def prepare_phone(self, job, cache_enabled):
        telemetry_prompt = 999
        if job['blddate'] < '2013-01-03':
            telemetry_prompt = 2
        prefs = { 'browser.firstrun.show.localepicker': False,
                  'browser.sessionstore.resume_from_crash': False,
                  'browser.firstrun.show.uidiscovery': False,
                  'shell.checkDefaultClient': False,
                  'browser.warnOnQuit': False,
                  'browser.EULA.override': True,
                  'toolkit.telemetry.prompted': telemetry_prompt,
                  'toolkit.telemetry.notifiedOptOut': telemetry_prompt,
                  'browser.cache.disk.enable': cache_enabled,
                  'browser.cache.memory.enable': cache_enabled}
        profile = FirefoxProfile(preferences=prefs, addons='%s/xpi/quitter.xpi' %
                                 os.getcwd())
        self.install_profile(profile)
        self.dm.mkDir('/mnt/sdcard/s1test')

        testroot = '/mnt/sdcard/s1test'

        if not os.path.exists(self.config_file):
            self.logger.error('Cannot find config file: %s' % self.config_file)
            raise NameError('Cannot find config file: %s' % self.config_file)

        cfg = ConfigParser.RawConfigParser()
        cfg.read(self.config_file)

        # Map URLS - {urlname: url} - urlname serves as testname
        self._urls = {}
        for u in cfg.items('urls'):
            self._urls[u[0]] = u[1]

        # Move the local html files in htmlfiles onto the phone's sdcard
        # Copy our HTML files for local use into place
        # FIXME: Check for errors, use defined path for configs (e.g. config/)
        #        so that we can properly strip path root, instead of just
        #        always using basename.
        for h in cfg.items('htmlfiles'):
            if os.path.isdir(h[1]):
                self.dm.pushDir(h[1], posixpath.join(testroot,
                                                     os.path.basename(h[1])))
            else:
                self.dm.pushFile(h[1], posixpath.join(testroot,
                                                      os.path.basename(h[1])))

        self._iterations = cfg.getint('settings', 'iterations')
        self._resulturl = cfg.get('settings', 'resulturl')
        self._initialize_url = cfg.get('settings', 'initialize_url')

    def analyze_logcat(self, job):
        self.logger.debug('analyzing logcat')
        throbberstartRE = re.compile('.*Throbber start$')
        throbberstopRE = re.compile('.*Throbber stop$')
        throbstart = 0
        throbstop = 0
        attempt = 0
        max_time = 90 # maximum time to wait for throbbers
        wait_time = 3 # time to wait between attempts
        max_attempts = max_time / wait_time

        # Always do at least one analysis of the logcat output
        # after fennec stops running to make sure we have not missed
        # throbberstop.
        fennec_still_running = True
        while (fennec_still_running and
               attempt < max_attempts and (throbstart == 0 or throbstop == 0)):
            if not self.dm.processExist(job['androidprocname']):
                fennec_still_running = False
            buf = [x.strip() for x in self.dm.getLogcat()]
            for line in buf:
                # we want the first throbberstart and throbberstop.
                if throbberstartRE.match(line) and not throbstart:
                    throbstart = line.split(' ')[-4]
                elif throbberstopRE.match(line) and not throbstop:
                    throbstop = line.split(' ')[-4]
                if throbstart and throbstop:
                    break
            if fennec_still_running and (throbstart == 0 or throbstop == 0):
                sleep(wait_time)
                attempt += 1
        if self.check_for_crashes():
            self.logger.info('fennec crashed')
            fennec_crashed = True
        else:
            fennec_crashed = False
        if throbstart and throbstop == 0 and not fennec_crashed:
            throbstop = int(throbstart) + max_time * 1000

        return (int(throbstart), int(throbstop))

    def publish_results(self, starttime=0, tstrt=0, tstop=0, job=None, testname='', cache_enabled=True):
        msg = 'Start Time: %s Throbber Start: %s Throbber Stop: %s' % (starttime, tstrt, tstop)
        cache_msg = 'cached' if cache_enabled else 'not cached'
        print 'RESULTS (%s) %s %s:%s' % (cache_msg,
                                         self.phone_cfg['phoneid'],
                                         datetime.datetime.fromtimestamp(int(job['blddate'])),
                                         msg)
        self.logger.info('RESULTS (%s): %s:%s' % (cache_msg, self.phone_cfg['phoneid'], msg))

        # Create JSON to send to webserver
        resultdata = {}
        resultdata['phoneid'] = self.phone_cfg['phoneid']
        resultdata['testname'] = testname
        resultdata['starttime'] = starttime
        resultdata['throbberstart'] = tstrt
        resultdata['throbberstop'] = tstop
        resultdata['blddate'] = job['blddate']
        resultdata['cached'] = cache_enabled

        resultdata['revision'] = job['revision']
        resultdata['productname'] = job['androidprocname']
        resultdata['productversion'] = job['version']
        resultdata['osver'] = self.phone_cfg['osver']
        resultdata['bldtype'] = job['bldtype']
        resultdata['machineid'] = self.phone_cfg['machinetype']

        # Upload
        result = json.dumps({'data': resultdata})
        req = urllib2.Request(self._resulturl, result,
                              {'Content-Type': 'application/json'})
        try:
            f = urllib2.urlopen(req)
        except urllib2.URLError, e:
            try:
                self.logger.error('Could not send results to server: %s' %
                                  e.reason.strerror)
            except:
                self.logger.error('Could not send results to server: %s' %
                                  e.reason)
        else:
            f.read()
            f.close()
