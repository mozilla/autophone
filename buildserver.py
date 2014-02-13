# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import SocketServer
import errno
import json
import socket
import threading

DEFAULT_PORT = 28008

class BuildCacheServer(SocketServer.ThreadingMixIn, SocketServer.TCPServer):

    build_cache = None
    cache_lock = threading.Lock()


class BuildCacheHandler(SocketServer.BaseRequestHandler):

    def handle(self):
        buffer = ''
        while True:
            try:
                data = self.request.recv(1024)
            except socket.error, e:
                if e.errno == errno.ECONNRESET:
                    return
                raise e
            if not data:
                return
            buffer += data
            while buffer:
                line, nl, rest = buffer.partition('\n')
                if not nl:
                    break
                buffer = rest
                line = line.strip()
                if not line:
                    continue
                if line == 'quit' or line == 'exit':
                    return
                cmds = line.split()
                build = cmds[0]
                force = False
                enable_unittests = False
                cmds = cmds[1:]
                for cmd in cmds:
                    force = (cmd.lower() == 'force')
                    enable_unittests = (cmd.lower() == 'enable_unittests')
                self.server.cache_lock.acquire()
                results = self.server.build_cache.get(build, force, enable_unittests)
                self.server.cache_lock.release()
                self.request.send(json.dumps(results) + '\n')


class BuildCacheClient(object):

    def __init__(self, host='127.0.0.1', port=DEFAULT_PORT):
        self.host = host
        self.port = port
        self.sock = None

    def connect(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.connect((self.host, self.port))

    def close(self):
        self.sock.close()
        self.sock = None

    def get(self, url, force=False, enable_unittests=False):
        if not self.sock:
            self.connect()
        line = url
        if force:
            line += ' force'
        if enable_unittests:
            line += ' enable_unittests'
        self.sock.sendall(line + '\n')
        buf = ''
        while not '\n' in buf:
            data = self.sock.recv(1024)
            if not data:
                print 'build server hung up!'
                return None
            buf += data
        return json.loads(buf)
