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

import androidutils
import worker
from phonetest import PhoneTestMessage

class MockAdb(object):

    def run_adb(self, command, *a, **kw):
        return command

    def run_empty_adb(self, command, *a, **kw):
        return ''


class WorkerTest(unittest.TestCase):

    phone_cfg = dict(
        phoneid='fake',
        serial='ABCD1234',
        ip='127.0.0.1',
        sutcmdport=20701,
        machinetype='fake phone',
        osver='1.0')

    def setUp(self):
        reload(androidutils)  # clear overrides
        self.logfile = tempfile.NamedTemporaryFile()
        self.msg_queue = multiprocessing.Queue()
        self.worker = worker.PhoneWorker(0, [], self.phone_cfg, self.msg_queue,
                                         self.logfile.name, logging.DEBUG, None)
        self.worker.JOB_QUEUE_TIMEOUT_SECONDS = 2

    def tearDown(self):
        self.worker.stop()
        #print 'worker log:'
        #print self.logfile.read()
        del self.logfile

    def test_init(self):
        pass

    def test_launch(self):
        mock_adb = MockAdb()
        androidutils.run_adb = mock_adb.run_adb
        self.worker.start()
        msg = self.msg_queue.get(True, 10)
        self.assertEqual(msg.phoneid, self.phone_cfg['phoneid'])
        self.assertEqual(msg.status, PhoneTestMessage.IDLE)
        self.assertEqual(msg.current_build, None)

    def test_disabled(self):
        self.worker.POST_REBOOT_SLEEP_SECONDS = 0
        self.worker.MAX_REBOOT_WAIT_SECONDS = 0
        mock_adb = MockAdb()
        androidutils.run_adb = mock_adb.run_empty_adb
        self.worker.start()
        msg = self.msg_queue.get(True, 10)
        self.assertEqual(msg.status, PhoneTestMessage.IDLE)
        start = datetime.datetime.now()
        while (msg.status == PhoneTestMessage.IDLE and
               datetime.datetime.now() - start < 
               datetime.timedelta(seconds=10)):
            msg = self.msg_queue.get(True, 10)
        self.assertEqual(msg.status, PhoneTestMessage.DISABLED)
