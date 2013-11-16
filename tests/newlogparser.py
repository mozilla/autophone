# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import datetime
import json
import logging
import os
import re
import sys
import uuid
from optparse import OptionParser


class TestLog(object):
    '''Manage test messages and results discovered in a log.
    '''

    active_test_log = None

    def __init__(self, log_filehandle=sys.stdin):
        # let's create a unique id for everything so we can track
        # it if we want.
        self.id = uuid.uuid1().hex
        self.log_filehandle = log_filehandle
        self.line_no = 0
        self.test_runs = []
        self.push_back = []

        # check for test output failing to terminate lines
        self.framework_re = re.compile(r'(INFO|WARNING|ERROR|FATAL|TEST-PASS|TEST-UNEXPECTED-FAIL|TEST-INFO|PROCESS-CRASH|REFTEST INFO) ')
        # use a negative look behind assertion to prevent splitting robocop bogus junit assertion messages
        # see http://docs.python.org/2/library/re and
        # https://bugzilla.mozilla.org/show_bug.cgi?id=808696
        self.mochitest_re = re.compile(r'(?<!Exception caught - junit.framework.AssertionFailedError: )\d+ (INFO|ERROR) ')
        self.reftest_re = re.compile(r'REFTEST ')
        # Do not attempt to handle unbuffered output for logcat messages.
        self.logcat_re = re.compile(r'\d{1,2}-\d{1,2} \d{2}:\d{2}:\d{2}\.\d{3} \w/\w+ *[(]\s*\d+[)]:')
        self.junit_re = re.compile(r'junit.framework.AssertionFailedError')
        self.traceback_re = re.compile(r'Traceback [(]most recent call last[)]:')
        self.logger = logging.getLogger('logparser')

        TestLog.active_test_log = self

    def __repr__(self):
        o = {'id': self.id,
             'log_filehandle': self.log_filehandle.name,
             'line_no': self.line_no,
             'push_back': self.push_back,
             'test_runs': self.test_runs}
        return o.__repr__()

    def next_line(self):
        '''Return the next log line from stdin while stripping any
        mozharness prefix. If mozharness indicates an error, record the
        error in the test run's results.

        mozharness tests have the format:
        HH:MM:SS REASON - MESSAGE
        where the HH:MM:SS prefix is the time, REASON is one of DEBUG, INFO,
        WARNING, ERROR, CRITICAL, FATAL and MESSAGE is the originally
        reported message.

        mozharness reported errors will be recorded but will not affect
        subsequent parsing of the test log.
        '''

        def handle_unbuffered_output(regex, line, push_back):
            self.logger.debug('handle_unbuffered_output: pattern %s' % regex.pattern)
            # check for test output failing to terminate lines
            found_match = False
            new_line = line
            match1 = regex.search(line)
            if match1:
                start1 = match1.start(0)
                if start1 == 0:
                    # matched at beginning of line
                    self.logger.debug('next_line: %d, match1:BOL %r' % (self.line_no, match1.group(0)))
                    match2 = regex.search(line, len(match1.group(0)))
                    if match2:
                        # also matched interior of line
                        # return prefix as line and push
                        # back the remainder
                        self.logger.debug('next_line: %d, match2: %r' % (self.line_no, match2.group(0)))
                        start2 = match2.start(0)
                        new_line = line[:start2]
                        push_back.append(line[start2:])
                        self.logger.debug('next_line: %d, PUSHBACK: %r' % (self.line_no, push_back[-1]))

                else:
                    # matched interior of line
                    # return prefix as line and push
                    # back the remainder
                    self.logger.debug('next_line: %d, match1:IOL %r' % (self.line_no, match1.group(0)))
                    new_line = line[:start1]
                    push_back.append(line[start1:])
                    self.logger.debug('next_line: %d, PUSHBACK: %r' % (self.line_no, push_back[-1]))

            found_match = (match1 is not None)
            self.logger.debug('next_line: %d, handle_unbuffered_output: results: %s, %s' % (self.line_no, found_match, new_line))

            return found_match, new_line

        error_reasons = 'ERROR,CRITICAL,FATAL'
        if len(self.push_back) > 0:
            line = self.push_back.pop()
            self.logger.debug('next_line: %d, PUSHBACK LINE: %r' % (self.line_no, line))
        else:
            # XXX: Change autolog's _convert_dict_keys_from_unicode's  newobj[key] = str(value)
            # to deal with encoding errors?
            line = unicode(self.log_filehandle.readline(), 'ascii', errors='ignore')
            self.line_no += 1
            self.logger.debug('next_line: %d, READ LINE: %r' % (self.line_no, line))

        if line:
            # check for mozharness prefix and remove it
            match = re.match(r'\d+:\d+:\d+\s+([^\s]+)\s-\s\s(.*)', line)
            if match:
                reason = match.group(1)
                line = match.group(2)
                if reason in error_reasons:
                    test_message = TestMessage('mozharness')
                    test_result = TestResult(reason, reason, line, True)
                    test_message.addTestResult(test_result)
                    self.test_runs[-1].addTestMessage(test_message)
                    self.test_runs[-1].failed += 1

            if self.logcat_re.match(line):
                self.logger.debug('next_line: %d, handle_unbuffered_output: ignoring logcat line' % self.line_no)
            elif self.junit_re.match(line):
                self.logger.debug('next_line: %d, handle_unbuffered_output: ignoring bare junit assertion' % self.line_no)
            else:
                # check for test output failing to terminate lines
                # Only check once per call to next_line().
                # Check for embedded Traceback messages first so that we don't lose them
                found_pushback, line = handle_unbuffered_output(self.traceback_re, line, self.push_back)
                if not found_pushback:
                    found_pushback, line = handle_unbuffered_output(self.mochitest_re, line, self.push_back)

                    if not found_pushback:
                        found_pushback, line = handle_unbuffered_output(self.reftest_re, line, self.push_back)

                        if not found_pushback:
                            found_pushback, line = handle_unbuffered_output(self.framework_re, line, self.push_back)

        self.logger.debug("next_line: %d, LINE: %r" % (self.line_no, line.rstrip()))
        return line

    def addTestRun(self, test_run):
        if test_run is None:
            raise ValueError('test run is null')

        if not isinstance(test_run, TestRun):
            raise ValueError('Not a TestRun object')

        if len(self.test_runs) > 0:
            self.test_runs[-1].end_line_no = self.line_no

        test_run.start_line_no = self.line_no
        test_run.filename = self.log_filehandle.name
        test_run.test_log = self

        self.test_runs.append(test_run)

    def delete_passing_tests(self):
        for test_run in self.test_runs:
            test_run.delete_passing_tests()

    def convert(self, include_passing_tests=False):
        converted_test_runs = []

        for test_run in self.test_runs:
            converted_test_runs.append(test_run.convert(include_passing_tests))

        return converted_test_runs


class TestRun(object):

#    mochitest
#
#       FRAMEWORK-START
#              v
#       FRAMEWORK-RUNNING
#              v
#       TESTSUITE-START
#              v
#  |---->TESTCASE-START
#  |           v
#  |     TESTCASE-RUNNING -> TESTCASE-IMG1->
#  |           |     ^                     v
#  |           |     -----------------------
#  |           v
#  <-----TESTCASE-END
#              v
#        TESTSUITE-SHUTDOWN
#              v
#        FRAMEWORK-END
#
#    reftest
#
#        FRAMEWORK-START
#              v
#        FRAMEWORK-RUNNING
#              v
#        TESTCASE-RUNNING -> TESTCASE-IMG1->
#              |     ^                     v
#              |     -----------------------
#              v
#        TESTSUITE-SHUTDOWN
#              v
#        FRAMEWORK-END

    valid_states = [
        'FRAMEWORK-START',
        'FRAMEWORK-RUNNING',
        'TESTSUITE-START',
        'TESTCASE-START',
        'TESTCASE-IMG1',
        'TESTCASE-RUNNING',
        'TESTCASE-END',
        'TESTSUITE-SHUTDOWN',
        'FRAMEWORK-END',
        'FRAMEWORK-SUMMARY',
        'FRAMEWORK-SUMMARY-START',
        'FRAMEWORK-SUMMARY-SHUTDOWN',
    ]

    def __init__(self, state='FRAMEWORK-START'):
        # instead of hashing the test run after it is collected which
        # is expensive and is only available after the test run has
        # been completely collected, just use a unique uuid which is
        # quicker and only takes 32 bytes instead of 40.

        self.id = uuid.uuid1().hex
        self._state = state
        self.filename = None
        self.suitename = None
        self._suitetype = None
        self.passed = 0
        self.failed = 0
        self.todo = 0
        self.parsedcounts = {'passed': 0, 'failed': 0, 'todo': 0}
        self.elapsedtime = None
        self.test_messages = []
        self.logger = logging.getLogger('logparser')
        self.test_log = None
        self.start_line_no = 0
        self.end_line_no = 0

        self.logger.info("TestRun: %s" % self)

    @property
    def state(self):
        return self._state

    @state.setter
    def state(self, value):
        if self.logger.getEffectiveLevel() == logging.DEBUG:
            if self._state != value:
                self.logger.debug("STATE TRANSITION: %s to %s" % (self._state, value))

        if TestRun.valid_states.index(value) == -1:
            raise Exception('Invalid Framework state %s' % value)

        self._state = value

    @property
    def suitetype(self):
        return self._suitetype

    @suitetype.setter
    def suitetype(self, value):
        self._suitetype = value
        if self.suitename is None:
            self.suitename = self._suitetype

    def __repr__(self):
        o = {'id': self.id,
             'state': self.state,
             'suitename': self.suitename,
             'suitetype': self.suitetype,
             'passed': self.passed,
             'failed': self.failed,
             'todo': self.todo,
             'parsedcounts': self.parsedcounts,
             'elapsedtime': self.elapsedtime,
             'start_line_no': self.start_line_no,
             'end_line_no': self.end_line_no,
             'test_messages': self.test_messages}
        return o.__repr__()

    def addTestMessage(self, test_message):
        if test_message is None:
            raise ValueError('test message is null')

        if not isinstance(test_message, TestMessage):
            raise ValueError('Not a TestMessage object')

        if len(self.test_messages) > 0:
            self.test_messages[-1].end_line_no = TestLog.active_test_log.line_no

        test_message.start_line_no = TestLog.active_test_log.line_no
        test_message.test_run = self

        self.test_messages.append(test_message)

    def delete_passing_tests(self):
        # 'delete' the passing tests by copying test messages
        # to a new list excluding those whose results all
        # have testerror False.

        new_messages = []

        for message in self.test_messages:
            message_passed = True

            for result in message.results:
                message_passed = message_passed and not result.testerror
                if not message_passed:
                    break

            if not message_passed:
                new_messages.append(message)

        self.test_messages = new_messages

    def convert(self, include_passing_tests=False):
        converted_test_run = {'id': self.id, 'filename': self.filename}

        if self.suitename:
            converted_test_run['suitename'] = self.suitename

        if self.elapsedtime is not None:
            converted_test_run['elapsedtime'] = self.elapsedtime

        converted_test_run['passed'] = self.passed
        converted_test_run['failed'] = self.failed
        converted_test_run['todo'] = self.todo

        if include_passing_tests:
            converted_test_run['passes'] = []

        converted_test_run['failures'] = []

        passed = 0
        failed = 0
        todo = 0

        # Sort the test messages by path to consolidate their results.
        # This is necessary since messages for a given testpath may be
        # interspersed with framework or other messages with different paths.
        # Autolog will only display messages for the first occurrence of a
        # testpath.
        test_messages = sorted(self.test_messages, key=lambda test_message: test_message.testpath)
        converted_message = None

        for message in test_messages:

            testpath = message.testpath
            results = message.results

            self.logger.debug('MESSAGE: testpath: %s, #results: %s' % (testpath, len(results)))

            if converted_message is None:
                converted_message = {'test': testpath, 'failures': []}
            elif converted_message['test'] == testpath:
                # just add the results to the existing converted_message.
                pass
            else:
                if len(converted_message['failures']) > 0:
                    converted_test_run['failures'].append(converted_message)
                elif include_passing_tests:
                    converted_test_run['passes'].append({'test': converted_message['test']})

                converted_message = {'test': testpath, 'failures': []}

            for result in results:
                text = result.text
                reason = result.reason
                stacktrace = result.stacktrace

                self.logger.debug('MESSAGE: result: error: %s, text: %s, reason: %s, stacktrace: %s' %
                                  (result.testerror, text, reason, stacktrace))
                if result.testerror:
                    failed += 1
                    converted_result = {}
                    converted_result['text'] = text
                    converted_result['status'] = reason
                    if result.stacktrace:
                        converted_result['stacktrace'] = stacktrace
                    if result.image1:
                        converted_result['image1'] = result.image1
                    if result.image2:
                        converted_result['image2'] = result.image2

                    converted_message['failures'].append(converted_result)

                else:
                    if 'KNOWN' in result.reason or 'RANDOM' in result.reason:
                        todo += 1
                    else:
                        passed += 1

        if len(converted_message['failures']) > 0:
            converted_test_run['failures'].append(converted_message)
        elif include_passing_tests:
            converted_test_run['passes'].append({'test': converted_message['test']})

        converted_test_run['testfailure_count'] = len(converted_test_run['failures'])

        if include_passing_tests and self.passed != passed:
            self.logger.error('COUNT ERROR passed: detected: %d, counted during conversion: %d, test run id: %s' % (self.passed, passed, converted_test_run['id']))

        if self.passed != self.parsedcounts['passed']:
            self.logger.warning('Passed counts differ from Passed: value in log: detected: %d, parsed from log: %d, test run id: %s' % (self.passed, self.parsedcounts['passed'], converted_test_run['id']))

        if self.failed != failed:
            self.logger.error('COUNT ERROR failed: detected: %d, counted during conversion: %d, test run id: %s' % (self.failed, failed, converted_test_run['id']))

        if self.failed != self.parsedcounts['failed']:
            self.logger.warning('Failed counts differ from Failed: value in log: detected: %d, parsed from log: %d, test run id: %s' % (self.failed, self.parsedcounts['failed'], converted_test_run['id']))

        if include_passing_tests and self.todo != todo:
            self.logger.error('COUNT ERROR todo: detected: %d, counted during conversion: %d, test run id: %s' % (self.todo, todo, converted_test_run['id']))

        if self.todo != self.parsedcounts['todo']:
            self.logger.warning('Todo counts differ from Todo: value in log: detected: %d, parsed from log: %d, test run id: %s' % (self.todo, self.parsedcounts['todo'], converted_test_run['id']))

        return converted_test_run


class TestMessage(object):

    def __init__(self, testpath):
        self.id = uuid.uuid1().hex
        self.testpath = testpath.rstrip().replace('\\', '/')
        self.results = []
        self.logger = logging.getLogger('logparser')
        self.start_line_no = 0
        self.end_line_no = 0
        self.test_run = None

        self.logger.info("TestMessage: %s" % self)

    def __repr__(self):
        o = {'id': self.id,
             'testpath': self.testpath,
             'start_line_no': self.start_line_no,
             'end_line_no': self.end_line_no,
             'results': self.results}
        return o.__repr__()

    def addTestResult(self, test_result):
        if test_result is None:
            raise ValueError('test result is null')

        if not isinstance(test_result, TestResult):
            raise ValueError('Not a TestResult object')

        test_result.line_no = TestLog.active_test_log.line_no
        test_result.test_message = self

        self.results.append(test_result)


class TestResult(object):

    def __init__(self, level, reason, text, testerror, duration=None):
        self.id = uuid.uuid1().hex
        self.level = level.rstrip()
        self.reason = reason.rstrip()
        self.text = text.rstrip()
        self.testerror = testerror
        self.duration = duration
        self.image1 = None
        self.image2 = None
        self.stacktrace = None
        self.logger = logging.getLogger('logparser')
        self.line_no = 0

        self.logger.info("TestResult: %s" % self)

    def __repr__(self):
        o = {'id': self.id,
             'level': self.level,
             'reason': self.reason,
             'text': self.text,
             'testerror': self.testerror,
             'duration': self.duration,
             'image1': self.image1,
             'image2': self.image2,
             'line_no': self.line_no,
             'stacktrace': self.stacktrace}
        return o.__repr__()


def parse_log(fh):
    found_crash = False

    xpcshell_re = re.compile(r'(TEST-[^ ]*) \| ([^ ]*) \| (running test \.\.\.|test passed [(]time: \d+\.\d+ms[)]|test failed [(]with xpcshell return code: -?\d+[)], see following log:)')

    logger = logging.getLogger('logparser')
    test_log = TestLog(fh)

    # prev_test_run is currently assigned to but not used.
    # We could technically remove it but I would prefer to
    # keep it at least for debugging purposes. /bc
    prev_test_run = None
    test_run = TestRun()
    test_log.addTestRun(test_run)

    prev_xpcshell_message = None
    xpcshell_message = None

    prev_framework_message = None
    framework_message = None

    prev_mochitest_message = None
    mochitest_message = None

    prev_browserchrome_message = None
    browserchrome_message = None

    prev_reftest_message = None
    reftest_message = None

    line = test_log.next_line()

    while line:

        logger.debug('line_no: %d, state: %s, passed: %s, failed: %s, todo: %s, test_messages: %s' %
                     (test_log.line_no, test_run.state, test_run.passed, test_run.failed, test_run.todo, len(test_run.test_messages)))

        match = re.match(r'Traceback \(most recent call last\):', line)
        if match:
            # Found a python exception stack. Collect up to and including
            # line which does not contain whitespace at the beginning of
            # the line.
            logger.debug('Begin Traceback')
            python_stack = line
            line = test_log.next_line()
            while line:
                python_stack += line
                if line[0:1] != ' ':
                    # This line contains the exception message
                    break

                line = test_log.next_line()

            logger.debug("python exception: [%s]" % python_stack)

            framework_message = TestMessage('framework')
            test_run.addTestMessage(framework_message)

            test_result = TestResult('ERROR', 'ERROR', python_stack, True)
            framework_message.addTestResult(test_result)

            test_run.failed += 1
            logger.debug('End Traceback')
            line = test_log.next_line()
            continue

        # remove trailing spaces, newlines and carriage returns
        line = line.rstrip()

        if found_crash and line.startswith('Crash dump filename'):
            line = test_log.next_line()
            if line.startswith('Operating system:'):
                # Definitely have a crash dump
                logger.debug('Begin collecting crash stack')
                stacktrace = ''
                crash_stack_start_re = re.compile(r"Thread (\d+) \(crashed\)")
                match_crash_stack_start = None
                line = test_log.next_line()
                while line:
                    # Skip to crashing thread
                    match_crash_stack_start = crash_stack_start_re.match(line)
                    if match_crash_stack_start:
                        break
                    line = test_log.next_line()
                if match_crash_stack_start:
                    stacktrace += line
                    line = test_log.next_line()
                    while line:
                        if line.rstrip() == '':
                            # crashing thread's stack terminates with a blank line
                            break

                        stacktrace += line
                        line = test_log.next_line()
                # add stack to the most recent message.
                # should be a PROCESS-CRASH framework message
                test_run.test_messages[-1].results[-1].stacktrace = stacktrace
                logger.debug('Finished collecting crash stack')
                continue

            found_crash = False

        if line.startswith('Robocop derived process name'):
            test_run.suitename = 'robocop'

        xpcshell_match = xpcshell_re.match(line)

        if xpcshell_match:

            if test_run.suitename != 'xpcshell':
                test_run.suitename = 'xpcshell'

            if logger.getEffectiveLevel() == logging.DEBUG:
                logger.debug('XPCSHELL')
                if prev_xpcshell_message != xpcshell_message:
                    logger.debug('PREVIOUS MESSAGE: %s' % prev_xpcshell_message)
                    logger.debug('CURRENT  MESSAGE: %s' % xpcshell_message)

            prev_xpcshell_message = xpcshell_message

            reason = xpcshell_match.group(1)
            testpath = xpcshell_match.group(2).replace('\\', '/')
            text = xpcshell_match.group(3)
            level = 'INFO'

            logger.debug('state: %s, reason: %s, text: %s' %
                         (test_run.state, reason, text))

            if reason == 'TEST-INFO' and text == 'running test ...':

                valid_states = 'FRAMEWORK-START,TESTCASE-END'

                if test_run.state not in valid_states:
                    message_text = 'line: %d: MALFORMED TEST LOG: %s %s: expected state %s, found state %s, test run id: %s' % (test_log.line_no, reason, text, valid_states, test_run.state, test_run.id)
                    logger.warning(message_text)
                    error_message = TestMessage(testpath)
                    error_result = TestResult('ERROR', 'ERROR',
                                              message_text, True)
                    error_message.addTestResult(error_result)
                    test_run.addTestMessage(error_message)
                    test_run.failed += 1

                xpcshell_message = TestMessage(testpath)
                test_run.addTestMessage(xpcshell_message)
                test_run.state = 'TESTCASE-RUNNING'

            elif reason == 'TEST-INFO' and text.startswith('skipping'):

                testpath = text.split('skipping')[1]

                valid_states = 'FRAMEWORK-RUNNING,TESTCASE-RUNNING'

                if test_run.state not in valid_states:
                    message_text = 'line: %d: MALFORMED TEST LOG: %s %s: expected state %s, found state %s, test run id: %s' % (test_log.line_no, reason, text, valid_states, test_run.state, test_run.id)
                    logger.warning(message_text)
                    error_message = TestMessage(testpath)
                    error_result = TestResult('ERROR', 'ERROR',
                                              message_text, True)
                    error_message.addTestResult(error_result)
                    test_run.addTestMessage(error_message)
                    test_run.failed += 1

                test_run.todo += 1
                test_run.state = 'TESTCASE-END'

            elif reason.startswith(('TEST-PASS',
                                    'TEST-UNEXPECTED-PASS',
                                    'TEST-KNOWN-FAIL',
                                    'TEST-UNEXPECTED-FAIL')):

                logger.debug('TEST RESULT FOUND: %s, %s, %s' % (reason, testpath, text))

                valid_states = 'TESTCASE-RUNNING'

                if test_run.state not in valid_states:
                    message_text = 'line: %d: MALFORMED TEST LOG: %s %s: expected state %s, found state %s, test run id: %s' % (test_log.line_no, reason, text, valid_states, test_run.state, test_run.id)
                    logger.warning(message_text)
                    xpcshell_message = TestMessage(testpath)
                    error_result = TestResult('ERROR', 'ERROR',
                                              message_text, True)
                    xpcshell_message.addTestResult(error_result)
                    test_run.failed += 1
                    test_run.addTestMessage(xpcshell_message)
                    test_run.state = 'TESTCASE-RUNNING'

                elif prev_xpcshell_message is not None and testpath != prev_xpcshell_message.testpath:
                    # mismatched testpath
                    message_text = 'line: %d: MALFORMED TEST RESULTS: %s %s: expected testpath %s, found testpath %s, test run id: %s' % (test_log.line_no, reason, text, prev_xpcshell_message.testpath, testpath, test_run.id)
                    logger.warning(message_text)
                    xpcshell_message = TestMessage(prev_xpcshell_message.testpath)
                    error_result = TestResult('ERROR', 'ERROR',
                                              message_text, True)
                    xpcshell_message.addTestResult(error_result)
                    test_run.failed += 1
                    xpcshell_message = TestMessage(testpath)
                    test_run.addTestMessage(xpcshell_message)
                    test_run.state = 'TESTCASE-RUNNING'

                testerror = 'UNEXPECTED' in reason

                if 'KNOWN' in reason or 'RANDOM' in reason:
                    test_run.todo += 1
                elif testerror:
                    test_run.failed += 1
                else:
                    test_run.passed += 1

                time_match = re.search(r'test passed [(]time: (\d+\.\d+)ms[)]', text)
                if time_match:
                    duration = int(float(time_match.group(1)))
                else:
                    duration = None

                xpcshell_result = TestResult(level, reason, text, testerror, duration)
                xpcshell_message.addTestResult(xpcshell_result)

                test_run.state = 'TESTCASE-END'

        elif test_run.suitename == 'xpcshell' and line == 'INFO | Result summary:':

            valid_states = 'TESTCASE-END'
            if test_run.state not in valid_states:
                # Missing shutdown
                message_text = 'line: %d: MALFORMED TEST LOG - missing test run shutdown: %s expected state %s, found state %s, test run id: %s' % (test_log.line_no, line, valid_states, test_run.state, test_run.id)
                logger.warning(message_text)
                error_message = TestMessage('unknown')
                error_result = TestResult('ERROR', 'ERROR',
                                          message_text, True)
                error_message.addTestResult(error_result)
                test_run.addTestMessage(error_message)
                test_run.failed += 1
                test_run.state = 'TESTSUITE-SHUTDOWN'

            test_run.state = 'TESTSUITE-SHUTDOWN'

        elif test_run.suitename == 'xpcshell' and re.match(r'INFO \| (Passed|Failed|Todo): \d+', line):
            logger.debug('COUNTER')
            valid_states = 'TESTSUITE-SHUTDOWN'
            if test_run.state not in valid_states:
                # Missing shutdown
                message_text = 'line: %d: MALFORMED TEST LOG - missing test run shutdown: %s: expected state %s, found state %s, test run id: %s' % (test_log.line_no, line, valid_states, test_run.state, test_run.id)
                logger.warning(message_text)
                error_message = TestMessage('unknown')
                error_result = TestResult('ERROR', 'ERROR',
                                          message_text, True)
                error_message.addTestResult(error_result)
                test_run.addTestMessage(error_message)
                test_run.failed += 1
                test_run.state = 'TESTSUITE-SHUTDOWN'

            counter_match = re.match(r'INFO \| (Passed|Failed|Todo): (\d+)', line)
            counter_type = counter_match.group(1)
            counter_value = int(counter_match.group(2))
            if counter_type == 'Passed':
                test_run.parsedcounts['passed'] = counter_value
            elif counter_type == 'Failed':
                test_run.parsedcounts['failed'] = counter_value
            elif counter_type == 'Todo':
                test_run.parsedcounts['todo'] = counter_value
            else:
                pass

        elif line == '*** Start BrowserChrome Test Results ***':
            test_run.suitename = 'BrowserChrome'
            test_run.suitetype = 'BrowserChrome'
            valid_states = 'TESTSUITE-SHUTDOWN,FRAMEWORK-RUNNING'
            if test_run.state not in valid_states:
                message_text = 'line: %d: MALFORMED TEST LOG - previous test run not properly shutdown: %s: expected state %s, found state %s, test run id: %s' % (test_log.line_no, line, valid_states, test_run.state, test_run.id)
                logger.warning(message_text)
                error_message = TestMessage('unknown')
                error_result = TestResult('ERROR', 'ERROR',
                                          message_text, True)
                error_message.addTestResult(error_result)
                test_run.addTestMessage(error_message)
                test_run.failed += 1

            test_run.state = 'TESTSUITE-START'

        elif line == '*** End BrowserChrome Test Results ***':
            valid_states = 'TESTSUITE-SHUTDOWN'

            if test_run.suitename != 'BrowserChrome':
                message_text = 'line: %d: MALFORMED TEST LOG - previous test run not properly shutdown: %s: expected test run BrowserChrome, found test run %s, test run id: %s' % (test_log.line_no, line, test_run.suitetype, test_run.id)
                logger.warning(message_text)
                error_message = TestMessage('unknown')
                error_result = TestResult('ERROR', 'ERROR',
                                          message_text, True)
                error_message.addTestResult(error_result)
                test_run.addTestMessage(error_message)
                test_run.failed += 1
                test_run.state = 'TESTSUITE-SHUTDOWN'

            if test_run.state not in valid_states:
                # Missing shutdown
                message_text = 'line: %d: MALFORMED TEST LOG - previous test run not properly shutdown: %s: expected state %s, found state %s, test run id: %s' % (test_log.line_no, line, valid_states, test_run.state, test_run.id)
                logger.warning(message_text)
                error_message = TestMessage('unknown')
                error_result = TestResult('ERROR', 'ERROR',
                                          message_text, True)
                error_message.addTestResult(error_result)
                test_run.addTestMessage(error_message)
                test_run.failed += 1
                test_run.state = 'TESTSUITE-SHUTDOWN'

        elif line == 'Browser Chrome Test Summary':
            valid_states = 'TESTSUITE-SHUTDOWN'

            if test_run.suitename != 'BrowserChrome':
                message_text = 'line: %d: MALFORMED TEST LOG - previous test run not properly shutdown: %s: expected test run BrowserChrome, found test run %s, test run id: %s' % (test_log.line_no, line, test_run.suitetype, test_run.id)
                logger.warning(message_text)
                error_message = TestMessage('unknown')
                error_result = TestResult('ERROR', 'ERROR',
                                          message_text, True)
                error_message.addTestResult(error_result)
                test_run.addTestMessage(error_message)
                test_run.failed += 1
                test_run.state = 'TESTSUITE-SHUTDOWN'

            if test_run.state not in valid_states:
                message_text = 'line: %d: MALFORMED TEST LOG - previous test run not properly shutdown: %s: expected state %s, found state %s, test run id: %s' % (test_log.line_no, line, valid_states, test_run.state, test_run.id)
                logger.warning(message_text)
                error_message = TestMessage('unknown')
                error_result = TestResult('ERROR', 'ERROR',
                                          message_text, True)
                error_message.addTestResult(error_result)
                test_run.addTestMessage(error_message)
                test_run.failed += 1

            test_run.state = 'TESTSUITE-SHUTDOWN'

        elif line.startswith(('\tPassed:', '\tFailed:', '\tTodo:')):
            valid_states = 'TESTSUITE-SHUTDOWN'

            if test_run.suitename != 'BrowserChrome':
                message_text = 'line: %d: MALFORMED TEST LOG - previous test run not properly shutdown: %s: expected test run BrowserChrome, found test run %s, test run id: %s' % (test_log.line_no, line, test_run.suitetype, test_run.id)
                logger.warning(message_text)
                error_message = TestMessage('unknown')
                error_result = TestResult('ERROR', 'ERROR',
                                          message_text, True)
                error_message.addTestResult(error_result)
                test_run.addTestMessage(error_message)
                test_run.failed += 1
                test_run.state = 'TESTSUITE-SHUTDOWN'

            (countertype, countervalue) = line.split(': ')
            countertype = countertype.strip()
            countervalue = int(countervalue.strip())
            if countertype == 'Passed':
                test_run.parsedcounts['passed'] = countervalue
            elif countertype == 'Failed':
                test_run.parsedcounts['failed'] = countervalue
            elif countertype == 'Todo':
                test_run.parsedcounts['todo'] = countervalue

            if test_run.state not in valid_states:
                # Missing shutdown
                message_text = 'line: %d: MALFORMED TEST LOG - previous test run not properly shutdown: %s: expected state %s, found state %s, test run id: %s' % (test_log.line_no, line, valid_states, test_run.state, test_run.id)
                logger.warning(message_text)
                error_message = TestMessage('unknown')
                error_result = TestResult('ERROR', 'ERROR',
                                          message_text, True)
                error_message.addTestResult(error_result)
                test_run.addTestMessage(error_message)
                test_run.failed += 1
                test_run.state = 'TESTSUITE-SHUTDOWN'

        elif test_run.suitetype == 'BrowserChrome':
            # Must do BrowserChrome before Framework testing since
            # they *share* the same prefixes. :-(

            browserchrome_match = re.match(r'(INFO TEST-END|INFO TEST-START|TEST-[^ ]*) (.*)', line)

            if browserchrome_match:

                if logger.getEffectiveLevel() == logging.DEBUG:
                    logger.debug('BROWSERCHROME')
                    if prev_browserchrome_message != browserchrome_message:
                        logger.debug('PREVIOUS MESSAGE: %s' % prev_browserchrome_message)
                        logger.debug('CURRENT  MESSAGE: %s' % browserchrome_message)

                prev_browserchrome_message = browserchrome_message

                level = 'INFO'
                parts = line.split(' | ')

                if parts[0] == 'INFO TEST-END':
                    parts[0] = 'TEST-END'
                elif parts[0] == 'INFO TEST-START':
                    parts[0] = 'TEST-START'
                else:
                    pass

                reason = parts[0]

                if len(parts) > 1:
                    parts[1] = parts[1].rstrip(' |')  # some tests do not have text
                    testpath = parts[1]
                    text = ' | '.join(parts[2:])
                else:
                    testpath = None
                    text = ' | '.join(parts[1:])

                logger.debug('state: %s, level: %s, reason: %s, text: %s' %
                             (test_run.state, level, reason, text))

                if reason == 'TEST-START':

                    valid_states = 'TESTSUITE-START,TESTCASE-END'
                    if testpath.find(' - ') != -1:
                        # robocop testBrowserProvider does a brain dead thing
                        # with test paths by changing the test path by adding
                        # a different suffix separated by a dash (-) to
                        # TEST-START while not ending test cases with a
                        # TEST-END.
                        valid_states += ',TESTCASE-RUNNING'

                    if test_run.state not in valid_states:
                        message_text = 'line: %d: MALFORMED TEST LOG: %s %s: expected state %s, found state %s, test run id: %s' % (test_log.line_no, reason, text, valid_states, test_run.state, test_run.id)
                        logger.warning(message_text)
                        error_message = TestMessage('unknown')
                        error_result = TestResult('ERROR', 'ERROR',
                                                  message_text, True)
                        error_message.addTestResult(error_result)
                        test_run.addTestMessage(error_message)
                        test_run.failed += 1

                    if testpath == 'Shutdown':
                        test_run.state = 'TESTSUITE-SHUTDOWN'
                    else:
                        test_run.state = 'TESTCASE-START'
                        browserchrome_message = TestMessage(testpath)
                        test_run.addTestMessage(browserchrome_message)

                elif reason in 'TEST-PASS,TEST-UNEXPECTED-PASS,TEST-KNOWN-FAIL,TEST-UNEXPECTED-FAIL':
                    valid_states = 'TESTCASE-START,TESTCASE-RUNNING'

                    if test_run.state not in valid_states:
                        message_text = 'line: %d: MALFORMED TEST LOG: %s %s: expected state %s, found state %s, test run id: %s' % (test_log.line_no, reason, text, valid_states, test_run.state, test_run.id)
                        logger.warning(message_text)
                        error_message = TestMessage('unknown')
                        error_result = TestResult('ERROR', 'ERROR',
                                                  message_text, True)
                        error_message.addTestResult(error_result)
                        test_run.addTestMessage(error_message)
                        test_run.failed += 1
                        # fake the missing test start
                        browserchrome_message = TestMessage(testpath)
                        test_run.addTestMessage(browserchrome_message)
                        test_run.state = 'TESTCASE-START'

                    elif prev_browserchrome_message is not None and testpath != prev_browserchrome_message.testpath:
                        # mismatched testpath
                        message_text = 'line: %d: MALFORMED TEST RESULTS: %s %s: expected testpath %s, found testpath %s, test run id: %s' % (test_log.line_no, reason, text, prev_browserchrome_message.testpath, testpath, test_run.id)
                        logger.warning(message_text)
                        error_message = TestMessage(prev_browserchrome_message.testpath)
                        error_result = TestResult('ERROR', 'ERROR',
                                                  message_text, True)
                        error_message.addTestResult(error_result)
                        test_run.addTestMessage(error_message)
                        test_run.failed += 1
                        # fake the missing test start
                        browserchrome_message = TestMessage(testpath)
                        test_run.addTestMessage(browserchrome_message)
                        test_run.state = 'TESTCASE-START'

                    testerror = 'UNEXPECTED' in reason

                    if 'KNOWN' in reason or 'RANDOM' in reason:
                        test_run.todo += 1
                    elif testerror:
                        test_run.failed += 1
                    else:
                        test_run.passed += 1

                    browserchrome_result = TestResult(level, reason, text, testerror)
                    browserchrome_message.addTestResult(browserchrome_result)

                    if test_run.state != 'TESTCASE-RUNNING':
                        test_run.state = 'TESTCASE-RUNNING'

                elif reason == 'TEST-END':
                    valid_states = 'TESTCASE-RUNNING'

                    match = re.match(r'finished in (\d+)ms', text)
                    if not match:
                        duration = None  # error?
                    else:
                        duration = match.group(1)

                    if test_run.state not in valid_states:
                        message_text = 'line: %d: MALFORMED TEST LOG: %s %s: expected state %s, found state %s, test run id: %s' % (test_log.line_no, reason, text, valid_states, test_run.state, test_run.id)
                        logger.warning(message_text)
                        error_message = TestMessage(testpath)
                        error_result = TestResult('ERROR', 'ERROR',
                                                  message_text, True)
                        error_message.addTestResult(error_result)
                        test_run.addTestMessage(error_message)
                        test_run.failed += 1
                        # fake the missing test start
                        browserchrome_message = TestMessage(testpath)
                        test_run.addTestMessage(browserchrome_message)
                        test_run.state = 'TESTCASE-RUNNING'

                    elif testpath != prev_browserchrome_message.testpath:
                        # mismatched testpath
                        message_text = 'line: %d: MALFORMED TEST RESULTS: %s %s: expected testpath %s, found testpath %s, test run id: %s' % (test_log.line_no, reason, text, prev_browserchrome_message.testpath, testpath, test_run.id)
                        logger.warning(message_text)
                        error_message = TestMessage(prev_browserchrome_message.testpath)
                        error_result = TestResult('ERROR', 'ERROR',
                                                  message_text, True)
                        error_message.addTestResult(error_result)
                        test_run.addTestMessage(error_message)
                        test_run.failed += 1
                        # fake the missing test start
                        browserchrome_message = TestMessage(testpath)
                        test_run.addTestMessage(browserchrome_message)
                        test_run.state = 'TESTCASE-RUNNING'

                    #browserchrome_result = TestResult(level, reason, text, False, duration)
                    #browserchrome_message.addTestResult(browserchrome_result)
                    test_run.state = 'TESTCASE-END'

                elif reason == 'TEST-INFO':
                    pass

                else:
                    if not reason.startswith('TEST-'):
                        message_text = 'line: %d: Unknown reason: %s, text: %s, test run id: %s' % (test_log.line_no, reason, text, test_run.id)
                        logger.warning(message_text)

        elif line.startswith(('INFO | ',
                              'WARNING | ',
                              'ERROR | ',
                              'FATAL | ',
                              'TEST-PASS | ',
                              'TEST-UNEXPECTED-FAIL | ',
                              'TEST-INFO | ',
                              'PROCESS-CRASH | ',
                              'REFTEST INFO | runreftest.py',
                              'runtests.py | ')):

            # framework message
            if logger.getEffectiveLevel() == logging.DEBUG:
                logger.debug('FRAMEWORK')
                if prev_framework_message != framework_message:
                    logger.debug('PREVIOUS MESSAGE: %s' % prev_framework_message)
                    logger.debug('CURRENT  MESSAGE: %s' % framework_message)

            prev_framework_message = framework_message

            parts = line.split(' | ')
            # Bug 865349 removed the leading INFO | from the Mochitest Running tests: start.
            # message. Check for the new condition and munge it to fit previous pattern.
            if parts[0] == 'runtests.py':
                reason = 'INFO'
                index = 0
            else:
                reason = parts[0]  # INFO, WARNING, ...
                index = 1
                if parts[1].find(' process ') != -1:
                    parts[-1] = parts[-1] + ' ' + parts[1]
                    index = 2

            testpath = parts[index]
            text = ' | '.join(parts[index + 1:])

            if reason in 'ERROR,FATAL,TEST-UNEXPECTED-FAIL,PROCESS-CRASH':
                level = 'ERROR'
                testerror = True
            else:
                level = 'INFO'
                testerror = False

            logger.debug('reason: %s, testpath: %s, text: %s' %
                         (reason, testpath, text))

            if text == 'Running tests: start.':
                if test_run.state == 'FRAMEWORK-START':
                    # transition: FRAMEWORK-START->start->FRAMEWORK-RUNNING ok.
                    # Use initial default test run to collect messages.
                    test_run.state = 'FRAMEWORK-RUNNING'
                elif test_run.state == 'FRAMEWORK-RUNNING':
                    # transition: FRAMEWORK-RUNNING->start->FRAMEWORK-RUNNING error.
                    # Terminate previous test run and start a new one.
                    if prev_framework_message is None:
                        message_text = 'Should not be possible: transition from FRAMEWORK-RUNNING to FRAMEWORK-RUNNING without previous message, test run id: %s' % test_run.id
                        logger.error(message_text)
                        framework_message = TestMessage('unknown')
                        error_result = TestResult('ERROR', 'ERROR',
                                                  message_text, True)
                        framework_message.addTestResult(error_result)
                        test_run.failed += 1
                        test_run.addTestMessage(framework_message)
                    else:
                        message_text = 'Previous test run not completed, test run id: %s' % test_run.id
                        logger.warning(message_text)
                        error_result = TestResult('ERROR', 'ERROR',
                                                  message_text, True)
                        test_run.failed += 1
                        prev_framework_message.addTestResult(error_result)

                    test_run.state = 'FRAMEWORK-END'
                    prev_test_run = test_run
                    test_run = TestRun('FRAMEWORK-RUNNING')
                    test_log.addTestRun(test_run)

                elif test_run.state in 'TESTSUITE-SHUTDOWN,FRAMEWORK-END':
                    # transition: FRAMEWORK-END->start->FRAMEWORK-RUNNING ok.
                    # Terminate previous test run and start a new one.
                    prev_test_run = test_run
                    test_run = TestRun('FRAMEWORK-RUNNING')
                    test_log.addTestRun(test_run)
                else:
                    # Invalid transition
                    # Terminate previous test run and start a new one.
                    if prev_framework_message is None:
                        message_text = 'line: %d: MALFORMED TEST LOG: terminate test run and start a new one: %s %s: found state %s, test run id: %s' % (test_log.line_no, reason, text, test_run.state, test_run.id)
                        logger.warning(message_text)
                        framework_message = TestMessage('unknown')
                        error_result = TestResult('ERROR', 'ERROR',
                                                  message_text, True)
                        framework_message.addTestResult(error_result)
                        test_run.failed += 1
                        test_run.addTestMessage(framework_message)
                    else:
                        message_text = 'line: %d: MALFORMED TEST LOG: previous test run not completed. start a new one: %s %s: found state %s, test run id: %s' % (test_log.line_no, reason, text, test_run.state, test_run.id)
                        logger.warning(message_text)
                        error_result = TestResult('ERROR', 'ERROR',
                                                  message_text, True)
                        test_run.failed += 1
                        prev_framework_message.addTestResult(error_result)

                    test_run.state = 'FRAMEWORK-END'
                    prev_test_run = test_run
                    test_run = TestRun('FRAMEWORK-RUNNING')
                    test_log.addTestRun(test_run)

            elif reason in 'ERROR,FATAL,TEST-PASS,TEST-UNEXPECTED-FAIL,PROCESS-CRASH':
                if False and test_run.state == 'FRAMEWORK-END':
                    # Should we see framework test messages after the framework end?
                    # Yes: can see Automation Error messages after Running tests: end.
                    message_text = 'line: %d: MALFORMED TEST LOG: previous test run already completed. start a new one: %s %s: found state %s, test run id: %s' % (test_log.line_no, reason, text, test_run.state, test_run.id)
                    logger.warning(message_text)
                    framework_message = TestMessage(testpath)
                    error_result = TestResult('ERROR', 'ERROR',
                                              message_text, True)
                    framework_message.addTestResult(error_result)
                    test_run.addTestMessage(framework_message)
                    test_run.failed += 1
                elif test_run.state.find('FRAMEWORK') == -1 and test_run.state != 'TESTSUITE-SHUTDOWN':
                    # Should we see framework test messages when not in a framework state?
                    # XXX: Yes, the framework can issue messges such as
                    # TEST-UNEXPECTED-FAIL application ran for longer than allowed maximum time
                    # when not in a framework state. Remove this error?
                    message_text = 'line: %d: MALFORMED TEST LOG: unexpected framework message: %s %s: found state %s, test run id: %s' % (test_log.line_no, reason, text, test_run.state, test_run.id)
                    logger.warning(message_text)
                    framework_message = TestMessage(testpath)
                    error_result = TestResult('ERROR', 'ERROR',
                                              message_text, True)
                    framework_message.addTestResult(error_result)
                    test_run.addTestMessage(framework_message)
                    test_run.failed += 1

                if reason == 'TEST-PASS':
                    testerror = False
                    test_run.passed += 1
                else:
                    testerror = True
                    test_run.failed += 1

                framework_message = TestMessage(testpath)
                test_result = TestResult(level, reason, text, testerror)
                framework_message.addTestResult(test_result)
                test_run.addTestMessage(framework_message)

                if reason == 'PROCESS-CRASH':
                    found_crash = True

            elif 'Application ran for:' in text:
                valid_states = 'TESTSUITE-SHUTDOWN'

                if test_run.state not in valid_states:
                    message_text = 'line: %d: MALFORMED TEST LOG: %s %s: expected state %s, found state %s, test run id: %s' % (test_log.line_no, reason, text, valid_states, test_run.state, test_run.id)
                    logger.warning(message_text)
                    framework_message = TestMessage(testpath)
                    error_result = TestResult('ERROR', 'ERROR',
                                              message_text, True)
                    framework_message.addTestResult(error_result)
                    test_run.addTestMessage(framework_message)
                    test_run.state = 'TESTSUITE-SHUTDOWN'
                    test_run.failed += 1

                match = re.search('Application ran for: (\d+:\d+:\d+)\.\d+', text)
                if match:
                    test_run.elapsedtime = str((datetime.datetime.strptime(match.group(1), '%H:%M:%S') -
                                                datetime.datetime(1900, 1, 1)).seconds)
                    logger.debug('elapsedtime: %s' % test_run.elapsedtime)

            elif text == 'Running tests: end.':
                valid_states = 'TESTSUITE-SHUTDOWN'

                if test_run.state not in valid_states:
                    message_text = 'line: %d: MALFORMED TEST LOG: %s %s: expected state %s, found state %s, test run id: %s' % (test_log.line_no, reason, text, valid_states, test_run.state, test_run.id)
                    logger.warning(message_text)
                    framework_message = TestMessage('end')
                    error_result = TestResult('ERROR', 'ERROR',
                                              message_text, True)
                    framework_message.addTestResult(error_result)
                    test_run.addTestMessage(framework_message)
                    test_run.failed += 1

                test_run.state = 'FRAMEWORK-END'

            elif text == 'Test summary: start.':
                valid_states = 'FRAMEWORK-END'

                if test_run.state not in valid_states:
                    message_text = 'line: %d: MALFORMED TEST LOG: %s %s: expected state %s, found state %s, test run id: %s' % (test_log.line_no, reason, text, valid_states, test_run.state, test_run.id)
                    logger.warning(message_text)
                    framework_message = TestMessage('end')
                    error_result = TestResult('ERROR', 'ERROR',
                                              message_text, True)
                    framework_message.addTestResult(error_result)
                    test_run.addTestMessage(framework_message)
                    test_run.failed += 1

                test_run.state = 'FRAMEWORK-SUMMARY'

            elif text == 'Test summary: end.':
                valid_states = 'FRAMEWORK-SUMMARY-SHUTDOWN'

                if test_run.state not in valid_states:
                    message_text = 'line: %d: MALFORMED TEST LOG: %s %s: expected state %s, found state %s, test run id: %s' % (test_log.line_no, reason, text, valid_states, test_run.state, test_run.id)
                    logger.warning(message_text)
                    framework_message = TestMessage('end')
                    error_result = TestResult('ERROR', 'ERROR',
                                              message_text, True)
                    framework_message.addTestResult(error_result)
                    test_run.addTestMessage(framework_message)
                    test_run.failed += 1

                test_run.state = 'FRAMEWORK-END'

            else:
                pass

        else:
            mochitest_match = re.match(r'\d+ (INFO|ERROR) (.*)', line)
            reftest_match = re.match(r'REFTEST (.*)', line)

            matched = mochitest_match is not None or reftest_match is not None

            if test_run.suitetype is None:
                if mochitest_match:
                    test_run.suitetype = 'mochitest'
                elif reftest_match:
                    test_run.suitetype = 'reftest'

            if matched and test_run.suitetype == 'mochitest':

                if logger.getEffectiveLevel() == logging.DEBUG:
                    logger.debug('MOCHITEST')
                    if prev_mochitest_message != mochitest_message:
                        logger.debug('PREVIOUS MESSAGE: %s' % prev_mochitest_message)
                        logger.debug('CURRENT  MESSAGE: %s' % mochitest_message)

                prev_mochitest_message = mochitest_message

                if mochitest_match:
                    level = mochitest_match.group(1)  # INFO, ERROR
                    parts = mochitest_match.group(2).split(' | ')
                    logger.debug('parts: %s' % parts)
                    reason = parts[0]

                    if len(parts) > 1:
                        parts[1] = parts[1].rstrip(' |')  # some tests do not have text
                        testpath = parts[1]
                        text = ' | '.join(parts[2:])
                    else:
                        testpath = None
                        text = ' | '.join(parts[1:])

                else:
                    # handle case where we are processing a REFTEST IMAGE
                    # inside of mochitest.
                    level = 'INFO'
                    parts = []
                    reason = 'None'
                    testpath = None
                    text = ''

                logger.debug('state: %s, level: %s, reason: %s, testpath: %s, text: %s' %
                             (test_run.state, level, reason, testpath, text))

                if reason.startswith('SimpleTest START'):  # reason can have trailing Loop <n>

                    valid_states = 'TESTSUITE-SHUTDOWN,FRAMEWORK-RUNNING,FRAMEWORK-SUMMARY'
                    if test_run.state not in valid_states:
                        message_text = 'line: %d: MALFORMED TEST LOG: %s %s: expected state %s, found state %s, test run id: %s' % (test_log.line_no, reason, text, valid_states, test_run.state, test_run.id)
                        logger.warning(message_text)
                        error_message = TestMessage('unknown')
                        error_result = TestResult('ERROR', 'ERROR',
                                                  message_text, True)
                        error_message.addTestResult(error_result)
                        test_run.addTestMessage(error_message)
                        test_run.failed += 1

                    if test_run.state == 'FRAMEWORK-SUMMARY':
                        test_run.state = 'FRAMEWORK-SUMMARY-START'
                    else:
                        test_run.state = 'TESTSUITE-START'

                elif reason == 'TEST-START':

                    valid_states = 'TESTSUITE-START,TESTCASE-END,FRAMEWORK-SUMMARY-START'
                    if testpath.find(' - ') != -1:
                        # robocop testBrowserProvider does a brain dead thing
                        # with test paths by changing the test path by adding
                        # a different suffix separated by a dash (-) to
                        # TEST-START while not ending test cases with a
                        # TEST-END.
                        valid_states += ',TESTCASE-RUNNING'

                    if test_run.state not in valid_states:
                        message_text = 'line: %d: MALFORMED TEST LOG: %s %s: expected state %s, found state %s, test run id: %s' % (test_log.line_no, reason, text, valid_states, test_run.state, test_run.id)
                        logger.warning(message_text)
                        error_message = TestMessage('unknown')
                        error_result = TestResult('ERROR', 'ERROR',
                                                  message_text, True)
                        error_message.addTestResult(error_result)
                        test_run.addTestMessage(error_message)
                        test_run.failed += 1

                    if testpath == 'Shutdown':
                        if test_run.state == 'FRAMEWORK-SUMMARY-START':
                            test_run.state = 'FRAMEWORK-SUMMARY-SHUTDOWN'
                        else:
                            test_run.state = 'TESTSUITE-SHUTDOWN'
                    else:
                        test_run.state = 'TESTCASE-START'
                        mochitest_message = TestMessage(testpath)
                        test_run.addTestMessage(mochitest_message)

                elif reason in 'TEST-PASS,TEST-UNEXPECTED-PASS,TEST-KNOWN-FAIL,TEST-UNEXPECTED-FAIL':

                    valid_states = 'TESTCASE-START,TESTCASE-RUNNING'
                    if test_run.state not in valid_states:
                        message_text = 'line: %d: MALFORMED TEST LOG: %s %s: expected state %s, found state %s, test run id: %s' % (test_log.line_no, reason, text, valid_states, test_run.state, test_run.id)
                        logger.warning(message_text)
                        error_message = TestMessage('unknown')
                        error_result = TestResult('ERROR', 'ERROR',
                                                  message_text, True)
                        error_message.addTestResult(error_result)
                        test_run.addTestMessage(error_message)
                        test_run.failed += 1
                        # fake the missing test start
                        mochitest_message = TestMessage(testpath)
                        test_run.addTestMessage(mochitest_message)
                        test_run.state = 'TESTCASE-START'

                    elif prev_mochitest_message is not None and testpath != prev_mochitest_message.testpath:
                        # mismatched testpath
                        message_text = 'line: %d: MALFORMED TEST RESULTS: %s %s: expected testpath %s, found testpath %s, test run id: %s' % (test_log.line_no, reason, text, prev_mochitest_message.testpath, testpath, test_run.id)
                        logger.warning(message_text)
                        error_message = TestMessage(prev_mochitest_message.testpath)
                        error_result = TestResult('ERROR', 'ERROR',
                                                  message_text, True)
                        error_message.addTestResult(error_result)
                        test_run.addTestMessage(error_message)
                        test_run.failed += 1
                        # fake the missing test start
                        mochitest_message = TestMessage(testpath)
                        test_run.addTestMessage(mochitest_message)
                        test_run.state = 'TESTCASE-START'

                    testerror = 'UNEXPECTED' in reason

                    if 'KNOWN' in reason or 'RANDOM' in reason:
                        test_run.todo += 1
                    elif testerror:
                        test_run.failed += 1
                    else:
                        test_run.passed += 1

                    mochitest_result = TestResult(level, reason, text, testerror)
                    mochitest_message.addTestResult(mochitest_result)

                    if test_run.state != 'TESTCASE-RUNNING':
                        test_run.state = 'TESTCASE-RUNNING'

                elif line.startswith('REFTEST   IMAGE 1 (TEST): '):
                    testpath = prev_mochitest_message.testpath

                    valid_states = 'TESTCASE-RUNNING'
                    if test_run.state not in valid_states:
                        message_text = 'line: %d: MALFORMED TEST LOG: REFTEST   IMAGE 1 (TEST): %s %s: expected state %s, found state %s, test run id: %s' % (test_log.line_no, reason, text, valid_states, test_run.state, test_run.id)
                        logger.warning(message_text)
                        error_result = TestResult('ERROR', 'TEST-UNEXPECTED-FAIL',
                                                  message_text, True)
                        prev_mochitest_message.addTestResult(error_result)
                        test_run.failed += 1
                        test_run.state = 'TESTCASE-RUNNING'

                    match = re.match(r'REFTEST   IMAGE 1 \(TEST\): (.*)', line)
                    prev_mochitest_message.results[-1].image1 = match.group(1)
                    test_run.state = 'TESTCASE-IMG1'

                elif line.startswith('REFTEST   IMAGE 2 (REFERENCE): '):
                    testpath = prev_mochitest_message.testpath

                    valid_states = 'TESTCASE-IMG1'
                    if test_run.state not in valid_states:
                        message_text = 'line: %d: MALFORMED TEST LOG: REFTEST   IMAGE 2 (REFERENCE): %s %s: expected state %s, found state %s, test run id: %s' % (test_log.line_no, reason, text, valid_states, test_run.state, test_run.id)
                        logger.warning(message_text)
                        error_result = TestResult('ERROR', 'TEST-UNEXPECTED-FAIL',
                                                  message_text, True)
                        prev_mochitest_message.addTestResult(error_result)
                        test_run.failed += 1
                        test_run.state = 'TESTCASE-RUNNING'

                    match = re.match(r'REFTEST   IMAGE 2 \(REFERENCE\): (.*)', line)
                    prev_mochitest_message.results[-1].image2 = match.group(1)

                    test_run.state = 'TESTCASE-RUNNING'

                elif reason == 'TEST-END':
                    valid_states = 'TESTCASE-RUNNING'

                    match = re.search(r'finished in (\d+)ms', text)
                    if not match:
                        duration = None  # error?
                    else:
                        duration = match.group(1)

                    if test_run.state not in valid_states:
                        message_text = 'line: %d: MALFORMED TEST LOG: %s %s: expected state %s, found state %s, test run id: %s' % (test_log.line_no, reason, text, valid_states, test_run.state, test_run.id)
                        logger.warning(message_text)
                        error_message = TestMessage('unknown')
                        error_result = TestResult('ERROR', 'ERROR',
                                                  message_text, True)
                        error_message.addTestResult(error_result)
                        test_run.addTestMessage(error_message)
                        test_run.failed += 1
                        # fake the missing test start
                        mochitest_message = TestMessage(testpath)
                        test_run.addTestMessage(mochitest_message)
                        test_run.state = 'TESTCASE-RUNNING'

                    elif testpath != prev_mochitest_message.testpath:
                        # mismatched testpath
                        message_text = 'line: %d: MALFORMED TEST RESULTS: %s %s: expected testpath %s, found testpath %s, test run id: %s' % (test_log.line_no, reason, text, prev_mochitest_message.testpath, testpath, test_run.id)
                        logger.warning(message_text)
                        error_message = TestMessage(prev_mochitest_message.testpath)
                        error_result = TestResult('ERROR', 'ERROR',
                                                  message_text, True)
                        error_message.addTestResult(error_result)
                        test_run.addTestMessage(error_message)
                        test_run.failed += 1
                        # fake the missing test start
                        mochitest_message = TestMessage(testpath)
                        test_run.addTestMessage(mochitest_message)
                        test_run.state = 'TESTCASE-RUNNING'

                    #mochitest_result = TestResult(level, reason, text, False, duration)
                    #mochitest_message.addTestResult(mochitest_result)
                    test_run.state = 'TESTCASE-END'

                elif (test_run.state != 'FRAMEWORK-SUMMARY-SHUTDOWN' and
                      reason.startswith(('Passed: ',
                                         'Failed: ',
                                         'Todo: '))):
                    (countertype, countervalue) = reason.split(': ')
                    countertype = countertype.strip()
                    countervalue = int(countervalue.strip())
                    if countertype == 'Passed':
                        test_run.parsedcounts['passed'] = countervalue
                    elif countertype == 'Failed':
                        test_run.parsedcounts['failed'] = countervalue
                    elif countertype == 'Todo':
                        test_run.parsedcounts['todo'] = countervalue
                    else:
                        pass

                    valid_states = 'TESTSUITE-SHUTDOWN'
                    if test_run.state not in valid_states:
                        # Missing shutdown
                        message_text = 'line: %d: MALFORMED TEST LOG - missing test run shutdown: %s: expected state %s, found state %s, test run id: %s' % (test_log.line_no, line, valid_states, test_run.state, test_run.id)
                        logger.warning(message_text)
                        error_message = TestMessage('unknown')
                        error_result = TestResult('ERROR', 'ERROR',
                                                  message_text, True)
                        error_message.addTestResult(error_result)
                        test_run.addTestMessage(error_message)
                        test_run.failed += 1
                        test_run.state = 'TESTSUITE-SHUTDOWN'

                elif reason == 'TEST-INFO':
                    pass

                elif text == 'SimpleTest FINISHED':
                    valid_states = 'TESTSUITE-SHUTDOWN,FRAMEWORK-SUMMARY-SHUTDOWN'
                    if test_run.state not in valid_states:
                        # Missing shutdown
                        message_text = 'line: %d: MALFORMED TEST LOG - missing test run shutdown: %s: expected state %s, found state %s, test run id: %s' % (test_log.line_no, line, valid_states, test_run.state, test_run.id)
                        logger.warning(message_text)
                        error_message = TestMessage('unknown')
                        error_result = TestResult('ERROR', 'ERROR',
                                                  message_text, True)
                        error_message.addTestResult(error_result)
                        test_run.addTestMessage(error_message)
                        test_run.failed += 1
                        test_run.state = 'TESTSUITE-SHUTDOWN'

            elif matched and test_run.suitetype == 'reftest':

                # REFTEST INFO | runreftest.py | Running tests: start.
                # REFTEST TEST-START | testpath | text
                # REFTEST TEST-PASS(EXPECTED RANDOM) | testpath | text
                # ..
                # REFTEST INFO | Loading a blank page

                # REFTEST TEST-START | testpath1 | n / N (99%)
                # REFTEST TEST-START | testpath2 | n / N (99%)
                # REFTEST TEST-UNEXPECTED-FAIL | testpath1 | text
                # REFTEST   IMAGE 1 (TEST): data:image/png;base64,blah
                # REFTEST   IMAGE 2 (REFERENCE): data:image/png;base64,blah
                # REFTEST INFO | Loading a blank page

                # REFTEST TEST-START | testpath | n / N (99%)
                # REFTEST TEST-PASS | testpath | text
                # REFTEST INFO | Loading a blank page
                # REFTEST TEST-PASS | testpath | text
                # REFTEST INFO | Loading a blank page

                if logger.getEffectiveLevel() == logging.DEBUG:
                    logger.debug('REFTEST')
                    if prev_reftest_message != reftest_message:
                        logger.debug('PREVIOUS MESSAGE: %s' % prev_reftest_message)
                        logger.debug('CURRENT  MESSAGE: %s' % reftest_message)

                prev_reftest_message = reftest_message

                level = 'INFO'
                parts = reftest_match.group(1).split(' | ')

                reason = parts[0]

                if len(parts) > 1:
                    parts[1] = parts[1].rstrip(' |')  # some tests do not have text
                    testpath = parts[1]
                    text = ' | '.join(parts[2:])
                else:
                    testpath = None
                    text = ' | '.join(parts[1:])

                logger.debug('state: %s, level: %s, reason: %s, testpath: %s, text: %s' %
                             (test_run.state, level, reason, testpath, text))

                if reason == 'TEST-START':

                    # with the exception of REFTEST TEST-START | Shutdown,
                    # REFTEST TEST-START actually only signifies that a
                    # document has begun loading and not that a particular
                    # test has started.

                    valid_states = 'FRAMEWORK-RUNNING,TESTCASE-RUNNING'

                    if testpath == 'Shutdown':
                        valid_states += 'TESTSUITE-SHUTDOWN'

                    if test_run.state not in valid_states:
                        message_text = 'line: %d: MALFORMED TEST LOG: %s %s: expected state %s, found state %s, test run id: %s' % (test_log.line_no, reason, text, valid_states, test_run.state, test_run.id)
                        logger.warning(message_text)
                        error_message = TestMessage(testpath)
                        error_result = TestResult('ERROR', 'ERROR',
                                                  message_text, True)
                        error_message.addTestResult(error_result)
                        test_run.addTestMessage(error_message)
                        test_run.failed += 1

                    if testpath == 'Shutdown':
                        test_run.state = 'TESTSUITE-SHUTDOWN'
                    else:
                        test_run.state = 'TESTCASE-RUNNING'

                elif reason.startswith(('TEST-PASS',
                                        'TEST-UNEXPECTED-PASS',
                                        'TEST-KNOWN-FAIL',
                                        'TEST-KNOWN-SLOW',
                                        'TEST-UNEXPECTED-FAIL')):

                    logger.debug('TEST RESULT: %s, %s, %s' % (reason, testpath, text))

                    valid_states = 'TESTCASE-RUNNING'
                    if test_run.state not in valid_states:
                        # tests which are skipped do not have initial TEST-START indicators
                        if reason == 'TEST-KNOWN-FAIL' and text == '(SKIP)':
                            pass
                        elif reason == 'TEST-KNOWN-SLOW' and text == '(SLOW)':
                            pass
                        else:
                            message_text = 'line: %d: MALFORMED TEST LOG: %s %s: expected state %s, found state %s, test run id: %s' % (test_log.line_no, reason, text, valid_states, test_run.state, test_run.id)
                            logger.warning(message_text)
                            error_message = TestMessage(testpath)
                            error_result = TestResult('ERROR', 'ERROR',
                                                      message_text, True)
                            error_message.addTestResult(error_result)
                            test_run.addTestMessage(error_message)
                            test_run.failed += 1

                    testerror = 'UNEXPECTED' in reason

                    if 'KNOWN' in reason or 'RANDOM' in reason:
                        test_run.todo += 1
                    elif testerror:
                        test_run.failed += 1
                    else:
                        test_run.passed += 1

                    new_message = False
                    if reftest_message is None or reftest_message.testpath != testpath:
                        new_message = True
                        reftest_message = TestMessage(testpath)

                    reftest_result = TestResult(level, reason, text, testerror)
                    reftest_message.addTestResult(reftest_result)
                    if new_message:
                        test_run.addTestMessage(reftest_message)

                    test_run.state = 'TESTCASE-RUNNING'

                elif line.startswith('REFTEST   IMAGE 1 (TEST): '):
                    testpath = prev_reftest_message.testpath

                    valid_states = 'TESTCASE-RUNNING'
                    if test_run.state not in valid_states:
                        message_text = 'line: %d: MALFORMED TEST LOG: REFTEST   IMAGE 1 (TEST): %s %s: expected state %s, found state %s, test run id: %s' % (test_log.line_no, reason, text, valid_states, test_run.state, test_run.id)
                        logger.warning(message_text)
                        error_result = TestResult('ERROR', 'ERROR',
                                                  message_text, True)
                        prev_reftest_message.addTestResult(error_result)
                        test_run.failed += 1
                        # fake a failing result to attach the image to.
                        error_result = TestResult('INFO', 'TEST-UNEXPECTED-FAIL',
                                                  'Generated failure', True)
                        prev_reftest_message.addTestResult(error_result)
                        test_run.failed += 1
                        test_run.state = 'TESTCASE-RUNNING'

                    match = re.match(r'REFTEST   IMAGE 1 \(TEST\): (.*)', line)
                    prev_reftest_message.results[-1].image1 = match.group(1)

                    test_run.state = 'TESTCASE-IMG1'

                elif line.startswith('REFTEST   IMAGE 2 (REFERENCE): '):
                    testpath = prev_reftest_message.testpath

                    valid_states = 'TESTCASE-IMG1'
                    if test_run.state not in valid_states:
                        message_text = 'line: %d: MALFORMED TEST LOG: REFTEST   IMAGE 2 (REFERENCE): %s %s: expected state %s, found state %s, test run id: %s' % (test_log.line_no, reason, text, valid_states, test_run.state, test_run.id)
                        logger.warning(message_text)
                        error_result = TestResult('ERROR', 'ERROR',
                                                  message_text, True)
                        prev_reftest_message.addTestResult(error_result)
                        test_run.failed += 1
                        test_run.state = 'TESTCASE-RUNNING'

                    match = re.match(r'REFTEST   IMAGE 2 \(REFERENCE\): (.*)', line)
                    prev_reftest_message.results[-1].image2 = match.group(1)

                    test_run.state = 'TESTCASE-RUNNING'

                elif line == 'REFTEST INFO | Loading a blank page':
                    testpath = prev_reftest_message.testpath
                    text = 'Loading a blank page'

                    valid_states = 'TESTCASE-RUNNING'
                    if test_run.state not in valid_states:
                        message_text = 'line: %d: MALFORMED TEST LOG: %s %s: expected state %s, found state %s, test run id: %s' % (test_log.line_no, reason, text, valid_states, test_run.state, test_run.id)
                        logger.warning(message_text)
                        error_message = TestMessage('unknown')
                        error_result = TestResult('ERROR', 'ERROR',
                                                  message_text, True)
                        error_message.addTestResult(error_result)
                        test_run.addTestMessage(error_message)
                        test_run.failed += 1

                    test_run.state = 'TESTCASE-RUNNING'

                elif line.startswith('REFTEST FINISHED'):

                    valid_states = 'TESTCASE-RUNNING'
                    if test_run.state not in valid_states:
                        # Missing shutdown
                        message_text = 'Counters Expected state %s, found state %s, test run id: %s' % (valid_states, test_run.state, test_run.id)
                        logger.warning(message_text)
                        error_message = TestMessage('unknown')
                        error_result = TestResult('ERROR', 'ERROR',
                                                  message_text, True)
                        error_message.addTestResult(error_result)
                        test_run.addTestMessage(error_message)
                        test_run.failed += 1
                        test_run.state = 'TESTSUITE-SHUTDOWN'

                    test_run.state = 'TESTSUITE-SHUTDOWN'

                elif re.match(r'REFTEST INFO \| (Successful|Unexpected|Known problems):.*', line):

                    valid_states = 'TESTSUITE-SHUTDOWN'
                    if test_run.state not in valid_states:
                        # Missing shutdown
                        message_text = 'line: %d: MALFORMED TEST LOG: %s %s: expected state %s, found state %s, test run id: %s' % (test_log.line_no, reason, text, valid_states, test_run.state, test_run.id)
                        logger.warning(message_text)
                        error_message = TestMessage('unknown')
                        error_result = TestResult('ERROR', 'ERROR',
                                                  message_text, True)
                        error_message.addTestResult(error_result)
                        test_run.addTestMessage(error_message)
                        test_run.failed += 1
                        test_run.state = 'TESTSUITE-SHUTDOWN'

                    counter_match = re.match(r'REFTEST INFO \| ([^:]+): (\d+) .*', line)
                    counter_type = counter_match.group(1)
                    counter_value = int(counter_match.group(2))
                    if counter_type == 'Successful':
                        test_run.parsedcounts['passed'] = counter_value
                    elif counter_type == 'Unexpected':
                        test_run.parsedcounts['failed'] = counter_value
                    elif counter_type == 'Known problems':
                        test_run.parsedcounts['todo'] = counter_value
                    else:
                        pass

                else:
                    pass

        line = test_log.next_line()

    for test_run in test_log.test_runs:
        if test_run.state == 'FRAMEWORK-START':
            message_text = 'UNKNOWN FRAMEWORK: test run id: %s' % test_run.id
            test_log.logger.error(message_text)
            error_message = TestMessage('framework')
            error_result = TestResult('ERROR', 'ERROR',
                                      message_text, True)
            error_message.addTestResult(error_result)
            test_run.addTestMessage(error_message)
            test_run.failed += 1

        elif test_run.state != 'FRAMEWORK-END':
            message_text = 'UNTERMINATED FRAMEWORK: state: %s, test run id: %s' % (test_run.state, test_run.id)
            test_log.logger.error(message_text)
            error_message = TestMessage('framework')
            error_result = TestResult('ERROR', 'ERROR',
                                      message_text, True)
            error_message.addTestResult(error_result)
            test_run.addTestMessage(error_message)
            test_run.failed += 1

        else:
            pass

    return test_log


def main(options):
    if options.test_log:
        test_log_filehandle = open(options.test_log, 'r')
    else:
        test_log_filehandle = sys.stdin

    sys.stdout = os.fdopen(sys.stdout.fileno(), 'w', 0)
    logging.basicConfig(stream=sys.stdout)
    logger = logging.getLogger('logparser')
    log_level = options.log_level.upper()
    logger.setLevel(getattr(logging, log_level))

    test_log = parse_log(test_log_filehandle)

    if options.test_log:
        test_log_filehandle.close()

    if options.dump_converted_test_log:
        converted_test_log = test_log.convert(options.include_passing_tests)
        sys.stdout.flush()
        print "\nCONVERTED: #Test Runs: %d" % len(converted_test_log)
        print json.dumps(converted_test_log, indent=4)

    if options.dump_test_log:
        if not options.include_passing_tests:
            # the native test log automatically includes passing tests.
            # though deleting them does not save memory it will save
            # disk/network if you wish to see/use the native test log.
            test_log.delete_passing_tests()

        print "\nNATIVE: #Test Runs: %d" % len(test_log.test_runs)
        s = test_log.__repr__()
        j = eval(s)
        s = None
        print json.dumps(j, indent=4)
        j = None


if __name__ == "__main__":
    usage = """usage: %prog [options]"""

    parser = OptionParser(usage=usage)
    parser.add_option("--test-log", action="store",
                      dest="test_log",
                      help="path to test log",
                      default=None)
    parser.add_option("--dump-test-log", action="store_true",
                      dest="dump_test_log",
                      help="dump native parsed test log",
                      default=False)
    parser.add_option("--dump-converted-test-log", action="store_true",
                      dest="dump_converted_test_log",
                      help="dump native test log converted to logparse format",
                      default=False)
    parser.add_option("--include-pass", action="store_true",
                      dest="include_passing_tests",
                      help="Include passing results in test logs",
                      default=False)
    parser.add_option("--log-level", action="store",
                      dest="log_level",
                      help="Logging level: debug, info, warning, error, critical",
                      default='warning')
    (options, args) = parser.parse_args()

    main(options)
