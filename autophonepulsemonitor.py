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
from builds import get_treeherder_tier

DEFAULT_SSL_PORT = 5671

# Set the logger globally in the file, but this must be reset when
# used in a child process.
LOGGER = logging.getLogger()

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
        thread.  `build_data` is an object containing information about the build.
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
        self.platforms.sort(cmp=lambda x, y: (len(y) - len(x)))
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
        # Add the new workerType names to the routing key...
        # See https://bugzilla.mozilla.org/show_bug.cgi?id=1307771
        platforms = list(self.platforms)
        platforms.extend(['gecko-%s-b-android' % level for level in [1,2,3]])
        for platform in platforms:
            # Create a queue for each platform
            self.queues.append(Queue(name='queue/%s/%s' % (userid, taskcompleted_queue_name),
                                     exchange=taskcompleted_exchange,
                                     routing_key='primary.#.#.#.#.#.%s.#.#.#' % platform,
                                     durable=durable_queues,
                                     auto_delete=not durable_queues))
        self.taskcluster_queue = taskcluster.queue.Queue()

    def start(self):
        """Runs the `listen` method on a new thread."""
        if self.listen_thread and self.listen_thread.is_alive():
            LOGGER.warning('AutophonePulseMonitor.start: listen thread already started')
            return
        LOGGER.debug('AutophonePulseMonitor.start: listen thread starting')
        self.listen_thread = threading.Thread(target=self.listen,
                                              name='PulseMonitorThread')
        self.listen_thread.daemon = True
        self.listen_thread.start()

    def stop(self):
        """Stops the pulse monitor listen thread."""
        LOGGER.debug('AutophonePulseMonitor stopping')
        self._stopping.set()
        self.listen_thread.join()
        LOGGER.debug('AutophonePulseMonitor stopped')

    def is_alive(self):
        return self.listen_thread.is_alive()

    def listen(self):
        connect_timeout = 5
        wait = 30
        connection = None
        restart = True
        while restart:
            restart = False
            if self.verbose:
                LOGGER.debug('AutophonePulseMonitor: start shared_lock.acquire')
            self.shared_lock.acquire()
            try:
                # connection does not connect to the server until
                # either the connection.connect() method is called
                # explicitly or until kombu calls it implicitly as
                # needed.
                LOGGER.debug('AutophonePulseMonitor: Connection()')
                connection = Connection(hostname=self.hostname,
                                        userid=self.userid,
                                        password=self.password,
                                        virtual_host=self.virtual_host,
                                        port=DEFAULT_SSL_PORT,
                                        ssl=True,
                                        connect_timeout=connect_timeout)
                LOGGER.debug('AutophonePulseMonitor: connection.Consumer()')
                consumer = connection.Consumer(self.queues,
                                               callbacks=[self.handle_message],
                                               accept=['json'],
                                               auto_declare=False)
                LOGGER.debug('AutophonePulseMonitor: bind queues')
                for queue in self.queues:
                    queue(connection).queue_declare(passive=False)
                    queue(connection).queue_bind()
                with consumer:
                    while not self._stopping.is_set():
                        try:
                            if self.verbose:
                                LOGGER.debug('AutophonePulseMonitor shared_lock.release')
                            self.shared_lock.release()
                            connection.drain_events(timeout=self.timeout)
                        except socket.timeout:
                            pass
                        finally:
                            if self.verbose:
                                LOGGER.debug('AutophonePulseMonitor shared_lock.acquire')
                            self.shared_lock.acquire()
                LOGGER.debug('AutophonePulseMonitor.listen: stopping')
            except:
                LOGGER.exception('AutophonePulseMonitor Exception')
                if connection:
                    connection.release()
                if self.verbose:
                    LOGGER.debug('AutophonePulseMonitor exit shared_lock.release')
                self.shared_lock.release()
                if not self._stopping.is_set():
                    restart = True
                    time.sleep(wait)
                if self.verbose:
                    LOGGER.debug('AutophonePulseMonitor shared_lock.acquire')
                self.shared_lock.acquire()
            finally:
                if self.verbose:
                    LOGGER.debug('AutophonePulseMonitor exit shared_lock.release')
                if connection and not restart:
                    connection.release()
                self.shared_lock.release()

    def handle_message(self, data, message):
        if self._stopping.is_set():
            return
        message.ack()
        try:
            relock = False
            if self.verbose:
                LOGGER.debug('AutophonePulseMonitor handle_message shared_lock.release')
            self.shared_lock.release()
            relock = True
        except ValueError, e:
            if self.verbose:
                LOGGER.debug('AutophonePulseMonitor handle_message shared_lock not set')
        if '_meta' in data and 'payload' in data:
            self.handle_build(data, message)
        elif (self.treeherder_url and 'action' in data and
              'project' in data and 'job_id' in data):
            self.handle_jobaction(data, message)
        elif 'status' in data:
            LOGGER.debug('handle_message: data: %s, message: %s', data, message)
            self.handle_taskcompleted(data, message)
        if relock:
            if self.verbose:
                LOGGER.debug('AutophonePulseMonitor handle_message shared_lock.acquire')
            self.shared_lock.acquire()

    def handle_build(self, data, message):
        if self.verbose:
            LOGGER.debug(
                'handle_build:\n'
                '\tdata   : %s\n'
                '\tmessage: %s',
                json.dumps(data, sort_keys=True, indent=4),
                json.dumps(message.__dict__, sort_keys=True, indent=4))
        try:
            build = data['payload']['build']
        except (KeyError, TypeError), e:
            LOGGER.debug('AutophonePulseMonitor.handle_build_event: %s pulse build data', e)
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
        )

        required_fields = (
            'appName',       # Fennec
            'branch',        # mozilla-central, ...
            'comments',
            'packageUrl',
            'platform',      # android...
        )

        build_properties = {}
        builder_name = build['builderName']
        build_properties['builder_name'] = builder_name

        for prop in build['properties']:
            property_name = prop[0]
            if property_name in fields and len(prop) > 1 and prop[1]:
                build_properties[property_name] = type(prop[1])(prop[1])

        if self.verbose:
            LOGGER.debug('AutophonePulseMonitor.handle_build: build_properties: %s',
                         build_properties)

        for required_field in required_fields:
            if required_field not in build_properties or not build_properties[required_field]:
                return

        if build_properties['appName'].lower() != 'fennec':
            return
        if build_properties['branch'] == 'try' and 'autophone' not in build_properties['comments']:
            return

        build_url = build_properties['packageUrl']
        build_data = utils.get_build_data(build_url, builder_type='buildbot')
        if not build_data:
            LOGGER.debug('AutophonePulseMonitor.handle_build: '
                         'ignoring missing build_data on url %s', build_url)
            return
        build_data['comments'] = build_properties['comments']
        build_data['app_name'] = build_properties['appName'].lower()
        build_data['builder_name'] = builder_name

        if build_data['repo'] not in self.trees:
            return
        if build_data['platform'] not in self.platforms:
            return
        if build_data['build_type'] not in self.buildtypes:
            return

        self.build_callback(build_data)

    def handle_jobaction(self, data, message):
        if self.verbose:
            LOGGER.debug(
                'handle_jobaction:\n'
                '\tdata   : %s\n'
                '\tmessage: %s',
                json.dumps(data, sort_keys=True, indent=4),
                json.dumps(message.__dict__, sort_keys=True, indent=4))
        action = data['action']
        project = data['project']
        job_id = data['job_id']

        if self.trees and project not in self.trees:
            LOGGER.debug('AutophonePulseMonitor.handle_jobaction_event: '
                         'ignoring job action %s on tree %s', action, project)
            return

        job = self.get_treeherder_job(project, job_id)
        if not job:
            LOGGER.debug('AutophonePulseMonitor.handle_jobaction_event: '
                         'ignoring unknown job id %s on tree %s', job_id, project)
            return

        LOGGER.debug('AutophonePulseMonitor.handle_jobaction_event: '
                     'job %s', json.dumps(job, sort_keys=True, indent=4))

        build_type = job['platform_option']
        if self.buildtypes and build_type not in self.buildtypes:
            LOGGER.debug('AutophonePulseMonitor.handle_jobaction_event: '
                         'ignoring build type %s on tree %s', build_type, project)
            return

        build_info = self.get_treeherder_privatebuild_info(project, job)
        if not build_info:
            LOGGER.debug('AutophonePulseMonitor.handle_jobaction_event: '
                         'ignoring missing build info on tree %s', project)
            return
        build_url = build_info['build_url']
        builder_type = build_info['builder_type']

        build_data = utils.get_build_data(build_url, builder_type=builder_type)
        if not build_data:
            LOGGER.debug('AutophonePulseMonitor.handle_jobaction_event: '
                         'ignoring missing build_data on url %s', build_url)
            return

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
                LOGGER.debug('AutophonePulseMonitor.handle_jobaction_event: '
                             'ignoring platform for build %s', build_url)
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
            'config_file': build_info['config_file'],
            'chunk': int(build_info['chunk']),
            'builder_type': builder_type,
        }
        jobaction_data.update(build_data)
        self.jobaction_callback(jobaction_data)

    def handle_taskcompleted(self, data, message):
        if self.verbose:
            LOGGER.debug(
                'handle_taskcompleted:\n'
                '\tdata   : %s\n'
                '\tmessage: %s',
                json.dumps(data, sort_keys=True, indent=4),
                json.dumps(message.__dict__, sort_keys=True, indent=4))
        artifact_data = {}
        task_id = data['status']['taskId']
        run_id = data['runId']
        task_definition = self.taskcluster_queue.task(task_id)
        LOGGER.debug('handle_taskcompleted: task_definition: %s', task_definition)
        # Test the repo early in order to prevent unnecessary IO for irrelevent branches.
        try:
            MH_BRANCH = task_definition['payload']['env']['MH_BRANCH']
            if MH_BRANCH not in self.trees:
                LOGGER.debug('handle_taskcompleted: task_id: %s, run_id: %s: '
                             'skip task_definition MH_BRANCH %s',
                             task_id, run_id, MH_BRANCH)
                return
        except KeyError:
            pass
        worker_type = task_definition['workerType']
        builder_type = 'buildbot' if worker_type == 'buildbot' else 'taskcluster'

        build_data = None
        artifact_data = {}
        artifacts = utils.taskcluster_artifacts(task_id, run_id)
        while True:
            try:
                artifact = artifacts.next()
            except StopIteration:
                break
            key = artifact['name'].replace('public/build/', '')
            artifact_data[key] = 'https://queue.taskcluster.net/v1/task/%s/runs/%s/artifacts/%s' % (
                task_id, run_id, artifact['name'])
            if key == 'target.apk':
                build_data = utils.get_build_data(artifact_data[key], builder_type=builder_type)
                if not build_data:
                    LOGGER.warning('handle_taskcompleted: task_id: %s, run_id: %s: '
                                   'could not get %s', task_id, run_id, artifact_data[key])
                    return
                tier = get_treeherder_tier(build_data['repo'], task_id, run_id)
                if builder_type != 'buildbot' and tier != 1:
                    LOGGER.debug('handle_taskcompleted: ignoring worker_type: %s, tier: %s',
                                 worker_type, tier)
                    return
                build_data['app_name'] = 'fennec'
                build_data['builder_name'] = 'unknown'

        if not build_data:
            LOGGER.debug('handle_taskcompleted: task_id: %s, run_id: %s: '
                         'no build found', task_id, run_id)
            return

        if 'id' not in build_data or 'build_type' not in build_data:
            LOGGER.warning('handle_taskcompleted: task_id: %s, run_id: %s: '
                           'skipping build due to missing id or build_type %s.',
                           task_id, run_id, build_data)
            return

        LOGGER.debug('handle_taskcompleted: task_id: %s, run_id: %s: build_data: %s',
                     task_id, run_id, build_data)
        if build_data['repo'] not in self.trees:
            LOGGER.debug('handle_taskcompleted: task_id: %s, run_id: %s: skip repo %s',
                         task_id, run_id, build_data['repo'])
            return
        if build_data['platform'] not in self.platforms:
            return
        if build_data['build_type'] not in self.buildtypes:
            LOGGER.debug('handle_taskcompleted: task_id: %s, run_id: %s: skip build_type %s',
                         task_id, run_id, build_data['build_type'])
            return

        rev_json_url = build_data['changeset'].replace('/rev/', '/json-rev/')
        rev_json = utils.get_remote_json(rev_json_url)
        if rev_json:
            build_data['comments'] = rev_json['desc']
        else:
            build_data['comments'] = 'unknown'
            LOGGER.warning('handle_taskcompleted: task_id: %s, run_id: %s: could not get %s',
                           task_id, run_id, rev_json_url)

        if build_data['repo'] == 'try' and 'autophone' not in build_data['comments']:
            LOGGER.debug('handle_taskcompleted: task_id: %s, run_id: %s: skip %s %s',
                         task_id, run_id, build_data['repo'], build_data['comments'])
            return

        self.build_callback(build_data)

    def get_treeherder_job(self, project, job_id):
        url = '%s/api/project/%s/jobs/%s/' % (
            self.treeherder_url, project, job_id)
        return utils.get_remote_json(url)

    def get_treeherder_privatebuild_info(self, project, job):
        url = '%s/api/jobdetail/?repository=%s&job_id=%s' % (
            self.treeherder_url, project, job['id'])
        data = utils.get_remote_json(url)
        LOGGER.debug("get_treeherder_privatebuild_info: data: %s", data)

        privatebuild_keys = set(['build_url', 'config_file', 'chunk',
                                 'builder_type'])
        info = {}
        for detail in data['results']:
            if detail['title'] in privatebuild_keys:
                # the build_url property is too long to fit into "value"
                if detail.get('url'):
                    value = detail['url']
                else:
                    value = detail['value']
                info[detail['title']] = value

        # If we're missing a privatebuild key for some reason
        # return None to avoid errors.
        missing_keys = privatebuild_keys - set(info.keys())
        if missing_keys:
            LOGGER.debug("get_treeherder_privatebuild_info: %s "
                         "missing keys: %s "
                         "job: %s", url, missing_keys, job)
            return None

        return info


def main():
    from optparse import OptionParser

    parser = OptionParser()

    def build_callback(build_data):
        LOGGER.debug('PULSE BUILD FOUND %s', build_data)

    def jobaction_callback(job_action):
        if job_action['job_group_name'] != 'Autophone':
            return
        LOGGER.debug('JOB ACTION FOUND %s', json.dumps(
            job_action, sort_keys=True, indent=4))

    LOGGER.setLevel(logging.DEBUG)

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

if __name__ == "__main__":
    main()
