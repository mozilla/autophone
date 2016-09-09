# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import logging
import sys
import traceback

class SensitiveDataFilter(logging.Filter):
    def __init__(self, sensitive_data):
        logging.Filter.__init__(self)
        self.sensitive_data = sensitive_data

    def filter(self, record):
        emit = True
        try:
            msg = record.getMessage()
            for data in self.sensitive_data:
                if data and data in msg:
                    emit = False
                    break
        except:
            # If we can't decode it, treat the message as sensitive
            etype, evalue, etraceback = sys.exc_info()
            print ''.join(traceback.format_exception(etype, evalue, etraceback))
            emit = False
        return emit
