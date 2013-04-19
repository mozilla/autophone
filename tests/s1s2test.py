# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import ConfigParser
import json
import jwt
import logging
import os
import posixpath
import re
import urllib2
from time import sleep

from logdecorator import LogDecorator
from mozdevice import DMError
from mozprofile import FirefoxProfile
from phonetest import PhoneTest

class S1S2Test(PhoneTest):

    def runjob(self, build_metadata, worker_subprocess):
        logger = self.logger
        loggerdeco = self.loggerdeco
        self.logger = logging.getLogger('autophone.worker.subprocess.test')
        self.loggerdeco = LogDecorator(self.logger,
                                       {'phoneid': self.phone_cfg['phoneid'],
                                        'phoneip': self.phone_cfg['ip'],
                                        'buildid': build_metadata['buildid']},
                                       '%(phoneid)s|%(phoneip)s|%(buildid)s|'
                                       '%(message)s')

        try:
            # Read our config file which gives us our number of
            # iterations and urls that we will be testing
            cfg = ConfigParser.RawConfigParser()
            cfg.read(self.config_file)
            self._signer = None
            self._jwt = {'id': '', 'key': None}
            for opt in self._jwt.keys():
                try:
                    self._jwt[opt] = cfg.get('signature', opt)
                except (ConfigParser.NoSectionError,
                        ConfigParser.NoOptionError):
                    break
            # phonedash requires both an id and a key.
            if self._jwt['id'] and self._jwt['key']:
                self._signer = jwt.jws.HmacSha(key=self._jwt['key'],
                                               key_id=self._jwt['id'])
            self._iterations = cfg.getint('settings', 'iterations')
            self._resulturl = cfg.get('settings', 'resulturl')
            if self._resulturl[-1] != '/':
                self._resulturl += '/'
            self._initialize_url = cfg.get('settings', 'initialize_url')
            self.clear_results(build_metadata)
            for cache_enabled in (False, True):
                self.runtest(build_metadata, worker_subprocess, cache_enabled)
        finally:
            self.logger = logger
            self.loggerdeco = loggerdeco

    def runtest(self, build_metadata, worker_subprocess, cache_enabled):
        self.loggerdeco = LogDecorator(self.logger,
                                       {'phoneid': self.phone_cfg['phoneid'],
                                        'phoneip': self.phone_cfg['ip'],
                                        'buildid': build_metadata['buildid'],
                                        'cache_enabled': cache_enabled},
                                       '%(phoneid)s|%(phoneip)s|%(buildid)s|'
                                       'Cache: %(cache_enabled)s|%(message)s')
        # Read our config file which gives us our number of
        # iterations and urls that we will be testing
        self.prepare_phone(build_metadata, cache_enabled)

        intent = build_metadata['androidprocname'] + '/.App'

        # Initialize profile
        self.loggerdeco.debug('initializing profile...')
        self.run_fennec_with_profile(intent, self._initialize_url)
        if not self.wait_for_fennec(build_metadata):
            self.loggerdeco.info('%s: Failed to initialize profile for build %s' %
                                 (self.phone_cfg['phoneid'],
                                  build_metadata['buildid']))
            self.set_status(msg='Failed to initialize profile for build %s' %
                            build_metadata['buildid'])
            return

        for testnum,(testname,url) in enumerate(self._urls.iteritems(), 1):
            self.loggerdeco = LogDecorator(self.logger,
                                           {'phoneid': self.phone_cfg['phoneid'],
                                            'phoneip': self.phone_cfg['ip'],
                                            'buildid': build_metadata['buildid'],
                                            'cache_enabled': cache_enabled,
                                            'testname': testname},
                                           '%(phoneid)s|%(phoneip)s|%(buildid)s|'
                                           'Cache: %(cache_enabled)s|'
                                           '%(testname)s|%(message)s')
            self.loggerdeco.info('Running test (%d/%d) for %d iterations' %
                                 (testnum, len(self._urls.keys()),
                                  self._iterations))
            for i in range(self._iterations):
                success = False
                attempt = 0
                while not success and attempt < 3:
                    # Set status
                    self.set_status(msg='Test %d/%d, run %d, attempt %d for url %s' %
                                    (testnum, len(self._urls.keys()), i, attempt,
                                     url))

                    # Clear logcat
                    self.loggerdeco.debug('clearing logcat')
                    self.dm.recordLogcat()
                    self.loggerdeco.debug('logcat cleared')

                    # Get start time
                    try:
                        starttime = self.dm.getInfo('uptimemillis')['uptimemillis'][0]
                    except IndexError:
                        # uptimemillis is not supported in all implementations
                        # therefore we can not exclude such cases.
                        starttime = 0

                    # Run test
                    self.loggerdeco.debug('running fennec')
                    self.run_fennec_with_profile(intent, url)

                    # Get results - do this now so we don't have as much to
                    # parse in logcat.
                    throbberstart, throbberstop = self.analyze_logcat(
                        build_metadata)

                    self.wait_for_fennec(build_metadata)

                    # Get rid of the browser and session store files
                    self.loggerdeco.debug('removing sessionstore files')
                    self.remove_sessionstore_files()

                    # Ensure we succeeded - no 0's reported
                    if (throbberstart and throbberstop):
                        success = True
                    else:
                        attempt = attempt + 1

                # Publish results
                self.loggerdeco.debug('%s throbbers after %d attempts' %
                                      ('successfully got' if success else
                                       'failed to get', attempt))
                if success:
                    self.loggerdeco.debug('publishing results')
                    self.publish_results(starttime=int(starttime),
                                         tstrt=throbberstart,
                                         tstop=throbberstop,
                                         build_metadata=build_metadata,
                                         testname=testname,
                                         cache_enabled=cache_enabled)

    def wait_for_fennec(self, build_metadata, max_wait_time=60, wait_time=5,
                        kill_wait_time=20):
        # Wait for up to a max_wait_time seconds for fennec to close
        # itself in response to the quitter request. Check that fennec
        # is still running every wait_time seconds. If fennec doesn't
        # close on its own, attempt up to 3 times to kill fennec, waiting
        # kill_wait_time seconds between attempts.
        # Return True if fennec exits on its own, False if it needs to be killed.
        # Re-raise the last exception if fennec can not be killed.
        max_wait_attempts = max_wait_time / wait_time
        for wait_attempt in range(max_wait_attempts):
            if not self.dm.processExist(build_metadata['androidprocname']):
                return True
            sleep(wait_time)
        self.loggerdeco.debug('killing fennec')
        max_killattempts = 3
        for kill_attempt in range(max_killattempts):
            try:
                self.dm.killProcess(build_metadata['androidprocname'])
                break
            except DMError:
                self.loggerdeco.info('Attempt %d to kill fennec failed' %
                                     kill_attempt)
                if kill_attempt == max_killattempts - 1:
                    raise
                sleep(kill_wait_time)
        return False

    def prepare_phone(self, build_metadata, cache_enabled):
        telemetry_prompt = 999
        if build_metadata['blddate'] < '2013-01-03':
            telemetry_prompt = 2
        prefs = { 'browser.firstrun.show.localepicker': False,
                  'browser.sessionstore.resume_from_crash': False,
                  'dom.ipc.plugins.flash.subprocess.crashreporter.enabled': False,
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
            self.loggerdeco.error('Cannot find config file: %s' % self.config_file)
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

    def analyze_logcat(self, build_metadata):
        self.loggerdeco.debug('analyzing logcat')
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
            if not self.dm.processExist(build_metadata['androidprocname']):
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
            self.loggerdeco.info('fennec crashed')
            fennec_crashed = True
        else:
            fennec_crashed = False
        if throbstart and throbstop == 0 and not fennec_crashed:
            self.loggerdeco.info('Unable to find Throbber stop')

        return (int(throbstart), int(throbstop))

    def clear_results(self, build_metadata):
        data = json.dumps({'revision': build_metadata['revision'],
                           'bldtype': build_metadata['bldtype'],
                           'phoneid': self.phone_cfg['phoneid']})
        req = urllib2.Request(self._resulturl + 'delete/', data,
                              {'Content-Type': 'application/json'})
        try:
            urllib2.urlopen(req)
        except urllib2.URLError, e:
            self.loggerdeco.error('Could not clear previous results on server: %s'
                                  % e)

    def publish_results(self, starttime=0, tstrt=0, tstop=0,
                        build_metadata=None, testname='', cache_enabled=True):
        msg = ('Start Time: %s Throbber Start: %s Throbber Stop: %s '
               'Total Throbber Time %s' % (
                starttime, tstrt, tstop, tstop - tstrt))
        self.loggerdeco.info('RESULTS: %s' % msg)

        # Create JSON to send to webserver
        resultdata = {}
        resultdata['phoneid'] = self.phone_cfg['phoneid']
        resultdata['testname'] = testname
        resultdata['starttime'] = starttime
        resultdata['throbberstart'] = tstrt
        resultdata['throbberstop'] = tstop
        resultdata['blddate'] = build_metadata['blddate']
        resultdata['cached'] = cache_enabled

        resultdata['revision'] = build_metadata['revision']
        resultdata['productname'] = build_metadata['androidprocname']
        resultdata['productversion'] = build_metadata['version']
        resultdata['osver'] = self.phone_cfg['osver']
        resultdata['bldtype'] = build_metadata['bldtype']
        resultdata['machineid'] = self.phone_cfg['machinetype']

        result = {'data': resultdata}
        # Upload
        if self._signer:
            encoded_result = jwt.encode(result, signer=self._signer)
            content_type = 'application/jwt'
        else:
            encoded_result = json.dumps(result)
            content_type = 'application/json; charset=utf-8'
        req = urllib2.Request(self._resulturl + 'add/', encoded_result,
                              {'Content-Type': content_type})
        try:
            f = urllib2.urlopen(req)
        except urllib2.URLError, e:
            self.loggerdeco.error('Could not send results to server: %s' % e)
        else:
            f.read()
            f.close()
