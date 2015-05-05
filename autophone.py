# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import ConfigParser
import Queue
import SocketServer
import datetime
import errno
import inspect
import json
import logging
import logging.handlers
import multiprocessing
import os
import re
import signal
import socket
import sys
import threading
from multiprocessinghandlers import (MultiprocessingStreamHandler,
                                     MultiprocessingTimedRotatingFileHandler)

from manifestparser import TestManifest

import builds
import buildserver
import jobs
import utils

#from adb import ADBHost
from adb_android import ADBAndroid as ADBDevice
from autophonepulsemonitor import AutophonePulseMonitor
from autophonetreeherder import AutophoneTreeherder
from mailer import Mailer
from options import AutophoneOptions
from phonestatus import PhoneStatus
from phonetest import PhoneTest
from worker import PhoneWorker

class PhoneData(object):
    def __init__(self, phoneid, serial, machinetype, osver, abi, sdk, ipaddr):
        self.id = phoneid
        self.serial = serial
        self.machinetype = machinetype
        self.osver = osver
        self.abi = abi
        self.sdk = sdk
        self.host_ip = ipaddr

    @property
    def architecture(self):
        abi = self.abi
        if 'armeabi-v7a' in abi:
            abi = 'armv7'
        return abi

    @property
    def os(self):
        return 'android-%s' % '-'.join(self.osver.split('.')[:2])

    @property
    def platform(self):
        if self.architecture == 'x86':
            return '%s-x86' % self.os
        return '%s-%s-%s' % (self.os,
                             self.architecture,
                             ''.join(self.sdk.split('-')))

    def __str__(self):
        return '%s' % self.__dict__


class AutoPhone(object):

    class CmdTCPServer(SocketServer.ThreadingMixIn, SocketServer.TCPServer):

        allow_reuse_address = True
        daemon_threads = True
        cmd_cb = None

    class CmdTCPHandler(SocketServer.BaseRequestHandler):
        def handle(self):
            buffer = ''
            self.request.send('Hello? Yes this is Autophone.\n')
            while True:
                try:
                    data = self.request.recv(1024)
                except socket.error, e:
                    if e.errno == errno.ECONNRESET:
                        break
                    raise e
                if not data:
                    break
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
                    response = self.server.cmd_cb(line)
                    self.request.send(response + '\n')

    def __init__(self, loglevel, options):
        self.logger = logging.getLogger('autophone')
        self.console_logger = logging.getLogger('autophone.console')
        self.options = options
        self.loglevel = loglevel
        self.mailer = Mailer(options.emailcfg, '[autophone] ')

        self._stop = False
        self._next_worker_num = 0
        self.jobs = jobs.Jobs(self.mailer)
        self.phone_workers = {}  # indexed by phone id
        self.worker_lock = threading.Lock()
        self.cmd_lock = threading.Lock()
        self._tests = []
        self._devices = {} # hash indexed by device names found in devices ini file
        self.server = None
        self.server_thread = None
        self.pulse_monitor = None
        self.restart_workers = {}
        self.treeherder = AutophoneTreeherder(None,
                                              self.logger,
                                              self.options)

        self.console_logger.info('Starting autophone.')

        # queue for listening to status updates from tests
        self.worker_msg_queue = multiprocessing.Queue()

        self.console_logger.info('Loading tests.')
        self.read_tests()

        self.console_logger.info('Initializing devices.')

        self.read_devices()

        if options.enable_pulse:
            self.pulse_monitor = AutophonePulseMonitor(
                userid=options.pulse_user,
                password=options.pulse_password,
                jobaction_exchange_name=options.pulse_jobactions_exchange,
                build_callback=self.on_build,
                jobaction_callback=self.on_jobaction,
                treeherder_url=self.options.treeherder_url,
                trees=options.repos,
                platforms=['android',
                           'android-api-9',
                           'android-api-10',
                           'android-api-11',
                           'android-x86'],
                buildtypes=options.buildtypes,
                durable_queues=self.options.pulse_durable_queue)
            self.pulse_monitor.start()
        self.logger.debug('autophone_options: %s' % self.options)

        self.console_logger.info('Autophone started.')

    @property
    def next_worker_num(self):
        n = self._next_worker_num
        self._next_worker_num += 1
        return n

    def run(self):
        self.server = self.CmdTCPServer(('0.0.0.0', self.options.port),
                                        self.CmdTCPHandler)
        self.server.cmd_cb = self.route_cmd
        self.server_thread = threading.Thread(target=self.server.serve_forever)
        self.server_thread.daemon = True
        self.server_thread.start()
        self.worker_msg_loop()

    def check_for_dead_workers(self):
        for phoneid, worker in self.phone_workers.iteritems():
            if not worker.is_alive():
                if phoneid in self.restart_workers:
                    self.logger.info('Worker %s exited; restarting with new '
                                     'values.' % phoneid)
                    self.create_worker(self.restart_workers[phoneid])
                    del self.restart_workers[phoneid]
                    continue

                self.console_logger.error('Worker %s died!' % phoneid)
                worker.stop()
                worker.crashes.add_crash()
                msg_subj = 'Worker for phone %s died' % \
                    worker.phone.id
                msg_body = 'Hello, this is Autophone. Just to let you know, ' \
                    'the worker process\nfor phone %s died.\n' % \
                    worker.phone.id
                if worker.crashes.too_many_crashes():
                    initial_state = PhoneStatus.DISABLED
                    msg_subj += ' and was disabled'
                    msg_body += 'It looks really crashy, so I disabled it. ' \
                        'Sorry about that.\n'
                else:
                    initial_state = PhoneStatus.DISCONNECTED
                worker.start(initial_state)
                self.logger.info('Sending notification...')
                self.mailer.send(msg_subj, msg_body)

    def worker_msg_loop(self):
        try:
            while not self._stop:
                self.check_for_dead_workers()
                if self.pulse_monitor and not self.pulse_monitor.is_alive():
                    self.pulse_monitor.start()
                try:
                    msg = self.worker_msg_queue.get(timeout=5)
                except Queue.Empty:
                    continue
                except IOError, e:
                    if e.errno == errno.EINTR:
                        continue
                self.phone_workers[msg.phone.id].process_msg(msg)
        except KeyboardInterrupt:
            self.stop()

    # Start the phones for testing
    def new_job(self, job_data):
        self.logger.debug('new_job: %s' % job_data)
        build_url = job_data['build']
        tests = job_data['tests']

        build_data = utils.get_build_data(build_url,
                                          logger=self.logger)
        self.logger.debug('new_job: build_data %s' % build_data)

        if not build_data:
            self.logger.warning('new_job: Could not find build_data for %s' %
                                build_url)
            return

        revision_hash = utils.get_treeherder_revision_hash(
            self.options.treeherder_url,
            build_data['repo'],
            build_data['revision'],
            logger=self.logger)

        self.logger.debug('new_job: revision_hash %s' % revision_hash)

        self.worker_lock.acquire()
        try:
            phoneids = set([test.phone.id for test in tests])
            for phoneid in phoneids:
                p = self.phone_workers[phoneid]
                self.logger.debug('new_job: worker phoneid %s' % phoneid)
                # Determine if we will test this build, which tests to run and if we
                # need to enable unittests.
                runnable_tests = PhoneTest.match(tests=tests, phoneid=phoneid)
                if not runnable_tests:
                    self.logger.debug('new_job: Ignoring build %s for phone %s' % (build_url, phoneid))
                    continue
                enable_unittests = False
                for t in runnable_tests:
                    enable_unittests = enable_unittests or t.enable_unittests

                new_tests = self.jobs.new_job(build_url,
                                              build_id=build_data['id'],
                                              changeset=build_data['changeset'],
                                              tree=build_data['repo'],
                                              revision=build_data['revision'],
                                              revision_hash=revision_hash,
                                              tests=runnable_tests,
                                              enable_unittests=enable_unittests,
                                              device=phoneid)
                if new_tests:
                    self.treeherder.submit_pending(phoneid,
                                                   build_url,
                                                   build_data['repo'],
                                                   revision_hash,
                                                   tests=new_tests)
                    self.logger.info('new_job: Notifying device %s of new job '
                                     '%s for tests %s, enable_unittests=%s.' %
                                     (phoneid, build_url, runnable_tests,
                                      enable_unittests))
                    p.new_job()
        finally:
            self.worker_lock.release()

    def route_cmd(self, data):
        response = ''
        self.cmd_lock.acquire()
        try:
            response = self._route_cmd(data)
        finally:
            self.cmd_lock.release()
        return response

    def _route_cmd(self, data):
        # There is not currently any way to get proper responses for commands
        # that interact with workers, since communication between the main
        # process and the worker processes is asynchronous.
        # It would be possible but nontrivial for the workers to put responses
        # onto a queue and have them routed to the proper connection by using
        # request IDs or something like that.
        self.logger.debug('route_cmd: %s' % data)
        data = data.strip()
        cmd, space, params = data.partition(' ')
        cmd = cmd.lower()
        response = 'ok'

        if cmd == 'stop':
            self.stop()
        elif cmd == 'log':
            self.logger.info(params)
        elif cmd == 'triggerjobs':
            response = self.trigger_jobs(params)
        elif cmd == 'status':
            response = ''
            now = datetime.datetime.now().replace(microsecond=0)
            phoneids = self.phone_workers.keys()
            phoneids.sort()
            for i in phoneids:
                w = self.phone_workers[i]
                response += 'phone %s (%s):\n' % (i, w.phone.serial)
                response += '  debug level %d\n' % w.options.debug
                if not w.last_status_msg:
                    response += '  no updates\n'
                else:
                    if w.last_status_msg.build and w.last_status_msg.build.id:
                        d = w.last_status_msg.build.id
                        d = '%s-%s-%s %s:%s:%s' % (d[0:4], d[4:6], d[6:8],
                                                   d[8:10], d[10:12], d[12:14])
                        response += '  current build: %s %s\n' % (
                            d,
                            w.last_status_msg.build.tree)
                    else:
                        response += '  no build loaded\n'
                    response += '  last update %s ago:\n    %s\n' % (now - w.last_status_msg.timestamp, w.last_status_msg.short_desc())
                    response += '  %s for %s\n' % (w.last_status_msg.phone_status, now - w.first_status_of_type.timestamp)
                    if w.last_status_of_previous_type:
                        response += '  previous state %s ago:\n    %s\n' % (now - w.last_status_of_previous_type.timestamp, w.last_status_of_previous_type.short_desc())
            response += 'ok'
        elif (cmd == 'disable' or cmd == 'enable' or cmd == 'debug' or
              cmd == 'ping' or cmd == 'reboot'):
            # Commands that take a phone as a parameter
            # FIXME: need start, stop, and remove
            phoneid, space, params = params.partition(' ')
            response = 'error: phone not found'
            for worker in self.phone_workers.values():
                if (phoneid.lower() == 'all' or
                    worker.phone.serial == phoneid or
                    worker.phone.id == phoneid):
                    f = getattr(worker, cmd)
                    if params:
                        f(params)
                    else:
                        f()
                    response = 'ok'
        else:
            response = 'Unknown command "%s"\n' % cmd
        return response

    def create_worker(self, phone):
        self.logger.info('Creating worker for %s: %s.' % (phone, self.options))
        tests = []
        for test_class, config_file, test_devices_repos in self._tests:
            skip_test = True
            if not test_devices_repos:
                # There is no restriction on this test being run by
                # specific devices.
                skip_test = False
            elif phone.id in test_devices_repos:
                # This test is to be run by this device on
                # the repos test_devices_repos[phone.id]
                skip_test = False
            if not skip_test:
                test = test_class(phone=phone,
                                  options=self.options,
                                  config_file=config_file)
                tests.append(test)
                for chunk in range(2, test.chunks+1):
                    self.logger.debug('Creating chunk %d/%d' % (chunk, test.chunks))
                    tests.append(test_class(phone=phone,
                                            options=self.options,
                                            config_file=config_file,
                                            chunk=chunk))
        if not tests:
            self.logger.warning('Not creating worker: No tests defined for '
                                'worker for %s: %s.' %
                                (phone, self.options))
            return
        logfile_prefix = os.path.splitext(self.options.logfile)[0]
        worker = PhoneWorker(self.next_worker_num,
                             tests, phone, self.options,
                             self.worker_msg_queue,
                             '%s-%s' % (logfile_prefix, phone.id),
                             self.loglevel, self.mailer)
        self.phone_workers[phone.id] = worker
        worker.start()

    def register_cmd(self, data):
        try:
            # Map MAC Address to ip and user name for phone
            # The configparser does odd things with the :'s so remove them.
            phoneid = data['device_name']
            phone = PhoneData(
                phoneid,
                data['serialno'],
                data['hardware'],
                data['osver'],
                data['abi'],
                data['sdk'],
                self.options.ipaddr) # XXX IPADDR no longer needed?
            if self.logger.getEffectiveLevel() == logging.DEBUG:
                self.logger.debug('register_cmd: phone: %s' % phone)
            if phoneid in self.phone_workers:
                self.logger.debug('Received registration message for known phone '
                                  '%s.' % phoneid)
                worker = self.phone_workers[phoneid]
                if worker.phone.__dict_ != phone.__dict__:
                    # This won't update the subprocess, but it will allow
                    # us to write out the updated values right away.
                    worker.phone = phone
                    self.logger.info('Registration info has changed; restarting '
                                     'worker.')
                    if phoneid in self.restart_workers:
                        self.logger.info('Phone worker is already scheduled to be '
                                     'restarted!')
                    else:
                        self.restart_workers[phoneid] = phone
                        worker.stop()
            else:
                self.create_worker(phone)
                self.logger.info('Registered phone %s.' % phone.id)
        except:
            self.logger.exception('register_cmd:')
            self.stop()

    def read_devices(self):
        cfg = ConfigParser.RawConfigParser()
        cfg.read(self.options.devicescfg)

        for device_name in cfg.sections():
            # failure for a device to have a serialno option is fatal.
            serialno = cfg.get(device_name, 'serialno')
            self.console_logger.info("Initializing device name=%s, serialno=%s" % (device_name, serialno))
            # XXX: We should be able to continue if a device isn't available immediately
            # or to add/remove devices but this will work for now.
            dm = ADBDevice(device=serialno,
                           logger_name='autophone.adb',
                           verbose=self.options.verbose)
            dm.power_on()
            device = {"device_name": device_name,
                      "serialno": serialno,
                      "dm" : dm}
            device['osver'] = dm.get_prop('ro.build.version.release')
            device['hardware'] = dm.get_prop('ro.product.model')
            device['abi'] = dm.get_prop('ro.product.cpu.abi')
            try:
                sdk = int(dm.get_prop('ro.build.version.sdk'))
                device['sdk'] = 'api-9' if sdk <= 10 else 'api-11'
            except ValueError:
                device['sdk'] = 'api-9'
            self._devices[device_name] = device
            self.register_cmd(device)


    def read_tests(self):
        self._tests = []
        manifest = TestManifest()
        manifest.read(self.options.test_path)
        tests_info = manifest.get()
        for t in tests_info:
            # Remove test section suffix.
            t['name'] = t['name'].split()[0]
            if not t['here'] in sys.path:
                sys.path.append(t['here'])
            if t['name'].endswith('.py'):
                t['name'] = t['name'][:-3]
            # add all classes in module that are derived from PhoneTest to
            # the test list
            tests = []
            for member_name, member_value in inspect.getmembers(__import__(t['name']),
                                                                inspect.isclass):
                if (member_name != 'PhoneTest' and
                    member_name != 'PerfTest' and
                    issubclass(member_value, PhoneTest)):
                    config = t.get('config', '')
                    # config is a space separated list of config
                    # files.  The test will be instantiated for each
                    # of the config files allowing tests such as the
                    # runremotetests.py to handle more than one unit
                    # test at a time.
                    #
                    # Each config file can contain additional options
                    # for a test.
                    #
                    # Other options are:
                    #
                    # <device> = <repo-list>
                    #
                    # which determines the devices which should
                    # run the test. If no devices are listed, then
                    # all devices will run the test.

                    devices = [device for device in t if device not in
                               ('name', 'here', 'manifest', 'path', 'config',
                                'relpath', 'unittests', 'subsuite')]
                    self.logger.debug('read_tests: test: %s, class: %s, '
                                      'config: %s, devices: %s' % (
                                          member_name,
                                          member_value,
                                          config,
                                          devices))
                    test_devices_repos = {}
                    for device in devices:
                        test_devices_repos[device] = t[device].split()
                    configs = config.split()
                    for config_file in configs:
                        config_file = os.path.normpath(
                            os.path.join(t['here'], config_file))
                        tests.append((member_value,
                                      config_file, test_devices_repos))

            self._tests.extend(tests)


    def trigger_jobs(self, data):
        self.logger.info('Received user-specified job: %s' % data)
        trigger_data = json.loads(data)
        if 'build' not in trigger_data:
            return 'invalid args'
        build_url = trigger_data['build']
        tests = []
        test_names = trigger_data['test_names']
        if not test_names:
            # No test names specified, force PhoneTest.match
            # to return tests with any name.
            test_names = [None]
        devices = trigger_data['devices']
        if not devices:
            # No devices specified, force PhoneTest.match
            # to return tests for any device.
            devices = [None]
        for test_name in test_names:
            for device in devices:
                tests.extend(PhoneTest.match(test_name=test_name,
                                             phoneid=device,
                                             build_url=build_url))
        if tests:
            job_data = {
                'build': build_url,
                'tests': tests,
            }
            self.new_job(job_data)
        return 'ok'

    def reset_phones(self):
        self.logger.info('Resetting phones...')
        for phoneid, phone in self.phone_workers.iteritems():
            phone.reboot()

    def on_build(self, msg):
        if self._stop:
            return
        self.logger.debug('PULSE BUILD FOUND %s' % msg)
        build_url = msg['packageUrl']
        if msg['branch'] != 'try':
            tests = PhoneTest.match(build_url=build_url)
        else:
            # Autophone try builds will have a comment of the form:
            # try: -b o -p android-api-9,android-api-11 -u autophone-smoke,autophone-s1s2 -t none
            tests = []
            reTests = re.compile('try:.* -u (.*) -t.*')
            match = reTests.match(msg['comments'])
            if match:
                test_names = [t for t in match.group(1).split(',')
                              if t.startswith('autophone-')]
                if 'autophone-tests' in test_names:
                    # Match all test names
                    test_names = [None]
                for test_name in test_names:
                    tests.extend(PhoneTest.match(test_name=test_name,
                                                 build_url=build_url))
        job_data = {'build': build_url, 'tests': tests}
        self.new_job(job_data)

    def on_jobaction(self, job_action):
        if self._stop or job_action['job_group_name'] != 'Autophone':
            return
        machine_name = job_action['machine_name']
        if machine_name not in self.phone_workers:
            self.logger.warning('on_jobaction: unknown device %s' % machine_name)
            return
        self.logger.debug('on_jobaction: found %s' % json.dumps(
            job_action, sort_keys=True, indent=4))

        worker_locked = False
        self.worker_lock.acquire()
        worker_locked = True
        try:
            p = self.phone_workers[machine_name]
            if job_action['action'] == 'cancel':
                request = (job_action['job_guid'],)
                self.worker_lock.release()
                worker_locked = False
                p.cancel_test(request)
            elif job_action['action'] == 'retrigger':
                test = PhoneTest.lookup(
                    machine_name,
                    job_action['config_file'],
                    job_action['chunk'])
                self.worker_lock.release()
                worker_locked = False
                if not test:
                    self.logger.warning(
                        'on_jobaction: No test found for %s' %
                        json.dumps(job_action, sort_keys=True, indent=4))
                else:
                    job_data = {
                        'build': job_action['build_url'],
                        'tests': [test],
                    }
                    self.new_job(job_data)
            else:
                self.logger.warning('on_jobaction: unknown action %s' %
                                    job_action['action'])
        finally:
            if worker_locked:
                self.worker_lock.release()

    def stop(self):
        self._stop = True
        for p in self.phone_workers.values():
            p.stop()
        if self.server:
            self.server.shutdown()
        if self.server_thread:
            self.server_thread.join()

def load_autophone_options(cmd_options):
    options = AutophoneOptions()
    option_tuples = [(option_name, type(option_value))
                     for option_name, option_value in inspect.getmembers(options)
                     if not option_name.startswith('_')]
    getter_map = {str: 'get', int: 'getint', bool: 'getboolean', list: 'get'}

    for option_name, option_type in option_tuples:
        try:
            value = getattr(cmd_options, option_name)
            if value is not None:
                value = option_type(value)
            setattr(options, option_name, value)
        except AttributeError:
            pass

    cfg = ConfigParser.RawConfigParser()
    if cmd_options.autophonecfg:
        cfg.read(cmd_options.autophonecfg)
        if cfg.has_option('settings', 'credentials_file'):
            cfg.read(cfg.get('settings', 'credentials_file'))
    if cmd_options.credentials_file:
        cfg.read(cmd_options.credentials_file)

    if cmd_options.autophonecfg or cmd_options.credentials_file:
        for option_name, option_type in option_tuples:
            try:
                getter = getattr(ConfigParser.RawConfigParser,
                                 getter_map[option_type])
                value = getter(cfg, 'settings', option_name)
                if option_type == list:
                    value = value.split()
                setattr(options, option_name, option_type(value))
            except ConfigParser.NoOptionError:
                pass

    if options.treeherder_url and options.treeherder_credentials_path:
        with open(options.treeherder_credentials_path) as credentials_file:
            setattr(options, 'treeherder_credentials', json.loads(credentials_file.read()))

    return options


def main(options):

    def sigterm_handler(signum, frame):
        autophone.stop()

    loglevel = e = None
    try:
        loglevel = getattr(logging, options.loglevel)
    except AttributeError, e:
        pass
    finally:
        if e or logging.getLevelName(loglevel) != options.loglevel:
            print 'Invalid log level %s' % options.loglevel
            return errno.EINVAL

    logger = logging.getLogger('autophone')
    logger.propagate = False
    logger.setLevel(loglevel)

    filehandler = MultiprocessingTimedRotatingFileHandler(options.logfile,
                                                          when='midnight',
                                                          backupCount=7)
    fileformatstring = ('%(asctime)s|%(levelname)s'
                        '|%(message)s')
    fileformatter = logging.Formatter(fileformatstring)
    filehandler.setFormatter(fileformatter)
    logger.addHandler(filehandler)

    console_logger = logging.getLogger('autophone.console')
    console_logger.setLevel(logging.INFO)
    streamhandler = MultiprocessingStreamHandler(stream=sys.stderr)
    streamformatstring = ('%(asctime)s|%(levelname)s'
                          '|%(message)s')
    streamformatter = logging.Formatter(streamformatstring)
    streamhandler.setFormatter(streamformatter)
    console_logger.addHandler(streamhandler)

    console_logger.info('Starting server on port %d.' % options.port)
    console_logger.info('Starting build-cache server on port %d.' %
                        options.build_cache_port)
    # Calling kill_server on the mac mini Autophone host horks
    # subsequent calls to adb devices. Temporarily disable until
    # this is resolved.
    # ensure that the previous runs have not left open adb ports.
    #adbhost = ADBHost()
    #adbhost.kill_server()

    product = 'fennec'
    build_platforms = ['android',
                       'android-api-9',
                       'android-api-10',
                       'android-api-11',
                       'android-x86']
    buildfile_ext = '.apk'
    try:
        build_cache = builds.BuildCache(
            options.repos,
            options.buildtypes,
            product,
            build_platforms,
            buildfile_ext,
            cache_dir=options.cache_dir,
            override_build_dir=options.override_build_dir,
            build_cache_size=options.build_cache_size,
            build_cache_expires=options.build_cache_expires,
            treeherder_url=options.treeherder_url)
    except builds.BuildCacheException, e:
        print '''%s

When specifying --override-build-dir, the directory must already exist
and contain a build.apk package file to be tested.

In addition, if you have specified --enable-unittests, the override
build directory must also contain a tests directory containing the
unpacked tests package for the build.

        ''' % e
        raise

    build_cache_server = buildserver.BuildCacheServer(
        ('127.0.0.1', options.build_cache_port),
        buildserver.BuildCacheHandler)
    build_cache_server.build_cache = build_cache
    build_cache_server_thread = threading.Thread(
        target=build_cache_server.serve_forever)
    build_cache_server_thread.daemon = True
    build_cache_server_thread.start()

    autophone = AutoPhone(loglevel, options)

    signal.signal(signal.SIGTERM, sigterm_handler)
    autophone.run()
    # Drop pending messages and commands to prevent hangs on shutdown.
    # autophone.worker_msg_queue
    while True:
        try:
            msg = autophone.worker_msg_queue.get_nowait()
            logger.debug('Dropping  worker_msg_queue: %s' % msg)
        except Queue.Empty:
            break

    # worker.cmd_queue
    for phoneid in autophone.phone_workers:
        worker = autophone.phone_workers[phoneid]
        while True:
            try:
                msg = worker.cmd_queue.get_nowait()
                logger.debug('Dropping phone %s cmd_queue: %s' % (phoneid, msg))
            except Queue.Empty:
                break

    console_logger.info('AutoPhone terminated.')
    console_logger.info('Shutting down build-cache server...')
    build_cache_server.shutdown()
    build_cache_server_thread.join()
    console_logger.info('Done.')
    return 0


if __name__ == '__main__':
    from optparse import OptionParser

    parser = OptionParser()
    parser.add_option('--ipaddr', action='store', type='string', dest='ipaddr',
                      default=None, help='IP address of interface to use for '
                      'phone callbacks, e.g. after rebooting. If not given, '
                      'it will be guessed.')
    parser.add_option('--port', action='store', type='int', dest='port',
                      default=28001,
                      help='Port to listen for incoming connections, defaults '
                      'to 28001')
    parser.add_option('--logfile', action='store', type='string',
                      dest='logfile', default='autophone.log',
                      help='Log file to store logging from entire system. '
                      'Individual phone worker logs will use '
                      '<logfile>-<phoneid>[.<ext>]. Default: autophone.log')
    parser.add_option('--loglevel', action='store', type='string',
                      dest='loglevel', default='INFO',
                      help='Log level - ERROR, WARNING, DEBUG, or INFO, '
                      'defaults to INFO')
    parser.add_option('-t', '--test-path', action='store', type='string',
                      dest='test_path', default='tests/manifest.ini',
                      help='path to test manifest')
    parser.add_option('--minidump-stackwalk', action='store', type='string',
                      dest='minidump_stackwalk', default='/usr/local/bin/minidump_stackwalk',
                      help='Path to minidump_stackwalk executable; defaults to /usr/local/bin/minidump_stackwalk.')
    parser.add_option('--emailcfg', action='store', type='string',
                      dest='emailcfg', default='',
                      help='config file for email settings; defaults to none')
    parser.add_option('--phonedash-url', action='store', type='string',
                      dest='phonedash_url', default='',
                      help='Url to Phonedash server. If not set, results for '
                      'each device will be written to comma delimited files in '
                      'the form: autophone-results-<deviceid>.csv.')
    parser.add_option('--phonedash-user', action='store', type='string',
                      dest='phonedash_user', default='',
                      help='user id for connecting to Phonedash server')
    parser.add_option('--phonedash-password', action='store', type='string',
                      dest='phonedash_password', default='',
                      help='password for connecting to Phonedash server')
    parser.add_option('--webserver-url', action='store', type='string',
                      dest='webserver_url', default='',
                      help='Url to web server for remote tests.')
    parser.add_option('--enable-pulse', action='store_true',
                      dest="enable_pulse", default=False,
                      help='Enable connecting to Pulse to look for new builds. '
                      'If specified, --pulse-user and --pulse-password must also '
                      'be specified.')
    parser.add_option('--pulse-durable-queue', action='store_true',
                      dest="pulse_durable_queue", default=False,
                      help='Use a durable queue when connecting to Pulse.')
    parser.add_option('--pulse-user', action='store', type='string',
                      dest='pulse_user', default='',
                      help='user id for connecting to PulseGuardian')
    parser.add_option('--pulse-password', action='store', type='string',
                      dest='pulse_password', default='',
                      help='password for connecting to PulseGuardian')
    parser.add_option('--pulse-jobactions-exchange', action='store', type='string',
                      dest='pulse_jobactions_exchange',
                      default='exchange/treeherder/v1/job-actions',
                      help='Exchange for Pulse Job Actions queue; '
                      'defaults to exchange/treeherder/v1/job-actions.')
    parser.add_option('--cache-dir', type='string',
                      dest='cache_dir', default='builds',
                      help='Use the specified directory as the build '
                      'cache directory; defaults to builds.')
    parser.add_option('--override-build-dir', type='string',
                      dest='override_build_dir', default=None,
                      help='Use the specified directory as the current build '
                      'cache directory without attempting to download a build '
                      'or test package.')
    parser.add_option('--repo',
                      dest='repos',
                      action='append',
                      default=['mozilla-central'],
                      help='The repos to test. '
                      'One of b2g-inbound, fx-team, mozilla-aurora, '
                      'mozilla-beta, mozilla-central, mozilla-inbound, '
                      'mozilla-release, try. To specify multiple '
                      'repos, specify them with additional --repo options. '
                      'Defaults to mozilla-central.')
    parser.add_option('--buildtype',
                      dest='buildtypes',
                      action='append',
                      default=['opt'],
                      help='The build types to test. '
                      'One of opt or debug. To specify multiple build types, '
                      'specify them with additional --buildtype options. '
                      'Defaults to opt.')
    parser.add_option('--lifo',
                      dest='lifo',
                      action='store_true',
                      default=False,
                      help="""Process jobs in LIFO order. Default of False
                      implies FIFO order.""")
    parser.add_option('--build-cache-port',
                      dest='build_cache_port',
                      action='store',
                      type='int',
                      default=buildserver.DEFAULT_PORT,
                      help='Port for build-cache server. If you are running '
                      'multiple instances of autophone, this will have to be '
                      'different in each. Defaults to %d.' %
                      buildserver.DEFAULT_PORT)
    parser.add_option('--devices',
                      dest='devicescfg',
                      action='store',
                      type='string',
                      default='devices.ini',
                      help="""Devices configuration ini file.
                      Each device is listed by name in the sections of the ini file.""")
    parser.add_option('--config',
                      dest='autophonecfg',
                      action='store',
                      type='string',
                      default=None,
                      help="""Optional autophone.py configuration ini file.
                      The values of the settings in the ini file override
                      any settings set on the command line.
                      autophone.ini.example contains all of the currently
                      available settings.""")
    parser.add_option('--credentials-file',
                      dest='credentials_file',
                      action='store',
                      type='string',
                      default=None,
                      help="""Optional autophone.py configuration ini file
                      which is to be loaded in addition to that specified
                      by the --config option. It is intended to contain
                      sensitive options such as credentials which should not
                      be checked into the source repository.
                      The values of the settings in the ini file override
                      any settings set on the command line.
                      autophone.ini.example contains all of the currently
                      available settings.""")
    parser.add_option('--verbose', action='store_true',
                      dest='verbose', default=False,
                      help='Include output from ADBDevice command_output and '
                      'shell_output commands when loglevel is DEBUG. '
                      'Defaults to False.')
    parser.add_option('--treeherder-url',
                      dest='treeherder_url',
                      action='store',
                      type='string',
                      default=None,
                      help="""Url of the treeherder server where test results are reported.
                      Defaults to None.""")
    parser.add_option('--treeherder-credentials-path',
                      dest='treeherder_credentials_path',
                      action='store',
                      type='string',
                      default=None,
                      help="""Path to credentials.json file containing OAuth
                      credentials for contacting the Treeherder server.
                      Defaults to None. If specified, --treeherder-url
                      must also be specified.""")
    parser.add_option('--treeherder-retries',
                      dest='treeherder_retries',
                      action='store',
                      type='int',
                      default=3,
                      help="""Number of attempts for sending data to
                      Treeherder. Defaults to 3.""")
    parser.add_option('--treeherder-retry-wait',
                      dest='treeherder_retry_wait',
                      action='store',
                      type='int',
                      default=300,
                      help="""Number of seconds to wait between attempts
                      to send data to Treeherder. Defaults to 300.""")
    parser.add_option('--s3-upload-bucket',
                      dest='s3_upload_bucket',
                      action='store',
                      type='string',
                      default=None,
                      help="""AWS S3 bucket name used to store logs.
                      Defaults to None. If specified, --aws-access-key-id
                      and --aws-secret-access-key must also be specified.
                      """)
    parser.add_option('--aws-access-key-id',
                      dest='aws_access_key_id',
                      action='store',
                      type='string',
                      default=None,
                      help="""AWS Access Key ID used to access AWS S3.
                      Defaults to None. If specified, --s3-upload-bucket
                      and --aws-secret-access-key must also be specified.
                      """)
    parser.add_option('--aws-access-key',
                      dest='aws_access_key',
                      action='store',
                      type='string',
                      default=None,
                      help="""AWS Access Key used to access AWS S3.
                      Defaults to None. If specified, --s3-upload-bucket
                      and --aws-secret-access-key-id must also be specified.
                      """)

    (cmd_options, args) = parser.parse_args()
    if cmd_options.treeherder_url and not cmd_options.treeherder_credentials_path:
        raise Exception('--treeherder-url specified without '
                        '--treeherder-credentials_path')
    elif not cmd_options.treeherder_url and cmd_options.treeherder_credentials_path:
        raise Exception('--treeherder-credentials_path specified without '
                        '--treeherder-url')
    if ((cmd_options.s3_upload_bucket or
         cmd_options.aws_access_key_id or
         cmd_options.aws_access_key) and (
             not cmd_options.s3_upload_bucket or
             not cmd_options.aws_access_key_id or
             not cmd_options.aws_access_key)):
        raise Exception('--s3-upload-bucket, --aws-access-key-id, '
                        '--aws-access-key must be specified together')
    options = load_autophone_options(cmd_options)

    exit_code = main(options)

    sys.exit(exit_code)
