# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import datetime
import os
import sqlite3


class Jobs(object):

    MAX_ATTEMPTS = 3

    def __init__(self, default_device=None):
        self.default_device = default_device
        self.filename = 'jobs.sqlite'
        if not os.path.exists(self.filename):
            conn = self._conn()
            c = conn.cursor()
            c.execute('create table jobs '
                      '(created text, last_attempt text, build_url text, '
                      'attempts int, device text)')
            conn.commit()

    def _conn(self):
        return sqlite3.connect(self.filename)

    def clear_all(self):
        conn = self._conn()
        conn.cursor().execute('delete from jobs')
        conn.commit()

    def new_job(self, build_url, device=None):
        if not device:
            device = self.default_device
        now = datetime.datetime.now().isoformat()
        conn = self._conn()
        conn.cursor().execute('insert into jobs values (?, ?, ?, 0, ?)',
                              (now, None, build_url, device))
        conn.commit()

    def jobs_pending(self, device=None):
        if not device:
            device = self.default_device
        return self._conn().cursor().execute(
            'select count(ROWID) from jobs where device=?',
            (device,)).fetchone()[0]

    def get_next_job(self, device=None):
        if not device:
            device = self.default_device
        conn = self._conn()
        c = conn.cursor()
        c.execute('delete from jobs where device=? and attempts>=?',
                  (device, self.MAX_ATTEMPTS))
        conn.commit()
        jobs = [{'id': job[0],
                 'created': job[1],
                 'last_attempt': job[2],
                 'build_url': job[3],
                 'attempts': job[4]}
                for job in c.execute(
                'select ROWID as id,created,last_attempt,build_url,attempts'
                ' from jobs where device=? order by created', (device,))]
        if not jobs:
            return None
        next_job = jobs[0]
        next_job['attempts'] += 1
        next_job['last_attempt'] = datetime.datetime.now().isoformat()
        c.execute('update jobs set attempts=?, last_attempt=? where ROWID=?',
                  (next_job['attempts'], next_job['last_attempt'],
                   next_job['id']))
        conn.commit()
        return next_job

    def job_completed(self, job_id):
        conn = self._conn()
        c = conn.cursor()
        c.execute('delete from jobs where ROWID=?', (job_id,))
        conn.commit()
