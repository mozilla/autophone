# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import ConfigParser
import json
import logging
import os
import re
import socket
import subprocess
import time
import traceback

from logparser import LogParser

from logdecorator import LogDecorator
from phonetest import PhoneTest, PhoneTestResult

# Set the logger globally in the file, but this must be reset when
# used in a child process.
logger = logging.getLogger()


class UnitTest(PhoneTest):
    def __init__(self, phone, options, config_file=None, chunk=1, repos=[]):
        PhoneTest.__init__(self, phone, options,
                           config_file=config_file, chunk=chunk, repos=repos)
        self.enable_unittests = True
        self.unittest_cfg = ConfigParser.RawConfigParser()

        unittest_config_file = self.cfg.get('runtests', 'unittest_defaults')
        self.unittest_cfg.read(unittest_config_file)

        self.loggerdeco.info('config_file = %s, unittest_config_file = %s' %
                             (config_file, unittest_config_file))

        # Mochitests in particular are broken when run via adb. We must
        # use the SUTAgent and will need the phone's ip address.
        phone_ip_address = None
        for attempt in range(1, self.options.phone_retry_limit+1):
            phone_ip_address = self.dm.get_ip_address()
            self.loggerdeco.debug(
                'UnitTest: get phone ip address Attempt: %d: %s' %
                (attempt, phone_ip_address))
            if phone_ip_address:
                break
            time.sleep(self.options.phone_retry_wait)
        if not phone_ip_address:
            raise Exception('UnitTest: Failed to get phone %s ip address' % self.phone.id)

        self.parms = {
            'host_ip_address': self.phone.host_ip,
            'phone_ip_address': phone_ip_address,
            'phoneid': self.phone.id,
            'config_file': config_file,
            'test_name': self.cfg.get('runtests', 'test_name'),
            'test_manifest': self.cfg.get('runtests', 'test_manifest'),
        }

        self.parms['xre_path'] = self.unittest_cfg.get('runtests', 'xre_path')
        self.parms['utility_path'] = self.unittest_cfg.get('runtests', 'utility_path')
        if self.unittest_cfg.has_option('runtests', 'include_pass'):
            self.parms['include_pass'] = self.unittest_cfg.getboolean('runtests', 'include_pass')
        else:
            self.parms['include_pass'] = False

        if self.cfg.has_option('runtests', 'app_name'):
            self.parms['app_name'] = self.cfg.get('runtests', 'app_name')

        self.parms['console_level'] = self.unittest_cfg.get('runtests', 'console_level')
        self.parms['log_level'] = self.unittest_cfg.get('runtests', 'log_level')
        self.parms['time_out'] = self.unittest_cfg.getint('runtests', 'time_out')

        if self.cfg.has_option('runtests', 'iterations'):
            self.parms['iterations'] = self.cfg.getint('runtests', 'iterations')
        else:
            self.parms['iterations'] = 1

        if self.cfg.has_option('runtests', 'total_chunks'):
            self.chunks = self.cfg.getint('runtests', 'total_chunks')

        if self.cfg.has_option('runtests', 'prefs'):
            self.parms['prefs'] = self.cfg.get('runtests', 'prefs').split(',')
        else:
            self.parms['prefs'] = []

    @property
    def name(self):
        return 'autophone-%s%s' % (self.parms['test_name'], self.name_suffix)

    def setup_job(self):
        PhoneTest.setup_job(self)
        build_dir = self.build.dir
        symbols_path = self.build.symbols
        if symbols_path and not os.path.exists(symbols_path):
            symbols_path = None
        re_revision = re.compile(r'http.*/rev/(.*)')
        match = re_revision.match(self.build.revision)
        if match:
            revision = match.group(1)
        else:
            revision = self.build.revision

        self.parms['app_name'] = self.build.app_name
        self.parms['build_dir'] = build_dir
        self.parms['symbols_path'] = symbols_path
        self.parms['revision'] = revision
        self.parms['buildid'] = self.build.id
        self.parms['tree'] = self.build.tree

        self._log = '%s/tests/%s-%s-%s-%s.log' % (build_dir,
                                               self.parms['test_name'],
                                               os.path.basename(self.config_file),
                                               self.chunk,
                                               self.parms['phoneid'])
        os.putenv('MINIDUMP_STACKWALK', self.options.minidump_stackwalk)
        os.putenv('MINIDUMP_SAVE_PATH', self.upload_dir)
        os.putenv('MOZ_UPLOAD_DIR', self.upload_dir)

    def teardown_job(self):
        os.unsetenv('MINIDUMP_STACKWALK')
        os.unsetenv('MINIDUMP_SAVE_PATH')
        os.unsetenv('MOZ_UPLOAD_DIR')
        PhoneTest.teardown_job(self)

    def run_job(self):
        self.loggerdeco.debug('runtestsremote.py run_job start')
        self.update_status(message='runtestsremote.py run_job start')

        self.worker_subprocess.check_sdcard()

        if logger.getEffectiveLevel() == logging.DEBUG:
            self.loggerdeco.debug('phone = %s' % self.phone)

        if not self.cfg.has_option('runtests', 'test_name'):
            raise Exception('Job configuration %s does not specify a test' %
                            self.config_file)
        try:
            self.runtest()
        except:
            # This exception handler deals with exceptions which occur outside
            # of the actual test runner. Exceptions from the test runner
            # are handled locally in runtest.
            self.loggerdeco.exception('runtestsremote.py:run_job: Exception '
                                      'running test')
            self.update_status(message='runtestsremote.py:run_job: Exception '
                               'running test')
            # give the phone a minute to recover
            time.sleep(60)
            self.worker_subprocess.recover_phone()

        self.loggerdeco.debug('runtestsremote.py run_job exit')
        self.update_status(message='runtestsremote.py run_job exit')

    def create_test_args(self):
        args = ['python', '-u']

        test_name_lower = self.parms['test_name'].lower()

        if test_name_lower.startswith('robocoptest'):
            self.parms['harness_type'] = 'mochitest'

            test_args = [
                'mochitest/runtestsremote.py',
                '--robocop-ini=%s' % self.parms['test_manifest'],
                '--robocop-ids=%s/fennec_ids.txt' % self.parms['build_dir'],
                '--certificate-path=certs',
                '--console-level=%s' % self.parms['console_level'],
                '--log-raw=%s' % 'raw-log-' + os.path.basename(self._log),
            ]
        elif test_name_lower.startswith('mochitest'):
            self.parms['harness_type'] = 'mochitest'

            test_args = [
                'mochitest/runtestsremote.py',
                '--manifest=%s' % self.parms['test_manifest'],
                '--testrun-manifest-file=%s-%s-%s-tests.json' % (self.parms['test_name'],
                                                                 self.chunk,
                                                                 self.parms['phoneid']),
                '--certificate-path=certs',
                '--console-level=%s' % self.parms['console_level'],
                '--log-raw=%s' % 'raw-log-' + os.path.basename(self._log),
            ]
        elif test_name_lower.startswith('reftest'):
            self.parms['harness_type'] = 'reftest'

            test_args = [
                'reftest/remotereftest.py',
                '--ignore-window-size',
                '--bootstrap',
                '%s' % self.parms['test_manifest'],
                ]
        elif test_name_lower.startswith('jsreftest'):
            self.parms['harness_type'] = 'reftest'

            test_args = [
                'reftest/remotereftest.py',
                '--ignore-window-size',
                '--bootstrap',
                '--extra-profile-file=jsreftest/tests/user.js',
                '%s' % self.parms['test_manifest'],
                ]
        elif test_name_lower.startswith('crashtest'):
            self.parms['harness_type'] = 'reftest'

            test_args = [
                'reftest/remotereftest.py',
                '--ignore-window-size',
                '--bootstrap',
                '%s' % self.parms['test_manifest'],
                ]
        else:
            self.loggerdeco.error('Unknown test_name %s' % self.parms['test_name'])
            raise Exception('Unknown test_name %s' % self.parms['test_name'])

        if self.parms['iterations'] > 1:
                test_args.append('--repeat=%d' % (self.parms['iterations']-1))

        self.parms['http_port'] = self.parms['port_manager'].reserve()
        self.parms['ssl_port'] = self.parms['port_manager'].reserve()

        common_args = [
            '--dm_trans=sut',
            '--deviceIP=%s' % self.parms['phone_ip_address'],
            '--devicePort=20701',
            '--app=%s' % self.parms['app_name'],
            '--xre-path=%s' % self.parms['xre_path'],
            '--utility-path=%s' % self.parms['utility_path'],
            '--timeout=%d' % self.parms['time_out'],
            '--remote-webserver=%s' % self.parms['host_ip_address'],
            '--http-port=%s' % self.parms['port_manager'].use(self.parms['http_port']),
            '--ssl-port=%s' % self.parms['port_manager'].use(self.parms['ssl_port']),
            '--total-chunks=%d' % self.chunks,
            '--this-chunk=%d' % self.chunk,
            '--pidfile=%s-%s-%s.pid' % (self.parms['test_name'], self.chunk, self.parms['phoneid']),
        ]
        for pref in self.parms['prefs']:
            common_args.append('--setpref=%s' % pref)

        args.extend(test_args)
        args.extend(common_args)

        if self.parms['symbols_path'] is not None:
            args.append('--symbols-path=%s' % self.parms['symbols_path'])

        return args

    def process_test_log(self, logfilehandle):

        logfilehandle.close()

        # convert embedded \n into real newlines
        logfilehandle = open(self._log)
        self.loggerdeco.debug('process_test_log: name: %s, self._log: %s' % (
            logfilehandle.name, self._log))
        logcontents = logfilehandle.read()
        logfilehandle.close()
        logcontents = re.sub(r'\\n', '\n', logcontents)
        logfilehandle = open(self._log, 'wb')
        logfilehandle.write(logcontents)
        logfilehandle.close()
        self.loggerdeco.debug('process_test_log: raw log:\n%s\n' % logcontents)

        lp = LogParser([logfilehandle.name],
                       includePass=True,
                       output_dir=None,
                       logger=self.loggerdeco,
                       harnessType=self.parms['harness_type'])
        parsed_log = lp.parseFiles()
        self.loggerdeco.debug('process_test_log: LogParser parsed log : %s' %
                              json.dumps(parsed_log, indent=2))

        self.test_result.todo = parsed_log.get('todo', 0)
        self.test_result.passes = parsed_log.get('passes', [])
        failures = parsed_log.get('failures', [])
        if failures:
            for failure in failures:
                for test_failure in failure['failures']:
                    self.test_failure(failure['test'],
                                      test_failure['status'],
                                      test_failure['text'],
                                      PhoneTestResult.TESTFAILED)
        self.loggerdeco.debug('process_test_log: test_result: %s' %
                              json.dumps(self.test_result.__dict__, indent=2))

    def runtest(self):

        self.loggerdeco = LogDecorator(logger,
                                       {'phoneid': self.phone.id,
                                        'buildid': self.parms['buildid'],
                                        'testname': self.parms['test_name']},
                                       '%(phoneid)s|%(buildid)s|'
                                       '%(testname)s|%(message)s')

        if logger.getEffectiveLevel() == logging.DEBUG:
            self.loggerdeco.debug('runtestsremote.py runtest start')
            for key in self.parms.keys():
                self.loggerdeco.debug('test parameters: %s = %s' %
                                      (key, self.parms[key]))

        self.update_status(message='Starting test %s' % self.parms['test_name'])

        if self.parms['test_name'] == 'robocoptest':
            try:
                self.dm.uninstall_app('org.mozilla.roboexample.test')
                robocop_apk_path = os.path.join(self.parms['build_dir'], 'robocop.apk')
                self.dm.install_app(robocop_apk_path)
            except Exception, e:
                self.loggerdeco.exception('runtestsremote.py:runtest: Exception running test.')
                self.test_result.status = PhoneTestResult.EXCEPTION
                self.message = 'Exception installing robocop.apk: %s' % e
                with open(self._log, "w") as logfilehandle:
                    logfilehandle.write('%s\n' % self.message)
                return

        self.parms['port_manager'] = PortManager(self.parms['host_ip_address'])

        try:
            logfilehandle = None
            while True:
                socket_collision = False

                logfilehandle = open(self._log, 'wb')
                self.loggerdeco.debug('logging to %s' % logfilehandle.name)

                args = self.create_test_args()

                self.parms['cmdline'] = ' '.join(args)
                self.loggerdeco.debug("cmdline = %s" %
                                      self.parms['cmdline'])

                self.update_status(message='Running test %s chunk %d of %d' %
                                   (self.parms['test_name'],
                                    self.chunk, self.chunks))
                if self.dm.process_exist(self.parms['app_name']):
                    max_kill_attempts = 3
                    for kill_attempt in range(1, max_kill_attempts+1):
                        self.loggerdeco.debug(
                            'Process %s exists. Attempt %d to kill.' % (
                                self.parms['app_name'], kill_attempt + 1))
                        self.dm.pkill(self.parms['app_name'], root=True)
                        if not self.dm.process_exist(self.parms['app_name']):
                            break
                    if kill_attempt == max_kill_attempts and \
                            self.dm.process_exist(self.parms['app_name']):
                        self.loggerdeco.warning(
                            'Could not kill process %s.' % (
                                self.parms['app_name']))
                proc = subprocess.Popen(
                    args,
                    cwd=os.path.join(self.parms['build_dir'],
                                     'tests'),
                    preexec_fn=lambda: os.setpgid(0, 0),
                    stdout=logfilehandle,
                    stderr=subprocess.STDOUT,
                    close_fds=True
                )
                returncode = None
                while True:
                    returncode = proc.poll()
                    if returncode is not None:
                        break
                    command = self.worker_subprocess.process_autophone_cmd(self)
                    if command['interrupt']:
                        proc.kill()
                        self.handle_test_interrupt(command['reason'])
                        break

                if command and command['interrupt']:
                    break
                elif proc.returncode != 0:
                    self.test_result.status = PhoneTestResult.EXCEPTION
                    self.message = 'Test exited with return code %d' % proc.returncode

                self.loggerdeco.debug('runtestsremote.py return code %d' %
                                      proc.returncode)

                logfilehandle.close()
                # XXX: investigate if this is still needed.
                re_socket_error = re.compile('socket\.error:')
                logfilehandle = open(self._log)
                logcontents = logfilehandle.read()
                logfilehandle.close()
                if re_socket_error.search(logcontents):
                    socket_collision = True

                if not socket_collision:
                    break

            self.update_status(message='Completed test %s chunk %d of %d' %
                               (self.parms['test_name'],
                                self.chunk, self.chunks))
        except:
            if logfilehandle:
                logfilehandle.close()
            error_message = ('Exception during test %s chunk %d of %d: %s' %
                             (self.parms['test_name'],
                              self.chunk, self.chunks,
                              traceback.format_exc()))
            self.update_status(message=error_message)
            self.loggerdeco.error(error_message)
            self.test_result.status = PhoneTestResult.EXCEPTION
            self.message = error_message
        finally:
            if logfilehandle:
                self.process_test_log(logfilehandle)

        self.loggerdeco.debug('runtestsremote.py runtest exit')

        return


class PortManager(object):
    '''
    Obtain a free port on ip address

    usage:
           port_manager = PortManager(ipaddress)
           port = port_manager.reserve()
           port_manager.use(port)

    See
    http://docs.python.org/library/socket.html
    http://code.activestate.com/recipes/531822-pick-unused-port/

    Chapter 4: Elementary Sockets
    UNIX Network Programming
    Networking APIs: Sockets and XTI
    Volume 1, Second Edition
    W. Richard Stevens
    '''

    def __init__(self, ipaddr):
        self.ipaddr = ipaddr
        self.reserved_ports = {}

    def reserve(self):
        '''
        Reserve a port for later use by creating a socket
        with a random port. The socket is left open to
        prevent others from using the port.
        '''
        while True:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind((self.ipaddr, 0))
            port = str(sock.getsockname()[1])
            self.reserved_ports[port] = sock
            return port

    def use(self, port):
        '''
        Prepare a reserved port for use by closing its socket and
        returning the port.
        '''
        sock = self.reserved_ports[port]
        sock.close()
        return port
