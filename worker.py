# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

from __future__ import with_statement

import Queue
import datetime
import hashlib
import logging
import multiprocessing
import os
import posixpath
import sys
import tempfile
import time
import traceback
from multiprocessinghandlers import MultiprocessingTimedRotatingFileHandler

from thclient import (TreeherderRequest, TreeherderJobCollection,
                      TreeherderClientError)

import buildserver
import jobs
from adb import ADBError, ADBTimeoutError
from adb_android import ADBAndroid as ADBDevice
from builds import BuildMetadata
from logdecorator import LogDecorator
from phonestatus import PhoneStatus, PhoneTestMessage, TestState, TestResult

def generate_guid(obj):
    sh = hashlib.sha1()
    sh.update("%s%s" % (str(obj), str(time.time())))
    return sh.hexdigest()

def timestamp_now():
    return int(time.mktime(datetime.datetime.now().timetuple()))

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

    def __init__(self, worker_num, tests, phone, options,
                 autophone_queue, logfile_prefix, loglevel, mailer):
        self.phone = phone
        self.options = options
        self.worker_num = worker_num
        self.last_status_msg = None
        self.first_status_of_type = None
        self.last_status_of_previous_type = None
        self.crashes = Crashes(crash_window=options.phone_crash_window,
                               crash_limit=options.phone_crash_limit)
        self.cmd_queue = multiprocessing.Queue()
        self.lock = multiprocessing.Lock()
        self.subprocess = PhoneWorkerSubProcess(self.worker_num,
                                                tests,
                                                phone, options,
                                                autophone_queue,
                                                self.cmd_queue, logfile_prefix,
                                                loglevel, mailer)
        self.logger = logging.getLogger('autophone.worker')
        self.loggerdeco = LogDecorator(self.logger,
                                       {'phoneid': self.phone.id,
                                        'pid': os.getpid()},
                                       '%(phoneid)s|%(pid)s|%(message)s')
        self.loggerdeco.debug('PhoneWorker:__init__')

    def is_alive(self):
        return self.subprocess.is_alive()

    def start(self, phone_status=PhoneStatus.IDLE):
        self.loggerdeco.debug('PhoneWorker:start')
        self.subprocess.start(phone_status)

    def stop(self):
        self.loggerdeco.debug('PhoneWorker:stop')
        self.subprocess.stop()

    def new_job(self):
        self.loggerdeco.debug('PhoneWorker:new_job')
        self.cmd_queue.put_nowait(('job', None))

    def reboot(self):
        self.loggerdeco.debug('PhoneWorker:reboot')
        self.cmd_queue.put_nowait(('reboot', None))

    def disable(self):
        self.loggerdeco.debug('PhoneWorker:disable')
        self.cmd_queue.put_nowait(('disable', None))

    def enable(self):
        self.loggerdeco.debug('PhoneWorker:enable')
        self.cmd_queue.put_nowait(('enable', None))

    def debug(self, level):
        self.loggerdeco.debug('PhoneWorker:debug')
        try:
            level = int(level)
        except ValueError:
            self.loggerdeco.error('Invalid argument for debug: %s' % level)
        else:
            self.options.debug = level
            self.cmd_queue.put_nowait(('debug', level))

    def ping(self):
        self.loggerdeco.debug('PhoneWorker:ping')
        self.cmd_queue.put_nowait(('ping', None))

    def process_msg(self, msg):
        self.loggerdeco.debug('PhoneWorker:process_msg')
        """These are status messages routed back from the autophone_queue
        listener in the main AutoPhone class. There is probably a bit
        clearer way to do this..."""
        if (not self.last_status_msg or
            msg.phone_status != self.last_status_msg.phone_status):
            self.last_status_of_previous_type = self.last_status_msg
            self.first_status_of_type = msg
        self.last_status_msg = msg


class PhoneWorkerSubProcess(object):

    """Worker subprocess.

    FIXME: Would be nice to have test results uploaded outside of the
    test objects, and to have them queued (and cached) if the results
    server is unavailable for some reason.  Might be best to communicate
    this back to the main AutoPhone process.
    """

    def __init__(self, worker_num, tests, phone, options,
                 autophone_queue, cmd_queue, logfile_prefix, loglevel, mailer):
        self.worker_num = worker_num
        self.tests = tests
        self.phone = phone
        self.options = options
        self.autophone_queue = autophone_queue
        self.cmd_queue = cmd_queue
        self.logfile = logfile_prefix + '.log'
        self.outfile = logfile_prefix + '.out'
        self.loglevel = loglevel
        self.mailer = mailer
        self._stop = False
        self.p = None
        self.jobs = jobs.Jobs(self.mailer, self.phone.id)
        self.build = None
        self.last_ping = None
        self.dm = None
        self.phone_status = None
        self.logger = logging.getLogger('autophone.worker.subprocess')
        self.loggerdeco = LogDecorator(self.logger,
                                       {'phoneid': self.phone.id,
                                        'pid': os.getpid()},
                                       '%(phoneid)s|%(pid)s|%(message)s')

        # Moved this from a @property since having the ADBDevice initialized on the fly
        # can cause problems.
        self.loggerdeco.info('Worker: Connecting to %s...' % self.phone.id)
        self.dm = ADBDevice(device=self.phone.serial,
                            logger_name='autophone.worker.adb',
                            device_ready_retry_wait=self.options.device_ready_retry_wait,
                            device_ready_retry_attempts=self.options.device_ready_retry_attempts,
                            verbose=self.options.verbose)

        # Override mozlog.logger
        self.dm._logger = self.loggerdeco
        self.loggerdeco.info('Worker: Connected.')
        self.loggerdeco.debug('PhoneWorkerSubProcess:__init__')
        for t in tests:
            t.worker_subprocess = self

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
        alive = self.p and self.p.is_alive()
        return alive

    def start(self, phone_status=None):
        """Call from main process."""
        self.loggerdeco.debug('PhoneWorkerSubProcess:start: %s' % phone_status)
        if self.p:
            if self.is_alive():
                self.loggerdeco.debug('PhoneWorkerSubProcess:start - already alive')
                return
            del self.p
        self.phone_status = phone_status
        self.p = multiprocessing.Process(target=self.run, name=self.phone.id)
        self.p.daemon = True
        self.p.start()

    def stop(self):
        """Call from main process."""
        self.loggerdeco.debug('PhoneWorkerSubProcess:stop')
        if self.is_alive():
            self.loggerdeco.debug('PhoneWorkerSubProcess:stop p.terminate()')
            self.p.terminate()
            self.loggerdeco.debug('PhoneWorkerSubProcess:stop p.join()')
            self.p.join(self.options.phone_command_queue_timeout*2)
        self.loggerdeco.debug('PhoneWorkerSubProcess:stopped')

    def has_error(self):
        return (self.phone_status == PhoneStatus.DISABLED or
                self.phone_status == PhoneStatus.DISCONNECTED)

    def update_status(self, build=None, phone_status=None,
                      message=None):
        self.loggerdeco.debug('PhoneWorkerSubProcess:update_status')
        self.phone_status = phone_status
        phone_message = PhoneTestMessage(self.phone, build=build,
                                         phone_status=phone_status,
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
        if self.has_error():
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

    def run_tests(self):
        # Check if we will run any tests with this repo
        # and bail if not.
        test_this_repo = False
        for t in self.tests:
            if t.test_this_repo:
                test_this_repo = True
                break
        if not test_this_repo:
            return True

        if not self.has_error():
            self.loggerdeco.info('Rebooting...')
            self.reboot()

        self.submit_treeherder_pending()
        # may have gotten an error trying to reboot, so test again
        if self.has_error():
            self.loggerdeco.info('Phone is in error state; not running tests.')
            self.submit_treeherder_complete(test_result=TestResult.USERCANCEL,
                                            test_message='Device Error')
            return False

        if self.dm.get_battery_percentage() < self.options.device_battery_min:
            while self.dm.get_battery_percentage() < self.options.device_battery_max:
                self.update_status(phone_status=PhoneStatus.CHARGING,
                                   build=self.build)
                time.sleep(900)

        self.update_status(phone_status=PhoneStatus.INSTALLING,
                           build=self.build)
        self.loggerdeco.info('Installing build %s.' % self.build.id)

        installed = False
        for attempt in range(1, self.options.phone_retry_limit+1):
            uninstalled = False
            exc = None
            e = None
            try:
                self.dm.uninstall_app(self.build.app_name, reboot=True)
                uninstalled = True
            except ADBError, e:
                if e.message.find('Failure') == -1:
                    exc = 'Exception uninstalling fennec attempt %d!\n\n%s' % (
                        attempt, traceback.format_exc())
                else:
                    # Failure indicates the failure was due to the
                    # app not being installed.
                    uninstalled = True
            except ADBTimeoutError, e:
                exc = 'Timed out uninstalling fennec attempt %d!\n\n%s' % (
                    attempt, traceback.format_exc())
                break
            if uninstalled:
                try:
                    self.dm.install_app(os.path.join(self.build.dir,
                                                    'build.apk'))
                    installed = True
                    break
                except ADBError, e:
                    exc = 'Exception installing fennec attempt %d!\n\n%s' % (
                        attempt, traceback.format_exc())
                    self.loggerdeco.exception('Exception installing fennec '
                                              'attempt %d!' % attempt)
                    time.sleep(self.options.phone_retry_wait)
                except ADBTimeoutError, e:
                    exc = 'Timed out installing fennec attempt %d!\n\n%s' % (
                        attempt, traceback.format_exc())
                    break
        if not uninstalled:
            self.phone_disconnected(exc)
            self.submit_treeherder_complete(
                test_result=TestResult.EXCEPTION,
                test_message='Device Error: Failed to uninstall fennec: %s' % exc)
            return False
        elif not installed:
            self.phone_disconnected(exc)
            if isinstance(e, ADBTimeoutError):
                self.submit_treeherder_complete(
                    test_result=TestResult.EXCEPTION,
                    test_message='Device Error: Failed to install fennec: %s' % exc)
            else:
                self.submit_treeherder_complete(
                    test_result=TestResult.BUSTED,
                    test_message='Failed to install fennec: %s' % exc)
            return False

        self.loggerdeco.info('Running tests...')
        for t in self.tests:
            if not t.test_this_repo:
                continue
            if self.has_error():
                self.loggerdeco.info('Rebooting...')
                self.reboot()
            if self.has_error():
                self.loggerdeco.warning('run_tests: not running test due '
                                        'to device error! %s %s %s' % (
                                            t.name, t.build.tree, t.build.id))
                self.submit_treeherder_complete(tests=[t],
                                                test_result=TestResult.USERCANCEL,
                                                test_message='Device Error')
                continue
            try:
                t.setup_job()
                t.run_job()
            except (ADBError, ADBTimeoutError):
                exc = ('Uncaught device error while running test!\n\n%s' %
                    traceback.format_exc())
                t.result = TestResult.EXCEPTION
                t.message = exc
                self.loggerdeco.exception('Uncaught device error while '
                                          'running test!')
                self.phone_disconnected(exc)
            finally:
                t.teardown_job()
        self.dm.uninstall_app(self.build.app_name)
        return True

    def handle_timeout(self):
        self.loggerdeco.debug('PhoneWorkerSubProcess:handle_timeout')
        if (self.phone_status != PhoneStatus.DISABLED and
            (not self.last_ping or
             (datetime.datetime.now() - self.last_ping >
              datetime.timedelta(seconds=self.options.phone_ping_interval)))):
            self.last_ping = datetime.datetime.now()
            if self.ping():
                if self.phone_status == PhoneStatus.DISCONNECTED:
                    self.recover_phone()
                if not self.has_error():
                    self.update_status(phone_status=PhoneStatus.IDLE)
            else:
                self.loggerdeco.info('Ping unanswered.')
                # No point in trying to recover, since we couldn't
                # even perform a simple action.
                if not self.has_error():
                    self.phone_disconnected('No response to ping.')

    def handle_job(self, job):
        self.loggerdeco.debug('PhoneWorkerSubProcess:handle_job')
        phoneid = self.phone.id
        abi = self.phone.abi
        build_url = job['build_url']
        self.loggerdeco.debug('handle_job: job: %s, abi: %s' % (job, abi))
        incompatible_job = False
        if abi == 'x86':
            if 'x86' not in build_url:
                incompatible_job = True
        elif abi == 'armeabi-v6':
            if 'armv6' not in build_url:
                incompatible_job = True
        else:
            if 'x86' in build_url or 'armv6' in build_url:
                incompatible_job = True
        if incompatible_job:
            self.loggerdeco.debug('Ignoring incompatible job %s '
                                  'for phone abi %s' %
                                  (build_url, abi))
            self.jobs.job_completed(job['id'])
            return
        # Determine if we will test this build and if we need
        # to enable unittests.
        skip_build = True
        enable_unittests = False
        for test in self.tests:
            test_devices_repos = test.test_devices_repos
            if not test_devices_repos:
                # We know we will test this build, but not yet
                # if any of the other tests enable_unittests.
                skip_build = False
            elif not phoneid in test_devices_repos:
                # This device will not run this test.
                pass
            else:
                for repo in test_devices_repos[phoneid]:
                    if repo in build_url:
                        skip_build = False
                        enable_unittests = test.enable_unittests
                        break
            if not skip_build:
                break
        if skip_build:
            self.loggerdeco.debug('Ignoring job %s ' % build_url)
            self.jobs.job_completed(job['id'])
            return
        self.loggerdeco.info('Checking job %s.' % build_url)
        client = buildserver.BuildCacheClient(port=self.options.build_cache_port)
        self.loggerdeco.info('Fetching build...')
        cache_response = client.get(build_url, enable_unittests=enable_unittests)
        client.close()
        if not cache_response['success']:
            self.loggerdeco.warning('Errors occured getting build %s: %s' %
                                    (build_url, cache_response['error']))
            return
        self.build = BuildMetadata().from_json(cache_response['metadata'])
        self.loggerdeco.info('Starting job %s.' % build_url)
        starttime = datetime.datetime.now()
        if self.run_tests():
            self.loggerdeco.info('Job completed.')
            self.jobs.job_completed(job['id'])
            self.update_status(phone_status=PhoneStatus.IDLE,
                               build=self.build)
        else:
            self.loggerdeco.error('Job failed.')
        stoptime = datetime.datetime.now()
        self.loggerdeco.info('Job elapsed time: %s' % (stoptime - starttime))

    def handle_cmd(self, request):
        self.loggerdeco.debug('PhoneWorkerSubProcess:handle_cmd')
        if not request:
            self.loggerdeco.debug('handle_cmd: No request')
            pass
        elif request[0] == 'stop':
            self.loggerdeco.info('Stopping at user\'s request...')
            self._stop = True
        elif request[0] == 'job':
            # This is just a notification that breaks us from waiting on the
            # command queue; it's not essential, since jobs are stored in
            # a db, but it allows the worker to react quickly to a request if
            # it isn't doing anything else.
            self.loggerdeco.debug('Received job command request...')
            pass
        elif request[0] == 'reboot':
            self.loggerdeco.info('Rebooting at user\'s request...')
            self.reboot()
        elif request[0] == 'disable':
            self.disable_phone('Disabled at user\'s request', False)
        elif request[0] == 'enable':
            self.loggerdeco.info('Enabling phone at user\'s request...')
            if self.has_error():
                self.update_status(phone_status=PhoneStatus.IDLE)
                self.last_ping = None
        elif request[0] == 'debug':
            self.loggerdeco.info('Setting debug level %d at user\'s request...' % request[1])
            self.options.debug = request[1]
            # update any existing ADB objects
            if self.dm:
                self.dm.log_level = self.options.debug
            for t in self.tests:
                t.set_dm_debug(self.options.debug)
        elif request[0] == 'ping':
            self.loggerdeco.info('Pinging at user\'s request...')
            self.ping()
        else:
            self.loggerdeco.debug('handle_cmd: Unknown request %s' % request[0])

    def main_loop(self):
        self.loggerdeco.debug('PhoneWorkerSubProcess:main_loop')
        # Commands take higher priority than jobs, so we deal with all
        # immediately available commands, then start the next job, if there is
        # one.  If neither a job nor a command is currently available,
        # block on the command queue for PhoneWorker.PHONE_COMMAND_QUEUE_TIMEOUT seconds.
        request = None
        while True:
            try:
                if not request:
                    request = self.cmd_queue.get_nowait()
                self.handle_cmd(request)
                request = None
                if self._stop:
                    return
            except Queue.Empty:
                request = None
                if self.has_error():
                    self.recover_phone()
                if not self.has_error():
                    job = self.jobs.get_next_job()
                    if job:
                        self.handle_job(job)
                    else:
                        try:
                            request = self.cmd_queue.get(
                                timeout=self.options.phone_command_queue_timeout)
                        except Queue.Empty:
                            request = None
                            self.handle_timeout()

    def run(self):
        self.loggerdeco = LogDecorator(self.logger,
                                       {'phoneid': self.phone.id,
                                        'pid': os.getpid()},
                                       '%(phoneid)s|%(pid)s|%(message)s')

        self.loggerdeco.info('PhoneWorker starting up.')

        sys.stdout = file(self.outfile, 'a', 0)
        sys.stderr = sys.stdout
        self.filehandler = MultiprocessingTimedRotatingFileHandler(self.logfile,
                                                                   when='midnight',
                                                                   backupCount=7)
        fileformatstring = ('%(asctime)s|%(levelname)s'
                            '|%(message)s')
        self.fileformatter = logging.Formatter(fileformatstring)
        self.filehandler.setFormatter(self.fileformatter)
        self.logger.addHandler(self.filehandler)

        for t in self.tests:
            t.update_status_cb = self.update_status

        self.update_status(phone_status=PhoneStatus.IDLE)
        if not self.check_sdcard():
            self.recover_phone()
        if self.has_error():
            self.loggerdeco.error('Initial SD card check failed.')

        self.main_loop()

    def post_treeherder_request(self, treeherder_job_collection):
        if not self.options.treeherder_url or not self.build.revision_hash:
            return

        req = TreeherderRequest(
            protocol=self.options.treeherder_protocol,
            host=self.options.treeherder_server,
            project=self.build.tree,
            oauth_key=self.options.treeherder_credentials[self.build.tree]['consumer_key'],
            oauth_secret=self.options.treeherder_credentials[self.build.tree]['consumer_secret']
            )

        try:
            for attempt in range(1, self.options.treeherder_retries+1):
                response = req.post(treeherder_job_collection)
                self.loggerdeco.debug('TreeherderRequest attempt %d: '
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
                self.loggerdeco.error(msg)
                self.mailer.send('Attempt %d for Phone %s failed to post to Treeherder' %
                                 (attempt, self.phone.id), msg)
                time.sleep(self.options.treeherder_retry_wait)
        except TreeherderClientError, e:
            self.loggerdeco.exception('Error submitting request to Treeherder')
            self.mailer.send('Error submitting request to Treeherder',
                             'Phone: %s\n'
                             'TreeherderClientError: %s\n'
                             'TreeherderJobCollection %s\n' % (
                                 self.phone.id,
                                 e,
                                 treeherder_job_collection.to_json()))

    def submit_treeherder_pending(self, tests=[]):
        if not self.options.treeherder_url or not self.build.revision_hash:
            return

        tjc = TreeherderJobCollection(job_type='update')

        if not tests:
            tests = self.tests

        for t in tests:
            if not t.test_this_repo:
                self.loggerdeco.debug('submit_treeherder_pending: not creating '
                                      'job for %s %s' % (t.name, t.build.tree))
                continue
            self.loggerdeco.debug('submit_treeherder_pending: creating job for '
                                  '%s %s' % (t.name, t.build.tree))

            t.state = TestState.PENDING
            t.result = None
            t.message = None
            t.submit_timestamp = timestamp_now()
            t.job_guid = generate_guid(t)
            t.job_details = []

            tj = tjc.get_job()
            tj.add_revision_hash(self.build.revision_hash)
            tj.add_project(self.build.tree)
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
            tj.add_build_url(self.build.url)
            tj.add_build_info('android', t.phone.os, t.phone.architecture)
            tj.add_machine_info('android',t.phone.os, t.phone.architecture)
            tj.add_option_collection({'opt': True})
            tjc.add(tj)

        self.loggerdeco.debug('submit_treeherder_pending: tjc: %s' % (
            tjc.to_json()))

        self.post_treeherder_request(tjc)

    def submit_treeherder_running(self, tests=[]):
        if not self.options.treeherder_url or not self.build.revision_hash:
            return

        tjc = TreeherderJobCollection(job_type='update')

        if not tests:
            tests = self.tests

        for t in tests:
            if not t.test_this_repo:
                continue
            self.loggerdeco.debug('submit_treeherder_running: running job '
                                  'for %s %s' % (t.name, t.build.tree))

            t.state = TestState.RUNNING
            t.start_timestamp = timestamp_now()

            tj = tjc.get_job()
            tj.add_revision_hash(self.build.revision_hash)
            tj.add_project(self.build.tree)
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
            tj.add_build_url(self.build.url)
            tj.add_build_info('android', t.phone.os, t.phone.architecture)
            tj.add_machine_info('android',t.phone.os, t.phone.architecture)
            tj.add_option_collection({'opt': True})
            tjc.add(tj)

        self.loggerdeco.debug('submit_treeherder_running: tjc: %s' %
                              tjc.to_json())

        self.post_treeherder_request(tjc)

    def submit_treeherder_complete(self, tests=[],
                                   test_result=None,
                                   test_message=None):
        if not self.options.treeherder_url:
            return

        tjc = TreeherderJobCollection()

        if not tests:
            tests = self.tests

        for t in tests:
            if not t.test_this_repo:
                continue
            self.loggerdeco.debug('run_tests: treeherder: completing job '
                                  'for %s %s' % (t.name, t.build.tree))

            bug_suggestions = []
            t.state = TestState.COMPLETED
            t.end_timestamp = timestamp_now()
            # A usercancelled job may not have a start_timestamp
            # since it may have been cancelled before it started.
            if not t.start_timestamp:
                t.start_timestamp = t.end_timestamp
            if not test_result:
                test_result = t.result
            if not test_message:
                test_message = t.message

            if test_message:
                if test_result == TestResult.SUCCESS:
                    t.job_details.append({
                        'value': test_message,
                        'content_type': 'text',
                        'title': 'Note'
                        })
                else:
                    lines = test_message.split('\n')
                    for line in lines:
                        if not line:
                            continue
                        bug_suggestions.append({
                            'search': line,
                            'bugs': {'open_recent': [], 'all_others': []}
                        })

            try:
                t.job_details.append({
                    'url': t.result_server,
                    'value': 'graph',
                    'content_type': 'link',
                    'title': 'phonedash:'
                    })
            except AttributeError:
                pass

            tj = tjc.get_job()
            tj.add_revision_hash(self.build.revision_hash)
            tj.add_project(self.build.tree)
            tj.add_job_guid(t.job_guid)
            tj.add_job_name(t.job_name)
            tj.add_job_symbol(t.job_symbol)
            tj.add_group_name(t.group_name)
            tj.add_group_symbol(t.group_symbol)
            tj.add_product_name('fennec')
            tj.add_state(TestState.COMPLETED)
            tj.add_result(test_result)
            tj.add_submit_timestamp(t.submit_timestamp)
            tj.add_start_timestamp(t.start_timestamp)
            tj.add_end_timestamp(t.end_timestamp)
            tj.add_machine(t.phone.id)
            tj.add_build_url(self.build.url)
            tj.add_build_info('android', t.phone.os, t.phone.architecture)
            tj.add_machine_info('android',t.phone.os, t.phone.architecture)
            tj.add_option_collection({'opt': True})
            tj.add_artifact('Job Info', 'json', {'job_details': t.job_details})
            if bug_suggestions:
                tj.add_artifact('Bug suggestions', 'json', bug_suggestions)
            tjc.add(tj)

            message = '%s %s %s TestResult: %s' % (self.build.tree,
                                                   self.build.id,
                                                   t.name, t.result)
            if t.message:
                message += ', %s' % t.message
            self.loggerdeco.info(message)

        self.loggerdeco.debug('submit_treeherder_completed: tjc: %s' %
                              tjc.to_json())

        self.post_treeherder_request(tjc)
