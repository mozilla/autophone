# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import ConfigParser
import base64
import datetime
import ftplib
import json
import logging
import math
import os
import pytz
import re
import shutil
import tempfile
import time
import urllib
import urlparse
import zipfile

logger = logging.getLogger('autophone.builds')

class Nightly(object):

    def __init__(self, repos, buildtypes):
        self.repos = repos
        self.buildtypes = buildtypes
        self.nightly_dirnames = [(re.compile('(.*)-%s-android$'
                                             % repo)) for repo in repos]

    def nightly_ftpdir(self, year, month):
        return 'ftp://ftp.mozilla.org/pub/mobile/nightly/%d/%02d/' % (year,
                                                                      month)

    def ftpdirs(self, start_time, end_time):
        logger.debug('Getting ftp dirs...')
        dirs = []
        y = start_time.year
        m = start_time.month
        while y < end_time.year or (y == end_time.year and m <= end_time.month):
            dirs.append(self.nightly_ftpdir(y, m))
            if m == 12:
                y += 1
                m = 1
            else:
                m += 1
        logger.debug('Searching these ftp dirs: %s' % ', '.join(dirs))
        return dirs

    def build_info_from_ftp(self, ftpline):
        srcdir = ftpline.split(' ')[-1].strip()
        build_time = None
        dirnamematch = None
        for r in self.nightly_dirnames:
            dirnamematch = r.match(srcdir)
            if dirnamematch:
                break
        if dirnamematch:
            build_time = datetime.datetime.strptime(dirnamematch.group(1),
                                                    '%Y-%m-%d-%H-%M-%S')
            build_time = build_time.replace(tzinfo=pytz.timezone('US/Pacific'))
        return (srcdir, build_time)

    def build_date_from_url(self, url):
        # nightly urls are of the form
        #  ftp://ftp.mozilla.org/pub/mobile/nightly/<year>/<month>/<year>-
        #    <month>-<day>-<hour>-<minute>-<second>-<repo>-android(-armv6)?/
        #    <buildfile>
        m = re.search('nightly\/[\d]{4}\/[\d]{2}\/([\d]{4}-[\d]{2}-[\d]{2}-[\d]{2}-[\d]{2}-[\d]{2})-', url)
        if not m:
            return None
        return datetime.datetime.strptime(m.group(1), '%Y-%m-%d-%H-%M-%S')


class Tinderbox(object):

    main_ftp_url = 'ftp://ftp.mozilla.org/pub/mozilla.org/mobile/tinderbox-builds/'

    def __init__(self, repos, buildtypes):
        self.repos = repos
        self.buildtypes = buildtypes

    def ftpdirs(self, start_time, end_time):
        # FIXME: Can we be certain that there's only one buildID (unique
        # timestamp) regardless of repo (at least m-i vs m-c)?
        dirnames = [('%s%s-android/' % (self.main_ftp_url, repo)) for repo in self.repos]

        return dirnames

    def build_info_from_ftp(self, ftpline):
        srcdir = ftpline.split()[8].strip()
        build_time = datetime.datetime.fromtimestamp(int(srcdir), pytz.timezone('US/Pacific'))
        return (srcdir, build_time)

    def build_date_from_url(self, url):
        # tinderbox urls are of the form
        #   ftp://ftp.mozilla.org/pub/mozilla.org/mobile/tinderbox-builds/
        #     <repo>-android/<build timestamp>/<buildfile>
        m = re.search('tinderbox-builds\/.*-android\/([\d]+)\/', url)
        logger.debug('build_date_from_url: url: %s, match: %s' % (url, m))
        if not m:
            return None
        logger.debug('build_date_from_url: match.group(1): %s' % m.group(1))
        return datetime.datetime.fromtimestamp(int(m.group(1)),
                                               pytz.timezone('US/Pacific'))


class BuildCacheException(Exception):
    pass


class BuildCache(object):

    MAX_NUM_BUILDS = 20
    EXPIRE_AFTER_SECONDS = 60 * 60 * 24

    class FtpLineCache(object):
        def __init__(self):
            self.lines = []

        def __call__(self, line):
            self.lines.append(line)

    def __init__(self, repos, buildtypes,
                 cache_dir='builds', override_build_dir=None,
                 enable_unittests=False):
        self.repos = repos
        self.buildtypes = buildtypes
        self.cache_dir = cache_dir
        self.enable_unittests = enable_unittests
        self.override_build_dir = override_build_dir
        if override_build_dir:
            if not os.path.exists(override_build_dir):
                raise BuildCacheException('Override Build Directory does not exist')

            build_path = os.path.join(override_build_dir, 'build.apk')
            if not os.path.exists(build_path):
                raise BuildCacheException('Override Build Directory %s does not contain a build.apk.' %
                                          override_build_dir)

            tests_path = os.path.join(override_build_dir, 'tests')
            if self.enable_unittests and not os.path.exists(tests_path):
                raise BuildCacheException('Override Build Directory %s does not contain a tests directory.' %
                                          override_build_dir)
        if not os.path.exists(self.cache_dir):
            os.mkdir(self.cache_dir)

    def build_location(self, s):
        if 'nightly' in s:
            return Nightly(self.repos, self.buildtypes)
        if 'tinderbox' in s:
            return Tinderbox(self.repos, self.buildtypes)
        return None

    def find_latest_build(self, build_location_name='nightly'):
        window = datetime.timedelta(days=3)
        now = datetime.datetime.now()
        builds = self.find_builds(now - window, now, build_location_name)
        if not builds:
            logger.error('Could not find any nightly builds in the last '
                         '%d days!' % window.days)
            return None
        builds.sort()
        return builds[-1]

    def find_builds(self, start_time, end_time, build_location_name='nightly'):
        logger.debug('Finding most recent build between %s and %s...' %
                     (start_time, end_time))
        build_location = self.build_location(build_location_name)
        if not build_location:
            logger.error('unsupported build_location "%s"' % build_location_name)
            return []

        if not start_time.tzinfo:
            start_time = start_time.replace(tzinfo=pytz.timezone('US/Pacific'))
        if not end_time.tzinfo:
            end_time = end_time.replace(tzinfo=pytz.timezone('US/Pacific'))

        builds = []
        fennecregex = re.compile("fennec.*\.android-arm\.apk")

        for d in build_location.ftpdirs(start_time, end_time):
            url = urlparse.urlparse(d)
            logger.debug('Logging into %s...' % url.netloc)
            f = ftplib.FTP(url.netloc)
            f.login()
            logger.debug('Looking for builds in %s...' % url.path)
            lines = self.FtpLineCache()
            f.dir(url.path, lines)
            file('lines.out', 'w').write('\n'.join(lines.lines))
            for line in lines.lines:
                srcdir, build_time = build_location.build_info_from_ftp(line)

                if not build_time:
                    continue

                if build_time and (build_time < start_time or
                                   build_time > end_time):
                    continue

                newpath = url.path + srcdir
                lines2 = self.FtpLineCache()
                f.dir(newpath, lines2)
                for l2 in lines2.lines:
                    filename = l2.split(' ')[-1].strip()
                    if fennecregex.match(filename):
                        buildurl = url.scheme + '://' + url.netloc + newpath + "/" + filename
                        builds.append(buildurl)
        if not builds:
            logger.error('No builds found.')
        return builds

    def build_date(self, url):
        build_location = self.build_location(url)
        builddate = None
        if build_location:
            builddate = build_location.build_date_from_url(url)
        if not builddate:
            logger.error('bad URL "%s"' % url)
        return builddate

    def get(self, buildurl, force=False):
        """Returns info on a cached build, fetching it if necessary.
        Returns a dict with a boolean 'success' item.
        If 'success' is False, the dict also contains an 'error' item holding a
        descriptive string.
        If 'success' is True, the dict also contains a 'metadata' item, which is
        a dict of build metadata.  The path to the build is the
        'cache_build_dir' item, which is a directory containing build.apk,
        symbols/, and, if self.enable_unittests is true, robocop.apk and tests/.
        If not found, fetches them, assuming a standard file structure.
        Cleans the cache before getting started.
        If self.override_build_dir is set, 'cache_build_dir' is set to
        that value without verifying the contents nor fetching anything (though
        it will still try to open build.apk to read in the metadata).
        See BuildCache.build_metadata() for the other metadata items.
        """
        if self.override_build_dir:
            return {'success': True,
                    'metadata': self.build_metadata(self.override_build_dir)}
        build_dir = base64.b64encode(buildurl)
        self.clean_cache([build_dir])
        cache_build_dir = os.path.join(self.cache_dir, build_dir)
        build_path = os.path.join(cache_build_dir, 'build.apk')
        if not os.path.exists(cache_build_dir):
            os.makedirs(cache_build_dir)

        # build
        try:
            download_build = (force or not os.path.exists(build_path) or
                              zipfile.ZipFile(build_path).testzip() is not None)
        except zipfile.BadZipfile:
            download_build = True
        if download_build:
            # retrieve to temporary file then move over, so we don't end
            # up with half a file if it aborts
            tmpf = tempfile.NamedTemporaryFile(delete=False)
            tmpf.close()
            try:
                urllib.urlretrieve(buildurl, tmpf.name)
            except IOError:
                os.unlink(tmpf.name)
                err = 'IO Error retrieving build: %s.' % buildurl
                logger.exception(err)
                return {'success': False, 'error': err}
            shutil.move(tmpf.name, build_path)
        file(os.path.join(cache_build_dir, 'lastused'), 'w')

        # symbols
        symbols_path = os.path.join(cache_build_dir, 'symbols')
        if force or not os.path.exists(symbols_path):
            tmpf = tempfile.NamedTemporaryFile(delete=False)
            tmpf.close()
            # XXX: assumes fixed buildurl-> symbols_url mapping
            symbols_url = re.sub('.apk$', '.crashreporter-symbols.zip', buildurl)
            try:
                urllib.urlretrieve(symbols_url, tmpf.name)
                symbols_zipfile = zipfile.ZipFile(tmpf.name)
                symbols_zipfile.extractall(symbols_path)
                symbols_zipfile.close()
            except IOError, ioerror:
                if '550 Failed to change directory' in ioerror.strerror.strerror.message:
                    logger.info('No symbols found: %s.' % symbols_url)
                else:
                    logger.exception('IO Error retrieving symbols: %s.' % symbols_url)
            except zipfile.BadZipfile:
                logger.info('Ignoring zipfile.BadZipfile Error retrieving symbols: %s.' % symbols_url)
                try:
                    with open(tmpf.name, 'r') as badzipfile:
                        logger.debug(badzipfile.read())
                except:
                    pass
            os.unlink(tmpf.name)

        # tests
        if self.enable_unittests:
            tests_path = os.path.join(cache_build_dir, 'tests')
            if force or not os.path.exists(tests_path):
                tmpf = tempfile.NamedTemporaryFile(delete=False)
                tmpf.close()
                # XXX: assumes fixed buildurl-> tests_url mapping
                tests_url = re.sub('.apk$', '.tests.zip', buildurl)
                try:
                    urllib.urlretrieve(tests_url, tmpf.name)
                except IOError:
                    os.unlink(tmpf.name)
                    err = 'IO Error retrieving tests: %s.' % tests_url
                    logger.exception(err)
                    return {'success': False, 'error': err}
                tests_zipfile = zipfile.ZipFile(tmpf.name)
                tests_zipfile.extractall(tests_path)
                tests_zipfile.close()
                os.unlink(tmpf.name)
                # XXX: assumes fixed buildurl-> robocop mapping
                robocop_url = urlparse.urljoin(buildurl, 'robocop.apk')
                robocop_path = os.path.join(cache_build_dir, 'robocop.apk')
                tmpf = tempfile.NamedTemporaryFile(delete=False)
                tmpf.close()
                try:
                    urllib.urlretrieve(robocop_url, tmpf.name)
                except IOError:
                    os.unlink(tmpf.name)
                    err = 'IO Error retrieving robocop.apk: %s.' % robocop_url
                    logger.exception(err)
                    return {'success': False, 'error': err}
                shutil.move(tmpf.name, robocop_path)
                # XXX: assumes fixed buildurl-> fennec_ids.txt mapping
                fennec_ids_url = urlparse.urljoin(buildurl, 'fennec_ids.txt')
                fennec_ids_path = os.path.join(cache_build_dir, 'fennec_ids.txt')
                tmpf = tempfile.NamedTemporaryFile(delete=False)
                tmpf.close()
                try:
                    urllib.urlretrieve(fennec_ids_url, tmpf.name)
                except IOError:
                    os.unlink(tmpf.name)
                    err = 'IO Error retrieving fennec_ids.txt: %s.' % \
                        fennec_ids_url
                    logger.exception(err)
                    return {'success': False, 'error': err}
                shutil.move(tmpf.name, fennec_ids_path)

        return {'success': True,
                'metadata': self.build_metadata(cache_build_dir)}

    def clean_cache(self, preserve=[]):
        def lastused_path(d):
            return os.path.join(self.cache_dir, d, 'lastused')

        def keep_build(d):
            if preserve and d in preserve:
                # specifically keep this build
                return True
            if not os.path.exists(lastused_path(d)):
                # probably not a build dir
                return True
            if ((datetime.datetime.now() -
                 datetime.datetime.fromtimestamp(os.stat(lastused_path(d)).st_mtime) <=
                 datetime.timedelta(microseconds=1000 * 1000 * self.EXPIRE_AFTER_SECONDS))):
                # too new
                return True
            return False

        builds = [(x, os.stat(lastused_path(x)).st_mtime) for x in
                  os.listdir(self.cache_dir) if not keep_build(x)]
        builds.sort(key=lambda x: x[1])
        while len(builds) > self.MAX_NUM_BUILDS:
            b = builds.pop(0)[0]
            logger.info('Expiring %s' % b)
            shutil.rmtree(os.path.join(self.cache_dir, b))

    def build_metadata(self, build_dir):
        build_metadata_path = os.path.join(build_dir, 'metadata.json')
        if os.path.exists(build_metadata_path):
            try:
                return json.loads(file(build_metadata_path).read())
            except (ValueError, IOError):
                pass
        tmpdir = tempfile.mkdtemp()
        try:
            build_path = os.path.join(build_dir, 'build.apk')
            apkfile = zipfile.ZipFile(build_path)
            apkfile.extract('application.ini', tmpdir)
        except zipfile.BadZipfile:
            # we should have already tried to redownload bad zips, so treat
            # this as fatal.
            logger.error('%s is a bad apk; aborting job.' % build_path)
            shutil.rmtree(tmpdir)
            return None
        cfg = ConfigParser.RawConfigParser()
        cfg.read(os.path.join(tmpdir, 'application.ini'))
        rev = cfg.get('App', 'SourceStamp')
        ver = cfg.get('App', 'Version')
        repo = cfg.get('App', 'SourceRepository')
        buildid = cfg.get('App', 'BuildID')
        blddate = datetime.datetime.strptime(buildid,
                                             '%Y%m%d%H%M%S')
        procname = ''
        if repo == 'http://hg.mozilla.org/mozilla-central':
            tree = 'mozilla-central'
            procname = 'org.mozilla.fennec'
        elif repo == 'http://hg.mozilla.org/integration/mozilla-inbound':
            tree = 'mozilla-inbound'
            procname = 'org.mozilla.fennec'
        elif repo == 'http://hg.mozilla.org/releases/mozilla-aurora':
            tree = 'mozilla-aurora'
            procname = 'org.mozilla.fennec_aurora'
        elif repo == 'http://hg.mozilla.org/releases/mozilla-beta':
            tree = 'mozilla-beta'
            procname = 'org.mozilla.firefox'

        metadata = {'cache_build_dir': build_dir,
                    'tree': tree,
                    'blddate': math.trunc(time.mktime(blddate.timetuple())),
                    'buildid': buildid,
                    'revision': rev,
                    'androidprocname': procname,
                    'version': ver,
                    'bldtype': 'opt'}
        shutil.rmtree(tmpdir)
        file(build_metadata_path, 'w').write(json.dumps(metadata))
        return metadata
