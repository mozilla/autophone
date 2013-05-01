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
from math import sqrt
from time import sleep

from logdecorator import LogDecorator
from mozdevice import DMError
from mozprofile import FirefoxProfile
from phonetest import PhoneTest

# STDERRP_THRESHOLD is the target maximum percentage standard error of
# the mean. We will continue to collect results until all of the
# measurement's percentage standard errors of the mean are below this
# value or until the number of iterations has been exceeded.
STDERRP_THRESHOLD = 1.0

def get_stats(values):
    """Calculate and return an object containing the count, mean,
    standard deviation, standard error of the mean and percentage
    standard error of the mean of the values list."""
    r = {'count': len(values)}
    if r['count'] == 1:
        r['mean'] = values[0]
        r['stddev'] = 0
        r['stderr'] = 0
        r['stderrp'] = 0
    else:
        r['mean'] = sum(values) / float(r['count'])
        r['stddev'] = sqrt(sum([(value - r['mean'])**2 for value in values])/float(r['count']-1))
        r['stderr'] = r['stddev']/sqrt(r['count'])
        r['stderrp'] = 100.0*r['stderr']/float(r['mean'])
    return r

def is_stderr_acceptable(dataset):
    """Return True if all of the measurements in the dataset have
    standard errors of the mean below the threshold."""

    for cachekey in ('uncached', 'cached'):
        for measurement in ('throbberstart', 'throbberstop', 'throbbertime'):
            stats = get_stats(dataset[cachekey][measurement])
            if stats['count'] == 1 or stats['stderrp'] >= STDERRP_THRESHOLD:
                return False
    return True

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
            self.runtests(build_metadata, worker_subprocess)
        finally:
            self.logger = logger
            self.loggerdeco = loggerdeco

    def runtests(self, build_metadata, worker_subprocess):
        self.loggerdeco = LogDecorator(self.logger,
                                       {'phoneid': self.phone_cfg['phoneid'],
                                        'phoneip': self.phone_cfg['ip'],
                                        'buildid': build_metadata['buildid']},
                                       '%(phoneid)s|%(phoneip)s|%(buildid)s|'
                                       '%(message)s')
        appname = build_metadata['androidprocname']

        # Attempt to launch fennec and load the initialize url
        # to see if we can actually test this build. Try up to
        # 2 times, otherwise abort the test.
        success = False
        self.prepare_phone(build_metadata)
        for attempt in range(2):
            self.loggerdeco.debug('Checking if fennec is runnable...')
            self.run_fennec_with_profile(appname, self._initialize_url)
            if self.wait_for_fennec(build_metadata):
                success = True
                break
            self.loggerdeco.info('%s: Attempt %d failed to run fennec for build %s' %
                                 (self.phone_cfg['phoneid'],
                                  attempt,
                                  build_metadata['buildid']))
            self.set_status(msg='Attempt %d failed to run fennec for build %s' %
                            (attempt,
                             build_metadata['buildid']))
        if not success:
            self.loggerdeco.info('%s: Could not run Fennec. Aborting test for '
                                 'build %s' %
                                 (self.phone_cfg['phoneid'],
                                  build_metadata['buildid']))
            self.set_status(msg='Could not run Fennec. Aborting test for '
                            'build %s' % build_metadata['buildid'])
            return

        testcount = len(self._urls.keys())
        for testnum,(testname,url) in enumerate(self._urls.iteritems(), 1):
            self.loggerdeco = LogDecorator(self.logger,
                                           {'phoneid': self.phone_cfg['phoneid'],
                                            'phoneip': self.phone_cfg['ip'],
                                            'buildid': build_metadata['buildid'],
                                            'testname': testname},
                                           '%(phoneid)s|%(phoneip)s|%(buildid)s|'
                                           '%(testname)s|%(message)s')
            self.loggerdeco.info('Running test (%d/%d) for %d iterations' %
                                 (testnum, len(self._urls.keys()),
                                  self._iterations))

            self.loggerdeco.debug('Rebooting phone before test...')
            worker_subprocess.recover_phone()
            if worker_subprocess.has_error():
                return

            # Collect up to self._iterations measurements. Terminate
            # measurements early if the percentage error of the mean
            # of all measurements is below the threshold.
            dataset = {
                'uncached': {
                    'throbberstart' : [],
                    'throbberstop' : [],
                    'throbbertime' : []
                    },
                'cached': {
                    'throbberstart' : [],
                    'throbberstop' : [],
                    'throbbertime' : []
                    }
                }
            iteration = 1
            attempt = 0
            while iteration <= self._iterations:
                attempt += 1
                data = {
                    'uncached': {
                        'starttime' : 0, 'throbberstart' : 0, 'throbberstop' : 0
                        },
                    'cached': {
                        'starttime' : 0, 'throbberstart' : 0, 'throbberstop' : 0
                        }
                    }
                self.prepare_phone(build_metadata)

                # Initialize profile
                success = False
                for initialize_attempt in range(2):
                    self.loggerdeco.debug('initializing profile...')
                    self.run_fennec_with_profile(appname, self._initialize_url)
                    if self.wait_for_fennec(build_metadata):
                        success = True
                        break
                    sleep(5)
                if not success:
                    self.loggerdeco.info('%s: Failed to initialize profile for build %s' %
                                         (self.phone_cfg['phoneid'],
                                          build_metadata['buildid']))
                    self.set_status(msg='Failed to initialize profile for build %s' %
                                    build_metadata['buildid'])
                    return

                if not self.runtest(build_metadata, appname, data, 'uncached',
                                    testname, testnum, testcount, iteration,
                                    attempt, url):
                    continue
                if self.runtest(build_metadata, appname, data, 'cached',
                                testname, testnum, testcount, iteration,
                                attempt, url):
                    self.loggerdeco.debug('publishing results')
                    for cachekey in ('uncached', 'cached'):
                        self.publish_results(starttime=data[cachekey]['starttime'],
                                             tstrt=data[cachekey]['throbberstart'],
                                             tstop=data[cachekey]['throbberstop'],
                                             build_metadata=build_metadata,
                                             testname=testname,
                                             cache_enabled=(cachekey=='cached'))
                        for measurement in ('throbberstart', 'throbberstop'):
                            dataset[cachekey][measurement].append(
                                data[cachekey][measurement] - data[cachekey]['starttime'])
                        dataset[cachekey]['throbbertime'].append(data[cachekey]['throbberstop'] -
                                                                 data[cachekey]['throbberstart'])
                    if is_stderr_acceptable(dataset):
                        break
                    iteration += 1
                    attempt = 0

    def runtest(self, build_metadata, appname, data, cachekey, testname, testnum,
                testcount, iteration, attempt, url):
        # Set status
        self.set_status(msg='Test %d/%d, %s run %d, attempt %d for url %s' %
                        (testnum, testcount, cachekey, iteration, attempt, url))

        # Clear logcat
        self.loggerdeco.debug('clearing logcat')
        self.dm.recordLogcat()
        self.loggerdeco.debug('logcat cleared')
        self.loggerdeco.debug('running fennec')

        # Get start time
        try:
            starttime = int(self.dm.getInfo('uptimemillis')['uptimemillis'][0])
        except IndexError:
            # uptimemillis is not supported in all implementations
            # therefore we can not exclude such cases.
            starttime = 0

        # Run test
        self.run_fennec_with_profile(appname, url)

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
            self.loggerdeco.debug('Successful %s throbber '
                                  'measurement run %d attempt %d' %
                                  (cachekey, iteration, attempt))
            data[cachekey]['starttime'] = int(starttime)
            data[cachekey]['throbberstart'] = throbberstart
            data[cachekey]['throbberstop'] = throbberstop
            return True
        return False

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

    def prepare_phone(self, build_metadata, custom_prefs=None):
        telemetry_prompt = 999
        if build_metadata['blddate'] < '2013-01-03':
            telemetry_prompt = 2
        prefs = {
            'browser.firstrun.show.localepicker': False,
            'browser.sessionstore.resume_from_crash': False,
            'dom.ipc.plugins.flash.subprocess.crashreporter.enabled': False,
            'browser.firstrun.show.uidiscovery': False,
            'shell.checkDefaultClient': False,
            'browser.warnOnQuit': False,
            'browser.EULA.override': True,
            'toolkit.telemetry.prompted': telemetry_prompt,
            'toolkit.telemetry.notifiedOptOut': telemetry_prompt
            }
        if isinstance(custom_prefs, dict):
            prefs = dict(prefs.items() + custom_prefs.items())
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
        msg = ('Cached: %s Start Time: %s Throbber Start: %s Throbber Stop: %s '
               'Total Throbber Time %s' % (
                cache_enabled, starttime, tstrt, tstop, tstop - tstrt))
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
