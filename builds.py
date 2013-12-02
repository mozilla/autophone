# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import ConfigParser
import base64
import datetime
import httplib
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

from bs4 import BeautifulSoup
import httplib2

logger = logging.getLogger('autophone.builds')

class Nightly(object):

    def __init__(self, repos, buildtypes):
        self.repos = repos
        self.buildtypes = buildtypes
        self.nightly_dirnames = [(re.compile('(.*)-%s-android(-armv6|-x86)?$'
                                             % repo)) for repo in repos]

    def get_search_directories(self, start_time, end_time):
        logger.debug('Nightly:get_search_directories(%s, %s)' % (start_time, end_time))
        dirs = []
        y = start_time.year
        m = start_time.month
        while y < end_time.year or (y == end_time.year and m <= end_time.month):
            dirs.append('http://ftp.mozilla.org/pub/mobile/nightly/%d/%02d/' % (y, m))
            if m == 12:
                y += 1
                m = 1
            else:
                m += 1
        logger.debug('Searching these http dirs: %s' % ', '.join(dirs))
        return dirs

    def build_time_from_directory_name(self, directory_name):
        logger.debug('Nightly:build_time_from_directory_name(%s)' % directory_name)
        build_time = None
        dirnamematch = None
        for r in self.nightly_dirnames:
            dirnamematch = r.match(directory_name)
            if dirnamematch:
                break
        if dirnamematch:
            build_time = datetime.datetime.strptime(dirnamematch.group(1),
                                                    '%Y-%m-%d-%H-%M-%S')
            build_time = build_time.replace(tzinfo=pytz.timezone('US/Pacific'))
        logger.debug('Nightly:build_time_from_directory_name: (%s, %s)' % (directory_name, build_time))
        return build_time


class Tinderbox(object):

    main_http_url = 'http://ftp.mozilla.org/pub/mozilla.org/mobile/tinderbox-builds/'

    def __init__(self, repos, buildtypes):
        self.repos = repos
        self.buildtypes = buildtypes

    def get_search_directories(self, start_time, end_time):
        logger.debug('Tinderbox:get_search_directories(%s, %s)' % (start_time, end_time))
        # FIXME: Can we be certain that there's only one buildID (unique
        # timestamp) regardless of repo (at least m-i vs m-c)?
        dirnames = []
        for repo in self.repos:
            for arch in ('', '-armv6', '-x86'):
                dirnames.append('%s%s-android%s/' % (self.main_http_url, repo, arch))

        return dirnames

    def build_time_from_directory_name(self, directory_name):
        logger.debug('Tinderbox:build_time_from_directory_name(%s)' % directory_name)
        try:
            build_time = datetime.datetime.fromtimestamp(int(directory_name), pytz.timezone('US/Pacific'))
        except ValueError:
            build_time = None
        return build_time


class BuildCacheException(Exception):
    pass


class BuildCache(object):

    MAX_NUM_BUILDS = 20
    EXPIRE_AFTER_SECONDS = 60 * 60 * 24

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

    def find_latest_builds(self, build_location_name='nightly'):
        window = datetime.timedelta(days=3)
        now = datetime.datetime.now()
        builds = self.find_builds(now - window, now, build_location_name)
        if not builds:
            logger.error('Could not find any nightly builds in the last '
                         '%d days!' % window.days)
            return None
        builds.sort()
        # Return the most recent builds for each of the architectures.
        # The phones will weed out the unnecessary architectures.
        builds.reverse()
        multiarch_builds = []
        for arch in ('armv6', 'i386', 'arm'):
            for build in builds:
                if arch in build:
                    multiarch_builds.append(build)
                    break
            builds = [build for build in builds if arch not in build]

        return multiarch_builds

    def find_builds(self, start_time, end_time, build_location_name='nightly'):
        logger.debug('Finding most recent build between %s and %s...' %
                     (start_time, end_time))
        http = httplib2.Http()
        build_location = self.build_location(build_location_name)
        if not build_location:
            logger.error('unsupported build_location "%s"' % build_location_name)
            return []

        if not start_time.tzinfo:
            start_time = start_time.replace(tzinfo=pytz.timezone('US/Pacific'))
        if not end_time.tzinfo:
            end_time = end_time.replace(tzinfo=pytz.timezone('US/Pacific'))

        builds = []
        fennecregex = re.compile("fennec.*\.android-(arm|arm-armv6|i386)\.apk")

        for d in build_location.get_search_directories(start_time, end_time):
            logger.debug('Checking %s...' % d)
            directory_response, directory_content = http.request(d, "GET")
            if directory_response.status != 200:
                logger.warning("Unable to get directory: %s : %s" % (
                    d, httplib.responses[directory_response.status]))
                continue
            directory_soup = BeautifulSoup(directory_content)
            for directory_link in directory_soup.findAll('a'):
                directory_name = directory_link.get_text().rstrip('/')
                directory_href = '%s%s/' % (d, directory_name)
                logger.debug('find_builds: directory: href: %s, name: %s' % (
                    directory_href, directory_name))
                build_time = build_location.build_time_from_directory_name(directory_name)

                if not build_time:
                    continue

                if build_time and (build_time < start_time or
                                   build_time > end_time):
                    continue

                builddir_response, builddir_content = http.request(directory_href)
                if builddir_response.status != 200:
                    logger.warning("Unable to get build directory: %s, %s" % (
                        directory_href, httplib.responses[builddir_response.status]))
                    continue

                builddir_soup = BeautifulSoup(builddir_content)
                for build_link in builddir_soup.findAll('a'):
                    filename = build_link.get_text()
                    logger.debug('find_builds: filename: %s' % filename)
                    if fennecregex.match(filename):
                        builds.append('%s%s' % (directory_href, filename))
        if not builds:
            logger.error('No builds found.')
        return builds

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
                if '550 Failed to change directory' in str(ioerror):
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
        tree = None
        procname = None
        for temp_tree, temp_procname in (
                ('mozilla-central', 'org.mozilla.fennec'),
                ('mozilla-inbound', 'org.mozilla.fennec'),
                ('mozilla-aurora', 'org.mozilla.fennec_aurora'),
                ('mozilla-beta', 'org.mozilla.firefox')):
            if temp_tree in repo:
                tree = temp_tree
                procname = temp_procname
                break
        if not tree:
            raise BuildCacheException('build %s contains an unknown SourceRepository %s' %
                                      (apkfile, repo))

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
