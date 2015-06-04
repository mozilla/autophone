# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

from __future__ import with_statement

import Queue
import datetime
import logging
import logging.handlers
import multiprocessing
import os
import posixpath
import re
import sys
import tempfile
import time
import traceback

import buildserver
import jobs
# The following direct imports are necessary in order to reference the
# modules when we reset their global loggers:
import autophonetreeherder
import builds
import mailer
import phonetest
import s3
import utils
from adb import ADBError, ADBTimeoutError
from autophonetreeherder import AutophoneTreeherder
from builds import BuildMetadata
from logdecorator import LogDecorator
from phonestatus import PhoneStatus
from phonetest import PhoneTest, PhoneTestResult
from process_states import ProcessStates
from s3 import S3Bucket
from sensitivedatafilter import SensitiveDataFilter

# Set the logger globally in the file, but this must be reset when
# used in a child process.
logger = logging.getLogger()

class Crashes(object):

    CRASH_WINDOW = 30
    CRASH_LIMIT = 5

    def __init__(self, crash_window=CRASH_WINDOW, crash_limit=CRASH_LIMIT):
        self.crash_times = []
        self.crash_window = datetime.timedelta(seconds=crash_window)
        self.crash_limit = crash_limit

    def add_crash(self):
        self.crash_times.append(datetime.datetime.now())
        self.crash_times = [x for x in self.crash_times
                            if self.crash_times[-1] - x <= self.crash_window]

    def too_many_crashes(self):
        return len(self.crash_times) >= self.crash_limit


class PhoneTestMessage(object):

    def __init__(self, phone, build=None, phone_status=None,
                 message=None):
        self.phone = phone
        self.build = build
        self.phone_status = phone_status
        self.message = message
        self.timestamp = datetime.datetime.now().replace(microsecond=0)

    def __str__(self):
        s = '<%s> %s (%s)' % (self.timestamp.isoformat(), self.phone.id,
                              self.phone_status)
        if self.message:
            s += ': %s' % self.message
        return s

    def short_desc(self):
        s = self.phone_status
        if self.message:
            s += ': %s' % self.message
        return s


class PhoneWorker(object):

    """Runs tests on a single phone in a separate process.
    This is the interface to the subprocess, accessible by the main
    process."""

    DEVICE_READY_RETRY_WAIT = 20
    DEVICE_READY_RETRY_ATTEMPTS = 3
    DEVICE_BATTERY_MIN = 90
    DEVICE_BATTERY_MAX = 95
    PHONE_RETRY_LIMIT = 2
    PHONE_RETRY_WAIT = 15
    PHONE_MAX_REBOOTS = 3
    PHONE_PING_INTERVAL = 15*60
    PHONE_COMMAND_QUEUE_TIMEOUT = 10

    def __init__(self, dm, worker_num, tests, phone, options,
                 autophone_queue, logfile_prefix, loglevel, mailer,
                 shared_lock):

        self.state = ProcessStates.STARTING
        self.tests = tests
        self.dm = dm
        self.phone = phone
        self.options = options
        self.worker_num = worker_num
        self.last_status_msg = None
        self.first_status_of_type = None
        self.last_status_of_previous_type = None
        self.crashes = Crashes(crash_window=options.phone_crash_window,
                               crash_limit=options.phone_crash_limit)
        # Messages are passed to the PhoneWorkerSubProcess worker from
        # the main process by PhoneWorker which puts messages into
        # PhoneWorker.queue. PhoneWorkerSubProcess is given a
        # reference to this queue and gets messages from the main
        # process via this queue.
        self.queue = multiprocessing.Queue()
        self.lock = multiprocessing.Lock()
        self.shared_lock = shared_lock
        self.subprocess = PhoneWorkerSubProcess(dm,
                                                self.worker_num,
                                                tests,
                                                phone, options,
                                                autophone_queue,
                                                self.queue, logfile_prefix,
                                                loglevel, mailer,
                                                shared_lock)
        self.loggerdeco = LogDecorator(logger,
                                       {'phoneid': self.phone.id},
                                       '%(phoneid)s|%(message)s')
        self.loggerdeco.debug('PhoneWorker:__init__')

    def is_alive(self):
        return self.subprocess.is_alive()

    def start(self, phone_status=PhoneStatus.IDLE):
        self.loggerdeco.debug('PhoneWorker:start')
        self.state = ProcessStates.STARTING
        self.subprocess.start(phone_status)

    def stop(self):
        self.loggerdeco.debug('PhoneWorker:stop')
        self.state = ProcessStates.STOPPING
        self.subprocess.stop()

    def shutdown(self):
        self.loggerdeco.debug('PhoneWorker:shutdown')
        self.state = ProcessStates.SHUTTINGDOWN
        self.queue.put_nowait(('shutdown', None))

    def restart(self):
        """We tell the PhoneWorkerSubProcess to shut down cleanly, but mark
        the PhoneWorker state as restarting. AutoPhone will use this
        information to not remove the device when it has completed
        shutting down and will restart it.
        """
        self.loggerdeco.debug('PhoneWorker:restart')
        self.state = ProcessStates.RESTARTING
        self.queue.put_nowait(('shutdown', None))

    def new_job(self):
        self.loggerdeco.debug('PhoneWorker:new_job')
        self.queue.put_nowait(('job', None))

    def reboot(self):
        self.loggerdeco.debug('PhoneWorker:reboot')
        self.queue.put_nowait(('reboot', None))

    def disable(self):
        self.loggerdeco.debug('PhoneWorker:disable')
        self.queue.put_nowait(('disable', None))

    def enable(self):
        self.loggerdeco.debug('PhoneWorker:enable')
        self.queue.put_nowait(('enable', None))

    def cancel_test(self, request):
        self.loggerdeco.debug('PhoneWorker:cancel_test')
        self.queue.put_nowait(('cancel_test', request))

    def ping(self):
        self.loggerdeco.debug('PhoneWorker:ping')
        self.queue.put_nowait(('ping', None))

    def process_msg(self, msg):
        self.loggerdeco.debug('PhoneWorker:process_msg: %s' % msg)

        """These are status messages routed back from the autophone_queue
        listener in the main AutoPhone class. There is probably a bit
        clearer way to do this..."""
        if (not self.last_status_msg or
            msg.phone_status != self.last_status_msg.phone_status):
            self.last_status_of_previous_type = self.last_status_msg
            self.first_status_of_type = msg
        self.last_status_msg = msg

    def status(self):
        response = ''
        now = datetime.datetime.now().replace(microsecond=0)
        response += 'phone %s (%s):\n' % (self.phone.id, self.phone.serial)
        response += '  state %s\n' % self.state
        response += '  debug level %d\n' % self.options.debug
        if not self.last_status_msg:
            response += '  no updates\n'
        else:
            if self.last_status_msg.build and self.last_status_msg.build.id:
                d = self.last_status_msg.build.id
                d = '%s-%s-%s %s:%s:%s' % (d[0:4], d[4:6], d[6:8],
                                           d[8:10], d[10:12], d[12:14])
                response += '  current build: %s %s\n' % (
                    d,
                    self.last_status_msg.build.tree)
            else:
                response += '  no build loaded\n'
            response += '  last update %s ago:\n    %s\n' % (
                now - self.last_status_msg.timestamp,
                self.last_status_msg.short_desc())
            response += '  %s for %s\n' % (
                self.last_status_msg.phone_status,
                now - self.first_status_of_type.timestamp)
            if self.last_status_of_previous_type:
                response += '  previous state %s ago:\n    %s\n' % (
                    now - self.last_status_of_previous_type.timestamp,
                    self.last_status_of_previous_type.short_desc())
        return response

class PhoneWorkerSubProcess(object):

    """Worker subprocess.

    FIXME: Would be nice to have test results uploaded outside of the
    test objects, and to have them queued (and cached) if the results
    server is unavailable for some reason.  Might be best to communicate
    this back to the main AutoPhone process.
    """

    def __init__(self, dm, worker_num, tests, phone, options,
                 autophone_queue, queue, logfile_prefix, loglevel, mailer,
                 shared_lock):
        global logger

        self.state = ProcessStates.RUNNING
        self.worker_num = worker_num
        self.tests = tests
        self.dm = dm
        self.phone = phone
        self.options = options
        # PhoneWorkerSubProcess.autophone_queue is used to pass
        # messages back to the main Autophone process while
        # PhoneWorkerSubProcess.queue is used to get messages from the
        # main process.
        self.autophone_queue = autophone_queue
        self.queue = queue
        self.logfile = logfile_prefix + '.log'
        self.outfile = logfile_prefix + '.out'
        self.loglevel = loglevel
        self.mailer = mailer
        self.shared_lock = shared_lock
        self.p = None
        self.jobs = None
        self.build = None
        self.last_ping = None
        self.phone_status = None
        self.filehandler = None
        self.s3_bucket = None
        self.treeherder = None

    def _check_device(self):
        for attempt in range(1, self.options.phone_retry_limit+1):
            output = self.dm.get_state()
            if output == 'device':
                break
            self.loggerdeco.warning(
                'PhoneTest:_check_device Attempt: %d, %s' %
                (attempt, output))
            time.sleep(self.options.phone_retry_wait)
        if output != 'device':
            raise ADBError('PhoneTest:_check_device: Failed')

    def is_alive(self):
        """Call from main process."""
        try:
            if self.options.verbose:
                logger.debug('is_alive: PhoneWorkerSubProcess.p %s, pid %s' % (
                    self.p, self.p.pid if self.p else None))
            return self.p and self.p.is_alive()
        except Exception:
            logger.exception('is_alive: PhoneWorkerSubProcess.p %s, pid %s' % (
                self.p, self.p.pid if self.p else None))
        return False

    def start(self, phone_status=None):
        """Call from main process."""
        logger.debug('PhoneWorkerSubProcess:starting: %s %s' % (self.phone.id,
                                                                phone_status))
        if self.p:
            if self.is_alive():
                logger.debug('PhoneWorkerSubProcess:start - %s already alive' %
                             self.phone.id)
                return
            del self.p
        self.phone_status = phone_status
        self.p = multiprocessing.Process(target=self.run, name=self.phone.id)
        #self.p.daemon = True
        self.p.start()
        logger.debug('PhoneWorkerSubProcess:started: %s %s' % (self.phone.id,
                                                               self.p.pid))

    def stop(self):
        """Call from main process."""
        logger.debug('PhoneWorkerSubProcess:stopping %s' % self.phone.id)
        if self.is_alive():
            logger.debug('PhoneWorkerSubProcess:stop p.terminate() %s %s %s' %
                         (self.phone.id, self.p, self.p.pid))
            self.p.terminate()
            logger.debug('PhoneWorkerSubProcess:stop p.join() %s %s %s' %
                         (self.phone.id, self.p, self.p.pid))
            self.p.join(self.options.phone_command_queue_timeout*2)
            logger.debug('PhoneWorkerSubProcess:stop %s %s %s alive %s' %
                         (self.phone.id, self.p, self.p.pid, self.p.is_alive()))

    def is_disconnected(self):
        return self.phone_status == PhoneStatus.DISCONNECTED

    def is_disabled(self):
        return self.phone_status == PhoneStatus.DISABLED

    def update_status(self, build=None, phone_status=None,
                      message=None):
        self.loggerdeco.debug('PhoneWorkerSubProcess:update_status')
        if phone_status:
            self.phone_status = phone_status
        phone_message = PhoneTestMessage(self.phone, build=build,
                                         phone_status=self.phone_status,
                                         message=message)
        self.loggerdeco.info(str(phone_message))
        try:
            self.autophone_queue.put_nowait(phone_message)
        except Queue.Full:
            self.loggerdeco.warning('Autophone queue is full!')

    def _check_sdcard(self):
        self.loggerdeco.info('Checking SD card.')
        success = True
        try:
            d = posixpath.join(self.dm.test_root, 'autophonetest')
            self.dm.rm(d, recursive=True, force=True)
            self.dm.mkdir(d, parents=True)
            with tempfile.NamedTemporaryFile() as tmp:
                tmp.write('autophone test\n')
                tmp.flush()
                self.dm.push(tmp.name,
                             posixpath.join(d, 'sdcard_check'))
            self.dm.rm(d, recursive=True)
        except (ADBError, ADBTimeoutError):
            self.loggerdeco.exception('Exception while checking SD card!')
            success = False
        return success

    def check_sdcard(self):
        self.loggerdeco.info('Checking SD card.')
        success = self._check_sdcard()

        if not success:
            # FIXME: Should this be called under more circumstances than just
            # checking the SD card?
            self.clear_test_base_paths()
            return False

        # reset status if there had previous been an error.
        # FIXME: should send email that phone is back up.
        if self.is_disconnected():
            self.update_status(phone_status=PhoneStatus.IDLE)
        return True

    def clear_test_base_paths(self):
        self.loggerdeco.debug('PhoneWorkerSubProcess:clear_test_base_paths')
        for t in self.tests:
            t._base_device_path = ''

    def recover_phone(self):
        self.loggerdeco.debug('PhoneWorkerSubProcess:recover_phone')
        exc = None
        reboots = 0
        while reboots < self.options.phone_max_reboots:
            self.loggerdeco.info('Rebooting phone...')
            reboots += 1
            try:
                if self.dm.reboot():
                    self.loggerdeco.info('Phone is back up.')
                    if self.check_sdcard():
                        return
                    self.loggerdeco.info('Failed SD card check.')
                else:
                    self.loggerdeco.info('Phone did not reboot successfully.')
            except (ADBError, ADBTimeoutError):
                self.loggerdeco.exception('Exception while rebooting!')

        self.loggerdeco.info('Phone has been rebooted %d times; giving up.' %
                             reboots)
        msg_body = 'Phone was rebooted %d times.' % reboots
        if exc:
            msg_body += '\n\n%s' % exc
        self.phone_disconnected(msg_body)

    def reboot(self):
        self.loggerdeco.debug('PhoneWorkerSubProcess:reboot')
        self.update_status(phone_status=PhoneStatus.REBOOTING)
        self.recover_phone()

    def phone_disconnected(self, msg_body):
        """Indicate that a phone has become unreachable or experienced a
        error from which we might be able to recover."""
        if self.is_disconnected() or self.is_disabled():
            return
        self.loggerdeco.warning('Phone disconnected: %s.' % msg_body)
        self.loggerdeco.info('Sending notification...')
        self.mailer.send('Phone %s disconnected' % self.phone.id,
                         'Phone %s appears to be disconnected:\n'
                         '\n'
                         '%s\n'
                         '\n'
                         'I\'ll keep trying to ping it periodically '
                         'in case it reappears.' %
                         (self.phone.id, msg_body))
        self.update_status(phone_status=PhoneStatus.DISCONNECTED)

    def disable_phone(self, errmsg, send_email=True):
        """Completely disable phone. No further attempts to recover it will
        be performed unless initiated by the user."""
        self.loggerdeco.info('Disabling phone: %s.' % errmsg)
        if errmsg and send_email:
            self.loggerdeco.info('Sending notification...')
            self.mailer.send('Phone %s was disabled' % self.phone.id,
                             'Phone %s has been disabled:\n'
                             '\n'
                             '%s\n'
                             '\n'
                             'I gave up on it. Sorry about that. '
                             'You can manually re-enable it with '
                             'the "enable" command.' %
                             (self.phone.id, errmsg))
        self.update_status(phone_status=PhoneStatus.DISABLED,
                           message=errmsg)

    def ping(self):
        self.loggerdeco.info('Pinging phone')
        # Can not use device root to check on phone,
        # since it no longer contacts the phone in
        # adb land. Just do a check_sdcard
        return self._check_sdcard()

    def check_battery(self):
        if self.dm.get_battery_percentage() < self.options.device_battery_min:
            while self.dm.get_battery_percentage() < self.options.device_battery_max:
                self.update_status(phone_status=PhoneStatus.CHARGING,
                                   build=self.build)
                time.sleep(900)

    def cancel_test(self, test_guid):
        """Cancel a job.

        If the test is currently queued up in run_tests(), mark it as
        canceled, then delete the test from the entry in the jobs
        database and we are done. There is no need to notify
        treeherder as it will handle marking the job as cancelled.

        """
        self.loggerdeco.debug('cancel_test: test.job_guid %s' % test_guid)
        tests = PhoneTest.match(job_guid=test_guid)
        if tests:
            assert len(tests) == 1, "test.job_guid %s is not unique" % test_guid
            for test in tests:
                test.test_result.status = PhoneTestResult.USERCANCEL
        self.jobs.cancel_test(test_guid, device=self.phone.id)

    def install_build(self, job):
        ### Why are we retrying here? is it helpful at all?
        """Install the build for this job.

        returns {success: Boolean, message: ''}
        """
        self.update_status(phone_status=PhoneStatus.INSTALLING,
                           build=self.build,
                           message='%s %s' % (job['tree'], job['build_id']))
        self.loggerdeco.info('Installing build %s.' % self.build.id)
        # Record start time for the install so can track how long this takes.
        start_time = datetime.datetime.now()
        message = ''
        for attempt in range(1, self.options.phone_retry_limit+1):
            uninstalled = False
            try:
                # Uninstall all org.mozilla.(fennec|firefox) packages
                # to make sure there are no previous installations of
                # different versions of fennec which may interfere
                # with the test.
                mozilla_packages = [
                    p.replace('package:', '') for p in
                    self.dm.shell_output("pm list package org.mozilla").split()
                    if re.match('package:.*(fennec|firefox)', p)]
                for p in mozilla_packages:
                    self.dm.uninstall_app(p)
                self.dm.reboot()
                uninstalled = True
                break
            except ADBError, e:
                if e.message.find('Failure') != -1:
                    # Failure indicates the failure was due to the
                    # app not being installed.
                    uninstalled = True
                break
                message = 'Exception uninstalling fennec attempt %d!\n\n%s' % (
                    attempt, traceback.format_exc())
                self.loggerdeco.exception('Exception uninstalling fennec '
                                          'attempt %d' % attempt)
            except ADBTimeoutError, e:
                message = 'Timed out uninstalling fennec attempt %d!\n\n%s' % (
                    attempt, traceback.format_exc())
                self.loggerdeco.exception('Timedout uninstalling fennec '
                                          'attempt %d' % attempt)
            time.sleep(self.options.phone_retry_wait)

        if not uninstalled:
            self.loggerdeco.warning('Failed to uninstall fennec.')
            return {'success': False, 'message': message}

        message = ''
        for attempt in range(1, self.options.phone_retry_limit+1):
            try:
                self.dm.install_app(os.path.join(self.build.dir,
                                                'build.apk'))
                stop_time = datetime.datetime.now()
                self.loggerdeco.info('Install build %s elapsed time: %s' % (
                    (job['build_url'], stop_time - start_time)))
                return {'success': True, 'message': ''}
            except ADBError, e:
                message = 'Exception installing fennec attempt %d!\n\n%s' % (
                    attempt, traceback.format_exc())
                self.loggerdeco.exception('Exception installing fennec '
                                          'attempt %d' % attempt)
            except ADBTimeoutError, e:
                message = 'Timed out installing fennec attempt %d!\n\n%s' % (
                    attempt, traceback.format_exc())
                self.loggerdeco.exception('Timedout installing fennec '
                                          'attempt %d' % attempt)
            time.sleep(self.options.phone_retry_wait)

        self.loggerdeco.warning('Failed to uninstall fennec.')
        return {'success': False, 'message': message}

    def run_tests(self, job):
        self.process_autophone_cmd(None)
        if self.state == ProcessStates.SHUTTINGDOWN or self.is_disabled():
            return False
        is_job_completed = True
        install_status = self.install_build(job)
        self.loggerdeco.info('Running tests for job %s' % job)
        for t in job['tests']:
            self.process_autophone_cmd(None)
            if self.state == ProcessStates.SHUTTINGDOWN or self.is_disabled():
                self.loggerdeco.info('Skipping test %s' % t.name)
                is_job_completed = False
                continue
            if t.test_result.status == PhoneTestResult.USERCANCEL:
                self.loggerdeco.info('Skipping Cancelled test %s' % t.name)
                continue
            self.loggerdeco.info('Running test %s' % t.name)
            is_test_completed = False
            # Save the test's job_quid since it will be reset during
            # the test's tear_down and we will need it to complete the
            # test.
            test_job_guid = t.job_guid
            try:
                t.setup_job()
                self.check_battery()
                if not install_status['success']:
                    self.loggerdeco.info('Not running test %s due to %s' % (
                        t.name, install_status['message']))
                    t.test_failure(t.name, 'TEST-UNEXPECTED-FAIL',
                                   install_status['message'],
                                   PhoneTestResult.EXCEPTION)
                else:
                    try:
                        if not self.is_disabled():
                            t.run_job()
                        is_test_completed = True
                    except (ADBError, ADBTimeoutError):
                        self.loggerdeco.exception('device error during '
                                                  '%s.run_job' % t.name)
                        message = ('Uncaught device error during %s.run_job\n\n%s' % (
                                   t.name, traceback.format_exc()))
                        t.test_failure(
                            t.name,
                            'TEST-UNEXPECTED-FAIL',
                            message,
                            PhoneTestResult.EXCEPTION)
            except (ADBError, ADBTimeoutError):
                self.loggerdeco.exception('device error during '
                                          '%s.setup_job.' % t.name)
                message = ('Uncaught device error during %s.setup_job.\n\n%s' % (
                           t.name, traceback.format_exc()))
                t.test_failure(t.name, 'TEST-UNEXPECTED-FAIL',
                               message, PhoneTestResult.EXCEPTION)

            if not is_test_completed and job['attempts'] < jobs.Jobs.MAX_ATTEMPTS:
                # This test did not run successfully and we have not
                # exceeded the maximum number of attempts, therefore
                # mark this attempt as a RETRY.
                t.test_result.status = PhoneTestResult.RETRY
            try:
                t.teardown_job()
            except:
                self.loggerdeco.exception('device error during '
                                          '%s.teardown_job' % t.name)
                message = ('Uncaught device error during %s.teardown_job\n\n%s' % (
                           t.name, traceback.format_exc()))
                t.test_failure(t.name, 'TEST-UNEXPECTED-FAIL',
                               message, PhoneTestResult.EXCEPTION)
            is_job_completed = is_job_completed and is_test_completed
            # Remove this test from the jobs database whether or not it
            # ran successfully.
            self.jobs.test_completed(test_job_guid)
            if not is_test_completed and job['attempts'] < jobs.Jobs.MAX_ATTEMPTS:
                # This test did not run successfully and we have not
                # exceeded the maximum number of attempts, therefore
                # re-add this test with a new guid so that Treeherder
                # will generate a new job for the next attempt.
                #
                # We must do this after tearing down the job since the
                # t.guid will change as a result of the call to
                # self.jobs.new_job.
                self.jobs.new_job(job['build_url'],
                                  build_id=job['build_id'],
                                  changeset=job['changeset'],
                                  tree=job['tree'],
                                  revision=job['revision'],
                                  revision_hash=job['revision_hash'],
                                  tests=[t],
                                  enable_unittests=job['enable_unittests'],
                                  device=self.phone.id)
                self.treeherder.submit_pending(self.phone.id,
                                               job['build_url'],
                                               job['tree'],
                                               job['revision_hash'],
                                               tests=[t])

        try:
            self.dm.uninstall_app(self.build.app_name)
        except:
            self.loggerdeco.exception('device error during '
                                      'uninstall_app %s' % self.build.app_name)
        return is_job_completed

    def handle_timeout(self):
        if (not self.is_disabled() and
            (not self.last_ping or
             (datetime.datetime.now() - self.last_ping >
              datetime.timedelta(seconds=self.options.phone_ping_interval)))):
            self.last_ping = datetime.datetime.now()
            if self.ping():
                if self.is_disconnected():
                    self.recover_phone()
            else:
                self.loggerdeco.info('Ping unanswered.')
                # No point in trying to recover, since we couldn't
                # even perform a simple action.
                if not self.is_disconnected():
                    self.phone_disconnected('No response to ping.')

    def handle_job(self, job):
        self.loggerdeco.debug('PhoneWorkerSubProcess:handle_job: %s, %s' % (
            self.phone, job))
        self.loggerdeco.info('Checking job %s.' % job['build_url'])
        client = buildserver.BuildCacheClient(port=self.options.build_cache_port)
        self.update_status(phone_status=PhoneStatus.FETCHING,
                           message='%s %s' % (job['tree'], job['build_id']))
        cache_response = client.get(job['build_url'],
                                    enable_unittests=job['enable_unittests'])
        client.close()
        if not cache_response['success']:
            self.loggerdeco.warning('Errors occured getting build %s: %s' %
                                    (job['build_url'], cache_response['error']))
            return
        self.build = BuildMetadata().from_json(cache_response['metadata'])
        self.loggerdeco.info('Starting job %s.' % job['build_url'])
        starttime = datetime.datetime.now()
        if self.run_tests(job):
            self.loggerdeco.info('Job completed.')
            self.jobs.job_completed(job['id'])
        elif self.state == ProcessStates.SHUTTINGDOWN:
            # Decrement the job attempts so that the remaining
            # tests aren't dropped simply due to a number of
            # shutdowns.
            job['attempts'] -= 1
            self.loggerdeco.debug('Shutting down... Reset job id %d attempts to %d.' % (job['id'], job['attempts']))
            self.jobs.set_job_attempts(job['id'], job['attempts'])
        elif self.is_disabled():
            pass
        elif job['attempts'] < jobs.Jobs.MAX_ATTEMPTS:
            self.loggerdeco.info('Job will be retried.')
        else:
            self.loggerdeco.info('Job attempts exceeded, will be deleted.')
        for t in self.tests:
            if t.test_result.status == PhoneTestResult.USERCANCEL:
                self.loggerdeco.warning(
                    'Job %s, Cancelled Test: %s was not reset after '
                    'the Job completed' % (job, t))
                t.test_result.status = PhoneTestResult.SUCCESS
        if not self.is_disconnected() and not self.is_disabled():
            self.update_status(phone_status=PhoneStatus.IDLE,
                               build=self.build)
        stoptime = datetime.datetime.now()
        self.loggerdeco.info('Job elapsed time: %s' % (stoptime - starttime))

    def handle_cmd(self, request, current_test=None):
        """Execute the command dispatched from the Autophone process.

        handle_cmd is used in the worker's main_loop method and in a
        test's run_job method to process pending Autophone
        commands. It returns a dict which is used by tests to
        determine if the currently running test should be terminated
        as a result of the command.

        :param request: tuple containing the command name and
            necessary argument values.

        :param current_test: currently running test. Defaults to
            None. A running test will pass this parameter which will
            be used to determine if a cancel_test request pertains to
            the currently running test and thus should be terminated.

        :returns: {'interrupt': boolean, True if current activity should be aborted
                   'reason': message to be used to indicate reason for interruption}
        """
        self.loggerdeco.debug('PhoneWorkerSubProcess:handle_cmd')
        if not request:
            self.loggerdeco.debug('handle_cmd: No request')
            return {'interrupt': False, 'reason': ''}
        if request[0] == 'shutdown':
            self.loggerdeco.info('Shutting down at user\'s request...')
            self.state = ProcessStates.SHUTTINGDOWN
            return {'interrupt': False, 'reason': ''}
        if request[0] == 'job':
            # This is just a notification that breaks us from waiting on the
            # command queue; it's not essential, since jobs are stored in
            # a db, but it allows the worker to react quickly to a request if
            # it isn't doing anything else.
            self.loggerdeco.debug('Received job command request...')
            return {'interrupt': False, 'reason': ''}
        if request[0] == 'reboot':
            self.loggerdeco.info('Rebooting at user\'s request...')
            self.reboot()
            return {'interrupt': True, 'reason': 'Worker rebooted by administrator'}
        if request[0] == 'disable':
            self.disable_phone('Disabled at user\'s request', False)
            return {'interrupt': True, 'reason': 'Worker disabled by administrator'}
        if request[0] == 'enable':
            self.loggerdeco.info('Enabling phone at user\'s request...')
            if self.is_disabled():
                self.update_status(phone_status=PhoneStatus.IDLE)
                self.last_ping = None
            return {'interrupt': False, 'reason': ''}
        if request[0] == 'cancel_test':
            self.loggerdeco.info('Received cancel_test request %s' % list(request))
            (test_guid,) = request[1]
            self.cancel_test(test_guid)
            if current_test and current_test.job_guid == test_guid:
                return {'interrupt': True, 'reason': 'Running Job Canceled'}
            return {'interrupt': False, 'reason': ''}
        if request[0] == 'ping':
            self.loggerdeco.info('Pinging at user\'s request...')
            self.ping()
            return {'interrupt': False, 'reason': ''}
        self.loggerdeco.debug('handle_cmd: Unknown request %s' % request[0])
        return {'interrupt': False, 'reason': ''}

    def process_autophone_cmd(self, test):
        while True:
            try:
                request = self.queue.get(True, 1)
                command = self.handle_cmd(request, current_test=test)
                if command['interrupt']:
                    return command
            except Queue.Empty:
                return {'interrupt': False, 'reason': ''}

    def main_loop(self):
        self.loggerdeco.debug('PhoneWorkerSubProcess:main_loop')
        # Commands take higher priority than jobs, so we deal with all
        # immediately available commands, then start the next job, if there is
        # one.  If neither a job nor a command is currently available,
        # block on the command queue for PhoneWorker.PHONE_COMMAND_QUEUE_TIMEOUT seconds.
        request = None
        while True:
            try:
                if self.state == ProcessStates.SHUTTINGDOWN:
                    self.update_status(phone_status=PhoneStatus.SHUTDOWN)
                    return
                if not request:
                    request = self.queue.get_nowait()
                self.handle_cmd(request)
                request = None
            except Queue.Empty:
                request = None
                if self.is_disconnected():
                    self.recover_phone()
                if not self.is_disconnected():
                    job = self.jobs.get_next_job(lifo=self.options.lifo, worker=self)
                    if job:
                        if not self.is_disabled():
                            self.handle_job(job)
                        else:
                            self.loggerdeco.info('Job skipped because device is disabled: %s' % job)
                            for t in job['tests']:
                                if t.test_result.status != PhoneTestResult.USERCANCEL:
                                    t.test_failure(
                                        t.name,
                                        'TEST_UNEXPECTED_FAIL',
                                        'Worker disabled by administrator',
                                        PhoneTestResult.USERCANCEL)
                                self.treeherder.submit_complete(
                                    t.phone.id,
                                    job['build_url'],
                                    job['tree'],
                                    job['revision_hash'],
                                    tests=[t])
                            self.jobs.job_completed(job['id'])
                    else:
                        try:
                            request = self.queue.get(
                                timeout=self.options.phone_command_queue_timeout)
                        except Queue.Empty:
                            request = None
                            self.handle_timeout()

    def run(self):
        global logger

        sys.stdout = file(self.outfile, 'a', 0)
        sys.stderr = sys.stdout
        # Complete initialization of PhoneWorkerSubProcess in the new
        # process.
        sensitive_data_filter = SensitiveDataFilter(self.options.sensitive_data)
        logger = logging.getLogger()
        logger.addFilter(sensitive_data_filter)
        logger.propagate = False
        logger.setLevel(self.loglevel)
        # Remove any handlers inherited from the main process.  This
        # prevents these handlers from causing the main process to log
        # the same messages.
        for handler in logger.handlers:
            handler.flush()
            handler.close()
            logger.removeHandler(handler)
        for other_logger_name, other_logger in logger.manager.loggerDict.iteritems():
            if not hasattr(other_logger, 'handlers'):
                continue
            other_logger.addFilter(sensitive_data_filter)
            for other_handler in other_logger.handlers:
                other_handler.flush()
                other_handler.close()
                other_logger.removeHandler(other_handler)
            other_logger.addHandler(logging.NullHandler())

        self.filehandler = logging.FileHandler(self.logfile)
        fileformatstring = ('%(asctime)s|%(process)d|%(threadName)s|%(name)s|'
                            '%(levelname)s|%(message)s')
        fileformatter = logging.Formatter(fileformatstring)
        self.filehandler.setFormatter(fileformatter)
        logger.addHandler(self.filehandler)

        self.loggerdeco = LogDecorator(logger,
                                       {'phoneid': self.phone.id},
                                       '%(phoneid)s|%(message)s')
        # Set the loggers for the imported modules
        for module in (autophonetreeherder, builds, jobs, mailer, phonetest,
                       s3, utils):
            module.logger = logger
        self.loggerdeco.info('Worker: Connecting to %s...' % self.phone.id)
        # Override mozlog.logger
        self.dm._logger = self.loggerdeco

        self.jobs = jobs.Jobs(self.mailer, self.phone.id)

        self.loggerdeco.info('Worker: Connected.')

        for t in self.tests:
            t.loggerdeco_original = None
            t.dm_logger_original = None
            t.loggerdeco = self.loggerdeco
            t.worker_subprocess = self
            t.dm = self.dm
            t.update_status_cb = self.update_status
        if self.options.s3_upload_bucket:
            self.s3_bucket = S3Bucket(self.options.s3_upload_bucket,
                                      self.options.aws_access_key_id,
                                      self.options.aws_access_key)
        self.treeherder = AutophoneTreeherder(self,
                                              self.options,
                                              s3_bucket=self.s3_bucket,
                                              mailer=self.mailer,
                                              shared_lock=self.shared_lock)
        self.update_status(phone_status=PhoneStatus.IDLE)
        if not self.check_sdcard():
            self.recover_phone()
        if self.is_disconnected():
            self.loggerdeco.error('Initial SD card check failed.')

        self.main_loop()

