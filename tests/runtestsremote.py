# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import subprocess
from phonetest import PhoneTest
import socket
import ConfigParser
import posixpath
import os
import tempfile
import traceback
import logging
from logparser import LogParser
from mozautolog import RESTfulAutologTestGroup
import re
import time
from mozdevice import DMError

import newlogparser

try:
    import json
except ImportError:
    # for python 2.5 compatibility
    import simplejson as json


class UnitTest(PhoneTest):

    def runjob(self, build_metadata, worker_subprocess):

        self.logger.debug('runtestsremote.py runjob start')
        self.set_status(msg='runtestsremote.py runjob start')

        self.worker_subprocess = worker_subprocess
        self.worker_subprocess.check_sdcard()

        host_ip_address = self.phone_cfg['ipaddr']
        phone_ip_address = self.phone_cfg['ip']
        device_port = self.phone_cfg['sutcmdport']

        cache_build_dir = os.path.abspath(build_metadata["cache_build_dir"])
        symbols_path = os.path.join(cache_build_dir, 'symbols')
        if not os.path.exists(symbols_path):
            symbols_path = None

        androidprocname = build_metadata['androidprocname']
        revision = build_metadata['revision']
        buildid = build_metadata['buildid']
        tree = build_metadata['tree']

        if self.logger.getEffectiveLevel() == logging.DEBUG:
            for prop in self.phone_cfg:
                self.logger.debug('phone_cfg[%s] = %s' %
                                  (prop, self.phone_cfg[prop]))

        job_cfg = ConfigParser.RawConfigParser()
        job_cfg.read(self.config_file)

        if job_cfg.has_option('runtests', 'config_files'):
            # job contains multiple unittests
            config_files = job_cfg.get('runtests', 'config_files').split(' ')
        elif job_cfg.has_option('runtests', 'test_name'):
            # job contains a single unittest
            config_files = [self.config_file]
        else:
            raise Exception('Job configuration does not specify a test')

        for config_file in config_files:
            try:
                test_parameters = {
                    'host_ip_address': host_ip_address,
                    'phone_ip_address': phone_ip_address,
                    'device_port': device_port,
                    'androidprocname': androidprocname,
                    'cache_build_dir': cache_build_dir,
                    'symbols_path': symbols_path,
                    'revision': revision,
                    'buildid': buildid,
                    'tree': tree,
                    'config_file': config_file,
                }

                self.load_test_parameters(test_parameters, config_file)
                self.runtest(test_parameters)

            except:
                # This exception handler deals with exceptions which occur outside
                # of the actual test runner. Exceptions from the test runner
                # are handled locally in runtest.
                error_message = ('runtestsremote.py:runjob: Exception running test: %s' %
                                 traceback.format_exc())
                self.logger.error(error_message)
                self.set_status(msg=error_message)
                # give the phone a minute to recover
                time.sleep(60)
                self.worker_subprocess.recover_phone()

        self.logger.debug('runtestsremote.py runjob exit')
        self.set_status(msg='runtestsremote.py runjob exit')

    def load_test_parameters(self, test_parameters, config_file):

        cfg = ConfigParser.RawConfigParser()
        cfg.read(config_file)

        test_parameters['test_name'] = cfg.get('runtests', 'test_name')

        unittest_config_file = cfg.get('runtests', 'unittest_defaults')
        cfg.read(unittest_config_file)

        self.logger.info('config_file = %s, unittest_config_file = %s' %
                         (config_file, unittest_config_file))

        test_parameters['xre_path'] = cfg.get('runtests', 'xre_path')
        test_parameters['utility_path'] = cfg.get('runtests', 'utility_path')
        if test_parameters['symbols_path'] is not None and cfg.has_option('runtests',
                                                       'minidump_stackwalk'):
            test_parameters['minidump_stackwalk'] = cfg.get('runtests', 'minidump_stackwalk')
            os.environ['MINIDUMP_STACKWALK'] = test_parameters['minidump_stackwalk']
        elif 'MINIDUMP_STACKWALK' in os.environ:
            del os.environ['MINIDUMP_STACKWALK']

        if cfg.has_option('runtests', 'androidprocname'):
            test_parameters['androidprocname'] = cfg.get('runtests', 'androidprocname')

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

        test_parameters['es_server'] = cfg.get('autolog', 'es_server')
        test_parameters['rest_server'] = cfg.get('autolog', 'rest_server')
        test_parameters['index'] = cfg.get('autolog', 'index')
        test_parameters['include_pass'] = cfg.getboolean('autolog', 'include_pass')
        test_parameters['submit_log'] = cfg.getboolean('autolog', 'submit_log')
        test_parameters['use_newparser'] = cfg.getboolean('autolog', 'use_newparser')

    def create_test_args(self, test_parameters):
        args = ['python']

        if test_parameters['test_name'] == 'robocoptest':

            test_args = [
                'mochitest/runtestsremote.py',
                '--robocop=mochitest/robocop_autophone.ini',
                '--robocop-ids=%s/fennec_ids.txt' % test_parameters['cache_build_dir'],
                '--certificate-path=certs',
                '--close-when-done',
                '--autorun',
                '--console-level=%s' % test_parameters['console_level'],
                '--file-level=%s' % test_parameters['file_level'],
                '--repeat=%d' % test_parameters['iterations'],
            ]
        elif test_parameters['test_name'] == 'mochitest':
            test_args = [
                'mochitest/runtestsremote.py',
                '--test-manifest=mochitest/android.json',
                '--certificate-path=certs',
                '--close-when-done',
                '--autorun',
                '--console-level=%s' % test_parameters['console_level'],
                '--file-level=%s' % test_parameters['file_level'],
                '--repeat=%d' % test_parameters['iterations'],
            ]
        elif test_parameters['test_name'] == 'reftest':
            test_args = [
                'reftest/remotereftest.py',
                '--enable-privilege',
                '--ignore-window-size',
                '--bootstrap',
                'reftest/tests/layout/reftests/reftest.list',
                ]
        elif test_parameters['test_name'] == 'jsreftest':
            test_args = [
                'reftest/remotereftest.py',
                '--enable-privilege',
                '--ignore-window-size',
                '--bootstrap',
                '--extra-profile-file=jsreftest/tests/user.js',
                'jsreftest/tests/jstests.list',
                ]
        elif test_parameters['test_name'] == 'crashtest':
            test_args = [
                'reftest/remotereftest.py',
                '--enable-privilege',
                '--ignore-window-size',
                '--bootstrap',
                'reftest/tests/testing/crashtest/crashtests.list',
                ]
        else:
            self.logger.error('Unknown test_name %s' % test_parameters['test_name'])
            raise Exception('Unknown test_name %s' % test_parameters['test_name'])

        test_parameters['http_port'] = test_parameters['port_manager'].reserve()
        test_parameters['ssl_port'] = test_parameters['port_manager'].reserve()

        common_args = [
            '--deviceIP=%s' % test_parameters['phone_ip_address'],
            '--devicePort=%s' % test_parameters['device_port'],
            '--app=%s' % test_parameters['androidprocname'],
            '--xre-path=%s' % test_parameters['xre_path'],
            '--utility-path=%s' % test_parameters['utility_path'],
            '--timeout=%d' % test_parameters['time_out'],
            '--http-port=%s' % test_parameters['port_manager'].use(test_parameters['http_port']),
            '--ssl-port=%s' % test_parameters['port_manager'].use(test_parameters['ssl_port']),
            '--total-chunks=%d' % test_parameters['total_chunks'],
            '--this-chunk=%d' % test_parameters['this_chunk'],
            '--log-file=%s-%s.log' % (test_parameters['test_name'], test_parameters['phone_ip_address']),
            '--pidfile=%s-%s.pid' % (test_parameters['test_name'], test_parameters['phone_ip_address']),
        ]

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
                logger_effectiveLevel = logger.getEffectiveLevel()
                logger.setLevel(logging.WARN)
                test_log = newlogparser.parse_log(logfilehandle)
                test_runs = test_log.convert(test_parameters['include_pass'])
            finally:
                logger.setLevel(logger_effectiveLevel)
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

        platform_name = self.phone_cfg['machinetype']

        self.logger.debug('testgroup_name = %s' % testgroup_name)

        testgroup = RESTfulAutologTestGroup(
            index=test_parameters['index'],
            testgroup=testgroup_name,
            os='android',
            platform=platform_name,
            harness=test_parameters['harness_type'],
            server=test_parameters['es_server'],
            restserver=test_parameters['rest_server'],
            machine=self.phone_cfg['phoneid'],
            logfile=logfilename)

        testgroup.set_primary_product(
            tree=test_parameters['tree'],
            buildtype='opt',
            buildid=test_parameters['buildid'],
            revision=test_parameters['revision'])

        for testdata in test_runs:

            self.logger.debug('Begin testdata')
            self.logger.debug(json.dumps(testdata, indent=4))
            self.logger.debug('End testdata')

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

        if self.logger.getEffectiveLevel() == logging.DEBUG:
            self.logger.debug('runtestsremote.py runtest start')
            for key in test_parameters.keys():
                self.logger.debug('test parameters: %s = %s' %
                                  (key, test_parameters[key]))

        self.set_status(msg='Starting test %s' % test_parameters['test_name'])

        test_parameters['harness_type'] = test_parameters['test_name']

        if test_parameters['test_name'] == 'robocoptest':
            test_parameters['harness_type'] = 'mochitest'
            robocop_apk_path = posixpath.join(self.dm.getDeviceRoot(), 'robocop.apk')

            # XXX: FIXME. When bug 792072 lands, change to have
            # installApp() push the file

            self.dm.pushFile(os.path.join(test_parameters['cache_build_dir'],
                                          'robocop.apk'),
                             robocop_apk_path)
            try:
                self.dm.uninstallApp('org.mozilla.roboexample.test')
            except DMError:
                self.logger.info('runtestsremote.py:runtest: Exception running test: %s' %
                                 traceback.format_exc())

            self.dm.installApp(robocop_apk_path)
            self.dm.removeFile(robocop_apk_path)

        test_parameters['port_manager'] = PortManager(test_parameters['host_ip_address'])

        for this_chunk in range(1, test_parameters['total_chunks'] + 1):

            test_parameters['this_chunk'] = this_chunk

            try:
                while True:
                    socket_collision = False

                    logfilehandle = tempfile.NamedTemporaryFile(delete=False)
                    self.logger.debug('logging to %s' % logfilehandle.name)

                    args = self.create_test_args(test_parameters)

                    test_parameters['cmdline'] = ' '.join(args)
                    self.logger.debug("cmdline = %s" %
                                      test_parameters['cmdline'])

                    self.set_status(msg='Running test %s chunk %d of %d' %
                                    (test_parameters['test_name'],
                                     this_chunk, test_parameters['total_chunks']))

                    proc = subprocess.Popen(
                        args,
                        cwd=os.path.join(test_parameters['cache_build_dir'],
                                         'tests'),
                        preexec_fn=lambda: os.setpgid(0, 0),
                        stdout=logfilehandle,
                        stderr=subprocess.STDOUT,
                        close_fds=True
                    )
                    proc.wait()
                    self.logger.debug('runtestsremote.py return code %d' %
                                      proc.returncode)

                    logfilehandle.close()
                    reSocketError = re.compile('socket\.error:')
                    logfilehandle = open(logfilehandle.name)
                    for logline in logfilehandle:
                        if reSocketError.search(logline):
                            socket_collision = True
                            break
                    logfilehandle.close()

                    if not socket_collision:
                        break

                self.set_status(msg='Completed test %s chunk %d of %d' %
                                (test_parameters['test_name'],
                                 this_chunk, test_parameters['total_chunks']))
            except:
                error_message = ('Exception during test %s chunk %d of %d: %s' %
                                 (test_parameters['test_name'],
                                  this_chunk, test_parameters['total_chunks'],
                                  traceback.format_exc()))
                self.set_status(msg=error_message)
                self.logger.error(error_message)
            finally:
                logfilehandle.close()
                self.process_test_log(test_parameters, logfilehandle)
                if self.logger.getEffectiveLevel() == logging.DEBUG:
                    logfilehandle = open(logfilehandle.name)
                    self.logger.debug(40 * '*')
                    self.logger.debug(logfilehandle.read())
                    self.logger.debug(40 * '-')
                    logfilehandle.close()
                os.unlink(logfilehandle.name)
                # wait for a minute to give the phone time to settle
                time.sleep(60)
                # Recover the phone in between tests/chunks.
                self.logger.info('Rebooting device after test.')
                self.worker_subprocess.recover_phone()

        self.logger.debug('runtestsremote.py runtest exit')

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
