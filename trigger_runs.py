# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import datetime
import errno
import json
import logging
import logging.handlers
import multiprocessing
import socket
import sys

import builds
import build_dates
import utils

import pytz

PACIFIC = pytz.timezone('US/Pacific')

def command_str(build, test_names, devices):
    # Dates are not json serializable. We don't need the build date in
    # the trigger_jobs in autophone, so let's just delete it.
    del build['date']
    build['changeset_dirs'] = build['changeset_dirs']
    job_data = {
        'build_data': build,
        'test_names': test_names or [],
        'devices': devices or [],
    }
    s = 'autophone-triggerjobs %s' % json.dumps(job_data)
    return s


def trigger_runs(args, options):
    # Attempt to connect to the Autophone server early, so we don't
    # waste time fetching builds if the server is not available.
    logging.basicConfig(level=logging.INFO)
    logging.info('Connecting to autophone server...')
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

    logging.captureWarnings(True)

    # First get the root logger and remove the default stream handler.
    logger = logging.getLogger('')
    logger.setLevel(loglevel)
    for handler in logger.handlers:
        handler.flush()
        handler.close()
        logger.removeHandler(handler)

    filehandler = logging.handlers.TimedRotatingFileHandler(options.logfile,
                                                            when='midnight',
                                                            backupCount=7)
    fileformatter = logging.Formatter(utils.getLoggerFormatString(loglevel))
    filehandler.setFormatter(fileformatter)
    logger.addHandler(filehandler)

    # Now use the trigger_runs logger
    logger = utils.getLogger()
    logger.info('Looking for builds...')
    product = 'fennec'
    build_platforms = [
        'android-api-15',
        'android-api-16',
        'android-api-15-gradle',
        'android-api-16-gradle'
    ]
    buildfile_ext = '.apk'

    cache = builds.BuildCache(
        options.repos, options.buildtypes,
        product, build_platforms,
        buildfile_ext)

    matching_builds = []
    if options.build_url:
        logger.debug('cache.find_builds_by_directory(%s)', options.build_url)
        matching_builds = cache.find_builds_by_directory(options.build_url)
    elif not args:
        logger.debug('cache.find_builds_by_revision(%s, %s, %s)',
                     options.first_revision, options.last_revision,
                     options.build_location)
        matching_builds = cache.find_builds_by_revision(
            options.first_revision, options.last_revision,
            options.build_location)
    elif args[0] == 'latest':
        logger.debug('cache.find_latest_builds(%s)', options.build_location)
        matching_builds = cache.find_latest_builds(options.build_location)
    else:
        date_format, start_time = build_dates.parse_datetime(args[0])
        if date_format == build_dates.BUILDID:
            end_time = start_time
        else:
            if len(args) > 1:
                date_format, end_time = build_dates.parse_datetime(args[1])
            else:
                end_time = datetime.datetime.now(tz=PACIFIC)
        logger.debug('cache.find_builds_by_time(%s, %s, %s)',
                     start_time, end_time, options.build_location)
        matching_builds = cache.find_builds_by_time(
            start_time, end_time, options.build_location)

    if not matching_builds:
        return 1
    commands = [command_str(b, options.test_names, options.devices)
                for b in matching_builds]
    commands.append('exit')
    logger.info('- %s', s.recv(1024).strip())
    for c in commands:
        sc = '%s' % c
        logger.info(sc)
        print sc
        s.sendall(c + '\n')
        sr = '- %s' % s.recv(1024).strip()
        logger.info(sr)
        print sr
    return 0


def main():
    from optparse import OptionParser

    # Set our process name which we will use in obtaining
    # the appropriate loggers when necessary.
    multiprocessing.current_process().name = 'trigger_runs'


    usage = '''%prog [options] <datetime, date/datetime, or date/datetime range>
Triggers one or more test runs.

The argument(s) should be one of the following:
- a build ID, e.g. 20120403063158
- a date/datetime, e.g. 2012-04-03 or 2012-04-03T06:31:58
- a date/datetime range, e.g. 2012-04-03T06:31:58 2012-04-05
- the string "latest"

The build ID and dates are specified in the UTC timezone.

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
                      ' or can be "tinderbox" for both m-c and m-i')
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
                      'One of b2g-inbound, fx-team, mozilla-aurora, '
                      'mozilla-beta, mozilla-central, mozilla-inbound, '
                      'autoland, mozilla-release, try. To specify multiple '
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
                      dest='test_names',
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
    if len(args) > 2 or (options.first_revision and not options.last_revision) or \
       (options.build_url and len(args) > 0):
        parser.print_help()
        sys.exit(errno.EINVAL)

    if not options.repos:
        options.repos = ['mozilla-central']

    if not options.buildtypes:
        options.buildtypes = ['opt']

    sys.exit(trigger_runs(args, options))


if __name__ == '__main__':
    main()
