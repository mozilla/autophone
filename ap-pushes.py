#!/usr/bin/env python
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import argparse
import datetime
import logging

import utils

logging.basicConfig()

parser = argparse.ArgumentParser(description="Count pushes to a repository on a date.")

parser.add_argument("--date",
                    dest="date",
                    default=None,
                    help="start date CCYY-MM-DD. (default: today's date).")
parser.add_argument("--repo",
                    dest="repo",
                    default="mozilla-central",
                    help="repository. (default: mozilla-central)")

args = parser.parse_args()

start_date = args.date
if not args.date:
    start_date = datetime.datetime.strftime(datetime.datetime.now(), '%Y-%m-%d')

pushes_base_url = 'https://hg.mozilla.org/'

if args.repo in 'mozilla-beta,mozilla-aurora,mozilla-release':
    pushes_base_url += 'releases/'
elif args.repo not in 'mozilla-central,try':
    pushes_base_url += 'integration/'

pushes_base_url += args.repo + '/json-pushes?startdate=%s&enddate=%s'

start = datetime.datetime.strptime(start_date, '%Y-%m-%d')
end = start + datetime.timedelta(days=1)
end_date = datetime.datetime.strftime(end, '%Y-%m-%d')

pushes_url = pushes_base_url % (start_date, end_date)
pushes_json = utils.get_remote_json(pushes_url)
if pushes_json:
    keys = pushes_json.keys()
    keys.sort()
    print int(keys[-1]) - int(keys[0]) + 1





