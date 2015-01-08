# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import logging
import logging.handlers
import multiprocessing
import os
import sys
import time
import traceback

class MultiprocessingStreamHandler(logging.StreamHandler):

    def __init__(self, stream=None):
        self.lock = multiprocessing.Lock()
        logging.StreamHandler.__init__(self, stream)

    def emit(self, record):
        self.lock.acquire()
        try:
            logging.StreamHandler.emit(self, record)
        finally:
            self.lock.release()


class MultiprocessingTimedRotatingFileHandler(logging.handlers.TimedRotatingFileHandler):

    def __init__(self, filename, mode='a', when='h', interval=1, backupCount=0,
                 encoding=None, delay=False, utc=False):
        self.lock = multiprocessing.Lock()
        if mode.startswith('w') and os.path.exists(filename):
            os.unlink(filename)
        logging.handlers.TimedRotatingFileHandler.__init__(self,
                                                           filename, when,
                                                           interval, backupCount,
                                                           encoding, delay, utc)

    def doRollover(self):
        try:
            self.lock.acquire()
            if self.multiprocess_should_reopen():
                self.multiprocess_reopen()
            else:
                try:
                    logging.handlers.TimedRotatingFileHandler.doRollover(self)
                except:
                    sys.stderr.write('Exception during doRollover:\n\n%s' %
                                     traceback.format_exc())
                    raise
        finally:
            self.lock.release()

    def emit(self, record):
        try:
            self.lock.acquire()
            if self.multiprocess_should_reopen():
                self.multiprocess_reopen()
            logging.handlers.TimedRotatingFileHandler.emit(self, record)
        except:
            self.handleError(record)
        finally:
            self.lock.release()

    def multiprocess_should_reopen(self):
        """Check if the log file has already been rotated by another
        process by comparing the file's inode and device to stream's
        inode and device.  If they differ, the log file named by
        baseFilename has been rotated, and the stream now points to
        the previous version.

        FIXME: On at least one occasion, the log has been renamed by
        another process and not recreated by the time this method is
        called. This can cause the os.stat call to fail with a file
        not found exception. This *should* have been prevented by the
        multiprocessing.locks."""
        self.stream.flush()
        try:
            bf_stat = os.stat(self.baseFilename)
        except OSError:
            sys.stderr.write('OSError Exception during os.stat(%s):\n\n%s' %
                             (self.baseFilename, traceback.format_exc()))
            raise
        except:
            sys.stderr.write('Unexpected Exception during os.stat(%s):\n\n%s' %
                             (self.baseFilename, traceback.format_exc()))
            raise
        st_stat = os.fstat(self.stream.fileno())
        if bf_stat.st_ino == st_stat.st_ino and bf_stat.st_dev == st_stat.st_dev:
            return False
        return True

    def multiprocess_reopen(self):
        """Reopen the log file and reset the rollover time so that
        different processes will log to the most recent log file."""
        self.stream.flush()
        self.stream.close()
        # make sure the mode was not reset to 'w' by the rollover.
        self.mode = 'a'
        self.stream = self._open()
        self.rolloverAt = self.computeRollover(time.time())
