# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# get_remote_content modelled on treeherder/etc/common.py

import httplib
import json
import logging
import os
import random
import re
import time
import urllib2
import urlparse
import uuid
import math

# Set the logger globally in the file, but this must be reset when
# used in a child process.
logger = logging.getLogger()

def get_remote_text(url):
    """Return the string containing the contents of a remote url if the
    HTTP response code is 200, otherwise return None.

    :param url: url of content to be retrieved.
    """
    conn = None

    try:
        scheme = urlparse.urlparse(url).scheme
        if not scheme:
            raise Exception('required scheme missing in url %s' % url)

        if scheme.startswith('file'):
            conn = urllib2.urlopen(url)
            return conn.read()

        while True:
            conn = urllib2.urlopen(url)
            code = conn.getcode()
            if code == 200:
                content = conn.read()
                return content
            if code != 503:
                logger.warning("Unable to open url %s : %s" % (
                    url, httplib.responses[code]))
                return None
            # Server is too busy. Wait and try again.
            # See https://bugzilla.mozilla.org/show_bug.cgi?id=1146983#c10
            logger.warning("HTTP 503 Server Too Busy: url %s" % url)
            conn.close()
            time.sleep(60 + random.randrange(0,30,1))
    except urllib2.HTTPError, e:
        logger.warning('%s Unable to open %s' % (e, url))
        return None
    except Exception:
        logger.exception('Unable to open %s' % url)
        return None
    finally:
        if conn:
            conn.close()

    return content


def get_remote_json(url):
    """Return the json representation of the contents of a remote url if
    the HTTP response code is 200, otherwise return None.

    :param url: url of content to be retrieved.
    """
    content = get_remote_text(url)
    if content:
        content = json.loads(content)
    return content


def get_build_data(build_url):
    """Return a dict containing information parsed from a build's .txt
    file.

    Returns None if the file does not exist or does not contain build
    data, otherwise returns a dict with keys:

       'id'       : build id of form 'CCYYMMDDHHSS'
       'changeset': url to changeset
       'repo'     : build repository
       'revision' : revision

    :param build_url: string containing url to the firefox build.
    """
    build_prefix, build_ext = os.path.splitext(build_url)
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
    changeset_match = changeset_regex.match(lines[1])
    if not buildid_match or not changeset_match:
        return None

    build_data = {
        'id' : lines[0],
        'changeset' : lines[1],
        'repo' : changeset_match.group(1),
        'revision' : changeset_match.group(2),
    }
    return build_data


def get_treeherder_revision_hash(treeherder_url, repo, revision):
    """Return the Treeherder revision_hash.

    :param treeherder_url: url to the treeherder server.
    :param repo: repository name for the revision.
    :param revision: revision id for the changeset.
    """
    if not treeherder_url or not repo or not revision:
        return None

    result_set_url = '%s/api/project/%s/resultset/?revision=%s' % (
        treeherder_url, repo, revision)
    result_set = get_remote_json(result_set_url)
    if not result_set:
        return None

    if ('results' not in result_set or len(result_set['results']) == 0 or
        'revision_hash' not in result_set['results'][0]):
        return None

    return result_set['results'][0]['revision_hash']


def generate_guid():
    return str(uuid.uuid4())


# These computational functions are taken from Talos:filter.py
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
