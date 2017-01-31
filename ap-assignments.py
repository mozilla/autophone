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

# Counts based on maximum pushes for weekdays 2016-10-17 through
# 2016-10-21.

builds = {
    'autoland': 53,
    'mozilla-inbound': 56,
    'mozilla-central': 8,
    'mozilla-beta': 6,
    'mozilla-aurora': 6,
    'mozilla-release': 2,
    'try': 1,
}

build_types = ['opt', 'debug']

device_times = {
    'nexus-4': {
        'runtestsremote.py Cw':       {'opt': 1, 'debug': 1},
        'runtestsremote.py gl':       {'opt': 60, 'debug': 60},
        'runtestsremote.py glm':      {'opt': 1, 'debug': 1},
        'runtestsremote.py C':        {'opt': 25, 'debug': 25},
        'runtestsremote.py J':        {'opt': 60, 'debug': 60},
        'runtestsremote.py M':        {'opt': 0, 'debug': 0},
        'runtestsremote.py Mdb':      {'opt': 3, 'debug': 3},
        'runtestsremote.py Mdm':      {'opt': 27, 'debug': 33},
        'runtestsremote.py Mm':       {'opt': 4, 'debug': 4},
        'runtestsremote.py Mw':       {'opt': 6, 'debug': 12},
        'runtestsremote.py Msk':      {'opt': 3, 'debug': 4},
        'runtestsremote.py Mtw':      {'opt': 1, 'debug': 2},
        'runtestsremote.py R':        {'opt': 0, 'debug': 0},
        'runtestsremote.py Rov':      {'opt': 1, 'debug': 1},
        'runtestsremote.py Rwv':      {'opt': 1, 'debug': 1},
        'runtestsremote.py rc':       {'opt': 60, 'debug': 60},
        'runtestsremote.py rca':      {'opt': 1, 'debug': 1},
        's1s2test.py blank-local':    {'opt': 5, 'debug': 5},
        's1s2test.py blank-remote':   {'opt': 5, 'debug': 5},
        's1s2test.py nytimes-local':  {'opt': 6, 'debug': 6},
        's1s2test.py nytimes-remote': {'opt': 6, 'debug': 6},
        's1s2test.py twitter-local':  {'opt': 5, 'debug': 5},
        's1s2test.py twitter-remote': {'opt': 5, 'debug': 5},
        'smoketest.py':               {'opt': 0, 'debug': 0},
        'talostest.py tp4m-remote':   {'opt': 2, 'debug': 2},
        'talostest.py tsvg-remote':   {'opt': 2, 'debug': 2},
        'webappstartup.py':           {'opt': 6, 'debug': 6},
    },
    'nexus-5': {
        'runtestsremote.py Cw':       {'opt': 1, 'debug': 1},
        'runtestsremote.py gl':       {'opt': 60, 'debug': 60},
        'runtestsremote.py glm':      {'opt': 1, 'debug': 1},
        'runtestsremote.py C':        {'opt': 18, 'debug': 18},
        'runtestsremote.py J':        {'opt': 60, 'debug': 60},
        'runtestsremote.py M':        {'opt': 0, 'debug': 0},
        'runtestsremote.py Mdb':      {'opt': 2, 'debug': 3},
        'runtestsremote.py Mdm':      {'opt': 27, 'debug': 33},
        'runtestsremote.py Mm':       {'opt': 6, 'debug': 7},
        'runtestsremote.py Mw':       {'opt': 4, 'debug': 7},
        'runtestsremote.py Msk':      {'opt': 2, 'debug': 2},
        'runtestsremote.py Mtw':      {'opt': 1, 'debug': 1},
        'runtestsremote.py R':        {'opt': 0, 'debug': 0},
        'runtestsremote.py Rov':      {'opt': 1, 'debug': 1},
        'runtestsremote.py Rwv':      {'opt': 1, 'debug': 1},
        'runtestsremote.py rc':       {'opt': 60, 'debug': 60},
        'runtestsremote.py rca':      {'opt': 0, 'debug': 0},
        's1s2test.py blank-local':    {'opt': 4, 'debug': 4},
        's1s2test.py blank-remote':   {'opt': 4, 'debug': 4},
        's1s2test.py nytimes-local':  {'opt': 4, 'debug': 4},
        's1s2test.py nytimes-remote': {'opt': 4, 'debug': 4},
        's1s2test.py twitter-local':  {'opt': 4, 'debug': 4},
        's1s2test.py twitter-remote': {'opt': 4, 'debug': 4},
        'smoketest.py':               {'opt': 1, 'debug': 1},
        'talostest.py tp4m-remote':   {'opt': 2, 'debug': 2},
        'talostest.py tsvg-remote':   {'opt': 2, 'debug': 2},
        'webappstartup.py':           {'opt': 6, 'debug': 6},
    },
    'nexus-6': {
        'runtestsremote.py Cw':       {'opt': 1, 'debug': 1},
        'runtestsremote.py gl':       {'opt': 60, 'debug': 60},
        'runtestsremote.py glm':      {'opt': 1, 'debug': 1},
        'runtestsremote.py C':        {'opt': 30, 'debug': 30},
        'runtestsremote.py J':        {'opt': 45, 'debug': 45},
        'runtestsremote.py M':        {'opt': 0, 'debug': 0},
        'runtestsremote.py Mdb':      {'opt': 2, 'debug': 2},
        'runtestsremote.py Mdm':      {'opt': 30, 'debug': 30},
        'runtestsremote.py Mm':       {'opt': 19, 'debug': 19},
        'runtestsremote.py Mw':       {'opt': 10, 'debug': 10},
        'runtestsremote.py Msk':      {'opt': 3, 'debug': 3},
        'runtestsremote.py Mtw':      {'opt': 2, 'debug': 2},
        'runtestsremote.py R':        {'opt': 0, 'debug': 0},
        'runtestsremote.py Rov':      {'opt': 1, 'debug': 1},
        'runtestsremote.py Rwv':      {'opt': 1, 'debug': 1},
        'runtestsremote.py rc':       {'opt': 60, 'debug': 60},
        'runtestsremote.py rca':      {'opt': 1, 'debug': 1},
        's1s2test.py blank-local':    {'opt': 5, 'debug': 5},
        's1s2test.py blank-remote':   {'opt': 5, 'debug': 5},
        's1s2test.py nytimes-local':  {'opt': 6, 'debug': 6},
        's1s2test.py nytimes-remote': {'opt': 6, 'debug': 6},
        's1s2test.py twitter-local':  {'opt': 5, 'debug': 5},
        's1s2test.py twitter-remote': {'opt': 5, 'debug': 5},
        'smoketest.py':               {'opt': 1, 'debug': 1},
        'talostest.py tp4m-remote':   {'opt': 2, 'debug': 2},
        'talostest.py tsvg-remote':   {'opt': 2, 'debug': 2},
        'webappstartup.py':           {'opt': 6, 'debug': 6},
    },
    'nexus-6p': {
        'runtestsremote.py Cw':       {'opt': 1, 'debug': 1},
        'runtestsremote.py gl':       {'opt': 60, 'debug': 60},
        'runtestsremote.py glm':      {'opt': 1, 'debug': 1},
        'runtestsremote.py C':        {'opt': 20, 'debug': 20},
        'runtestsremote.py J':        {'opt': 45, 'debug': 45},
        'runtestsremote.py M':        {'opt': 0, 'debug': 0},
        'runtestsremote.py Mdb':      {'opt': 1, 'debug': 2},
        'runtestsremote.py Mdm':      {'opt': 28, 'debug': 30},
        'runtestsremote.py Mm':       {'opt': 7, 'debug': 7},
        'runtestsremote.py Mw':       {'opt': 3, 'debug': 6},
        'runtestsremote.py Msk':      {'opt': 2, 'debug': 2},
        'runtestsremote.py Mtw':      {'opt': 1, 'debug': 1},
        'runtestsremote.py R':        {'opt': 0, 'debug': 0},
        'runtestsremote.py Rov':      {'opt': 1, 'debug': 1},
        'runtestsremote.py Rwv':      {'opt': 1, 'debug': 1},
        'runtestsremote.py rc':       {'opt': 65, 'debug': 65},
        'runtestsremote.py rca':      {'opt': 1, 'debug': 1},
        's1s2test.py blank-local':    {'opt': 4, 'debug': 4},
        's1s2test.py blank-remote':   {'opt': 4, 'debug': 4},
        's1s2test.py nytimes-local':  {'opt': 4, 'debug': 4},
        's1s2test.py nytimes-remote': {'opt': 4, 'debug': 4},
        's1s2test.py twitter-local':  {'opt': 3, 'debug': 3},
        's1s2test.py twitter-remote': {'opt': 3, 'debug': 3},
        'smoketest.py':               {'opt': 0, 'debug': 0},
        'talostest.py tp4m-remote':   {'opt': 2, 'debug': 2},
        'talostest.py tsvg-remote':   {'opt': 2, 'debug': 2},
        'webappstartup.py':           {'opt': 6, 'debug': 6},
    },
    'nexus-9': {
        'runtestsremote.py Cw':       {'opt': 1, 'debug': 1},
        'runtestsremote.py gl':       {'opt': 60, 'debug': 60},
        'runtestsremote.py glm':      {'opt': 1, 'debug': 1},
        'runtestsremote.py C':        {'opt': 190, 'debug': 190},
        'runtestsremote.py J':        {'opt': 30, 'debug': 30},
        'runtestsremote.py M':        {'opt': 0, 'debug': 0},
        'runtestsremote.py Mdb':      {'opt': 1, 'debug': 2},
        'runtestsremote.py Mdm':      {'opt': 22, 'debug': 22},
        'runtestsremote.py Mm':       {'opt': 4, 'debug': 4},
        'runtestsremote.py Mw':       {'opt': 10, 'debug': 10},
        'runtestsremote.py Msk':      {'opt': 7, 'debug': 7},
        'runtestsremote.py Mtw':      {'opt': 1, 'debug': 1},
        'runtestsremote.py R':        {'opt': 0, 'debug': 0},
        'runtestsremote.py Rov':      {'opt': 1, 'debug': 1},
        'runtestsremote.py Rwv':      {'opt': 1, 'debug': 1},
        'runtestsremote.py rc':       {'opt': 60, 'debug': 60},
        'runtestsremote.py rca':      {'opt': 1, 'debug': 1},
        's1s2test.py blank-local':    {'opt': 4, 'debug': 4},
        's1s2test.py blank-remote':   {'opt': 4, 'debug': 4},
        's1s2test.py nytimes-local':  {'opt': 4, 'debug': 4},
        's1s2test.py nytimes-remote': {'opt': 4, 'debug': 4},
        's1s2test.py twitter-local':  {'opt': 4, 'debug': 4},
        's1s2test.py twitter-remote': {'opt': 4, 'debug': 4},
        'smoketest.py':               {'opt': 1, 'debug': 1},
        'talostest.py tp4m-remote':   {'opt': 2, 'debug': 2},
        'talostest.py tsvg-remote':   {'opt': 2, 'debug': 2},
        'webappstartup.py':           {'opt': 6, 'debug': 6},
    },
    'pixel': {
        'runtestsremote.py Cw':       {'opt': 1, 'debug': 1},
        'runtestsremote.py gl':       {'opt': 60, 'debug': 60},
        'runtestsremote.py glm':      {'opt': 1, 'debug': 1},
        'runtestsremote.py C':        {'opt': 20, 'debug': 20},
        'runtestsremote.py J':        {'opt': 45, 'debug': 45},
        'runtestsremote.py M':        {'opt': 0, 'debug': 0},
        'runtestsremote.py Mdb':      {'opt': 1, 'debug': 2},
        'runtestsremote.py Mdm':      {'opt': 28, 'debug': 30},
        'runtestsremote.py Mm':       {'opt': 7, 'debug': 7},
        'runtestsremote.py Mw':       {'opt': 3, 'debug': 6},
        'runtestsremote.py Msk':      {'opt': 2, 'debug': 2},
        'runtestsremote.py Mtw':      {'opt': 1, 'debug': 1},
        'runtestsremote.py R':        {'opt': 0, 'debug': 0},
        'runtestsremote.py Rov':      {'opt': 1, 'debug': 1},
        'runtestsremote.py Rwv':      {'opt': 1, 'debug': 1},
        'runtestsremote.py rc':       {'opt': 65, 'debug': 65},
        'runtestsremote.py rca':      {'opt': 1, 'debug': 1},
        's1s2test.py blank-local':    {'opt': 4, 'debug': 4},
        's1s2test.py blank-remote':   {'opt': 4, 'debug': 4},
        's1s2test.py nytimes-local':  {'opt': 4, 'debug': 4},
        's1s2test.py nytimes-remote': {'opt': 4, 'debug': 4},
        's1s2test.py twitter-local':  {'opt': 3, 'debug': 3},
        's1s2test.py twitter-remote': {'opt': 3, 'debug': 3},
        'smoketest.py':               {'opt': 0, 'debug': 0},
        'talostest.py tp4m-remote':   {'opt': 2, 'debug': 2},
        'talostest.py tsvg-remote':   {'opt': 2, 'debug': 2},
        'webappstartup.py':           {'opt': 6, 'debug': 6},
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
            repo_name_build_type_parts = repo_name_build_type.split('-')
            repo_name = '-'.join(repo_name_build_type_parts[0:-1])
            build_type = repo_name_build_type_parts[-1]
            build_count = builds[repo_name]
            #print "repo %s count %s" % (repo_name_build_type, build_count)
            if repo_name_build_type not in device_repos:
                #print "adding install time %s" % install_time
                device_repos[repo_name_build_type] = 1
                repo_time += install_time
            repo_time += device_times[device_type][test_name][build_type]
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




