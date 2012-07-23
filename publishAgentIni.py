# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Publishes a SUTAgent.ini file to the specified device."""

import errno
import os
import sys
from optparse import OptionParser

from mozdevice import DeviceManagerSUT

def main(ip, port, filename):
    dm = DeviceManagerSUT(ip, port)
    dm.pushFile(filename,
                '/data/data/com.mozilla.SUTAgentAndroid/files/SUTAgent.ini')

if __name__ == '__main__':
    parser = OptionParser()
    parser.add_option('-i', '--ip', dest='ip', help='IP address of device; '
                      'can also be specified through the TEST_DEVICE '
                      'environment variable')
    parser.add_option('-p', '--port', dest='port', type='int', default=20701,
                      help='Agent port on device; defaults to 20701')
    parser.add_option('-f', '--file', dest='file', default='SUTAgent.ini',
                      help='SUTAgent ini file to copy to device; defaults '
                      'to ./SUTAgent.ini')
    (options, args) = parser.parse_args()

    # Verify Options
    ip = options.ip
    port = options.port
    file = options.file

    if not ip:
        ip = os.environ.get('TEST_DEVICE')

    if not ip:
        print 'You must specify an ip address for the device via the -i ' \
            'option or through\nthe TEST_DEVICE environment variable.'
        sys.exit(errno.EINVAL)

    if not os.path.isfile(file):
        print 'SUTAgent ini file "%s" is not found or not a regular file.' % \
            file
        sys.exit(errno.EINVAL)

    main(ip, port, file)
