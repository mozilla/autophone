# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import ConfigParser
import json
import logging
import os
import posixpath
import re
import socket
import subprocess
import tempfile
import time
import traceback

from logparser import LogParser
from mozautolog import RESTfulAutologTestGroup

import newlogparser
from adb import ADBError
from logdecorator import LogDecorator
from phonetest import PhoneTest

class UnitTest(PhoneTest):

    def setup_job(self):
        PhoneTest.setup_job(self)

    def run_job(self):
        self.loggerdeco.debug('runtestsremote.py run_job start')
        self.update_status(message='runtestsremote.py run_job start')

        self.worker_subprocess.check_sdcard()

        build_dir = os.path.abspath(self.build.dir)
        symbols_path = os.path.join(build_dir, 'symbols')
        if not os.path.exists(symbols_path):
            symbols_path = None

        re_revision = re.compile(r'http.*/rev/(.*)')
        match = re_revision.match(self.build.revision)
        if match:
            revision = match.group(1)
        else:
            revision = self.build.revision

        if self.logger.getEffectiveLevel() == logging.DEBUG:
            self.loggerdeco.debug('phone = %s' % self.phone)

        if self.cfg.has_option('runtests', 'config_files'):
            # job contains multiple unittests
            config_files = self.cfg.get('runtests', 'config_files').split(' ')
        elif self.cfg.has_option('runtests', 'test_name'):
            # job contains a single unittest
            config_files = [self.config_file]
        else:
            raise Exception('Job configuration %s does not specify a test' %
                            self.config_file)
        missing_config_files = []
        for config_file in config_files:
            if not os.path.exists(config_file):
                missing_config_files.append(config_file)
        if missing_config_files:
            raise Exception("Can not run tests with missing config files: %s" %
                            ', '.join(missing_config_files))
        for config_file in config_files:
            try:
                test_parameters = {
                    'host_ip_address': self.phone.host_ip,
                    'phoneid': self.phone.id,
                    'app_name': self.build.app_name,
                    'build_dir': build_dir,
                    'symbols_path': symbols_path,
                    'revision': revision,
                    'buildid': self.build.id,
                    'tree': self.build.tree,
                    'config_file': config_file,
                }

                self.load_test_parameters(test_parameters, config_file)
                self.runtest(test_parameters)

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

    def load_test_parameters(self, test_parameters, config_file):

        cfg = ConfigParser.RawConfigParser()
        cfg.read(config_file)

        test_parameters['test_name'] = cfg.get('runtests', 'test_name')
        test_parameters['test_manifest'] = cfg.get('runtests', 'test_manifest')
        try:
            test_parameters['test_path'] = cfg.get('runtests', 'test_path')
        except ConfigParser.NoOptionError:
            test_parameters['test_path'] = None

        unittest_config_file = cfg.get('runtests', 'unittest_defaults')
        cfg.read(unittest_config_file)

        self.loggerdeco.info('config_file = %s, unittest_config_file = %s' %
                             (config_file, unittest_config_file))

        test_parameters['xre_path'] = cfg.get('runtests', 'xre_path')
        test_parameters['utility_path'] = cfg.get('runtests', 'utility_path')
        if test_parameters['symbols_path'] is not None and cfg.has_option('runtests',
                                                       'minidump_stackwalk'):
            test_parameters['minidump_stackwalk'] = cfg.get('runtests', 'minidump_stackwalk')
            os.environ['MINIDUMP_STACKWALK'] = test_parameters['minidump_stackwalk']
        elif 'MINIDUMP_STACKWALK' in os.environ:
            del os.environ['MINIDUMP_STACKWALK']

        if cfg.has_option('runtests', 'app_name'):
            test_parameters['app_name'] = cfg.get('runtests', 'app_name')

        test_parameters['console_level'] = cfg.get('runtests', 'console_level')
        test_parameters['file_level'] = cfg.get('runtests', 'file_level')
        test_parameters['time_out'] = cfg.getint('runtests', 'time_out')

        if cfg.has_option('runtests', 'iterations'):
            test_parameters['iterations'] = cfg.getint('runtests', 'iterations')
        else:
            test_parameters['iterations'] = 1

        if cfg.has_option('runtests', 'total_chunks'):
            test_parameters['total_chunks'] = cfg.getint('runtests', 'total_chunks')
        else:
            test_parameters['total_chunks'] = 1

        if cfg.has_option('runtests', 'prefs'):
            test_parameters['prefs'] = cfg.get('runtests', 'prefs').split(',')
        else:
            test_parameters['prefs'] = []

        test_parameters['es_server'] = cfg.get('autolog', 'es_server')
        test_parameters['rest_server'] = cfg.get('autolog', 'rest_server')
        test_parameters['index'] = cfg.get('autolog', 'index')
        test_parameters['include_pass'] = cfg.getboolean('autolog', 'include_pass')
        test_parameters['submit_log'] = cfg.getboolean('autolog', 'submit_log')
        test_parameters['use_newparser'] = cfg.getboolean('autolog', 'use_newparser')

    def create_test_args(self, test_parameters):
        args = ['python']

        test_name_lower = test_parameters['test_name'].lower()

        if test_name_lower.startswith('robocoptest'):
            test_args = [
                'mochitest/runtestsremote.py',
                '--robocop=%s' % test_parameters['test_manifest'],
                '--robocop-ids=%s/fennec_ids.txt' % test_parameters['build_dir'],
                '--certificate-path=certs',
                '--close-when-done',
                '--autorun',
                '--console-level=%s' % test_parameters['console_level'],
                '--file-level=%s' % test_parameters['file_level'],
                '--repeat=%d' % test_parameters['iterations'],
            ]
        elif test_name_lower.startswith('mochitest'):
            test_args = [
                'mochitest/runtestsremote.py',
                '--test-manifest=%s' % test_parameters['test_manifest'],
                '--test-path=%s' % test_parameters['test_path'],
                '--certificate-path=certs',
                '--close-when-done',
                '--autorun',
                '--console-level=%s' % test_parameters['console_level'],
                '--file-level=%s' % test_parameters['file_level'],
                '--repeat=%d' % test_parameters['iterations'],
            ]
        elif test_name_lower.startswith('reftest'):
            test_args = [
                'reftest/remotereftest.py',
                '--ignore-window-size',
                '--bootstrap',
                '%s' % test_parameters['test_manifest'],
                ]
        elif test_name_lower.startswith('jsreftest'):
            test_args = [
                'reftest/remotereftest.py',
                '--ignore-window-size',
                '--bootstrap',
                '--extra-profile-file=jsreftest/tests/user.js',
                '%s' % test_parameters['test_manifest'],
                ]
        elif test_name_lower.startswith('crashtest'):
            test_args = [
                'reftest/remotereftest.py',
                '--ignore-window-size',
                '--bootstrap',
                '%s' % test_parameters['test_manifest'],
                ]
        else:
            self.loggerdeco.error('Unknown test_name %s' % test_parameters['test_name'])
            raise Exception('Unknown test_name %s' % test_parameters['test_name'])

        test_parameters['http_port'] = test_parameters['port_manager'].reserve()
        test_parameters['ssl_port'] = test_parameters['port_manager'].reserve()

        # XXX: Temporarily set --deviceIP=dummy since --deviceIP is currently
        # a required parameter in the remote test runners. This can be removed
        # when the test runners are updated to support specifying device by
        # serial number.

        common_args = [
            '--dm_trans=sut',
            '--deviceIP=dummy',
            '--app=%s' % test_parameters['app_name'],
            '--xre-path=%s' % test_parameters['xre_path'],
            '--utility-path=%s' % test_parameters['utility_path'],
            '--timeout=%d' % test_parameters['time_out'],
            '--http-port=%s' % test_parameters['port_manager'].use(test_parameters['http_port']),
            '--ssl-port=%s' % test_parameters['port_manager'].use(test_parameters['ssl_port']),
            '--total-chunks=%d' % test_parameters['total_chunks'],
            '--this-chunk=%d' % test_parameters['this_chunk'],
            '--log-file=%s-%s.log' % (test_parameters['test_name'], test_parameters['phoneid']),
            '--pidfile=%s-%s.pid' % (test_parameters['test_name'], test_parameters['phoneid']),
        ]
        for pref in test_parameters['prefs']:
            common_args.append('--setpref=%s' % pref)

        args.extend(test_args)
        args.extend(common_args)

        if test_parameters['symbols_path'] is not None:
            args.append('--symbols-path=%s' % test_parameters['symbols_path'])

        return args

    def process_test_log(self, test_parameters, logfilehandle):

        test_log = None
        test_runs = []

        if test_parameters['use_newparser']:
            logfilehandle.close()
            logfilehandle = open(logfilehandle.name)
            try:
                # Turn off verbose logging for the log parser
                logger = logging.getLogger('logparser')
                logger_effective_level = logger.getEffectiveLevel()
                logger.setLevel(logging.WARN)
                test_log = newlogparser.parse_log(logfilehandle)
                test_runs = test_log.convert(test_parameters['include_pass'])
            finally:
                logger.setLevel(logger_effective_level)
                logfilehandle.close()
        else:
            lp = LogParser([logfilehandle.name],
                           es=False,
                           es_server=None,
                           includePass=True,
                           output_dir=None,
                           logger=self.logger,
                           harnessType=test_parameters['harness_type'])

            # Use logparser's parsers, but do not allow it to
            # submit data directly to elasticsearch.
            test_runs.append(lp.parseFiles())

        if test_parameters['es_server'] is None or test_parameters['rest_server'] is None:
            return

        # testgroup must match entry in autolog/js/Config.js:testNames
        # os        must match entry in autolog/js/Config.js:OSNames
        # platform  must match entry in autolog/js/Config.js:OSNames

        logfilename = None
        if test_parameters['submit_log']:
            logfilename = logfilehandle.name

        chunk_descriptor = ''
        if test_parameters['total_chunks'] > 1:
            chunk_descriptor = 's-%d' % test_parameters['this_chunk']

        testgroup_name = '%s%s' % (test_parameters['test_name'],
                                   chunk_descriptor)

        platform_name = self.phone.machinetype

        self.loggerdeco.debug('testgroup_name = %s' % testgroup_name)

        testgroup = RESTfulAutologTestGroup(
            index=test_parameters['index'],
            testgroup=testgroup_name,
            os='android',
            platform=platform_name,
            harness=test_parameters['harness_type'],
            server=test_parameters['es_server'],
            restserver=test_parameters['rest_server'],
            machine=self.phone.id,
            logfile=logfilename)

        testgroup.set_primary_product(
            tree=test_parameters['tree'],
            buildtype='opt',
            buildid=test_parameters['buildid'],
            revision=test_parameters['revision'])

        for testdata in test_runs:

            if self.logger.getEffectiveLevel() == logging.DEBUG:
                self.loggerdeco.debug('Begin testdata')
                self.loggerdeco.debug(json.dumps(testdata, indent=4))
                self.loggerdeco.debug('End testdata')

            testgroup.add_test_suite(
                testsuite=testgroup_name,
                cmdline=test_parameters['cmdline'],
                passed=testdata.get('passed', None),
                failed=testdata.get('failed', None),
                todo=testdata.get('todo', None))

            for t in testdata.get('failures', {}):
                test = t["test"]
                for f in t["failures"]:
                    text = f["text"]
                    status = f["status"]
                    testgroup.add_test_failure(test=test,
                                               text=text,
                                               status=status)

            # Submitting passing tests not supported via REST API
            if test_parameters['include_pass']:
                for t in testdata.get('passes', {}):
                    test = t["test"]
                    duration = None
                    if "duration" in t:
                        duration = t["duration"]
                    testgroup.add_test_pass(test=test,
                                            duration=duration)

        testgroup.submit()

    def runtest(self, test_parameters):

        self.loggerdeco = LogDecorator(self.logger,
                                       {'phoneid': self.phone.id,
                                        'buildid': test_parameters['buildid'],
                                        'testname': test_parameters['test_name']},
                                       '%(phoneid)s|%(buildid)s|'
                                       '%(testname)s|%(message)s')

        if self.logger.getEffectiveLevel() == logging.DEBUG:
            self.loggerdeco.debug('runtestsremote.py runtest start')
            for key in test_parameters.keys():
                self.loggerdeco.debug('test parameters: %s = %s' %
                                      (key, test_parameters[key]))

        self.update_status(message='Starting test %s' % test_parameters['test_name'])

        test_parameters['harness_type'] = test_parameters['test_name']

        if test_parameters['test_name'] == 'robocoptest':
            test_parameters['harness_type'] = 'mochitest'
            robocop_apk_path = posixpath.join(self.dm.test_root, 'robocop.apk')

            # XXX: FIXME. When bug 792072 lands, change to have
            # install_app() push the file

            self.dm.push(os.path.join(test_parameters['build_dir'],
                                      'robocop.apk'),
                             robocop_apk_path)
            try:
                self.dm.uninstall_app('org.mozilla.roboexample.test')
            except ADBError:
                self.loggerdeco.exception('runtestsremote.py:runtest: Exception running test.')

            self.dm.install_app(robocop_apk_path)
            self.dm.rm(robocop_apk_path)

        test_parameters['port_manager'] = PortManager(test_parameters['host_ip_address'])

        for this_chunk in range(1, test_parameters['total_chunks'] + 1):

            test_parameters['this_chunk'] = this_chunk

            try:
                while True:
                    socket_collision = False

                    logfilehandle = tempfile.NamedTemporaryFile(delete=False)
                    self.loggerdeco.debug('logging to %s' % logfilehandle.name)

                    args = self.create_test_args(test_parameters)

                    test_parameters['cmdline'] = ' '.join(args)
                    self.loggerdeco.debug("cmdline = %s" %
                                          test_parameters['cmdline'])

                    self.update_status(message='Running test %s chunk %d of %d' %
                                       (test_parameters['test_name'],
                                        this_chunk, test_parameters['total_chunks']))
                    if self.dm.process_exist(test_parameters['app_name']):
                        max_kill_attempts = 3
                        for kill_attempt in range(1, max_kill_attempts+1):
                            self.loggerdeco.debug(
                                'Process %s exists. Attempt %d to kill.' % (
                                    test_parameters['app_name'], kill_attempt + 1))
                            self.dm.pkill(test_parameters['app_name'])
                            if not self.dm.process_exist(test_parameters['app_name']):
                                break
                        if kill_attempt == max_kill_attempts and \
                                self.dm.process_exist(test_parameters['app_name']):
                            self.loggerdeco.warning(
                                'Could not kill process %s.' % (
                                    test_parameters['app_name']))
                    proc = subprocess.Popen(
                        args,
                        cwd=os.path.join(test_parameters['build_dir'],
                                         'tests'),
                        preexec_fn=lambda: os.setpgid(0, 0),
                        stdout=logfilehandle,
                        stderr=subprocess.STDOUT,
                        close_fds=True
                    )
                    proc.wait()
                    self.loggerdeco.debug('runtestsremote.py return code %d' %
                                          proc.returncode)

                    logfilehandle.close()
                    re_socket_error = re.compile('socket\.error:')
                    logfilehandle = open(logfilehandle.name)
                    for logline in logfilehandle:
                        if re_socket_error.search(logline):
                            socket_collision = True
                            break
                    logfilehandle.close()

                    if not socket_collision:
                        break

                self.update_status(message='Completed test %s chunk %d of %d' %
                                   (test_parameters['test_name'],
                                    this_chunk, test_parameters['total_chunks']))
            except:
                error_message = ('Exception during test %s chunk %d of %d: %s' %
                                 (test_parameters['test_name'],
                                  this_chunk, test_parameters['total_chunks'],
                                  traceback.format_exc()))
                self.update_status(message=error_message)
                self.loggerdeco.error(error_message)
            finally:
                logfilehandle.close()
                self.process_test_log(test_parameters, logfilehandle)
                if self.logger.getEffectiveLevel() == logging.DEBUG:
                    logfilehandle = open(logfilehandle.name)
                    self.loggerdeco.debug(40 * '*')
                    self.loggerdeco.debug(logfilehandle.read())
                    self.loggerdeco.debug(40 * '-')
                    logfilehandle.close()
                os.unlink(logfilehandle.name)
                # wait for a minute to give the phone time to settle
                time.sleep(60)
                # Recover the phone in between tests/chunks.
                self.loggerdeco.info('Rebooting device after test.')
                self.worker_subprocess.recover_phone()

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
