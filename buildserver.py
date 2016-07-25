# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import SocketServer
import errno
import json
import socket
import threading
import urlparse

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
                builder_type = None
                test_package_names = set()
                collecting_test_packages = False
                cmds = cmds[1:]
                for cmd in cmds:
                    if collecting_test_packages:
                        test_package_names.add(cmd)
                    elif cmd.lower() == 'force':
                        force = True
                    elif cmd.lower() == 'enable_unittests':
                        enable_unittests = True
                    elif cmd.lower() == 'builder_type_buildbot':
                        builder_type = 'buildbot'
                    elif cmd.lower() == 'builder_type_taskcluster':
                        builder_type = 'taskcluster'
                    elif cmd.lower() == 'test_packages':
                        collecting_test_packages = True
                self.server.cache_lock.acquire()
                try:
                    results = self.server.build_cache.get(
                        build,
                        force=force,
                        enable_unittests=enable_unittests,
                        test_package_names=test_package_names,
                        builder_type=builder_type)
                except Exception, e:
                    results = {
                        'success': False,
                        'error': 'Exception: %s' % e,
                        'metadata': ''
                    }
                finally:
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

    def get(self, url, force=False, enable_unittests=False,
            test_package_names=None, builder_type=None):
        if not self.sock:
            self.connect()
        line = url
        force = force or not urlparse.urlparse(url).scheme.startswith('http')
        if force:
            line += ' force'
        if enable_unittests:
            line += ' enable_unittests'
        if builder_type:
            line += ' builder_type_' + builder_type
        if test_package_names:
            line += ' test_packages'
            for test_package in test_package_names:
                line += ' ' + test_package
        self.sock.sendall(line + '\n')
        buf = ''
        while not '\n' in buf:
            data = self.sock.recv(1024)
            if not data:
                print 'build server hung up!'
                return None
            buf += data
        return json.loads(buf)
