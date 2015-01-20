# Using Autophone

## tl;dr

Once you have set up a devices.ini
[devices configuration file](#devices-configuration-file), you can run
the following right out of the box with default configurations. With
the default configuration, Autophone will test builds from the
mozilla-central repository and the results will be written to
autophone\*.log files and autophone-results\*.csv files. Note that
autophone.log contains log messages for Autophone itself along with
log messages from each of the test workers. Logs for individual
devices can be found in the files autophone-*devicename*.log

* [Run Smoketest with default options](#configuring-smoketest)

        python autophone.py --devices devices.ini --test-path tests/smoketest_manifest.ini

* [Run WebappStartupTest with default options](#configuring-webappstartuptest)

        python autophone.py --devices devices.ini --test-path tests/webappstartup_manifest.ini

* [Run S1S2Test with default options](#configuring-s1s2test)

        python autophone.py --devices devices.ini

        python autophone.py --devices devices.ini --test-path tests/manifest.ini

If you have a more complicated use case, read the rest of this
document, select your tests and command line options in
autophone.ini, set up your test configurations in [configs/](configs/) and
then run:

* [Run Autophone with config file](#autophone-command-line-options)

        python autophone.py --config autophone.ini

When Autophone detects that a build is available to be tested, the main
Autophone process downloads it and notifies all of its workers, which
install it on the phones and begin a test run by executing the tests
listed in the [test manifest file](#test-manifest-file). If a test run
is ongoing, the new job is queued.

When Autophone has initialized, it will output a message 'Autophone
started' to the console.  You can then manually trigger test runs on
builds with `trigger_runs.py`. This script can trigger tests given a
build ID, date/time range, revision range, or build url.

Fennec developers will probably be most interested in using
`trigger_runs.py` with the `--build-url=BUILD_URL` option. With
`--build-url`, it is possible to test local builds or builds located
on ftp.mozilla.org including try builds. You can also select builds
from different build locations via the --build-location option.

* all nightly (http://ftp.mozilla.org/pub/mozilla.org/firefox/nightly)
  builds for mozilla-central for a date range:

        python trigger_runs.py 2014-10-01T00:00:00 2014-10-07T00:00:00

* tinderbox
  (http://ftp.mozilla.org/pub/mozilla.org/firefox/tinderbox-builds/)
  builds for fx-team for build dates on or after a date:

         python trigger_runs.py --build-location=tinderbox --repo=fx-team 2014-10-25T11:10:11

* tinderbox build for fx-team with buildid

         python trigger_runs.py --build-location=tinderbox --repo=fx-team 20141025111011

* inboundarchive (http://inbound-archive.pub.build.mozilla.org/)
  builds for fx-team by revision range:

        python trigger_runs.py --build-location=inboundarchive --repo=fx-team --first-revision 0c0f0b98deee --last-revision c7fc9fadd71e

* try-server build:

        python trigger_runs.py --repo=try --build-url=http://ftp.mozilla.org/pub/mozilla.org/mobile/try-builds/wlitwinczyk@mozilla.com-fde6088fe349/try-android/

* local build:

        python trigger_runs.py --repo=mozilla-central --build-url=/mozilla/builds/nightly/mozilla/fennec-opt/dist/

See
[trigger_runs.py command line options](trigger_runspy-command-line-options)
for more details on running `trigger_runs.py`.

Autophone can be configured to listen to
[Pulse](https://wiki.mozilla.org/Auto-tools/Projects/Pulse) for new
fennec builds. To use pulse notification, you will need to get a Pulse
Guardian account from
[https://pulse.mozilla.org/](https://pulse.mozilla.org/) and use
[Autophone's pulse related options](autophonepy-command-line-options):

    --enable-pulse
    --pulse-applabel=PULSE_APPLABEL
    --pulse-durable-queue
    --pulse-user=PULSE_USER
    --pulse-password=PULSE_PASSWORD

## Configuring Autophone

Autophone has many configuration options which can be used to
customize its operation, although the default values will be sufficient
for basic purposes. For a full description of Autophone's options see
[Autophone Command Line Options](#autophone-command-line-options).

The [Autophone production configuration](PRODUCTION.md) is an example
of a fully configured system using all of the features described here.

Two configuration files are *required* for running tests with
Autophone: the *Devices configuration file* and the *Test manifest
file*. Individual tests may also use configuration files which are
located in [configs/](configs/).

### Devices configuration file

The devices configuration file, typically named `devices.ini`, is used
to define the set of devices which Autophone will use. It also
provides a mapping between user readable names and the adb serial
numbers for each device.

The file is a standard ini file which has a section for
each device in the form:

    [devicename]
    serialno=deviceserialnumber

You can determine the adb serial number for a device by connecting
each device to your computer one at a time, then executing `adb
devices`.  For example, if you have a Nexus One device which you which
to call nexus-one-1, get the serial number of the device via:

    $ adb devices
    List of devices attached
    HT018P800097	device

Then edit devices.ini to contain:

    [nexus-one-1]
    serialno=HT018P800097

Add an additional section for each device you wish to test.

You can use emulators instead of physical devices, but your host
operating system must support the "Use Host GPU" option. If you can
install and run Fennec on your emulator, then you should be able to
use emulators in Autophone.

Specifying an emulator in the devices configuration file is the same
as for physical devices, except that you will need to use
emulator-*port* for the device serial number.

An example devices ini file can be found at
[devices.ini.example](devices.ini.example).

### Test manifest file

The test manifest file is used to select the tests which Autophone
will run. The file is a standard ini file which has a section for each
test which can contain a `config` option which is a space separated
list of test configuration files and per-device options. Tests are
implemented as python scripts in the [tests/](tests/) directory. The
section name for a test is the basename of the test's script.

The value of the `config` option for each test section is a space
separated list of paths (relative to the test manifest file) to test
configuration files which can be used to provide additional
configuration information for the test. Most tests will use a single
config file. In cases like the unit tests where the same python
test script can run different tests, using multiple config files allows
the creation of different configurations of the same test script. If
the `config` file does not exist or it otherwise not readable, default
values for the settings will be used.

The names of the optional devicename options are the device names, and
their values are space delimited list of repository names which the
device will test.

    [exampletest.py]
    config = ../configs/exampletest_settings.ini
    devicename = mozilla-central mozilla-inbound

You can find other examples in
[Configuring Smoketest](#configuring-smoketest),
[Autophone production configuration](PRODUCTION.md) and
[tests/](tests/).

#### Common Settings in the Test configuration file

Autophone has the following tests:

* Smoketest
* WebappStartupTest
* S1S2Test
* UnitTest

Each of these tests *may* be configured to report results to
Treeherder by adding a `[treeherder]` section to the test's
configuration file which defines the test's Treeherder display.

    [treeherder]
    job_name = test job display name
    job_symbol = test job display symbol
    group_name = test group name
    group_symbol = test group symbol

*Note*: Sending results to Treeherder requires additional options:

          --treeherder-url=TREEHERDER_URL
          --treeherder-credentials-path=TREEHERDER_CREDENTIALS_PATH
          --treeherder-retries=TREEHERDER_RETRIES
          --treeherder-retry-wait=TREEHERDER_RETRY_WAIT

See the Treeherder-related options in
[Autophone Command Line Options](#autophone-command-line-options) for more details.

#### Configuring Smoketest

Smoketest is used to test if Fennec is able to run on devices and
if it is possible to detect Throbber start and stop values in the
logcat output. It is implemented by
[tests/smoketest.py](tests/smoketest.py).

The smoketest manifest file
[tests/smoketest_manifest.ini](tests/smoketest_manifest.ini) is the
simplest example which we can use to illustrate the different
components of a test manifest file. The Smoketest manifest file is
`tests/smoketest_settings.ini` which consists of a single section for
the smoketest:

    [smoketest.py]

If we wished the smoketest to send its results to Treeherder, we could
copy
[`configs/smoketest_settings.ini.example`](configs/smoketest_settings.ini.example)
to `configs/smoketest_settings.ini`. It already contains the settings
for Treeherder:

    [treeherder]
    job_name = Autophone Smoketest
    job_symbol = s
    group_name = Autophone
    group_symbol = A

Then add a config option to the test manifest file
`tests/smoketest_settings.ini` pointing to the test configuration
settings file.

    [smoketest.py]
    config = ../configs/smoketest_settings.ini

If we wished to only test mozilla central builds on the nexus-one-1
device and mozilla-inbound builds on the nexus-one-2 device, we would
add a device option for each device as in:

    [smoketest.py]
    config = ../configs/smoketest_settings.ini
    nexus-one-1 = mozilla-central
    nexus-one-2 = mozilla-inbound

*Note*: Each device listed in the test manifest file *must* also be
listed in the [devices configuration ini file](#devices-configuration-file).

*Note*: Each repository that is to be tested must be listed in the
Autophone command line options. See the `--repo` option in
[Autophone Command Line Options](#autophone-command-line-options).

#### Configuring Performance Tests

PerfTest is a special test class which is used as the base class for
the performance tests WebappStartupTest and S1S2Test. It is not executed
directly, but serves to consolidate the common features of the Autophone
performance tests.

Performance tests are configured via a test configuration ini file
which may contain `[settings]`, `[signature]` and `[treeherder]`
sections.

##### Performance Test configuration file sections

* settings

  `[settings]` may contain the following options:

  * resulturl

      Autophone can submit its performance tests results to the
      phonedash server specified in the resulturl option. resulturl
      has the form http://server:port/api/s1s2/.

      If resulturl is not specified, it will default to None.

      For production, use http://phonedash.mozilla.org/api/s1s2/.

      For staging, use http://phonedash-dev.mozilla.org/api/s1s2/.

      For a local phonedash instance running on your own machine, use
      http://localhost:18000/api/s1s2/

      If you set resulturl to None, then each device's results will be
      written to comma delimited files named
      autophone-results-deviceid.csv. Use None when you do not wish to
      post results to a phonedash server and only want local test
      results.

  * iterations

      The number of times the test should be repeated for each build.
      If not set, iterations defaults to 1.

  * stderrp_accept

      The iterations for a test are terminated early if the standard
      error falls below stderrp_accept.  If not set, stderrp_accept
      defaults to 0.

  * stderrp_reject

      The iterations for a test are rejected if the standard
      error exceeds stderrp_reject.
      If not set, stderrp_reject defaults to 100.

  * stderrp_attempts

      The iterations for a test are retried up to
      stderrp_attempts if they are rejected.
      If not set, stderrp_attempts defaults to 1.

* signature

  The `[signature]` section is used to specify the jot credentials used to
  post results to the phonedash server specified in the resulturl option.

  It may contain the following options:

  * id

      The jot id to be used to post results to the phonedash server.

  * key

      The jot key to be used to post results to the phonedash server.

#### Configuring WebappStartupTest

WebappStartupTest installs webapp-startup-test.apk, launches the app,
then measures Web app start times by detecting `WEBAPP STARTUP
COMPLETE` messages in logcat. It is implemented by
[tests/webappstartup.py](tests/webappstartup.py).

WebappStartupTest is a Performance test and shares the
[Performance test configuration](#configuring-performance-tests)
options. To create your `configs/webappstartup_settings.ini` file,
start by copying
[`configs/webappstartup_settings.ini.example`](webappstartup_settings.ini.example)
to `configs/webappstartup_settings.ini` then customize the settings to
fit your needs.

Note that it is possible to also add per device options to the
webappstartuptest.py section in the same fashion as we did for the
Smoketest.

#### Configuring S1S2Test

S1S2Test measures fennec load times for web pages by detecting the
Throbber start and Throbber stop messages in the logcat output. It is
implemented by [tests/s1s2test.py](tests/s1s2test.py).

S1S2Test is a Performance test and shares the
[Performance test configuration](#configuring-performance-tests)
options. To create your `configs/s1s2_settings.ini` file, start by
copying
[`configs/s1s2_settings.ini.example`](configs/s1s2_settings.ini.example)
to `configs/s1s2_settings.ini` then customize the settings to fit your
needs.

Note that it is possible to also add per device options to the s1s2test.py section in the
same fashion as we did for the Smoketest.

#### Configuring UnitTests

Autophone can run individual unit tests such as robocop, reftests,
crashtests, jsreftests or mochitests for each build or it can run combinations of
them.

Before running the unit tests, you will need to copy
configs/unittest_defaults.ini.example to configs/unittest_defaults.ini
and edit configs/unittest_defaults.ini to change the xre_path and
utility_path values.

For example,

to run only the robocop tests:

   python autophone.py --devices=devices.ini --enable-unittests --test-path=./tests/robocoptests_manifest.ini

to run all of the unit tests specified in the tests/unittests_manifest.ini file:

   python autophone.py --devices=devices.ini --enable-unittests --test-path=./tests/unittests_manifest.ini

#### Configuring Multiple tests simultaneously

It is possible to combine multiple tests into a single Test manifest
file in order to execute more than one type of test per Autophone
instance. Configuration is identical to configuring the individual
test manifests as described above with a separate section for each
test. For example:

##### haxxor_virginia_manifest.ini

        [smoketest.py]
        config = ../configs/smoketest_settings.ini
        nexus-one-1=mozilla-inbound
        nexus-one-2=mozilla-central
        nexus-one-3=fx-team
        samsung-gs2-1=fx-team
        samsung-gs3-1=mozilla-inbound
        samsung-gs3-2=mozilla-inbound

        [s1s2test.py]
        config = ../configs/s1s2_settings.ini
        nexus-one-1=mozilla-inbound
        nexus-one-2=mozilla-central
        nexus-one-3=fx-team
        samsung-gs2-1=fx-team
        samsung-gs3-1=mozilla-inbound
        samsung-gs3-2=mozilla-inbound

        [webappstartup.py]
        config = ../configs/webappstartup_settings.ini
        nexus-one-1=mozilla-inbound
        nexus-one-2=mozilla-central
        nexus-one-3=fx-team
        samsung-gs2-1=fx-team
        samsung-gs3-1=mozilla-inbound
        samsung-gs3-2=mozilla-inbound

        [runtestsremote.py]
        config = ../configs/crashtests_settings.ini ../configs/jsreftests_settings.ini ../configs/mochitests_settings.ini ../configs/mochitests_skia_settings.ini ../configs/reftests_settings.ini ../configs/robocoptests_settings.ini
        unittests = 1

### Autophone Command Line Options

Autophone supports a large number of options. Since this can be
unwieldy, one of the options it supports is `--config=AUTOPHONECFG`
which can be used to store your preferred command line options in a
file to be easily reused. To set up your own Autophone configuration
file, copy [`autophone.ini.example`](autophone.ini.example) to
`autophone.ini` and edit it to suit your needs.

#### autophone.py command line options

        Usage: autophone.py [options]

        Options:
          -h, --help            show this help message and exit
          --ipaddr=IPADDR       IP address of interface to use for phone callbacks,
                                e.g. after rebooting. If not given, it will be
                                guessed.
          --port=PORT           Port to listen for incoming connections, defaults to
                                28001
          --logfile=LOGFILE     Log file to store logging from entire system.
                                Individual phone worker logs will use
                                <logfile>-<phoneid>[.<ext>]. Default: autophone.log
          --loglevel=LOGLEVEL   Log level - ERROR, WARNING, DEBUG, or INFO, defaults
                                to INFO
          -t TEST_PATH, --test-path=TEST_PATH
                                path to test manifest
          --minidump-stackwalk=MINIDUMP_STACKWALK
                                Path to minidump_stackwalk executable; defaults to
                                /usr/local/bin/minidump_stackwalk.
          --emailcfg=EMAILCFG   config file for email settings; defaults to none
          --enable-pulse        Enable connecting to Pulse to look for new builds. If
                                specified, --pulse-user and --pulse-password must also
                                be specified.
          --pulse-applabel=PULSE_APPLABEL
                                Applabel for Pulse queue; defaults to autophone-build-
                                monitor.
          --pulse-durable-queue
                                Use a durable queue when connecting to Pulse.
          --pulse-user=PULSE_USER
                                user id for connecting to PulseGuardian
          --pulse-password=PULSE_PASSWORD
                                password for connecting to PulseGuardian
          --enable-unittests    Enable running unittests by downloading and installing
                                the unittests package for each build
          --cache-dir=CACHE_DIR
                                Use the specified directory as the build cache
                                directory; defaults to builds.
          --override-build-dir=OVERRIDE_BUILD_DIR
                                Use the specified directory as the current build cache
                                directory without attempting to download a build or
                                test package.
          --repo=REPOS          The repos to test. One of mozilla-central, mozilla-
                                inbound, mozilla-aurora, mozilla-beta, fx-team, b2g-
                                inbound, try. To specify multiple repos, specify them
                                with additional --repo options. Defaults to mozilla-
                                central.
          --buildtype=BUILDTYPES
                                The build types to test. One of opt or debug. To
                                specify multiple build types, specify them with
                                additional --buildtype options. Defaults to opt.
          --lifo                Process jobs in LIFO order. Default of False
                                implies FIFO order.
          --build-cache-port=BUILD_CACHE_PORT
                                Port for build-cache server. If you are running
                                multiple instances of autophone, this will have to be
                                different in each. Defaults to 28008.
          --devices=DEVICESCFG  Devices configuration ini file.
                                Each device is listed by name in the sections of the
                                ini file.
          --config=AUTOPHONECFG
                                Optional autophone.py configuration ini file.
                                The values of the settings in the ini file override
                                any settings set on the command line.
                                autophone.ini.example contains all of the currently
                                available settings.
          --verbose             Include output from ADBDevice command_output and
                                shell_output commands when loglevel is DEBUG. Defaults
                                to False.
          --treeherder-url=TREEHERDER_URL
                                Url of the treeherder server where test results are
                                reported.                       Defaults to None.
          --treeherder-credentials-path=TREEHERDER_CREDENTIALS_PATH
                                Path to credentials.json file containing OAuth
                                credentials for contacting the Treeherder server.
                                Defaults to None. If specified, --treeherder-url
                                must also be specified.
          --treeherder-retries=TREEHERDER_RETRIES
                                Number of attempts for sending data to
                                Treeherder. Defaults to 3.
          --treeherder-retry-wait=TREEHERDER_RETRY_WAIT
                                Number of seconds to wait between attempts
                                to send data to Treeherder. Defaults to 300.
          --s3-upload-bucket=S3_UPLOAD_BUCKET
                                AWS S3 bucket name used to store logs.
                                Defaults to None. If specified, --aws-access-key-id
                                and --aws-secret-access-key must also be specified.
          --aws-access-key-id=AWS_ACCESS_KEY_ID
                                AWS Access Key ID used to access AWS S3.
                                Defaults to None. If specified, --s3-upload-bucket
                                and --aws-secret-access-key must also be specified.
          --aws-access-key=AWS_ACCESS_KEY
                                AWS Access Key used to access AWS S3.
                                Defaults to None. If specified, --s3-upload-bucket
                                and --aws-secret-access-key-id must also be specified.

##### Configuring Email notifications

If you want to get notifications indicating when Autophone has
disabled a device due to errors, you can create email.ini:

    [report]
    from = <from address>

    [email]
    dest = <comma delimited list of email addresses>
    server = <SMTP server>
    port = <SMTP server port, defaults to 465>
    ssl = <enable SMTP over SSL, defaults to true>
    username = <username for SMTP, optional>
    password = <password for SMTP, optional>

Then invoke `autophone.py` with the option `--emailcfg=email.ini` to
get automatic email notifications.

#### trigger_runs.py command line options

Once Autophone has started, `trigger_runs.py` can be used to trigger
tests for specific builds.

        Usage: trigger_runs.py [options] <datetime, date/datetime, or date/datetime range>
        Triggers one or more test runs.

        The argument(s) should be one of the following:
        - a build ID, e.g. 20120403063158
        - a date/datetime, e.g. 2012-04-03 or 2012-04-03T06:31:58
        - a date/datetime range, e.g. 2012-04-03T06:31:58 2012-04-05
        - the string "latest"

        If a build ID is given, a test run is initiated for that, and only that,
        particular build.

        If a single date or datetime is given, test runs are initiated for all builds
        with build IDs between the given date/datetime and now.

        If a date/datetime range is given, test runs are initiated for all builds
        with build IDs in the given range.

        If "latest" is given, test runs are initiated for the most recent build.

        Options:
          -h, --help            show this help message and exit
          -i IP, --ip=IP        IP address of autophone controller; defaults to
                                localhost
          -p PORT, --port=PORT  port of autophone controller; defaults to 28001
          -b BUILD_LOCATION, --build-location=BUILD_LOCATION
                                build location to search for builds, defaults to
                                nightly; can be "tinderbox" or "inboundarchive" for
                                both m-c and m-i
          --logfile=LOGFILE     Log file to store build system logs, defaults to
                                autophone.log
          --loglevel=LOGLEVEL_NAME
                                Log level - ERROR, WARNING, DEBUG, or INFO, defaults
                                to INFO
          --repo=REPOS          The repos to test. One of mozilla-central, mozilla-
                                inbound, mozilla-aurora, mozilla-beta, fx-team, b2g-
                                inbound, try. To specify multiple repos, specify them
                                with additional --repo options. Defaults to mozilla-
                                central.
          --buildtype=BUILDTYPES
                                The build types to test. One of opt or debug. To
                                specify multiple build types, specify them with
                                additional --buildtype options. Defaults to opt.
          --first-revision=FIRST_REVISION
                                revision of first build; must match a build; last
                                revision must also be specified; can not be used with
                                date arguments.
          --last-revision=LAST_REVISION
                                revision of second build; must match a build; first
                                revision must also be specified; can not be used with
                                date arguments.
          --build-url=BUILD_URL
                                url of build to test; may be an http or file schema;
                                --repo must be specified if the build was not built
                                from the mozilla-central repository.
          --test=TESTS          Test to be executed by the job.  Defaults to all if
                                not specified. Can be specified multiple times. See
                                Android-Only Unittest Suites at
                                http://trychooser.pub.build.mozilla.org/ for supported
                                test identifiers.
          --device=DEVICES      Device on which to run the job.  Defaults to all if
                                not specified. Can be specified multiple times.
