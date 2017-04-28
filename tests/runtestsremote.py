# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import ConfigParser
import errno
import fcntl
import glob
import logging
import os
import re
import socket
import subprocess
import tempfile
import time
import traceback

from phonetest import PhoneTest, TreeherderStatus, TestStatus, FLASH_PACKAGE


class UnitTest(PhoneTest):
    def __init__(self, dm=None, phone=None, options=None,
                 config_file=None, chunk=1, repos=[]):
        PhoneTest.__init__(self, dm=dm, phone=phone, options=options,
                           config_file=config_file, chunk=chunk, repos=repos)
        # Set the profile relative to the base_device_path. This will
        # match the profile used by the Unit Test runner.
        self.enable_unittests = True
        self.unittest_cfg = ConfigParser.RawConfigParser()

        unittest_config_file = self.cfg.get('runtests', 'unittest_defaults')
        self.unittest_cfg.read(unittest_config_file)

        self.loggerdeco.info('config_file = %s, unittest_config_file = %s' %
                             (config_file, unittest_config_file))

        self.parms = {
            'phoneid': self.phone.id,
            'config_file': config_file,
            'test_name': self.cfg.get('runtests', 'test_name'),
            'test_manifest': self.cfg.get('runtests', 'test_manifest'),
            'test_packages': set(self.cfg.get('runtests',
                                              'test_package_names').split(' ')),
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

        self.phone_ip_address = None

        self.loggerdeco.debug('UnitTest: %s', self.__dict__)

    @property
    def name(self):
        return 'autophone-%s%s' % (self.parms['test_name'], self.name_suffix)

    def get_test_package_names(self):
        return set(self.parms['test_packages'])

    def setup_job(self):
        PhoneTest.setup_job(self)
        # Remove the AutophoneCrashProcessor set in PhoneTest.setup_job
        # since the Unit Test runner will handle crash processing.
        self.crash_processor = None
        build_dir = self.build.dir
        symbols_path = self.build.symbols
        if symbols_path and not os.path.exists(symbols_path):
            symbols_path = None

        # Check that the device is accessible and that its network is up.
        ping_msg = self.worker_subprocess.ping(test=self, require_ip_address=True)
        if not self.worker_subprocess.is_ok():
            raise Exception(ping_msg)
        # Delay getting the phone's ip address until job setup.
        for attempt in range(1, self.options.device_ready_retry_attempts+1):
            self.phone_ip_address = self.dm.get_ip_address()
            if self.phone_ip_address:
                break
            self.loggerdeco.info('Attempt %d/%d failed to get ip address' %
                                 (attempt,
                                  self.options.device_ready_retry_attempts))
            time.sleep(self.options.device_ready_retry_wait)
        if not self.phone_ip_address:
            raise Exception('PhoneTest: Failed to get phone %s ip address' % self.phone.id)

        self.parms['host_ip_address'] = self.phone.host_ip
        self.parms['app_name'] = self.build.app_name
        self.parms['build_dir'] = build_dir
        self.parms['symbols_path'] = symbols_path
        self.parms['revision'] = self.build.revision
        self.parms['buildid'] = self.build.id
        self.parms['buildtype'] = self.build.type
        self.parms['tree'] = self.build.tree

        self.unittest_logpath = '%s/tests/%s-%s-%s-%s.log' % (
            build_dir,
            self.parms['test_name'],
            os.path.basename(self.config_file),
            self.chunk,
            self.parms['phoneid'])

        if self.parms['test_name'] == 'robocoptest-autophone':
            if self.dm.is_app_installed(FLASH_PACKAGE):
                self.dm.uninstall_app(FLASH_PACKAGE)
            try:
                sdk = int(self.dm.get_prop('ro.build.version.sdk'))
            except ValueError:
                sdk = 9
            if sdk < 14:
                flash_apk = 'apk/install_flash_player_pre_ics.apk'
            else:
                flash_apk = 'apk/install_flash_player_ics.apk'
            if os.path.exists(flash_apk):
                self.dm.install_app(flash_apk)
            else:
                raise Exception('%s does not exist' % flash_apk)

    def teardown_job(self):
        PhoneTest.teardown_job(self)
        roboexampletest = 'org.mozilla.roboexample.test'
        try:
            if self.dm.is_app_installed(roboexampletest):
                self.dm.uninstall_app(roboexampletest)
        except:
            self.loggerdeco.exception('Failure uninstalling %s', roboexampletest)
        try:
            if self.dm.is_app_installed(FLASH_PACKAGE):
                self.dm.uninstall_app(FLASH_PACKAGE)
        except:
            self.loggerdeco.exception('Failure uninstalling %s', FLASH_PACKAGE)

    def run_job(self):
        self.loggerdeco.debug('runtestsremote.py run_job start')
        self.update_status(message='runtestsremote.py run_job start')

        if self.loggerdeco.getEffectiveLevel() == logging.DEBUG:
            self.loggerdeco.debug('phone = %s' % self.phone)

        if not self.cfg.has_option('runtests', 'test_name'):
            raise Exception('Job configuration %s does not specify a test' %
                            self.config_file)
        lock_file = None
        lock_filehandle = None
        try:
            lock_file = self.cfg.get('runtests', 'lock_file')
        except (ConfigParser.NoSectionError, ConfigParser.NoOptionError):
            pass
        if lock_file:
            self.update_status(message='runtestsremote.py run_job obtaining lock %s' % lock_file)
            locked = False
            lock_filehandle = open('/tmp/%s' % lock_file, 'a')
            while not locked:
                try:
                    fcntl.lockf(lock_filehandle, fcntl.LOCK_EX|fcntl.LOCK_NB)
                    locked = True
                except IOError, e:
                    if e.errno != errno.EACCES and e.errno != errno.EAGAIN:
                        raise # propagate unexpected IOError
                    self.loggerdeco.warning('Failed to lock %s.', lock_file)
                    time.sleep(60)
                    command = self.worker_subprocess.process_autophone_cmd(
                        test=self, require_ip_address=True)
                    if command['interrupt']:
                        self.handle_test_interrupt(command['reason'],
                                                   command['test_result'])
                        lock_filehandle.close()
                        self.update_status(message='runtestsremote.py run_job '
                                           'interrupted during locking')
                        return False # test not completed
        try:
            is_test_completed = self.runtest()
        except:
            is_test_completed = False
            # This exception handler deals with exceptions which occur outside
            # of the actual test runner. Exceptions from the test runner
            # are handled locally in runtest.
            self.loggerdeco.exception('runtestsremote.py:run_job: Exception '
                                      'running test')
            self.update_status(message='runtestsremote.py:run_job: Exception '
                               'running test')
            # give the phone a minute to recover
            time.sleep(60)
            self.worker_subprocess.ping()
        finally:
            if lock_file:
                fcntl.lockf(lock_filehandle, fcntl.LOCK_UN)
                lock_filehandle.close()

        # Failure screenshots are irrelevent since they are of the host
        # and not the device and pose a potential information disclosure.
        # While failure screenshots can be disabled via command line arguments,
        # timeout failure screenshots can not be disabled.
        for screenshot in glob.glob(os.path.join(self.upload_dir,
                                                 '*test-fail-screenshot*')):
            try:
                self.loggerdeco.debug('runtestsremote.py deleting %s',
                                      screenshot)
                os.unlink(screenshot)
            except Exception, e:
                self.loggerdeco.debug('runtestsremote.py exception %s '
                                      'deleting %s', e, screenshot)
        self.loggerdeco.debug('runtestsremote.py run_job exit')
        self.update_status(message='runtestsremote.py run_job exit')
        return is_test_completed

    def create_test_args(self):
        args = ['python', '-u']

        test_name_lower = self.parms['test_name'].lower()

        if test_name_lower.startswith('robocoptest'):
            # See Bug 1179981 - Robocop harness has too much per-test overhead
            # which changed the way the robocop tests are run. If the new
            # runrobocop.py script is available, use that otherwise fall back
            # to the older runtestsremote.py script.
            self.parms['harness_type'] = 'mochitest'
            runrobocop_path = ('%s/tests/mochitest/runrobocop.py' %
                               self.parms['build_dir'])
            self.loggerdeco.debug('create_test_args: runrobocop_path: %s' %
                                  runrobocop_path)

            harness_script = 'mochitest/runtestsremote.py'
            if os.path.exists(runrobocop_path):
                harness_script = 'mochitest/runrobocop.py'

            test_args = [
                harness_script,
                '--robocop-ini=%s' % self.parms['test_manifest'],
                '--certificate-path=certs',
                '--console-level=%s' % self.parms['console_level'],
            ]

        elif test_name_lower.startswith('mochitest'):
            self.parms['harness_type'] = 'mochitest'

            # Create a short version of the testrun manifest file.
            fh, temppath = tempfile.mkstemp(
                suffix='.json',
                dir='%s/tests' % self.parms['build_dir'])
            os.close(fh)
            os.unlink(temppath)
            self.parms['testrun_manifest_file'] = temppath
            temppath = os.path.basename(temppath)
            self.parms['turn_port'] = self.parms['port_manager'].reserve()
            test_args = [
                'mochitest/runtestsremote.py',
                '--manifest=%s' % self.parms['test_manifest'],
                '--testrun-manifest-file=%s' % temppath,
                '--certificate-path=certs',
                '--console-level=%s' % self.parms['console_level'],
                '--websocket-process-bridge-port=%s' % self.parms['turn_port'],
            ]
            # Check if the test manifest defines a subsuite which
            # should be specified. Although we normally parse these
            # manifests with manifestparser, that is overkill for our
            # needs. Using the normal ConfigParser we can determine
            # the subsuite value if it is defined and add the subsuite
            # command line argument. Missing section or option errors
            # simply mean no subsuite is defined.
            try:
                manifest_cfg = ConfigParser.RawConfigParser()
                manifest_cfg.read("%s/tests/%s" % (
                    self.parms['build_dir'], self.parms['test_manifest']))
                test_subsuite = manifest_cfg.get('DEFAULT', 'subsuite')
                test_args.append('--subsuite=%s' % test_subsuite)
            except (ConfigParser.NoOptionError,
                    ConfigParser.MissingSectionHeaderError):
                pass

        elif test_name_lower.startswith('reftest'):
            self.parms['harness_type'] = 'reftest'

            test_args = [
                'reftest/remotereftest.py',
                '--suite=reftest',
                '--ignore-window-size',
                '%s' % self.parms['test_manifest'],
                ]
        elif test_name_lower.startswith('jsreftest'):
            self.parms['harness_type'] = 'reftest'

            test_args = [
                'reftest/remotereftest.py',
                '--suite=jstestbrowser',
                '--ignore-window-size',
                '--extra-profile-file=jsreftest/tests/user.js',
                '%s' % self.parms['test_manifest'],
                ]
        elif test_name_lower.startswith('crashtest'):
            self.parms['harness_type'] = 'reftest'

            test_args = [
                'reftest/remotereftest.py',
                '--suite=crashtest',
                '--ignore-window-size',
                '%s' % self.parms['test_manifest'],
                ]
        else:
            self.loggerdeco.error('Unknown test_name %s' % self.parms['test_name'])
            raise Exception('Unknown test_name %s' % self.parms['test_name'])

        if self.parms['iterations'] > 1:
            test_args.append('--repeat=%d' % (self.parms['iterations']-1))

        self.parms['http_port'] = self.parms['port_manager'].reserve()
        self.parms['ssl_port'] = self.parms['port_manager'].reserve()

        # Create a short version of the remote logfile name.
        fh, temppath = tempfile.mkstemp(
            suffix='.log',
            dir='%s/tests' % self.parms['build_dir'])
        os.close(fh)
        os.unlink(temppath)
        self.parms['remote_logfile'] = temppath
        remote_logfile = os.path.basename(temppath)

        # Create a short version of the pid file name.
        # We don't need to save this to self.parms since it will be
        # deleted after the test completes.
        fh, temppath = tempfile.mkstemp(
            suffix='.pid',
            dir='%s/tests' % self.parms['build_dir'])
        os.close(fh)
        os.unlink(temppath)
        pid_file = os.path.basename(temppath)

        common_args = [
            '--deviceSerial=%s' % self.phone.serial,
            '--remoteTestRoot=%s' % self.base_device_path,
            '--app=%s' % self.parms['app_name'],
            '--xre-path=%s' % self.parms['xre_path'],
            '--utility-path=%s' % self.parms['utility_path'],
            '--timeout=%d' % self.parms['time_out'],
            '--remote-webserver=%s' % self.parms['host_ip_address'],
            '--http-port=%s' % self.parms['http_port'],
            '--ssl-port=%s' % self.parms['ssl_port'],
            '--total-chunks=%d' % self.chunks,
            '--this-chunk=%d' % self.chunk,
            '--pidfile=%s' % pid_file,
            '--remote-logfile=%s' % remote_logfile,
        ]

        args.extend(test_args)
        args.extend(common_args)

        if self.parms['symbols_path'] is not None:
            args.append('--symbols-path=%s' % self.parms['symbols_path'])

        return args

    def runtest(self):

        self.loggerdeco = self.loggerdeco.clone(
            extradict={
                'buildid': self.parms['buildid'],
                'buildtype': self.parms['buildtype'],
                'testname': self.parms['test_name']
            },
            extraformat='UnitTestJob %(buildid)s %(buildtype)s %(testname)s %(message)s')

        self.loggerdeco.info('runtestsremote.py runtest start')
        for key in self.parms.keys():
            self.loggerdeco.info('test parameters: %s = %s' %
                                 (key, self.parms[key]))

        self.update_status(message='Starting test %s' % self.parms['test_name'])

        is_test_completed = False

        if self.parms['test_name'].startswith('robocoptest'):
            try:
                roboexampletest = 'org.mozilla.roboexample.test'
                if self.dm.is_app_installed(roboexampletest):
                    self.dm.uninstall_app(roboexampletest)
                robocop_apk_path = os.path.join(self.parms['build_dir'], 'robocop.apk')
                self.dm.install_app(robocop_apk_path)
            except Exception, e:
                self.loggerdeco.exception('runtestsremote.py:runtest: Exception running test.')
                self.status = TreeherderStatus.EXCEPTION
                self.message = 'Exception installing robocop.apk: %s' % e
                with open(self.unittest_logpath, "w") as logfilehandle:
                    logfilehandle.write('%s\n' % self.message)
                return is_test_completed

        self.parms['port_manager'] = PortManager(self.parms['host_ip_address'])

        # Create the env dictionary to pass to the test runner.
        env = dict(os.environ)
        # Set the environment to process crashes.

        env['MINIDUMP_STACKWALK'] = self.options.minidump_stackwalk
        env['MINIDUMP_SAVE_PATH'] = self.upload_dir
        env['MOZ_UPLOAD_DIR'] = self.upload_dir

        # Create PYTHONPATH to point the test runner to the test's
        # mozbase packages.  Be certain this contains absolute paths.
        build_path = os.path.abspath(self.parms['build_dir'])
        python_path = ':'.join(
            [pkg for pkg in
             glob.glob('%s/tests/mozbase/*' % build_path)
             if os.path.isdir(pkg)])
        env['PYTHONPATH'] = python_path
        try:
            is_test_completed = True
            logfilehandle = None

            self.loggerdeco.info('logging to %s' % self.unittest_logpath)
            if os.path.exists(self.unittest_logpath):
                os.unlink(self.unittest_logpath)

            args = self.create_test_args()

            self.parms['cmdline'] = ' '.join(args)
            self.loggerdeco.info("cmdline = %s" %
                                 self.parms['cmdline'])

            self.update_status(message='Running test %s chunk %d of %d' %
                               (self.parms['test_name'],
                                self.chunk, self.chunks))
            if self.dm.process_exist(self.parms['app_name']):
                kill_attempt = 0
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
            logfilehandle = open(self.unittest_logpath, 'wb')
            # Release the reserved ports for use by the Unittest
            # process. This will reduce though not eliminate the
            # possibility of socket contention between worker
            # processes.
            self.parms['port_manager'].release(self.parms['http_port'])
            self.parms['port_manager'].release(self.parms['ssl_port'])
            if 'turn_port' in self.parms:
                self.parms['port_manager'].release(self.parms['turn_port'])
            proc = subprocess.Popen(
                args,
                cwd=os.path.join(self.parms['build_dir'],
                                 'tests'),
                env=env,
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
                command = self.worker_subprocess.process_autophone_cmd(
                    test=self, require_ip_address=True)
                if command['interrupt']:
                    proc.kill()
                    proc.wait()
                    # Note handle_test_interrupt will add a failure
                    # and increment self.failed.
                    self.handle_test_interrupt(command['reason'],
                                               command['test_result'])
                    break
                # Don't beat up the device by pinging it
                # continually without a pause.
                time.sleep(60)
                # Collect logcat as we go, since many unit tests
                # will cause it to overflow. Autophone's copy of
                # the data should be complete while the version
                # collected by the unit test framework may be
                # missing the initial portions.
                self.worker_subprocess.logcat.get()

            logfilehandle.close()
            logfilehandle = open(self.unittest_logpath)
            passedRe = re.compile(r'(INFO |INFO \| |\t)(Passed|Successful):(\s+)(\d+)')
            failedRe = re.compile(r'(INFO |INFO \| |\t)(Failed|Unexpected):(\s+)(\d+)')
            todoRe = re.compile(r'(INFO |INFO \| |\t)(Todo|Known problems):(\s+)(\d+)')
            for logline in logfilehandle:
                match = passedRe.search(logline)
                if match:
                    self.passed += int(match.group(4))
                match = failedRe.search(logline)
                if match:
                    self.failed += int(match.group(4))
                match = todoRe.search(logline)
                if match:
                    self.todo += int(match.group(4))
            logfilehandle.close()
            logfilehandle = None
            self.loggerdeco.debug('self.failed = %s, self.status = %s', self.failed, self.status)
            if self.failed > 0 and self.status == TreeherderStatus.SUCCESS:
                # Handle case where we have counted a failure but not
                # set the Treeherder status to a non-success result.
                self.loggerdeco.debug('self.failed > 0 ... setting self.status = TreeherderStatus.TESTFAILED')
                self.status = TreeherderStatus.TESTFAILED

            if proc.returncode != 0:
                self.message = ('Test exited with return code %d' %
                                proc.returncode)
                # Only track the non zero return code as an error
                # if no other failures were detected. Otherwise we
                # would be counting an error twice.
                if self.failed == 0:
                    if self.status == TreeherderStatus.USERCANCEL:
                        treeherder_status = TreeherderStatus.USERCANCEL
                    else:
                        treeherder_status = TreeherderStatus.TESTFAILED

                    self.add_failure(
                        self.name, TestStatus.TEST_UNEXPECTED_FAIL,
                        self.message,
                        treeherder_status)

            self.loggerdeco.info('runtestsremote.py return code %d' %
                                 proc.returncode)


            self.update_status(message='Completed test %s chunk %d of %d' %
                               (self.parms['test_name'],
                                self.chunk, self.chunks))
        except:
            self.message = ('Exception during test %s chunk %d of %d: %s' %
                            (self.parms['test_name'],
                             self.chunk, self.chunks,
                             traceback.format_exc()))
            self.update_status(message=self.message)
            self.loggerdeco.error(self.message)
            self.status = TreeherderStatus.EXCEPTION
            if 'http_port' in self.parms:
                self.parms['port_manager'].release(self.parms['http_port'])
            if 'ssl_port' in self.parms:
                self.parms['port_manager'].release(self.parms['ssl_port'])
            if 'turn_port' in self.parms:
                self.parms['port_manager'].release(self.parms['turn_port'])
        finally:
            if logfilehandle:
                logfilehandle.close()
            tests_dir = '%s/tests' % self.build.dir
            test_classifier = '%s-%s-%s' % (self.parms['test_name'],
                                            self.chunk,
                                            self.parms['phoneid'])
            # Rename the short versions of the files to more readable versions.
            old_file = self.parms.get('testrun_manifest_file', None)
            if old_file and os.path.exists(old_file):
                new_file = os.path.join(tests_dir, '%s.json' % test_classifier)
                os.rename(old_file, new_file)
            old_file = self.parms.get('remote_logfile', None)
            if old_file and os.path.exists(old_file):
                new_file = os.path.join(tests_dir, 'remote-%s.log' % test_classifier)
                os.rename(old_file, new_file)
        self.loggerdeco.debug('runtestsremote.py runtest exit')

        return is_test_completed


class PortManager(object):
    '''
    Obtain a free port on ip address

    usage:
           port_manager = PortManager(ipaddress)
           port = port_manager.reserve()
           # ...
           port_manager.release(port)
           # open port for use

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

    def release(self, port):
        '''
        Prepare a reserved port for use by closing its socket.
        '''
        sock = self.reserved_ports[port]
        sock.close()
