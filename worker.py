# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import ConfigParser
import Queue
import datetime
import logging
import multiprocessing
import os
import socket
import time
import traceback

import androidutils
import phonetest
from devicemanagerSUT import DeviceManagerSUT


class PhoneWorker(object):

    """Runs tests on a single phone in a separate process.
    This is the interface to the subprocess, accessible by the main
    process."""

    def __init__(self, worker_num, tests, phone_cfg, autophone_queue, logfile,
                 loglevel, mailer):
        self.phone_cfg = phone_cfg
        self.worker_num = worker_num
        self.last_status_msg = None
        self.first_status_of_type = None
        self.last_status_of_previous_type = None
        self.job_queue = multiprocessing.Queue()
        self.lock = multiprocessing.Lock()
        self.subprocess = PhoneWorkerSubProcess(tests, phone_cfg,
                                                autophone_queue,
                                                self.job_queue, logfile,
                                                loglevel, mailer)

    def start(self):
        self.subprocess.start()

    def stop(self):
        self.subprocess.stop()

    def add_job(self, job):
        self.job_queue.put_nowait(('job', job))

    def reboot(self):
        self.job_queue.put_nowait(('reboot', None))

    def disable(self):
        self.job_queue.put_nowait(('disable', None))

    def reenable(self):
        self.job_queue.put_nowait(('reenable', None))

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
    POST_REBOOT_SLEEP_SECONDS = 10

    def __init__(self, tests, phone_cfg, autophone_queue, job_queue, logfile,
                 loglevel, mailer):
        self.tests = tests
        self.phone_cfg = phone_cfg
        self.autophone_queue = autophone_queue
        self.job_queue = job_queue
        self.logfile = logfile
        self.loglevel = loglevel
        self.mailer = mailer
        self.p = None
        self.disabled = False
        self.skipped_job_queue = []
        self.current_build = None

    def start(self):
        """Call from main process."""
        if self.p:
            return
        self.p = multiprocessing.Process(target=self.loop)
        self.p.start()

    def stop(self):
        """Call from main process."""
        if self.p:
            self.job_queue.put_nowait(('stop', None))
            self.p.join(self.JOB_QUEUE_TIMEOUT_SECONDS*2)

    def is_disabled(self):
        disabled = self.disabled
        return disabled

    def status_update(self, msg):
        try:
            self.autophone_queue.put_nowait(msg)
        except Queue.Full:
            logging.warn('Autophone queue is full!')

    def check_sdcard(self, dm=None):
        if not dm:
            dm = DeviceManagerSUT(self.phone_cfg['ip'],
                                  self.phone_cfg['sutcmdport'])
        dev_root = dm.getDeviceRoot()
        if dev_root is None:
            logging.error('no response from device when querying device root')
            return False
        d = dev_root + '/autophonetest'
        androidutils.run_adb('shell', ['rmdir', d], serial=self.phone_cfg['serial'], timeout=15)
        out = androidutils.run_adb('shell', ['mkdir', d], serial=self.phone_cfg['serial'], timeout=15)
        if not out:
            # Sometimes we don't get an error creating the dir, but we do
            # when changing to it. Blah.
            out = androidutils.run_adb('shell', ['cd', d], serial=self.phone_cfg['serial'], timeout=15)            
        if out:
            logging.error('device root is not writable!')
            logging.error(out)
            logging.info('checking sdcard...')
            out = androidutils.run_adb('shell', ['mkdir', '/mnt/sdcard/tests/autophonetest'], serial=self.phone_cfg['serial'], timeout=15)
            if out:
                logging.error(out)
            else:
                logging.error('weird, sd card is writable but device root isn\'t! I\'m confused and giving up anyway!')
            self.clear_test_base_paths()
            return False
        androidutils.run_adb('shell', ['rmdir', d], serial=self.phone_cfg['serial'], timeout=15)
        return True

    def clear_test_base_paths(self):
        for t in self.tests:
            t._base_device_path = ''

    def recover_phone(self):
        reboots = 0
        while not self.disabled:
            if reboots < self.MAX_REBOOT_ATTEMPTS:
                logging.info('Rebooting phone...')
                phone_is_up = False
                reboots += 1
                try:
                    androidutils.reboot_adb(self.phone_cfg['serial'])
                except androidutils.AndroidError:
                    logging.error('Could not reboot phone.')
                    self.disable_phone('Could not reboot phone via adb.')
                    return
                time.sleep(self.POST_REBOOT_SLEEP_SECONDS)
                max_time = datetime.datetime.now() + \
                    datetime.timedelta(seconds=self.MAX_REBOOT_WAIT_SECONDS)
                while datetime.datetime.now() <= max_time:
                    dm = DeviceManagerSUT(self.phone_cfg['ip'],
                                          self.phone_cfg['sutcmdport'])
                    if dm._sock:
                        logging.info('Phone is back up.')
                        phone_is_up = True
                        break
                    time.sleep(5)
                if phone_is_up:
                    if self.check_sdcard():
                        return
                else:
                    logging.info('Phone did not come back up within %d seconds.' %
                                 self.MAX_REBOOT_WAIT_SECONDS)
            else:
                logging.info('Phone has been rebooted %d times; giving up.' %
                             reboots)
                self.disable_phone('Phone was rebooted %d times.' % reboots)

    def disable_phone(self, msg_body):
        self.disabled = True
        if msg_body and self.mailer:
            try:
                self.mailer.send('Phone %s disabled' % self.phone_cfg['phoneid'],
                                 '''Hello, this is AutoPhone. Phone %s has been disabled:

%s

We gave up on it. Sorry about that.
''' % (self.phone_cfg['phoneid'], msg_body))
            except socket.error:
                logging.error('Failed to send disabled-phone notification.')
                logging.info(traceback.format_exc())
        self.status_update(phonetest.PhoneTestMessage(
                self.phone_cfg['phoneid'],
                phonetest.PhoneTestMessage.DISABLED))
        
    def retry_func(self, error_str, func, args, kwargs):
        """Retries a function up to three times.
        Note that each attempt may reboot the phone up to three times if it
        comes up in a bad state. This loop is to catch errors caused by the
        test or help functions like androidutils.install_adb_build().
        """
        attempts = 0
        android_errors = []
        while not self.disabled and attempts < 3:
            attempts += 1
            # blech I don't like bare try/except clauses, but we
            # want to track down if/why phone processes are
            # suddenly exiting.
            try:
                return func(*args, **kwargs)
            except Exception, e:
                if isinstance(e, androidutils.AndroidError):
                    android_errors.append(str(e))
                logging.info(error_str)
                logging.info(traceback.format_exc())
                if attempts < 2:
                    self.recover_phone()
            else:
                return
            if attempts == 2:
                logging.warn('Failed to run test three times; giving up on it.')
                if len(android_errors) == 2:
                    logging.warn('Phone experienced three android errors in a row; giving up.')
                    self.disable_phone(
'''Phone experienced two android errors in a row:
%s''' % '\n'.join(['* %s' % x for x in android_errors]))

    def ping(self):
        logging.info('Pinging phone')
        # verify that the phone is still responding
        response = androidutils.run_adb('shell',
                                        ['echo', 'autophone'],
                                        self.phone_cfg['serial'])
        if response:
            logging.info('Pong!')
            return True

        logging.info('No response!')
        return False

    def loop(self):
        for h in logging.getLogger().handlers:
            logging.getLogger().removeHandler(h)
        logging.basicConfig(filename=self.logfile,
                            filemode='a',
                            level=self.loglevel,
                            format='%(asctime)s|%(levelname)s|%(message)s')

        logging.info('Worker for phone %s starting up.' %
                     self.phone_cfg['phoneid'])

        for t in self.tests:
            t.status_cb = self.status_update

        self.status_update(phonetest.PhoneTestMessage(
                self.phone_cfg['phoneid'],
                phonetest.PhoneTestMessage.IDLE))

        last_ping = None

        while True:
            request = None
            try:
                request = self.job_queue.get(timeout=self.JOB_QUEUE_TIMEOUT_SECONDS)
            except Queue.Empty:
                if (not self.disabled and
                    (not last_ping or
                     ((datetime.datetime.now() - last_ping) >
                      datetime.timedelta(
                                microseconds=1000*1000*self.PING_SECONDS)))):
                    last_ping = datetime.datetime.now()
                    if self.ping():
                        self.status_update(phonetest.PhoneTestMessage(
                                self.phone_cfg['phoneid'],
                                phonetest.PhoneTestMessage.IDLE,
                                self.current_build))
                    else:
                        logging.info('No response!')
                        self.recover_phone()
                        # try pinging again, since, if the phone is
                        # physically disconnected but the agent is running,
                        # the reboot will appear to succeed even though we
                        # still can't access it through adb.
                        if not self.ping():
                            self.disable_phone('No response to ping via adb.')
                            
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
                if not self.disabled and not self.check_sdcard():
                    self.recover_phone()
                if self.disabled:
                    logging.info('Phone is disabled; queuing job for later.')
                    self.skipped_job_queue.append(job)
                    continue
                self.status_update(phonetest.PhoneTestMessage(
                        self.phone_cfg['phoneid'],
                        phonetest.PhoneTestMessage.INSTALLING, job['blddate']))
                logging.info('Installing build %s.' % datetime.datetime.fromtimestamp(float(job['blddate'])))
                
                if not self.retry_func('Exception installing build',
                                       androidutils.install_build_adb, [],
                                       dict(phoneid=self.phone_cfg['phoneid'],
                                            apkpath=job['apkpath'],
                                            blddate=job['blddate'],
                                            procname=job['androidprocname'],
                                            serial=self.phone_cfg['serial'])):
                    logging.error('Failed to install build!')
                    continue

                self.current_build = job['blddate']
                logging.info('Running tests...')
                for t in self.tests:
                    if self.disabled:
                        break
                    t.current_build = job['blddate']
                    # TODO: Attempt to see if pausing between jobs helps with
                    # our reconnection issues
                    time.sleep(30)
                    self.retry_func('Exception running test %s.' %
                                    t.__class__.__name__, t.runjob, [job], {})

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
                self.status_update(phonetest.PhoneTestMessage(
                        self.phone_cfg['phoneid'],
                        phonetest.PhoneTestMessage.REBOOTING))
                self.recover_phone()
                if not self.disabled:
                    self.status_update(phonetest.PhoneTestMessage(
                            self.phone_cfg['phoneid'],
                            phonetest.PhoneTestMessage.IDLE, msg='phone reset'))
            elif request[0] == 'disable':
                self.disable_phone(None)
            elif request[0] == 'reenable':
                if self.disabled:
                    self.disabled = False
                    last_ping = None
                for j in self.skipped_job_queue:
                    self.job_queue.put(('job', j))

