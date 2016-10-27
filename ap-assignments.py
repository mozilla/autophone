#!/usr/bin/env python
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import ConfigParser
import os
import sys
from glob import glob

from utils import autophone_path

install_time = 2

builds = {
    'autoland': 40,
    'mozilla-inbound': 60,
    'fx-team': 10,
    'mozilla-central': 5,
    'mozilla-beta': 4,
    'mozilla-aurora': 5,
    'mozilla-release': 1,
    'try': 1,
}

build_types = ['opt', 'debug']

device_times = {
    'nexus-4': {
        'runtestsremote.py Cw':        1,
        'runtestsremote.py gl':        60,
        'runtestsremote.py glm':       1,
        'runtestsremote.py C':         25,
        'runtestsremote.py J':         60,
        'runtestsremote.py M':         0,
        'runtestsremote.py Mdb':       3,
        'runtestsremote.py Mdm':       26,
        'runtestsremote.py Mm':        11,
        'runtestsremote.py Mw':        10,
        'runtestsremote.py Cwrtc':     10,
        'runtestsremote.py Msk':       3,
        'runtestsremote.py Mtw':       2,
        'runtestsremote.py R':         0,
        'runtestsremote.py Rov':       1,
        'runtestsremote.py Rwv':       1,
        'runtestsremote.py rc':        60,
        'runtestsremote.py rca':       1,
        's1s2test.py blank-local':     5,
        's1s2test.py blank-remote':    5,
        's1s2test.py nytimes-local':   6,
        's1s2test.py nytimes-remote':  6,
        's1s2test.py twitter-local':   5,
        's1s2test.py twitter-remote':  5,
        'smoketest.py':                1,
        'talostest.py tp4m-remote':    2,
        'talostest.py tsvg-remote':    2,
        'webappstartup.py':            6,
    },
    'nexus-5': {
        'runtestsremote.py Cw':        1,
        'runtestsremote.py gl':        60,
        'runtestsremote.py glm':       1,
        'runtestsremote.py C':         18,
        'runtestsremote.py J':         60,
        'runtestsremote.py M':         0,
        'runtestsremote.py Mdb':       3,
        'runtestsremote.py Mdm':       15,
        'runtestsremote.py Mm':        11,
        'runtestsremote.py Mw':        10,
        'runtestsremote.py Cwrtc':     10,
        'runtestsremote.py Msk':       3,
        'runtestsremote.py Mtw':       1,
        'runtestsremote.py R':         0,
        'runtestsremote.py Rov':       1,
        'runtestsremote.py Rwv':       1,
        'runtestsremote.py rc':        60,
        'runtestsremote.py rca':       1,
        's1s2test.py blank-local':     4,
        's1s2test.py blank-remote':    4,
        's1s2test.py nytimes-local':   4,
        's1s2test.py nytimes-remote':  4,
        's1s2test.py twitter-local':   4,
        's1s2test.py twitter-remote':  4,
        'smoketest.py':                1,
        'talostest.py tp4m-remote':    2,
        'talostest.py tsvg-remote':    2,
        'webappstartup.py':            6,
    },
    'nexus-6': {
        'runtestsremote.py Cw':        1,
        'runtestsremote.py gl':        60,
        'runtestsremote.py glm':       1,
        'runtestsremote.py C':         30,
        'runtestsremote.py J':         45,
        'runtestsremote.py M':         0,
        'runtestsremote.py Mdb':       2,
        'runtestsremote.py Mdm':       30,
        'runtestsremote.py Mm':        19,
        'runtestsremote.py Mw':        10,
        'runtestsremote.py Cwrtc':     10,
        'runtestsremote.py Msk':       3,
        'runtestsremote.py Mtw':       2,
        'runtestsremote.py R':         0,
        'runtestsremote.py Rov':       1,
        'runtestsremote.py Rwv':       1,
        'runtestsremote.py rc':        60,
        'runtestsremote.py rca':       1,
        's1s2test.py blank-local':     5,
        's1s2test.py blank-remote':    5,
        's1s2test.py nytimes-local':   6,
        's1s2test.py nytimes-remote':  6,
        's1s2test.py twitter-local':   5,
        's1s2test.py twitter-remote':  5,
        'smoketest.py':                1,
        'talostest.py tp4m-remote':    2,
        'talostest.py tsvg-remote':    2,
        'webappstartup.py':            6,
    },
    'nexus-6p': {
        'runtestsremote.py Cw':        1,
        'runtestsremote.py gl':        60,
        'runtestsremote.py glm':       1,
        'runtestsremote.py C':         20,
        'runtestsremote.py J':         45,
        'runtestsremote.py M':         0,
        'runtestsremote.py Mdb':       2,
        'runtestsremote.py Mdm':       25,
        'runtestsremote.py Mm':        4,
        'runtestsremote.py Mw':        10,
        'runtestsremote.py Cwrtc':     10,
        'runtestsremote.py Msk':       2,
        'runtestsremote.py Mtw':       1,
        'runtestsremote.py R':         0,
        'runtestsremote.py Rov':       1,
        'runtestsremote.py Rwv':       1,
        'runtestsremote.py rc':        65,
        'runtestsremote.py rca':       1,
        's1s2test.py blank-local':     4,
        's1s2test.py blank-remote':    4,
        's1s2test.py nytimes-local':   4,
        's1s2test.py nytimes-remote':  4,
        's1s2test.py twitter-local':   4,
        's1s2test.py twitter-remote':  4,
        'smoketest.py':                1,
        'talostest.py tp4m-remote':    2,
        'talostest.py tsvg-remote':    2,
        'webappstartup.py':            6,
    },
    'nexus-9': {
        'runtestsremote.py Cw':        1,
        'runtestsremote.py gl':        60,
        'runtestsremote.py glm':       1,
        'runtestsremote.py C':         190,
        'runtestsremote.py J':         30,
        'runtestsremote.py M':         0,
        'runtestsremote.py Mdb':       2,
        'runtestsremote.py Mdm':       22,
        'runtestsremote.py Mm':        4,
        'runtestsremote.py Mw':        10,
        'runtestsremote.py Cwrtc':     10,
        'runtestsremote.py Msk':       7,
        'runtestsremote.py Mtw':       1,
        'runtestsremote.py R':         0,
        'runtestsremote.py Rov':       1,
        'runtestsremote.py Rwv':       1,
        'runtestsremote.py rc':        60,
        'runtestsremote.py rca':       1,
        's1s2test.py blank-local':     4,
        's1s2test.py blank-remote':    4,
        's1s2test.py nytimes-local':   4,
        's1s2test.py nytimes-remote':  4,
        's1s2test.py twitter-local':   4,
        's1s2test.py twitter-remote':  4,
        'smoketest.py':                1,
        'talostest.py tp4m-remote':    2,
        'talostest.py tsvg-remote':    2,
        'webappstartup.py':            6,
    },
}

repos = {} # repos[repo_name_build_type][test_name] = [device_names]

devices = {} # devices[device_name][test_name] = [repo_names_build_types]

tests = {} # tests[test_name][repo_name_build_type] = [device_names]

devicecfg = ConfigParser.RawConfigParser()

device_manifest_paths = glob(os.path.join(autophone_path(),
                                          'production-autophone-*-devices.ini'))
test_manifest_paths = glob(os.path.join(autophone_path(),
                                        'tests/production-autophone-*.ini'))

devicecfg.read(device_manifest_paths)

for device in devicecfg.sections():
    if device in devices:
        print "ERROR: device %s already in %s" % (device, devices)
    else:
        devices[device] = {}

test_manifests = ConfigParser.RawConfigParser()

test_manifests.read(test_manifest_paths)

test_sections = test_manifests.sections()
test_sections.sort()

for test_name in test_sections:
    if test_name not in tests:
        tests[test_name] = {}

    device_name = None # signal if any devices explicitly mentioned
    test_options = test_manifests.options(test_name)
    test_options.sort()
    for test_option in test_options:
        test_value = test_manifests.get(test_name, test_option)
        #print "test_name: %s, test_option: %s, test_value: %s" % (test_name, test_option, test_value)
        if test_option == 'config':
            # config is required and is the first option in the section.
            test_config = ConfigParser.RawConfigParser()
            test_config.read("%s/tests/%s" % (autophone_path(),
                                              test_value))
            try:
                test_build_types = test_config.get('builds', 'buildtypes').split()
            except ConfigParser.Error:
                test_build_types = list(build_types)
            #print "test_build_types: %s" % test_build_types
            continue
        device_name = test_option
        if device_name not in devices:
            print "ERROR: %s not in devices" % device_name
            devices[device] = []
        repo_names = test_value.split()
        repo_names_build_types = []
        for repo_name in repo_names:
            for build_type in test_build_types:
                repo_names_build_types.append('%s-%s' % (repo_name, build_type))
        for repo_name_build_type in repo_names_build_types:
            if repo_name_build_type not in repos:
                repos[repo_name_build_type] = {}
            if test_name not in repos[repo_name_build_type]:
                repos[repo_name_build_type][test_name] = []
            repos[repo_name_build_type][test_name].append(device_name)
            #
            if device_name not in devices:
                devices[device_name] = {}
            if test_name not in devices[device_name]:
                devices[device_name][test_name] = []
            devices[device_name][test_name].append(repo_name_build_type)
            #
            if test_name not in tests:
                tests[test_name] = {}
            if repo_name_build_type not in tests[test_name]:
                tests[test_name][repo_name_build_type] = []
            tests[test_name][repo_name_build_type].append(device_name)

print "="* 10, "Device assignments", "="*10

device_names = devices.keys()
device_names.sort()

for device_name in device_names:
    device_type = ('-').join(device_name.split('-')[:-1])
    device_repos = {} # repos seen so far
    device_time = 0
    test_names = devices[device_name].keys()
    test_names.sort()
    for test_name in test_names:
        test_time = 0
        repo_names_build_types = devices[device_name][test_name]
        repo_names_build_types.sort()
        print "device=%-28s test=%-30s repos=%s" % (device_name, test_name, repo_names_build_types)
        for repo_name_build_type in repo_names_build_types:
            repo_time = 0
            repo_name = '-'.join(repo_name_build_type.split('-')[0:-1])
            build_count = builds[repo_name]
            #print "repo %s count %s" % (repo_name_build_type, build_count)
            if repo_name_build_type not in device_repos:
                #print "adding install time %s" % install_time
                device_repos[repo_name_build_type] = 1
                repo_time += install_time
            repo_time += device_times[device_type][test_name]
            repo_time *= build_count
            #print "repo %s time %s" % (repo_name_build_type, repo_time)
            device_time += repo_time
    print "device=%-28s time=%s" % (device_name, device_time)

print "="* 10, "Repository-Build type assignments", "="*10

repo_names_build_types = repos.keys()
repo_names_build_types.sort()
### ...
for repo_name_build_type in repo_names_build_types:
    test_names = repos[repo_name_build_type].keys()
    test_names.sort()
    for test_name in test_names:
        device_names = repos[repo_name_build_type][test_name]
        device_names.sort()
        print "repo=%-30s test=%-30s devices=%s" % (repo_name_build_type, test_name, device_names)

print "="* 10, "Test assignments", "="*10

test_names = tests.keys()
test_names.sort()

for test_name in test_names:
    repo_names_build_types = tests[test_name].keys()
    repo_names_build_types.sort()
    for repo_name_build_type in repo_names_build_types:
        device_names = tests[test_name][repo_name_build_type]
        device_names.sort()
        print "test=%-30s repo=%-25s devices=%s" % (test_name, repo_name_build_type, device_names)




