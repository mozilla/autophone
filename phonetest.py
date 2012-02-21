# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import datetime
import logging

class PhoneTestMessage(object):

    def __init__(self, phoneid, online, msg):
        self.phoneid = phoneid
        self.online = online
        self.msg = msg
        self.timestamp = datetime.datetime.now().isoformat()

    def __str__(self):
        if self.online:
            online_status = 'ONLINE'
        else:
            online_status = 'OFFLINE'
        return '<%s> %s (%s): %s' % (self.timestamp, self.phoneid,
                                     online_status, self.msg)


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
        self.set_status(msg='Initialized')

    def runjob(self, job):
        raise NotImplementedError

    """
    sets the status
    Params:
    online = boolean True of False
    msg = the message of status
    """
    def set_status(self, online=True, msg=None):
        self.status = PhoneTestMessage(self.phone_cfg['phoneid'], online,
                                       msg)
        if self.status_cb:
            self.status_cb(self.status)


