# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import ConfigParser
import base64
import datetime
import glob
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

import utils
from build_dates import (TIMESTAMP, DIRECTORY_DATE, DIRECTORY_DATETIME,
                         parse_datetime, convert_datetime_to_string,
                         set_time_zone, convert_buildid_to_date,
                         convert_timestamp_to_date)

# Set the logger globally in the file, but this must be reset when
# used in a child process.
logger = logging.getLogger()

repo_urls = {
    'b2g-inbound': 'http://hg.mozilla.org/integration/b2g-inbound/',
    'fx-team': 'http://hg.mozilla.org/integration/fx-team/',
    'mozilla-aurora': 'http://hg.mozilla.org/releases/mozilla-aurora/',
    'mozilla-beta': 'http://hg.mozilla.org/releases/mozilla-beta/',
    'mozilla-central': 'http://hg.mozilla.org/mozilla-central/',
    'mozilla-inbound': 'http://hg.mozilla.org/integration/mozilla-inbound/',
    'mozilla-release': 'http://hg.mozilla.org/releases/mozilla-release/',
    'try': 'http://hg.mozilla.org/try/',
}

urls_repos = dict([(url, repo) for repo, url in repo_urls.items()])

# lifted from mozregression:utils.py:urlLinks
def url_links(url):
    """Return list of all non-navigation links found in web page.

    arguments:
    url - location of web page.

    returns: list of BeautifulSoup links.
    """
    content = utils.get_remote_text(url)
    if not content:
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
                      mozilla-inbound, fx-team, b2g-inbound
    first_revision  - string.
    last_revision - string.

    returns: first_timestamp, last_timestamp.
    """
    prefix = '%sjson-pushes?changeset=' % repo_urls[repo]
    first = utils.get_remote_json('%s%s' % (prefix, first_revision))
    last = utils.get_remote_json('%s%s' % (prefix, last_revision))

    return first[first.keys()[0]]['date'], last[last.keys()[0]]['date']


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
            if platform.startswith('android-x86'):
                buildfile_pattern += 'android-i386|'
            elif platform.startswith('android'):
                buildfile_pattern += 'android-arm|'
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

    def find_builds_by_directory(self, directory):
        logger.debug('Finding builds in directory %s' %
                     directory)

        builds = []
        # Ensure directory ends with a trailing /.
        # See https://docs.python.org/2.7/library/os.path.html#os.path.join
        directory = os.path.join(directory, '')

        logger.debug('Checking directory %s...' % directory)
        directory_tuple = urlparse.urlparse(directory)
        if directory_tuple.scheme.startswith('http'):
            build_links = url_links(directory)
            for build_link in build_links:
                filename = build_link.get_text()
                logger.debug('find_builds_by_directory: checking filename: %s' % filename)
                if self.build_regex.match(filename):
                    logger.debug('find_builds_by_directory: found filename: %s' % filename)
                    builds.append('%s%s' % (directory, filename))
                    break
        else:
            # Make sure the directory does not have a file scheme.
            directory = directory_tuple.path
            filepaths = glob.glob(directory + '*')
            for filepath in filepaths:
                filename = os.path.basename(filepath)
                logger.debug('find_builds_by_directory: checking %s' % filepath)
                if self.build_regex.match(filename):
                    logger.debug('find_builds_by_directory: found %s' % filepath)
                    # Make sure the returned build urls have a file scheme.
                    builds.append(urlparse.urljoin('file:', filepath))
                    break
        if not builds:
            logger.error('No builds found.')
        logger.debug('find_builds_by_directory: builds %s' % builds)
        return builds

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
        builds = []

        for repo in self.repos:
            try:
                first_timestamp, last_timestamp = get_revision_timestamps(
                    repo,
                    first_revision,
                    last_revision)
            except Exception:
                logger.exception('repo %s' % repo)
                continue

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
                        # or x86, we need to search each to get
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
                                build_data = utils.get_build_data(build_url)
                                if build_data:
                                    if repo != build_data['repo']:
                                        logger.info('find_builds_by_revisions: '
                                                    'skipping build: %s != %s'
                                                    % (repo,
                                                       urls_repos[build_data['repo']]))
                                        continue
                                    if build_data['revision'].startswith(first_revision):
                                        start_time = convert_buildid_to_date(build_data['id'])
                                    elif build_data['revision'].startswith(last_revision):
                                        end_time = convert_buildid_to_date(build_data['id'])
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
    EXPIRE_AFTER_DAYS = 1

    def __init__(self, repos, buildtypes,
                 product, build_platforms, buildfile_ext,
                 cache_dir='builds', override_build_dir=None,
                 build_cache_size=MAX_NUM_BUILDS,
                 build_cache_expires=EXPIRE_AFTER_DAYS,
                 treeherder_url=None):
        self.repos = repos
        self.buildtypes = buildtypes
        self.product = product
        self.build_platforms = build_platforms
        self.buildfile_ext = buildfile_ext
        self.cache_dir = cache_dir
        self.override_build_dir = override_build_dir
        if override_build_dir:
            if not os.path.exists(override_build_dir):
                raise BuildCacheException('Override Build Directory does not exist')

            build_path = os.path.join(override_build_dir, 'build.apk')
            if not os.path.exists(build_path):
                raise BuildCacheException('Override Build Directory %s does not contain a build.apk.' %
                                          override_build_dir)
        if not os.path.exists(self.cache_dir):
            os.mkdir(self.cache_dir)
        self.build_cache_size = build_cache_size
        self.build_cache_expires = build_cache_expires
        self.treeherder_url = treeherder_url
        logger.debug('BuildCache: %s' % self.__dict__)

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

    def find_builds_by_directory(self, directory, build_location_name='nightly'):
        build_location = self.build_location(build_location_name)
        if not build_location:
            logger.error('unsupported build_location "%s"' % build_location_name)
            return []

        return build_location.find_builds_by_directory(directory)

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

    def get(self, buildurl, force=False, enable_unittests=False,
            test_package_names=None):
        """Returns info on a cached build, fetching it if necessary.
        Returns a dict with a boolean 'success' item.
        If 'success' is False, the dict also contains an 'error' item holding a
        descriptive string.
        If 'success' is True, the dict also contains a 'metadata' item, which is
        a json encoding of BuildMetadata.  The path to the build is the
        'dir' item, which is a directory containing build.apk,
        symbols/, and, if enable_unittests is true, robocop.apk and tests/.
        If not found, fetches them, assuming a standard file structure.
        Cleans the cache before getting started.
        If self.override_build_dir is set, 'dir' is set to
        that value without verifying the contents nor fetching anything (though
        it will still try to open build.apk to read in the metadata).
        See BuildMetadata and BuildCache.build_metadata() for the other
        metadata items.
        """
        if self.override_build_dir:
            tests_path = os.path.join(self.override_build_dir, 'tests')
            if enable_unittests and not os.path.exists(tests_path):
                raise BuildCacheException(
                    'Override Build Directory %s does not contain a tests directory.' %
                    self.override_build_dir)
            if test_package_names:
                test_packages_json_path = os.path.join(self.override_build_dir,
                                                       'test_packages.json')
                if not os.path.exists(test_packages_json_path):
                    raise BuildCacheException(
                        'Override Build Directory %s does not contain a test_packages.json file.' %
                        self.override_build_dir)
                saved_test_packages = json.loads(file(test_packages_json_path).read())
                missing_packages = []
                for test_package_name in test_package_names:
                    if test_package_name not in saved_test_packages:
                        missing_packages.append(test_package_name)
                if missing_packages:
                    raise BuildCacheException(
                        'Override Build Directory %s is missing test packages %s.' %
                        (self.override_build_dir, missing_packages))
            metadata = self.build_metadata(buildurl, self.override_build_dir)
            if metadata:
                metadata_json = metadata.to_json()
            else:
                metadata_json = ''
            return {
                'success': metadata is not None,
                'error': '' if metadata is not None else 'metadata is None',
                'metadata': metadata_json
            }
        # If the buildurl is for a local build, force the download since it may
        # have changed even though the buildurl hasn't.
        force = force or not urlparse.urlparse(buildurl).scheme.startswith('http')
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
        except (zipfile.BadZipfile, IOError), e:
            logger.warning('%s checking build: %s. Forcing download.' % (e, buildurl))
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
                elif 'No such file or directory' in str(ioerror):
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
        if enable_unittests:
            tests_path = os.path.join(cache_build_dir, 'tests')
            if force or not os.path.exists(tests_path):
                # XXX: assumes fixed buildurl-> robocop mapping
                robocop_url = urlparse.urljoin(buildurl, 'robocop.apk')
                robocop_path = os.path.join(cache_build_dir, 'robocop.apk')
                if not os.path.exists(robocop_path):
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
                if not os.path.exists(fennec_ids_path):
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
                test_packages = utils.get_remote_json(
                    urlparse.urljoin(buildurl, 'test_packages.json'))
                # The test_packages.json file contains keys for each
                # test category but they all point to the same tests
                # zip file. This will change when
                # https://bugzilla.mozilla.org/show_bug.cgi?id=917999
                # goes into production, but using a set allows us to
                # easily eliminate duplicate file names.
                test_package_files = set()
                if test_package_names and test_packages:
                    logger.debug('test_packages: %s' % json.dumps(test_packages))
                    for test_package_name in test_package_names:
                        logger.debug('test_package_name: %s' % test_package_name)
                        test_package_files.update(set(test_packages[test_package_name]))
                else:
                    # XXX: assumes fixed buildurl-> tests_url mapping
                    logger.debug('default test package')
                    tests_url = re.sub('.apk$', '.tests.zip', buildurl)
                    test_package_files = set([os.path.basename(tests_url)])
                for test_package_file in test_package_files:
                    logger.debug('test_package_file: %s' % test_package_file)
                    test_package_path = os.path.join(cache_build_dir,
                                                     test_package_file)
                    if os.path.exists(test_package_path):
                        continue
                    test_package_url = urlparse.urljoin(buildurl, test_package_file)
                    tmpf = tempfile.NamedTemporaryFile(delete=False)
                    tmpf.close()
                    try:
                        urllib.urlretrieve(test_package_url, tmpf.name)
                    except IOError:
                        os.unlink(tmpf.name)
                        err = 'IO Error retrieving tests: %s.' % test_package_url
                        logger.exception(err)
                        return {'success': False, 'error': err}
                    tests_zipfile = zipfile.ZipFile(tmpf.name)
                    tests_zipfile.extractall(tests_path)
                    tests_zipfile.close()
                    # Move the test package zip file to the cache
                    # build directory so we can check if it has been
                    # downloaded.
                    shutil.move(tmpf.name, test_package_path)
                if test_packages:
                    # Save the test_packages.json file
                    test_packages_json_path = os.path.join(cache_build_dir,
                                                          'test_packages.json')
                    file(test_packages_json_path, 'w').write(
                        json.dumps(test_packages))

        metadata = self.build_metadata(buildurl, cache_build_dir)
        if metadata:
            metadata_json = metadata.to_json()
        else:
            metadata_json = ''
        return {
            'success': metadata is not None,
            'error': '' if metadata is not None else 'metadata is None',
            'metadata': metadata_json
        }

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
                 datetime.timedelta(days=self.build_cache_expires))):
                # too new
                return True
            return False

        builds = [(x, os.stat(lastused_path(x)).st_mtime) for x in
                  os.listdir(self.cache_dir) if not keep_build(x)]
        builds.sort(key=lambda x: x[1])
        while len(builds) > self.build_cache_size:
            b = builds.pop(0)[0]
            logger.info('Expiring %s' % b)
            shutil.rmtree(os.path.join(self.cache_dir, b))

    def build_metadata(self, build_url, build_dir):
        # If the build is a local build, do not rely on any
        # existing cached build.
        remote = urlparse.urlparse(build_url).scheme.startswith('http')
        build_metadata_path = os.path.join(build_dir, 'metadata.json')
        if remote and os.path.exists(build_metadata_path):
            try:
                return BuildMetadata().from_json(
                    json.loads(file(build_metadata_path).read()))
            except (ValueError, IOError):
                pass
        tmpdir = tempfile.mkdtemp()
        try:
            build_path = os.path.join(build_dir, 'build.apk')
            apkfile = zipfile.ZipFile(build_path)
            apkfile.extract('application.ini', tmpdir)
            apkfile.extract('package-name.txt', tmpdir)
        except zipfile.BadZipfile:
            # we should have already tried to redownload bad zips, so treat
            # this as fatal.
            logger.exception('%s is a bad apk; aborting job.' % build_path)
            shutil.rmtree(tmpdir)
            return None
        with open(os.path.join(tmpdir, 'package-name.txt')) as package_file:
            procname = package_file.read().strip()
        cfg = ConfigParser.RawConfigParser()
        cfg.read(os.path.join(tmpdir, 'application.ini'))
        rev = cfg.get('App', 'SourceStamp')
        ver = cfg.get('App', 'Version')
        try:
            repo = cfg.get('App', 'SourceRepository')
        except ConfigParser.NoOptionError:
            logger.warning('%s does not specifiy SourceRepository. '
                           'Guessing mozilla-central.' % build_url)
            repo = 'https://hg.mozilla.org/mozilla-central/'
        buildid = cfg.get('App', 'BuildID')
        tree = None
        for temp_tree in (
                'b2g-inbound',
                'fx-team',
                'mozilla-aurora',
                'mozilla-beta',
                'mozilla-central',
                'mozilla-inbound',
                'mozilla-release',
                'try'):
            if temp_tree in repo:
                tree = temp_tree
                break
        if not tree:
            raise BuildCacheException('build %s contains an unknown SourceRepository %s' %
                                      (apkfile, repo))

        build_type = 'debug' if 'debug' in build_url else 'opt'
        metadata = BuildMetadata(url=build_url,
                                 dir=build_dir,
                                 tree=tree,
                                 id=buildid,
                                 revision='%srev/%s' % (repo_urls[tree], rev),
                                 app_name=procname,
                                 version=ver,
                                 build_type=build_type,
                                 treeherder_url=self.treeherder_url)
        shutil.rmtree(tmpdir)
        if metadata:
            file(build_metadata_path, 'w').write(json.dumps(metadata.to_json()))
        return metadata


class BuildMetadata(object):
    def __init__(self,
                 url=None,
                 dir=None,
                 tree=None,
                 id=None,
                 revision=None,
                 app_name=None,
                 version=None,
                 build_type=None,
                 treeherder_url=None):
        self._date = None
        self.url = url
        if not dir:
            self.dir = None
            self.symbols = None
        else:
            self.dir = os.path.abspath(dir)
            self.symbols = os.path.join(self.dir, 'symbols')
            if not os.path.exists(self.symbols):
                self.symbols = None
        self.tree = tree
        self.id = id
        self.type = build_type
        self.revision = revision
        self.app_name = app_name
        self.version = version
        self.revision_hash = None

        if treeherder_url:
            # TODO: Should be consistent with changeset and revision
            # variable names in terms of the changeset url and the
            # revision id.
            changeset = os.path.basename(urlparse.urlparse(revision).path)
            self.revision_hash = utils.get_treeherder_revision_hash(
                treeherder_url, tree, changeset)
            if not self.revision_hash:
                logger.warning('Failed to get the revision_hash for %s' %
                               self.revision)
        logger.debug('BuildMetadata: %s' % self.__dict__)

    @property
    def date(self):
        if not self._date:
            d = datetime.datetime.strptime(self.id, '%Y%m%d%H%M%S')
            self._date = math.trunc(time.mktime(d.timetuple()))
        return self._date

    def __str__(self):
        d = self.__dict__.copy()
        d['date'] = self.date
        del d['_date']
        return '%s' % d

    def to_json(self):
        return {
            '__class__': 'BuildMetadata',
            'url': self.url,
            'dir': self.dir,
            'symbols': self.symbols,
            'tree': self.tree,
            'type': self.type,
            'id': self.id,
            'revision': self.revision,
            'app_name': self.app_name,
            'version': self.version,
            'revision_hash': self.revision_hash,
        }

    def from_json(self, j):
        if '__class__' not in j or j['__class__'] != 'BuildMetadata':
            raise ValueError
        self.url = j['url']
        self.dir = j['dir']
        self.symbols = j['symbols']
        self.tree = j['tree']
        self.type = j['type']
        self.id = j['id']
        self.revision = j['revision']
        self.app_name = j['app_name']
        self.version = j['version']
        self.revision_hash = j['revision_hash']
        return self
