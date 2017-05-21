# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# get_remote_content modelled on treeherder/etc/common.py

import json
import logging
import math
import multiprocessing
import os
import os.path
import random
import re
import sys
import time
import traceback
import urlparse
import uuid

import requests

import taskcluster

import build_dates
import builds

from sensitivedatafilter import SensitiveDataFilter

def getLoggerFormatString(loglevel):
    if loglevel == logging.DEBUG:
        formatstring = '%(asctime)s %(process)-6d %(processName)-14s %(threadName)-12s %(name)15s %(levelname)-8s %(message)s'
    else:
        formatstring = '%(asctime)s %(name)15s %(levelname)-8s %(message)s'
    return formatstring

SENSITIVE_DATA_FILTER = None
def recordSensitiveData(sensitive_data):
    global SENSITIVE_DATA_FILTER
    SENSITIVE_DATA_FILTER = SensitiveDataFilter(sensitive_data)
    return SENSITIVE_DATA_FILTER

def getSensitiveDataFilter():
    return SENSITIVE_DATA_FILTER

def getLogger(name=None):
    """Return a safe logger for the current process.

    :param name: Name of logger to return. If name is not specified,
        it defaults to None which will return a logger whose name is
        the name of the current process as returned by multiprocessing.current_process().name.

    If utils.recordSensitiveData() has been called prior to getLogger,
    then the root logger will be guaranteed to have a SensitiveDataFilter
    applied otherwise getLogger will assert.
    """
    # Make sure the root logger has a sensitive data filter.
    root_logger = logging.getLogger('')
    if SENSITIVE_DATA_FILTER and not hasattr(root_logger, 'SENSITIVE_DATA_FILTER'):
        root_logger.addFilter(SENSITIVE_DATA_FILTER)
        setattr(root_logger, 'SENSITIVE_DATA_FILTER', SENSITIVE_DATA_FILTER)
    assert(not SENSITIVE_DATA_FILTER or hasattr(root_logger, 'SENSITIVE_DATA_FILTER'))

    if name == None:
        name = multiprocessing.current_process().name
    logger = logging.getLogger(name)

    # Make sure this logger has a sensitive data filter.
    if SENSITIVE_DATA_FILTER and not hasattr(logger, 'SENSITIVE_DATA_FILTER'):
        logger.addFilter(SENSITIVE_DATA_FILTER)
        setattr(logger, 'SENSITIVE_DATA_FILTER', SENSITIVE_DATA_FILTER)
    assert(not SENSITIVE_DATA_FILTER or hasattr(logger, 'SENSITIVE_DATA_FILTER'))

    return logger

def get_remote_text(url):
    """Return the string containing the contents of a remote url if the
    request is successful, otherwise return None.

    :param url: url of content to be retrieved.
    """
    logger = getLogger()

    try:
        parse_result = urlparse.urlparse(url)
        if not parse_result.scheme or parse_result.scheme.startswith('file'):
            local_file = open(parse_result.path)
            with local_file:
                return local_file.read()

        while True:
            r = requests.get(url, headers={'user-agent': 'autophone'})
            if r.ok:
                return r.text
            elif r.status_code != 503:
                logger.warning("Unable to open url %s : %s",
                               url, r.reason)
                return None
            # Server is too busy. Wait and try again.
            # See https://bugzilla.mozilla.org/show_bug.cgi?id=1146983#c10
            logger.warning("HTTP 503 Server Too Busy: url %s", url)
            time.sleep(60 + random.randrange(0, 30, 1))
    except Exception:
        logger.exception('Unable to open %s', url)

    return None


def get_remote_json(url):
    """Return the json representation of the contents of a remote url if
    the HTTP response code is 200, otherwise return None.

    :param url: url of content to be retrieved.
    """
    logger = getLogger()
    content = get_remote_text(url)
    if content:
        content = json.loads(content)
    logger.debug('get_remote_json(%s): %s', url, content)
    return content


def get_build_data_from_taskcluster_task_definition(task_definition):
    logger = getLogger()

    # 1 = project/repo, 2 = pushdate CCYYMMDDHHMMSS, 3 = platform, 4 = build_type
    re_route_pushdate = re.compile(r'index\.gecko\.v2\.([^.]+)\.pushdate\.\d{4}\.\d{2}\.\d{2}\.(\d{14})\.mobile\.(android.*)-(opt|debug)')

    # 1 = project/repo, 2 = revision, # 3 = platform, 4 = build_type
    re_route_revision = re.compile(r'index\.gecko\.v2\.([^.]+)\.revision\.([^.]+)\.mobile\.(android.*)-(opt|debug)')

    # 1 - api, 2 - custom
    re_platform = re.compile(r'android-(x86|api-\d+)-?(\w+)?')

    repo = None
    pushdate = None
    revision = None
    platform = None
    build_type = None
    success = False

    for route in task_definition['routes']:
        match = re_route_pushdate.match(route)
        if match:
            (repo, pushdate, platform, build_type) = match.groups()
        else:
            match = re_route_revision.match(route)
            if match:
                (repo, revision, platform, build_type) = match.groups()
        if repo and pushdate and revision and platform and build_type:
            success = True
            break
    logger.debug('get_build_data_from_taskcluster_task_definition: %s, %s, %s, %s, %s',
                 repo, pushdate, revision, platform, build_type)
    if not success:
        return None

    formatstr, build_date = build_dates.parse_datetime(pushdate, tz=build_dates.UTC)

    match = re_platform.match(platform)
    if match:
        (api, extra) = match.groups()
        if api == 'x86':
            api = 'api-15'
            abi = 'x86'
        else:
            abi = 'arm'

    changeset = builds.REPO_URLS[repo] + 'rev/' + revision

    build_data = {
        'url': None,
        'id': pushdate,
        'date': build_date,
        'changeset': changeset,
        'changeset_dirs': get_changeset_dirs(changeset),
        'revision': revision,
        'builder_type': 'taskcluster',
        'repo': repo,
        'abi': abi,
        'sdk': api,
        'build_type': build_type,
        'nightly': False,
        'platform': platform,
    }
    logger.debug('get_build_data_from_taskcluster_task_definition: %s', build_data)
    return build_data


def get_build_data(build_url, builder_type='taskcluster'):
    """Return a dict containing information about a build.

    :param build_url: string containing url to the firefox build.
    :param builder_type: either 'buildbot' or'taskcluster'

    Returns None if the file does not exist or does not contain build
    data, otherwise returns a dict with keys:

       'url'          : url to build
       'id'           : CCYYMMDDHHMMSS string in UTC
       'date'         : build id as UTC datetime
       'changeset'    : full url to changeset,
       'changeset_dirs' : set of directories where changes occurred.
       'revision'     : revision,
       'builder_type' : either 'buildbot' or 'taskcluster'
       'repo'         : repository name
       'abi'          : 'arm' or 'x86'
       'sdk'          : 'api-<minimum sdk>' if known or None
       'build_type'   : 'opt' or 'debug'
       'nightly'      : True if this is a nighlty build.
       'platform'     : android, android-x86, android-<sdk>
    """
    logger = getLogger()
    logger.debug('get_build_data(%s, builder_type=%s)', build_url, builder_type)

    re_taskcluster_build_url = re.compile(r'https://queue.taskcluster.net/v1/task/([^/]+)/runs/\d/artifacts/public/build/')
    match = re_taskcluster_build_url.match(build_url)
    if match:
        task_id = match.group(1)
        logger.debug("get_build_data: taskId %s", task_id)
        task_definition = get_taskcluster_task_definition(task_id)
        build_data = get_build_data_from_taskcluster_task_definition(task_definition)
        if build_data:
            build_data['url'] = build_url
        return build_data

    if builder_type == 'taskcluster':
        build_id_tz = build_dates.UTC
    else:
        build_id_tz = build_dates.PACIFIC

    build_id = None
    changeset = None
    revision = None
    repo = None
    abi = None
    sdk = None
    build_type = None
    platform = None
    nightly = None

    # Parse the url for meta data if possible.
    re_platform = re.compile(r'(android)-?(x86)?-?(api-\d+)?-?(debug)?')
    re_mozconfig_sdk = re.compile(r'(api-\d+)')
    ftp_build = False
    re_tinderbox = re.compile(r'https?://ftp.mozilla.org/pub/mobile/tinderbox-builds/(.*)-(android[^/]*)/\d+/fennec.*\.apk$')
    match_tinderbox = re_tinderbox.match(build_url)
    if match_tinderbox:
        ftp_build = True
        nightly = False
        (repo, platform_api_build_type) = match_tinderbox.groups()
        logger.debug('get_build_data: match_tinderbox: repo: %s, platform_api_build_type: %s',
                     repo, platform_api_build_type)
    else:
        re_nightly = re.compile(r'https?://ftp.mozilla.org/pub/mobile/nightly/\d{4}/\d{2}/\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}-(.*)-(android[^/]*)/fennec.*\.apk$')
        match_nightly = re_nightly.match(build_url)
        if match_nightly:
            ftp_build = True
            nightly = True
            (repo, platform_api_build_type) = match_nightly.groups()
            logger.debug('get_build_data: match_nightly: repo: %s, platform_api_build_type: %s',
                         repo, platform_api_build_type)
    if ftp_build:
        if builder_type == 'taskcluster':
            logger.error('get_build_data(%s, builder_type=%s) for ftp build. '
                         'Setting timezone to Pacific.', build_url, builder_type)
            build_id_tz = build_dates.PACIFIC
        match_platform = re_platform.match(platform_api_build_type)
        if match_platform:
            (platform, abi, sdk, debug) = match_platform.groups()
            build_type = 'debug' if debug else 'opt'
            if not abi:
                abi = 'arm'
            elif abi == 'i386' or abi == 'i686':
                abi = 'x86'
            logger.debug('get_build_data: platform: %s, abi: %s, sdk: %s, debug: %s',
                         platform, abi, sdk, debug)

    build_prefix, build_ext = os.path.splitext(build_url)

    build_json_url = build_prefix + '.json'
    build_json = get_remote_json(build_json_url)
    if build_json:
        build_id = build_json['buildid']
        formatstr, build_date = build_dates.parse_datetime(build_id, tz=build_id_tz)
        # convert buildid to UTC to match Taskcluster
        build_id = build_dates.convert_datetime_to_string(build_date,
                                                          build_dates.BUILDID,
                                                          tz=build_dates.UTC)
        if not abi:
            abi = build_json['target_cpu']
            if abi == 'i386' or abi == 'i686':
                abi = 'x86'
        moz_source_repo = build_json['moz_source_repo'].replace('MOZ_SOURCE_REPO=', '')
        repo = os.path.basename(moz_source_repo)
        revision = build_json['moz_source_stamp']
        changeset = moz_source_repo + '/rev/' + revision
        if not sdk and 'mozconfig' in build_json:
            search = re_mozconfig_sdk.search(build_json['mozconfig'])
            if search:
                sdk = search.group(1)
        logger.debug('get_build_data: build_json: build_id: %s, platform: %s, abi: %s, '
                     'sdk: %s, repo: %s, revision: %s, changeset: %s',
                     build_id, platform, abi, sdk, repo, revision, changeset)
    if build_type is None or sdk is None or nightly is None or platform is None:
        build_mozinfo_json_url = build_prefix + '.mozinfo.json'
        build_mozinfo_json = get_remote_json(build_mozinfo_json_url)
        if build_mozinfo_json:
            if not build_type and 'debug' in build_mozinfo_json:
                build_type = 'debug' if build_mozinfo_json['debug'] else 'opt'
            if not sdk:
                if 'android_min_sdk' in build_mozinfo_json:
                    sdk = 'api-%s' % build_mozinfo_json['android_min_sdk']
                else:
                    if 'mozconfig' in build_mozinfo_json:
                        search = re_mozconfig_sdk.search(build_mozinfo_json['mozconfig'])
                        if search:
                            sdk = search.group(1)
            if not platform:
                platform = build_mozinfo_json['os']
                if sdk:
                    platform += sdk
            if not nightly and 'nightly_build' in build_mozinfo_json:
                nightly = build_mozinfo_json['nightly_build']
            logger.debug('get_build_data: mozinfo build_type: %s, sdk: %s, nightly: %s',
                         build_type, sdk, nightly)

    if not build_id or not changeset or not repo or not revision:
        build_id_tz = build_dates.PACIFIC
        build_txt = build_prefix + '.txt'
        content = get_remote_text(build_txt)
        if not content:
            return None

        lines = content.splitlines()
        if len(lines) < 1:
            return None

        buildid_regex = re.compile(r'([\d]{14})$')
        changeset_regex = re.compile(r'.*/([^/]*)/rev/(.*)')

        buildid_match = buildid_regex.match(lines[0])

        if len(lines) >= 2:
            changeset_match = changeset_regex.match(lines[1])
        else:
            logger.warning("Unable to find revision in %s, results cannot be "
                           " uploaded to treeherder", build_url)
            changeset_match = changeset_regex.match("file://local/rev/local")
            lines.append("file://local/rev/local")
        if not buildid_match or not changeset_match:
            return None

        txt_build_id = lines[0]
        txt_changeset = lines[1]
        txt_repo = changeset_match.group(1)
        txt_revision = changeset_match.group(2)
        logger.debug('get_build_data: txt build_id: %s, changeset: %s, repo: %s, revision: %s',
                     txt_build_id, txt_changeset, txt_repo, txt_revision)

        formatstr, build_date = build_dates.parse_datetime(txt_build_id, tz=build_id_tz)
        # convert buildid to UTC to match Taskcluster
        txt_build_id = build_dates.convert_datetime_to_string(build_date,
                                                              build_dates.BUILDID,
                                                              tz=build_dates.UTC)

        if not build_id:
            build_id = txt_build_id
        elif build_id != txt_build_id:
            logger.warning('get_build_data: build_id %s != txt_build_id %s', build_id, txt_build_id)

        if not changeset:
            changeset = txt_changeset
        elif txt_changeset not in changeset:
            logger.warning('get_build_data: txt_changeset %s not in changeset %s',
                           txt_changeset, changeset)

        if not repo:
            repo = txt_repo
        else:
            logger.warning('get_build_data: repo %s != txt_repo %s', repo, txt_repo)

        if not revision:
            revision = txt_revision
        else:
            logger.warning('get_build_data: revision %s != txt_revision %s', revision, txt_revision)

    platform = 'android'
    if abi == 'x86':
        platform += '-x86'
    if sdk:
        platform += '-' + sdk

    build_data = {
        'url'        : build_url,
        'id'         : build_id,
        'date'       : build_date.astimezone(build_dates.UTC),
        'changeset'  : changeset,
        'changeset_dirs': get_changeset_dirs(changeset),
        'revision'   : revision,
        'builder_type': builder_type,
        'repo'       : repo,
        'abi'        : abi,
        'sdk'        : sdk,
        'build_type' : build_type,
        'nightly'    : nightly,
        'platform'   : platform,
    }
    logger.debug('get_build_data: %s', build_data)
    return build_data


def get_changeset_dirs(changeset_url, max_changesets=32):
    """Return a list of the directories changed in this changeset.

    If the number of changesets exceeds max_changesets, return []
    which will match any directory defined for a test.
    """
    logger = getLogger()
    url = changeset_url.replace('rev/', 'json-pushes?changeset=')
    pushlog = get_remote_json(url)

    if not pushlog:
        logger.debug('get_changeset_dirs: Could not find pushlog at %s', url)
        return []

    dirs_set = set()
    for pushid in pushlog:
        logger.debug('get_changeset_dirs: %s: pushid %s', changeset_url, pushid)
        try:
            changesets = pushlog[pushid]['changesets']
            logger.debug('get_changeset_dirs: %s: %s changesets %s',
                         changeset_url, len(changesets), changesets)
            if len(changesets) > max_changesets:
                logger.warning(
                    'get_changeset_dirs: %s contains %s '
                    'changesets exceeding max %s. '
                    'Returning [].',
                    changeset_url, len(changesets),
                    max_changesets)
                return []
        except:
            # We normally see a 'user' in this json blob, more might
            # be added in the future.
            logger.debug('get_changeset_dirs: Exception getting changesets: %s',
                         traceback.format_exc())
            continue
        base_url = os.path.dirname(changeset_url).replace('rev', 'raw-rev')
        for changeset in changesets:
            #TODO: When Bug 1286353 is fixed, use json-rev for this,
            #      as this requires retrieving and scanning the whole
            #      diff.
            url = os.path.join(base_url, changeset)
            logger.debug('get_changeset_dirs: diff url: %s', url)
            diff = get_remote_text(url)
            if diff:
                for line in diff.splitlines():
                    if line.startswith('#') or line.find('/dev/null') != -1:
                        continue
                    if line.startswith('+++ b/') or line.startswith('--- a/'):
                        # skip markers, space and leading slash
                        path = os.path.dirname(line[6:])
                        # Note that if the changeset was due to a
                        # change in a top level file or tagging of a
                        # branch, then path will be empty which will
                        # result in all directory restricted tests
                        # running which is alright.
                        dirs_set.add(path)
            else:
                logger.debug('get_changeset_dirs: Could not find diff for '
                             'revision %s at %s', changeset, url)
                # We return an empty list here to force the test to be
                # run, in case the missing diff here contained files
                # we care about.
                return []

    dirs = list(dirs_set)
    logger.debug('get_changeset_dirs: %s', dirs)
    return dirs


def generate_guid():
    return str(uuid.uuid4())


# These computational functions are taken from Talos:filter.py
def mean(series):
    """
    mean of data needs at least one data point
    """
    if len(series) == 0:
        return 0

    total = 0
    for v in series:
        total += v

    return total/len(series)


def median(series):
    """
    median of data; needs at least one data point
    """
    series = sorted(series)
    if len(series) % 2:
        # odd
        return series[len(series)/2]
    else:
        # even
        middle = len(series)/2  # the higher of the middle 2, actually
        return 0.5*(series[middle-1] + series[middle])


def geometric_mean(series):
    """
    geometric_mean: http://en.wikipedia.org/wiki/Geometric_mean
    """
    if len(series) == 0:
        return 0
    total = 0
    for i in series:
        total += math.log(i+1)
    return math.exp(total / len(series)) - 1


def host():
    return os.uname()[1]


def urlretrieve(url, dest, max_attempts=3):
    """Downloads the contents of url to the path dest while handling
    partial downloads by retrying the download up to max_attempts
    times.

    :param url: url to be downloaded.
    :param dest: path where to save downloaded content.
    :param max_attempts: maximum number of attempts to retry partial
        downloads. Defaults to 3.
    """
    logger = getLogger()

    for attempt in range(max_attempts):
        try:
            r = requests.get(url, stream=True)
            if not r.ok:
                r.raise_for_status()
            with open(dest, 'wb') as dest_file:
                for chunk in r.iter_content(chunk_size=4096):
                    dest_file.write(chunk)
            break
        except requests.HTTPError, http_error:
            logger.info("urlretrieve(%s, %s) %s", url, dest, http_error)
            raise
        except (requests.ConnectionError, requests.Timeout), e:
            logger.warning("utils.urlretrieve: %s: Attempt %s: %s",
                           url, attempt, e)
            if attempt == max_attempts - 1:
                raise


def get_taskcluster_task_definition(task_id):
    queue = taskcluster.queue.Queue()
    task_definition = queue.task(task_id)
    return task_definition


def taskcluster_artifacts(task_id, run_id):
    logger = getLogger()
    queue = taskcluster.queue.Queue()
    response = queue.listArtifacts(task_id, run_id)
    while True:
        if 'artifacts' not in response:
            logger.warning('taskcluster_artifacts: listArtifacts(%s, %s) '
                           'response missing artifacts', task_id, run_id)
            raise StopIteration
        artifacts = response['artifacts']
        for artifact in artifacts:
            logger.debug('taskcluster_artifacts: %s', artifact)
            yield artifact
        if 'continuationToken' not in response:
            raise StopIteration
        logger.debug('taskcluster_artifacts: continuing listArtifacts(%s, %s)',
                     task_id, run_id)
        response = queue.listArtifacts(task_id, run_id, {
            'continuationToken': response['continationToken']})

_AUTOPHONE_PATH = None

def autophone_path():
    global _AUTOPHONE_PATH

    if not _AUTOPHONE_PATH:
        _AUTOPHONE_PATH = os.path.dirname(sys.argv[0])
        if not os.path.isabs(_AUTOPHONE_PATH):
            _AUTOPHONE_PATH = os.path.join(os.getcwd(), _AUTOPHONE_PATH)
        _AUTOPHONE_PATH = os.path.normpath(_AUTOPHONE_PATH)
    return _AUTOPHONE_PATH

def find_files(path):
    """Return list of all files contained in path"""

    def add_file(files, dir_name, file_names):
        for file_name in file_names:
            file_path = os.path.join(dir_name, file_name)
            if os.path.isdir(file_path):
                continue
            files.append(file_path)

    files = []
    os.path.walk(path, add_file, files)
    return files
