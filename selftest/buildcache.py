# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import datetime
import logging
import shutil
import tempfile
import unittest

import builds

class BuildsTest(unittest.TestCase):

    def setUp(self):
        self.cache_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.cache_dir)
        
    def test_find_builds(self):
        """Basic test just to ensure that we can find any builds at all, with
        no errors."""
        bc = builds.BuildCache(self.cache_dir)
        now = datetime.datetime.now()
        yesterday = now - datetime.timedelta(days=2)
        buildlist = bc.find_builds(yesterday, now, 'nightly')
        for l in buildlist:
            logging.info(l)
        self.assertTrue(buildlist)
        buildlist = bc.find_builds(yesterday, now, 'tinderbox')
        for l in buildlist:
            logging.info(l)
        self.assertTrue(buildlist)
