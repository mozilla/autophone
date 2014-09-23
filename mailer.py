# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import ConfigParser
import logging
import socket
from sendemail import sendemail

class Mailer(object):

    logger = logging.getLogger('autophone.mailer')

    def __init__(self, cfgfile, subject_prefix=''):
        self.send_mail = True
        self.cfgfile = cfgfile
        self.subject_prefix = subject_prefix

        cfg = ConfigParser.ConfigParser()
        if not cfg.read(self.cfgfile):
            self.logger.info('No email configuration file found. No emails will be sent.')
            self.send_mail = False
            return

        try:
            self.from_address = cfg.get('report', 'from')
            if not self.from_address:
                self.send_mail = False
        except (ConfigParser.NoSectionError, ConfigParser.NoOptionError):
            self.send_mail = False
            self.logger.error('No "from" option defined in "report" section of file "%s".\n' % self.cfgfile)
            return

        try:
            self.mail_dest = [x.strip() for x in cfg.get('email', 'dest').split(',')]
            if not self.mail_dest:
                self.send_mail = False
        except (ConfigParser.NoSectionError, ConfigParser.NoOptionError):
            self.send_mail = False

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
            self.mail_server = 'mail.mozilla.com'

        try:
            self.mail_port = cfg.getint('email', 'port')
        except (ConfigParser.NoSectionError, ConfigParser.NoOptionError):
            self.mail_port = 465

        try:
            self.mail_ssl = cfg.getboolean('email', 'ssl')
        except (ConfigParser.NoSectionError, ConfigParser.NoOptionError):
            self.mail_ssl = True


    def send(self, subject, body):
        if not self.send_mail:
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
            self.logger.exception('Failed to send email notification: '
                                  'subject: %s, body: %s' % (subject, body))
