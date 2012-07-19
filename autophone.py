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
import signal
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

from devicemanager import NetworkTools
from mailer import Mailer
from worker import PhoneWorker


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
                except IOError, e:
                    if e.errno == errno.EINTR:
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

        logfile_prefix, logfile_ext = os.path.splitext(self.logfile)
        worker = PhoneWorker(len(self.phone_workers.keys()), tests, phone_cfg,
                             self.worker_msg_queue,
                             '%s-%s%s' % (logfile_prefix, phone_cfg['phoneid'],
                                          logfile_ext),
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

    def sigterm_handler(signum, frame):
        autophone.stop()

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
    signal.signal(signal.SIGTERM, sigterm_handler)
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
