# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import ConfigParser
import Queue
import SocketServer
import datetime
import errno
import inspect
import logging
import logging.handlers
import multiprocessing
import os
import signal
import socket
import sys
import threading

from manifestparser import TestManifest
from adb_android import ADBAndroid as ADBDevice
from pulsebuildmonitor import start_pulse_monitor

import builds
import buildserver
import jobs
import phonetest

from mailer import Mailer
from multiprocessinghandlers import MultiprocessingStreamHandler, MultiprocessingTimedRotatingFileHandler
from options import *
from worker import Crashes, PhoneWorker


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
        self.mailer = Mailer(options[EMAILCFG], '[autophone] ')

        self._stop = False
        self._next_worker_num = 0
        self.jobs = jobs.Jobs(self.mailer)
        self.phone_workers = {}  # indexed by mac address
        self.worker_lock = threading.Lock()
        self.cmd_lock = threading.Lock()
        self._tests = []
        self._devices = {} # hash indexed by serialno of devices found in devices ini file
        self.server = None
        self.server_thread = None
        self.pulsemonitor = None
        self.restart_workers = {}

        self.logger.info('Starting autophone.')
        self.console_logger.info('Starting autophone.')

        # queue for listening to status updates from tests
        self.worker_msg_queue = multiprocessing.Queue()

        self.logger.info('Loading tests.')
        self.console_logger.info('Loading tests.')
        self.read_tests()

        self.logger.info('Initializing devices.')
        self.console_logger.info('Initializing devices.')

        self.read_devices()

        if options[ENABLE_PULSE]:
            self.pulsemonitor = start_pulse_monitor(buildCallback=self.on_build,
                                                    trees=options[REPOS],
                                                    platforms=['android',
                                                               'android-armv6',
                                                               'android-x86'],
                                                    buildtypes=options[BUILDTYPES],
                                                    logger=self.logger)

        self.logger.debug('autophone_options: %s' % self.options)

        self.logger.info('Autophone started.')
        self.console_logger.info('Autophone started.')

    @property
    def next_worker_num(self):
        n = self._next_worker_num
        self._next_worker_num += 1
        return n

    def run(self):
        self.server = self.CmdTCPServer(('0.0.0.0', self.options[PORT]),
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
                    self.create_worker(self.restart_workers[phoneid],
                                       worker.user_cfg)
                    del self.restart_workers[phoneid]
                    continue

                self.logger.error('Worker %s died!' % phoneid)
                self.console_logger.error('Worker %s died!' % phoneid)
                worker.stop()
                worker.crashes.add_crash()
                msg_subj = 'Worker for phone %s died' % \
                    worker.phone_cfg['phoneid']
                msg_body = 'Hello, this is Autophone. Just to let you know, ' \
                    'the worker process\nfor phone %s died.\n' % \
                    worker.phone_cfg['phoneid']
                if worker.crashes.too_many_crashes():
                    initial_state = phonetest.PhoneTestMessage.DISABLED
                    msg_subj += ' and was disabled'
                    msg_body += 'It looks really crashy, so I disabled it. ' \
                        'Sorry about that.\n'
                else:
                    initial_state = phonetest.PhoneTestMessage.DISCONNECTED
                worker.start(initial_state)
                self.logger.info('Sending notification...')
                try:
                    self.mailer.send(msg_subj, msg_body)
                    self.logger.info('Sent.')
                except socket.error:
                    self.logger.exception('Failed to send dead-phone notification.')

    def worker_msg_loop(self):
        try:
            while not self._stop:
                self.check_for_dead_workers()
                try:
                    msg = self.worker_msg_queue.get(timeout=5)
                except Queue.Empty:
                    continue
                except IOError, e:
                    if e.errno == errno.EINTR:
                        continue
                self.phone_workers[msg.phoneid].process_msg(msg)
        except KeyboardInterrupt:
            self.stop()

    # Start the phones for testing
    def new_job(self, build_url, devices=None):
        self.worker_lock.acquire()
        try:
            for p in self.phone_workers.values():
                phoneid = p.phone_cfg['phoneid']
                if devices and phoneid not in devices:
                    continue
                abi = p.phone_cfg['abi']
                incompatible_job = False
                if abi == 'x86':
                    if 'x86' not in build_url:
                        incompatible_job = True
                elif abi == 'armeabi-v6':
                    if 'armv6' in build_url:
                        incompatible_job = True
                else:
                    if 'x86' in build_url or 'armv6' in build_url:
                        incompatible_job = True
                if incompatible_job:
                    self.logger.debug('Ignoring incompatible job %s '
                                      'for phone %s abi %s' %
                                      (build_url, phoneid, abi))
                    continue
                self.jobs.new_job(build_url, phoneid)
                self.logger.info('Notifying device %s of new job %s.' %
                                 (phoneid, build_url))
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
                response += 'phone %s (%s):\n' % (i, w.phone_cfg['serial'])
                response += '  debug level %d\n' % w.user_cfg.get('debug', 3)
                if not w.last_status_msg:
                    response += '  no updates\n'
                else:
                    if w.last_status_msg.current_build:
                        response += '  current build: %s %s\n' % (
                            datetime.datetime.fromtimestamp(float(w.last_status_msg.current_build)),
                            w.last_status_msg.current_repo)
                    else:
                        response += '  no build loaded\n'
                    response += '  last update %s ago:\n    %s\n' % (now - w.last_status_msg.timestamp, w.last_status_msg.short_desc())
                    response += '  %s for %s\n' % (w.last_status_msg.status, now - w.first_status_of_type.timestamp)
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
                    worker.phone_cfg['serial'] == phoneid or
                    worker.phone_cfg['phoneid'] == phoneid):
                    f = getattr(worker, cmd)
                    if params:
                        f(params)
                    else:
                        f()
                    response = 'ok'
        else:
            response = 'Unknown command "%s"\n' % cmd
        return response

    def create_worker(self, phone_cfg, user_cfg):
        phoneid = phone_cfg['phoneid']
        self.logger.info('Creating worker for %s: %s, %s.' % (phoneid, phone_cfg, user_cfg))
        tests = []
        for test_class, config_file, enable_unittests, test_devices_repos in self._tests:
            skip_test = True
            if not test_devices_repos:
                # There is no restriction on this test being run by
                # specific devices.
                skip_test = False
            elif phoneid in test_devices_repos:
                # This test is to be run by this device on test_repos
                skip_test = False
            if not skip_test:
                tests.append(test_class(phone_cfg=phone_cfg,
                                        user_cfg=user_cfg,
                                        config_file=config_file,
                                        enable_unittests=enable_unittests,
                                        test_devices_repos=test_devices_repos))
        if not tests:
                self.logger.warning('Not creating worker: No tests defined for '
                                    'worker for %s: %s, %s.' %
                                    (phoneid, phone_cfg, user_cfg))
                return
        logfile_prefix = os.path.splitext(self.options[LOGFILE])[0]
        worker = PhoneWorker(self.next_worker_num, self.options[IPADDR],
                             tests, phone_cfg, user_cfg,
                             self.worker_msg_queue,
                             '%s-%s' % (logfile_prefix, phoneid),
                             self.loglevel, self.mailer,
                             self.options[BUILD_CACHE_PORT])
        self.phone_workers[phoneid] = worker
        worker.start()

    def get_user_cfg(self):
        user_cfg = {}
        for option_name in INI_OPTION_NAMES:
            user_cfg[option_name] = self.options[option_name]
        return user_cfg

    def register_cmd(self, data):
        try:
            # Map MAC Address to ip and user name for phone
            # The configparser does odd things with the :'s so remove them.
            phoneid = data['device_name']
            phone_cfg = dict(
                phoneid=phoneid,
                serial=data['serialno'],
                machinetype=data['hardware'],
                osver=data['osver'],
                abi=data['abi'],
                ipaddr=self.options[IPADDR]) # XXX IPADDR no longer needed?
            if self.logger.getEffectiveLevel() == logging.DEBUG:
                self.logger.debug('register_cmd: phone_cfg: %s' % phone_cfg)
            if phoneid in self.phone_workers:
                self.logger.debug('Received registration message for known phone '
                                  '%s.' % phoneid)
                worker = self.phone_workers[phoneid]
                if worker.phone_cfg != phone_cfg:
                    # This won't update the subprocess, but it will allow
                    # us to write out the updated values right away.
                    worker.phone_cfg = phone_cfg
                    self.logger.info('Registration info has changed; restarting '
                                     'worker.')
                    if phoneid in self.restart_workers:
                        self.logger.info('Phone worker is already scheduled to be '
                                     'restarted!')
                    else:
                        self.restart_workers[phoneid] = phone_cfg
                        worker.stop()
            else:
                user_cfg = self.get_user_cfg()
                user_cfg['debug'] = 3
                self.create_worker(phone_cfg, user_cfg)
                self.logger.info('Registered phone %s.' % phone_cfg['phoneid'])
        except:
            self.logger.exception('register_cmd:')
            self.stop()

    def read_devices(self):
        cfg = ConfigParser.RawConfigParser()
        cfg.read(self.options['devicescfg'])

        for device_name in cfg.sections():
            # failure for a device to have a serialno option is fatal.
            serialno = cfg.get(device_name, 'serialno')
            self.logger.info("Initializing device name=%s, serialno=%s" % (device_name, serialno))
            self.console_logger.info("Initializing device name=%s, serialno=%s" % (device_name, serialno))
            # XXX: We should be able to continue if a device isn't available immediately
            # or to add/remove devices but this will work for now.
            dm = ADBDevice(device_serial=serialno,
                           log_level=self.loglevel,
                           logger_name='autophone.adb')
            dm.power_on()
            device = {"device_name": device_name,
                      "serialno": serialno,
                      "dm" : dm}
            device['osver'] = dm.get_prop('ro.build.version.release')
            device['hardware'] = dm.get_prop('ro.product.model')
            device['abi'] = dm.get_prop('ro.product.cpu.abi')
            self._devices[serialno] = device
            self.register_cmd(device)


    def read_tests(self):
        self._tests = []
        manifest = TestManifest()
        manifest.read(self.options[TEST_PATH])
        tests_info = manifest.get()
        for t in tests_info:
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
                    issubclass(member_value, phonetest.PhoneTest)):
                    config = os.path.join(t['here'],
                                          t.get('config', ''))
                    # The config file can contain additional options
                    # for each test:
                    # unittests = 1  determines if the tests zip file should
                    # be downloaded along with the build.
                    # <device> = <repo-list> determines the devices
                    # which should run the test. If no devices are listed,
                    # then all devices will run the test.
                    enable_unittests = bool(t.get('unittests', False))

                    devices = [device for device in t if device not in
                               ('name', 'here', 'manifest', 'path', 'config',
                                'relpath', 'unittest', 'subsuite')]
                    test_devices_repos = {}
                    for device in devices:
                        test_devices_repos[device] = t[device].split()

                    tests.append((member_value, config, enable_unittests, test_devices_repos))

            self._tests.extend(tests)


    def trigger_jobs(self, data):
        self.logger.info('Received user-specified job: %s' % data)
        args = data.split(' ')
        if not args:
            return 'invalid args'
        self.new_job(args[0], args[1:])
        return 'ok'

    def reset_phones(self):
        self.logger.info('Resetting phones...')
        for phoneid, phone in self.phone_workers.iteritems():
            phone.reboot()

    def on_build(self, msg):
        if self._stop:
            return
        # Use the msg to get the build and install it then kick off our tests
        self.logger.debug('---------- BUILD FOUND ----------')
        self.logger.debug('%s' % msg)
        self.logger.debug('---------------------------------')

        # We will get a msg on busted builds with no URLs, so just ignore
        # those, and only run the ones with real URLs
        # We create jobs for all the phones and push them into the queue
        if 'buildurl' in msg:
            self.new_job(msg['buildurl'])

    def stop(self):
        self._stop = True
        for p in self.phone_workers.values():
            p.stop()
        if self.server:
            self.server.shutdown()
        if self.server_thread:
            self.server_thread.join()

def load_autophone_options(cmd_options):
    options = {"devicescfg" : cmd_options.devicescfg}

    if not cmd_options.repos:
        cmd_options.repos = ['mozilla-central']

    if not cmd_options.buildtypes:
        cmd_options.buildtypes = ['opt']

    if cmd_options.autophonecfg:
        cfg = ConfigParser.RawConfigParser()
        cfg.read(cmd_options.autophonecfg)

        for setting_name in CMD_OPTION_NAMES:
            try:
                getter = getattr(ConfigParser.RawConfigParser,
                                 CMD_OPTION_NAMES[setting_name])
                value = getter(cfg, 'settings', setting_name)
                if setting_name == REPOS:
                    value = value.split()
                elif setting_name == BUILDTYPES:
                    value = value.split()
                options[setting_name] = value
            except ConfigParser.NoOptionError:
                pass
        for setting_name in INI_OPTION_NAMES:
            try:
                getter = getattr(ConfigParser.RawConfigParser,
                                 INI_OPTION_NAMES[setting_name])
                value = getter(cfg, 'settings', setting_name)
                options[setting_name] = value
            except ConfigParser.NoOptionError:
                pass

    for setting_name in CMD_OPTION_NAMES:
        if setting_name not in options:
            options[setting_name] = getattr(cmd_options, setting_name)

    # Force defaults if they were not explicitly set.
    def set_value(o, p, v):
        if p not in o:
            o[p] = v

    set_value(options, BUILD_CACHE_SIZE,
              builds.BuildCache.MAX_NUM_BUILDS)
    set_value(options, BUILD_CACHE_EXPIRES,
              builds.BuildCache.EXPIRE_AFTER_DAYS)
    set_value(options, DEVICE_READY_RETRY_WAIT,
              PhoneWorker.DEVICE_READY_RETRY_WAIT)
    set_value(options, DEVICE_READY_RETRY_ATTEMPTS,
              PhoneWorker.DEVICE_READY_RETRY_ATTEMPTS)
    set_value(options, DEVICE_BATTERY_MIN,
              PhoneWorker.DEVICE_BATTERY_MIN)
    set_value(options, DEVICE_BATTERY_MAX,
              PhoneWorker.DEVICE_BATTERY_MAX)
    set_value(options, PHONE_RETRY_LIMIT,
              PhoneWorker.PHONE_RETRY_LIMIT)
    set_value(options, PHONE_RETRY_WAIT,
              PhoneWorker.PHONE_RETRY_WAIT)
    set_value(options, PHONE_MAX_REBOOTS,
              PhoneWorker.PHONE_MAX_REBOOTS)
    set_value(options, PHONE_PING_INTERVAL,
              PhoneWorker.PHONE_PING_INTERVAL)
    set_value(options, PHONE_COMMAND_QUEUE_TIMEOUT,
              PhoneWorker.PHONE_COMMAND_QUEUE_TIMEOUT)
    set_value(options, PHONE_CRASH_WINDOW,
              Crashes.CRASH_WINDOW)
    set_value(options, PHONE_CRASH_LIMIT,
              Crashes.CRASH_LIMIT)

    return options


def main(options):

    def sigterm_handler(signum, frame):
        autophone.stop()

    loglevel = e = None
    try:
        loglevel = getattr(logging, options[LOGLEVEL])
    except AttributeError, e:
        pass
    finally:
        if e or logging.getLevelName(loglevel) != options[LOGLEVEL]:
            print 'Invalid log level %s' % options[LOGLEVEL]
            return errno.EINVAL

    logger = logging.getLogger('autophone')
    logger.propagate = False
    logger.setLevel(loglevel)

    filehandler = MultiprocessingTimedRotatingFileHandler(options[LOGFILE],
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

    console_logger.info('Starting server on port %d.' % options[PORT])
    console_logger.info('Starting build-cache server on port %d.' %
                        options[BUILD_CACHE_PORT])
    product = 'fennec'
    build_platforms = ['android', 'android-armv6', 'android-x86']
    buildfile_ext = '.apk'
    try:
        build_cache = builds.BuildCache(
            options[REPOS],
            options[BUILDTYPES],
            product,
            build_platforms,
            buildfile_ext,
            cache_dir=options[CACHE_DIR],
            override_build_dir=options[OVERRIDE_BUILD_DIR],
            enable_unittests=options[ENABLE_UNITTESTS],
            build_cache_size=options[BUILD_CACHE_SIZE],
            build_cache_expires=options[BUILD_CACHE_EXPIRES])
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
        ('127.0.0.1', options[BUILD_CACHE_PORT]),
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
                      dest='loglevel', default='DEBUG',
                      help='Log level - ERROR, WARNING, DEBUG, or INFO, '
                      'defaults to DEBUG')
    parser.add_option('-t', '--test-path', action='store', type='string',
                      dest='test_path', default='tests/manifest.ini',
                      help='path to test manifest')
    parser.add_option('--emailcfg', action='store', type='string',
                      dest='emailcfg', default='email.ini',
                      help='config file for email settings; defaults to email.ini')
    parser.add_option('--disable-pulse', action='store_false',
                      dest="enable_pulse", default=True,
                      help="Disable connecting to pulse to look for new builds")
    parser.add_option('--enable-unittests', action='store_true',
                      dest='enable_unittests', default=False,
                      help='Enable running unittests by downloading and installing '
                      'the unittests package for each build')
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
                      help='The repos to test. '
                      'One of mozilla-central, mozilla-inbound, mozilla-aurora, '
                      'mozilla-beta, fx-team, b2g-inbound. To specify multiple '
                      'repos, specify them with additional --repo options. '
                      'Defaults to mozilla-central.')
    parser.add_option('--buildtype',
                      dest='buildtypes',
                      action='append',
                      help='The build types to test. '
                      'One of opt or debug. To specify multiple build types, '
                      'specify them with additional --buildtype options. '
                      'Defaults to opt.')
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

    (cmd_options, args) = parser.parse_args()
    options = load_autophone_options(cmd_options)

    exit_code = main(options)

    sys.exit(exit_code)
