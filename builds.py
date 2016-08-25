# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import ConfigParser
import base64
import datetime
import glob
import json
import logging
import os
import re
import shutil
import tempfile
import urllib
import urllib2
import urlparse
import zipfile

import slugid
import taskcluster
from thclient import TreeherderClient

from bs4 import BeautifulSoup

import utils

from build_dates import (TIMESTAMP, DIRECTORY_DATE, DIRECTORY_DATETIME,
                         PACIFIC, UTC,
                         parse_datetime, convert_datetime_to_string,
                         convert_timestamp_to_date)

# Set the logger globally in the file, but this must be reset when
# used in a child process.
logger = logging.getLogger()

repo_urls = {
    'autoland': 'http://hg.mozilla.org/integration/autoland/',
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

    soup = BeautifulSoup(content, 'html.parser')
    # do not return a generator but an array, so we can store it for later use
    return [link for link in soup.findAll('a')
            if not link.get('href').startswith('?') and
            link.get_text() != 'Parent Directory']

def get_revision_datetimes(repo, first_revision, last_revision):
    """Returns a tuple containing dates for the revisions from
    the given repo.

    arguments:
    repo            - name of repository. For example, one of
                      mozilla-central, mozilla-aurora, mozilla-beta,
                      mozilla-inbound, autoland, fx-team, b2g-inbound
    first_revision  - string.
    last_revision - string.

    returns: first_datetime, last_datetime.
    """
    prefix = '%sjson-pushes?changeset=' % repo_urls[repo]
    first = utils.get_remote_json('%s%s' % (prefix, first_revision))
    if first:
        first_timestamp = first[first.keys()[0]]['date']
    else:
        first_timestamp = None
    last = utils.get_remote_json('%s%s' % (prefix, last_revision))
    if last:
        last_timestamp = last[last.keys()[0]]['date']
    else:
        last_timestamp = None

    if first_timestamp and last_timestamp:
        first_datetime = convert_timestamp_to_date(first_timestamp)
        last_datetime = convert_timestamp_to_date(last_timestamp)
    else:
        first_datetime = None
        last_datetime = None

    return first_datetime, last_datetime

def parse_taskcluster_namespace(namespace):
    platform_parts = namespace.split('.')[-1].split('-')
    platform = '-'.join(platform_parts[:-1])
    build_type = platform_parts[-1]
    if build_type == 'dbg':
        build_type = 'debug'
    logger.debug('parse_taskcluster_namespace: %s: %s, %s' % (namespace, platform, build_type))
    return (platform, build_type)


def parse_taskcluster_worker_type(worker_type):
    re_worker_type = re.compile(r'(android.*)-(api-\d+)')
    match = re_worker_type.match(worker_type)
    if match:
        platform, sdk = match.groups()
        platform += '-' + sdk
    else:
        platform, sdk = None, None
    return (platform, sdk)


def get_treeherder_job_guid(task_id, run_id):
    job_guid = '%s/%s' % (slugid.decode(str(task_id)), run_id)
    return job_guid


def get_treeherder_job(repo, job_guid):
    job = None
    try:
        # Note this uses the production instance of Treeherder which
        # should be alright since we are looking up build jobs which
        # will always be available on the production instance.
        client = TreeherderClient()
        jobs = client.get_jobs(repo, job_guid=job_guid)

        if len(jobs) == 0 or len(jobs) > 1:
            logger.warning('get_treeherder_job: job_guid: %s returned %s jobs' % (job_guid, len(jobs)))

        if len(jobs) > 0:
            job = jobs[0]
    except:
        pass

    return job


def get_treeherder_tier(repo, task_id, run_id):
    tier = None
    treeherder_job_guid = get_treeherder_job_guid(task_id, run_id)
    if treeherder_job_guid:
        treeherder_job = get_treeherder_job(repo, treeherder_job_guid)
        if treeherder_job and 'tier' in treeherder_job:
            tier = treeherder_job['tier']
    logger.debug('get_treeherder_tier: repo: %s, task_id: %s, run_id: %s, tier: %s' % (repo, task_id, run_id, tier))
    return tier


def get_push_revisions(repo, parameters):
    """Return a list containing tip revisions from the pushlog for the
    repo and the corresponding range parameter.  The returned list of
    revisions is guaranteed to be in push date order.

    :param parameters: An object whose key, value pairs will be used
                       to construct the query parameters for the push log query.

    To query by revision range, use  {'fromchange': 'rev1', 'tochange': 'rev2'}
    To query by date range, use      {'startdate': 'date1', 'enddate': 'date2'}
    To query by a single revision use {'changeset': 'rev'}

    """
    def cmp_push(x, y):
        return x['date'] - y['date']

    revisions = []
    pushlog_url = (repo_urls[repo] +
                   'json-pushes?tipsonly=1&' +
                   '&'.join(['%s=%s' % (parameter, urllib.quote(value))
                             for (parameter, value) in parameters.iteritems()]))

    pushlog_json = utils.get_remote_json(pushlog_url)
    if not pushlog_json:
        logger.warning('get_push_revisions: %s not found.' % pushlog_url)
    else:
        # Guarantee that the pushes are accessed in push date order.
        push_id_dates = []
        for push_id in pushlog_json:
            push_id_dates.append( {'push_id': push_id, 'date': pushlog_json[push_id]['date']} )
        push_id_dates.sort(cmp=cmp_push)

        for push_id_date in push_id_dates:
            push_id = push_id_date['push_id']
            revisions.append(pushlog_json[push_id]['changesets'][-1])
    return revisions

class BuildLocation(object):
    def __init__(self, repos, buildtypes,
                 product, build_platforms, buildfile_ext):
        self.repos = repos
        self.buildtypes = buildtypes
        self.product = product
        self.build_platforms = build_platforms
        self.buildfile_ext = buildfile_ext

    def find_latest_builds(self):
        raise NotImplementedError()

    def find_builds_by_directory(self, directory):
        raise NotImplementedError()

    def find_builds_by_time(self, start_time, end_time):
        raise NotImplementedError()

    def find_builds_by_revision(self, first_revision, last_revision):
        raise NotImplementedError()


class TaskClusterBuilds(BuildLocation):
    def __init__(self, repos, buildtypes,
                 product, build_platforms, buildfile_ext, nightly):
        BuildLocation.__init__(self, repos, buildtypes,
                               product, build_platforms, buildfile_ext)
        self.nightly = nightly
        self.index = taskcluster.client.Index()
        self.queue = taskcluster.client.Queue()

    def find_latest_builds(self):
        task_ids_by_repo = self._find_latest_task_ids()
        builds_by_repo = self._find_builds_by_task_ids(task_ids_by_repo)
        builds = []
        # The taskcluster 'latest' routes can include older builds
        # which are no longer being built but which have not expired
        # yet. Filter the return builds so we only return the latest
        # builds in the last week.
        threshold = datetime.datetime.now(tz=UTC) - datetime.timedelta(days=7)

        for repo in builds_by_repo:
            for build_data in builds_by_repo[repo]:
                if build_data['date'] >= threshold:
                    if not build_data['nightly'] and self.nightly:
                        logger.debug('find_latest_builds: overriding build_data nightly %s with %s' % (build_data['nightly'], self.nightly))
                    builds.append(build_data)
        logger.debug('find_latest_builds: %s' % builds)
        return builds

    def find_builds_by_directory(self, directory):
        directory = os.path.join(directory, '')
        if 'taskcluster.net' in directory:
            task_id = None
            run_id = None
            re_taskcluster_v1 = re.compile(r'https://queue.taskcluster.net/v1/task/(\w+)/runs/([0-9]+)/artifacts/public/build/')
            # Example: https://queue.taskcluster.net/v1/task/cGnFzWmmToikNzuJYJ096Q/runs/0/artifacts/public/build/
            match = re_taskcluster_v1.match(directory)
            if match:
                task_id = match.group(1)
                run_id = int(match.group(2))
            else:
                re_taskcluster_v2 = re.compile(r'https://public-artifacts.taskcluster.net/([^/]+)/([0-9]+)/public/build/')
                # Example: https://public-artifacts.taskcluster.net/cGnFzWmmToikNzuJYJ096Q/0/public/build/
                match = re_taskcluster_v2.match(directory)
                if match:
                    task_id = match.group(1)
                    run_id = int(match.group(2))
            if task_id is not None and run_id is not None:
                # Fake a task_ids_by_repo for use with
                # _find_builds_by_task_ids
                task_ids_by_repo = {'dummy': [task_id]}
                builds_by_repo = self._find_builds_by_task_ids(task_ids_by_repo,
                                                               only_run_id=run_id)
                builds = builds_by_repo['dummy']
            else:
                logger.error('No builds found.')
                builds = []
        else:
            if self.nightly:
                ftp_build_location = FtpNightly(self.repos, self.buildtypes,
                                                self.product, self.build_platforms,
                                                self.buildfile_ext)
            else:
                ftp_build_location = FtpTinderbox(self.repos, self.buildtypes,
                                                  self.product, self.build_platforms,
                                                  self.buildfile_ext)
            builds = ftp_build_location.find_builds_by_directory(directory)
        logger.debug('find_builds_by_directory: builds %s' % builds)
        return builds

    def find_builds_by_time(self, start_time, end_time):
        logger.debug('find_builds_by_time(%s, %s)' % (start_time, end_time))
        if not start_time.tzinfo or not end_time.tzinfo:
            raise Exception('find_builds_by_time: naive times not permitted')

        revisions_by_repo = self._find_revisions_by_dates(start_time, end_time)
        task_ids_by_repo = self._find_task_ids_by_revisions(revisions_by_repo)
        builds_by_repo = self._find_builds_by_task_ids(task_ids_by_repo)
        builds = []
        for repo in builds_by_repo:
            repo_builds = builds_by_repo[repo]
            ftp_fallback = False
            if len(repo_builds) == 0:
                ftp_fallback = True
                ftp_start_time = start_time
                ftp_end_time = end_time
                inclusive = True
            else:
                first_build_date = repo_builds[0]['date']
                logger.debug('find_builds_by_time: repo: %s, first_build_date: %s' % (repo, first_build_date))
                if first_build_date > start_time:
                    ftp_fallback = True
                    ftp_start_time = start_time
                    ftp_end_time = first_build_date
                    inclusive = False
            if ftp_fallback:
                # TaskCluster does not have builds for the full range of dates.
                # Fallback to the FTP locations.
                logger.debug('find_builds_by_time: fallback to ftp: repo: %s, %s-%s' % (repo, ftp_start_time, ftp_end_time))
                if self.nightly:
                    ftp_build_location = FtpNightly(self.repos, self.buildtypes,
                                                    self.product, self.build_platforms,
                                                    self.buildfile_ext)
                else:
                    ftp_build_location = FtpTinderbox(self.repos, self.buildtypes,
                                                      self.product, self.build_platforms,
                                                      self.buildfile_ext)
                repo_builds.extend(ftp_build_location.find_builds_by_time(ftp_start_time,
                                                                          ftp_end_time,
                                                                          inclusive=inclusive))

            # Filter by exact start and end times.
            all_builds = list(repo_builds)
            repo_builds = []
            for build_data in all_builds:
                build_date = build_data['date']
                if build_date >= start_time and build_date <= end_time:
                    builds.append(build_data)
        logger.debug('find_builds_by_time: %s' % builds)
        return builds

    def find_builds_by_revision(self, start_revision, end_revision, inclusive=True):
        revisions_by_repo = self._find_revisions_by_revisions(start_revision,
                                                              end_revision,
                                                              inclusive=inclusive)
        task_ids_by_repo = self._find_task_ids_by_revisions(revisions_by_repo)
        builds_by_repo = self._find_builds_by_task_ids(task_ids_by_repo)
        builds = []
        for repo in builds_by_repo:
            repo_builds = builds_by_repo[repo]
            ftp_fallback = False

            if len(repo_builds) == 0:
                ftp_fallback = True
                ftp_start_revision = start_revision
                ftp_end_revision = end_revision
                inclusive = True
            else:
                first_revision = repo_builds[0]['revision']
                if first_revision[:12] != start_revision[:12]:
                    ftp_fallback = True
                    ftp_start_revision = start_revision
                    ftp_end_revision = first_revision
                    inclusive = False

            if ftp_fallback:
                # TaskCluster does not have builds for the full range of revisions.
                # Fallback to the FTP locations.
                logger.debug('find_builds_by_revision: fallback to ftp: repo: %s, %s-%s' % (repo, ftp_start_revision, ftp_end_revision))
                if self.nightly:
                    ftp_build_location = FtpNightly(self.repos, self.buildtypes,
                                                    self.product, self.build_platforms,
                                                    self.buildfile_ext)
                else:
                    ftp_build_location = FtpTinderbox(self.repos, self.buildtypes,
                                                      self.product, self.build_platforms,
                                                      self.buildfile_ext)
                repo_builds.extend(
                    ftp_build_location.find_builds_by_revision(ftp_start_revision,
                                                               ftp_end_revision,
                                                               inclusive=inclusive))
            builds.extend(repo_builds)
        logger.debug('find_builds_by_revision: %s' % builds)
        return builds

    def _find_builds_by_task_ids(self, task_ids_by_repo, only_run_id = None, start_time=None, end_time=None):
        """Return a list of build_data objects for the build_urls for the
        specified tasks.
        """
        builds_by_repo = {}
        url_format = 'https://queue.taskcluster.net/v1/task/%s/runs/%s/artifacts/%s'
        re_fennec = re.compile(r'(fennec|target).*apk$')
        for repo in task_ids_by_repo:
            builds_by_repo[repo] = builds = []
            for task_id in task_ids_by_repo[repo]:
                status = self.queue.status(task_id)['status']
                worker_type = status['workerType']
                builder_type = 'buildbot' if (worker_type == 'buildbot') else 'taskcluster'
                logger.debug('_find_builds_by_task_ids: status: %s' % status)
                build_found = False
                for run in reversed(status['runs']): # runs
                    if build_found:
                        break
                    if run['state'] != 'completed':
                        continue
                    run_id = run['runId']
                    if only_run_id is not None and only_run_id != run_id:
                        continue
                    tier = get_treeherder_tier(repo, task_id, run_id)
                    artifacts = utils.taskcluster_artifacts(task_id, run_id)
                    try:
                        build_date = build_url = None
                        while not build_found: # artifacts
                            artifact = artifacts.next()
                            artifact_name = artifact['name']
                            search = re_fennec.search(artifact_name)
                            if search:
                                build_url = url_format % (task_id, run_id, artifact_name)
                                build_data = utils.get_build_data(build_url, builder_type=builder_type)
                                if not build_data:
                                    # Failed to get the build data for this
                                    # build. Break out of the artifacts for this
                                    # run but keep looking for a build in earlier
                                    # runs.
                                    logger.warning('_find_builds_by_task_ids: task_id: %s, run_id: %s: '
                                                   'could not get %s' % (task_id, run_id, build_url))
                                    break # artifacts
                                # Fall back to the taskcluster workerType to get the sdk if possible
                                if build_data['sdk'] is None:
                                    (platform, sdk) = parse_taskcluster_worker_type(worker_type)
                                    if sdk:
                                        build_data['platform'] = worker_type
                                        build_data['sdk'] = sdk
                                if 'nightly_build' in build_data and not build_data['nightly_build']:
                                    break # artifacts
                                build_date = build_data['date']
                                if ((start_time and end_time and
                                     build_date >= start_time and build_date <= end_time) or
                                    (start_time and build_date >= start_time) or
                                    (end_time and build_date <= end_time) or
                                    (not start_time and not end_time)):
                                    build_found = True
                                    logger.debug('_find_builds_by_task_ids: adding worker_type: %s, build_data: %s, tier: %s' % (worker_type, build_data, tier))
                                    builds.append( build_data )
                                    break # artifacts
                    except StopIteration:
                        pass
        logger.debug('_find_builds_by_task_ids: %s' % builds)
        return builds_by_repo

    def _find_latest_task_ids(self):
        """Return an object keyed by repository name. Each item in the object
        is a list of the task ids for the latest builds for the
        matching platforms and build types.
        """
        namespace_version = 'v2'
        if self.nightly:
            namespace_format = 'gecko.%s.%s.nightly.latest.mobile'
        else:
            namespace_format = 'gecko.%s.%s.latest.mobile'

        logger.debug('_find_latest_task_ids: nightly: %s, namespace_format: %s' % (self.nightly, namespace_format))
        task_ids_by_repo = {}
        for repo in self.repos:
            # We could iterate over the build_platforms by adding
            # the build_platform to the routing key, but that will
            # end up paying a cost of looking up obsolete
            # platforms in perpetuity. Instead we can list the
            # namespaces under mobile and get the currently
            # supported namespaces and filter those.
            task_ids_by_repo[repo] = task_ids = []
            namespace = namespace_format % (namespace_version, repo)
            payload = {}
            response = self.index.listTasks(namespace, payload)
            logger.debug('_find_latest_task_ids: listTasks(%s, %s): response: %s' % (namespace, payload, response))
            for task in response['tasks']:
                # gecko.v2.mozilla-central.nightly.latest.mobile.android-api-15-opt
                logger.debug('_find_latest_task_ids: task: %s' % task)
                task_id = task['taskId']
                task_definition = self.queue.task(task_id)
                logger.debug('_find_latest_task_ids: task_definition: %s' % task_definition)
                worker_type = task_definition['workerType']
                builder_type = 'buildbot' if worker_type == 'buildbot' else 'taskcluster'
                # Just hard-code run_id 0 since we are only interested in the tier.
                tier = get_treeherder_tier(repo, task_id, 0)
                task_namespace = task['namespace']
                (platform, build_type) = parse_taskcluster_namespace(task_namespace)
                if (platform in self.build_platforms and
                    build_type in self.buildtypes and
                    (builder_type == 'buildbot' or tier == 1)):
                    logger.debug('_find_lastest_task_ids: adding worker_type: %s, task_id: %s, tier: %s, repo: %s, platform: %s, build_type: %s' % (worker_type, task_id, tier, repo, platform, build_type))
                    task_ids.append(task_id)

        logger.debug('_find_latest_task_ids: %s' % task_ids_by_repo)
        return task_ids_by_repo

    def _find_revisions_by_revisions(self, start_revision, end_revision, inclusive=True):
        """Return an object keyed by repository name. Each item in the object
        is a list of the revisions found from start_revision to
        end_revision for that repo. If inclusive is True, the
        end_revision is included in the list.

        """
        revisions_by_repo = {}
        for repo in self.repos:
            if start_revision != end_revision:
                parameters = {'fromchange': start_revision[:12], 'tochange': end_revision[:12]}
            else:
                parameters = {'changeset': start_revision[:12]}
            revisions_by_repo[repo] = revisions = get_push_revisions(repo, parameters)
            # If inclusive is True, include the end_revision since the json-pushes does not
            # include it. Although we return the end_revision for each
            # repo, it may not be valid for all of them.
            if inclusive and (start_revision != end_revision or len(revisions) > 0):
                revisions.append(end_revision)
        logger.debug('_find_revisions_by_revisions: %s' % revisions_by_repo)
        return revisions_by_repo

    def _find_revisions_by_dates(self, start_date, end_date):
        """Return an object keyed by repository name. Each item in the object
        is a list of the revisions found from start_date through
        end_date for that repo.

        Adjust initial start_date to include the previous 3 hours and
        the following 3 hours. Users of _find_revisions_by_dates are
        responsible for filtering these results based upon the actual
        date range.
        """
        delta = datetime.timedelta(seconds=3*3600)
        start_date -= delta
        end_date += delta
        # json-pushlog does not appear to support timestamps so we must use
        # UTC date strings.
        start_date = start_date.astimezone(UTC)
        end_date = end_date.astimezone(UTC)
        start_date_str = datetime.datetime.strftime(start_date, '%Y-%m-%d %H:%M:%S')
        end_date_str = datetime.datetime.strftime(end_date, '%Y-%m-%d %H:%M:%S')
        revisions_by_repo = {}
        for repo in self.repos:
            revisions_by_repo[repo] = get_push_revisions(repo,
                                                         {'startdate': start_date_str,
                                                          'enddate': end_date_str})
        logger.debug('_find_revisions_by_dates: %s' % revisions_by_repo)
        return revisions_by_repo

    def _find_task_ids_by_revisions(self, revisions_by_repo):
        """Return an object keyed by repository name. Each item in the object
        is a list of the task ids corresponding to the revisions.
        """
        namespace_version = 'v2'
        namespace_format = 'gecko.%s.%s.revision.%s.mobile'

        task_ids_by_repo = {}

        for repo in revisions_by_repo:
            task_ids_by_repo[repo] = task_ids = []
            for revision in revisions_by_repo[repo]:
                # We could iterate over the build_platforms by adding
                # the build_platform to the routing key, but that will
                # end up paying a cost of looking up obsolete
                # platforms in perpetuity. Instead we can list the
                # namespaces under mobile and get the currently
                # supported namespaces and filter those.

                namespace = namespace_format % (namespace_version, repo, revision)
                payload = {}
                response = self.index.listTasks(namespace, payload)
                for task in response['tasks']:
                    logger.debug('_find_task_ids_by_revisions: task: %s' % task)
                    task_id = task['taskId']
                    task_namespace = task['namespace']
                    task_definition = self.queue.task(task_id)
                    logger.debug('_find_task_ids_by_revisions: task_definition: %s' % task_definition)
                    worker_type = task_definition['workerType']
                    builder_type = 'buildbot' if worker_type == 'buildbot' else 'taskcluster'
                    # Just hard-code run_id 0 since the tier shouldn't change.
                    tier = get_treeherder_tier(repo, task_id, 0)
                    platform = build_type = None
                    if builder_type == 'buildbot':
                        (platform, build_type) = parse_taskcluster_namespace(task_namespace)
                    elif 'build_type' in task_definition['extra']:
                        platform = task_definition['workerType']
                        build_type = task_definition['extra']['build_type']
                    else:
                        logger.warning('_find_task_ids_by_revisions: missing build_type in task_definition["extra"] %s' % task_definition)
                    logger.debug('_find_task_ids_by_revisions: worker_type: %s, platform: %s, build_type: %s, tier: %s' % (worker_type, platform, build_type, tier))
                    if (platform in self.build_platforms and
                        build_type in self.buildtypes and
                        (builder_type == 'buildbot' or tier == 1)):
                        logger.debug('_find_task_ids_by_revisions: adding worker_type: %s, task_id: %s, tier: %s, repo: %s, platform: %s, build_type; %s' % (builder_type, task_id, tier, repo, platform, build_type))
                        task_ids.append(task_id)
        logger.debug('_find_task_ids_by_revisions: %s' % task_ids_by_repo)
        return task_ids_by_repo


class FtpBuildLocation(BuildLocation):
    def __init__(self, repos, buildtypes,
                 product, build_platforms, buildfile_ext):
        BuildLocation.__init__(self,  repos, buildtypes,
                               product, build_platforms, buildfile_ext)
        buildfile_pattern = self.product + '.*\.('
        for platform in self.build_platforms:
            if platform.startswith('android-x86'):
                buildfile_pattern += 'android-i386|'
            elif platform.startswith('android'):
                buildfile_pattern += 'android-arm|androideabi-arm|eabi-arm|'
            else:
                raise Exception('Unsupported platform: %s' % platform)

        buildfile_pattern = buildfile_pattern.rstrip('|')
        buildfile_pattern += ')'
        self.buildfile_regex = re.compile(buildfile_pattern)
        self.build_regex = re.compile("(%s%s)$" % (buildfile_pattern,
                                                  self.buildfile_ext))
        self.buildtxt_regex = re.compile("(%s)\.txt$" % buildfile_pattern)
        logger.debug('FtpBuildLocation: '
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
        Currently, this returns True only for FtpNightly
        FtpBuildLocations.
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
        window = datetime.timedelta(days=7)
        now = datetime.datetime.now(tz=UTC)
        builds = self.find_builds_by_time(now - window, now)
        if not builds:
            logger.error('Could not find any nightly builds in the last '
                         '%d days!' % window.days)
            return None
        # Return the most recent builds for each of the architectures.
        # The phones will weed out the unnecessary architectures.
        def cmp_build_data(x, y):
            if x['date'] < y['date']:
                return -1
            if x['date'] > y['date']:
                return +1
            return 0
        builds.sort(cmp=cmp_build_data, reverse=True)
        multiarch_builds = []
        # check the longest strings first
        platforms = self.build_platforms
        platforms.sort(key=len, reverse=True)
        for platform in self.build_platforms:
            for build_data in builds:
                if platform in build_data['platform']:
                    multiarch_builds.append(build_data)
                    break
            builds = [build_data for build_data in builds if platform not in build_data['platform']]

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
                    build_url = '%s%s' % (directory, filename)
                    build_data = utils.get_build_data(build_url, builder_type='buildbot')
                    if build_data:
                        builds.append(build_data)
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
                    build_url = urlparse.urljoin('file:', filepath)
                    build_data = utils.get_build_data(build_url, builder_type='buildbot')
                    if build_data:
                        builds.append(build_data)
                    break
        if not builds:
            logger.error('No builds found.')
        logger.debug('find_builds_by_directory: builds %s' % builds)
        return builds

    def find_builds_by_time(self, start_time, end_time, inclusive=True):
        logger.debug('Finding builds between %s and %s' %
                     (start_time, end_time))
        if not start_time.tzinfo or not end_time.tzinfo:
            raise Exception('find_builds_by_time: naive times not permitted')

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

                if (build_time < start_time or
                    (inclusive and build_time > end_time) or
                    (not inclusive and build_time >= end_time)):
                    continue

                build_links = url_links(directory_href)
                for build_link in build_links:
                    filename = build_link.get_text()
                    logger.debug('find_builds_by_time: checking filename: %s' % filename)
                    if self.build_regex.match(filename):
                        logger.debug('find_builds_by_time: found filename: %s' % filename)
                        build_url = '%s%s' % (directory_href, filename)
                        build_data = utils.get_build_data(build_url, builder_type='buildbot')
                        if build_data:
                            builds.append(build_data)
                        break
        if not builds:
            logger.error('No builds found.')
        return builds

    def find_builds_by_revision(self, first_revision, last_revision, inclusive=True):
        logger.debug('Finding builds between revisions %s and %s' %
                     (first_revision, last_revision))

        range = datetime.timedelta(hours=12)
        builds = []

        for repo in self.repos:
            try:
                first_datetime, last_datetime = get_revision_datetimes(
                    repo,
                    first_revision,
                    last_revision)
            except Exception:
                logger.exception('repo %s' % repo)
                continue

            logger.debug('find_builds_by_revision: repo %s, '
                         'first_revision: %s, first_datetime: %s, '
                         'last_revision: %s, last_datetime: %s' % (
                             repo, first_revision, first_datetime,
                             last_revision, last_datetime))
            if not first_datetime or not last_datetime:
                continue
            for search_directory_repo, search_directory in self.get_search_directories_by_time(first_datetime,
                                                                                               last_datetime):
                # search_directory_repo is not None for FtpTinderbox builds and
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
                        link_format, link_datetime = parse_datetime(datetimestring, tz=PACIFIC)
                        if not format:
                            format = link_format
                        logger.debug('find_builds_by_revisions: link_format: %s,'
                                     'link_datetime: %s' % (link_format, link_datetime))
                        if link_datetime > first_datetime - range and link_datetime < last_datetime + range:
                            datetimestamps.append(link_datetime)
                    except ValueError:
                        logger.exception()
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
                                build_data = utils.get_build_data(build_url, builder_type='buildbot')
                                if build_data:
                                    if repo != build_data['repo']:
                                        logger.info('find_builds_by_revisions: '
                                                    'skipping build: %s != %s'
                                                    % (repo,
                                                       urls_repos[build_data['repo']]))
                                        continue
                                    if build_data['revision'].startswith(first_revision):
                                        start_time = build_data['date']
                                    elif build_data['revision'].startswith(last_revision):
                                        end_time = build_data['date']
                                    if start_time and not end_time:
                                        builds.append(build_data)
                                        break
                    if end_time and inclusive:
                        builds.append(build_data)
                        break

        return builds


class FtpNightly(FtpBuildLocation):

    def __init__(self, repos, buildtypes,
                 product, build_platforms, buildfile_ext):
        FtpBuildLocation.__init__(self, repos, buildtypes,
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
        logger.debug('FtpNightly: nightly_dirname_regexs: %s' % patterns)

    def does_build_directory_contain_repo_name(self):
        return True

    def get_search_directories_by_time(self, start_time, end_time):
        # ftp directory directories use Pacific time in their names.
        start_time = start_time.astimezone(PACIFIC)
        end_time = end_time.astimezone(PACIFIC)
        logger.debug('FtpNightly:get_search_directories_by_time(%s, %s)' % (start_time, end_time))
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
        logger.debug('FtpNightly:build_time_from_directory_name(%s)' % directory_name)
        build_time = None
        dirnamematch = None
        for r in self.nightly_dirname_regexs:
            dirnamematch = r.match(directory_name)
            if dirnamematch:
                break
        if dirnamematch:
            format, build_time = parse_datetime(directory_name, tz=PACIFIC)
            logger.debug('FtpNightly:build_time_from_directory_name(%s)=%s,%s)' %
                         (directory_name, format, build_time))
        return build_time

    def directory_names_from_datetimestamp(self, datetimestamp):
        dates = [convert_datetime_to_string(datetimestamp, DIRECTORY_DATE, tz=PACIFIC), # only really needed for non mobile
                 convert_datetime_to_string(datetimestamp, DIRECTORY_DATETIME, tz=PACIFIC)]
        for platform in self.build_platforms:
            for date in dates:
                for repo in self.repos:
                    yield repo, '%s-%s-%s' % (date, repo, platform)


class FtpTinderbox(FtpBuildLocation):

    main_http_url = 'http://ftp.mozilla.org/pub/mozilla.org/mobile/tinderbox-builds/'

    def __init__(self, repos, buildtypes,
                 product, build_platforms, buildfile_ext):
        FtpBuildLocation.__init__(self, repos, buildtypes,
                                  product, build_platforms, buildfile_ext)

    def get_search_directories_by_time(self, start_time, end_time):
        # Note: start_time, end_time are unused.
        logger.debug('FtpTinderbox:get_search_directories_by_time(%s, %s)' % (start_time, end_time))
        # FIXME: Can we be certain that there's only one buildID (unique
        # timestamp) regardless of repo (at least m-i vs m-c)?
        for platform in self.build_platforms:
            for repo in self.repos:
                for buildtype in self.buildtypes:
                    if buildtype == 'opt':
                        buildtypestring = ''
                    elif buildtype == 'debug':
                        buildtypestring = '-debug'
                    yield repo, '%s%s-%s%s/' % (self.main_http_url, repo, platform, buildtypestring)

    def build_time_from_directory_name(self, directory_name):
        logger.debug('FtpTinderbox:build_time_from_directory_name(%s)' % directory_name)
        try:
            date_format, build_time = parse_datetime(directory_name, tz=UTC)
        except ValueError:
            build_time = None
        logger.debug('FtpTinderbox:build_time_from_directory_name: (%s, %s)' %
                     (directory_name, build_time))
        return build_time

    def directory_names_from_datetimestamp(self, datetimestamp):
        yield None, convert_datetime_to_string(datetimestamp, TIMESTAMP)


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
        return TaskClusterBuilds(self.repos, self.buildtypes,
                                 self.product, self.build_platforms,
                                 self.buildfile_ext,
                                 'nightly' in s)

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
        logger.debug('Finding %s builds between %s and %s' %
                     (build_location_name, start_time, end_time))
        if not start_time.tzinfo or not end_time.tzinfo:
            raise Exception('find_builds_by_time: naive times not permitted')

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
            test_package_names=None, builder_type=None):
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
            metadata = self.build_metadata(buildurl, self.override_build_dir, builder_type=builder_type)
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
                utils.urlretrieve(buildurl, tmpf.name)
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
                utils.urlretrieve(symbols_url, tmpf.name)
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
            # Do not skip installing the tests if the tests directory
            # already exists, since it may be the case that trigger_builds.py
            # was used to specify a new test package which has not already
            # been installed.
            tests_path = os.path.join(cache_build_dir, 'tests')
            # XXX: assumes fixed buildurl-> robocop mapping
            robocop_url = urlparse.urljoin(buildurl, 'robocop.apk')
            robocop_path = os.path.join(cache_build_dir, 'robocop.apk')
            if force or not os.path.exists(robocop_path):
                tmpf = tempfile.NamedTemporaryFile(delete=False)
                tmpf.close()
                try:
                    utils.urlretrieve(robocop_url, tmpf.name)
                except IOError:
                    os.unlink(tmpf.name)
                    err = 'IO Error retrieving robocop.apk: %s.' % robocop_url
                    logger.exception(err)
                    return {'success': False, 'error': err}
                shutil.move(tmpf.name, robocop_path)
            test_packages_url = re.sub('.apk$', '.test_packages.json', buildurl)
            logger.info('downloading test package json %s' % test_packages_url)
            test_packages = utils.get_remote_json(test_packages_url)
            if not test_packages:
                logger.warning('test package json %s not found' %
                               test_packages_url)
                test_packages_url = urlparse.urljoin(buildurl,
                                                     'test_packages.json')
                logger.info('falling back to test package json %s' %
                            test_packages_url)
                test_packages = utils.get_remote_json(test_packages_url)

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
                if not test_packages:
                    # Only use the old style tests zip file if
                    # the split test_packages.json was not found.
                    logger.warning('Using the default test package')
                    tests_url = re.sub('.apk$', '.tests.zip', buildurl)
                    test_package_files = set([os.path.basename(tests_url)])
                else:
                    err = 'No test packages specified for build %s' % buildurl
                    logger.exception(err)
                    return {'success': False, 'error': err}
            for test_package_file in test_package_files:
                test_package_path = os.path.join(cache_build_dir,
                                                 test_package_file)
                test_package_url = urlparse.urljoin(buildurl, test_package_file)
                if not force and os.path.exists(test_package_path):
                    logger.info('skipping already downloaded '
                                'test package %s' % test_package_url)
                    continue
                logger.info('downloading test package %s' % test_package_url)
                tmpf = tempfile.NamedTemporaryFile(delete=False)
                tmpf.close()
                try:
                    utils.urlretrieve(test_package_url, tmpf.name)
                except IOError:
                    os.unlink(tmpf.name)
                    err = 'IO Error retrieving tests: %s.' % test_package_url
                    logger.exception(err)
                    return {'success': False, 'error': err}
                try:
                    tests_zipfile = zipfile.ZipFile(tmpf.name)
                    tests_zipfile.extractall(tests_path)
                    tests_zipfile.close()
                    # Move the test package zip file to the cache
                    # build directory so we can check if it has been
                    # downloaded.
                    shutil.move(tmpf.name, test_package_path)
                except zipfile.BadZipfile:
                    err = 'Zip file error retrieving tests: %s.' % test_package_url
                    logger.exception(err)
                    return {'success': False, 'error': err}
            if test_packages:
                # Save the test_packages.json file
                test_packages_json_path = os.path.join(cache_build_dir,
                                                      'test_packages.json')
                file(test_packages_json_path, 'w').write(
                    json.dumps(test_packages))

        metadata = self.build_metadata(buildurl, cache_build_dir, builder_type=builder_type)
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
            # Use local time now since os.stat returns times in localtime.
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

    def build_metadata(self, build_url, build_dir, builder_type='taskcluster'):
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
        build_data = utils.get_build_data(build_url, builder_type=builder_type)
        if not build_data:
            raise BuildCacheException('Could not get build_data for %s' % build_url)
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
        ver = cfg.get('App', 'Version')

        metadata = BuildMetadata(url=build_url,
                                 dir=build_dir,
                                 tree=build_data['repo'],
                                 id=build_data['id'],
                                 revision=build_data['revision'],
                                 changeset=build_data['changeset'],
                                 changeset_dirs=build_data['changeset_dirs'],
                                 app_name=procname,
                                 version=ver,
                                 build_type=build_data['build_type'],
                                 treeherder_url=self.treeherder_url,
                                 abi=build_data['abi'],
                                 sdk=build_data['sdk'],
                                 nightly=build_data['nightly'],
                                 platform=build_data['platform'],
                                 builder_type=builder_type)
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
                 changeset=None,
                 changeset_dirs=[],
                 app_name=None,
                 version=None,
                 build_type=None,
                 treeherder_url=None,
                 abi=None,
                 sdk=None,
                 nightly=None,
                 platform=None,
                 builder_type=None):
        logger.debug('BuildMetadata: url: %s, dir: %s, tree: %s, id: %s, '
                     'revision: %s, changeset: %s, changeset_dirs: %s, '
                     'app_name: %s, version: %s, '
                     'build_type: %s, treeherder_url: %s, abi: %s, sdk: %s, '
                     'nightly: %s, platform: %s, builder_type: %s, '
                     '' % (
                         url, dir, tree, id,
                         revision, changeset, changeset_dirs, app_name, version,
                         build_type, treeherder_url, abi, sdk,
                         nightly, platform, builder_type ))
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
        self.changeset = changeset
        self.changeset_dirs = changeset_dirs
        self.app_name = app_name
        self.version = version
        self.abi = abi
        self.sdk = sdk
        self.nightly = nightly
        self.platform = platform
        self.builder_type = builder_type

        logger.debug('BuildMetadata: %s' % self.__dict__)

    @property
    def date(self):
        if not self._date:
            format, self._date = parse_datetime(self.id, tz=UTC)
        return self._date

    def __str__(self):
        d = self.__dict__.copy()
        d['date'] = self.date
        del d['_date']
        return '%s' % d

    def to_json(self):
        j = {
            '__class__': 'BuildMetadata',
            'url': self.url,
            'dir': self.dir,
            'symbols': self.symbols,
            'tree': self.tree,
            'type': self.type,
            'id': self.id,
            'revision': self.revision,
            'changeset': self.changeset,
            'changeset_dirs': json.dumps(self.changeset_dirs),
            'app_name': self.app_name,
            'version': self.version,
            'abi': self.abi,
            'sdk': self.sdk,
            'nightly': self.nightly,
            'platform': self.platform,
            'builder_type': self.builder_type,
        }
        logger.debug('BuildMetadata: to_json: %s' % j)
        return j

    def from_json(self, j):
        if '__class__' not in j or j['__class__'] != 'BuildMetadata':
            raise ValueError
        logger.debug('BuildMetadata: from_json: %s' % j)
        self.url = j['url']
        self.dir = j['dir']
        self.symbols = j['symbols']
        self.tree = j['tree']
        self.type = j['type']
        self.id = j['id']
        self.revision = j['revision']
        self.changeset = j['changeset']
        self.changeset_dirs = json.loads(j['changeset_dirs'])
        self.app_name = j['app_name']
        self.version = j['version']
        self.abi = j['abi']
        self.sdk = j['sdk']
        self.nightly = j['nightly']
        self.platform = j['platform']
        self.builder_type = j['builder_type']
        return self
