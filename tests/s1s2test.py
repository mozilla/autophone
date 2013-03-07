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
        self.prepare_phone(job)

        intent = job['androidprocname'] + '/.App'

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
                        starttime = 0

                    # Run test
                    self.logger.debug('running fennec')
                    self.run_fennec_with_profile(intent, url)

                    # Let browser stabilize - this was 5s but that wasn't long
                    # enough for the device to stabilize on slow devices
                    sleep(10)

                    # Get results - do this now so we don't have as much to
                    # parse in logcat.
                    self.logger.debug('analyzing logcat')
                    throbberstart, throbberstop = self.analyze_logcat(job)

                    self.logger.debug('killing fennec')
                    # Get rid of the browser and session store files
                    self.dm.killProcess(job['androidprocname'])

                    self.logger.debug('removing sessionstore files')
                    self.remove_sessionstore_files()

                    # Ensure we succeeded - no 0's reported
                    if (throbberstart and
                        throbberstop and
                        starttime):
                        success = True
                    else:
                        attempt = attempt + 1

                # Publish results
                self.logger.debug('publishing results')
                self.publish_results(starttime=int(starttime),
                                     tstrt=throbberstart,
                                     tstop=throbberstop,
                                     job=job,
                                     testname=testname)

    def prepare_phone(self, job):
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
                  'toolkit.telemetry.notifiedOptOut': telemetry_prompt }
        profile = FirefoxProfile(preferences=prefs)
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

    def analyze_logcat(self, job):
        buf = [x.strip('\r\n') for x in self.dm.getLogcat()]
        throbberstartRE = re.compile('.*Throbber start$')
        throbberstopRE = re.compile('.*Throbber stop$')
        throbstart = 0
        throbstop = 0

        for line in buf:
            line = line.strip()
            # we want the first throbberstart and throbberstop.
            if throbberstartRE.match(line) and not throbstart:
                throbstart = line.split(' ')[-4]
            elif throbberstopRE.match(line) and not throbstop:
                throbstop = line.split(' ')[-4]
            if throbstart and throbstop:
                break
        return (int(throbstart), int(throbstop))

    def publish_results(self, starttime=0, tstrt=0, tstop=0, job=None, testname = ''):
        msg = 'Start Time: %s Throbber Start: %s Throbber Stop: %s' % (starttime, tstrt, tstop)
        print 'RESULTS %s %s:%s' % (self.phone_cfg['phoneid'], datetime.datetime.fromtimestamp(int(job['blddate'])), msg)
        self.logger.info('RESULTS: %s:%s' % (self.phone_cfg['phoneid'], msg))

        # Create JSON to send to webserver
        resultdata = {}
        resultdata['phoneid'] = self.phone_cfg['phoneid']
        resultdata['testname'] = testname
        resultdata['starttime'] = starttime
        resultdata['throbberstart'] = tstrt
        resultdata['throbberstop'] = tstop
        resultdata['blddate'] = job['blddate']

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
