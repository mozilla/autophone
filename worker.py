# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

from __future__ import with_statement

import Queue
import StringIO
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

import phonetest
from mozdevice import DeviceManagerSUT


class PhoneWorker(object):

    """Runs tests on a single phone in a separate process.
    This is the interface to the subprocess, accessible by the main
    process."""

    def __init__(self, worker_num, ipaddr, tests, phone_cfg, autophone_queue,
                 logfile_prefix, loglevel, mailer):
        self.phone_cfg = phone_cfg
        self.worker_num = worker_num
        self.ipaddr = ipaddr
        self.last_status_msg = None
        self.first_status_of_type = None
        self.last_status_of_previous_type = None
        self.job_queue = multiprocessing.Queue()
        self.lock = multiprocessing.Lock()
        self.subprocess = PhoneWorkerSubProcess(self.worker_num, self.ipaddr,
                                                tests, phone_cfg,
                                                autophone_queue,
                                                self.job_queue, logfile_prefix,
                                                loglevel, mailer)

    def is_alive(self):
        return self.subprocess.is_alive()

    def start(self, disabled=False):
        self.subprocess.start(disabled)

    def stop(self):
        self.subprocess.stop()

    def add_job(self, job):
        self.job_queue.put_nowait(('job', job))

    def reboot(self):
        self.job_queue.put_nowait(('reboot', None))

    def disable(self):
        self.job_queue.put_nowait(('disable', None))

    def enable(self):
        self.job_queue.put_nowait(('enable', None))

    def debug(self, level):
        try:
            level = int(level)
        except ValueError:
            logging.error('Invalid argument for debug: %s' % level)
        else:
            self.phone_cfg['debug'] = level
            self.job_queue.put_nowait(('debug', level))

    def ping(self):
        self.job_queue.put_nowait(('ping', None))

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
    JOB_QUEUE_TIMEOUT_SECONDS = 10

    def __init__(self, worker_num, ipaddr, tests, phone_cfg, autophone_queue,
                 job_queue, logfile_prefix, loglevel, mailer):
        self.worker_num = worker_num
        self.ipaddr = ipaddr
        self.tests = tests
        self.phone_cfg = phone_cfg
        self.autophone_queue = autophone_queue
        self.job_queue = job_queue
        self.logfile = logfile_prefix + '.log'
        self.outfile = logfile_prefix + '.out'
        self.loglevel = loglevel
        self.mailer = mailer
        self.p = None
        self.disabled = False
        self.skipped_job_queue = []
        self.current_build = None
        self._dm = None

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

    def start(self, disabled=False):
        """Call from main process."""
        if self.p:
            if self.is_alive():
                return
            del self.p
        if disabled:
            self.disabled = True
        self.p = multiprocessing.Process(target=self.loop)
        self.p.start()

    def stop(self):
        """Call from main process."""
        if self.is_alive():
            self.job_queue.put_nowait(('stop', None))
            self.p.join(self.JOB_QUEUE_TIMEOUT_SECONDS*2)

    def is_disabled(self):
        disabled = self.disabled
        return disabled

    def disconnect_dm(self):
        self._dm = None

    def status_update(self, msg):
        try:
            self.autophone_queue.put_nowait(msg)
        except Queue.Full:
            logging.warn('Autophone queue is full!')

    def check_sdcard(self):
        logging.info('Checking SD card.')
        dev_root = self.dm.getDeviceRoot()
        if dev_root is None:
            logging.error('No response from device when querying device root.')
            return False
        d = posixpath.join(dev_root, 'autophonetest')
        self.dm.removeDir(d)
        success = self.dm.mkDir(d) and self.dm.dirExists(d)
        if success:
            with tempfile.NamedTemporaryFile() as tmp:
                tmp.write('autophone test\n')
                tmp.flush()
                success = self.dm.pushFile(tmp.name, posixpath.join(d,
                                                                    'testfile'))
        if not success:
            logging.error('Device root is not writable!')
            logging.info('Checking sdcard...')
            sdcard_writable = self.dm.mkDir('/mnt/sdcard/tests/autophonetest')
            if sdcard_writable:
                logging.error('Weird, sd card is writable but device root isn\'t! I\'m confused and giving up anyway!')
            self.clear_test_base_paths()
        self.dm.removeDir(d)
        return success

    def clear_test_base_paths(self):
        for t in self.tests:
            t._base_device_path = ''

    def recover_phone(self):
        reboots = 0
        while not self.disabled:
            if reboots < self.MAX_REBOOT_ATTEMPTS:
                logging.info('Rebooting phone...')
                reboots += 1
                success = self.dm.reboot(self.ipaddr, 30000+self.worker_num)
                if success:
                    logging.info('Phone is back up.')
                    if self.check_sdcard():
                        self.disabled = False
                        return
                    logging.info('Failed SD card check.')
                else:
                    logging.info('Phone did not reboot successfully.')
                    # DM can be in a weird state if reboot failed.
                    self.disconnect_dm()
            else:
                logging.info('Phone has been rebooted %d times; giving up.' %
                             reboots)
                self.disable_phone('Phone was rebooted %d times.' % reboots)

    def reboot(self):
        self.status_update(phonetest.PhoneTestMessage(
                self.phone_cfg['phoneid'],
                phonetest.PhoneTestMessage.REBOOTING))
        self.recover_phone()
        if not self.disabled:
            self.status_update(phonetest.PhoneTestMessage(
                    self.phone_cfg['phoneid'],
                    phonetest.PhoneTestMessage.IDLE, msg='phone reset'))

    def disable_phone(self, msg_body):
        logging.info('Disabling phone: %s.' % msg_body)
        self.disabled = True
        if msg_body and self.mailer:
            logging.info('Sending notification...')
            try:
                self.mailer.send('Phone %s disabled' % self.phone_cfg['phoneid'],
                                 '''Hello, this is Autophone. Phone %s has been disabled:

%s

We gave up on it. Sorry about that.
''' % (self.phone_cfg['phoneid'], msg_body))
                logging.info('Sent.')
            except socket.error:
                logging.error('Failed to send disabled-phone notification.')
                logging.info(traceback.format_exc())
        self.status_update(phonetest.PhoneTestMessage(
                self.phone_cfg['phoneid'],
                phonetest.PhoneTestMessage.DISABLED))

    def ping(self):
        logging.info('Pinging phone')
        # verify that the phone is still responding
        output = StringIO.StringIO()

        # It should always be possible to get the device root, so use this
        # command to ensure that the device is still reachable.
        if self.dm.getDeviceRoot():
            logging.info('Pong!')
            return True

        logging.info('No response!')
        return False

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

        DeviceManagerSUT.debug = self.phone_cfg.get('debug', 3)

        for t in self.tests:
            t.status_cb = self.status_update

        if self.disabled:
            # In case the worker was started in a disabled state
            self.status_update(phonetest.PhoneTestMessage(
                    self.phone_cfg['phoneid'],
                    phonetest.PhoneTestMessage.DISABLED,
                    msg='Starting in disabled mode.'))
        else:
            self.status_update(phonetest.PhoneTestMessage(
                    self.phone_cfg['phoneid'],
                    phonetest.PhoneTestMessage.IDLE))

            last_ping = None

            if not self.check_sdcard():
                self.recover_phone()
            if self.disabled:
                logging.error('Initial SD card check failed.')

        while True:
            request = None
            try:
                request = self.job_queue.get(timeout=self.JOB_QUEUE_TIMEOUT_SECONDS)
            except Queue.Empty:
                if (not last_ping or
                    (datetime.datetime.now() - last_ping >
                     datetime.timedelta(seconds=self.PING_SECONDS))):
                    last_ping = datetime.datetime.now()
                    if self.ping():
                        if self.disabled:
                            self.recover_phone()
                        if not self.disabled:
                            self.status_update(phonetest.PhoneTestMessage(
                                    self.phone_cfg['phoneid'],
                                    phonetest.PhoneTestMessage.IDLE,
                                    self.current_build))
                    else:
                        logging.info('Ping unanswered.')
                        # No point in trying to recover, since we couldn't
                        # even perform a simple action.
                        if not self.disabled:
                            self.disable_phone('No response to ping.')

            except KeyboardInterrupt:
                return
            if not request:
                continue
            if request[0] == 'stop':
                return
            if request[0] == 'job':
                job = request[1]
                if not job:
                    continue
                logging.info('Got job.')
                if not self.disabled:
                    logging.info('Rebooting...')
                    self.reboot()
                if self.disabled:
                    logging.info('Phone is disabled; queuing job for later.')
                    # FIXME: Write these to a file to resume eventually.
                    # We'll need a way (command, cli option) to automatically
                    # clear these as well.
                    self.skipped_job_queue.append(job)
                    continue
                self.status_update(phonetest.PhoneTestMessage(
                        self.phone_cfg['phoneid'],
                        phonetest.PhoneTestMessage.INSTALLING, job['blddate']))
                logging.info('Installing build %s.' % datetime.datetime.fromtimestamp(float(job['blddate'])))

                pathOnDevice = posixpath.join(self.dm.getDeviceRoot(),
                                              'build.apk')
                self.dm.pushFile(os.path.join(job['cache_build_dir'], 'build.apk'), pathOnDevice)
                self.dm.installApp(pathOnDevice)
                self.dm.removeFile(pathOnDevice)

                self.current_build = job['blddate']
                logging.info('Running tests...')
                for t in self.tests:
                    if self.disabled:
                        break
                    t.current_build = job['blddate']
                    # TODO: Attempt to see if pausing between jobs helps with
                    # our reconnection issues
                    time.sleep(30)
                    # FIXME: We need to detect fatal DeviceManager errors.
                    t.runjob(job)

                if self.disabled:
                    self.status_update(phonetest.PhoneTestMessage(
                            self.phone_cfg['phoneid'],
                            phonetest.PhoneTestMessage.DISABLED))
                else:
                    logging.info('Job completed.')
                    self.status_update(phonetest.PhoneTestMessage(
                            self.phone_cfg['phoneid'],
                            phonetest.PhoneTestMessage.IDLE,
                            self.current_build))
            elif request[0] == 'reboot':
                logging.info('Rebooting at user\'s request...')
                self.reboot()
            elif request[0] == 'disable':
                logging.info('Disabling phone at user\'s request...')
                self.disable_phone(None)
            elif request[0] == 'enable':
                logging.info('Enabling phone at user\'s request...')
                if self.disabled:
                    self.disabled = False
                    last_ping = None
                for j in self.skipped_job_queue:
                    self.job_queue.put(('job', j))
            elif request[0] == 'debug':
                self.phone_cfg['debug'] = request[1]
                DeviceManagerSUT.debug = self.phone_cfg['debug']
                # update any existing DeviceManagerSUT objects
                if self._dm:
                    self._dm.debug = self.phone_cfg['debug']
                for t in self.tests:
                    t.set_dm_debug(self.phone_cfg['debug'])
            elif request[0] == 'ping':
                self.ping()
