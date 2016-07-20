# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import json
import logging
import socket
import threading
import time

from kombu import Connection, Exchange, Queue
import taskcluster

import utils

DEFAULT_SSL_PORT = 5671

# Set the logger globally in the file, but this must be reset when
# used in a child process.
logger = logging.getLogger()

class AutophonePulseMonitor(object):
    """AutophonePulseMonitor provides the means to be notified when
    Android builds are available for testing and when users have initiated
    retriggers and cancels via the Treeherder UI. Builds can be selected using
    repository names, Android platform names or build types.

    AutophonePulseMonitor detects new buildbot builds by listening to
    un-normalized buildbot initiated pulse messages rather than the
    normalized messages in order to obtain the check-in comment for a
    build. The comment is used to determine if a try build has
    requested Autophone testing.

    Taskcluster builds are detected by listening to the
    exchange/taskcluster-queue/v1/task-completed exchange.

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
        'android-api-9', 'android-api-10', 'android-api-11',
        'android-api-15' and 'android-x86'.
    :param buildtypes: Required list of build types to
        process. Possible values are 'opt', 'debug'
    :param timeout: Timeout in seconds for the kombu connection
        drain_events. Defaults to 5 seconds.
    :param shared_lock: Required lock used to control concurrent
        access. Used to prevent socket based deadlocks.
    :param verbose: If True, will log build and job action messages.
        Defaults to False.

    Usage:

    ::
    import threading
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

    shared_lock = threading.Lock()
    monitor = AutophonePulseMonitor(
        userid=options.pulse_user,
        password=options.pulse_password,
        jobaction_exchange_name='exchange/treeherder-stage/v1/job-actions',
        build_callback=build_callback,
        jobaction_callback=jobaction_callback,
        trees=['try', 'mozilla-inbound'],
        platforms=['android-api-9', 'android-api-11', 'android-api-15'],
        buildtypes=['opt'],
        shared_lock=shared_lock)

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
                 timeout=5,
                 shared_lock=None,
                 verbose=False):

        assert userid, "userid is required."
        assert password, "password is required."
        assert build_callback, "build_callback is required."
        assert trees, "trees is required."
        assert platforms, "platforms is required."
        assert buildtypes, "buildtypes is required."
        assert shared_lock, "shared_lock is required."

        taskcompleted_exchange_name = 'exchange/taskcluster-queue/v1/task-completed'
        taskcompleted_queue_name = 'task-completed'

        self.hostname = hostname
        self.userid = userid
        self.password = password
        self.virtual_host = virtual_host
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
        self.shared_lock = shared_lock
        self.verbose = verbose
        self._stopping = threading.Event()
        self.listen_thread = None
        build_exchange = Exchange(name=build_exchange_name, type='topic')
        self.queues = [Queue(name='queue/%s/%s' % (userid, build_queue_name),
                             exchange=build_exchange,
                             routing_key='build.#.finished',
                             durable=durable_queues,
                             auto_delete=not durable_queues)]
        if treeherder_url:
            jobaction_exchange = Exchange(name=jobaction_exchange_name, type='topic')
            self.queues.append(Queue(name='queue/%s/%s' % (userid, jobaction_queue_name),
                                 exchange=jobaction_exchange,
                                 routing_key='#',
                                 durable=durable_queues,
                                 auto_delete=not durable_queues))
        taskcompleted_exchange = Exchange(name=taskcompleted_exchange_name, type='topic')
        for platform in self.platforms:
            # Create a queue for each platform
            self.queues.append(Queue(name='queue/%s/%s' % (userid, taskcompleted_queue_name),
                                 exchange=taskcompleted_exchange,
                                 routing_key='primary.#.#.#.#.#.%s.#.#.#' % platform,
                                 durable=durable_queues,
                                 auto_delete=not durable_queues))

    def start(self):
        """Runs the `listen` method on a new thread."""
        if self.listen_thread and self.listen_thread.is_alive():
            logger.warning('AutophonePulseMonitor.start: listen thread already started')
            return
        logger.debug('AutophonePulseMonitor.start: listen thread starting')
        self.listen_thread = threading.Thread(target=self.listen,
                                              name='PulseMonitorThread')
        self.listen_thread.daemon = True
        self.listen_thread.start()

    def stop(self):
        """Stops the pulse monitor listen thread."""
        logger.debug('AutophonePulseMonitor stopping')
        self._stopping.set()
        self.listen_thread.join()
        logger.debug('AutophonePulseMonitor stopped')

    def is_alive(self):
        return self.listen_thread.is_alive()

    def listen(self):
        if self.verbose:
            logger.debug('AutophonePulseMonitor: start shared_lock.acquire')
        connection = None
        restart = True
        while restart:
            restart = False
            self.shared_lock.acquire()
            try:
                # connection does not connect to the server until
                # either the connection.connect() method is called
                # explicitly or until kombu calls it implicitly as
                # needed.
                connection = Connection(hostname=self.hostname,
                                        userid=self.userid,
                                        password=self.password,
                                        virtual_host=self.virtual_host,
                                        port=DEFAULT_SSL_PORT,
                                        ssl=True)
                consumer = connection.Consumer(self.queues,
                                               callbacks=[self.handle_message],
                                               accept=['json'],
                                               auto_declare=False)
                for queue in self.queues:
                    queue(connection).queue_declare(passive=False)
                    queue(connection).queue_bind()
                with consumer:
                    while not self._stopping.is_set():
                        try:
                            if self.verbose:
                                logger.debug('AutophonePulseMonitor shared_lock.release')
                            self.shared_lock.release()
                            connection.drain_events(timeout=self.timeout)
                        except socket.timeout:
                            pass
                        except KeyboardInterrupt:
                            raise
                        finally:
                            if self.verbose:
                                logger.debug('AutophonePulseMonitor shared_lock.acquire')
                            self.shared_lock.acquire()
                logger.debug('AutophonePulseMonitor.listen: stopping')
            except:
                logger.exception('AutophonePulseMonitor Exception')
                if connection:
                    connection.release()
                restart = True
                time.sleep(1)
            finally:
                if self.verbose:
                    logger.debug('AutophonePulseMonitor exit shared_lock.release')
                if connection and not restart:
                    connection.release()
                self.shared_lock.release()

    def handle_message(self, data, message):
        if self._stopping.is_set():
            return
        message.ack()
        if '_meta' in data and 'payload' in data:
            self.handle_build(data, message)
        elif (self.treeherder_url and 'action' in data and
            'project' in data and 'job_id' in data):
            self.handle_jobaction(data, message)
        elif 'status' in data:
            logger.debug('handle_message: data: %s, message: %s' % (data, message))
            self.handle_taskcompleted(data, message)

    def handle_build(self, data, message):
        if self.verbose:
            logger.debug(
                'handle_build:\n'
                '\tdata   : %s\n'
                '\tmessage: %s' % (
                    json.dumps(data, sort_keys=True, indent=4),
                    json.dumps(message.__dict__, sort_keys=True, indent=4)))
        try:
            build = data['payload']['build']
        except (KeyError, TypeError), e:
            logger.debug('AutophonePulseMonitor.handle_build_event: %s pulse build data' % e)
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
        build_data['abi'] = 'x86' if 'x86' in builder_name else 'arm'

        for property in build['properties']:
            property_name = property[0]
            if property_name in fields and len(property) > 1 and property[1]:
                build_data[property_name] = type(property[1])(property[1])

        for required_field in required_fields:
            if required_field not in build_data or not build_data[required_field]:
                return

        if build_data['appName'].lower() != 'fennec':
            return
        if not build_data['platform'].startswith('android'):
            return
        if build_data['branch'] not in self.trees:
            return
        if build_data['platform'].replace('-debug', '') not in self.platforms:
            return
        if build_data['build_type'] not in self.buildtypes:
            return
        if build_data['branch'] == 'try' and 'autophone' not in build_data['comments']:
            return

        build_data['sdk'] = 'api-' + build_data['platform'].split('-')[-1]

        # Only use buildbot for non-trunk builds until TaskCluster is available.
        if build_data['branch'] in 'mozilla-beta,mozilla-aurora,mozilla-release':
            self.build_callback(build_data)

    def handle_jobaction(self, data, message):
        if self.verbose:
            logger.debug(
                'handle_jobaction:\n'
                '\tdata   : %s\n'
                '\tmessage: %s' % (
                    json.dumps(data, sort_keys=True, indent=4),
                    json.dumps(message.__dict__, sort_keys=True, indent=4)))
        action = data['action']
        project = data['project']
        job_id = data['job_id']

        if self.trees and project not in self.trees:
            logger.debug('AutophonePulseMonitor.handle_jobaction_event: '
                              'ignoring job action %s on tree %s' % (action, project))
            return

        job = self.get_treeherder_job(project, job_id)
        if not job:
            logger.debug('AutophonePulseMonitor.handle_jobaction_event: '
                              'ignoring unknown job id %s on tree %s' % (job_id, project))
            return

        logger.debug('AutophonePulseMonitor.handle_jobaction_event: '
                          'job %s' % json.dumps(job, sort_keys=True, indent=4))

        build_type = job['platform_option']
        if self.buildtypes and build_type not in self.buildtypes:
            logger.debug('AutophonePulseMonitor.handle_jobaction_event: '
                              'ignoring build type %s on tree %s' % (build_type, project))
            return

        build_artifact = self.get_treeherder_privatebuild_artifact(job)
        if not build_artifact:
            logger.debug('AutophonePulseMonitor.handle_jobaction_event: '
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
                logger.debug('AutophonePulseMonitor.handle_jobaction_event: '
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

    def handle_taskcompleted(self, data, message):
        if self.verbose:
            logger.debug(
                'handle_taskcompleted:\n'
                '\tdata   : %s\n'
                '\tmessage: %s' % (
                    json.dumps(data, sort_keys=True, indent=4),
                    json.dumps(message.__dict__, sort_keys=True, indent=4)))
        tc_queue = taskcluster.Queue()
        build_metadata = {}
        artifact_data = {}
        task_id = data['status']['taskId']
        run_id = data['runId']
        # The sdk level is only available from the pulse
        # message workerType and is of the form android-(arm|x86)-15
        build_metadata['platform'] = data['status']['workerType']
        build_metadata['sdk'] = 'api-' + build_metadata['platform'].split('-')[-1]
        artifact_data = {}
        response = tc_queue.listArtifacts(task_id, run_id)
        while True:
            if 'artifacts' not in response:
                logger.warning('handle_taskcompleted: listArtifacts(%s, %s) '
                               'response missing artifacts' % (task_id, run_id))
                break
            for artifact in response['artifacts']:
                key = artifact['name'].replace('public/build/', '')
                artifact_data[key] = 'https://queue.taskcluster.net/v1/task/%s/runs/%s/artifacts/%s' % (
                    task_id, run_id, artifact['name'])
                if key == 'target.apk':
                    build_data = utils.get_build_data(artifact_data[key])
                    if not build_data:
                        logger.warning('handle_taskcompleted: task_id: %s, run_id: %s: '
                                       'could not get %s' % (task_id, run_id, artifact_data[key]))
                        break
                    build_metadata['packageUrl'] = artifact_data[key]
                    build_metadata['buildid'] = build_data['id']
                    build_metadata['changeset'] = build_data['changeset']
                    build_metadata['branch'] = build_data['repo']
                    build_metadata['revision'] = build_data['revision']
                elif key == 'target.mozinfo.json':
                    mozinfo_json = utils.get_remote_json(artifact_data[key])
                    if not mozinfo_json:
                        logger.warning('handle_taskcompleted: task_id: %s, run_id: %s: '
                                       'could not get %s' % (task_id, run_id, artifact_data[key]))
                        break
                    logger.debug('handle_taskcompleted: mozinfo_json: %s' % mozinfo_json)
                    build_metadata['appName'] = mozinfo_json['appname']
                    build_metadata['builder_name'] = 'unknown'
                    if mozinfo_json['debug']:
                        build_metadata['build_type'] = 'debug'
                    else:
                        build_metadata['build_type'] = 'opt'
                    build_metadata['abi'] = mozinfo_json['processor']
                elif key == 'robocop.apk':
                    build_metadata['robocopApkUrl'] = artifact_data[key]
                elif key.endswith('crashreporter-symbols.zip'):
                    build_metadata['symbolsUrl'] = artifact_data[key]

            if 'continuationToken' not in response:
                break
            logger.debug('handle_taskcompleted: continuing listArtifacts(%s, %s)' %
                         (task_id, run_id))
            response = tc_queue.listArtifacts(task_id, run_id, {
                'continuationToken': response['continuationToken']})

        if 'buildid' not in build_metadata or 'build_type' not in build_metadata:
            logger.debug('handle_taskcompleted: task_id: %s, run_id: %s: '
                         'skipping build due to incomplete metadata %s. Not a build?' %
                         (task_id, run_id, build_metadata))
            return

        logger.debug('handle_taskcompleted: task_id: %s, run_id: %s: build_metadata: %s' % (task_id, run_id, build_metadata))

        if build_metadata['appName'] != 'fennec':
            logger.debug('handle_taskcompleted: task_id: %s, run_id: %s: skip appName %s' % (task_id, run_id, build_metadata['appName']))
            return
        if build_metadata['branch'] not in self.trees:
            logger.debug('handle_taskcompleted: task_id: %s, run_id: %s: skip branch %s' % (task_id, run_id, build_metadata['branch']))
            return
        if build_metadata['platform'] not in self.platforms:
            return
        if build_metadata['build_type'] not in self.buildtypes:
            logger.debug('handle_taskcompleted: task_id: %s, run_id: %s: skip build_type %s' % (task_id, run_id, build_metadata['build_type']))
            return

        rev_json_url = build_metadata['changeset'].replace('/rev/', '/json-rev/')
        rev_json = utils.get_remote_json(rev_json_url)
        if rev_json:
            build_metadata['comments'] = rev_json['desc']
            build_metadata['who'] = rev_json['user']
        else:
            build_metadata['comments'] = 'unknown'
            build_metadata['who'] = 'unknown'
            logger.warning('handle_taskcompleted: task_id: %s, run_id: %s: could not get %s' % (task_id, run_id, rev_json_url))

        if build_metadata['branch'] == 'try' and 'autophone' not in build_metadata['comments']:
            logger.debug('handle_taskcompleted: task_id: %s, run_id: %s: skip %s %s' % (
                task_id, run_id, build_metadata['branch'], build_metadata['comments']))
            return

        # Use TaskCluster for trunk builds until TaskCluster is available elsewhere.
        if build_metadata['branch'] not in 'mozilla-beta,mozilla-aurora,mozilla-release':
            self.build_callback(build_metadata)

    def get_treeherder_job(self, project, job_id):
        url = '%s/api/project/%s/jobs/%s/' % (
            self.treeherder_url, project, job_id)
        return utils.get_remote_json(url)

    def get_treeherder_privatebuild_artifact(self, job):
        if job:
            for artifact in job['artifacts']:
                if artifact['name'] == 'privatebuild':
                    url = '%s%s' % (
                        self.treeherder_url, artifact['resource_uri'])
                    return utils.get_remote_json(url)
        return None


if __name__ == "__main__":
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

    shared_lock = threading.Lock()
    monitor = AutophonePulseMonitor(
        userid=options.pulse_user,
        password=options.pulse_password,
        jobaction_exchange_name='exchange/treeherder-stage/v1/job-actions',
        build_callback=build_callback,
        jobaction_callback=jobaction_callback,
        trees=['try', 'mozilla-inbound'],
        platforms=['android-api-9', 'android-api-11', 'android-api-15'],
        buildtypes=['opt'],
        shared_lock=shared_lock)

    monitor.start()
    time.sleep(3600)
