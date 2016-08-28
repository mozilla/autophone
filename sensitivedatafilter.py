# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import logging

class SensitiveDataFilter(logging.Filter):
    def __init__(self, sensitive_data):
        logging.Filter.__init__(self)
        self.sensitive_data = sensitive_data

    def filter(self, record):
        try:
            msg = record.getMessage()
            for data in self.sensitive_data:
                if data and data in msg:
                    return False
            return True
        except:
            # If we can't decode it, treat the message as sensitive
            return False
