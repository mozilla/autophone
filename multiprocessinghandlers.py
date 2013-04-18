# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import logging
import logging.handlers
import multiprocessing

class MultiprocessingStreamHandler(logging.StreamHandler):

    lock = multiprocessing.Lock()

    def emit(self, record):
        self.lock.acquire()
        try:
            logging.StreamHandler.emit(self, record)
        finally:
            self.lock.release()


class MultiprocessingTimedRotatingFileHandler(logging.handlers.TimedRotatingFileHandler):

    lock = multiprocessing.Lock()

    def doRollover(self):
        self.lock.acquire()
        try:
            logging.handlers.TimedRotatingFileHandler.doRollover(self)
        finally:
            self.lock.release()

    def emit(self, record):
        self.lock.acquire()
        try:
            logging.handlers.TimedRotatingFileHandler.emit(self, record)
        finally:
            self.lock.release()
