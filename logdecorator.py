# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import re
import sys
import traceback

class LogDecorator(object):
    def __init__(self, logger, extradict, extraformat):
        self._logger = logger
        self._extradict = extradict
        self._extraformat = extraformat

        if re.search('(%[(][a-zA-Z]+[)][^a-z]|%[(][a-zA-Z]+[)]$)', extraformat):
            raise ValueError('format string contains a %(attribute)'
                             'pattern without a type specifier.')

    def _expanded_message(self, message):
        extradict = dict(self._extradict)
        try:
            # If the message is actualy an object, such as DMError,
            # convert it to a string.
            message = "%s" % message
            if isinstance(message, unicode):
                extradict['message'] = message
            else:
                extradict['message'] = unicode(message, errors='replace')
            extramessage = self._extraformat % extradict
        except UnicodeDecodeError:
            etype, evalue, etraceback = sys.exc_info()
            extramessage = ''.join(traceback.format_exception(etype, evalue, etraceback))
            print extramessage
            print message
        return extramessage

    def logger(self):
        return self._logger

    def getEffectiveLevel(self):
        return self._logger.getEffectiveLevel()

    def debug(self, message, *args, **kwargs):
        self._logger.debug(self._expanded_message(message), *args, **kwargs)

    def info(self, message, *args, **kwargs):
        self._logger.info(self._expanded_message(message), *args, **kwargs)

    def warning(self, message, *args, **kwargs):
        self._logger.warning(self._expanded_message(message), *args, **kwargs)

    def warn(self, message, *args, **kwargs):
        self._logger.warning(self._expanded_message(message), *args, **kwargs)

    def error(self, message, *args, **kwargs):
        self._logger.error(self._expanded_message(message), *args, **kwargs)

    def critical(self, message, *args, **kwargs):
        self._logger.critical(self._expanded_message(message), *args, **kwargs)

    def log(self, lvl, message, *args, **kwargs):
        self._logger.log(lvl, self._expanded_message(message), *args, **kwargs)

    def exception(self, message, *args, **kwargs):
        self._logger.exception(self._expanded_message(message), *args, **kwargs)
