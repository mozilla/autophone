# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import errno
import shutil
import subprocess
import os
import logging
from time import sleep
import urllib
import sys
import ConfigParser
import devicemanagerSUT

# this is probably not specific to adb stuff--it should represent a general
# android failure
class AndroidError(Exception):
    pass


# This code is meant to be used from threads, so make subprocess threadsafe in
# a very hacky way, python: http://bugs.python.org/issue1731717
subprocess._cleanup = lambda: None        

"""
install_build_sut - installs build on phone via sutagent
Params:
* phoneID: phone id (for reporting)
* url: url of build to download and install
* procname: process name for uninstall of existing app
* sutip: ip address of sutagent
* sutport: port of sutagent on phone (cmdport)
* callbackport: port to use as a callback on this machine
"""
def install_build_sut(phoneid=None, url=None, procname='org.mozilla.fennec',
                  sutip=None, sutport='20701', callbackport='30000'):
    if not phoneid or not url or not sutip:
        print 'You must specify a phoneid, url, and sutip address'
        return False


    apkpath = get_build(url, phoneid)
    if not apkpath:
        return False

    ret = True

    nt = NetworkTools()
    myip = nt.getLanIp()

    try:
        dm = devicemanagerSUT.DeviceManagerSUT(sutip, sutport)
        devroot = dm.getDeviceRoot()
        # If we get a real deviceroot, then we can connect to the phone
        if devroot:
            devpath = devroot + '/fennecbld.apk'
            dm.pushFile(apkpath, devpath)
            dm.updateApp(devpath, processName=procname, ipAddr=myip,
                    port=callbackport)
            logging.debug('Completed update for phoneID: %s' % phoneid)
        else:
            logging.warn('Could not get devroot for phone: %s' % phoneid)
    except:
        logging.error('Could not install latest nightly on %s' % phoneid)
        ret = False

    # If the file exists, clean it up
    if os.path.exists(apkpath):
        os.remove(apkpath)
        os.rmdir(phoneid)
    return ret


"""
install build adb - downloads and installs build on phone via adb
Params:
* phoneid: id of phone for reporting
* apkpath: path to build
* procname: process name to uninstall old build
* serial: adb serial number for phone
"""
def install_build_adb(phoneid=None, apkpath=None, blddate=None,
                      procname='org.mozilla.fennec', serial=None):
    if not phoneid or not apkpath or not serial:
        print 'You must specify a phoneid, url, and a serial number'
        return False
    #import pdb
    #pdb.set_trace()

    ret = True
    o = run_adb('uninstall', [procname], serial, timeout=30)
    if o.lower().find('success') == -1:
        logging.warn('Unable to uninstall application on phoneID: %s' %
                phoneid)
        ret = False

    o = run_adb('install', [apkpath], serial, timeout=120)
    print o
    if o.lower().find('success') == -1:
        logging.error('Unable to install application on phoneID: %s' % phoneid)
        ret = False
    else:
        # It could be the case that the app wasn't installed so we might have
        # failed to uninstall which would be ok
        ret = True

    return ret


def find_adb():
    if 'ANDROID_SDK' in os.environ:
        return os.path.join(os.environ['ANDROID_SDK'], 'platform-tools', 'adb')
    return 'adb'


def check_for_adb():
    adb = find_adb()
    try:
        p = subprocess.Popen([adb], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE)
    except OSError, e:
        return e.errno
    p.communicate()  # wait for it to terminate
    return 0


"""
run_adb - runs an adb command
Assumes that the android sdk location is specified by ANDROID_SDK environment
variable or that adb is accessible from your path. If this is not the case this
will throw
Params:
* adbcmd - the adb command to run install, logcat, shell etc
* cmd - an ARRAY of command parameters, MUST BE AN ARRAY
* serial - optional serial number if multiple adb devices are installed
* check_for_error - if True and stderr from the subprocess starts with 'error',
                    raise an AndroidError exception
* timeout - if > 0 and subprocess takes longer than this value, in seconds, to
            complete, raise AndroidError exception

RETURNS:
* The stdout of the adb command
"""
def run_adb(adbcmd, cmd, serial=None, check_for_error=False, timeout=0):
    adb = find_adb()

    if serial:
        logging.debug('adb cmd: %s' %
                      subprocess.list2cmdline([adb, '-s', serial, adbcmd]
                                              + cmd))
        p = subprocess.Popen([adb, '-s', serial, adbcmd] + cmd,
                             stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE)
    else:
        logging.debug('run adb cmd: %s' %
                      subprocess.list2cmdline([adb, adbcmd] + cmd))
        p = subprocess.Popen([adb, adbcmd] + cmd,
                             stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE)
    if timeout > 0:
        for i in range(0, timeout):
            if p.poll() is not None:
                break
            sleep(1)
        if p.poll() is None:
            p.terminate()
            for i in range(0, 5):
                if p.poll() is not None:
                    break
            if p.poll() is None:
                p.kill()
            p.wait()
            logging.error('adb processed timed out')
            raise AndroidError('timeout: process has been running for %d seconds' %
                           timeout)
    stdout, stderr = p.communicate()
    if check_for_error and stderr.startswith('error'):
        logging.error('adb error: %s' % stderr)
        raise AndroidError(stderr)
    logging.debug('adb command finished')
    return stdout


def reboot_adb(serial):
    run_adb('shell', ['su', '-c', 'reboot'], serial=serial)


"""
Use sutagent to kill an application
Params:
    ip of phone
    port of cmd port for agent on phone
    process name
"""
def kill_proc_sut(ip=None, port='20701', procname='org.mozilla.fennec'):
    if not ip:
        print 'You must specify an IP address to the phone'
        return False

    dm = devicemanagerSUT.DeviceManagerSUT(ip, port)
    dm.killProcess(procname)


def get_fennec_profile_path_adb(serial=None, procname=None):
    if not serial:
        print 'You must specify a serial number for adb'
        return None
    logging.debug('Getting Fennec Profile Path')
    path = '/data/data/' + procname + '/files/mozilla/profiles.ini'
    data = run_adb('shell', ['su', '-c', 'cat %s' % path], serial=serial)

    if data == '':
        return None

    pfile = open('profiles.ini', 'w')
    pfile.writelines(data.split('\r'))
    pfile.flush()
    path = None
    if os.path.exists('profiles.ini'):
        cfg = ConfigParser.RawConfigParser()
        cfg.read('profiles.ini')

        if cfg.has_section('Profile0'):
            isrelative = cfg.get('Profile0', 'IsRelative')
            profname = cfg.get('Profile0', 'Path')
        else:
            logging.error('Unknown profile')

        if isrelative == '1':
            path = '/data/data/%s/files/mozilla/%s' % (procname,profname)
        else:
            path = profname
        os.remove('profiles.ini')
    return path


def remove_sessionstore_files_adb(serial=None, procname=None):
    if not serial or not procname:
        print 'You must specify a serial number for adb and the app name'
        return None

    # Get the profile
    fennec_profile = get_fennec_profile_path_adb(serial=serial,
                                                 procname=procname)

    if fennec_profile:
        sessionstorepth = fennec_profile + '/sessionstore.js'
        run_adb('shell', ['su', '-c', 'rm %s' % sessionstorepth], serial=serial)
        sessionstorepth = fennec_profile + '/sessionstore.bak'
        run_adb('shell', ['su', '-c', 'rm %s' % sessionstorepth], serial=serial)
    else:
        # The first run doesn't have a profile so that's ok
        logging.warn('No profile exists')
