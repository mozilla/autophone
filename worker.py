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
import pytz
import re
import sys
import tempfile
import time
import traceback

from time import sleep

import buildserver
import jobs
import utils
from adb import ADBError, ADBTimeoutError
from autophonetreeherder import AutophoneTreeherder
from builds import BuildMetadata
from logdecorator import LogDecorator
from phonestatus import PhoneStatus
from phonetest import PhoneTest, TreeherderStatus, TestStatus, FLASH_PACKAGE
from process_states import ProcessStates
from s3 import S3Bucket

class Crashes(object):

    CRASH_WINDOW = 30
    CRASH_LIMIT = 5

    def __init__(self, crash_window=CRASH_WINDOW, crash_limit=CRASH_LIMIT):
        self.crash_times = []
        self.crash_window = datetime.timedelta(seconds=crash_window)
        self.crash_limit = crash_limit

    def add_crash(self):
        self.crash_times.append(datetime.datetime.now(tz=pytz.utc))
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
        self.timestamp = datetime.datetime.now(tz=pytz.utc).replace(microsecond=0)

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

    def __init__(self,
                 dm,
                 tests,
                 phone,
                 options,
                 autophone_queue,
                 loglevel,
                 mailer,
                 shared_lock):

        self.state = ProcessStates.STARTING
        self.tests = tests
        self.dm = dm
        self.phone = phone
        self.options = options
        self.last_status_msg = None
        self.first_status_of_type = None
        self.last_status_of_previous_type = None
        # Set up the worker logger which will actually write the worker's log
        # to disk. This will happen in the main process.
        self.logger = utils.getLogger(name=phone.id)
        self.logger.setLevel(loglevel)
        self.loglevel = loglevel
        self.logfile = '%s-%s.log' % (os.path.splitext(options.logfile)[0], phone.id)
        self.create_filehandler()
        self.log_server_flushed_event = multiprocessing.Event()
        self.log_server_closed_event = multiprocessing.Event()
        self.log_flushed_event = multiprocessing.Event()
        self.log_closed_event = multiprocessing.Event()
        self.loggerdeco  = LogDecorator(self.logger,
                                        {},
                                       '%(message)s')
        self.loggerdeco.debug('PhoneWorker:__init__')
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
                                                self,
                                                tests,
                                                phone,
                                                options,
                                                autophone_queue,
                                                self.queue,
                                                loglevel,
                                                mailer,
                                                shared_lock,
                                                self.log_server_flushed_event,
                                                self.log_server_closed_event,
                                                self.log_flushed_event,
                                                self.log_closed_event)

    def create_filehandler(self):
        # Create the worker's log filehandler so that it will be
        # truncated when it is opened if we are submitting results to
        # Treeherder. If we are not submitting to Treeherder the log
        # will be opened for appending. This prevents the loss of the
        # existing log during local testing if Autophone is restarted.
        if self.options.treeherder_url:
            mode = 'w'
        else:
            mode = 'a'
        if hasattr(self, 'filehandler'):
            self.logger.debug('Removing old logger filehandler for %s', self.phone.id)
            self.logger.removeHandler(self.filehandler)
        self.filehandler = logging.FileHandler(self.logfile, mode=mode)
        fileformatter = logging.Formatter(utils.getLoggerFormatString(self.loglevel))
        self.filehandler.setFormatter(fileformatter)
        self.logger.addHandler(self.filehandler)
        self.logger.debug('Created logger filehandler for %s', self.phone.id)

    def is_alive(self):
        return self.subprocess.is_alive()

    def start(self, phone_status=PhoneStatus.IDLE):
        self.loggerdeco.debug('PhoneWorker:start')
        self.state = ProcessStates.RUNNING
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

    def flush_log(self):
        # Wait until the log server has received the flush request.
        self.log_server_flushed_event.wait()
        self.filehandler.flush()
        os.fsync(self.filehandler.stream.fileno())
        # Tell the sub process worker the log has been flushed.
        self.log_flushed_event.set()

    def close_log(self):
        # Wait until the log server has received the close request.
        self.log_server_closed_event.wait()
        self.filehandler.flush()
        os.fsync(self.filehandler.stream.fileno())
        self.filehandler.close()
        # Recreate the file handler to reopen the logfile.
        self.create_filehandler()
        # Tell the sub process worker the log has been closed.
        self.log_closed_event.set()

    def clear_log_events(self):
        self.log_server_flushed_event.clear()
        self.log_server_closed_event.clear()
        self.log_flushed_event.clear()
        self.log_closed_event.clear()

    def process_msg(self, msg):
        """These are status messages routed back from the autophone_queue
        listener in the main AutoPhone class. There is probably a bit
        clearer way to do this..."""
        if not self.last_status_msg or \
           msg.phone_status != self.last_status_msg.phone_status:
            self.last_status_of_previous_type = self.last_status_msg
            self.first_status_of_type = msg
        if msg.message == 'Heartbeat':
            self.last_status_msg.timestamp = msg.timestamp
        elif msg.message == 'logcontrol: close log':
            self.close_log()
        elif msg.message == 'logcontrol: flush log':
            self.flush_log()
        elif msg.message == 'logcontrol: test complete':
            self.clear_log_events()
        else:
            self.loggerdeco.debug('PhoneWorker:process_msg: %s', msg)
            self.last_status_msg = msg

    def status(self):
        response = ''
        now = datetime.datetime.now(tz=pytz.utc).replace(microsecond=0)
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

class Logcat(object):
    def __init__(self, worker_subprocess):
        self.worker_subprocess = worker_subprocess
        self.logger = worker_subprocess.loggerdeco
        self._accumulated_logcat = []
        self.logger.debug('Logcat()')

    def get(self, full=False):
        """Return the contents of logcat as list of strings.

        :param full: optional boolean which defaults to False. If full
                     is False, then get() will only return logcat
                     output since the last call to clear(). If
                     full is True, then get() will return all
                     logcat output since the test was initialized or
                     teardown_job was last called.
        """

        # Get the datetime from the last logcat message
        # previously collected. Note that with the time
        # format, logcat lines begin with a date time of the
        # form: 09-17 16:45:04.370 which is the first 18
        # characters of the line.
        if self._accumulated_logcat:
            logcat_datestr = self._accumulated_logcat[-1][:18]
        else:
            logcat_datestr = '00-00 00:00:00.000'

        self.logger.debug('Logcat.get() since %s', logcat_datestr)

        # adb logcat can return lines with bogus dates where the MM-DD
        # is not correct. This in particular has happened on a Samsung
        # Galaxy S3 where Vold emits lines with the fixed date
        # 11-30. It is not known if this is a general problem with
        # other devices.

        # This incorrect date causes problems when attempting to use
        # the logcat dates to eliminate duplicates. It also conflicts
        # with the strategy used in analyze_logcat to determine a
        # potential year change during a test run. To distinguish
        # between false year changes, we can decide that if the
        # previous date and the current date are different by more
        # than an hour, a decision must be made on which date is
        # legitimate.

        for attempt in range(1, self.worker_subprocess.options.phone_retry_limit+1):
            try:
                raw_logcat = [
                    unicode(x, 'UTF-8', errors='replace').strip()
                    for x in self.worker_subprocess.dm.get_logcat(filter_specs=['*:V'])]
                break
            except ADBError:
                self.logger.exception('Attempt %d get logcat', attempt)
                if attempt == self.worker_subprocess.options.phone_retry_limit:
                    raise
                sleep(self.worker_subprocess.options.phone_retry_wait)

        current_logcat = []
        prev_line_date = None
        curr_line_date = None
        curr_year = datetime.datetime.utcnow().year
        hour = datetime.timedelta(hours=1)

        for line in raw_logcat:
            try:
                curr_line_date = datetime.datetime.strptime('%4d-%s' % (
                    curr_year, line[:18]), '%Y-%m-%d %H:%M:%S.%f')
            except ValueError:
                curr_line_date = None
            if curr_line_date and prev_line_date:
                delta = curr_line_date - prev_line_date
                prev_line_datestr = prev_line_date.strftime('%m-%d %H:%M:%S.%f')
                if delta <= -hour:
                    # The previous line's date is one or more hours in
                    # the future compared to the current line. Keep
                    # the current lines which are before the previous
                    # line's date.
                    new_current_logcat = []
                    for x in current_logcat:
                        if x < prev_line_datestr:
                            if self.worker_subprocess.options.verbose:
                                self.logger.debug('Logcat.get(): Discarding future line: %s', x)
                        else:
                            if self.worker_subprocess.options.verbose:
                                self.logger.debug('Logcat.get(): keeping line: %s', x)
                            new_current_logcat.append(x)
                    current_logcat = new_current_logcat
                elif delta >= hour:
                    # The previous line's date is one or more hours in
                    # the past compared to the current line. Keep the
                    # current lines which are after the previous
                    # line's date.
                    new_current_logcat = []
                    for x in current_logcat:
                        if x > prev_line_datestr:
                            if self.worker_subprocess.options.verbose:
                                self.logger.debug('Logcat.get(): Discarding past line: %s', x)
                        else:
                            if self.worker_subprocess.options.verbose:
                                self.logger.debug('Logcat.get(): keeping line: %s', x)
                            new_current_logcat.append(x)
                    current_logcat = new_current_logcat
            # Keep the messages which are on or after the last accumulated
            # logcat date.
            if line >= logcat_datestr:
                current_logcat.append(line)
            prev_line_date = curr_line_date

        # In order to eliminate the possible duplicate
        # messages, partition the messages by before, on and
        # after the logcat_datestr.
        accumulated_logcat_before = []
        accumulated_logcat_now = []
        for x in self._accumulated_logcat:
            if x < logcat_datestr:
                accumulated_logcat_before.append(x)
            elif x[:18] == logcat_datestr:
                accumulated_logcat_now.append(x)

        current_logcat_now = []
        current_logcat_after = []
        for x in current_logcat:
            if x[:18] == logcat_datestr:
                current_logcat_now.append(x)
            elif x > logcat_datestr:
                current_logcat_after.append(x)

        # Remove any previously received messages from
        # current_logcat_now for the logcat_datestr.
        current_logcat_now = set(current_logcat_now).difference(
            set(accumulated_logcat_now))
        current_logcat_now = list(current_logcat_now)
        current_logcat_now.sort()

        current_logcat = current_logcat_now + current_logcat_after
        self._accumulated_logcat += current_logcat

        if full:
            return self._accumulated_logcat
        return current_logcat

    def reset(self):
        """Clears the Logcat buffers and the device's logcat buffer."""
        self.logger.debug('Logcat.reset()')
        self.__init__(self.worker_subprocess)
        self.worker_subprocess.dm.clear_logcat()

    def clear(self):
        """Accumulates current logcat buffers, then clears the device's logcat
        buffers. clear() is used to prevent the device's logcat buffer
        from overflowing while not losing any output.
        """
        self.logger.debug('Logcat.clear()')
        self.get()
        self.worker_subprocess.dm.clear_logcat()


class PhoneWorkerSubProcess(object):

    """Worker subprocess.

    FIXME: Would be nice to have test results uploaded outside of the
    test objects, and to have them queued (and cached) if the results
    server is unavailable for some reason.  Might be best to communicate
    this back to the main AutoPhone process.
    """

    def __init__(self, dm, parent_worker, tests, phone, options,
                 autophone_queue, queue, loglevel, mailer, shared_lock,
                 log_server_flushed_event, log_server_closed_event,
                 log_flushed_event, log_closed_event):

        self.state = ProcessStates.RUNNING
        self.parent_worker = parent_worker
        self.tests = tests
        self.dm = dm
        self.phone = phone
        self.options = options
        # Grab the main process logger for this worker that was set up
        # in the PhoneWorker class. When the subprocess is started via
        # run(), it will be reset to the socket streaming logger.
        logger = utils.getLogger(name=phone.id)
        self.loggerdeco = LogDecorator(logger,
                                       {},
                                       '%(message)s')
        # PhoneWorkerSubProcess.autophone_queue is used to pass
        # messages back to the main Autophone process while
        # PhoneWorkerSubProcess.queue is used to get messages from the
        # main process.
        self.autophone_queue = autophone_queue
        self.queue = parent_worker.queue
        self.outfile = '%s-%s.out' % (os.path.splitext(options.logfile)[0], phone.id)
        self.test_logfile = None
        self.loglevel = loglevel
        self.mailer = mailer
        self.shared_lock = shared_lock
        self.log_server_flushed_event = log_server_flushed_event
        self.log_server_closed_event = log_server_closed_event
        self.log_flushed_event = log_flushed_event
        self.log_closed_event = log_closed_event
        self.p = None
        self.jobs = None
        self.build = None
        self.last_ping = None
        self.phone_status = None
        self.s3_bucket = None
        self.treeherder = None
        self.logcat = None
        # Treeherder log step processing.
        self.log_step_formatstring = "\n%s %s %s (results: 0, elapsed: %d secs) (at %s) %s"
        self.log_step_stack = []
        self.log_step_eq = 9 * "="

    def log_step(self, step_name):
        line = ''
        now = datetime.datetime.now()
        dt = now.strftime('%Y-%m-%d %H:%M:%S.%f')
        if len(self.log_step_stack) > 0:
            data = self.log_step_stack.pop()
            seconds = (now - data['datetime']).seconds
            line = self.log_step_formatstring % (self.log_step_eq,
                                                 "Finished",
                                                 data['step_name'],
                                                 seconds,
                                                 dt,
                                                 self.log_step_eq)
        data = {'step_name': step_name, 'datetime': now}
        self.log_step_stack.append(data)
        line += self.log_step_formatstring % (self.log_step_eq,
                                              "Started",
                                              step_name,
                                              0,
                                              dt,
                                              self.log_step_eq)
        self.loggerdeco.info(line)


    def is_alive(self):
        """Call from main process."""
        try:
            if self.options.verbose:
                self.loggerdeco.debug('is_alive: PhoneWorkerSubProcess.p %s, pid %s',
                                      self.p, self.p.pid if self.p else None)
            return self.p and self.p.is_alive()
        except Exception:
            self.loggerdeco.exception('is_alive: PhoneWorkerSubProcess.p %s, pid %s',
                                      self.p, self.p.pid if self.p else None)
        return False

    def start(self, phone_status=None):
        """Call from main process."""
        self.loggerdeco.debug('starting: %s %s', self.phone.id,
                              phone_status)
        if self.p:
            if self.is_alive():
                self.loggerdeco.debug('start - %s already alive',
                                      self.phone.id)
                return
            del self.p
        self.phone_status = phone_status
        self.p = multiprocessing.Process(target=self.run,
                                         name=self.phone.id)
        self.p.start()
        self.loggerdeco.debug('started: %s %s', self.phone.id,
                              self.p.pid)

    def stop(self):
        """Call from main process."""
        self.loggerdeco.debug('stopping %s', self.phone.id)
        if self.is_alive():
            self.loggerdeco.debug('stop p.terminate() %s %s %s',
                                  self.phone.id, self.p, self.p.pid)
            self.p.terminate()
            self.loggerdeco.debug('stop p.join() %s %s %s',
                                  self.phone.id, self.p, self.p.pid)
            self.p.join(self.options.phone_command_queue_timeout*2)
            if self.p.is_alive():
                self.loggerdeco.debug('stop killing %s %s '
                                      'stuck process %s',
                                      self.phone.id, self.p, self.p.pid)
                os.kill(self.p.pid, 9)

    def is_ok(self):
        return (self.phone_status != PhoneStatus.DISCONNECTED and
                self.phone_status != PhoneStatus.ERROR)

    def is_disabled(self):
        return self.phone_status == PhoneStatus.DISABLED

    def update_status(self, build=None, phone_status=None,
                      message=None):
        if phone_status:
            self.phone_status = phone_status
        phone_message = PhoneTestMessage(self.phone, build=build,
                                         phone_status=self.phone_status,
                                         message=message)
        if message != 'Heartbeat':
            self.loggerdeco.info(str(phone_message))
        try:
            self.autophone_queue.put_nowait(phone_message)
        except Queue.Full:
            self.loggerdeco.warning('Autophone queue is full!')

    def heartbeat(self):
        self.update_status(message='Heartbeat')

    def flush_log(self):
        """Send messages to the Log Server and the main process to flush the
        log. update_status will simultaneously send a message to the
        main process worker via the autophone_queue, but will also
        send a log message to the logging server which will act upon
        it as well.

        The main process worker will wait on the logging server flushed
        event to make sure that the logging server has received all of
        the messages sent prior to the flush request. The sub process
        worker will wait on the main process worker flush event which
        will signal that the log has been received by the logging
        server and flushed by the main process worker.
        """
        self.update_status(message='logcontrol: flush log')
        self.log_flushed_event.wait()
        self.loggerdeco.info('Waiting for log_flushed_event')
        if self.log_flushed_event.wait():
            self.loggerdeco.info('Got log_flushed_event')

    def close_log(self):
        """Send messages to the Log Server and the main process to close the
        log. update_status will simultaneously send a message to the
        main process worker via the autophone_queue, but will also
        send a log message to the logging server which will act upon
        it as well.

        The main process worker will wait on the logging server closed
        event to make sure that the logging server has received all of
        the messages sent prior to the close request. The sub process
        worker will wait on the main process worker close event which
        will signal that the log has been received by the logging
        server and closed by the main process worker.
        """
        self.update_status(message='logcontrol: close log')
        self.log_closed_event.wait()
        self.loggerdeco.info('Waiting for log_closed_event')
        if self.log_closed_event.wait():
            self.loggerdeco.info('Got log_closed_event')
        # Reset the log steps for the new log.
        self.log_step_stack = []

    def _check_path(self, path):
        self.loggerdeco.debug('Checking path %s.', path)
        success = True
        try:
            d = posixpath.join(path, 'autophone_check_path')
            self.dm.rm(d, recursive=True, force=True, root=True)
            self.dm.mkdir(d, parents=True, root=True)
            self.dm.chmod(d, recursive=True, root=True)
            with tempfile.NamedTemporaryFile() as tmp:
                tmp.write('autophone test\n')
                tmp.flush()
                self.dm.push(tmp.name,
                             posixpath.join(d, 'path_check'))
            self.dm.rm(d, recursive=True, root=True)
        except (ADBError, ADBTimeoutError):
            self.loggerdeco.exception('Exception while checking path %s', path)
            success = False
        return success

    def start_usbwatchdog(self):
        try:
            if not self.dm.is_app_installed(self.options.usbwatchdog_appname):
                return
            debugarg = '--esn debug' if self.options.loglevel == 'DEBUG' else ''

            self.dm.shell_output(
                'am startservice '
                '-n %s/.USBService '
                '--ei poll_interval %s %s' %
                (self.options.usbwatchdog_appname,
                 self.options.usbwatchdog_poll_interval,
                 debugarg))
        except (ADBError, ADBTimeoutError):
            self.loggerdeco.exception('Ignoring Exception starting USBWatchdog')

    def reboot(self):
        self.loggerdeco.info('reboot')
        self.update_status(phone_status=PhoneStatus.REBOOTING)
        self.dm.reboot()
        # Setting svc power stayon true after rebooting is necessary
        # since the setting does not survive reboots. This is also the
        # case for the optional usbwatchdog service.
        self.dm.power_on()
        self.start_usbwatchdog()
        self.ping()

    def disable_phone(self, errmsg, send_email=True):
        """Completely disable phone. No further attempts to recover it will
        be performed unless initiated by the user."""
        self.loggerdeco.info('Disabling phone: %s.', errmsg)
        if errmsg and send_email:
            self.loggerdeco.info('Sending notification...')
            self.mailer.send('%s %s was disabled' % (utils.host(),
                                                     self.phone.id),
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

    def ping(self, test=None, require_ip_address=False):
        """Checks if the device is accessible via adb and that its sdcard and
        /data/local/tmp are accessible. If the device is accessible
        via adb but the sdcard or /data/local/tmp are not accessible,
        the device is rebooted in an attempt to recover.
        """
        for attempt in range(1, self.options.phone_retry_limit+1):
            self.loggerdeco.debug('Pinging phone attempt %d', attempt)
            msg = 'Phone OK'
            phone_status = PhoneStatus.OK
            try:
                state = self.dm.get_state(timeout=60)
            except (ADBError, ADBTimeoutError):
                state = 'missing'
            try:
                if state != 'device':
                    msg = 'Attempt: %d, ping state: %s' % (attempt, state)
                    phone_status = PhoneStatus.DISCONNECTED
                elif (self.dm.selinux and
                      self.dm.shell_output('getenforce') != 'Permissive'):
                    msg = 'Attempt: %d, SELinux is not permissive' % attempt
                    phone_status = PhoneStatus.ERROR
                    self.dm.shell_output("setenforce Permissive", root=True)
                elif not self._check_path('/data/local/tmp'):
                    msg = 'Attempt: %d, ping path: %s' % (attempt, '/data/local/tmp')
                    phone_status = PhoneStatus.ERROR
                elif not self._check_path(self.dm.test_root):
                    msg = 'Attempt: %d, ping path: %s' % (attempt, self.dm.test_root)
                    phone_status = PhoneStatus.ERROR
                elif require_ip_address:
                    try:
                        ip_address = self.dm.get_ip_address()
                    except (ADBError, ADBTimeoutError):
                        ip_address = None
                    if not ip_address:
                        msg = 'Device network offline'
                        phone_status = PhoneStatus.ERROR
                        # If a backup wpa_supplicant.conf is available
                        # in /data/local/tmp/, attempt to recover by
                        # turning off wifi, copying the backup
                        # wpa_supplicant.conf to /data/misc/wifi/,
                        # then turning wifi back on.
                        source_wpa = '/data/local/tmp/wpa_supplicant.conf'
                        dest_wpa = '/data/misc/wifi/wpa_supplicant.conf'
                        if self.dm.exists(source_wpa):
                            self.loggerdeco.info('Resetting wpa_supplicant')
                            self.dm.shell_output('svc wifi disable', root=True)
                            self.dm.shell_output('dd if=%s of=%s' % (
                                source_wpa, dest_wpa), root=True)
                            try:
                                # First, attempt to use older chown syntax
                                # chown user.group FILE.
                                self.loggerdeco.debug('attempting chown wifi.wifi')
                                self.dm.shell_output(
                                    'chown wifi.wifi %s' % dest_wpa, root=True)
                            except ADBError, e1:
                                if 'No such user' not in e1.message:
                                    # The error is not a chown syntax
                                    # compatibility issue.
                                    raise
                                self.loggerdeco.debug('attempting chown wifi:wifi')
                                # The error was due to a chown
                                # user.group syntax compatibilty
                                # issue, re-attempt to use the newer
                                # chown syntax chown user:group FILE.
                                self.dm.shell_output('chown wifi:wifi %s' %
                                                     dest_wpa, root=True)
                            self.dm.shell_output('svc wifi enable', root=True)
                if phone_status == PhoneStatus.OK:
                    self.dm.shell_output("setprop usbwatchdog.heartbeat %s" % time.time(),
                                         root=True)
                    if not self.dm.process_exist(self.options.usbwatchdog_appname):
                        self.start_usbwatchdog()
                    break
            except (ADBError, ADBTimeoutError):
                msg = 'Exception pinging device: %s' % traceback.format_exc()
                phone_status = PhoneStatus.ERROR
            self.loggerdeco.warning(msg)
            time.sleep(self.options.phone_retry_wait)
            if self.is_ok() and phone_status == PhoneStatus.ERROR:
                # Only reboot if the previous state was ok.
                self.loggerdeco.warning('Rebooting due to ping failure.')
                try:
                    self.reboot()
                except (ADBError, ADBTimeoutError):
                    phone_status = PhoneStatus.DISCONNECTED
                    msg2 = 'Exception rebooting device: %s' % traceback.format_exc()
                    self.loggerdeco.warning(msg2)
                    msg += '\n\n' + msg2

        if test:
            test_msg = 'during %s %s\n' % (test.name, os.path.basename(test.config_file))
        else:
            test_msg = ''

        if self.is_disabled():
            self.heartbeat()
        elif phone_status == PhoneStatus.ERROR:
            # The phone is in an error state related to its storage or
            # networking and requires user intervention.
            self.loggerdeco.warning('Phone is in an error state %s %s.',
                                    phone_status, msg)
            if self.is_ok():
                msg_subject = ('%s %s is in an error state %s' %
                               (utils.host(), self.phone.id, phone_status))
                msg_body = ("Phone %s requires intervention:\n\n%s\n\n%s\n" %
                            (self.phone.id, msg, test_msg))
                msg_body += ("I'll keep trying to ping it periodically "
                             "in case it reappears.")
                self.mailer.send(msg_subject, msg_body)
            self.update_status(phone_status=phone_status)
        elif phone_status == PhoneStatus.DISCONNECTED:
            # If the phone is disconnected, there is nothing we can do
            # to recover except reboot the host.
            self.loggerdeco.warning('Phone is in an error state %s %s.',
                                    phone_status, msg)
            if self.is_ok():
                msg_subject = ('%s %s is in an error state %s' %
                               (utils.host(), self.phone.id, phone_status))
                msg_body = ("Phone %s is unusable:\n\n%s\n\n%s\n" %
                            (self.phone.id, msg, test_msg))
                if self.options.reboot_on_error:
                    msg_body += ("I'll reboot after shutting down cleanly "
                                 "which will hopefully recover.")
                else:
                    msg_body += ("I'll keep trying to ping it periodically "
                                 "in case it reappears.")
                self.mailer.send(msg_subject, msg_body)
            self.update_status(phone_status=phone_status)
        elif not self.is_ok():
            # The phone has recovered and is usable again.
            self.loggerdeco.warning('Phone has recovered.')
            self.mailer.send('%s %s has recovered' % (utils.host(),
                                                      self.phone.id),
                             'Phone %s is now usable.' % self.phone.id)
            self.update_status(phone_status=PhoneStatus.OK)

        self.last_ping = datetime.datetime.now(tz=pytz.utc)
        return msg

    def check_battery(self, test):
        if self.dm.get_battery_percentage() < self.options.device_battery_min:
            while self.dm.get_battery_percentage() < self.options.device_battery_max:
                self.update_status(phone_status=PhoneStatus.CHARGING,
                                   build=self.build)
                command = self.process_autophone_cmd(test=test, wait_time=60)
                if command['interrupt']:
                    return command
                if self.state == ProcessStates.SHUTTINGDOWN:
                    return {'interrupt': True,
                            'reason': 'Shutdown while charging',
                            'test_result': TreeherderStatus.RETRY}

        return {'interrupt': False, 'reason': '', 'test_result': None}

    def cancel_test(self, test_guid):
        """Cancel a job.

        If the test is currently queued up in run_tests(), mark it as
        canceled, then delete the test from the entry in the jobs
        database and we are done. There is no need to notify
        treeherder as it will handle marking the job as cancelled.

        """
        self.loggerdeco.debug('cancel_test: test.job_guid %s', test_guid)
        tests = PhoneTest.match(job_guid=test_guid)
        if tests:
            assert len(tests) == 1, "test.job_guid %s is not unique" % test_guid
            for test in tests:
                test.status = TreeherderStatus.USERCANCEL
        self.jobs.cancel_test(test_guid, device=self.phone.id)

    def install_build(self, job):
        ### Why are we retrying here? is it helpful at all?
        """Install the build for this job.

        returns {success: Boolean, message: ''}
        """
        self.update_status(phone_status=PhoneStatus.INSTALLING,
                           build=self.build,
                           message='%s %s' % (job['tree'], job['build_id']))
        self.loggerdeco.info('Installing build %s.', self.build.id)
        # Record start time for the install so can track how long this takes.
        start_time = datetime.datetime.now(tz=pytz.utc)
        message = ''
        for attempt in range(1, self.options.phone_retry_limit+1):
            uninstalled = False
            try:
                # Uninstall all org.mozilla.(fennec|firefox|geckoview) packages
                # to make sure there are no previous installations of
                # different versions of fennec which may interfere
                # with the test.
                mozilla_packages = [
                    p.replace('package:', '') for p in
                    self.dm.shell_output("pm list package org.mozilla").split()
                    if re.match('package:.*(fennec|firefox|geckoview)', p)]
                for p in mozilla_packages:
                    self.dm.uninstall_app(p)
                if self.dm.is_app_installed(FLASH_PACKAGE):
                    self.dm.uninstall_app(FLASH_PACKAGE)
                self.reboot()
                uninstalled = True
                break
            except ADBError, e:
                # Failure in the exception message indicates the
                # failure was due to the app not being installed.
                if e.message.find('Failure') == -1:
                    message = 'Exception uninstalling fennec attempt %d!\n\n%s' % (
                        attempt, traceback.format_exc())
                    self.loggerdeco.exception('Exception uninstalling fennec '
                                              'attempt %d', attempt)
                    self.ping()
                else:
                    uninstalled = True
                    break
            except ADBTimeoutError, e:
                message = 'Timed out uninstalling fennec attempt %d!\n\n%s' % (
                    attempt, traceback.format_exc())
                self.loggerdeco.exception('Timedout uninstalling fennec '
                                          'attempt %d', attempt)
                self.ping()
            time.sleep(self.options.phone_retry_wait)

        if not uninstalled:
            self.loggerdeco.warning('Failed to uninstall fennec.')
            return {'success': False, 'message': message}

        message = ''
        for attempt in range(1, self.options.phone_retry_limit+1):
            try:
                self.dm.install_app(self.build.apk)
                stop_time = datetime.datetime.now(tz=pytz.utc)
                self.loggerdeco.info('Install build %s elapsed time: %s',
                                     job['build_url'], stop_time - start_time)
                return {'success': True, 'message': ''}
            except ADBError, e:
                message = 'Exception installing fennec attempt %d!\n\n%s' % (
                    attempt, traceback.format_exc())
                self.loggerdeco.exception('Exception installing fennec '
                                          'attempt %d', attempt)
                self.ping()
            except ADBTimeoutError, e:
                message = 'Timed out installing fennec attempt %d!\n\n%s' % (
                    attempt, traceback.format_exc())
                self.loggerdeco.exception('Timedout installing fennec '
                                          'attempt %d', attempt)
                self.ping()
            time.sleep(self.options.phone_retry_wait)

        self.loggerdeco.warning('Failed to install fennec.')
        return {'success': False, 'message': message}

    def run_tests(self, job):
        """Install build, run tests, report results and uninstall build.
        Returns True if the caller should call job_completed to remove
        the job from the jobs database.

        If an individual test fails to complete, it is re-inserted
        into the jobs database with a new job row but the same number
        of attempts as the original. It will be retried for up to
        jobs.Jobs.MAX_ATTEMPTS times. Therefore even if individual
        tests fail to complete but all of the tests are actually
        attempted to run, we return True to delete the original job.

        In cases where the test run is interrupted by a command or a
        device failure, we will return False to the caller where the
        caller will not delete the original job and will decrement its
        attempts so that jobs are not deleted due to device errors.
        """
        command = self.process_autophone_cmd(test=None)
        if command['interrupt'] or \
            self.state == ProcessStates.SHUTTINGDOWN or \
            self.is_disabled():
            return False
        install_status = self.install_build(job)
        if not install_status['success']:
            self.loggerdeco.info('Not running tests due to %s',
                                 install_status['message'])
            # We have to bump the attempts in the event of an installation
            # failure so that we don't reset the attempts back to 1 when
            # run_tests returns False.
            job['attempts'] += 1
            self.jobs.set_job_attempts(job['id'], job['attempts'])
            return False

        self.loggerdeco.info('Running tests for job %s', job)
        for t in job['tests']:
            if t.status == TreeherderStatus.USERCANCEL:
                self.loggerdeco.info('Skipping Canceled test %s', t.name)
                continue
            command = self.process_autophone_cmd(test=t)
            if command['interrupt'] or \
               self.state == ProcessStates.SHUTTINGDOWN or \
               self.is_disabled() or not self.is_ok():
                self.loggerdeco.info('Skipping test %s', t.name)
                return False
            self.loggerdeco.info('Running test %s', t.name)
            is_test_completed = False
            # Save the test's job_quid since it will be reset during
            # the test's tear_down and we will need it to complete the
            # test.
            test_job_guid = t.job_guid
            try:
                t.setup_job()
                # Note that check_battery calls process_autophone_cmd
                # which can receive commands to cancel the currently
                # running test.
                command = self.check_battery(t)
                if command['interrupt']:
                    t.handle_test_interrupt(command['reason'],
                                            command['test_result'])
                else:
                    try:
                        if self.is_ok() and not self.is_disabled():
                            self.log_step('Run Test')
                            is_test_completed = t.run_job()
                    except (ADBError, ADBTimeoutError):
                        self.loggerdeco.exception('device error during '
                                                  '%s.run_job', t.name)
                        message = ('Uncaught device error during %s.run_job\n\n%s' % (
                            t.name, traceback.format_exc()))
                        t.add_failure(
                            t.name,
                            TestStatus.TEST_UNEXPECTED_FAIL,
                            message,
                            TreeherderStatus.EXCEPTION)
                        self.ping(test=t)
            except:
                self.loggerdeco.exception('device error during '
                                          '%s.setup_job.', t.name)
                message = ('Uncaught device error during %s.setup_job.\n\n%s' % (
                    t.name, traceback.format_exc()))
                t.add_failure(
                    t.name, TestStatus.TEST_UNEXPECTED_FAIL,
                    message, TreeherderStatus.EXCEPTION)
                self.ping(test=t)

            if job['attempts'] >= jobs.Jobs.MAX_ATTEMPTS:
                t.add_failure(
                    t.name, TestStatus.TEST_UNEXPECTED_FAIL,
                    "Job aborted: Exceeded maximum number of attempts: %s" % job,
                    TreeherderStatus.BUSTED)
            elif t.status != TreeherderStatus.USERCANCEL and \
               not is_test_completed:
                # This test did not run successfully and we have not
                # exceeded the maximum number of attempts, therefore
                # mark this attempt as a RETRY.
                t.status = TreeherderStatus.RETRY
            try:
                t.teardown_job()
            except:
                self.loggerdeco.exception('device error during '
                                          '%s.teardown_job', t.name)
                message = ('Uncaught device error during %s.teardown_job\n\n%s' % (
                    t.name, traceback.format_exc()))
                t.add_failure(
                    t.name, TestStatus.TEST_UNEXPECTED_FAIL,
                    message, TreeherderStatus.EXCEPTION)
            # Remove this test from the jobs database whether or not it
            # ran successfully.
            self.jobs.test_completed(test_job_guid)
            if t.status != TreeherderStatus.USERCANCEL and \
               not is_test_completed and \
               job['attempts'] < jobs.Jobs.MAX_ATTEMPTS:
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
                                  build_type=job['build_type'],
                                  build_abi=job['build_abi'],
                                  build_platform=job['build_platform'],
                                  build_sdk=job['build_sdk'],
                                  changeset=job['changeset'],
                                  changeset_dirs=job['changeset_dirs'],
                                  tree=job['tree'],
                                  revision=job['revision'],
                                  builder_type=job['builder_type'],
                                  tests=[t],
                                  enable_unittests=job['enable_unittests'],
                                  device=self.phone.id,
                                  attempts=job['attempts'])
                self.treeherder.submit_pending(self.phone.id,
                                               job['build_url'],
                                               job['tree'],
                                               job['revision'],
                                               job['build_type'],
                                               job['build_abi'],
                                               job['build_platform'],
                                               job['build_sdk'],
                                               job['builder_type'],
                                               tests=[t])

            # If the log is being submitted to Treeherder, it will be
            # truncated after each test. Otherwise it will grow
            # indefinitely.
            # Tell the parent the job is complete so it will clear the log
            # event flags.
            self.update_status(message='Test Complete')
            self.close_log()

        try:
            if self.is_ok():
                self.dm.uninstall_app(self.build.app_name)
        except:
            self.loggerdeco.exception('device error during '
                                      'uninstall_app %s', self.build.app_name)
        return True

    def handle_timeout(self):
        if not self.is_disabled() and \
           (not self.last_ping or \
            datetime.timedelta(seconds=self.options.phone_ping_interval)):
            self.ping()

    def handle_job(self, job):
        if self.options.treeherder_url:
            # Clear the log before starting the new job.
            self.close_log()
        self.log_step('Job Start')
        self.loggerdeco.debug('handle_job: %s, %s',
                              self.phone, job)
        self.loggerdeco.info('Checking job %s.', job['build_url'])
        client = buildserver.BuildCacheClient(port=self.options.build_cache_port)
        self.update_status(phone_status=PhoneStatus.FETCHING,
                           message='%s %s' % (job['tree'], job['build_id']))
        test_package_names = set()
        for t in job['tests']:
            test_package_names.update(t.get_test_package_names())
        cache_response = client.get(
            job['build_url'],
            enable_unittests=job['enable_unittests'],
            test_package_names=test_package_names,
            builder_type=job['builder_type'])
        client.close()
        if not cache_response['success']:
            self.loggerdeco.warning('Errors occured getting build %s: %s',
                                    job['build_url'], cache_response['error'])
            if job['attempts'] >= jobs.Jobs.MAX_ATTEMPTS:
                # We need to create a BuildMetadata object to use
                # while tearing down the job since we were unable to
                # download the build.
                self.build = BuildMetadata(url=job['build_url'],
                                           tree=job['tree'],
                                           buildid=job['build_id'],
                                           revision=job['revision'],
                                           changeset=job['changeset'],
                                           changeset_dirs=job['changeset_dirs'],
                                           build_type=job['build_type'],
                                           treeherder_url=self.options.treeherder_url,
                                           abi=job['build_abi'],
                                           sdk=job['build_sdk'],
                                           platform=job['build_platform'],
                                           builder_type=job['builder_type'])
                for t in job['tests']:
                    t.add_failure(
                        t.name, TestStatus.TEST_UNEXPECTED_FAIL,
                        "Job aborted: Exceeded maximum number of attempts: %s" % job,
                        TreeherderStatus.BUSTED)
                    try:
                        t.teardown_job()
                    except:
                        self.loggerdeco.exception('device error during '
                                                  '%s.teardown_job', t.name)
                        message = ('Uncaught device error during %s.teardown_job\n\n%s' % (
                            t.name, traceback.format_exc()))
                        t.add_failure(
                            t.name, TestStatus.TEST_UNEXPECTED_FAIL,
                            message, TreeherderStatus.EXCEPTION)
            return
        self.build = BuildMetadata().from_json(cache_response['metadata'])
        self.loggerdeco.info('Starting job %s.', job['build_url'])
        starttime = datetime.datetime.now(tz=pytz.utc)
        if self.run_tests(job):
            self.loggerdeco.info('Job completed.')
            self.jobs.job_completed(job['id'])
        else:
            if job['attempts'] >= jobs.Jobs.MAX_ATTEMPTS:
                for t in job['tests']:
                    t.add_failure(
                        t.name, TestStatus.TEST_UNEXPECTED_FAIL,
                        "Job aborted: Exceeded maximum number of attempts: %s" % job,
                        TreeherderStatus.BUSTED)
                    t.teardown_job()
            else:
                # Decrement the job attempts so that the remaining
                # tests aren't dropped simply due to a device error or
                # user command.
                self.loggerdeco.debug(
                    'run_tests Failed. Reset job id %d attempts to %d.',
                    job['id'], job['attempts'])
                job['attempts'] -= 1
                self.jobs.set_job_attempts(job['id'], job['attempts'])
        for t in self.tests:
            if t.status == TreeherderStatus.USERCANCEL:
                self.loggerdeco.warning(
                    'Job %s, Canceled Test: %s was not reset after '
                    'the Job completed', job, t)
                t.status = TreeherderStatus.SUCCESS
        if self.is_ok() and not self.is_disabled():
            self.update_status(phone_status=PhoneStatus.IDLE,
                               build=self.build)
        stoptime = datetime.datetime.now(tz=pytz.utc)
        self.loggerdeco.info('Job elapsed time: %s', (stoptime - starttime))

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
                   'reason': message to be used to indicate reason for interruption,
                   'test_result': PhoneTest Treeherder Test status to be used for the test result}
        """
        self.loggerdeco.debug('handle_cmd: %s', request)
        command = {'interrupt': False, 'reason': '', 'test_result': None}
        if not request:
            self.loggerdeco.debug('handle_cmd: No request')
        elif request[0] == 'shutdown':
            self.loggerdeco.info('Shutting down at user\'s request...')
            self.state = ProcessStates.SHUTTINGDOWN
        elif request[0] == 'job':
            # This is just a notification that breaks us from waiting on the
            # command queue; it's not essential, since jobs are stored in
            # a db, but it allows the worker to react quickly to a request if
            # it isn't doing anything else.
            self.loggerdeco.debug('Received job command request...')
        elif request[0] == 'reboot':
            self.loggerdeco.info("Rebooting at user's request...")
            try:
                self.reboot()
                command['interrupt'] = True
                command['reason'] = 'Worker rebooted by administrator'
                command['test_result'] = TreeherderStatus.RETRY
            except (ADBError, ADBTimeoutError):
                self.loggerdeco.error("Exception rebooting device")
        elif request[0] == 'disable':
            self.disable_phone("Disabled at user's request", False)
            command['interrupt'] = True
            command['reason'] = 'Worker disabled by administrator'
            command['test_result'] = TreeherderStatus.USERCANCEL
        elif request[0] == 'enable':
            self.loggerdeco.info("Enabling phone at user's request...")
            if self.is_disabled():
                self.update_status(phone_status=PhoneStatus.IDLE)
                self.last_ping = None
        elif request[0] == 'cancel_test':
            self.loggerdeco.info('Received cancel_test request %s', list(request))
            (test_guid,) = request[1]
            self.cancel_test(test_guid)
            if current_test and current_test.job_guid == test_guid:
                command['interrupt'] = True
                command['reason'] = 'Running Job Canceled'
                command['test_result'] = TreeherderStatus.USERCANCEL
        elif request[0] == 'ping':
            self.loggerdeco.info("Pinging at user's request...")
            self.ping()
        else:
            self.loggerdeco.debug('handle_cmd: Unknown request %s', request[0])
        return command

    def process_autophone_cmd(self, test=None, wait_time=1, require_ip_address=False):
        """Process any outstanding commands received from the main process,
        then check on the phone's status to see if the device is healthy
        enough to continue testing.
        """
        while True:
            try:
                self.heartbeat()
                request = self.queue.get(True, wait_time)
                command = self.handle_cmd(request, current_test=test)
                if command['interrupt']:
                    return command
            except Queue.Empty:
                reason = self.ping(test=test, require_ip_address=require_ip_address)
                if self.is_ok():
                    return {'interrupt': False,
                            'reason': '',
                            'test_result': None}
                else:
                    return {'interrupt': True,
                            'reason': reason,
                            'test_result': TreeherderStatus.RETRY}

    def main_loop(self):
        self.loggerdeco.debug('main_loop')
        # Commands take higher priority than jobs, so we deal with all
        # immediately available commands, then start the next job, if there is
        # one.  If neither a job nor a command is currently available,
        # block on the command queue for PhoneWorker.PHONE_COMMAND_QUEUE_TIMEOUT seconds.
        request = None
        while True:
            while True:
                try:
                    (pid, status, resource) = os.wait3(os.WNOHANG)
                    self.loggerdeco.debug('Reaped %s %s', pid, status)
                except OSError:
                    break
            try:
                self.heartbeat()
                if self.state == ProcessStates.SHUTTINGDOWN:
                    self.update_status(phone_status=PhoneStatus.SHUTDOWN)
                    return
                if not request:
                    request = self.queue.get_nowait()
                self.handle_cmd(request)
                request = None
            except Queue.Empty:
                request = None
                if not self.is_ok():
                    self.ping()
                if self.is_ok():
                    job = self.jobs.get_next_job(lifo=self.options.lifo, worker=self)
                    if job:
                        if not self.is_disabled():
                            self.handle_job(job)
                        else:
                            self.loggerdeco.info('Job skipped because device is disabled: %s', job)
                            for t in job['tests']:
                                if t.status != TreeherderStatus.USERCANCEL:
                                    t.add_failure(
                                        t.name,
                                        TestStatus.TEST_UNEXPECTED_FAIL,
                                        'Worker disabled by administrator',
                                        TreeherderStatus.USERCANCEL)
                                self.treeherder.submit_complete(
                                    t.phone.id,
                                    job['build_url'],
                                    job['tree'],
                                    job['revision'],
                                    job['build_type'],
                                    job['build_abi'],
                                    job['build_platform'],
                                    job['build_sdk'],
                                    job['builder_type'],
                                    tests=[t])
                            self.jobs.job_completed(job['id'])
                    else:
                        try:
                            request = self.queue.get(
                                timeout=self.options.phone_command_queue_timeout)
                        except Queue.Empty:
                            request = None
                            self.handle_timeout()

        # Clean up before exiting worker process.
        # Clear pending messages from Autophone.
        while True:
            try:
                request = self.queue.get_nowait()
                self.loggerdeco.info('shutting down dropping request: %s', request)
            except Queue.Empty:
                break
        # Reap child processes.
        while True:
            try:
                (pid, status, resource) = os.wait3(os.WNOHANG)
                self.loggerdeco.info('Reaped %s %s', pid, status)
            except OSError:
                break

    def run(self):
        # Complete initialization of PhoneWorkerSubProcess in the new
        # process.
        self.state = ProcessStates.RUNNING
        sys.stdout = file(self.outfile, 'a', 0)
        sys.stderr = sys.stdout

        # use a socket handler to send the log to the main process
        # logging server.
        socket_handler = logging.handlers.SocketHandler(
            'localhost',
            logging.handlers.DEFAULT_TCP_LOGGING_PORT)

        logger = utils.getLogger()
        logger.propagate = False
        logger.setLevel(self.loglevel)
        # Remove any handlers inherited from the main process.  This
        # prevents these handlers from causing the main process to log
        # the same messages.
        for handler in logger.handlers:
            handler.flush()
            handler.close()
            logger.removeHandler(handler)
        logger.addHandler(socket_handler)

        for other_logger_name, other_logger in logger.manager.loggerDict.iteritems():
            if other_logger == logger or not hasattr(other_logger, 'handlers'):
                continue
            logger.debug('Library logger %s', other_logger_name)
            other_logger.setLevel(self.loglevel)
            other_logger.addFilter(utils.getSensitiveDataFilter())
            for other_handler in other_logger.handlers:
                other_handler.flush()
                other_handler.close()
                other_logger.removeHandler(other_handler)
            other_logger.addHandler(socket_handler)

        self.loggerdeco = LogDecorator(logger,
                                       {},
                                       '%(message)s')
        self.logcat = Logcat(self)

        # Close the log here to initialize it for Treeherder.
        self.close_log()
        self.log_step('Starting Worker Process')

        self.loggerdeco.info('Worker: Connecting to %s...', self.phone.id)
        # Override mozlog.logger
        self.dm._logger = self.loggerdeco

        self.jobs = jobs.Jobs(self.mailer,
                              default_device=self.phone.id,
                              allow_duplicates=self.options.allow_duplicate_jobs)

        self.loggerdeco.info('Worker: Connected.')

        for t in self.tests:
            t.set_worker_subprocess(self)
        if self.options.s3_upload_bucket:
            self.s3_bucket = S3Bucket(self.options.s3_upload_bucket,
                                      self.options.aws_access_key_id,
                                      self.options.aws_access_key)
        self.treeherder = AutophoneTreeherder(self,
                                              self.options,
                                              self.jobs,
                                              s3_bucket=self.s3_bucket,
                                              mailer=self.mailer,
                                              shared_lock=self.shared_lock)
        self.update_status(phone_status=PhoneStatus.IDLE)
        self.dm.power_on()
        self.start_usbwatchdog()
        self.ping()
        self.main_loop()
