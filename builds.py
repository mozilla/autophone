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
import re
import shutil
import tempfile
import time
import urllib
import urllib2
import urlparse
import zipfile

from bs4 import BeautifulSoup

from build_dates import (TIMESTAMP, DIRECTORY_DATE, DIRECTORY_DATETIME,
                         parse_datetime, convert_datetime_to_string,
                         set_time_zone, convert_buildid_to_date,
                         convert_timestamp_to_date)

logger = logging.getLogger('autophone.builds')

repo_urls = {
    'fx-team': 'http://hg.mozilla.org/integration/fx-team/',
    'mozilla-central': 'http://hg.mozilla.org/mozilla-central/',
    'mozilla-aurora': 'http://hg.mozilla.org/releases/mozilla-aurora/',
    'mozilla-beta': 'http://hg.mozilla.org/releases/mozilla-beta/',
    'mozilla-inbound': 'http://hg.mozilla.org/integration/mozilla-inbound/'}

urls_repos = dict([(url, repo) for repo, url in repo_urls.items()])

# lifted from mozregression:utils.py:urlLinks
def url_links(url):
    """Return list of all non-navigation links found in web page.

    arguments:
    url - location of web page.

    returns: list of BeautifulSoup links.
    """
    r = urllib2.urlopen(url)
    content = r.read()
    if r.getcode() != 200:
        logger.warning("Unable to open url %s : %s" % (
            url, httplib.responses[r.getcode()]))
        return []

    soup = BeautifulSoup(content)
    # do not return a generator but an array, so we can store it for later use
    return [link for link in soup.findAll('a')
            if not link.get('href').startswith('?') and
            link.get_text() != 'Parent Directory']

def get_revision_timestamps(repo, first_revision, last_revision):
    """Returns a tuple containing timestamps for the revisions from
    the given repo.

    arguments:
    repo            - name of repository. For example, one of
                      mozilla-central, mozilla-aurora, mozilla-beta,
                      mozilla-inbound, fx-team
    first_revision  - string.
    last_revision - string.

    returns: first_timestamp, last_timestamp.

    Note this will return the revisions after the fromchange up to and
    including the tochange.
    """
    revisions = []
    r = urllib2.urlopen('%sjson-pushes?fromchange=%s&tochange=%s' % (
        repo_urls[repo], first_revision, last_revision))

    pushlog = json.loads(r.read())
    for pushid in sorted(pushlog.keys()):
        push = pushlog[pushid]
        revisions.append((push['changesets'][-1], push['date']))

    if not revisions:
        return None, None
    revisions.sort(key=lambda r: r[1])

    return revisions[0][1], revisions[-1][1]


class BuildLocation(object):
    def __init__(self, repos, buildtypes,
                 product, build_platforms, buildfile_ext):
        self.repos = repos
        self.buildtypes = buildtypes
        self.product = product
        self.build_platforms = build_platforms
        self.buildfile_ext = buildfile_ext
        buildfile_pattern = self.product + '.*\.('
        for platform in self.build_platforms:
            if platform == 'android':
                buildfile_pattern += 'android-arm|'
            elif platform == 'android-armv6':
                buildfile_pattern += 'android-arm-armv6|'
            elif platform == 'android-x86':
                buildfile_pattern += 'android-i386|'
            else:
                raise Exception('Unsupported platform: %s' % platform)

        buildfile_pattern = buildfile_pattern.rstrip('|')
        buildfile_pattern += ')'
        self.buildfile_regex = re.compile(buildfile_pattern)
        self.build_regex = re.compile("(%s%s)" % (buildfile_pattern,
                                                  self.buildfile_ext))
        self.buildtxt_regex = re.compile("(%s)\.txt" % buildfile_pattern)

        logger.debug('BuildLocation: '
                     'repos: %s, '
                     'buildtypes: %s, '
                     'product: %s, '
                     'build_platforms: %s, '
                     'buildfile_ext: %s, '
                     'pattern: %s, '
                     'buildfile_regex: %s, '
                     'build_regex: %s, '
                     'buildtxt_regex: %s' % (
                         self.repos,
                         self.buildtypes,
                         self.product,
                         self.build_platforms,
                         self.buildfile_ext,
                         buildfile_pattern,
                         self.buildfile_regex.pattern,
                         self.build_regex.pattern,
                         self.buildtxt_regex.pattern))

    def does_build_directory_contain_repo_name(self):
        """Returns True if the build directory name
        contains the repository name as a substring.
        Currently, this returns True only for Nightly
        BuildLocations.
        """
        return False

    def get_search_directories_by_time(self, start_time, end_time):
        """A generator which returns a tuple consisting of the
        next repository, directory pair.
        """
        raise NotImplementedError()

    def build_time_from_directory_name(self, directory_name):
        """Returns the datetime parsed from the directory name.
        """
        raise NotImplementedError()

    def directory_names_from_datetimestamp(self, datetimestamp):
        """A generator which returns the next directory name.
        """
        raise NotImplementedError()

    def find_latest_builds(self):
        window = datetime.timedelta(days=3)
        now = datetime.datetime.now()
        builds = self.find_builds_by_time(now - window, now)
        if not builds:
            logger.error('Could not find any nightly builds in the last '
                         '%d days!' % window.days)
            return None
        # Return the most recent builds for each of the architectures.
        # The phones will weed out the unnecessary architectures.
        builds.sort(reverse=True)
        multiarch_builds = []
        # check the longest strings first
        platforms = self.build_platforms
        platforms.sort(key=len, reverse=True)
        for platform in self.build_platforms:
            for build in builds:
                if platform in build:
                    multiarch_builds.append(build)
                    break
            builds = [build for build in builds if platform not in build]

        logger.debug('find_latest_builds: builds: %s' % multiarch_builds)
        return multiarch_builds

    def find_builds_by_time(self, start_time, end_time):
        logger.debug('Finding builds between %s and %s' %
                     (start_time, end_time))

        start_time = set_time_zone(start_time)
        end_time = set_time_zone(end_time)

        builds = []

        for directory_repo, directory in self.get_search_directories_by_time(start_time,
                                                                             end_time):
            logger.debug('Checking repo %s directory %s...' % (directory_repo, directory))
            directory_links = url_links(directory)
            for directory_link in directory_links:
                directory_name = directory_link.get_text().rstrip('/')
                directory_href = '%s%s/' % (directory, directory_name)
                logger.debug('find_builds_by_time: directory: href: %s, name: %s' % (
                    directory_href, directory_name))
                build_time = self.build_time_from_directory_name(directory_name)

                if not build_time:
                    continue

                if build_time < start_time or build_time > end_time:
                    continue

                build_links = url_links(directory_href)
                for build_link in build_links:
                    filename = build_link.get_text()
                    logger.debug('find_builds_by_time: checking filename: %s' % filename)
                    if self.build_regex.match(filename):
                        logger.debug('find_builds_by_time: found filename: %s' % filename)
                        builds.append('%s%s' % (directory_href, filename))
                        break
        if not builds:
            logger.error('No builds found.')
        return builds

    def find_builds_by_revision(self, first_revision, last_revision):
        logger.debug('Finding builds between revisions %s and %s' %
                     (first_revision, last_revision))

        range = datetime.timedelta(hours=12)
        buildid_regex = re.compile(r'([\d]{14})$')
        builds = []

        for repo in self.repos:
            first_timestamp, last_timestamp = get_revision_timestamps(
                repo,
                first_revision,
                last_revision)
            first_datetime = convert_timestamp_to_date(first_timestamp)
            last_datetime = convert_timestamp_to_date(last_timestamp)
            logger.debug('find_builds_by_revision: repo %s, '
                         'first_revision: %s, first_datetime: %s, '
                         'last_revision: %s, last_datetime: %s' % (
                             repo, first_revision, first_datetime,
                             last_revision, last_datetime))
            if not first_datetime or not last_datetime:
                continue
            for search_directory_repo, search_directory in self.get_search_directories_by_time(first_datetime,
                                                                                               last_datetime):
                # search_directory_repo is not None for Tinderbox builds and
                # can be used to filter the search directories.
                logger.debug('find_builds_by_revision: Checking repo: %s '
                             'search_directory_repo %s search_directory %s...' %
                             (repo, search_directory_repo, search_directory))
                if search_directory_repo and search_directory_repo != repo:
                    logger.info('find_builds_by_revision: skipping repo %s, '
                                'search_directory_repo: %s, search_directory: %s' %
                                (repo, search_directory_repo, search_directory))
                    continue

                format = None
                datetimestamps = []

                for link in url_links(search_directory):
                    try:
                        datetimestring = link.get('href').strip('/')
                        if self.does_build_directory_contain_repo_name() and repo not in datetimestring:
                            logger.info('find_builds_by_revisions:'
                                        'skipping datetimestring: repo: %s, '
                                        'datetimestring: %s' % (repo, datetimestring))
                            continue

                        logger.debug('find_builds_by_revisions: datetimestring: %s' % datetimestring)
                        link_format, link_datetime = parse_datetime(datetimestring)
                        if not format:
                            format = link_format
                        logger.debug('find_builds_by_revisions: link_format: %s,'
                                     'link_datetime: %s' % (link_format, link_datetime))
                        if link_datetime > first_datetime - range and link_datetime < last_datetime + range:
                            datetimestamps.append(set_time_zone(link_datetime))
                    except ValueError:
                        pass

                total_datetimestamps = len(datetimestamps)
                datetimestamps = sorted(set(datetimestamps))
                unique_datetimestamps = len(datetimestamps)
                logger.debug('find_builds_by_revision: total_datetimestamps=%d, unique_datetimestamps=%d' %
                             (total_datetimestamps,
                              unique_datetimestamps))

                logger.debug('find_builds_by_revisions: datetimestamps: %s' % datetimestamps)

                start_time = None
                end_time = None
                for datetimestamp in datetimestamps:
                    for directory_repo, directory_name in self.directory_names_from_datetimestamp(datetimestamp):

                        # Since Autophone requires returning builds
                        # for each of its supported platforms, arm,
                        # armv6 or x86, we need to search each to get
                        # all of the builds. That is why we don't
                        # terminate this loop when we find the first
                        # build which matches the ending revision.

                        logger.debug('find_builds_by_revisions: '
                                     'datetimestamp: %s, repo: %s, '
                                     'search_directory_repo: %s, '
                                     'search_directory: %s, directory_repo: %s, '
                                     'directory_name: %s' %
                                     (datetimestamp, repo, search_directory_repo,
                                      search_directory, directory_repo,
                                      directory_name))

                        try:
                            links = url_links("%s%s/" % (search_directory, directory_name))
                        except urllib2.HTTPError:
                            continue
                        for link in links:
                            href = link.get('href')
                            match = self.buildtxt_regex.match(href)
                            if match:
                                txturl = "%s%s/%s" % (search_directory,
                                                      directory_name, href)
                                build_url = "%s%s/%s%s" % (search_directory,
                                                         directory_name,
                                                         match.group(1),
                                                         self.buildfile_ext)
                                logger.debug('find_builds_by_revisions: '
                                             'found build: datetimestamp: %s, '
                                             'repo: %s, search_directory_repo:%s,'
                                             'search_directory: %s, '
                                             'directory_repo: %s, '
                                             'directory_name: %s, found build: %s'
                                             % (datetimestamp, repo,
                                             search_directory_repo,
                                             search_directory, directory_repo,
                                             directory_name, build_url))
                                contents = urllib2.urlopen(txturl).read()
                                lines = contents.splitlines()
                                if len(lines) > 1 and buildid_regex.match(lines[0]):
                                    buildid = lines[0]
                                    parts = lines[1].split('rev/')
                                    if len(parts) == 2:
                                        if repo != urls_repos[parts[0]]:
                                            logger.info('find_builds_by_revisions: '
                                                        'skipping build: %s != %s'
                                                        % (repo,
                                                           urls_repos[parts[0]]))
                                            continue
                                        revision = parts[1]
                                        if revision.startswith(first_revision):
                                            start_time = convert_buildid_to_date(buildid)
                                        elif revision.startswith(last_revision):
                                            end_time = convert_buildid_to_date(buildid)
                                    if start_time:
                                        builds.append(build_url)
                                        break
                    if end_time:
                        break

        return builds


class Nightly(BuildLocation):

    def __init__(self, repos, buildtypes,
                 product, build_platforms, buildfile_ext):
        BuildLocation.__init__(self, repos, buildtypes,
                               product, build_platforms, buildfile_ext)
        self.nightly_dirname_regexs = []
        for repo in repos:
            pattern = '(.*)-%s-(' % repo
            for platform in self.build_platforms:
                pattern += platform + '|'
            pattern = pattern.rstrip('|')
            pattern += ')$'
            self.nightly_dirname_regexs.append(re.compile(pattern))
        patterns = [regex.pattern for regex in self.nightly_dirname_regexs]
        logger.debug('Nightly: nightly_dirname_regexs: %s' % patterns)

    def does_build_directory_contain_repo_name(self):
        return True

    def get_search_directories_by_time(self, start_time, end_time):
        logger.debug('Nightly:get_search_directories_by_time(%s, %s)' % (start_time, end_time))
        y = start_time.year
        m = start_time.month
        while y < end_time.year or (y == end_time.year and m <= end_time.month):
            yield None, 'http://ftp.mozilla.org/pub/mobile/nightly/%d/%02d/' % (y, m)
            if m == 12:
                y += 1
                m = 1
            else:
                m += 1

    def build_time_from_directory_name(self, directory_name):
        logger.debug('Nightly:build_time_from_directory_name(%s)' % directory_name)
        build_time = None
        dirnamematch = None
        for r in self.nightly_dirname_regexs:
            dirnamematch = r.match(directory_name)
            if dirnamematch:
                break
        if dirnamematch:
            format, build_time = parse_datetime(directory_name)
        logger.debug('Nightly:build_time_from_directory_name: (%s, %s)' %
                     (directory_name, build_time))
        return build_time

    def directory_names_from_datetimestamp(self, datetimestamp):
        dates = [convert_datetime_to_string(datetimestamp, DIRECTORY_DATE), # only really needed for non mobile
                 convert_datetime_to_string(datetimestamp, DIRECTORY_DATETIME)]
        for platform in self.build_platforms:
            for date in dates:
                for repo in self.repos:
                    yield repo, '%s-%s-%s' % (date, repo, platform)


class Tinderbox(BuildLocation):

    main_http_url = 'http://ftp.mozilla.org/pub/mozilla.org/mobile/tinderbox-builds/'

    def __init__(self, repos, buildtypes,
                 product, build_platforms, buildfile_ext):
        BuildLocation.__init__(self, repos, buildtypes,
                               product, build_platforms, buildfile_ext)

    def get_search_directories_by_time(self, start_time, end_time):
        logger.debug('Tinderbox:get_search_directories_by_time(%s, %s)' % (start_time, end_time))
        # FIXME: Can we be certain that there's only one buildID (unique
        # timestamp) regardless of repo (at least m-i vs m-c)?
        for platform in self.build_platforms:
            for repo in self.repos:
                yield repo, '%s%s-%s/' % (self.main_http_url, repo, platform)

    def build_time_from_directory_name(self, directory_name):
        logger.debug('Tinderbox:build_time_from_directory_name(%s)' % directory_name)
        try:
            build_time = convert_timestamp_to_date(int(directory_name))
        except ValueError:
            build_time = None
        return build_time

    def directory_names_from_datetimestamp(self, datetimestamp):
        yield None, convert_datetime_to_string(datetimestamp, TIMESTAMP)


class InboundArchive(Tinderbox):

    main_http_url = 'http://inbound-archive.pub.build.mozilla.org/pub/mozilla.org/mobile/tinderbox-builds/'


class BuildCacheException(Exception):
    pass


class BuildCache(object):

    MAX_NUM_BUILDS = 20
    EXPIRE_AFTER_SECONDS = 60 * 60 * 24

    def __init__(self, repos, buildtypes,
                 product, build_platforms, buildfile_ext,
                 cache_dir='builds', override_build_dir=None,
                 enable_unittests=False):
        self.repos = repos
        self.buildtypes = buildtypes
        self.product = product
        self.build_platforms = build_platforms
        self.buildfile_ext = buildfile_ext
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
            return Nightly(self.repos, self.buildtypes,
                           self.product, self.build_platforms,
                           self.buildfile_ext)
        if 'tinderbox' in s:
            return Tinderbox(self.repos, self.buildtypes,
                             self.product, self.build_platforms,
                             self.buildfile_ext)
        if 'inboundarchive' in s:
            return InboundArchive(self.repos, self.buildtypes,
                                  self.product, self.build_platforms,
                                  self.buildfile_ext)
        return None

    def find_latest_builds(self, build_location_name='nightly'):
        build_location = self.build_location(build_location_name)
        if not build_location:
            logger.error('unsupported build_location "%s"' % build_location_name)
            return []
        return build_location.find_latest_builds()

    def find_builds_by_time(self, start_time, end_time, build_location_name='nightly'):
        build_location = self.build_location(build_location_name)
        if not build_location:
            logger.error('unsupported build_location "%s"' % build_location_name)
            return []

        return build_location.find_builds_by_time(start_time, end_time)

    def find_builds_by_revision(self, first_revision, last_revision,
                                build_location_name='nightly'):
        build_location = self.build_location(build_location_name)
        if not build_location:
            logger.error('unsupported build_location "%s"' % build_location_name)
            return []

        return build_location.find_builds_by_revision(first_revision, last_revision)

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
                ('fx-team', 'org.mozilla.fennec'),
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
                    'revision': '%srev/%s' % (repo_urls[tree], rev),
                    'androidprocname': procname,
                    'version': ver,
                    'bldtype': 'opt'}
        shutil.rmtree(tmpdir)
        file(build_metadata_path, 'w').write(json.dumps(metadata))
        return metadata
