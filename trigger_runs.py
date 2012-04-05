# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import ConfigParser
import datetime
import math
import os
import pdb
import re
import shutil
import socket
import time
import urllib
import urllib2
from zipfile import ZipFile

# dirname should be what the directory our change is in - it either ends with
# "birch-android" for builds before 12/06 or "mozilla-central-android" for
# builds after that.
dirnames  = [#re.compile('.*birch-android$'),
             #re.compile('.*mozilla-aurora-android$'),
             re.compile('(.*)-mozilla-central-android$')]

jobnum = 0

def ftpdir(year, month):
    return 'ftp://ftp.mozilla.org/pub/mobile/nightly/%d/%02d/' % (year, month)


def trigger_command(build_time, fileurl, fname):
    global jobnum
    # This writes to the ini file and opens the apk to get the 
    # revision out of it then cleans up the apk.
    # Get the revision
    apkfile = ZipFile(fname)
    apkfile.extract("application.ini", "extdir")
    cfg = ConfigParser.RawConfigParser()
    cfg.read("extdir/application.ini")
    rev = cfg.get("App", "SourceStamp")
    ver = cfg.get("App", "Version")
    repo = cfg.get("App", "SourceRepository")
    procname = ""
    if repo == "http://hg.mozilla.org/mozilla-central":
        procname = "org.mozilla.fennec"
    elif repo == "http://hg.mozilla.org/releases/mozilla-aurora":
        procname = "org.mozilla.fennec_aurora"
    elif repo == "http://hg.mozilla.org/releases/mozilla-beta":
        procname = "org.mozilla.firefox"
    
    bldstamp = math.trunc(time.mktime(build_time.timetuple()))
    
    # Now we write to our config file
    cmd = 'triggerjobs buildurl=%s,blddate=%s,revision=%s,androidprocname=%s,version=%s,bldtype=opt' % (fileurl, bldstamp, rev, procname, ver)
    if os.path.exists(fname):
        os.remove(fname)
    if os.path.exists("extdir"):
        shutil.rmtree("extdir")
    return cmd


def build_commands(time_range):
    commands = []
    fennecregex = re.compile("fennec.*\.android-arm\.apk")
    ftpdirs = []
    y = time_range[0].year
    m = time_range[0].month
    while y < time_range[1].year or m <= time_range[1].month:
        ftpdirs.append(ftpdir(y, m))
        if m == 12:
            y += 1
            m = 1
        else:
            m += 1

    for d in ftpdirs:
        print 'Checking %s for builds...' % d
        f = urllib2.urlopen(d)
        for line in f:
            srcdir = line.split(' ')[-1].strip()
            dirnamematch = None
            for r in dirnames:
                dirnamematch = r.match(srcdir)
                if dirnamematch:
                    break
            if dirnamematch:
                build_time = datetime.datetime.strptime(dirnamematch.group(1),
                                                        '%Y-%m-%d-%H-%M-%S')
                if build_time < time_range[0] or build_time > time_range[1]:
                    continue

                # We have a matching directory.  Go get our apk.
                newurl = d + srcdir
                f2 = urllib.urlopen(newurl)
                for l2 in f2:
                    filename = l2.split(' ')[-1].strip()
                    if fennecregex.match(filename):
                        # Download the build and add URL to our list
                        fileurl = newurl + "/" + filename
                        print 'Fetching %s...' % fileurl
                        fname, hdrs = urllib.urlretrieve(fileurl)
                        print 'Checking metadata...'
                        commands.append(trigger_command(build_time, fileurl,
                                                        fname))
                        break

    return commands    


def from_iso_date_or_datetime(s):
    datefmt = '%Y-%m-%d'
    datetimefmt = datefmt + 'T%H:%M:%S'
    try:
        d = datetime.datetime.strptime(s, datetimefmt)
    except ValueError:
        d = datetime.datetime.strptime(s, datefmt)
    return d


def main(args, options):
    m = re.match('(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})', args[0])
    if re.match('\d{14}', args[0]):
        # build id
        build_time = datetime.datetime.strptime(args[0], '%Y%m%d%H%M%S')
        time_range = (build_time, build_time)
    else:
        time_range = [from_iso_date_or_datetime(args[0])]
        if len(args) > 1:
            time_range.append(from_iso_date_or_datetime(args[1]))
        else:
            time_range.append(datetime.datetime.now())
        if time_range[0] > time_range[1]:
            time_range = (time_range[1], time_range[0])
    commands = build_commands(time_range)
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect((options.ip, options.port))
    print s.recv(1024).strip()
    for c in commands:
        s.sendall(c + '\n')
        print s.recv(1024).strip()
    s.sendall('exit\n')
    print s.recv(1024).strip()
    return 0

            
if __name__ == '__main__':
    import errno
    import sys
    from optparse import OptionParser
    usage = '''%prog [options] <datetime, date/datetime, or date/datetime range>
Triggers one or more test runs.

The argument(s) should be one of the following:
- a build ID, e.g. 20120403063158
- a date/datetime, e.g. 2012-04-03 or 2012-04-03T06:31:58
- a date/datetime range, e.g. 2012-04-03T06:31:58 2012-04-05

If a build ID is given, a test run is initiated for that, and only that,
particular build.

If a single date or datetime is given, test runs are initiated for all builds
with build IDs between the given date/datetime and now.

If a date/datetime range is given, test runs are initiated for all builds
with build IDs in the given range.'''
    parser = OptionParser(usage=usage)
    parser.add_option('-i', '--ip', action='store', type='string', dest='ip',
                      default='127.0.0.1',
                      help='IP address of autophone controller; defaults to localhost')
    parser.add_option('-p', '--port', action='store', type='int', dest='port',
                      default=28001,
                      help='port of autophone controller; defaults to 28001')
    (options, args) = parser.parse_args()
    if len(args) > 2:
        parser.print_help()
        sys.exit(errno.EINVAL)
    sys.exit(main(args, options))
