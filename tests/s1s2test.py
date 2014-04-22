# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import ConfigParser
import datetime
import glob
import json
import jwt
import logging
import os
import posixpath
import re
import sys
import urllib
import urllib2
from math import sqrt
from time import sleep

from logdecorator import LogDecorator
from adb import ADBError
from mozprofile import FirefoxProfile
from options import *
from phonetest import PhoneTest

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
        r['stddev'] = sqrt(sum([(value - r['mean'])**2 for value in values])/float(r['count']-1.5))
        r['stderr'] = r['stddev']/sqrt(r['count'])
        r['stderrp'] = 100.0*r['stderr']/float(r['mean'])
    return r

class S1S2Test(PhoneTest):

    def runjob(self, build_metadata, worker_subprocess):
        logger = self.logger
        loggerdeco = self.loggerdeco
        self.logger = logging.getLogger('autophone.worker.subprocess.test')
        self.loggerdeco = LogDecorator(self.logger,
                                       {'phoneid': self.phone_cfg['phoneid'],
                                        'pid': os.getpid(),
                                        'buildid': build_metadata['buildid']},
                                       '%(phoneid)s|%(pid)s|%(buildid)s|'
                                       '%(message)s')
        self.dm._logger = self.loggerdeco

        try:
            # Read our config file which gives us our number of
            # iterations and urls that we will be testing
            cfg = ConfigParser.RawConfigParser()
            cfg.read(self.config_file)
            # [signature]
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
            # [paths]
            autophone_directory = os.path.dirname(os.path.abspath(sys.argv[0]))
            self._paths = {}
            self._paths['source'] = os.path.join(autophone_directory, 'files/')
            self._paths['dest'] = posixpath.join(self.base_device_path, 's1test/')
            self._paths['remote'] = 'http://phonedash.mozilla.org/'
            try:
                for opt in ('source', 'dest', 'profile', 'remote'):
                    try:
                        self._paths[opt] = cfg.get('paths', opt)
                        if not self._paths[opt].endswith('/'):
                            self._paths[opt] += '/'
                    except ConfigParser.NoOptionError:
                        pass
            except ConfigParser.NoSectionError:
                pass
            if 'profile' in self._paths:
                self.profile_path = self._paths['profile']
            # _pushes = {'sourcepath' : 'destpath', ...}
            self._pushes = {}
            for push in glob.glob(self._paths['source'] + '*'):
                if push.endswith('~') or push.endswith('.bak'):
                    continue
                push_dest = posixpath.join(self._paths['dest'], os.path.basename(push))
                self._pushes[push] = push_dest
            # [tests]
            self._tests = {}
            for t in cfg.items('tests'):
                self._tests[t[0]] = t[1]
            # Map URLS - {urlname: url} - urlname serves as testname
            self._urls = {}
            for test_location in ('local', 'remote'):
                for test_name in self._tests:
                    if test_location == 'local':
                        test_url = 'file://' + self._paths['dest'] + self._tests[test_name]
                    else:
                        test_url = self._paths['remote'] + self._tests[test_name]
                    self._urls["%s-%s" % (test_location, test_name)] = test_url
            # [settings]
            self._iterations = cfg.getint('settings', 'iterations')
            try:
                self.stderrp_accept = cfg.getfloat('settings', 'stderrp_accept')
            except ConfigParser.NoOptionError:
                self.stderrp_accept = 0
            try:
                self.stderrp_reject = cfg.getfloat('settings', 'stderrp_reject')
            except ConfigParser.NoOptionError:
                self.stderrp_reject = 100
            try:
                self.stderrp_attempts = cfg.getint('settings', 'stderrp_attempts')
            except ConfigParser.NoOptionError:
                self.stderrp_attempts = 1
            self._resulturl = cfg.get('settings', 'resulturl')
            if not self._resulturl.endswith('/'):
                self._resulturl += '/'
            self._initialize_url = 'file://' + self._paths['dest'] + 'initialize_profile.html'

            self.runtests(build_metadata, worker_subprocess)
        finally:
            self.logger = logger
            self.loggerdeco = loggerdeco
            self.dm._logger = loggerdeco

    def is_stderr_below_threshold(self, dataset, threshold):
        """Return True if all of the measurements in the dataset have
        standard errors of the mean below the threshold.

        Return False if at least one measurement is above the threshold
        or if one or more datasets have only one value.

        Return None if at least one measurement has no values.
        """

        self.loggerdeco.debug("is_stderr_below_threshold: %s" % dataset)

        for cachekey in ('uncached', 'cached'):
            for measurement in ('throbberstart', 'throbberstop'):
                data = [datapoint[cachekey][measurement] - datapoint[cachekey]['starttime']
                        for datapoint in dataset
                        if datapoint and cachekey in datapoint]
                if not data:
                    return None
                stats = get_stats(data)
                self.loggerdeco.debug('%s %s count: %d, mean: %.2f, '
                                      'stddev: %.2f, stderr: %.2f, '
                                      'stderrp: %.2f' % (
                                          cachekey, measurement,
                                          stats['count'], stats['mean'],
                                          stats['stddev'], stats['stderr'],
                                          stats['stderrp']))
                if stats['count'] == 1 or stats['stderrp'] >= threshold:
                    return False
        return True

    def runtests(self, build_metadata, worker_subprocess):
        self.loggerdeco = LogDecorator(self.logger,
                                       {'phoneid': self.phone_cfg['phoneid'],
                                        'pid': os.getpid(),
                                        'buildid': build_metadata['buildid']},
                                       '%(phoneid)s|%(pid)s|%(buildid)s|'
                                       '%(message)s')
        self.dm._logger = self.loggerdeco
        appname = build_metadata['androidprocname']

        if not self.install_local_pages():
            self.set_status(msg='Could not install local pages on phone. '
                            'Aborting test for '
                            'build %s' % build_metadata['buildid'])
            return

        if not self.create_profile(build_metadata):
            self.set_status(msg='Could not run Fennec. Aborting test for '
                            'build %s' % build_metadata['buildid'])
            return

        testcount = len(self._urls.keys())
        for testnum,(testname,url) in enumerate(self._urls.iteritems(), 1):
            self.loggerdeco = LogDecorator(self.logger,
                                           {'phoneid': self.phone_cfg['phoneid'],
                                            'pid': os.getpid(),
                                            'buildid': build_metadata['buildid'],
                                            'testname': testname},
                                           '%(phoneid)s|%(pid)s|%(buildid)s|'
                                           '%(testname)s|%(message)s')
            self.dm._logger = self.loggerdeco
            if self.check_results(build_metadata, testname):
                # We already have good results for this test and build.
                # No need to test it again.
                self.loggerdeco.info('Skipping test (%d/%d) for %d iterations' %
                                     (testnum, testcount, self._iterations))
                continue
            self.loggerdeco.info('Running test (%d/%d) for %d iterations' %
                                 (testnum, testcount, self._iterations))

            for attempt in range(self.stderrp_attempts):
                # dataset is a list of the measurements made for the
                # iterations for this test.
                #
                # An empty item in the dataset list represents a
                # failure to obtain any measurement for that
                # iteration.
                #
                # It is possible for an item in the dataset to have an
                # uncached value and not have a corresponding cached
                # value if the cached test failed to record the
                # values.

                dataset = []
                for iteration in range(self._iterations):
                    self.set_status(msg='Attempt %d/%d for Test %d/%d, '
                                    'run %d, for url %s' %
                                    (attempt+1, self.stderrp_attempts,
                                     testnum, testcount, iteration+1, url))

                    dataset.append({})

                    if not self.create_profile(build_metadata):
                        continue

                    measurement = self.runtest(build_metadata, appname, url)
                    if not measurement:
                        continue
                    dataset[-1]['uncached'] = measurement

                    measurement = self.runtest(build_metadata, appname, url)
                    if not measurement:
                        continue
                    dataset[-1]['cached'] = measurement

                    if self.is_stderr_below_threshold(dataset,
                                                      self.stderrp_accept):
                        self.loggerdeco.info(
                            'Accepted test (%d/%d) after %d of %d iterations' %
                            (testnum, testcount, iteration+1, self._iterations))
                        break

                self.loggerdeco.debug('publishing results')

                if self.is_stderr_below_threshold(dataset, self.stderrp_reject):
                    rejected = False
                else:
                    rejected = True
                    self.loggerdeco.info(
                        'Rejected test (%d/%d) after %d/%d iterations' %
                        (testnum, testcount, iteration+1, self._iterations))

                for datapoint in dataset:
                    for cachekey in datapoint:
                        self.publish_results(
                            starttime=datapoint[cachekey]['starttime'],
                            tstrt=datapoint[cachekey]['throbberstart'],
                            tstop=datapoint[cachekey]['throbberstop'],
                            build_metadata=build_metadata,
                            testname=testname,
                            cache_enabled=(cachekey=='cached'),
                            rejected=rejected)
                if not rejected:
                    break

    def runtest(self, build_metadata, appname, url):
        # Clear logcat
        self.dm.clear_logcat()

        # Run test
        self.run_fennec_with_profile(appname, url)

        # Get results - do this now so we don't have as much to
        # parse in logcat.
        starttime, throbberstart, throbberstop = self.analyze_logcat(
            build_metadata)

        self.wait_for_fennec(build_metadata)

        # Ensure we succeeded - no 0's reported
        datapoint = {}
        if (throbberstart and throbberstop):
            datapoint['starttime'] = starttime
            datapoint['throbberstart'] = throbberstart
            datapoint['throbberstop'] = throbberstop
            datapoint['throbbertime'] = throbberstop - throbberstart
        return datapoint

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
            if not self.dm.process_exist(build_metadata['androidprocname']):
                return True
            sleep(wait_time)
        self.loggerdeco.debug('killing fennec')
        max_killattempts = 3
        for kill_attempt in range(max_killattempts):
            try:
                self.dm.pkill(build_metadata['androidprocname'], root=True)
                break
            except ADBError:
                self.loggerdeco.exception('Attempt %d to kill fennec failed' %
                                          kill_attempt)
                if kill_attempt == max_killattempts - 1:
                    raise
                sleep(kill_wait_time)
        return False

    def create_profile(self, build_metadata, custom_prefs=None):
        # Create, install and initialize the profile to be
        # used in the test.

        # make sure firefox isn't running when we try to
        # install the profile.

        self.dm.pkill(build_metadata['androidprocname'], root=True)

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
            'toolkit.telemetry.notifiedOptOut': telemetry_prompt,
            'datareporting.healthreport.service.enabled': False,
            }
        if isinstance(custom_prefs, dict):
            prefs = dict(prefs.items() + custom_prefs.items())
        profile = FirefoxProfile(preferences=prefs, addons='%s/xpi/quitter.xpi' %
                                 os.getcwd())
        if not self.install_profile(profile):
            return False

        appname = build_metadata['androidprocname']
        buildid = build_metadata['buildid']
        success = False
        for attempt in range(self.user_cfg[PHONE_RETRY_LIMIT]):
            self.loggerdeco.debug('Attempt %d Initializing profile' % attempt)
            self.run_fennec_with_profile(appname, self._initialize_url)
            if self.wait_for_fennec(build_metadata):
                success = True
                break
            sleep(self.user_cfg[PHONE_RETRY_WAIT])

        if not success:
            msg = 'Failure initializing profile for build %s' % buildid
            self.loggerdeco.error(msg)
            self.set_status(msg=msg)

        return success

    def install_local_pages(self):
        success = False
        for attempt in range(self.user_cfg[PHONE_RETRY_LIMIT]):
            self.loggerdeco.debug('Attempt %d Installing local pages' % attempt)
            try:
                self.dm.rm(self._paths['dest'], recursive=True, force=True)
                self.dm.mkdir(self._paths['dest'], parents=True)
                for push_source in self._pushes:
                    push_dest = self._pushes[push_source]
                    if os.path.isdir(push_source):
                        self.dm.push(push_source, push_dest)
                    else:
                        self.dm.push(push_source, push_dest)
                success = True
                break
            except ADBError:
                self.loggerdeco.exception('Attempt %d Installing local pages' % attempt)
                sleep(self.user_cfg[PHONE_RETRY_WAIT])

        if not success:
            self.loggerdeco.error('Failure installing local pages')

        return success

    def is_fennec_running(self, appname):
        for attempt in range(self.user_cfg[PHONE_RETRY_LIMIT]):
            try:
                return self.dm.process_exist(appname)
            except ADBError:
                self.loggerdeco.exception('Attempt %d is fennec running' % attempt)
                if attempt == self.user_cfg[PHONE_RETRY_LIMIT] - 1:
                    raise
                sleep(self.user_cfg[PHONE_RETRY_WAIT])

    def get_logcat_throbbers(self):
        for attempt in range(self.user_cfg[PHONE_RETRY_LIMIT]):
            try:
                return [x.strip() for x in self.dm.get_logcat()]
            except ADBError:
                self.loggerdeco.exception('Attempt %d get logcat throbbers' % attempt)
                if attempt == self.user_cfg[PHONE_RETRY_LIMIT] - 1:
                    raise
                sleep(self.user_cfg[PHONE_RETRY_WAIT])

    def analyze_logcat(self, build_metadata):
        self.loggerdeco.debug('analyzing logcat')

        logcat_prefix = '(\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})'
        throbber_prefix = 'I/GeckoToolbarDisplayLayout.*zerdatime (\d+) - Throbber'
        re_base_time = re.compile('%s' % logcat_prefix)
        re_start_time = re.compile('%s .*(Gecko|fennec)' %
                                   logcat_prefix)
        re_throbber_start_time = re.compile('%s %s start' %
                                            (logcat_prefix, throbber_prefix))
        re_throbber_stop_time = re.compile('%s %s stop' %
                                           (logcat_prefix, throbber_prefix))

        base_time = 0
        start_time = 0
        throbber_start_time = 0
        throbber_stop_time = 0

        attempt = 0
        max_time = 90 # maximum time to wait for throbbers
        wait_time = 3 # time to wait between attempts
        max_attempts = max_time / wait_time

        while (attempt < max_attempts and (throbber_start_time == 0 or
                                           throbber_stop_time == 0)):
            buf = self.get_logcat_throbbers()
            for line in buf:
                self.loggerdeco.debug('analyze_logcat: %s' % line)
                match = re_base_time.match(line)
                if match and not base_time:
                    base_time = match.group(1)
                    self.loggerdeco.debug('analyze_logcat: base_time: %s' % base_time)
                # we want the first throbberstart and throbberstop.
                match = re_start_time.match(line)
                if match and not start_time:
                    start_time = match.group(1)
                    self.loggerdeco.debug('analyze_logcat: start_time: %s' % start_time)
                    continue
                match = re_throbber_start_time.match(line)
                if match and not throbber_start_time:
                    throbber_start_time = match.group(1)
                    self.loggerdeco.debug('analyze_logcat: throbber_start_time: %s' % throbber_start_time)
                    continue
                match = re_throbber_stop_time.match(line)
                if match and not throbber_stop_time:
                    throbber_stop_time = match.group(1)
                    self.loggerdeco.debug('analyze_logcat: throbber_stop_time: %s' % throbber_stop_time)
                    continue
                if start_time and throbber_start_time and throbber_stop_time:
                    break
            if (start_time == 0 or
                throbber_start_time == 0 or
                throbber_stop_time == 0):
                sleep(wait_time)
                attempt += 1
        if self.check_for_crashes():
            self.loggerdeco.info('fennec crashed')
            fennec_crashed = True
        else:
            fennec_crashed = False
        if throbber_start_time and throbber_stop_time == 0 and not fennec_crashed:
            self.loggerdeco.info('Unable to find Throbber stop')

        # The captured time from the logcat lines is in the format
        # MM-DD HH:MM:SS.mmm. It is possible for the year to change
        # between the different times, so we need to make adjustments
        # if necessary. First, we assume the year does not change and
        # parse the dates as if they are in the current year. If
        # the dates violate the natural order start_time,
        # throbber_start_time, throbber_stop_time, we can adjust the
        # year.

        if base_time and start_time and throbber_start_time and throbber_stop_time:
            parse = lambda y, t: datetime.datetime.strptime('%4d-%s' % (y, t), '%Y-%m-%d %H:%M:%S.%f')
            year = datetime.datetime.now().year
            base_time = parse(year, base_time)
            start_time = parse(year, start_time)
            throbber_start_time = parse(year, throbber_start_time)
            throbber_stop_time = parse(year, throbber_stop_time)

            self.loggerdeco.debug('analyze_logcat: before year adjustment '
                                  'base: %s, start: %s, '
                                  'throbber start: %s' %
                                  (base_time, start_time,
                                   throbber_start_time))

            if base_time > start_time:
                base_time.replace(year=year-1)
            elif start_time > throbber_start_time:
                base_time.replace(year=year-1)
                start_time.replace(year=year-1)
            elif throbber_start_time > throbber_stop_time:
                base_time.replace(year=year-1)
                start_time.replace(year=year-1)
                throbber_start_time.replace(year-1)

            self.loggerdeco.debug('analyze_logcat: after year adjustment '
                                  'base: %s, start: %s, '
                                  'throbber start: %s' %
                                  (base_time, start_time,
                                   throbber_start_time))

            # Convert the times to milliseconds from the base time.
            convert = lambda t1, t2: round((t2 - t1).total_seconds() * 1000.0)

            start_time = convert(base_time, start_time)
            throbber_start_time = convert(base_time, throbber_start_time)
            throbber_stop_time = convert(base_time, throbber_stop_time)

            self.loggerdeco.debug('analyze_logcat: base: %s, start: %s, '
                                  'throbber start: %s, throbber stop: %s, '
                                  'throbber time: %s ' %
                                  (base_time, start_time,
                                   throbber_start_time, throbber_stop_time,
                                   throbber_stop_time - throbber_start_time))

            if (start_time > throbber_start_time or
                start_time > throbber_stop_time or
                throbber_start_time > throbber_stop_time):
                self.loggerdeco.warning('analyze_logcat: inconsistent measurements: '
                                        'start: %s, '
                                        'throbber start: %s, throbber stop: %s ' %
                                      (start_time,
                                       throbber_start_time,
                                       throbber_stop_time))
                start_time = throbber_start_time = throbber_stop_time = 0
        else:
            self.loggerdeco.warning(
                'analyze_logcat: failed to get measurements '
                'start_time: %s, throbber start: %s, throbber stop: %s' % (
                    start_time, throbber_start_time, throbber_stop_time))
            start_time = throbber_start_time = throbber_stop_time = 0

        return (start_time, throbber_start_time, throbber_stop_time)

    def publish_results(self, starttime=0, tstrt=0, tstop=0,
                        build_metadata=None, testname='', cache_enabled=True,
                        rejected=False):
        msg = ('Cached: %s Start Time: %s Throbber Start: %s Throbber Stop: %s '
               'Total Throbber Time: %s Rejected: %s' % (
                   cache_enabled, starttime, tstrt, tstop, tstop - tstrt, rejected))
        self.loggerdeco.debug('RESULTS: %s' % msg)

        # Create JSON to send to webserver
        resultdata = {}
        resultdata['phoneid'] = self.phone_cfg['phoneid']
        resultdata['testname'] = testname
        resultdata['starttime'] = starttime
        resultdata['throbberstart'] = tstrt
        resultdata['throbberstop'] = tstop
        resultdata['blddate'] = build_metadata['blddate']
        resultdata['cached'] = cache_enabled
        resultdata['rejected'] = rejected

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

    def check_results(self, build_metadata=None, testname=''):
        """Return True if there already exist unrejected results for this device,
        build and test.
        """

        # Create JSON to send to webserver
        query = {}
        query['phoneid'] = self.phone_cfg['phoneid']
        query['test'] = testname
        query['revision'] = build_metadata['revision']
        query['product'] = build_metadata['androidprocname']

        self.loggerdeco.debug('check_results for: %s' % query)

        try:
            url = self._resulturl + 'check/?' + urllib.urlencode(query)
            f = urllib2.urlopen(url)
        except urllib2.URLError, e:
            self.loggerdeco.error(
                'check_results: %s could not check: '
                'phoneid: %s, test: %s, revision: %s, product: %s' % (
                    e,
                    query['phoneid'], query['test'],
                    query['revision'], query['product']))
            return False
        data = f.read()
        self.loggerdeco.debug('check_results: data: %s' % data)
        f.close()
        response = json.loads(data)
        return response['result']
