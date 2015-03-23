# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import datetime
import logging
import os
import sqlite3
import time

logger = logging.getLogger('autophone.jobs')

class Jobs(object):

    MAX_ATTEMPTS = 3
    SQL_RETRY_DELAY = 60
    SQL_MAX_RETRIES = 10

    def __init__(self, mailer, default_device=None):
        self.mailer = mailer
        self.default_device = default_device
        self.filename = 'jobs.sqlite'
        if not os.path.exists(self.filename):
            conn = self._conn()
            c = conn.cursor()
            c.execute('create table jobs ('
                      'id integer primary key, '
                      'created text, '
                      'last_attempt text, '
                      'build_url text, '
                      'build_id text, '
                      'changeset text, '
                      'tree text, '
                      'revision text, '
                      'revision_hash, '
                      'enable_unittests int, '
                      'attempts int, '
                      'device text)')
            c.execute('create table tests ('
                      'id integer primary key, '
                      'name text, '
                      'guid text, '
                      'jobid integer)')
            conn.commit()

    def report_sql_error(self, attempt, email_sent,
                         email_subject, email_body,
                         exception_message):
        logger.exception(exception_message)
        if attempt > self.SQL_MAX_RETRIES and not email_sent:
            email_sent = True
            self.mailer.send(email_subject, email_body)
            logger.info('Sent mail notification about jobs database sql error.')
            time.sleep(self.SQL_RETRY_DELAY)
        return email_sent

    def _conn(self):
        attempt = 0
        email_sent = False
        while True:
            attempt += 1
            try:
                return sqlite3.connect(self.filename)
            except sqlite3.OperationalError:
                email_sent = self.report_sql_error(attempt, email_sent,
                                                   'Unable to connect to jobs '
                                                   'database.',
                                                   'Please check the logs for '
                                                   'full details.',
                                                   'Attempt %d failed to connect '
                                                   'to jobs database. Waiting '
                                                   'for %d seconds.' %
                                                   (attempt,
                                                    self.SQL_RETRY_DELAY))

    def clear_all(self):
        attempt = 0
        email_sent = False
        while True:
            attempt += 1
            try:
                conn = self._conn()
                conn.cursor().execute('delete from tests')
                conn.commit()
                break
            except sqlite3.OperationalError:
                email_sent = self.report_sql_error(attempt, email_sent,
                                                   'Unable to clear all tests in '
                                                   'jobs database.',
                                                   'Please check the logs for '
                                                   'full details.',
                                                   'Attempt %d failed to delete '
                                                   'all jobs database. Waiting '
                                                   'for %d seconds.' %
                                                   (attempt,
                                                    self.SQL_RETRY_DELAY))
        attempt = 0
        email_sent = False
        while True:
            attempt += 1
            try:
                conn = self._conn()
                conn.cursor().execute('delete from jobs')
                conn.commit()
                break
            except sqlite3.OperationalError:
                email_sent = self.report_sql_error(attempt, email_sent,
                                                   'Unable to clear all jobs in '
                                                   'jobs database.',
                                                   'Please check the logs for '
                                                   'full details.',
                                                   'Attempt %d failed to delete '
                                                   'all jobs database. Waiting '
                                                   'for %d seconds.' %
                                                   (attempt,
                                                    self.SQL_RETRY_DELAY))

    def new_job(self, build_url, build_id=None, changeset=None, tree=None,
                revision=None, revision_hash=None, tests=None,
                enable_unittests=False, device=None):
        logger.debug('jobs.new_job: %s %s %s %s %s %s %s %s %s' % (
            build_url, build_id, changeset, tree, revision, revision_hash,
            tests, enable_unittests, device))
        if not device:
            device = self.default_device
        now = datetime.datetime.now().isoformat()
        email_sent = False
        job_id = None
        duplicate = False
        test_names = []
        for test in tests:
            test_names.append(test.name)
            # generate a new guid for the test
            test.generate_guid()
        test_names.sort()

        try:
            conn = self._conn()
            job_cursor = conn.cursor()
            test_cursor = conn.cursor()
            job_cursor.execute(
                'select id from jobs where device=? and build_url=?',
                (device, build_url))
            job = job_cursor.fetchone()
            while job:
                job_id = job[0]
                job_test_names = [
                    test_row[0] for test_row in
                    test_cursor.execute(
                        'select name from tests where jobid=?',
                        (job_id,))]
                job_test_names.sort()
                if test_names == job_test_names:
                    duplicate = True
                    break
                job = job_cursor.fetchone()
        except sqlite3.OperationalError:
            logger.exception('jobs.new_job')
            pass

        if duplicate:
            logger.warning('jobs.new_job: Not adding duplicate job: '
                           'build: %s, device: %s, test_names: %s' % (
                               build_url, device, test_names))
            return

        if not job_id:
            attempt = 0
            while True:
                attempt += 1
                try:
                    conn = self._conn()
                    job_cursor = conn.cursor()
                    job_cursor.execute(
                        'insert into jobs values '
                        '(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)',
                        (None, now, None, build_url, build_id, changeset, tree,
                         revision, revision_hash, enable_unittests, device))
                    conn.commit()
                    job_id = job_cursor.lastrowid
                    break
                except sqlite3.OperationalError:
                    email_sent = self.report_sql_error(
                        attempt, email_sent,
                        'Unable to insert job into jobs database.',
                        'Please check the logs for full details.',
                        'Attempt %d failed to insert job (%s, %s, %s) into '
                        'database. Waiting for %d seconds.' %
                        (attempt, build_url, device, test_names,
                         self.SQL_RETRY_DELAY))

        for test in tests:
            attempt = 0
            email_sent = False
            while True:
                attempt += 1
                try:
                    conn = self._conn()
                    test_cursor = conn.cursor()
                    test_cursor.execute(
                        'insert into tests values (?, ?, ?, ?)',
                        (None, test.name, test.job_guid, job_id))
                    conn.commit()
                    break
                except sqlite3.OperationalError:
                    email_sent = self.report_sql_error(
                        attempt, email_sent,
                        'Unable to insert job into jobs database.',
                        'Please check the logs for full details.',
                        'Attempt %d failed to insert job (%s, %s, %s, %s, %s, %s) into '
                        'database. Waiting for %d seconds.' %
                        (attempt, now, None, build_url, test_names,
                         enable_unittests, device, self.SQL_RETRY_DELAY))

    def jobs_pending(self, device=None):
        if not device:
            device = self.default_device
        try:
            count = self._conn().cursor().execute(
                'select count(id) from jobs where device=?',
                (device,)).fetchone()[0]
        except sqlite3.OperationalError:
            count = 0
        return count

    def get_next_job(self, lifo=False, device=None, worker=None):
        if not device:
            device = self.default_device
        order = 'desc' if lifo else 'asc'

        try:
            conn = self._conn()
            c = conn.cursor()
            job_ids = [
                job[0] for job in
                c.execute('select id from jobs where device=? and attempts>=?',
                          (device, self.MAX_ATTEMPTS))]
            for job_id in job_ids:
                c.execute('delete from tests where jobid=?',
                          (job_id,))
            c.execute('delete from jobs where device=? and attempts>=?',
                      (device, self.MAX_ATTEMPTS))
            conn.commit()
            jobs = [{'id': job[0],
                     'created': job[1],
                     'last_attempt': job[2],
                     'build_url': job[3],
                     'build_id': job[4],
                     'changeset': job[5],
                     'tree': job[6],
                     'revision': job[7],
                     'revision_hash': job[8],
                     'enable_unittests': job[9],
                     'attempts': job[10],
                     'istry': job[11]}
                    for job in c.execute(
                            'select id,created,last_attempt,build_url,'
                            'build_id,changeset,tree,revision,revision_hash,'
                            'enable_unittests,attempts,instr(build_url,"try") as istry '
                            'from jobs where device=? order by istry desc, '
                            'created %s' % order,
                            (device,))]
            if not jobs:
                return None
            next_job = jobs[0]
            next_job['attempts'] += 1
            next_job['last_attempt'] = datetime.datetime.now().isoformat()
            c.execute('update jobs set attempts=?, last_attempt=? where id=?',
                      (next_job['attempts'], next_job['last_attempt'],
                       next_job['id']))
            conn.commit()
            next_job['tests'] = []
            test_rows = [
                {'name': test_row[0], 'guid': test_row[1]}
                 for test_row in
                 c.execute('select name, guid from tests where jobid=?',
                           (next_job['id'],))]
            for test_row in test_rows:
                # Convert the list of test names in the job to a list
                # of test objects and update their guid values.
                for test in worker.tests:
                    if test.name == test_row['name']:
                        test.job_guid = test_row['guid']
                        next_job['tests'].append(test)
        except sqlite3.OperationalError:
            logger.exception('jobs.get_next_job')
            next_job = None
        if next_job:
            logger.debug('jobs.get_next_job: %s' % next_job)
        return next_job

    def cancel_job(self, test_name, test_guid, device=None):
        logger.debug('jobs.cancel_job: %s %s %s' % (test_name, test_guid, device))
        if not device:
            device = self.default_device

        email_sent = False
        attempt = 0
        while True:
            attempt += 1
            logger.debug('jobs.cancel_job: attempt %d to delete tests %s %s' % (attempt, test_name, test_guid))
            try:
                conn = self._conn()
                c = conn.cursor()
                # Collect the job ids corresponding to this test.
                # There should be at most one such job id.
                logger.debug('jobs.cancel_job: getting job_ids')
                job_ids = [test_row[0]
                           for test_row in c.execute(
                                   'select distinct jobid from tests '
                                   'where name=? and guid=?',
                                   (test_name, test_guid))]
                logger.debug('jobs.cancel_job: job_ids %s' % job_ids)
                logger.debug('jobs.cancel_job: deleting test_name: %s, guid: %s' % (test_name, test_guid))
                c.execute('delete from tests where '
                          'name=? and guid=?',
                          (test_name, test_guid))
                conn.commit()
                break
            except sqlite3.OperationalError:
                email_sent = self.report_sql_error(
                    attempt, email_sent,
                    'Unable to cancel job tests from jobs database.',
                    'Please check the logs for full details.',
                    'Attempt %d failed to delete test_name %s, '
                    'test_guid %s from database. Waiting for %d '
                    'seconds.' %
                    (attempt, test_name, test_guid, self.SQL_RETRY_DELAY))
        email_sent = False
        attempt = 0
        while True:
            attempt += 1
            logger.debug('jobs.cancel_job: attempt %d to delete jobs %s' % (attempt, job_ids))
            try:
                conn = self._conn()
                c = conn.cursor()
                for job_id in job_ids:
                    logger.debug('jobs.cancel_job: delete job_id %s' % job_id)
                    c.execute('delete from jobs where id=?',
                              (job_id,))
                conn.commit()
                break
            except sqlite3.OperationalError:
                email_sent = self.report_sql_error(
                    attempt, email_sent,
                    'Unable to cancel job from jobs database.',
                    'Please check the logs for full details.',
                    'Attempt %d failed to delete test_name %s, '
                    'test_guid %s from database. Waiting for %d '
                    'seconds.' %
                    (attempt, test_name, test_guid, self.SQL_RETRY_DELAY))
        logger.debug('jobs.cancel_job: exit')

    def test_completed(self, test_guid):
        logger.debug('jobs.test_completed: %s' % test_guid)
        attempt = 0
        email_sent = False
        while True:
            attempt += 1
            try:
                conn = self._conn()
                c = conn.cursor()
                c.execute('delete from tests where guid=?', (test_guid,))
                conn.commit()
                break
            except sqlite3.OperationalError:
                email_sent = self.report_sql_error(
                    attempt, email_sent,
                    'Unable to delete completed test from jobs database.',
                    'Please check the logs for full details.',
                    'Attempt %d failed to delete completed test %s from '
                    'database. Waiting for %d seconds.' %
                    (attempt, test_guid, self.SQL_RETRY_DELAY))

    def job_completed(self, job_id):
        logger.debug('jobs.job_completed: %s' % job_id)
        attempt = 0
        email_sent = False
        while True:
            attempt += 1
            try:
                conn = self._conn()
                c = conn.cursor()
                c.execute('delete from tests where jobid=?', (job_id,))
                conn.commit()
                break
            except sqlite3.OperationalError:
                email_sent = self.report_sql_error(
                    attempt, email_sent,
                    'Unable to delete completed job tests from jobs database.',
                    'Please check the logs for full details.',
                    'Attempt %d failed to delete completed job %s from '
                    'database. Waiting for %d seconds.' %
                    (attempt, job_id, self.SQL_RETRY_DELAY))
        attempt = 0
        email_sent = False
        while True:
            attempt += 1
            try:
                conn = self._conn()
                c = conn.cursor()
                c.execute('delete from jobs where id=?', (job_id,))
                conn.commit()
                break
            except sqlite3.OperationalError:
                email_sent = self.report_sql_error(
                    attempt, email_sent,
                    'Unable to delete completed job from jobs database.',
                    'Please check the logs for full details.',
                    'Attempt %d failed to delete completed job %s from '
                    'database. Waiting for %d seconds.' %
                    (attempt, job_id, self.SQL_RETRY_DELAY))
