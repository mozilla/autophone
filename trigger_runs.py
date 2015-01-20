# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import datetime
import json
import logging
import re
import socket
import sys
from multiprocessinghandlers import MultiprocessingTimedRotatingFileHandler

import pytz

import builds

def from_iso_date_or_datetime(s):
    datefmt = '%Y-%m-%d'
    datetimefmt = datefmt + 'T%H:%M:%S'
    try:
        d = datetime.datetime.strptime(s, datetimefmt)
    except ValueError:
        d = datetime.datetime.strptime(s, datefmt)
    return d


def command_str(build, tests, devices):
    job_data = {'build': build,
                'tests': tests or [],
                'devices': devices or []}
    s = 'triggerjobs %s' % json.dumps(job_data)
    return s


def main(args, options):
    # Attempt to connect to the Autophone server early, so we don't
    # waste time fetching builds if the server is not available.
    print 'Connecting to autophone server...'
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.connect((options.ip, options.port))
    except socket.error, e:
        print "Unable to contact Autophone server. %s" % e
        sys.exit(1)

    loglevel = e = None
    try:
        loglevel = getattr(logging, options.loglevel_name)
    except AttributeError, e:
        pass
    finally:
        if e or logging.getLevelName(loglevel) != options.loglevel_name:
            print 'Invalid log level %s' % options.loglevel_name
            return errno.EINVAL

    logger = logging.getLogger('autophone.builds')
    logger.setLevel(loglevel)
    filehandler = MultiprocessingTimedRotatingFileHandler(options.logfile,
                                                          when='midnight',
                                                          backupCount=7)
    fileformatstring = ('%(asctime)s|%(levelname)s'
                        '|builds|%(message)s')
    fileformatter = logging.Formatter(fileformatstring)
    filehandler.setFormatter(fileformatter)
    logger.addHandler(filehandler)

    logger.info('Looking for builds...')
    product = 'fennec'
    build_platforms = ['android',
                       'android-api-9',
                       'android-api-10',
                       'android-api-11',
                       'android-x86']
    buildfile_ext = '.apk'

    cache = builds.BuildCache(
        options.repos, options.buildtypes,
        product, build_platforms,
        buildfile_ext)

    build_urls = []
    if options.build_url:
        build_urls = cache.find_builds_by_directory(options.build_url)
    elif not args:
        build_urls = cache.find_builds_by_revision(
            options.first_revision, options.last_revision,
            options.build_location)
    elif args[0] == 'latest':
        build_urls = cache.find_latest_builds(options.build_location)
    else:
        if re.match('\d{14}', args[0]):
            # build id
            build_time = datetime.datetime.strptime(args[0], '%Y%m%d%H%M%S')
            start_time = build_time
            end_time = build_time
        else:
            start_time = from_iso_date_or_datetime(args[0])
            if len(args) > 1:
                end_time = from_iso_date_or_datetime(args[1])
            else:
                end_time = datetime.datetime.now()
        if not start_time.tzinfo:
            start_time = start_time.replace(tzinfo=pytz.timezone('US/Pacific'))
        if not end_time.tzinfo:
            end_time = end_time.replace(tzinfo=pytz.timezone('US/Pacific'))
        build_urls = cache.find_builds_by_time(
            start_time, end_time, options.build_location)

    if not build_urls:
        return 1
    commands = [command_str(b, options.tests, options.devices)
                for b in build_urls]
    commands.append('exit')
    logger.info('- %s' % s.recv(1024).strip())
    for c in commands:
        sc = '%s' % c
        logger.info(sc)
        print(sc)
        s.sendall(c + '\n')
        sr = '- %s' % s.recv(1024).strip()
        logger.info(sr)
        print(sr)
    return 0


if __name__ == '__main__':
    import errno
    from optparse import OptionParser

    usage = '''%prog [options] <datetime, date/datetime, or date/datetime range>
Triggers one or more test runs.

The argument(s) should be one of the following:
- a build ID, e.g. 20120403063158
- a date/datetime, e.g. 2012-04-03 or 2012-04-03T06:31:58
- a date/datetime range, e.g. 2012-04-03T06:31:58 2012-04-05
- the string "latest"

If a build ID is given, a test run is initiated for that, and only that,
particular build.

If a single date or datetime is given, test runs are initiated for all builds
with build IDs between the given date/datetime and now.

If a date/datetime range is given, test runs are initiated for all builds
with build IDs in the given range.

If "latest" is given, test runs are initiated for the most recent build.'''
    parser = OptionParser(usage=usage)
    parser.add_option('-i', '--ip', action='store', type='string', dest='ip',
                      default='127.0.0.1',
                      help='IP address of autophone controller; defaults to localhost')
    parser.add_option('-p', '--port', action='store', type='int', dest='port',
                      default=28001,
                      help='port of autophone controller; defaults to 28001')
    parser.add_option('-b', '--build-location', action='store', type='string',
                      dest='build_location', default='nightly',
                      help='build location to search for builds, defaults to nightly;'
                      ' can be "tinderbox" or "inboundarchive" for both m-c and m-i')
    parser.add_option('--logfile', action='store', type='string',
                      dest='logfile', default='autophone.log',
                      help='Log file to store build system logs, '
                      'defaults to autophone.log')
    parser.add_option('--loglevel', action='store', type='string',
                      dest='loglevel_name', default='INFO',
                      help='Log level - ERROR, WARNING, DEBUG, or INFO, '
                      'defaults to INFO')
    parser.add_option('--repo',
                      dest='repos',
                      action='append',
                      help='The repos to test. '
                      'One of mozilla-central, mozilla-inbound, mozilla-aurora, '
                      'mozilla-beta, fx-team, b2g-inbound, try. To specify multiple '
                      'repos, specify them with additional --repo options. '
                      'Defaults to mozilla-central.')
    parser.add_option('--buildtype',
                      dest='buildtypes',
                      action='append',
                      help='The build types to test. '
                      'One of opt or debug. To specify multiple build types, '
                      'specify them with additional --buildtype options. '
                      'Defaults to opt.')
    parser.add_option('--first-revision', action='store', type='string',
                      dest='first_revision',
                      help='revision of first build; must match a build;'
                      ' last revision must also be specified;'
                      ' can not be used with date arguments.')
    parser.add_option('--last-revision', action='store', type='string',
                      dest='last_revision',
                      help='revision of second build; must match a build;'
                      ' first revision must also be specified;'
                      ' can not be used with date arguments.')
    parser.add_option('--build-url', action='store', type='string',
                      dest='build_url',
                      help='url of build to test; may be an http or file schema;'
                      ' --repo must be specified if the build was not built from'
                      ' the mozilla-central repository.')
    parser.add_option('--test',
                      dest='tests',
                      action='append',
                      help='Test to be executed by the job.  Defaults to all '
                      'if not specified. Can be specified multiple times. '
                      'See Android-Only Unittest Suites at '
                      'http://trychooser.pub.build.mozilla.org/ for supported '
                      'test identifiers.')
    parser.add_option('--device',
                      dest='devices',
                      action='append',
                      help='Device on which to run the job.  Defaults to all '
                      'if not specified. Can be specified multiple times.')
    (options, args) = parser.parse_args()
    if (len(args) > 2 or
        (options.first_revision and not options.last_revision) or
        (not options.first_revision and options.last_revision) or
        (options.first_revision and len(args) > 0) or
        (options.build_url and len(args) > 0)):
        parser.print_help()
        sys.exit(errno.EINVAL)

    if not options.repos:
        options.repos = ['mozilla-central']

    if not options.buildtypes:
        options.buildtypes = ['opt']

    sys.exit(main(args, options))
