# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import ConfigParser

# command line options
CLEAR_CACHE = 'clear_cache'
IPADDR = 'ipaddr'
PORT = 'port'
CACHEFILE = 'cachefile'
LOGFILE = 'logfile'
LOGLEVEL = 'loglevel'
TEST_PATH = 'test_path'
EMAILCFG = 'emailcfg'
ENABLE_PULSE = 'enable_pulse'
ENABLE_UNITTESTS = 'enable_unittests'
CACHE_DIR = 'cache_dir'
OVERRIDE_BUILD_DIR = 'override_build_dir'
REPOS = 'repos'
BUILDTYPES = 'buildtypes'
BUILD_CACHE_PORT = 'build_cache_port'

# ini file internal options
BUILD_CACHE_SIZE = 'build_cache_size'
BUILD_CACHE_EXPIRES = 'build_cache_expires'
DEVICEMANAGER_RETRY_LIMIT = 'devicemanager_retry_limit'
DEVICEMANAGER_SETTLING_TIME = 'devicemanager_settling_time'
PHONE_RETRY_LIMIT = 'phone_retry_limit'
PHONE_RETRY_WAIT = 'phone_retry_wait'
PHONE_MAX_REBOOTS = 'phone_max_reboots'
PHONE_PING_INTERVAL = 'phone_ping_interval'
PHONE_COMMAND_QUEUE_TIMEOUT = 'phone_command_queue_timeout'
PHONE_CRASH_WINDOW = 'phone_crash_window'
PHONE_CRASH_LIMIT = 'phone_crash_limit'


# application command line options
CMD_OPTION_NAMES = {
    CLEAR_CACHE: 'getboolean',
    IPADDR: 'get',
    PORT: 'getint',
    CACHEFILE: 'get',
    LOGFILE: 'get',
    LOGLEVEL: 'get',
    TEST_PATH: 'get',
    EMAILCFG: 'get',
    ENABLE_PULSE: 'getboolean',
    ENABLE_UNITTESTS: 'getboolean',
    CACHE_DIR: 'get',
    OVERRIDE_BUILD_DIR: 'get',
    REPOS: 'get',
    BUILDTYPES: 'get',
    BUILD_CACHE_PORT: 'getint'
}

# application configuration settings.
INI_OPTION_NAMES = {
    BUILD_CACHE_SIZE: 'getint',
    BUILD_CACHE_EXPIRES: 'getint',
    DEVICEMANAGER_RETRY_LIMIT: 'getint',
    DEVICEMANAGER_SETTLING_TIME: 'getint',
    PHONE_RETRY_LIMIT: 'getint',
    PHONE_RETRY_WAIT: 'getint',
    PHONE_MAX_REBOOTS: 'getint',
    PHONE_PING_INTERVAL: 'getint',
    PHONE_COMMAND_QUEUE_TIMEOUT: 'getint',
    PHONE_CRASH_WINDOW: 'getint',
    PHONE_CRASH_LIMIT: 'getint'
}

