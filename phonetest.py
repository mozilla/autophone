# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import datetime
import json
import logging
import StringIO

from mozdevice import DeviceManagerSUT
from mozprofile import FirefoxProfile

class PhoneTestMessage(object):

    IDLE = 'IDLE'
    INSTALLING = 'INSTALLING BUILD'
    WORKING = 'WORKING'
    REBOOTING = 'REBOOTING'
    DISABLED = 'DISABLED'

    class JsonEncoder(json.JSONEncoder):
        def default(self, obj):
            if isinstance(obj, PhoneTestMessage):
                return { 'phoneid': obj.phoneid, 'online': obj.online,
                         'msg': obj.msg, 'timestamp': obj.timestamp }
            return json.JSONEncoder.default(self, obj)


    def __init__(self, phoneid, status, current_build=None, msg=None):
        self.phoneid = phoneid
        self.status = status
        self.current_build = current_build
        self.msg = msg
        self.timestamp = datetime.datetime.now().replace(microsecond=0)

    def __str__(self):
        s = '<%s> %s (%s)' % (self.timestamp.isoformat(), self.phoneid,
                              self.status)
        if self.msg:
            s += ': %s' % self.msg
        return s

    def short_desc(self):
        s = self.status
        if self.msg:
            s += ': %s' % self.msg
        return s


class PhoneTest(object):

    """
    The initialization function. It takes and stores all the information
    related to contacting this phone.
    Params:
    phoneid = ID of phone, to be used in log messages and reporting
    serial = serial number for adb style interfaces
    ip = phone's IP address (where sutagent running if it is running)
    sutcmdport = cmd port of sutagent if it is running
    sutdataport = data port of sutagent if it is running
    machinetype = pretty name of machine type - i.e. galaxy_nexus, droid_pro etc
    osver = version string of phone OS
    TODO: Add in connection data here for programmable power so we can add a
    powercycle method to this class.
    """
    def __init__(self, phone_cfg, config_file=None, status_cb=None):
        self.config_file = config_file
        self.status_cb = status_cb
        self.phone_cfg = phone_cfg
        self.status = None
        self.logger = logging.getLogger('phonetest')
        self._base_device_path = ''
        self._dm = None

    @property
    def dm(self):
        if not self._dm:
            self._dm = DeviceManagerSUT(self.phone_cfg['ip'],
                                        self.phone_cfg['sutcmdport'])
        return self._dm

    @property
    def base_device_path(self):
        if self._base_device_path:
            return self._base_device_path
        self._base_device_path = self.dm.getDeviceRoot() + '/autophone'
        if not self.dm.dirExists(self._base_device_path):
            self.dm.mkDirs(self._base_device_path)
        return self._base_device_path

    @property
    def profile_path(self):
        return self.base_device_path + '/profile'

    def runjob(self, job):
        raise NotImplementedError


    """
    sets the status
    Params:
    online = boolean True of False
    msg = the message of status
    """
    def set_status(self, status=PhoneTestMessage.WORKING, msg=None):
        self.status = PhoneTestMessage(self.phone_cfg['phoneid'], status,
                                       self.current_build, msg)
        if self.status_cb:
            self.status_cb(self.status)

    def install_profile(self, profile=None):
        if not profile:
            profile = FirefoxProfile()
        
        self.dm.removeDir(self.profile_path)
        self.dm.mkDir(self.profile_path)
        self.dm.pushDir(profile.profile, self.profile_path)

    def run_fennec_with_profile(self, intent, url):
        self.dm.pushFile('runbrowserprofile.sh',
                         self.base_device_path + '/runbrowserprofile.sh')
        output = StringIO.StringIO()
        args = ['am', 'start', '-a', 'android.intent.action.VIEW', '-n',
                intent, '--es', 'args', '--profile %s' % self.profile_path,
                '-d', url]
        self.dm.shell(args, output)
        logging.debug(output.getvalue())

    def remove_sessionstore_files(self):
        self.dm.removeFile(self.profile_path + '/sessionstore.js')
        self.dm.removeFile(self.profile_path + '/sessionstore.bak')
