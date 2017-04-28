# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# Modeled after the example https://docs.python.org/2.7/howto/logging-cookbook.html#sending-and-receiving-logging-events-across-a-network

import SocketServer
import logging
import logging.handlers
import pickle
import struct

import utils

class LogRecordHandler(SocketServer.StreamRequestHandler):

    def handle(self):
        try:
            data = ''
            while True:
                data = self.connection.recv(4)
                if len(data) < 4:
                    break
                pickle_len = struct.unpack('>L', data)[0]
                data = self.connection.recv(pickle_len)
                while len(data) < pickle_len:
                    data = data + self.connection.recv(pickle_len - len(data))
                record = logging.makeLogRecord(pickle.loads(data))
                logger = utils.getLogger(record.name)
                logger.handle(record)
                if 'logcontrol:' in record.message:
                    device = record.name
                    workers = self.server.autophone.phone_workers
                    if device in workers:
                        worker = workers[device]
                        if 'flush log' in record.message:
                            worker.log_server_flushed_event.set()
                        elif 'close log' in record.message:
                            worker.log_server_closed_event.set()
        except:
            logger = utils.getLogger()
            logger.exception("Error receiving log record: data: %s", data)

class LogRecordServer(SocketServer.ThreadingTCPServer):

    allow_reuse_address = True
    request_queue_size = 100

    def __init__(self,
                 autophone=None,
                 host='localhost',
                 port=logging.handlers.DEFAULT_TCP_LOGGING_PORT,
                 handler=LogRecordHandler):
        SocketServer.ThreadingTCPServer.__init__(self, (host, port), handler)
        self.autophone = autophone
        self.shutdown_requested = False
        self.timeout = 1

    def serve_forever(self):
        import select
        while not self.shutdown_requested:
            try:
                rlist, wlist, xlist = select.select(
                    [self.socket.fileno()],
                    [],
                    [],
                    self.timeout)
                if rlist:
                    self.handle_request()
            except:
                logger = utils.getLogger()
                logger.exception("Error selecting log record socket")

    def shutdown(self):
        self.shutdown_requested = True
