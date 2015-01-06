# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import datetime
import glob
import json
import os
import re
import tempfile
import time
import urllib
import urllib2
import urlparse
import uuid

from thclient import (TreeherderRequest, TreeherderJobCollection)

from phonetest import PhoneTestResult
from s3 import S3Error

LEAK_RE = re.compile('\d+ bytes leaked \((.+)\)$')
CRASH_RE = re.compile('.+ application crashed \[@ (.+)\]$')

def generate_guid():
    return str(uuid.uuid4())

def timestamp_now():
    return int(time.mktime(datetime.datetime.now().timetuple()))


class TestState(object):
    COMPLETED = 'completed'
    PENDING = 'pending'
    RUNNING = 'running'


class AutophoneTreeherder(object):

    def __init__(self, worker_subprocess):
        self.worker = worker_subprocess
        self.worker.loggerdeco.debug('AutophoneTreeherder: %s' % self.worker)

        self.url = worker_subprocess.options.treeherder_url
        if not self.url:
            self.worker.loggerdeco.debug('AutophoneTreeherder: no treeherder url')
            return

        self.server = self.worker.options.treeherder_server
        self.protocol = self.worker.options.treeherder_protocol
        self.host = self.worker.options.treeherder_server
        self.credentials = self.worker.options.treeherder_credentials
        self.retries = self.worker.options.treeherder_retries
        self.retry_wait = self.worker.options.treeherder_retry_wait
        self.bugscache_uri = '%s/api/bugscache/' % self.url

        self.worker.loggerdeco.debug('AutophoneTreeherder: %s' % self.__dict__)

    def post_request(self, job_collection):
        self.worker.loggerdeco.debug('AutophoneTreeherder.post_request: %s' % job_collection.__dict__)
        if not self.url or not self.worker.build.revision_hash:
            self.worker.loggerdeco.debug('AutophoneTreeherder.post_request: no url/revision hash')
            return

        req = TreeherderRequest(
            protocol=self.protocol,
            host=self.server,
            project=self.worker.build.tree,
            oauth_key=self.credentials[self.worker.build.tree]['consumer_key'],
            oauth_secret=self.credentials[self.worker.build.tree]['consumer_secret']
            )

        try:
            for attempt in range(1, self.retries+1):
                response = req.post(job_collection)
                self.worker.loggerdeco.debug('AutophoneTreeherder.post_request attempt %d: '
                                             'body: %s headers: %s msg: %s status: %s '
                                             'reason: %s' % (
                                                 attempt,
                                                 response.read(),
                                                 response.getheaders(),
                                                 response.msg,
                                                 response.status,
                                                 response.reason))
                if response.reason == 'OK':
                    break
                msg = ('Attempt %d to post result to Treeherder failed.\n\n'
                       'Response:\n'
                       'body: %s\n'
                       'headers: %s\n'
                       'msg: %s\n'
                       'status: %s\n'
                       'reason: %s\n' % (
                           attempt,
                           response.read(), response.getheaders(),
                           response.msg, response.status,
                           response.reason))
                self.worker.loggerdeco.error(msg)
                self.worker.mailer.send('Attempt %d for Phone %s failed to post to Treeherder' %
                                        (attempt, self.worker.phone.id), msg)
                time.sleep(self.retry_wait)
        except Exception, e:
            self.worker.loggerdeco.exception('Error submitting request to Treeherder')
            self.worker.mailer.send('Error submitting request to Treeherder',
                                    'Phone: %s\n'
                                    'TreeherderClientError: %s\n'
                                    'TreeherderJobCollection %s\n' % (
                                        self.worker.phone.id,
                                        e,
                                        job_collection.to_json()))

    def submit_pending(self, tests=[]):
        self.worker.loggerdeco.debug('AutophoneTreeherder.submit_pending: %s' % tests)
        if not self.url or not self.worker.build.revision_hash:
            self.worker.loggerdeco.debug('AutophoneTreeherder.submit_pending: no url/revision hash')
            return

        tjc = TreeherderJobCollection(job_type='update')

        if not tests:
            tests = self.worker.tests

        for t in tests:
            if not t.test_this_repo:
                self.worker.loggerdeco.debug('AutophoneTreeherder.submit_pending: not creating '
                                             'job for %s %s' % (t.name, t.build.tree))
                continue

            t.message = None
            t.submit_timestamp = timestamp_now()
            t.job_guid = generate_guid()
            t.job_details = []

            self.worker.loggerdeco.info('creating Treeherder job %s for %s %s, '
                                        'revision: %s, revision_hash: %s' % (
                                            t.job_guid, t.name, t.build.tree,
                                            t.build.revision, t.build.revision_hash))

            self.worker.loggerdeco.debug('AutophoneTreeherder.submit_pending: '
                                         'test config_file=%s, config sections=%s' % (
                                             t.config_file, t.cfg.sections()))

            tj = tjc.get_job()
            tj.add_revision_hash(self.worker.build.revision_hash)
            tj.add_project(self.worker.build.tree)
            tj.add_job_guid(t.job_guid)
            tj.add_job_name(t.job_name)
            tj.add_job_symbol(t.job_symbol)
            tj.add_group_name(t.group_name)
            tj.add_group_symbol(t.group_symbol)
            tj.add_product_name('fennec')
            tj.add_state(TestState.PENDING)
            tj.add_submit_timestamp(t.submit_timestamp)
            # XXX need to send these until Bug 1066346 fixed.
            tj.add_start_timestamp(t.submit_timestamp)
            tj.add_end_timestamp(t.submit_timestamp)
            #
            tj.add_machine(t.phone.id)
            tj.add_build_url(self.worker.build.url)
            tj.add_build_info('android', t.phone.platform, t.phone.architecture)
            tj.add_machine_info('android',t.phone.platform, t.phone.architecture)
            tj.add_option_collection({'opt': True})

            # Fake the buildername from buildbot...
            tj.add_artifact('buildapi', 'json', {'buildername': t.buildername})

            tjc.add(tj)

        self.worker.loggerdeco.debug('AutophoneTreeherder.submit_pending: tjc: %s' % (
            tjc.to_json()))

        self.post_request(tjc)

    def submit_running(self, tests=[]):
        self.worker.loggerdeco.debug('AutophoneTreeherder.submit_running: %s' % tests)
        if not self.url or not self.worker.build.revision_hash:
            self.worker.loggerdeco.debug('AutophoneTreeherder.submit_running: no url/revision hash')
            return

        tjc = TreeherderJobCollection(job_type='update')

        if not tests:
            tests = self.worker.tests

        for t in tests:
            if not t.test_this_repo:
                continue
            self.worker.loggerdeco.debug('AutophoneTreeherder.submit_running: '
                                         'for %s %s' % (t.name, t.build.tree))

            t.start_timestamp = timestamp_now()

            tj = tjc.get_job()
            tj.add_revision_hash(self.worker.build.revision_hash)
            tj.add_project(self.worker.build.tree)
            tj.add_job_guid(t.job_guid)
            tj.add_job_name(t.job_name)
            tj.add_job_symbol(t.job_symbol)
            tj.add_group_name(t.group_name)
            tj.add_group_symbol(t.group_symbol)
            tj.add_product_name('fennec')
            tj.add_state(TestState.RUNNING)
            tj.add_submit_timestamp(t.submit_timestamp)
            tj.add_start_timestamp(t.start_timestamp)
            # XXX need to send these until Bug 1066346 fixed.
            tj.add_end_timestamp(t.start_timestamp)
            #
            tj.add_machine(t.phone.id)
            tj.add_build_url(self.worker.build.url)
            tj.add_build_info('android', t.phone.platform, t.phone.architecture)
            tj.add_machine_info('android',t.phone.platform, t.phone.architecture)
            tj.add_option_collection({'opt': True})

            tj.add_artifact('buildapi', 'json', {'buildername': t.buildername})
            tjc.add(tj)

        self.worker.loggerdeco.debug('AutophoneTreeherder.submit_running: tjc: %s' %
                                     tjc.to_json())

        self.post_request(tjc)

    def submit_complete(self, test=None,
                        test_status=None,
                        test_message=None):
        """Submit test results for the worker's current job to Treeherder.

        submit_complete operates in two modes:

        * To report an infrastructure error which has prevented any of the
          tests from running.

          In this case, the test argument is None and the test_status
          and test_message will be used to report the error for each
          of the tests defined for the worker using
          PhoneTest.add_failure.

        * To report the status of an individual test.

          In this case, the test argument references a test object and
          both test_status and test_message are required to be
          None. The Treeherder test_status is determined by whether
          there were any failures reported.

        :param test: test to be reported.
        :param test_status: global test status to be reported.
        :param test_message: global test message to be reported.

        """
        self.worker.loggerdeco.debug('AutophoneTreeherder.submit_complete: %s' % test)

        assert((test is None and test_status and test_message and
                test_status != PhoneTestResult.SUCCESS) or
               (test is not None and test_status is None and test_message is None))

        if not self.url or not self.worker.build.revision_hash:
            self.worker.loggerdeco.debug('AutophoneTreeherder.submit_complete: no url/revision hash')
            return

        tjc = TreeherderJobCollection()

        if test:
            tests = [test]
        else:
            tests = self.worker.tests
            for t in tests:
                if t.test_this_repo:
                    t.test_result.add_failure(t.name, test_status, test_message)

        for t in tests:
            if not t.test_this_repo:
                continue
            self.worker.loggerdeco.debug('AutophoneTreeherder.submit_complete '
                                         'for %s %s' % (t.name, t.build.tree))

            t.end_timestamp = timestamp_now()
            # A usercancelled job may not have a start_timestamp
            # since it may have been cancelled before it started.
            if not t.start_timestamp:
                t.start_timestamp = t.end_timestamp

            if t.test_result.failed == 0:
                test_status = PhoneTestResult.SUCCESS
                failed = '0'
            else:
                if not test_status:
                    test_status = PhoneTestResult.TESTFAILED
                failed = '<em class="testfail">%s</em>' % t.test_result.failed

            t.job_details.append({
                'value': "%s/%s/%s" % (t.test_result.passed, failed, t.test_result.todo),
                'content_type': 'raw_html',
                'title': "%s-%s" % (t.job_name, t.job_symbol)
            })

            bug_suggestions = self.get_bug_suggestions(t.test_result.failures)

            if hasattr(t, 'phonedash_url'):
                t.job_details.append({
                    'url': t.phonedash_url,
                    'value': 'graph',
                    'content_type': 'link',
                    'title': 'phonedash:'
                    })

            tj = tjc.get_job()

            # Attach logs
            if self.worker.s3_bucket:
                key_prefix = os.path.dirname(
                    urlparse.urlparse(self.worker.build.url).path)
                key_prefix = re.sub('/tmp$', '', key_prefix)
                # Logcat
                with tempfile.NamedTemporaryFile(suffix='logcat.txt') as f:
                    for line in t.logcat.get(full=True):
                        f.write('%s\n' % line)
                    try:
                        url = self.worker.s3_bucket.upload(f.name, "%s/%s" % (
                            key_prefix, 'logcat.txt'))
                        t.job_details.append({
                            'url': url,
                            'value': 'logcat.txt',
                            'content_type': 'link',
                            'title': 'Logcat:'})
                    except S3Error:
                        self.worker.loggerdeco.exception('Error uploading logcat')
                        t.job_details.append({
                            'value': 'Failed to upload logcat.txt',
                            'content_type': 'text',
                            'title': 'Error:'})
                # Log
                if t._log:
                    logfile = os.path.basename(t._log)
                    try:
                        url = self.worker.s3_bucket.upload(t._log, "%s/%s" % (
                            key_prefix, logfile))
                        t.job_details.append({
                            'url': url,
                            'value': logfile,
                            'content_type': 'link',
                            'title': 'Log:'})
                        # don't add log reference  since we don't
                        # use treeherder's log parsing.
                        #tj.add_log_reference(logfile, url)
                    except S3Error:
                        self.worker.loggerdeco.exception('Error uploading log')
                        t.job_details.append({
                            'value': 'Failed to upload log',
                            'content_type': 'text',
                            'title': 'Error:'})
                # upload directory containing ANRs, tombstones and other items
                # to be uploaded.
                if t.upload_dir:
                    for f in glob.glob(os.path.join(t.upload_dir, '*')):
                        try:
                            fname = os.path.basename(f)
                            url = self.worker.s3_bucket.upload(f, "%s/%s" % (
                                key_prefix, fname))
                            t.job_details.append({
                                'url': url,
                                'value': fname,
                                'content_type': 'link',
                                'title': 'Artifact:'})
                        except S3Error:
                            self.worker.loggerdeco.exception('Error uploading artifact')
                            t.job_details.append({
                                'value': 'Failed to upload artifact %s' % fname,
                                'content_type': 'text',
                                'title': 'Error:'})

            tj.add_revision_hash(self.worker.build.revision_hash)
            tj.add_project(self.worker.build.tree)
            tj.add_job_guid(t.job_guid)
            tj.add_job_name(t.job_name)
            tj.add_job_symbol(t.job_symbol)
            tj.add_group_name(t.group_name)
            tj.add_group_symbol(t.group_symbol)
            tj.add_product_name('fennec')
            tj.add_state(TestState.COMPLETED)
            tj.add_result(test_status)
            tj.add_submit_timestamp(t.submit_timestamp)
            tj.add_start_timestamp(t.start_timestamp)
            tj.add_end_timestamp(t.end_timestamp)
            tj.add_machine(t.phone.id)
            tj.add_build_url(self.worker.build.url)
            tj.add_build_info('android', t.phone.platform, t.phone.architecture)
            tj.add_machine_info('android',t.phone.platform, t.phone.architecture)
            tj.add_option_collection({'opt': True})
            tj.add_artifact('Job Info', 'json', {'job_details': t.job_details})
            if bug_suggestions:
                tj.add_artifact('Bug suggestions', 'json', bug_suggestions)

            tj.add_artifact('buildapi', 'json', {'buildername': t.buildername})
            tjc.add(tj)

            message = '%s %s %s TestResult: %s' % (self.worker.build.tree,
                                                   self.worker.build.id,
                                                   t.name, test_status)
            if t.message:
                message += ', %s' % t.message
            self.worker.loggerdeco.info(message)

        self.worker.loggerdeco.debug('AutophoneTreeherder.submit_completed: tjc: %s' %
                                     tjc.to_json())

        self.post_request(tjc)

    # copied from https://github.com/mozilla/treeherder-service/blob/master/treeherder/log_parser/utils.py
    # get_remote_content from treeherder/etc/common.py

    def _get_remote_content(self, url):
        """A thin layer of abstraction over urllib. """
        req = urllib2.Request(url)
        req.add_header('Accept', 'application/json')
        req.add_header('Content-Type', 'application/json')
        try:
            conn = urllib2.urlopen(req)
        except Exception:
            self.worker.loggerdeco.exception('%s' % url)
            return None

        if not conn.getcode() == 200:
            return None
        try:
            content = json.loads(conn.read())
        except:
            content = None
        finally:
            conn.close()

        return content

    def _is_helpful_search_term(self, search_term):
        # Search terms that will match too many bug summaries
        # and so not result in useful suggestions.
        search_term = search_term.strip()

        blacklist = [
            'automation.py',
            'remoteautomation.py',
            'Shutdown',
            'undefined',
            'Main app process exited normally',
            'Traceback (most recent call last):',
            'Return code: 0',
            'Return code: 1',
            'Return code: 2',
            'Return code: 9',
            'Return code: 10',
            'Exiting 1',
            'Exiting 9',
            'CrashingThread(void *)',
            'libSystem.B.dylib + 0xd7a',
            'linux-gate.so + 0x424',
            'TypeError: content is null',
            'leakcheck'
        ]

        return len(search_term) > 4 and not (search_term in blacklist)

    def _get_error_search_term(self, error_line):
        """
        retrieves bug suggestions from bugscache using search_term
        in a full_text search.
        """
        if not error_line:
            return None

        # this is STRONGLY inspired to
        # https://hg.mozilla.org/webtools/tbpl/file/tip/php/inc/AnnotatedSummaryGenerator.php#l73

        tokens = error_line.split(" | ")
        search_term = None

        if len(tokens) >= 3:
            # it's in the "FAILURE-TYPE | testNameOrFilePath | message" type format.
            test_name_or_path = tokens[1]
            message = tokens[2]

            # Leak failure messages are of the form:
            # leakcheck | .*leaked \d+ bytes (Object-1, Object-2, Object-3, ...)
            match = LEAK_RE.match(message)
            if match:
                search_term = match.group(1)
            else:
                for splitter in ("/", "\\"):
                    # if this is a path, we are interested in the last part
                    test_name_or_path = test_name_or_path.split(splitter)[-1]
                search_term = test_name_or_path

        # If the failure line was not in the pipe symbol delimited format or the search term
        # will likely return too many (or irrelevant) results (eg: too short or matches terms
        # on the blacklist), then we fall back to searching for the entire failure line if
        # it is suitable.
        if not (search_term and self._is_helpful_search_term(search_term)):
           search_term = error_line if self._is_helpful_search_term(error_line) else None

        # Searching for extremely long search terms is undesirable, since:
        # a) Bugzilla's max summary length is 256 characters, and once "Intermittent "
        # and platform/suite information is prefixed, there are even fewer characters
        # left for us to use for the failure string against which we need to match.
        # b) For long search terms, the additional length does little to prevent against
        # false positives, but means we're more susceptible to false negatives due to
        # run-to-run variances in the error messages (eg paths, process IDs).
        if search_term:
           search_term = search_term[:100]

        return search_term

    def _get_crash_signature(self, error_line):
        """
        Detect if the error_line contains a crash signature
        and return it if it's a helpful search term
        """
        search_term = None
        match = CRASH_RE.match(error_line)
        if match and self._is_helpful_search_term(match.group(1)):
            search_term = match.group(1)
        return search_term

    def _get_bugs_for_search_term(self, search):
        params = {
            'search': search
        }
        query_string = urllib.urlencode(params)
        url = '{0}?{1}'.format(
            self.bugscache_uri,
            query_string
        )
        return self._get_remote_content(url)

    # copied from https://github.com/mozilla/treeherder-service/blob/master/treeherder/log_parser/tasks.py

    def get_bug_suggestions(self, failures):
        self.worker.loggerdeco.debug('get_bug_suggestions: failures: %s' % failures)
        try:
            bug_suggestions = []
            terms_requested = {}

            for failure in failures:
                status = failure['status']
                test = failure['test']
                text = failure['text']
                if status and test and text:
                    line = '%s | %s | %s' % (status, test, text)
                elif test and text:
                    line = '%s | %s' % (test, text)
                elif text:
                    line = text
                else:
                    continue

                # get a meaningful search term out of the error line
                search_term = self._get_error_search_term(line)
                bugs = dict(open_recent=[], all_others=[])

                # collect open recent and all other bugs suggestions
                if search_term:
                    if not search_term in terms_requested:
                        # retrieve the list of suggestions from the api
                        bugs = self._get_bugs_for_search_term(search_term)
                        terms_requested[search_term] = bugs
                    else:
                        bugs = terms_requested[search_term]

                if not bugs or not (bugs['open_recent']
                                    or bugs['all_others']):
                    # no suggestions, try to use
                    # the crash signature as search term
                    crash_signature = self._get_crash_signature(line)
                    if crash_signature:
                        if not crash_signature in terms_requested:
                            bugs = self._get_bugs_for_search_term(crash_signature)
                            terms_requested[crash_signature] = bugs
                        else:
                            bugs = terms_requested[crash_signature]

                bug_suggestions.append({
                    "search": line,
                    "bugs": bugs
                })
        except Exception:
            raise

        self.worker.loggerdeco.debug('get_bug_suggestions: %s' % bug_suggestions)
        return bug_suggestions

if __name__ == "__main__":
    class DummyOptions(object):
        def __init__(self):
            self.treeherder_url = 'https://treeherder.allizom.org'
            self.treeherder_server = 'https://treeherder.allizom.org'
            self.treeherder_protocol = 'https'
            self.treeherder_credentials = None
            self.treeherder_retries = 3
            self.treeherder_retry_wait = 60

    class DummyWorker(object):
        def __init__(self):
            self.options = DummyOptions()

    failures = [{
        'test': 'file:///builds/slave/talos-slave/test/build/tests/reftest/tests/layout/reftests/font-face/cross-iframe-1.html',
        'failures': [{'status': 'TEST-UNEXPECTED-FAIL', 'text': 'image comparison (==), max difference: 255, number of differing pixels: 222'}]
        }]
    treeherder = AutophoneTreeherder(DummyWorker())
    bugs = treeherder.get_bug_suggestions(failures)
    print bugs
