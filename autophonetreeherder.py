# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import calendar
import datetime
import glob
import json
import logging
import os
import re
import tempfile
import time
import urlparse

import pytz
from thclient import (TreeherderClient, TreeherderJobCollection, TreeherderJob)

import utils

from s3 import S3Error

LEAK_RE = re.compile('\d+ bytes leaked \((.+)\)$')
CRASH_RE = re.compile('.+ application crashed \[@ (.+)\]$')

# Set the logger globally in the file, but this must be reset when
# used in a child process.
logger = logging.getLogger()

def timestamp_now():
    return int(calendar.timegm(datetime.datetime.now(tz=pytz.utc).timetuple()))


def platform(architecture, platform, sdk):
    if architecture == 'x86':
        return '%s-x86' % platform
    return '%s-%s-%s' % (platform,
                         architecture,
                         ''.join(sdk.split('-')))


def architecture(abi):
    if 'armeabi-v7a' in abi:
        return 'armv7'
    if 'arm64-v8a' in abi:
        return 'armv8'
    return 'unknown'


def buildername(platform, tree, build_type, test_name):
    return "%s %s %s %s" % (
        platform, tree, build_type, test_name)


class TestState(object):
    COMPLETED = 'completed'
    PENDING = 'pending'
    RUNNING = 'running'


class AutophoneTreeherder(object):

    def __init__(self, worker_subprocess, options, jobs, s3_bucket=None,
                 mailer=None, shared_lock=None):
        assert options, "options is required."
        assert shared_lock, "shared_lock is required."

        self.options = options
        self.jobs = jobs
        self.s3_bucket = s3_bucket
        self.mailer = mailer
        self.shared_lock = shared_lock
        self.worker = worker_subprocess
        self.shutdown_requested = False
        logger.debug('AutophoneTreeherder')

        self.url = self.options.treeherder_url
        if not self.url:
            logger.debug('AutophoneTreeherder: no treeherder url')
            return

        self.client_id = self.options.treeherder_client_id
        self.secret = self.options.treeherder_secret
        self.retry_wait = self.options.treeherder_retry_wait

        self.client = TreeherderClient(server_url=self.url,
                                       client_id=self.client_id,
                                       secret=self.secret)

        logger.debug('AutophoneTreeherder: %s' % self)

    def __str__(self):
        # Do not publish sensitive information
        whitelist = ('url',
                     'retry_wait')
        d = {}
        for attr in whitelist:
            d[attr] = getattr(self, attr)
        return '%s' % d

    def post_request(self, machine, project, job_collection, attempts, last_attempt):
        logger.debug('AutophoneTreeherder.post_request: %s, attempt=%d, last=%s' %
                     (job_collection.__dict__, attempts, last_attempt))

        try:
            self.client.post_collection(project, job_collection)
            return True
        except Exception, e:
            logger.exception('Error submitting request to Treeherder, attempt=%d, last=%s' %
                             (attempts, last_attempt))
            if self.mailer:
                if hasattr(e, 'response') and e.response:
                    response_json = json.dumps(e.response.json(),
                                               indent=2, sort_keys=True)
                else:
                    response_json = None
                request_len = len(job_collection.to_json())
                self.mailer.send(
                    '%s attempt %d Error submitting request to Treeherder' %
                    (utils.host(), attempts),
                    'Phone: %s\n'
                    'Exception: %s\n'
                    'Last attempt: %s\n'
                    'Request length: %d\n'
                    'Response: %s\n' % (
                        machine,
                        e,
                        last_attempt,
                        request_len,
                        response_json))
        return False

    def queue_request(self, machine, project, job_collection):
        logger.debug('AutophoneTreeherder.queue_request: %s' % job_collection.__dict__)
        logger.debug('AutophoneTreeherder shared_lock.acquire')
        self.shared_lock.acquire()
        try:
            self.jobs.new_treeherder_job(machine, project, job_collection)
        finally:
            logger.debug('AutophoneTreeherder shared_lock.release')
            self.shared_lock.release()

    def _create_job(self, tjc, machine, build_url, project, revision, build_type, build_abi, build_platform, build_sdk, builder_type, t):
        """Create job to be sent to Treeherder

        :param tjc: treeherder job collection instance
        :param machine: machine id
        :param build_url: url to build being tested.
        :param project: repository of build.
        :param revision: Either a URL to the changeset or the revision id.
        :param t: test to be reported.
        """
        logger.debug('AutophoneTreeherder.create_job: %s' % t)
        assert self.url and revision, 'AutophoneTreeherder.create_job: no url/revision'

        if len(revision) != 40:
            logger.warning('AutophoneTreeherder using revision with length %d: %s' % (len(revision), revision))

        logger.info('creating Treeherder job %s for %s %s, '
                    'revision: %s' % (
                        t.job_guid, t.name, project,
                        revision))

        logger.debug('AutophoneTreeherder.create_job: '
                     'test config_file=%s, config sections=%s' % (
                         t.config_file, t.cfg.sections()))

        tj = tjc.get_job()
        tj.add_tier(self.options.treeherder_tier)
        tj.add_revision(revision)
        tj.add_project(project)
        tj.add_job_guid(t.job_guid)
        tj.add_job_name(t.job_name)
        tj.add_job_symbol(t.job_symbol)
        tj.add_group_name(t.group_name)
        tj.add_group_symbol(t.group_symbol)
        tj.add_product_name('fennec')

        tj.add_machine(machine)
        build_platform = platform(architecture(build_abi),
                                  build_platform,
                                  build_sdk)
        build_architecture = architecture(build_abi)
        machine_platform = platform(architecture(t.phone.abi),
                                   t.phone.os,
                                   build_sdk)
                                   #t.phone.sdk)
        machine_architecture = architecture(t.phone.abi)
        tj.add_build_info('android', build_platform, build_architecture)
        tj.add_machine_info('android', machine_platform, machine_architecture)
        tj.add_option_collection({build_type: True})

        # Fake the buildername from buildbot...
        tj.add_artifact('buildapi', 'json', {
            'buildername': buildername(machine_platform, project, build_type, t.name)})
        # Create a 'privatebuild' artifact for storing information
        # regarding the build.
        tj.add_artifact('privatebuild', 'json', {
            'build_url': build_url,
            'config_file': t.config_file,
            'chunk': t.chunk,
            'builder_type': builder_type})

        return tj

    def submit_pending(self, machine, build_url, project, revision, build_type, build_abi, build_platform, build_sdk, builder_type, tests=[]):
        """Submit tests pending notifications to Treeherder

        :param machine: machine id
        :param build_url: url to build being tested.
        :param project: repository of build.
        :param revision: Either a URL to the changeset or the revision id.
        :param tests: Lists of tests to be reported.
        """
        logger.debug('AutophoneTreeherder.submit_pending: %s' % tests)
        if not self.url or not revision:
            logger.debug('AutophoneTreeherder.submit_pending: no url/revision')
            return

        tjc = TreeherderJobCollection()

        for t in tests:
            logger.debug('AutophoneTreeherder.submit_pending: '
                         'for %s %s' % (t.name, project))

            t.message = None
            t.submit_timestamp = timestamp_now()
            t.job_details = []

            tj = self._create_job(tjc, machine, build_url, project, revision, build_type, build_abi, build_platform, build_sdk, builder_type, t)
            tj.add_state(TestState.PENDING)
            tj.add_submit_timestamp(t.submit_timestamp)
            # XXX need to send these until Bug 1066346 fixed.
            tj.add_start_timestamp(0)
            tj.add_end_timestamp(0)
            tjc.add(tj)

        logger.debug('AutophoneTreeherder.submit_pending: tjc: %s' % (
            tjc.to_json()))

        self.queue_request(machine, project, tjc)

    def submit_running(self, machine, build_url, project, revision, build_type, build_abi, build_platform, build_sdk, builder_type, tests=[]):
        """Submit tests running notifications to Treeherder

        :param machine: machine id
        :param build_url: url to build being tested.
        :param project: repository of build.
        :param revision: Either a URL to the changeset or the revision id.
        :param tests: Lists of tests to be reported.
        """
        logger.debug('AutophoneTreeherder.submit_running: %s' % tests)
        if not self.url or not revision:
            logger.debug('AutophoneTreeherder.submit_running: no url/revision')
            return

        tjc = TreeherderJobCollection()

        for t in tests:
            logger.debug('AutophoneTreeherder.submit_running: '
                         'for %s %s' % (t.name, project))

            t.submit_timestamp = timestamp_now()
            t.start_timestamp = timestamp_now()

            tj = self._create_job(tjc, machine, build_url, project, revision, build_type, build_abi, build_platform, build_sdk, builder_type, t)
            tj.add_state(TestState.RUNNING)
            tj.add_submit_timestamp(t.submit_timestamp)
            tj.add_start_timestamp(t.start_timestamp)
            # XXX need to send these until Bug 1066346 fixed.
            tj.add_end_timestamp(0)
            tjc.add(tj)

        logger.debug('AutophoneTreeherder.submit_running: tjc: %s' %
                     tjc.to_json())

        self.queue_request(machine, project, tjc)

    def submit_complete(self, machine, build_url, project, revision, build_type, build_abi, build_platform, build_sdk, builder_type, tests=None):
        """Submit test results for the worker's current job to Treeherder.

        :param machine: machine id
        :param build_url: url to build being tested.
        :param project: repository of build.
        :param revision: Either a URL to the changeset or the revision id.
        :param tests: Lists of tests to be reported.
        """
        logger.debug('AutophoneTreeherder.submit_complete: %s' % tests)

        if not self.url or not revision:
            logger.debug('AutophoneTreeherder.submit_complete: no url/revision')
            return

        tjc = TreeherderJobCollection()

        for t in tests:
            logger.debug('AutophoneTreeherder.submit_complete '
                         'for %s %s' % (t.name, project))

            t.end_timestamp = timestamp_now()
            # A usercancelled job may not have a start_timestamp
            # since it may have been cancelled before it started.
            if not t.start_timestamp:
                t.start_timestamp = t.end_timestamp

            tj = self._create_job(tjc, machine, build_url, project, revision, build_type, build_abi, build_platform, build_sdk, builder_type, t)
            tj.add_state(TestState.COMPLETED)
            tj.add_result(t.test_result.status)
            tj.add_submit_timestamp(t.submit_timestamp)
            tj.add_start_timestamp(t.start_timestamp)
            tj.add_end_timestamp(t.end_timestamp)

            t.job_details.append({
                'value': os.path.basename(t.config_file),
                'title': 'Config'})
            t.job_details.append({
                'url': build_url,
                'value': os.path.basename(build_url),
                'title': 'Build'})
            t.job_details.append({
                'value': utils.host(),
                'title': 'Host'})

            if t.test_result.passed + t.test_result.failed + t.test_result.todo > 0:
                if t.test_result.failed == 0:
                    failed = '0'
                else:
                    failed = '<em class="testfail">%s</em>' % t.test_result.failed

                t.job_details.append({
                    'value': "%s/%s/%s" % (t.test_result.passed, failed, t.test_result.todo),
                    'title': "%s-%s" % (t.job_name, t.job_symbol)
                })

            if hasattr(t, 'phonedash_url'):
                t.job_details.append({
                    'url': t.phonedash_url,
                    'value': 'graph',
                    'title': 'phonedash'
                    })

            # Attach logs, ANRs, tombstones, etc.

            logurl = None
            if self.s3_bucket:
                # We must make certain that S3 keys for uploaded files
                # are unique. We can create a unique log_identifier as
                # follows: For Unittests, t.unittest_logpath's
                # basename contains a unique name based on the actual
                # Unittest name, chunk and machine id. For
                # Non-Unittests, the test classname, chunk and machine
                # id can be used.

                if t.unittest_logpath:
                    log_identifier = os.path.splitext(os.path.basename(
                        t.unittest_logpath))[0]
                else:
                    log_identifier = "%s-%s-%s-%s" % (
                        t.name, os.path.basename(t.config_file), t.chunk,
                        machine)
                # We must make certain the key is unique even in the
                # event of retries.
                log_identifier = '%s-%s' % (log_identifier, t.job_guid)

                key_prefix = os.path.dirname(
                    urlparse.urlparse(build_url).path)
                key_prefix = re.sub('/tmp$', '', key_prefix)

                # Logcat
                fname = '%s-logcat.log' % log_identifier
                lname = 'logcat'
                key = "%s/%s" % (key_prefix, fname)
                with tempfile.NamedTemporaryFile(suffix='logcat.txt') as f:
                    try:
                        if self.worker.is_ok():
                            for line in t.logcat.get(full=True):
                                f.write('%s\n' % line.encode('UTF-8',
                                                             errors='replace'))
                            t.logcat.reset()
                        else:
                            # Device is in an error state so we can't
                            # get the full logcat but we can output
                            # any logcat output we accumulated
                            # previously.
                            for line in t.logcat._accumulated_logcat:
                                f.write('%s\n' % line.encode('UTF-8',
                                                             errors='replace'))
                    except Exception, e:
                        logger.exception('Error reading logcat %s' % fname)
                        t.job_details.append({
                            'value': 'Failed to read %s: %s' % (fname, e),
                            'title': 'Error'})
                    try:
                        url = self.s3_bucket.upload(f.name, key)
                        t.job_details.append({
                            'url': url,
                            'value': lname,
                            'title': 'artifact uploaded'})
                    except S3Error, e:
                        logger.exception('Error uploading logcat %s' % fname)
                        t.job_details.append({
                            'value': 'Failed to upload %s: %s' % (fname, e),
                            'title': 'Error'})
                # Upload directory containing ANRs, tombstones and other items
                # to be uploaded.
                if t.upload_dir:
                    for f in glob.glob(os.path.join(t.upload_dir, '*')):
                        try:
                            lname = os.path.basename(f)
                            try:
                                fname = '%s-%s' % (log_identifier, lname)
                            except UnicodeDecodeError, e:
                                logger.exception('Ignoring artifact %s' %
                                                 lname.decode('utf-8',
                                                              errors='replace'))
                                continue
                            url = self.s3_bucket.upload(f, "%s/%s" % (
                                key_prefix, fname))
                            t.job_details.append({
                                'url': url,
                                'value': lname,
                                'title': 'artifact uploaded'})
                        except S3Error, e:
                            logger.exception('Error uploading artifact %s' % fname)
                            t.job_details.append({
                                'value': 'Failed to upload artifact %s: %s' % (fname, e),
                                'title': 'Error'})

                # Bug 1113264 - Multiple job logs push action buttons outside
                # the job details navbar
                #
                # Due to the way Treeherder UI displays log buttons in the
                # Job Info panel, it is important to only specify one log
                # file to prevent the multiple log buttons from hiding the
                # retrigger button. If the test is a Unit Test, its log
                # will marked as the log file. Otherwise, the Autophone
                # log will be marked as the log file.

                # UnitTest Log
                if t.unittest_logpath and os.path.exists(t.unittest_logpath):
                    fname = '%s.log' % log_identifier
                    key = "%s/%s" % (key_prefix, fname)
                    try:
                        logurl = self.s3_bucket.upload(t.unittest_logpath, key)
                        tj.add_log_reference('buildbot_text', logurl,
                                             parse_status='pending')
                        t.job_details.append({
                            'url': logurl,
                            'value': fname,
                            'title': 'artifact uploaded'})
                    except Exception, e:
                        logger.exception('Error %s uploading log %s' % (
                            e, fname))
                        t.job_details.append({
                            'value': 'Failed to upload log %s: %s' % (fname, e),
                            'title': 'Error'})
                # Autophone Log
                # Since we are submitting results to Treeherder, we flush
                # the worker's log before uploading the log to
                # Treeherder. When we upload the log, it will contain
                # results for a single test run with possibly an error
                # message from the previous test if the previous log
                # upload failed.
                if t.test_logfile:
                    try:
                        t.test_logfilehandler.flush()
                        fname = '%s-autophone.log' % log_identifier
                        lname = 'Autophone Log'
                        key = "%s/%s" % (key_prefix, fname)
                        url = self.s3_bucket.upload(t.test_logfile, key)
                        t.job_details.append({
                            'url': url,
                            'value': lname,
                            'title': 'artifact uploaded'})
                        if not logurl:
                            tj.add_log_reference('buildbot_text', url,
                                                 parse_status='pending')
                            logurl = url
                    except Exception, e:
                        logger.exception('Error %s uploading %s' % (
                            e, fname))
                        t.job_details.append({
                            'value': 'Failed to upload Autophone log: %s' % e,
                            'title': 'Error'})

            tj.add_artifact('Job Info', 'json', {'job_details': t.job_details})

            if hasattr(t, 'perfherder_artifact') and t.perfherder_artifact:
                jsondata = json.dumps({'performance_data': t.perfherder_artifact})
                logger.debug("AutophoneTreeherder.submit_complete: perfherder_artifact: %s" % jsondata)
                tj.add_artifact('performance_data', 'json', jsondata)

            tjc.add(tj)
            message = 'TestResult: %s %s %s' % (t.test_result.status, t.name, build_url)
            if t.message:
                message += ', %s' % t.message
            logger.info(message)

        logger.debug('AutophoneTreeherder.submit_completed: tjc: %s' %
                     tjc.to_json())

        self.queue_request(machine, project, tjc)

    def serve_forever(self):
        while not self.shutdown_requested:
            wait_seconds = 1    # avoid busy loop
            job = self.jobs.get_next_treeherder_job()
            if job:
                tjc = TreeherderJobCollection()
                for data in job['job_collection']:
                    tj = TreeherderJob(data)
                    tjc.add(tj)
                if self.post_request(job['machine'], job['project'], tjc, job['attempts'], job['last_attempt']):
                    self.jobs.treeherder_job_completed(job['id'])
                    wait_seconds = 0
                else:
                    attempts = int(job['attempts'])
                    wait_seconds = min(self.retry_wait * attempts, 3600)
                    logger.debug('AutophoneTreeherder waiting for %d seconds after failed attempt %d' % (wait_seconds, attempts))
            if wait_seconds > 0:
                for i in range(wait_seconds):
                    if self.shutdown_requested:
                        break
                    time.sleep(1)

    def shutdown(self):
        self.shutdown_requested = True
