# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import ConfigParser
import logging
import socket
from sendemail import sendemail

# Set the logger globally in the file, but this must be reset when
# used in a child process.
LOGGER = logging.getLogger()

class Mailer(object):

    def __init__(self, cfgfile, subject_prefix=''):
        self.cfgfile = cfgfile
        self.subject_prefix = subject_prefix
        self.from_address = None
        self.mail_dest = None
        self.mail_username = None
        self.mail_password = None
        self.mail_server = None
        self.mail_port = None
        self.mail_ssl = None

        cfg = ConfigParser.ConfigParser()
        if not cfg.read(self.cfgfile):
            LOGGER.info('No email configuration file found. No emails will be sent.')
            return

        try:
            self.from_address = cfg.get('report', 'from')
        except (ConfigParser.NoSectionError, ConfigParser.NoOptionError):
            LOGGER.error('No "from" option defined in "report" section '
                         'of file "%s".\n', self.cfgfile)
            return

        try:
            self.mail_dest = [x.strip() for x in cfg.get('email', 'dest').split(',')]
        except (ConfigParser.NoSectionError, ConfigParser.NoOptionError):
            LOGGER.error('No "dest" option defined in "email" section '
                         'of file "%s".\n', self.cfgfile)
            return

        try:
            self.mail_username = cfg.get('email', 'username')
        except (ConfigParser.NoSectionError, ConfigParser.NoOptionError):
            self.mail_username = None

        try:
            self.mail_password = cfg.get('email', 'password')
        except (ConfigParser.NoSectionError, ConfigParser.NoOptionError):
            self.mail_password = None

        try:
            self.mail_server = cfg.get('email', 'server')
        except (ConfigParser.NoSectionError, ConfigParser.NoOptionError):
            self.mail_server = 'smtp.mozilla.org'

        try:
            self.mail_port = cfg.getint('email', 'port')
        except (ConfigParser.NoSectionError, ConfigParser.NoOptionError):
            self.mail_port = 25

        try:
            self.mail_ssl = cfg.getboolean('email', 'ssl')
        except (ConfigParser.NoSectionError, ConfigParser.NoOptionError):
            self.mail_ssl = False


    def send(self, subject, body):
        if not self.from_address or not self.mail_dest:
            return

        # encode string as ascii ignoring encoding errors
        subject = subject.encode('ascii', errors='ignore')
        body = body.encode('ascii', errors='ignore')

        try:
            sendemail(from_addr=self.from_address,
                      to_addrs=self.mail_dest,
                      subject='%s%s' % (self.subject_prefix, subject),
                      username=self.mail_username,
                      password=self.mail_password,
                      text_data=body,
                      server=self.mail_server,
                      port=self.mail_port,
                      use_ssl=self.mail_ssl)
        except socket.error:
            LOGGER.exception('Failed to send email notification: '
                             'subject: %s, body: %s', subject, body)
