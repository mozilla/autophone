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
import math
import multiprocessing
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import traceback
import urlparse
import zipfile

try:
    import json
except ImportError:
    import simplejson

import androidutils
import builds
import phonetest

from manifestparser import TestManifest

from pulsebuildmonitor import start_pulse_monitor

from devicemanager import DMError, NetworkTools
from devicemanagerSUT import DeviceManagerSUT
from sendemail import sendemail


class Mailer(object):

    def __init__(self, cfgfile):
        self.cfgfile = cfgfile

    def send(self, subject, body):
        cfg = ConfigParser.ConfigParser()
        cfg.read(self.cfgfile)
        try:
            from_address = cfg.get('report', 'from')
        except (ConfigParser.NoSectionError, ConfigParser.NoOptionError):
            logging.error('No "from" option defined in "report" section of file "%s".\n' % options.config_file)
            return

        try:
            mail_dest = [x.strip() for x in cfg.get('email', 'dest').split(',')]
        except (ConfigParser.NoSectionError, ConfigParser.NoOptionError):
            mail_dest = []

        try:
            mail_username = cfg.get('email', 'username')
        except (ConfigParser.NoSectionError, ConfigParser.NoOptionError):
            mail_username = None

        try:
            mail_password = cfg.get('email', 'password')
        except (ConfigParser.NoSectionError, ConfigParser.NoOptionError):
            mail_password = None

        try:
            mail_server = cfg.get('email', 'server')
        except (ConfigParser.NoSectionError, ConfigParser.NoOptionError):
            mail_server = 'mail.mozilla.com'

        try:
            mail_port = cfg.getint('email', 'port')
        except (ConfigParser.NoSectionError, ConfigParser.NoOptionError):
            mail_port = 465

        try:
            mail_ssl = cfg.getboolean('email', 'ssl')
        except (ConfigParser.NoSectionError, ConfigParser.NoOptionError):
            mail_ssl = True

        sendemail(from_addr=from_address, to_addrs=mail_dest, subject=subject,
                  username=mail_username, password=mail_password,
                  text_data=body, server=mail_server, port=mail_port,
                  use_ssl=mail_ssl)


class PhoneWorker(object):

    """Runs tests on a single phone in a separate process.

    FIXME: This class represents both the interface to the subprocess and
    the subprocess itself. It would be best to split those so we can easily
    tell what can and cannot be accessed from the main process.

    FIXME: Would be nice to have test results uploaded outside of the
    test objects, and to have them queued (and cached) if the results
    server is unavailable for some reason.  Might be best to communicate
    this back to the main AutoPhone process.
    """

    MAX_REBOOT_WAIT_SECONDS = 300
    MAX_REBOOT_ATTEMPTS = 3
    PING_SECONDS = 60*15

    def __init__(self, worker_num, tests, phone_cfg, callback_ipaddr,
                 autophone_queue, main_logfile, loglevel, mailer):
        self.worker_num = worker_num
        self.tests = tests
        self.phone_cfg = phone_cfg
        self.callback_ipaddr = callback_ipaddr
        self.autophone_queue = autophone_queue
        logfile_prefix, logfile_ext = os.path.splitext(main_logfile)
        self.logfile = '%s-%s%s' % (logfile_prefix, phone_cfg['phoneid'],
                                    logfile_ext)
        self.loglevel = loglevel
        self.mailer = mailer
        self.job_queue = multiprocessing.Queue()
        self.stopped = False
        self.lock = multiprocessing.Lock()
        self.p = None
        self.disabled = False
        self.skipped_job_queue = []
        self.current_build = None
        self.last_status_msg = None
        self.first_status_of_type = None
        self.last_status_of_previous_type = None

    def process_msg(self, msg):
        if not self.last_status_msg or msg.status != self.last_status_msg.status:
            self.last_status_of_previous_type = self.last_status_msg
            self.first_status_of_type = msg
        self.last_status_msg = msg
        logging.info(msg)

    def add_job(self, job):
        self.job_queue.put_nowait(('job', job))

    def reboot(self):
        self.job_queue.put_nowait(('reboot', None))

    def start(self):
        if self.p:
            return
        self.p = multiprocessing.Process(target=self.loop)
        self.p.start()

    def stop(self):
        self.lock.acquire()
        self.stopped = True
        self.lock.release()
        self.job_queue.put_nowait(None)
        self.p.join()

    def disable(self):
        self.lock.acquire()
        self.job_queue.put_nowait(('disable', None))
        self.lock.release()

    def reenable(self):
        self.lock.acquire()
        self.job_queue.put_nowait(('reenable', None))
        self.lock.release()

    def is_disabled(self):
        self.lock.acquire()
        disabled = self.disabled
        self.lock.release()
        return disabled

    def should_stop(self):
        self.lock.acquire()
        stopped = self.stopped
        self.lock.release()
        return stopped

    def status_update(self, msg):
        try:
            self.autophone_queue.put_nowait(msg)
        except Queue.Full:
            logging.warn('Autophone queue is full!')

    def check_sdcard(self, dm=None):
        if not dm:
            dm = DeviceManagerSUT(self.phone_cfg['ip'],
                                  self.phone_cfg['sutcmdport'])
        dev_root = dm.getDeviceRoot()
        if dev_root is None:
            logging.error('no response from device when querying device root')
            return False
        d = dev_root + '/autophonetest'
        androidutils.run_adb('shell', ['rmdir', d], serial=self.phone_cfg['serial'], timeout=15)
        out = androidutils.run_adb('shell', ['mkdir', d], serial=self.phone_cfg['serial'], timeout=15)
        if not out:
            # Sometimes we don't get an error creating the dir, but we do
            # when changing to it. Blah.
            out = androidutils.run_adb('shell', ['cd', d], serial=self.phone_cfg['serial'], timeout=15)            
        if out:
            logging.error('device root is not writable!')
            logging.error(out)
            logging.info('checking sdcard...')
            out = androidutils.run_adb('shell', ['mkdir', '/mnt/sdcard/tests/autophonetest'], serial=self.phone_cfg['serial'], timeout=15)
            if out:
                logging.error(out)
            else:
                logging.error('weird, sd card is writable but device root isn\'t! I\'m confused and giving up anyway!')
            self.clear_test_base_paths()
            return False
        androidutils.run_adb('shell', ['rmdir', d], serial=self.phone_cfg['serial'], timeout=15)
        return True

    def clear_test_base_paths(self):
        for t in self.tests:
            t._base_device_path = ''

    def recover_phone(self):
        reboots = 0
        while not self.disabled:
            if reboots < self.MAX_REBOOT_ATTEMPTS:
                logging.info('Rebooting phone...')
                phone_is_up = False
                reboots += 1
                try:
                    androidutils.reboot_adb(self.phone_cfg['serial'])
                except androidutils.AndroidError:
                    logging.error('Could not reboot phone.')
                    self.disable_phone('Could not reboot phone via adb.')
                    return
                time.sleep(10)
                max_time = datetime.datetime.now() + \
                    datetime.timedelta(seconds=self.MAX_REBOOT_WAIT_SECONDS)
                while datetime.datetime.now() <= max_time:
                    dm = DeviceManagerSUT(self.phone_cfg['ip'],
                                          self.phone_cfg['sutcmdport'])
                    if dm._sock:
                        logging.info('Phone is back up.')
                        phone_is_up = True
                        break
                    time.sleep(5)
                if phone_is_up:
                    if self.check_sdcard():
                        return
                else:
                    logging.info('Phone did not come back up within %d seconds.' %
                                 self.MAX_REBOOT_WAIT_SECONDS)
            else:
                logging.info('Phone has been rebooted %d times; giving up.' %
                             reboots)
                self.disable_phone('Phone was rebooted %d times.' % reboots)

    def disable_phone(self, msg_body):
        self.disabled = True
        if msg_body:
            try:
                self.mailer.send('Phone %s disabled' % self.phone_cfg['phoneid'],
                                 '''Hello, this is AutoPhone. Phone %s has been disabled:

%s

We gave up on it. Sorry about that.
''' % (self.phone_cfg['phoneid'], msg_body))
            except socket.error:
                logging.error('Failed to send disabled-phone notification.')
                logging.info(traceback.format_exc())
        self.status_update(phonetest.PhoneTestMessage(
                self.phone_cfg['phoneid'],
                phonetest.PhoneTestMessage.DISABLED))
        
    def retry_func(self, error_str, func, args, kwargs):
        """Retries a function up to three times.
        Note that each attempt may reboot the phone up to three times if it
        comes up in a bad state. This loop is to catch errors caused by the
        test or help functions like androidutils.install_adb_build().
        """
        attempts = 0
        android_errors = []
        while not self.disabled and attempts < 3:
            attempts += 1
            # blech I don't like bare try/except clauses, but we
            # want to track down if/why phone processes are
            # suddenly exiting.
            try:
                return func(*args, **kwargs)
            except Exception, e:
                if isinstance(e, androidutils.AndroidError):
                    android_errors.append(str(e))
                logging.info(error_str)
                logging.info(traceback.format_exc())
                if attempts < 2:
                    self.recover_phone()
            else:
                return
            if attempts == 2:
                logging.warn('Failed to run test three times; giving up on it.')
                if len(android_errors) == 2:
                    logging.warn('Phone experienced three android errors in a row; giving up.')
                    self.disable_phone(
'''Phone experienced two android errors in a row:
%s''' % '\n'.join(['* %s' % x for x in android_errors]))

    def ping(self):
        logging.info('Pinging phone')
        # verify that the phone is still responding
        response = androidutils.run_adb('shell',
                                        ['echo', 'autophone'],
                                        self.phone_cfg['serial'])
        if response:
            logging.info('Pong!')
            return True

        logging.info('No response!')
        return False

    def loop(self):
        for h in logging.getLogger().handlers:
            logging.getLogger().removeHandler(h)
        logging.basicConfig(filename=self.logfile,
                            filemode='a',
                            level=self.loglevel,
                            format='%(asctime)s|%(levelname)s|%(message)s')

        for t in self.tests:
            t.status_cb = self.status_update

        self.status_update(phonetest.PhoneTestMessage(
                self.phone_cfg['phoneid'],
                phonetest.PhoneTestMessage.IDLE))

        last_ping = None

        while True:
            request = None
            try:
                request = self.job_queue.get(timeout=10)
            except Queue.Empty:
                if (not last_ping or
                    ((datetime.datetime.now() - last_ping) >
                     datetime.timedelta(
                            microseconds=1000*1000*self.PING_SECONDS))):
                    last_ping = datetime.datetime.now()
                    if not self.disabled:
                        if self.ping():
                            self.status_update(phonetest.PhoneTestMessage(
                                    self.phone_cfg['phoneid'],
                                    phonetest.PhoneTestMessage.IDLE,
                                    self.current_build))
                        else:
                            logging.info('No response!')
                            self.recover_phone()
                            # try pinging again, since, if the phone is
                            # physically disconnected but the agent is running,
                            # the reboot will appear to succeed even though we
                            # still can't access it through adb.
                            if not self.ping():
                                self.disable_phone('No response to ping via adb.')
            except KeyboardInterrupt:
                return
            if self.should_stop():
                return
            if not request:
                continue
            if request[0] == 'job':
                job = request[1]
                if not job:
                    continue
                logging.info('Got job.')
                if not self.disabled and not self.check_sdcard():
                    self.recover_phone()
                if self.disabled:
                    logging.info('Phone is disabled; queuing job for later.')
                    self.skipped_job_queue.append(job)
                    continue
                self.status_update(phonetest.PhoneTestMessage(
                        self.phone_cfg['phoneid'],
                        phonetest.PhoneTestMessage.INSTALLING, job['blddate']))
                logging.info('Installing build %s.' % datetime.datetime.fromtimestamp(float(job['blddate'])))
                
                if not self.retry_func('Exception installing build',
                                       androidutils.install_build_adb, [],
                                       dict(phoneid=self.phone_cfg['phoneid'],
                                            apkpath=job['apkpath'],
                                            blddate=job['blddate'],
                                            procname=job['androidprocname'],
                                            serial=self.phone_cfg['serial'])):
                    logging.error('Failed to install build!')
                    continue

                self.current_build = job['blddate']
                logging.info('Running tests...')
                for t in self.tests:
                    if self.disabled:
                        break
                    t.current_build = job['blddate']
                    # TODO: Attempt to see if pausing between jobs helps with
                    # our reconnection issues
                    time.sleep(30)
                    self.retry_func('Exception running test %s.' %
                                    t.__class__.__name__, t.runjob, [job], {})
                    if self.should_stop():
                        return

                if self.disabled:
                    self.status_update(phonetest.PhoneTestMessage(
                            self.phone_cfg['phoneid'],
                            phonetest.PhoneTestMessage.DISABLED))
                else:
                    logging.info('Job completed.')
                    self.status_update(phonetest.PhoneTestMessage(
                            self.phone_cfg['phoneid'],
                            phonetest.PhoneTestMessage.IDLE,
                            self.current_build))
            elif request[0] == 'reboot':
                self.status_update(phonetest.PhoneTestMessage(
                        self.phone_cfg['phoneid'],
                        phonetest.PhoneTestMessage.REBOOTING))
                self.recover_phone()
                self.status_update(phonetest.PhoneTestMessage(
                        self.phone_cfg['phoneid'],
                        phonetest.PhoneTestMessage.IDLE, msg='phone reset'))
            elif request[0] == 'disable':
                self.disable_phone(None)
            elif request[0] == 'reenable':
                if self.disabled:
                    self.disabled = False
                    last_ping = None
                for j in self.skipped_job_queue:
                    self.job_queue.put(('job', j))


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
                        self.request.close()
                        return
                    response = self.server.cmd_cb(line)
                    self.request.send(response + '\n')

    def __init__(self, is_restarting, reboot_phones, test_path, cachefile,
                 ipaddr, port, logfile, loglevel, emailcfg):
        self._test_path = test_path
        self._cache = cachefile
        if ipaddr:
            self.ipaddr = ipaddr
        else:
            nt = NetworkTools()
            self.ipaddr = nt.getLanIp()
            logging.info('IP address for phone callbacks not provided; using '
                         '%s.' % self.ipaddr)
        self.port = port
        self.logfile = logfile
        self.loglevel = loglevel
        self.mailer = Mailer(emailcfg)
        self.build_cache = builds.BuildCache()
        self._stop = False
        self.phone_workers = {}  # indexed by mac address
        self.worker_lock = threading.Lock()
        self.cmd_lock = threading.Lock()
        self._tests = []
        logging.info('Starting autophone.')
        
        # queue for listening to status updates from tests
        self.worker_msg_queue = multiprocessing.Queue()

        self.read_tests()

        if not os.path.exists(self._cache):
            # If we don't have a cache you aren't restarting
            is_restarting = False
            open(self._cache, 'wb')
        elif not is_restarting:
            # If we have a cache and we are NOT restarting, then assume that
            # cache is invalid. Blow it away and recreate it
            os.remove(self._cache)
            open(self._cache, 'wb')

        if is_restarting:
            self.read_cache()
            if reboot_phones:
                self.reset_phones()

        self.server = None
        self.server_thread = None

        self.pulsemonitor = start_pulse_monitor(buildCallback=self.on_build,
                                                trees=['mozilla-central'],
                                                platforms=['android'],
                                                buildtypes=['opt'],
                                                logger=logging.getLogger())

    def run(self):
        self.server = self.CmdTCPServer(('0.0.0.0', self.port),
                                        self.CmdTCPHandler)
        self.server.cmd_cb = self.route_cmd
        self.server_thread = threading.Thread(target=self.server.serve_forever)
        self.server_thread.start()
        self.worker_msg_loop()

    def worker_msg_loop(self):
        # FIXME: look up worker by msg.phoneid and have worker process
        # message. worker, as part of the main process, can log status
        # and store it for later querying.
        # also, store first instance of current status (e.g. idle for 30
        # minutes, last update 1 minute ago). store test name and start time
        # if status is WORKING. All this will help us determine if and where
        # a phone/worker process is stuck.
        try:
            while not self._stop:
                try:
                    msg = self.worker_msg_queue.get(timeout=5)
                except Queue.Empty:
                    continue
                self.phone_workers[msg.phoneid].process_msg(msg)
        except KeyboardInterrupt:
            self.stop()

    # This runs the tests and resets the self._lasttest variable.
    # It also can install a new build, to install, set build_url to the URL of the
    # build to download and install
    def disperse_jobs(self):
        try:
            logging.debug('Asking for jobs')
            while not self._jobqueue.empty():
                job = self._jobqueue.get()
                logging.debug('Got job: %s' % job)
                for k,v in self._phonemap.iteritems():
                    # TODO: Refactor so that the job can specify the test so that
                    # then multiple types of test objects can be ran on one set of
                    # phones.
                    logging.debug('Adding job to phone: %s' % v['name'])
                    ### FIXME: Need to serialize tests on each phone...
                    for t in v['testobjs']:
                        t.add_job(job)
                self._jobqueue.task_done()
        except:
            logging.error('Exception adding jobs: %s %s' % sys.exc_info()[:2])

    # Start the phones for testing
    def start_tests(self, job):
        self.worker_lock.acquire()
        for p in self.phone_workers.values():
            logging.info('Starting job on phone: %s' % p.phone_cfg['phoneid'])
            p.add_job(job)
        self.worker_lock.release()

    def route_cmd(self, data):
        self.cmd_lock.acquire()
        data = data.strip()
        cmd, space, params = data.partition(' ')
        cmd = cmd.lower()
        response = 'ok'

        if cmd == 'stop':
            self.stop()
        elif cmd == 'log':
            logging.info(params)
        elif cmd == 'triggerjobs':
            self.trigger_jobs(params)
        elif cmd == 'register':
            self.register_cmd(params)
        elif cmd == 'status':
            response = ''
            now = datetime.datetime.now().replace(microsecond=0)
            for i, w in self.phone_workers.iteritems():
                response += 'phone %s (%s):\n' % (i, w.phone_cfg['ip'])
                if not w.last_status_msg:
                    response += '  no updates\n'
                else:
                    if w.last_status_msg.current_build:
                        response += '  current build: %s\n' % datetime.datetime.fromtimestamp(float(w.last_status_msg.current_build))
                    else:
                        response += '  no build loaded\n'
                    response += '  last update %s ago:\n    %s\n' % (now - w.last_status_msg.timestamp, w.last_status_msg.short_desc())
                    response += '  %s for %s\n' % (w.last_status_msg.status, now - w.first_status_of_type.timestamp)
                    if w.last_status_of_previous_type:
                        response += '  previous state %s ago:\n    %s\n' % (now - w.last_status_of_previous_type.timestamp, w.last_status_of_previous_type.short_desc())
            response += 'ok'
        elif cmd == 'disable' or cmd == 'reenable':
            serial = params.strip()
            worker = None
            for w in self.phone_workers.values():
                if w.phone_cfg['serial'] == serial:
                    worker = w
                    break
            if worker:
                getattr(worker, cmd)()
                response = 'ok'
            else:
                response = 'error: phone not found'
        else:
            response = 'Unknown command "%s"\n' % cmd
        self.cmd_lock.release()
        return response

    def register_phone(self, phone_cfg):
        tests = [x[0](phone_cfg=phone_cfg, config_file=x[1]) for
                 x in self._tests]

        worker = PhoneWorker(len(self.phone_workers.keys()), tests, phone_cfg,
                             self.ipaddr, self.worker_msg_queue, self.logfile,
                             self.loglevel, self.mailer)
        self.phone_workers[phone_cfg['phoneid']] = worker
        worker.start()
        logging.info('Registered phone %s.' % phone_cfg['phoneid'])

    def register_cmd(self, data):
        # Un-url encode it
        data = urlparse.parse_qs(data.lower())

        try:
            # Map MAC Address to ip and user name for phone
            # The configparser does odd things with the :'s so remove them.
            macaddr = data['name'][0].replace(':', '_')
            phoneid = '%s_%s' % (macaddr, data['hardware'][0])

            if phoneid not in self.phone_workers:
                phone_cfg = dict(
                    phoneid=phoneid,
                    serial=data['pool'][0].upper(),
                    ip=data['ipaddr'][0],
                    sutcmdport=data['cmdport'][0],
                    machinetype=data['hardware'][0],
                    osver=data['os'][0])
                self.register_phone(phone_cfg)
                self.update_phone_cache()
            else:
                logging.debug('Registering known phone: %s' %
                              self.phone_workers[phoneid].phone_cfg['phoneid'])
        except:
            print 'ERROR: could not write cache file, exiting'
            traceback.print_exception(*sys.exc_info())
            self.stop()

    def read_cache(self):
        self.phone_workers.clear()
        f = file(self._cache, 'r')
        try:
            cache = json.loads(f.read())
        except ValueError:
            cache = {}
        f.close()

        for phone_cfg in cache.get('phones', []):
            self.register_phone(phone_cfg)

    def update_phone_cache(self):
        f = file(self._cache, 'r')
        try:
            cache = json.loads(f.read())
        except ValueError:
            cache = {}

        cache['phones'] = [x.phone_cfg for x in self.phone_workers.values()]
        f = file(self._cache, 'w')
        f.write(json.dumps(cache))
        f.close()

    def read_tests(self):
        self._tests = []
        manifest = TestManifest()
        manifest.read(self._test_path)
        tests_info = manifest.get()
        for t in tests_info:
            if not t['here'] in sys.path:
                sys.path.append(t['here'])
            if t['name'].endswith('.py'):
                t['name'] = t['name'][:-3]
            # add all classes in module that are derived from PhoneTest to
            # the test list
            tests = [(x[1], os.path.normpath(os.path.join(t['here'],
                                                          t.get('config', ''))))
                     for x in inspect.getmembers(__import__(t['name']),
                                                 inspect.isclass)
                     if x[0] != 'PhoneTest' and issubclass(x[1],
                                                           phonetest.PhoneTest)]
            self._tests.extend(tests)

    def trigger_jobs(self, data):
        job = self.build_job(self.get_build(data))
        logging.info('Adding user-specified job: %s' % job)
        self.start_tests(job)

    def reset_phones(self):
        logging.info('Restting phones...')
        for phoneid, phone in self.phone_workers.iteritems():
            phone.reboot()

    def on_build(self, msg):
        # Use the msg to get the build and install it then kick off our tests
        logging.debug('---------- BUILD FOUND ----------')
        logging.debug('%s' % msg)
        logging.debug('---------------------------------')

        # We will get a msg on busted builds with no URLs, so just ignore
        # those, and only run the ones with real URLs
        # We create jobs for all the phones and push them into the queue
        if 'buildurl' in msg:
            self.start_tests(self.build_job(self.get_build(msg['buildurl'])))

    def get_build(self, url_or_path):
        cmps = urlparse.urlparse(url_or_path)
        if not cmps.scheme or cmps.scheme == 'file':
            return cmps.path
        apkpath = self.build_cache.get(url_or_path)
        try:
            z = zipfile.ZipFile(apkpath)
            z.testzip()
        except zipfile.BadZipfile:
            logging.warn('%s is a bad apk; redownloading...' % apkpath)
            apkpath = self.build_cache.get(url_or_path, force=True)
        return apkpath

    def build_job(self, apkpath):
        tmpdir = tempfile.mkdtemp()
        try:
            apkfile = zipfile.ZipFile(apkpath)
            apkfile.extract('application.ini', tmpdir)
        except zipfile.BadZipfile:
            # we should have already tried to redownload bad zips, so treat
            # this as fatal.
            logging.error('%s is a bad apk; aborting job.' % apkpath)
            shutil.rmtree(tmpdir)    
            return None
        cfg = ConfigParser.RawConfigParser()
        cfg.read(os.path.join(tmpdir, 'application.ini'))
        rev = cfg.get('App', 'SourceStamp')
        ver = cfg.get('App', 'Version')
        repo = cfg.get('App', 'SourceRepository')
        blddate = datetime.datetime.strptime(cfg.get('App', 'BuildID'),
                                             '%Y%m%d%H%M%S')
        procname = ''
        if (repo == 'http://hg.mozilla.org/mozilla-central' or
            repo == 'http://hg.mozilla.org/integration/mozilla-inbound'):
            procname = 'org.mozilla.fennec'
        elif repo == 'http://hg.mozilla.org/releases/mozilla-aurora':
            procname = 'org.mozilla.fennec_aurora'
        elif repo == 'http://hg.mozilla.org/releases/mozilla-beta':
            procname = 'org.mozilla.firefox'

        job = { 'apkpath': apkpath,
                'blddate': math.trunc(time.mktime(blddate.timetuple())),
                'revision': rev,
                'androidprocname': procname,
                'version': ver,
                'bldtype': 'opt' }
        shutil.rmtree(tmpdir)    
        return job

    def stop(self):
        self._stop = True
        self.server.shutdown()
        for p in self.phone_workers.values():
            p.stop()
        self.server_thread.join()


def main(is_restarting, reboot_phones, test_path, cachefile, ipaddr, port,
         logfile, loglevel_name, emailcfg):

    adb_check = androidutils.check_for_adb()
    if adb_check != 0:
        print 'Could not execute adb: %s.' % os.strerror(adb_check)
        print 'Ensure that the "ANDROID_SDK" environment variable is correctly '
        print 'set, or that adb is in your path.'

        sys.exit(adb_check)
    loglevel = e = None
    try:
        loglevel = getattr(logging, loglevel_name)
    except AttributeError, e:
        pass
    finally:
        if e or logging.getLevelName(loglevel) != loglevel_name:
            print 'Invalid log level %s' % loglevel_name
            return errno.EINVAL

    logging.basicConfig(filename=logfile,
                        filemode='a',
                        level=loglevel,
                        format='%(asctime)s|%(levelname)s|%(message)s')

    print '%s Starting server on port %d.' % \
        (datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), port)
    autophone = AutoPhone(is_restarting, reboot_phones, test_path, cachefile,
                          ipaddr, port, logfile, loglevel, emailcfg)
    autophone.run()
    print '%s AutoPhone terminated.' % datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    return 0


if __name__ == '__main__':
    from optparse import OptionParser

    parser = OptionParser()
    parser.add_option('--restarting', action='store_true', dest='is_restarting',
                      default=False,
                      help='If specified, we restart using the information '
                      'in cache')
    parser.add_option('--no-reboot', action='store_false', dest='reboot_phones',
                      default=True, help='With --restart, indicates that '
                      'phones should not be rebooted when autophone starts')
    parser.add_option('--ipaddr', action='store', type='string', dest='ipaddr',
                      default=None, help='IP address of interface to use for '
                      'phone callbacks, e.g. after rebooting. If not given, '
                      'it will be guessed.')
    parser.add_option('--port', action='store', type='int', dest='port',
                      default=28001,
                      help='Port to listen for incoming connections, defaults '
                      'to 28001')
    parser.add_option('--cache', action='store', type='string', dest='cachefile',
                      default='autophone_cache.json',
                      help='Cache file to use, defaults to autophone_cache.json '
                      'in local dir')
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
    (options, args) = parser.parse_args()

    exit_code = main(options.is_restarting, options.reboot_phones,
                     options.test_path, options.cachefile, options.ipaddr,
                     options.port, options.logfile, options.loglevel,
                     options.emailcfg) 

    sys.exit(exit_code)
