# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import threading

from mozillapulse.consumers import BuildConsumer

class AutophonePulseBuildMonitor(object):
    """AutophonePulseBuildMonitor provides the means to be notified when
    Android builds are available for testing. Builds can be selected using
    repository names, Android platform names or build types.

    AutophonePulseBuildMonitor listens to un-normalized buildbot pulse
    messages rather than the normalized messages in order to obtain
    the check-in comment for a build. The comment is used to determine
    if a try build has requested Autophone testing.

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
    :param trees: Required list of repository names to be matched.
    :param platforms: Required list of platforms to be
        matched. Currently, the possible values are 'android',
        'android-api-9', 'android-api-10', 'android-api-11', and
        'android-x86'.
    :param buildtypes: Required list of build types to
        process. Possible values are 'opt', 'debug'
    :param logger: Required Logger instance.
    :param pulse_applabel: Required applabel for the Pulse queue.
    :param pulse_config: Required PulseConfiguration object for Pulse.
    :param durable: Boolean which if True creates a durable queue.
        Defaults to True.

    Usage:

    ::

            # Define the PulseConfiguration object to be
            # used to connect to Pulse.
            pulse_config = PulseConfiguration(
                             user='user',
                             password='password')
            # Define the callback function to process
            # the build notifications.
            def on_build(build_data):
                pass

            # Create the monitor object with the desired
            # selection parameters.
            monitor = AutophonePulseBuildMonitor(
                build_callback=on_build,
                trees=['mozilla-inbound', 'try'],
                platforms=['android',
                           'android-api-9',
                           'android-api-10',
                           'android-api-11',
                           'android-x86'],
                buildtypes=['opt'],
                logger=logger,
                pulse_applabel='autophone-build-monitor',
                pulse_config=pulse_config)

            # Call the monitor's start method to start
            # the listener on a new thread.
            monitor.start()
    """

    def __init__(self,
                 build_callback=None,
                 trees=[],
                 platforms=[],
                 buildtypes=[],
                 logger=None,
                 pulse_applabel=None,
                 pulse_config=None,
                 durable=True):
        """Initializes AutophonePulseBuildMonitor object."""

        assert build_callback, "build_callback is required."
        assert trees, "trees is required."
        assert platforms, "platforms is required."
        assert buildtypes, "buildtypes is required."
        assert logger is not None, "logger is required."
        assert pulse_applabel is not None, "pulse_applabel is required."
        assert pulse_config is not None, "pulse_config is required."

        self.build_callback = build_callback
        self.trees = list(trees)
        self.platforms = list(platforms)
        self.buildtypes = list(buildtypes)
        self.logger = logger
        self.pulse_config = pulse_config
        self.pulse_args = {
            'connect': False, # False so we can use PulseConfiguration
            'topic': 'build.#.finished',
            'applabel': pulse_applabel,
            'durable': durable,
            'callback': self.on_build_event,
        }
        self.listen_thread = None
        self.logger.info('Created AutophonePulseBuildMonitor %s' %
                         self.pulse_args['applabel'])

    def listen(self):
        """Construct a mozillapulse.consumers.BuildConsumer to
        listen to un-normalized buildbot pulse messages. Use the
        `start` method to call `listen` from a thread since the
        call to BuildConsumer.listen blocks.
        """

        while True:
            try:
                build_consumer = BuildConsumer(**self.pulse_args)
                build_consumer.config = self.pulse_config
                build_consumer.connect()
                build_consumer.listen()
            except KeyboardInterrupt:
                raise
            except IOError, e:
                self.logger.warning('Error %s during pulse.listen()' % e)
                pass
            except:
                self.logger.exception('Error during pulse.listen()')

    def handle_build_event(self, data, message):
        """Process the pulse message and calls the callback function
        on build matching the specified trees, platforms and build types.

        :param data: Buildbot pulse message data.
        :param message: Buildbot pulse message metadata.
        """
        try:
            build = data['payload']['build']
        except (KeyError, TypeError), e:
            self.logger.debug('AutophonePulseBuildMonitor.handle_build_event: %s pulse build data' % e)
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
            if property_name in fields:
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

    def start(self):
        """Runs the `listen` method on a new thread."""
        if self.listen_thread:
            self.logger.warning('AutophonePulseBuildMonitor.start: listen thread already started')
            return
        self.logger.debug('AutophonePulseBuildMonitor.start: listen thread starting')
        self.listen_thread = threading.Thread(target=self.listen)
        self.listen_thread.daemon = True
        self.listen_thread.start()

    def on_build_event(self, data, message):
        """Runs the callback function on a new thread."""
        try:
            message.ack()
        except Exception, e:
            # ack can raise the following error due to issues with how
            # the ssl library is used:
            # SSLError: [Errno 1] _ssl.c:1312: error:1409F07F:SSL routines:SSL3_WRITE_PENDING:bad write retry
            # Whatever error may occur it should not cause us to miss
            # processing the message or to cause the program to terminate.
            # Therefore, we will log the error and retry once and continue.
            self.logger.warning('Error %s acknowledging pulse message. '
                                'Retrying...')
            try:
                message.ack()
            except Exception, e:
                self.logger.warning('Repeated Error %s acknowledging pulse message.' % e)
        callback_thread = threading.Thread(
            target=self.handle_build_event,
            args=(data, message))
        callback_thread.daemon = True
        callback_thread.start()
