# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import datetime

class PhoneStatus(object):

    IDLE = 'IDLE'
    CHARGING = 'CHARGING'
    INSTALLING = 'INSTALLING BUILD'
    WORKING = 'WORKING'
    REBOOTING = 'REBOOTING'
    DISCONNECTED = 'DISCONNECTED'  # temporary error
    DISABLED = 'DISABLED'  # permanent error


class PhoneTestMessage(object):

    def __init__(self, phone, build=None, phone_status=None,
                 message=None):
        self.phone = phone
        self.build = build
        self.phone_status = phone_status
        self.message = message
        self.timestamp = datetime.datetime.now().replace(microsecond=0)

    def __str__(self):
        s = '<%s> %s (%s)' % (self.timestamp.isoformat(), self.phone.id,
                              self.phone_status)
        if self.message:
            s += ': %s' % self.message
        return s

    def short_desc(self):
        s = self.phone_status
        if self.message:
            s += ': %s' % self.message
        return s
