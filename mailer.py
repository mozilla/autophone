# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import logging
import ConfigParser
from sendemail import sendemail

class Mailer(object):

    def __init__(self, cfgfile):
        self.cfgfile = cfgfile

    def send(self, subject, body):
        cfg = ConfigParser.ConfigParser()
        cfg.read(self.cfgfile)
        try:
            from_address = cfg.get('report', 'from')
        except (ConfigParser.NoSectionError, ConfigParser.NoOptionError):
            logging.error('No "from" option defined in "report" section of file "%s".\n' % self.cfgfile)
            return

        try:
            mail_dest = [x.strip() for x in cfg.get('email', 'dest').split(',')]
        except (ConfigParser.NoSectionError, ConfigParser.NoOptionError):
            mail_dest = []

        try:
            mail_username = cfg.get('email', 'username')
        except (ConfigParser.NoSectionError, ConfigParser.NoOptionError):
            mail_username = None

        try:
            mail_password = cfg.get('email', 'password')
        except (ConfigParser.NoSectionError, ConfigParser.NoOptionError):
            mail_password = None

        try:
            mail_server = cfg.get('email', 'server')
        except (ConfigParser.NoSectionError, ConfigParser.NoOptionError):
            mail_server = 'mail.mozilla.com'

        try:
            mail_port = cfg.getint('email', 'port')
        except (ConfigParser.NoSectionError, ConfigParser.NoOptionError):
            mail_port = 465

        try:
            mail_ssl = cfg.getboolean('email', 'ssl')
        except (ConfigParser.NoSectionError, ConfigParser.NoOptionError):
            mail_ssl = True

        sendemail(from_addr=from_address, to_addrs=mail_dest, subject=subject,
                  username=mail_username, password=mail_password,
                  text_data=body, server=mail_server, port=mail_port,
                  use_ssl=mail_ssl)
