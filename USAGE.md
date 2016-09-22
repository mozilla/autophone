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

        python autophone.py --devices devices.ini --test-path tests/smoketest.ini

* [Run WebappStartupTest with default options](#configuring-webappstartuptest)

        python autophone.py --devices devices.ini --test-path tests/webappstartup.ini

* [Run S1S2Test with default options](#configuring-s1s2test)

        python autophone.py --devices devices.ini

        python autophone.py --devices devices.ini --test-path tests/s1s2-blank-local.ini

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

### Credentials configuration file

  Autophone can contact several systems in order to report its
  results. The credentials configuration file is a means of specifying
  the credentials in a central location.

  The credentials file has one section named `settings` and may
  contain the following options:

<pre>
[settings]
phonedash_user = XXXX
phonedash_password = XXXX
pulse_user = XXXX
pulse_password = XXXX
treeherder_credentials_path = XXXX
s3_upload_bucket=XXXX
aws_access_key_id=XXXX
aws_access_key=XXXX
</pre>

  * phonedash_user

      The jot id to be used to post results to the phonedash server.

  * phonedash_password

      The jot key to be used to post results to the phonedash server.

  * pulse_user

      The user id used to connect to PulseGuardian.

  * pulse_password

      The password used to connect to PulseGuardian.

  * treeherder_credentials_path

      The path to json file containing Treeherder credentials.

  * s3_upload_bucket

      The AWS S3 bucket name used to store logs.

  * aws_access_key_id

      The AWS Access Key ID used to access AWS S3.

  * aws_access_key

      The AWS Access Key used to access AWS S3.

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
    config = ../configs/exampletest-settings.ini
    devicename = mozilla-central mozilla-inbound

If you wish to use different config options for different devices on
the same test, you can include multiple sections for the test by
specifing a suffix in the test's section name separated from the
script name by a space. For example to specify different test
configurations for Android 2.3 and Android 4.x you might specify

    [exampletest.py android2.3]
    config = ../configs/exampletest-android23-settings.ini
    devicename1 = mozilla-central mozilla-inbound

    [exampletest.py android4.0]
    config = ../configs/exampletest-android40-settings.ini
    devicename2 = mozilla-central mozilla-inbound

Note that you must ensure that a test script is not invoked multiple
times for a device using the same config file. If a test script for a
device is created multiple times using the same config file, an
AssertionError: Duplicate PhoneTest will stop Autophone.

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
          --treeherder-retry-wait=TREEHERDER_RETRY_WAIT

See the Treeherder-related options in
[Autophone Command Line Options](#autophone-command-line-options) for more details.

#### Configuring Smoketest

Smoketest is used to test if Fennec is able to run on devices and
if it is possible to detect Throbber start and stop values in the
logcat output. It is implemented by
[tests/smoketest.py](tests/smoketest.py).

The smoketest manifest file
[tests/smoketest.ini](tests/smoketest.ini) is the
simplest example which we can use to illustrate the different
components of a test manifest file. The Smoketest manifest file is
`tests/smoketest-settings.ini` which consists of a single section for
the smoketest:

    [smoketest.py]

If we wished the smoketest to send its results to Treeherder, we could
copy
[`configs/smoketest-settings.ini.example`](configs/smoketest-settings.ini.example)
to `configs/smoketest-settings.ini`. It already contains the settings
for Treeherder:

    [treeherder]
    job_name = Autophone Smoketest
    job_symbol = s
    group_name = Autophone
    group_symbol = A

Then add a config option to the test manifest file
`tests/smoketest-settings.ini` pointing to the test configuration
settings file.

    [smoketest.py]
    config = ../configs/smoketest-settings.ini

If we wished to only test mozilla central builds on the nexus-one-1
device and mozilla-inbound builds on the nexus-one-2 device, we would
add a device option for each device as in:

    [smoketest.py]
    config = ../configs/smoketest-settings.ini
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

#### Configuring WebappStartupTest

WebappStartupTest installs webapp-startup-test.apk, launches the app,
then measures Web app start times by detecting `WEBAPP STARTUP
COMPLETE` messages in logcat. It is implemented by
[tests/webappstartup.py](tests/webappstartup.py).

WebappStartupTest is a Performance test and shares the
[Performance test configuration](#configuring-performance-tests)
options. To create your `configs/webappstartup-settings.ini` file,
start by copying
[`configs/webappstartup-settings.ini.example`](webappstartup-settings.ini.example)
to `configs/webappstartup-settings.ini` then customize the settings to
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
options. To create your `configs/s1s2-settings.ini` file, start by
copying
[`configs/s1s2-settings.ini.example`](configs/s1s2-settings.ini.example)
to `configs/s1s2-settings.ini` then customize the settings to fit your
needs (or use one of the existing `configs/s1s2-*.ini` files).

Note that it is possible to also add per device options to the s1s2test.py section in the
same fashion as we did for the Smoketest.

#### Configuring UnitTests

Autophone can run individual unit tests such as robocop, reftests,
crashtests, jsreftests or mochitests for each build or it can run combinations of
them.

Before running the unit tests, you will need to copy
configs/unittest-defaults.ini.example to configs/unittest-defaults.ini
and edit configs/unittest-defaults.ini to change the xre_path and
utility_path values.

For example,

to run only the robocop tests:

   python autophone.py --devices=devices.ini --test-path=./tests/robocoptests.ini

Note that if you are planning to run the Autophone Robocop Autophone
test in configs/robocoptests-autophone-settings.ini, you will need to
obtain the apk [installer files for
Flash](https://helpx.adobe.com/flash-player/kb/archived-flash-player-versions.html)
(install_flash_player_ics.apk, install_flash_player_pre_ics.apk) and
copy them into the apk directory.

#### Configuring Multiple tests simultaneously

It is possible to combine multiple tests into a single Test manifest
file in order to execute more than one type of test per Autophone
instance. Configuration is identical to configuring the individual
test manifests as described above with a separate section for each
test. For example:

##### haxxor-virginia-manifest.ini

        [smoketest.py]
        config = ../configs/smoketest-settings.ini
        nexus-one-1=mozilla-inbound
        nexus-one-2=mozilla-central
        nexus-one-3=fx-team
        samsung-gs2-1=fx-team
        samsung-gs3-1=mozilla-inbound
        samsung-gs3-2=mozilla-inbound

        [s1s2test.py]
        config = ../configs/s1s2-blank-local.ini
        nexus-one-1=mozilla-inbound
        nexus-one-2=mozilla-central
        nexus-one-3=fx-team
        samsung-gs2-1=fx-team
        samsung-gs3-1=mozilla-inbound
        samsung-gs3-2=mozilla-inbound

        [webappstartup.py]
        config = ../configs/webappstartup-settings.ini
        nexus-one-1=mozilla-inbound
        nexus-one-2=mozilla-central
        nexus-one-3=fx-team
        samsung-gs2-1=fx-team
        samsung-gs3-1=mozilla-inbound
        samsung-gs3-2=mozilla-inbound

        [runtestsremote.py]
        config = ../configs/crashtests-settings.ini ../configs/jsreftests-settings.ini ../configs/mochitests-settings.ini ../configs/mochitests-skia-settings.ini ../configs/reftests-settings.ini ../configs/robocoptests-settings.ini
        unittests = 1

### Autophone Command Line Options

Autophone supports a large number of options. Since this can be
unwieldy, one of the options it supports is `--config=AUTOPHONECFG`
which can be used to store your preferred command line options in a
file to be easily reused. To set up your own Autophone configuration
file, copy [`autophone.ini.example`](autophone.ini.example) to
`autophone.ini` and edit it to suit your needs.

Note that if you wish to use Autophone as a service with the
--reboot-on-error command line option, the user that autophone.py runs
under must be able to execute sudo reboot without requiring a
password.

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
          --phonedash-url=PHONEDASH_URL
                                Url to Phonedash server. If not set, results for each
                                device will be written to comma delimited files in the
                                form: autophone-results-<deviceid>.csv.
          --phonedash-user=PHONEDASH_USER
                                user id for connecting to Phonedash server
          --phonedash-password=PHONEDASH_PASSWORD
                                password for connecting to Phonedash server
          --webserver-url=WEBSERVER_URL
                                Url to web server for remote tests.
          --enable-pulse        Enable connecting to Pulse to look for new builds. If
                                specified, --pulse-user and --pulse-password must also
                                be specified.
          --pulse-durable-queue
                                Use a durable queue when connecting to Pulse.
          --pulse-user=PULSE_USER
                                user id for connecting to PulseGuardian
          --pulse-password=PULSE_PASSWORD
                                password for connecting to PulseGuardian
          --pulse-jobactions-exchange=PULSE_JOBACTIONS_EXCHANGE
                                Exchange for Pulse Job Actions queue; defaults to
                                exchange/treeherder/v1/job-actions.
          --cache-dir=CACHE_DIR
                                Use the specified directory as the build cache
                                directory; defaults to builds.
          --override-build-dir=OVERRIDE_BUILD_DIR
                                Use the specified directory as the current build cache
                                directory without attempting to download a build or
                                test package.
          --allow-duplicate-jobs
                                Allow duplicate jobs to be queued. This is useful when
                                testing intermittent failures. Defaults to False.
          --repo=REPOS          The repos to test. One of b2g-inbound, fx-team,
                                mozilla-aurora, mozilla-beta, mozilla-central,
                                mozilla-inbound, mozilla-release, try. To specify
                                multiple repos, specify them with additional --repo
                                options. Defaults to mozilla-central.
          --buildtype=BUILDTYPES
                                The build types to test. One of opt or debug. To
                                specify multiple build types, specify them with
                                additional --buildtype options. Defaults to opt.
          --lifo                Process jobs in LIFO order. Default of False implies
                                FIFO order.
          --build-cache-port=BUILD_CACHE_PORT
                                Port for build-cache server. If you are running
                                multiple instances of autophone, this will have to be
                                different in each. Defaults to 28008.
          --devices=DEVICESCFG  Devices configuration ini file. Each device is listed
                                by name in the sections of the ini file.
          --config=AUTOPHONECFG
                                Optional autophone.py configuration ini file. The
                                values of the settings in the ini file override any
                                settings set on the command line.
                                autophone.ini.example contains all of the currently
                                available settings.
          --credentials-file=CREDENTIALS_FILE
                                Optional autophone.py configuration ini file which is
                                to be loaded in addition to that specified by the
                                --config option. It is intended to contain sensitive
                                options such as credentials which should not be
                                checked into the source repository. The values of the
                                settings in the ini file override any settings set on
                                the command line. autophone.ini.example contains all
                                of the currently available settings.
          --verbose             Include output from ADBDevice command_output and
                                shell_output commands when loglevel is DEBUG. Defaults
                                to False.
          --treeherder-url=TREEHERDER_URL
                                Url of the treeherder server where test results are
                                reported. If specified, --treeherder-client-id and
                                --treeherder-secret must also be specified. Defaults
                                to None.
          --treeherder-client-id=TREEHERDER_CLIENT_ID
                                Treeherder client id. If specified, --treeherder-url
                                and --treeherder-secret must also be specified.
                                Defaults to None.
          --treeherder-secret=TREEHERDER_SECRET
                                Treeherder secret. If specified, --treeherder-url and
                                --treeherder-client-id must also be specified.
                                Defaults to None.
          --treeherder-tier=TREEHERDER_TIER
                                Integer specifying Treeherder Job Tier. Defaults to 3.
          --treeherder-retry-wait=TREEHERDER_RETRY_WAIT
                                Number of seconds to wait between attempts to send
                                data to Treeherder. Defaults to 300.
          --s3-upload-bucket=S3_UPLOAD_BUCKET
                                AWS S3 bucket name used to store logs. Defaults to
                                None. If specified, --aws-access-key-id and --aws-
                                secret-access-key must also be specified.
          --aws-access-key-id=AWS_ACCESS_KEY_ID
                                AWS Access Key ID used to access AWS S3. Defaults to
                                None. If specified, --s3-upload-bucket and --aws-
                                secret-access-key must also be specified.
          --aws-access-key=AWS_ACCESS_KEY
                                AWS Access Key used to access AWS S3. Defaults to
                                None. If specified, --s3-upload-bucket and --aws-
                                secret-access-key-id must also be specified.
          --reboot-on-error     Reboot host in the event of an unrecoverable
                                error.Defaults to False.
          --maximum-heartbeat=MAXIMUM_HEARTBEAT
                                Maximum heartbeat in seconds before worker is
                                considered to be hung. Defaults to 900.
          --usbwatchdog-appname=USBWATCHDOG_APPNAME
                                Name of USBWatchdog. Defaults to
                                com.mozilla.autophone.usbwatchdog.
          --usbwatchdog-poll-interval=USBWATCHDOG_POLL_INTERVAL
                                USBWatchdog poll interval in seconds. Defaults to
                                1800.
          --device-test-root=DEVICE_TEST_ROOT
                                Device directory to be used as the test root. Defaults
                                to an empty string which will defer selection of the
                                test root to ADBAndroid. Can be overridden via a
                                test_root option for a device in the devices.ini file.

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
                                nightly; or can be "tinderbox" for
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

## Autophone Administration

Production Autophone consists of 3 Ubuntu 16 servers:
autophone-{1,2,3}.qa.mtv2.mozilla.com which are located in the QA Lab
in Mountain View. You may find it convenient to create aliases for the
Autophone servers in your `/etc/hosts` file so that you can refer to
them simply as autophone-{1,2,3}.

Each server runs two services: autophone and phonedash which start
when the servers boot which can be controlled via the normal systemd
commands:

    sudo systemctl start autophone.service
    sudo systemctl stop autophone.service
    sudo systemctl status autophone.service

    sudo systemctl start phonedash.service
    sudo systemctl stop phonedash.service
    sudo systemctl status phonedash.service

While the Autophone service can be stopped using the normal systemd
command, the preferred method of shutting down Autophone is to tell it
to shut down cleanly using either the
[Autophone autophone-shutdown command message](#autophone-command-help)
or the more user friendly [ap-shutdown](#ap-shutdown) script. This
allows the devices which are currently running tests to complete them
before stopping the Autophone process.

[Scripts](#administrative-scripts) which can be used to administer an
Autophone instance are located in the Autophone source directory. You
can run these scripts against the production Autophone servers from a
local installation of Autophone. They will "dispatch" the commands to
the remote servers as needed. In order to for this to work you will
need to add the following to the `~/.profile` file in your account.

    export AUTOPHONE_PATH=<path to autophone source directory>
    export ACTIVATE_AUTOPHONE=<path to autophone virtual environment activate script>
    export PATH=$AUTOPHONE_PATH:$PATH

On the production servers, these values will be

    export AUTOPHONE_PATH=/mozilla/autophone/autophone
    export ACTIVATE_AUTOPHONE=mozilla/autophone/venv/bin/activate
    export PATH=$AUTOPHONE_PATH:$PATH


### Deploying Code Changes

If the test manifests are changed, you must wait for all jobs to be
completed before deploying the change. You can display a report on the
pending jobs on the various Autophone servers via:

    ap-jobs autophone-{1,2,3}

Once there are no more pending jobs, you can shut down the Autophone
processes via:

    ap-shutdown autophone-{1,2,3}

You can check on the status of the Autophone processes via:

    ap-status autophone-{1,2,3}

If the Autophone processes have stopped, you will see the following
message for the server:

    Ncat: Connection refused.

You can alternatively get the console messages for the server via:

    ap-console autophone-{1,2,3}

Once the server has shut down, the consoles will show a message
similar to the following at the end of the log:

    2016-09-22 08:31:34,370|23990|Thread-3|console|INFO|Shutting down Autophone...

Once the Autophone processes are shut down on each server, you can ssh
to the server, change to the autophone directory and pull the changes
from the github repository. The servers, code and adb debugger
connections were deployed using user bclary. Until we can get on-site
time to reauthorise the adb connections under the autophone user, we
must stay with the original user. Therefore you must deploy the code
changes as the bclary user.

    ssh autophone-1
    sudo su bclary
    cd /mozilla/autophone/autophone
    git pull

If there are no operating system updates, you can simply start the
service again via:

    sudo systemctl start autophone.service

However, if there are pending updates to the system packages, you
should update them then reboot the server to make sure all in use
packages are updated.

    sudo aptitude safe-upgrade
    sudo reboot

### Adding a device which failed to initialize

When an Autophone server detects that one of its connected Android
devices has disconnected and is no longer visible via `adb`, it will
reboot in the hope that the device will become visible after the
reboot. If the device remains disconnected after the server reboots
and Autophone completes starting, you will need to file an Autophone
bug asking for someone in Mountain View to reconnect the USB cable or
otherwise intervene with the device. Make your bug block the
[autophone-devices](https://bugzilla.mozilla.org/show_bug.cgi?id=autophone-devices)
bug. You can find recent examples as dependencies of that bug and
clone them. Be sure to NEEDINFO van, stephend, and uber and ask one of
them to intervene.

Once they have reconnected the device, you can check on its status via
the `ap-getstate` script which lists the connected devices for an
Autophone server. For example, if nexus-6p-1 connected to autophone-3
had disconnected you could check if the intervention succeeded via

ap-getstate autophone-3

You should see it in the output:

    nexus-4-4=device
    nexus-4-7=device
    nexus-5-2=device
    nexus-6-2=device
    nexus-6p-1=device
    nexus-6p-2=device
    nexus-6p-3=device
    nexus-6p-4=device
    nexus-6p-5=device
    nexus-6p-8=device
    nexus-9-2=device

Once it is reconnected, you can add it to the already running Autophone instance via:

    ap-add-device -s autophone-3 -d nexus-6p-1

### Autophone Command Server

Autophone listens by default on port 28001 for commands. You can send
commands to Autophone using the the program ncat/nc:

echo command | nc server port

where command is one of the [supported Autophone commands](#autophone-command-help).

On Fedora ncat can be installed via `sudo yum install nmap-ncat`.
On Ubuntu it can be installed via `sudo apt install nmap`.

Many but not all of these commands are also implemented in helper
scripts in [Administrative Scripts](#administrative-scripts). If a
helper script is not available, you can manually invoke one of the
following commands using [ap](#ap).

#### Autophone command help:

        autophone-help
            Generate this message.

        autophone-add-device <name> <serialno>

            Adds a new device to the active workers.

        autophone-restart
            Shutdown each worker after its current test, then restart
            autophone.

        autophone-shutdown
            Shutdown each worker after its current test, then
            shutdown autophone.

        autophone-status
            Generate a status report for each device.

        autophone-stop
            Immediately stop autophone and all worker processes; may be
            delayed by pending download.

        device-disable <device>
           Disable the device's worker and cancel its pending jobs.

        device-enable <device>
           Enable a disabled device's worker.

        device-is-alive <device>
           Check if the device's worker process is alive, report to log.

        device-ping <device>
           Issue a ping command to the device's worker which checks the sdcard
           availability.

        device-reboot  <device>
           Reboot the device.

        device-restart <device>
           Shutdown the device's worker process after the current test, then
           restart the worker picking up test manifest and test configuration
           changes.

        device-status <device>
           Generate a status report for the device's worker.

        device-shutdown  <device>
           Shutdown the device's worker process after the current test. The
           device's worker process will not be restarted and will be removed
           from the active list of workers.

        device-stop <device>

           Immediately stop the device's worker process and remove it from the
           list of active workers.

### Administrative Scripts

#### ap

ap is used to send commands to an autophone instance. Without any
arguments, ap will send autophone-status to the local autophone
instance.

    $ ap -h
    usage: ./ap [-s server] [-p port] [command]

    optional arguments:
      -h
                 show this help message
      -s server
                 autophone server to contact. (default: localhost)
      -p port
                 autophone server port to contact (default: 28001)
      command
                 command. (default: autophone-status)
                 Use autophone-help to get help on autophone commands.

#### ap-add-device

ap-add-device adds a device to an already running autophone instance.

    $ ap-add-device -h
    usage: ap-add-device [-h] [-s server] [-d device]

    optional arguments:
      -h
                 show this help message
      -s server
                 autophone server to contact. (default: localhost)
      -p port
                 autophone server port to contact (default: 28001)
      -d device
                 device name to add

#### ap-assignments

ap-assignments reads the production manifests and produces several reports showing
the assignments of devices to repositories, to build types and to tests.

`ap-assignments` is a shell wrapper around the python script
`ap-assignments.py` which ensures the Autophone virtual environment is
set up prior to starting `ap-assignents.py`.

`ap-assignments` is helpful in determining if a test has sufficient
coverage or if a device will be able to complete its expected test
load in a timely fashion. You can add new tests, repositories, devices
and timing data by editing ap-assignments.py.


#### ap-battery

ap-battery reports on the battery status of the devices connected to
the specified hosts.

Usage:

    ap-battery [host1...]

#### ap-console

ap-console displays the console messages for the autophone process for
the specified hosts. If no host is specified, the local host is used.

Usage:

    ap-console [host1...]

#### ap-errors

ap-errors displays the error messages from the log for the autophone
process for the specified hosts. If no host is specified, the local
host is used.

Usage:

    ap-errors [host1...]

#### ap-getlogs

ap-getlogs downloads logs from either the production or staging
instance of Autophone's AWS storage into a directory hierarchy which
matches that found on AWS.

    $ ap-getlogs
    usage: ap-getlogs [-h] [-t [production|staging]] [-p prefix]

    optional arguments:
      -h
                 show this help message
      -s [production|staging]
                 Download logs from either production or staging AWS. (default: production)
      -p prefix
                 Path prefix used to select logs to download.

For example, to download the logs corresponding to the production run
https://treeherder.mozilla.org/#/jobs?repo=autoland&revision=6957fdb6a7128810bc9a8ba1f871eede183dd9f2
you would find an example log such as https://autophone.s3.amazonaws.com/pub/mobile/tinderbox-builds/autoland-android-api-15/1474488360/autophone-s1s2-s1s2-twitter-local.ini-1-nexus-4-5-e27764e7-8db1-4c65-9919-afdf32b09258-autophone.log

The prefix is that part of the url following the host name up to the
point where you wish to match. For example to download all logs for
this build, you would run:

    ap-getlogs -p pub/mobile/tinderbox-builds/autoland-android-api-15/1474488360/

#### ap-getstate

ap-getstate displays a list of device=serialno messages for the
android devices attached to the specified hosts. If no host is
specified, the local host is used.

Usage:

    ap-getstate [host1...]

#### ap-inventory

    $ ap-inventory --help
    usage: ap-inventory [-h] [--device-manifests DEVICE_MANIFEST_PATTERN]
                        [--match MATCH_RULES] [--output-format OUTPUT_FORMAT]

    Query device manifests.

    optional arguments:
      -h, --help            show this help message and exit
      --device-manifests DEVICE_MANIFEST_PATTERN
                            File glob pattern matching device manifests. (default:
                            *-devices.ini)
      --match MATCH_RULES   Match devices by attribute=regexp. (default: match all
                            devices)
      --output-format OUTPUT_FORMAT
                            Output format. raw - output python dict, attribute
                            format string.

#### ap-jobs

ap-jobs generates a report of the pending jobs, tests and treeherder
submissions for the specified hosts. If no host is specified, the
local host is used.

Usage:

    ap-jobs [host1...]

#### ap-pushes


    $ ap-pushes --help
    usage: ap-pushes [-h] [--date DATE] [--repo REPO]

    Count pushes to a repository on a date.

    optional arguments:
      -h, --help   show this help message and exit
      --date DATE  start date CCYY-MM-DD. (default: today's date).
      --repo REPO  repository. (default: mozilla-central)

`ap-pushes` is a shell wrapper around the python script
`ap-pushes.py` which ensures the Autophone virtual environment is
set up prior to starting `ap-pushes.py`.

#### ap-restart

ap-restart restarts the autophone process for the specified hosts. If
no host is specified, the local host is used.

Usage:

    ap-restart [host1...]

#### ap-shutdown

ap-shutdown cleanly shuts down the autophone process for the specified hosts. If
no host is specified, the local host is used.

Usage:

    ap-shutdown [host1...]

#### ap-status

ap-status generates a report of the autophone status for the specified
hosts. If no host is specified, the local host is used.

Usage:

    ap-status [host1...]

#### ap-wifi

ap-wifi reports on the status of the wifi connection for devices
connected to the specified hosts.

Usage:

    ap-wifi [host1...]
