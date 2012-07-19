#!/usr/bin/env python

# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

"""
run mozbase tests from a manifest,
by default https://github.com/mozilla/mozbase/blob/master/test-manifest.ini
"""

import imp
import logging
import manifestparser
import os
import sys
import unittest

here = os.path.dirname(os.path.abspath(__file__))

def unittests(path):
    """return the unittests in a .py file"""
    path = os.path.abspath(path)
    unittests = []
    assert os.path.exists(path)
    modname = os.path.splitext(os.path.basename(path))[0]
    module = imp.load_source(modname, path)
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(module)
    for test in suite:
        unittests.append(test)
    return unittests

def main(args=sys.argv[1:]):
    logging.basicConfig(filename='test.log',
                        filemode='w',
                        level=logging.DEBUG,
                        format='%(asctime)s|%(levelname)s|%(message)s')

    # read the manifest
    if args:
        manifests = args
    else:
        manifests = [os.path.join(here, 'selftest', 'manifest.ini')]
    missing = []
    for manifest in manifests:
        # ensure manifests exist
        if not os.path.exists(manifest):
            missing.append(manifest)
    assert not missing, 'manifest%s not found: %s' % ((len(manifests) == 1 and '' or 's'), ', '.join(missing))
    manifest = manifestparser.TestManifest(manifests=manifests)

    sys.path.insert(0, here)

    # gather the tests
    tests = manifest.active_tests()
    unittestlist = []
    for test in tests:
        unittestlist.extend(unittests(test['path']))

    # run the tests
    suite = unittest.TestSuite(unittestlist)
    runner = unittest.TextTestRunner()
    results = runner.run(suite)

    # exit according to results
    sys.exit((results.failures or results.errors) and 1 or 0)

if __name__ == '__main__':
    main()
