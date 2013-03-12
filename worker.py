# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

from __future__ import with_statement

import Queue
import datetime
import logging
import multiprocessing
import os
import posixpath
import socket
import sys
import tempfile
import time
import traceback

import buildserver
import phonetest
from mozdevice import DeviceManagerSUT, DMError


class Crashes(object):

    CRASH_WINDOW = datetime.timedelta(seconds=30)
    MAX_CRASHES = 5

    def __init__(self):
        self.crash_times = []

    def add_crash(self):
        self.crash_times.append(datetime.datetime.now())
        self.crash_times = [x for x in self.crash_times
                            if self.crash_times[-1] - x <= self.CRASH_WINDOW]

    def too_many_crashes(self):
        return len(self.crash_times) >= self.MAX_CRASHES


class PhoneWorker(object):

    """Runs tests on a single phone in a separate process.
    This is the interface to the subprocess, accessible by the main
    process."""

    def __init__(self, worker_num, ipaddr, tests, phone_cfg, user_cfg,
                 autophone_queue, logfile_prefix, loglevel, mailer,
                 build_cache_port):
        self.phone_cfg = phone_cfg
        self.user_cfg = user_cfg
        self.worker_num = worker_num
        self.ipaddr = ipaddr
        self.last_status_msg = None
        self.first_status_of_type = None
        self.last_status_of_previous_type = None
        self.crashes = Crashes()
        self.cmd_queue = multiprocessing.Queue()
        self.lock = multiprocessing.Lock()
        self.subprocess = PhoneWorkerSubProcess(self.worker_num, self.ipaddr,
                                                tests, phone_cfg, user_cfg,
                                                autophone_queue,
                                                self.cmd_queue, logfile_prefix,
                                                loglevel, mailer,
                                                build_cache_port)

    def is_alive(self):
        return self.subprocess.is_alive()

    def start(self, status=phonetest.PhoneTestMessage.IDLE):
        self.subprocess.start(status)

    def stop(self):
        self.subprocess.stop()

    def new_build(self, build_url):
        self.cmd_queue.put_nowait(('build', build_url))

    def reboot(self):
        self.cmd_queue.put_nowait(('reboot', None))

    def disable(self):
        self.cmd_queue.put_nowait(('disable', None))

    def enable(self):
        self.cmd_queue.put_nowait(('enable', None))

    def debug(self, level):
        try:
            level = int(level)
        except ValueError:
            logging.error('Invalid argument for debug: %s' % level)
        else:
            self.user_cfg['debug'] = level
            self.cmd_queue.put_nowait(('debug', level))

    def ping(self):
        self.cmd_queue.put_nowait(('ping', None))

    def process_msg(self, msg):
        """These are status messages routed back from the autophone_queue
        listener in the main AutoPhone class. There is probably a bit
        clearer way to do this..."""
        if not self.last_status_msg or msg.status != self.last_status_msg.status:
            self.last_status_of_previous_type = self.last_status_msg
            self.first_status_of_type = msg
        self.last_status_msg = msg
        logging.info(msg)


class PhoneWorkerSubProcess(object):

    """Worker subprocess.

    FIXME: Would be nice to have test results uploaded outside of the
    test objects, and to have them queued (and cached) if the results
    server is unavailable for some reason.  Might be best to communicate
    this back to the main AutoPhone process.
    """

    MAX_REBOOT_WAIT_SECONDS = 300
    MAX_REBOOT_ATTEMPTS = 3
    PING_SECONDS = 60*15
    CMD_QUEUE_TIMEOUT_SECONDS = 10

    def __init__(self, worker_num, ipaddr, tests, phone_cfg, user_cfg,
                 autophone_queue, cmd_queue, logfile_prefix, loglevel, mailer,
                 build_cache_port):
        self.worker_num = worker_num
        self.ipaddr = ipaddr
        self.tests = tests
        self.phone_cfg = phone_cfg
        self.user_cfg = user_cfg
        self.autophone_queue = autophone_queue
        self.cmd_queue = cmd_queue
        self.logfile = logfile_prefix + '.log'
        self.outfile = logfile_prefix + '.out'
        self.loglevel = loglevel
        self.mailer = mailer
        self.build_cache_port = build_cache_port
        self.p = None
        self.skipped_job_queue = []
        self.current_build = None
        self._dm = None
        self.status = None

    @property
    def dm(self):
        if not self._dm:
            logging.info('Connecting to %s:%d...' %
                         (self.phone_cfg['ip'], self.phone_cfg['sutcmdport']))
            # Droids and other slow phones can take a while to come back
            # after a SUT crash or spontaneous reboot, so we up the
            # default retrylimit.
            self._dm = DeviceManagerSUT(self.phone_cfg['ip'],
                                        self.phone_cfg['sutcmdport'],
                                        retrylimit=8)
            logging.info('Connected.')
        return self._dm

    def is_alive(self):
        """Call from main process."""
        return self.p and self.p.is_alive()

    def start(self, status):
        """Call from main process."""
        if self.p:
            if self.is_alive():
                return
            del self.p
        self.status = status
        self.p = multiprocessing.Process(target=self.loop)
        self.p.start()

    def stop(self):
        """Call from main process."""
        if self.is_alive():
            self.cmd_queue.put_nowait(('stop', None))
            self.p.join(self.CMD_QUEUE_TIMEOUT_SECONDS*2)

    def has_error(self):
        return (self.status == phonetest.PhoneTestMessage.DISABLED or
                self.status == phonetest.PhoneTestMessage.DISCONNECTED)

    def disconnect_dm(self):
        self._dm = None

    def status_update(self, msg):
        self.status = msg.status
        try:
            self.autophone_queue.put_nowait(msg)
        except Queue.Full:
            logging.warn('Autophone queue is full!')

    def check_sdcard(self):
        logging.info('Checking SD card.')
        success = True
        try:
            dev_root = self.dm.getDeviceRoot()
            if dev_root:
                d = posixpath.join(dev_root, 'autophonetest')
                self.dm.removeDir(d)
                self.dm.mkDir(d)
                if self.dm.dirExists(d):
                    with tempfile.NamedTemporaryFile() as tmp:
                        tmp.write('autophone test\n')
                        tmp.flush()
                        self.dm.pushFile(tmp.name,
                                         posixpath.join(d, 'sdcard_check'))
                    self.dm.removeDir(d)
                else:
                    logging.error('Failed to create directory under device '
                                  'root!')
                    success = False
            else:
                logging.error('Invalid device root.')
                success = False
        except DMError:
            logging.error('Exception while checking SD card!')
            logging.error(traceback.format_exc())
            success = False

        if not success:
            # FIXME: Should this be called under more circumstances than just
            # checking the SD card?
            self.clear_test_base_paths()
            return False

        # reset status if there had previous been an error.
        # FIXME: should send email that phone is back up.
        self.status_update(phonetest.PhoneTestMessage(
                self.phone_cfg['phoneid'],
                phonetest.PhoneTestMessage.IDLE))
        return True

    def clear_test_base_paths(self):
        for t in self.tests:
            t._base_device_path = ''

    def recover_phone(self):
        exc = None
        reboots = 0
        while reboots < self.MAX_REBOOT_ATTEMPTS:
            logging.info('Rebooting phone...')
            reboots += 1
            # FIXME: reboot() no longer indicates success/failure; instead
            # we just verify the device root.
            try:
                self.dm.reboot(self.ipaddr, 30000+self.worker_num)
                if self.dm.getDeviceRoot():
                    logging.info('Phone is back up.')
                    if self.check_sdcard():
                        return
                    logging.info('Failed SD card check.')
                else:
                    logging.info('Phone did not reboot successfully.')
            except DMError:
                exc = 'Exception while checking SD card!\n%s' % \
                    traceback.format_exc()
                logging.error(exc)
            # DM can be in a weird state if reboot failed.
            self.disconnect_dm()

        logging.info('Phone has been rebooted %d times; giving up.' %
                     reboots)
        msg_body = 'Phone was rebooted %d times.' % reboots
        if exc:
            msg_body += '\n\n%s' % exc
        self.phone_disconnected(msg_body)

    def reboot(self):
        self.status_update(phonetest.PhoneTestMessage(
                self.phone_cfg['phoneid'],
                phonetest.PhoneTestMessage.REBOOTING))
        self.recover_phone()

    def phone_disconnected(self, msg_body):
        """Indicate that a phone has become unreachable or experienced a
        error from which we might be able to recover."""
        if self.has_error():
            return
        logging.info('Phone disconnected: %s.' % msg_body)
        if msg_body and self.mailer:
            logging.info('Sending notification...')
            try:
                self.mailer.send('Phone %s disconnected' % self.phone_cfg['phoneid'],
                                 '''Hello, this is Autophone. Phone %s appears to be disconnected:

%s

I'll keep trying to ping it periodically in case it reappears.
''' % (self.phone_cfg['phoneid'], msg_body))
                logging.info('Sent.')
            except socket.error:
                logging.error('Failed to send disconnected-phone '
                              'notification.')
                logging.error(traceback.format_exc())
        self.status_update(phonetest.PhoneTestMessage(
                self.phone_cfg['phoneid'],
                phonetest.PhoneTestMessage.DISCONNECTED))

    def disable_phone(self, errmsg, send_email=True):
        """Completely disable phone. No further attempts to recover it will
        be performed unless initiated by the user."""
        logging.info('Disabling phone: %s.' % errmsg)
        if errmsg and send_email and self.mailer:
            logging.info('Sending notification...')
            try:
                self.mailer.send('Phone %s disabled' % self.phone_cfg['phoneid'],
                                 '''Hello, this is Autophone. Phone %s has been disabled:

%s

I gave up on it. Sorry about that. You can manually re-enable it with
the "enable" command.
''' % (self.phone_cfg['phoneid'], errmsg))
                logging.info('Sent.')
            except socket.error:
                logging.error('Failed to send disabled-phone notification.')
                logging.error(traceback.format_exc())
        self.status_update(phonetest.PhoneTestMessage(
                self.phone_cfg['phoneid'],
                phonetest.PhoneTestMessage.DISABLED,
                msg=errmsg))

    def ping(self):
        logging.info('Pinging phone')
        # Verify that the phone is still responding.
        # It should always be possible to get the device root, so use this
        # command to ensure that the device is still reachable.
        try:
            if self.dm.getDeviceRoot():
                logging.info('Pong!')
                return True
        except DMError:
            logging.error('Exception while pinging:')
            logging.error(traceback.format_exc())
        logging.error('Got empty device root!')
        return False

    def run_tests(self, build_metadata):
        if not self.has_error():
            logging.info('Rebooting...')
            self.reboot()

        # may have gotten an error trying to reboot, so test again
        if self.has_error():
            logging.info('Phone is in error state; queuing job '
                         'for later.')
            # FIXME: Write these to a file to resume eventually.
            # We'll need a way (command, cli option) to automatically
            # clear these as well.
            return False

        self.status_update(phonetest.PhoneTestMessage(
                self.phone_cfg['phoneid'],
                phonetest.PhoneTestMessage.INSTALLING,
                build_metadata['blddate']))
        logging.info(
            'Installing build %s.' % datetime.datetime.fromtimestamp(
                float(build_metadata['blddate'])))

        try:
            pathOnDevice = posixpath.join(self.dm.getDeviceRoot(),
                                          'build.apk')
            self.dm.pushFile(os.path.join(build_metadata['cache_build_dir'],
                                          'build.apk'), pathOnDevice)
            self.dm.installApp(pathOnDevice)
            self.dm.removeFile(pathOnDevice)
        except DMError:
            exc = 'Exception installing fennec!\n\n%s' % traceback.format_exc()
            logging.error(exc)
            self.phone_disconnected(exc)
            return False
        self.current_build = build_metadata['blddate']

        logging.info('Running tests...')
        for t in self.tests:
            if self.has_error():
                break
            t.current_build = build_metadata['blddate']
            # TODO: Attempt to see if pausing between jobs helps with
            # our reconnection issues
            time.sleep(30)
            try:
                t.runjob(build_metadata, self)
            except DMError:
                exc = 'Uncaught device error while running test!\n\n%s' % \
                    traceback.format_exc()
                logging.error(exc)
                # FIXME: We should retry the whole thing; as it is, we
                # actually skip this job.
                self.phone_disconnected(exc)
                return False
        return True

    def loop(self):
        sys.stdout = file(self.outfile, 'a', 0)
        sys.stderr = sys.stdout
        print '%s Worker starting up.' % \
            datetime.datetime.now().replace(microsecond=0).isoformat()
        for h in logging.getLogger().handlers:
            logging.getLogger().removeHandler(h)
        logging.basicConfig(filename=self.logfile,
                            filemode='a',
                            level=self.loglevel,
                            format='%(asctime)s|%(levelname)s|%(message)s')

        logging.info('Worker for phone %s starting up.' %
                     self.phone_cfg['phoneid'])

        DeviceManagerSUT.debug = self.user_cfg.get('debug', 3)

        for t in self.tests:
            t.status_cb = self.status_update

        last_ping = None

        self.status_update(phonetest.PhoneTestMessage(
                self.phone_cfg['phoneid'], self.status))

        if self.status != phonetest.PhoneTestMessage.DISABLED:
            if not self.check_sdcard():
                self.recover_phone()
            if self.has_error():
                logging.error('Initial SD card check failed.')

        while True:
            request = None
            try:
                request = self.cmd_queue.get(
                    timeout=self.CMD_QUEUE_TIMEOUT_SECONDS)
            except Queue.Empty:
                if (self.status != phonetest.PhoneTestMessage.DISABLED and
                    (not last_ping or
                     (datetime.datetime.now() - last_ping >
                      datetime.timedelta(seconds=self.PING_SECONDS)))):
                    last_ping = datetime.datetime.now()
                    if self.ping():
                        if (self.status ==
                            phonetest.PhoneTestMessage.DISCONNECTED):
                            self.recover_phone()
                        if not self.has_error():
                            self.status_update(phonetest.PhoneTestMessage(
                                    self.phone_cfg['phoneid'],
                                    phonetest.PhoneTestMessage.IDLE,
                                    self.current_build))
                    else:
                        logging.info('Ping unanswered.')
                        # No point in trying to recover, since we couldn't
                        # even perform a simple action.
                        if not self.has_error():
                            self.phone_disconnected('No response to ping.')
            except KeyboardInterrupt:
                return

            if not request:
                continue
            if request[0] == 'stop':
                return
            if request[0] == 'build':
                build_url = request[1]
                logging.info('Got notification of build %s.' % build_url)
                client = buildserver.BuildCacheClient(
                    port=self.build_cache_port)
                logging.info('Fetching build...')
                cache_response = client.get(build_url)
                client.close()
                if not cache_response['success']:
                    logging.warn('Errors occured getting build %s: %s' %
                                 (build_url, cache_response['error']))
                    continue
                if self.run_tests(cache_response['metadata']):
                    logging.info('Job completed.')
                    self.status_update(phonetest.PhoneTestMessage(
                            self.phone_cfg['phoneid'],
                            phonetest.PhoneTestMessage.IDLE,
                            self.current_build))
                else:
                    logging.error('Job failed; queuing it for later.')
                    self.skipped_job_queue.append(cache_response['metadata'])
            elif request[0] == 'reboot':
                logging.info('Rebooting at user\'s request...')
                self.reboot()
            elif request[0] == 'disable':
                self.disable_phone('Disabled at user\'s request', False)
            elif request[0] == 'enable':
                logging.info('Enabling phone at user\'s request...')
                if self.has_error():
                    self.status_update(phonetest.PhoneTestMessage(
                            self.phone_cfg['phoneid'],
                            phonetest.PhoneTestMessage.IDLE,
                            self.current_build))
                    last_ping = None
                for j in self.skipped_job_queue:
                    self.cmd_queue.put(('job', j))
            elif request[0] == 'debug':
                self.user_cfg['debug'] = request[1]
                DeviceManagerSUT.debug = self.user_cfg['debug']
                # update any existing DeviceManagerSUT objects
                if self._dm:
                    self._dm.debug = self.user_cfg['debug']
                for t in self.tests:
                    t.set_dm_debug(self.user_cfg['debug'])
            elif request[0] == 'ping':
                self.ping()
