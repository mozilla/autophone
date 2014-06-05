# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import ConfigParser
import json
import jwt
import logging
import os
import urllib
import urllib2
from math import sqrt
from time import sleep

from logdecorator import LogDecorator
from adb import ADBError
from options import *
from phonetest import PhoneTest

class PerfTest(PhoneTest):

    def setup_job(self, build_metadata, worker_subprocess):
        self.build_metadata = build_metadata
        self.worker_subprocess = worker_subprocess
        self.fennec_appname = build_metadata['androidprocname']
        self.buildid = build_metadata['buildid']

        self.logger_original = self.logger
        self.loggerdeco_original = self.loggerdeco
        self.dm_logger_original = self.dm._logger

        self.logger = logging.getLogger('autophone.worker.subprocess.test')
        self.loggerdeco = LogDecorator(self.logger,
                                       {'phoneid': self.phone_cfg['phoneid'],
                                        'pid': os.getpid(),
                                        'buildid': self.buildid},
                                       '%(phoneid)s|%(pid)s|%(buildid)s|'
                                       '%(message)s')
        self.dm._logger = self.loggerdeco

        # Read our config file which gives us our number of
        # iterations and urls that we will be testing
        self.cfg = ConfigParser.RawConfigParser()
        self.cfg.read(self.config_file)
        # [signature]
        self._signer = None
        self._jwt = {'id': '', 'key': None}
        for opt in self._jwt.keys():
            try:
                self._jwt[opt] = self.cfg.get('signature', opt)
            except (ConfigParser.NoSectionError,
                    ConfigParser.NoOptionError):
                break
        # phonedash requires both an id and a key.
        if self._jwt['id'] and self._jwt['key']:
            self._signer = jwt.jws.HmacSha(key=self._jwt['key'],
                                           key_id=self._jwt['id'])
        # [settings]
        self._iterations = self.cfg.getint('settings', 'iterations')
        try:
            self.stderrp_accept = self.cfg.getfloat('settings', 'stderrp_accept')
        except ConfigParser.NoOptionError:
            self.stderrp_accept = 0
        try:
            self.stderrp_reject = self.cfg.getfloat('settings', 'stderrp_reject')
        except ConfigParser.NoOptionError:
            self.stderrp_reject = 100
        try:
            self.stderrp_attempts = self.cfg.getint('settings', 'stderrp_attempts')
        except ConfigParser.NoOptionError:
            self.stderrp_attempts = 1
        self._resulturl = self.cfg.get('settings', 'resulturl')
        if not self._resulturl.endswith('/'):
            self._resulturl += '/'


    def run_job(self):
        self.run_tests()

    def teardown_job(self):
        self.logger = self.logger_original
        self.loggerdeco = self.loggerdeco_original
        self.dm._logger = self.dm_logger_original

    def run_tests(self):
        pass

    def get_logcat(self):
        for attempt in range(self.user_cfg[PHONE_RETRY_LIMIT]):
            try:
                return [x.strip() for x in self.dm.get_logcat(
                    filter_specs=['*:V']
                )]
            except ADBError:
                self.loggerdeco.exception('Attempt %d get logcat throbbers' % attempt)
                if attempt == self.user_cfg[PHONE_RETRY_LIMIT] - 1:
                    raise
                sleep(self.user_cfg[PHONE_RETRY_WAIT])

    def publish_results(self, starttime=0, tstrt=0, tstop=0,
                        testname='', cache_enabled=True,
                        rejected=False):
        msg = ('Cached: %s Start Time: %s Throbber Start: %s Throbber Stop: %s '
               'Total Throbber Time: %s Rejected: %s' % (
                   cache_enabled, starttime, tstrt, tstop, tstop - tstrt, rejected))
        self.loggerdeco.debug('RESULTS: %s' % msg)

        # Create JSON to send to webserver
        resultdata = {
            'phoneid': self.phone_cfg['phoneid'],
            'testname': testname,
            'starttime': starttime,
            'throbberstart': tstrt,
            'throbberstop': tstop,
            'blddate': self.build_metadata['blddate'],
            'cached': cache_enabled,
            'rejected': rejected,
            'revision': self.build_metadata['revision'],
            'productname': self.fennec_appname,
            'productversion': self.build_metadata['version'],
            'osver': self.phone_cfg['osver'],
            'bldtype': self.build_metadata['bldtype'],
            'machineid': self.phone_cfg['machinetype']
        }

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

    def check_results(self, testname=''):
        """Return True if there already exist unrejected results for this device,
        build and test.
        """

        # Create JSON to send to webserver
        query = {
            'phoneid': self.phone_cfg['phoneid'],
            'test': testname,
            'revision': self.build_metadata['revision'],
            'product': self.fennec_appname
        }

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

    def get_stats(self, values):
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

    def is_stderr_below_threshold(self, measurements, dataset, threshold):
        """Return True if all of the measurements in the dataset have
        standard errors of the mean below the threshold.

        Return False if at least one measurement is above the threshold
        or if one or more datasets have only one value.

        Return None if at least one measurement has no values.
        """

        self.loggerdeco.debug("is_stderr_below_threshold: %s" % dataset)

        for cachekey in ('uncached', 'cached'):
            for measurement in measurements:
                data = [datapoint[cachekey][measurement] - datapoint[cachekey]['starttime']
                        for datapoint in dataset
                        if datapoint and cachekey in datapoint]
                if not data:
                    return None
                stats = self.get_stats(data)
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
