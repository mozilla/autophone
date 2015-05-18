# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import json
import logging
import socket
import threading

from kombu import Connection, Exchange, Queue

import utils

DEFAULT_SSL_PORT = 5671


class AutophonePulseMonitor(object):
    """AutophonePulseMonitor provides the means to be notified when
    Android builds are available for testing and when users have initiated
    retriggers and cancels via the Treeherder UI. Builds can be selected using
    repository names, Android platform names or build types.

    AutophonePulseMonitor detects new builds by listening to
    un-normalized buildbot initiated pulse messages rather than the
    normalized messages in order to obtain the check-in comment for a
    build. The comment is used to determine if a try build has
    requested Autophone testing.

    :param hostname: Hostname of Pulse. Defaults to the production pulse
        server pulse.mozilla.org.
    :param userid: Pulse User id
    :param password: Pulse Password
    :param virtual_host: AMQP virtual host, defaults to '/'.
    :param durable_queues: If True, will create durable queues in
        Pulse for the build and job action messages. Defaults to
        False. In production, durable_queues should be set to True to
        avoid losing messages if the connection is broken or the
        application crashes.
    :param build_exchange_name: Name of build exchange. Defaults to
        'exchange/build/'.
    :param build_queue_name: Build queue name suffix. Defaults to
        'builds'. The pulse build queue will be created with a name
        of the form 'queue/<userid>/<build_queue_name>'.
    :param jobaction_exchange_name: Name of job action exchange.
        Defaults to 'exchange/treeherder/v1/job-actions'. Use
        'exchange/treeherder-stage/v1/job-actions' to listen to job
        action messages for Treeherder staging.
    :param jobaction_queue_name: Job action queue name suffix. Defaults to
        'jobactions'. The pulse jobaction queue will be created with a name
        of the form 'queue/<userid>/<jobaction_queue_name>'.
    :param build_callback: Required callback function which takes a
        single `build_data` object as argument containing information
        on matched builds. `build_callback` is always called on a new
        thread.  `build_data` is an object which is guaranteed to
        contain the following keys:
            'appName': Will always be 'Fennec'
            'branch':  The repository name of the build, e.g. 'mozilla-central'.
            'comments': Check-in comment.
            'packageUrl': The url to the apk package for the build.
            'platform': The platform name of the build, e.g. 'android-api-11'
        `build_data` may also contain the following keys:
            'buildid': Build id in CCYYMMDDHHMMSS format.
            'robocopApkUrl': Url to robocop apk for the build.
            'symbolsUrl': Url to the symbols zip file for the build.
            'testsUrl': Url to the tests zip file for the build.
            'who': Check-in Commiter.
    :param jobaction_callback: Required callback function which takes a
        single `jobaction_data` object as argument containing information
        on matched actions. `jobaction_callback` is always called on a new
        thread.  `jobaction_data` is an object which is contains the following keys:
            'action': 'cancel' or 'retrigger',
            'project': repository name,
            'job_id': treeherder job_id,
            'job_guid': treeherder job_guid,
            'build_type': 'opt' or 'debug',
            'platform': the detected platform,
            'build_url': build url,
            'machine_name': name of machine ,
            'job_group_name': treeherder job group name,
            'job_group_symbol': treeherder job group symbol,
            'job_type_name': treeherder job type name,
            'job_type_symbol': treeherder job type symbol,
            'result': test result result',
    :param treeherder_url: Optional Treeherder server url if Treeherder
        job action pulse messages are to be processed. Defaults to None.
    :param trees: Required list of repository names to be matched.
    :param platforms: Required list of platforms to be
        matched. Currently, the possible values are 'android',
        'android-api-9', 'android-api-10', 'android-api-11', and
        'android-x86'.
    :param buildtypes: Required list of build types to
        process. Possible values are 'opt', 'debug'

    Usage:

    ::
    import time
    from optparse import OptionParser

    parser = OptionParser()

    def build_callback(build_data):
        logger = logging.getLogger()
        logger.debug('PULSE BUILD FOUND %s' % build_data)

    def jobaction_callback(job_action):
        logger = logging.getLogger()
        if job_action['job_group_name'] != 'Autophone':
            return
        logger.debug('JOB ACTION FOUND %s' % json.dumps(
            job_action, sort_keys=True, indent=4))

    logging.basicConfig()
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)

    parser.add_option('--pulse-user', action='store', type='string',
                      dest='pulse_user', default='',
                      help='user id for connecting to PulseGuardian')
    parser.add_option('--pulse-password', action='store', type='string',
                      dest='pulse_password', default='',
                      help='password for connecting to PulseGuardian')

    (options, args) = parser.parse_args()

    monitor = AutophonePulseMonitor(
        userid=options.pulse_user,
        password=options.pulse_password,
        jobaction_exchange_name='exchange/treeherder-stage/v1/job-actions',
        build_callback=build_callback,
        jobaction_callback=jobaction_callback,
        trees=['try', 'mozilla-inbound'],
        platforms=['android-api-9', 'android-api-11'],
        buildtypes=['opt'])

    monitor.start()
    time.sleep(3600)
    """

    def __init__(self,
                 hostname='pulse.mozilla.org',
                 userid=None,
                 password=None,
                 virtual_host='/',
                 durable_queues=False,
                 build_exchange_name='exchange/build/',
                 build_queue_name='builds',
                 jobaction_exchange_name='exchange/treeherder/v1/job-actions',
                 jobaction_queue_name='jobactions',
                 build_callback=None,
                 jobaction_callback=None,
                 treeherder_url=None,
                 trees=[],
                 platforms=[],
                 buildtypes=[],
                 timeout=5):

        assert userid, "userid is required."
        assert password, "password is required."
        assert build_callback, "build_callback is required."
        assert trees, "trees is required."
        assert platforms, "platforms is required."
        assert buildtypes, "buildtypes is required."

        self.treeherder_url = treeherder_url
        self.build_callback = build_callback
        self.jobaction_callback = jobaction_callback
        self.trees = list(trees)
        self.platforms = list(platforms)
        # Sort the platforms in descending order of length, so we do
        # not make a match on a substring of the platform prematurely.
        self.platforms.sort(cmp=lambda x,y: (len(y) - len(x)))
        self.buildtypes = list(buildtypes)
        self.timeout = timeout

        self.logger = logging.getLogger()
        self._stopping = threading.Event()
        self.listen_thread = None
        # connection does not connect to the server until either the
        # connection.connect() method is called explicitly or until
        # kombu calls it implicitly as needed.
        self.connection = Connection(hostname=hostname,
                                     userid=userid,
                                     password=password,
                                     virtual_host=virtual_host,
                                     port=DEFAULT_SSL_PORT,
                                     ssl=True)
        build_exchange = Exchange(name=build_exchange_name, type='topic')
        self.queues = [Queue(name='queue/%s/build' % userid,
                             exchange=build_exchange,
                             routing_key='build.#.finished',
                             durable=durable_queues,
                             auto_delete=not durable_queues)]
        if treeherder_url:
            jobaction_exchange = Exchange(name=jobaction_exchange_name, type='topic')
            self.queues.append(Queue(name='queue/%s/jobactions' % userid,
                                 exchange=jobaction_exchange,
                                 routing_key='#',
                                 durable=durable_queues,
                                 auto_delete=not durable_queues))

    def start(self):
        """Runs the `listen` method on a new thread."""
        if self.listen_thread and self.listen_thread.is_alive():
            self.logger.warning('AutophonePulseMonitor.start: listen thread already started')
            return
        self.logger.debug('AutophonePulseMonitor.start: listen thread starting')
        self.listen_thread = threading.Thread(target=self.listen)
        self.listen_thread.daemon = True
        self.listen_thread.start()

    def stop(self):
        """Stops the pulse monitor listen thread."""
        self.logger.debug('AutophonePulseMonitor stopping')
        self._stopping.set()
        self.listen_thread.join()
        self.logger.debug('AutophonePulseMonitor stopped')

    def is_alive(self):
        return self.listen_thread.is_alive()

    def listen(self):
        consumer = self.connection.Consumer(self.queues,
                                            callbacks=[self.handle_message],
                                            accept=['json'],
                                            auto_declare=False)
        for queue in self.queues:
            queue(self.connection).queue_declare(passive=False)
            queue(self.connection).queue_bind()
        with consumer:
            while not self._stopping.is_set():
                try:
                    self.connection.drain_events(timeout=self.timeout)
                except socket.timeout:
                    pass
                except KeyboardInterrupt:
                    raise
        self.logger.debug('AutophonePulseMonitor.listen: stopping')
        self.connection.release()

    def handle_message(self, data, message):
        if self._stopping.is_set():
            return
        message.ack()
        if '_meta' in data and 'payload' in data:
            self.handle_build(data, message)
        if (self.treeherder_url and 'action' in data and
            'project' in data and 'job_id' in data):
            self.handle_jobaction(data, message)

    def handle_build(self, data, message):
        self.logger.debug(
            'handle_build:\n'
            '\tdata   : %s\n'
            '\tmessage: %s' % (
                json.dumps(data, sort_keys=True, indent=4),
                json.dumps(message.__dict__, sort_keys=True, indent=4)))
        try:
            build = data['payload']['build']
        except (KeyError, TypeError), e:
            self.logger.debug('AutophonePulseMonitor.handle_build_event: %s pulse build data' % e)
            return

        fields = (
            'appName',       # Fennec
            'branch',
            'buildid',
            'comments',
            'packageUrl',
            'platform',
            'robocopApkUrl',
            'symbolsUrl',
            'testsUrl',
            'who'
        )

        required_fields = (
            'appName',       # Fennec
            'branch',        # mozilla-central, ...
            'comments',
            'packageUrl',
            'platform',      # android...
        )

        build_data = {}
        builder_name = build['builderName']
        build_data['builder_name'] = builder_name
        build_data['build_type'] = 'debug' if 'debug' in builder_name else 'opt'

        for property in build['properties']:
            property_name = property[0]
            if property_name in fields and len(property) > 1 and property[1]:
                build_data[property_name] = type(property[1])(property[1])

        for required_field in required_fields:
            if required_field not in build_data or not build_data[required_field]:
                return

        if build_data['appName'] != 'Fennec':
            return
        if not build_data['platform'].startswith('android'):
            return
        if build_data['branch'] not in self.trees:
            return
        if build_data['platform'] not in self.platforms:
            return
        if build_data['build_type'] not in self.buildtypes:
            return
        if build_data['branch'] == 'try' and 'autophone' not in build_data['comments']:
            return

        self.build_callback(build_data)

    def handle_jobaction(self, data, message):
        self.logger.debug(
            'handle_jobaction:\n'
            '\tdata   : %s\n'
            '\tmessage: %s' % (
                json.dumps(data, sort_keys=True, indent=4),
                json.dumps(message.__dict__, sort_keys=True, indent=4)))
        action = data['action']
        project = data['project']
        job_id = data['job_id']

        if self.trees and project not in self.trees:
            self.logger.debug('AutophonePulseMonitor.handle_jobaction_event: '
                              'ignoring job action %s on tree %s' % (action, project))
            return

        job = self.get_treeherder_job(project, job_id)
        if not job:
            self.logger.debug('AutophonePulseMonitor.handle_jobaction_event: '
                              'ignoring unknown job id %s on tree %s' % (job_id, project))
            return

        self.logger.debug('AutophonePulseMonitor.handle_jobaction_event: '
                          'job %s' % json.dumps(job, sort_keys=True, indent=4))

        build_type = job['platform_option']
        if self.buildtypes and build_type not in self.buildtypes:
            self.logger.debug('AutophonePulseMonitor.handle_jobaction_event: '
                              'ignoring build type %s on tree %s' % (build_type, project))
            return

        build_artifact = self.get_treeherder_privatebuild_artifact(job)
        if not build_artifact:
            self.logger.debug('AutophonePulseMonitor.handle_jobaction_event: '
                              'ignoring missing privatebuild artifact on tree %s' % project)
            return
        build_url = build_artifact['blob']['build_url']

        # TODO: This needs to be generalized for non-autophone systems
        # where the platform selection is more complicated. Perhaps a
        # regular expression instead of a list?
        detected_platform = job['platform']
        if self.platforms:
            for platform in self.platforms:
                if platform in build_url:
                    detected_platform = platform
                    break
            if not detected_platform:
                self.logger.debug('AutophonePulseMonitor.handle_jobaction_event: '
                                  'ignoring platform for build %s' % build_url)
                return

        jobaction_data = {
            'action': action,
            'project': project,
            'job_id': job_id,
            'job_guid': job['job_guid'],
            'build_type': build_type,
            'platform': detected_platform,
            'build_url': build_url,
            'machine_name': job['machine_name'],
            'job_group_name': job['job_group_name'],
            'job_group_symbol': job['job_group_symbol'],
            'job_type_name': job['job_type_name'],
            'job_type_symbol': job['job_type_symbol'],
            'result': job['result'],
            'config_file': build_artifact['blob']['config_file'],
            'chunk': build_artifact['blob']['chunk'],
        }
        self.jobaction_callback(jobaction_data)

    def get_treeherder_job(self, project, job_id):
        url = '%s/api/project/%s/jobs/%s' % (
            self.treeherder_url, project, job_id)
        return utils.get_remote_json(url, logger=self.logger)

    def get_treeherder_privatebuild_artifact(self, job):
        if job:
            for artifact in job['artifacts']:
                if artifact['name'] == 'privatebuild':
                    url = '%s%s' % (
                        self.treeherder_url, artifact['resource_uri'])
                    return utils.get_remote_json(url, logger=self.logger)
        return None


if __name__ == "__main__":
    import time
    from optparse import OptionParser

    parser = OptionParser()

    def build_callback(build_data):
        logger = logging.getLogger()
        logger.debug('PULSE BUILD FOUND %s' % build_data)

    def jobaction_callback(job_action):
        logger = logging.getLogger()
        if job_action['job_group_name'] != 'Autophone':
            return
        logger.debug('JOB ACTION FOUND %s' % json.dumps(
            job_action, sort_keys=True, indent=4))

    logging.basicConfig()
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)

    parser.add_option('--pulse-user', action='store', type='string',
                      dest='pulse_user', default='',
                      help='user id for connecting to PulseGuardian')
    parser.add_option('--pulse-password', action='store', type='string',
                      dest='pulse_password', default='',
                      help='password for connecting to PulseGuardian')

    (options, args) = parser.parse_args()

    monitor = AutophonePulseMonitor(
        userid=options.pulse_user,
        password=options.pulse_password,
        jobaction_exchange_name='exchange/treeherder-stage/v1/job-actions',
        build_callback=build_callback,
        jobaction_callback=jobaction_callback,
        trees=['try', 'mozilla-inbound'],
        platforms=['android-api-9', 'android-api-11'],
        buildtypes=['opt'])

    monitor.start()
    time.sleep(3600)
