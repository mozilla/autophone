# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import datetime
import logging
import multiprocessing
import os
import tempfile
import time
import unittest

import mozdevice

class MockDeviceManagerSUT(mozdevice.DeviceManagerSUT):

    DISABLE_SHELL = False

    def getDeviceRoot(self):
        return '/tmp'

    def mkDir(self, d):
        return True

    def dirExists(self, d):
        return True

    def removeDir(self, d):
        return True

    def removeFile(self, f):
        return True

    def reboot(self, ip, port):
        return True

    def shell(self, args, output):
        if self.DISABLE_SHELL:
            return
        output.write(' '.join(args))

    def pushFile(self, src, dest):
        return True

    def installApp(self, path):
        return True

mozdevice.DeviceManagerSUT = MockDeviceManagerSUT

import worker
from phonetest import PhoneTestMessage


class WorkerTest(unittest.TestCase):

    SHOW_LOGS = False

    phone_cfg = dict(
        phoneid='fake',
        serial='ABCD1234',
        ip='127.0.0.1',
        sutcmdport=20701,
        machinetype='fake phone',
        osver='1.0')

    def setUp(self):
        MockDeviceManagerSUT.DISABLE_SHELL = False
        self.logfile = tempfile.NamedTemporaryFile()
        self.msg_queue = multiprocessing.Queue()
        self.worker = worker.PhoneWorker(0, '', [], self.phone_cfg,
                                         self.msg_queue, self.logfile.name,
                                         logging.DEBUG, None)
        self.worker.subprocess.JOB_QUEUE_TIMEOUT_SECONDS = 2

    def tearDown(self):
        self.worker.stop()
        if self.SHOW_LOGS:
            print 'worker log:'
            print self.logfile.read()
        del self.logfile

    def wait_for_state(self, state):
        msg = None
        start = datetime.datetime.now()
        while ((not msg or msg.status != state) and
               (datetime.datetime.now() - start < 
                datetime.timedelta(seconds=10))):
            msg = self.msg_queue.get(True, 10)
        return msg

    def test_init(self):
        pass

    def test_launch(self):
        self.worker.start()
        msg = self.msg_queue.get(True, 10)
        self.assertEqual(msg.phoneid, self.phone_cfg['phoneid'])
        self.assertEqual(msg.status, PhoneTestMessage.IDLE)
        self.assertEqual(msg.current_build, None)

    def test_disabled(self):
        self.worker.subprocess.POST_REBOOT_SLEEP_SECONDS = 0
        self.worker.subprocess.MAX_REBOOT_WAIT_SECONDS = 0
        MockDeviceManagerSUT.DISABLE_SHELL = True
        self.worker.start()
        msg = self.msg_queue.get(True, 10)
        self.assertEqual(msg.status, PhoneTestMessage.IDLE)
        msg = self.wait_for_state(PhoneTestMessage.DISABLED)
        self.assertEqual(msg.status, PhoneTestMessage.DISABLED)

    def test_manual_disable(self):
        self.worker.start()
        msg = self.msg_queue.get(True, 10)
        self.assertEqual(msg.phoneid, self.phone_cfg['phoneid'])
        self.assertEqual(msg.status, PhoneTestMessage.IDLE)
        self.assertEqual(msg.current_build, None)
        self.worker.disable()
        msg = self.wait_for_state(PhoneTestMessage.DISABLED)
        self.assertEqual(msg.status, PhoneTestMessage.DISABLED)
        self.worker.reenable()
        msg = self.wait_for_state(PhoneTestMessage.IDLE)
        self.assertEqual(msg.status, PhoneTestMessage.IDLE)
