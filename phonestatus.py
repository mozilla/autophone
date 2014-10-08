# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

class PhoneStatus(object):

    IDLE = 'IDLE'
    CHARGING = 'CHARGING'
    INSTALLING = 'INSTALLING BUILD'
    WORKING = 'WORKING'
    REBOOTING = 'REBOOTING'
    DISCONNECTED = 'DISCONNECTED'  # temporary error
    DISABLED = 'DISABLED'  # permanent error


