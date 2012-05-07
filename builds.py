# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import base64
import datetime
import logging
import os
import pytz
import re
import shutil
import urllib
import urllib2

dirnames = [re.compile('(.*)-mozilla-central-android$')]


class BuildCache(object):

    MAX_NUM_BUILDS = 10

    def __init__(self, cache_dir='builds'):
        self.cache_dir = cache_dir
        if not os.path.exists(self.cache_dir):
            os.mkdir(self.cache_dir)

    def nightly_ftpdir(self, year, month):
        return 'ftp://ftp.mozilla.org/pub/mobile/nightly/%d/%02d/' % (year, month)

    def find_builds(self, time_range, type='nightly'):
        builds = []
        fennecregex = re.compile("fennec.*\.android-arm\.apk")
        ftpdirs = []
        if type == 'nightly':
            y = time_range[0].year
            m = time_range[0].month
            while y < time_range[1].year or m <= time_range[1].month:
                ftpdirs.append(self.nightly_ftpdir(y, m))
                if m == 12:
                    y += 1
                    m = 1
                else:
                    m += 1
        elif type == 'tinderbox':
            # FIXME: Can we be certain that there's only one buildID (unique
            # timestamp) regardless of branch (at least m-i vs m-c)?
            ftpdirs.append('ftp://ftp.mozilla.org/pub/mozilla.org/mobile/tinderbox-builds/mozilla-inbound-android/')
            ftpdirs.append('ftp://ftp.mozilla.org/pub/mozilla.org/mobile/tinderbox-builds/mozilla-central-android/')

        for d in ftpdirs:
            f = urllib2.urlopen(d)
            for line in f:
                build_time = None
                if type == 'nightly':
                    srcdir = line.split(' ')[-1].strip()
                    dirnamematch = None
                    for r in dirnames:
                        dirnamematch = r.match(srcdir)
                        if dirnamematch:
                            break
                    if dirnamematch:
                        build_time = datetime.datetime.strptime(dirnamematch.group(1),
                                                                '%Y-%m-%d-%H-%M-%S')
                    else:
                        continue
                elif type == 'tinderbox':
                    srcdir = line.split()[8].strip()
                    build_time = datetime.datetime.fromtimestamp(int(srcdir), pytz.timezone('US/Pacific'))

                if build_time and (build_time < time_range[0] or
                                   build_time > time_range[1]):
                    continue

                newurl = d + srcdir
                f2 = urllib.urlopen(newurl)
                for l2 in f2:
                    filename = l2.split(' ')[-1].strip()
                    if fennecregex.match(filename):
                        fileurl = newurl + "/" + filename
                        builds.append(fileurl)
        return builds

    def build_date(self, url):
        # nightly urls are of the form
        #  ftp://ftp.mozilla.org/pub/mobile/nightly/<year>/<month>/<year>-
        #    <month>-<day>-<hour>-<minute>-<second>-<branch>-android/
        #    <buildfile>
        # tinderbox urls are of the form
        #   ftp://ftp.mozilla.org/pub/mozilla.org/mobile/tinderbox-builds/
        #     <branch>-android/<build timestamp>/<buildfile>
        builddate = None
        if 'nightly' in url:
            m = re.search('nightly\/[\d]{4}\/[\d]{2}\/([\d]{4}-[\d]{2}-[\d]{2}-[\d]{2}-[\d]{2}-[\d]{2})-', url)
            if not m:
                logging.error('bad URL "%s"' % url)
                return None
            builddate = datetime.datetime.strptime(m.group(1), '%Y-%m-%d-%H-%M-%S')
        elif 'tinderbox' in url:
            m = re.search('tinderbox-builds\/.*-android\/[\d]+\/', url)
            if not m:
                logging.error('bad URL "%s"' % url)
                return None
            builddate = datetime.datetime.fromtimestamp(int(m.group(1)),
                                                        pytz.timezone('US/Pacific'))
        return builddate

    def get(self, url):
        build_dir = base64.b64encode(url)
        self.clean_cache(build_dir)
        dir = os.path.join(self.cache_dir, build_dir)
        f = os.path.join(dir, 'build.apk')
        if not os.path.exists(dir):
            os.makedirs(dir)
        if not os.path.exists(f):
            urllib.urlretrieve(url, f)
        file(os.path.join(dir, 'lastused'), 'w')
        return f

    def clean_cache(self, preserve=None):
        builds = [(x, os.stat(os.path.join(self.cache_dir, x, 'lastused')).st_mtime) for x in
                  os.listdir(self.cache_dir) if preserve and x != preserve and os.path.exists(os.path.join(self.cache_dir, x, 'lastused'))]
        builds.sort(key=lambda x: x[1])
        while len(builds) > self.MAX_NUM_BUILDS:
            shutil.rmtree(os.path.join(self.cache_dir, builds.pop(0)[0]))
