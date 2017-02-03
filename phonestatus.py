# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

class PhoneStatus(object):

    OK = 'OK'
    IDLE = 'IDLE'
    CHARGING = 'CHARGING'
    FETCHING = 'FETCHING BUILD'
    INSTALLING = 'INSTALLING BUILD'
    WORKING = 'WORKING'
    REBOOTING = 'REBOOTING'
    DISCONNECTED = 'DISCONNECTED'  # phone not connected by adb/usb
    ERROR = 'ERROR' # phone connected but not responding
    DISABLED = 'DISABLED'  # phone worker disabled by user
    SHUTDOWN = 'SHUTDOWN' # worker shutdown, process exited


class TreeherderStatus(object):
    BUSTED = 'busted'
    EXCEPTION = 'exception'
    TESTFAILED = 'testfailed'
    UNKNOWN = 'unknown'
    USERCANCEL = 'usercancel'
    RETRY = 'retry'
    SUCCESS = 'success'


class TestStatus(object):
    TEST_PASS = 'TEST-PASS'
    TEST_UNEXPECTED_FAIL = 'TEST-UNEXPECTED-FAIL'
    PROCESS_CRASH = 'PROCESS-CRASH'
