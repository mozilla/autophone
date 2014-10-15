Installation Instructions
=========================

There are three separate components in a complete autophone system:

- one or more servers running the autophone app
- mobile devices with root access
- a server running phonedash to collect, serve, and present the results

Multiple autophone servers can run on the same machine. Until mozpool support
is added, each server runs independently with no knowledge of the others and
should be configured with individual device pools. If running two or more
instances from the same installation, you will need to use the --cache-dir
option on all but the primary to avoid cache contention.

The phonedash server is optional, e.g. for development environments. It can
be found at https://github.com/markrcote/phonedash/. It is customized for
the s1s2 test and will be eventually deprecated in favour of DataZilla.


Setting up autophone
--------------------

"pip install -r requirements.txt" will install all required packages.

Autophone is packaged with two tests: s1s2 and unittests.

s1s2
----
s1s2 measures fennec load times for web pages,
served both remotely and from a local file.

The pages to be served are located in autophone/files/base/ and
autophone/files/s1s2/. The twitter test pages are retrieved from
https://git.mozilla.org/?p=automation/ep1.git;a=summary and placed in
autophone/files/ep1/twitter.com via

git submodule update --init

You will need a way to serve them (FIXME: autophone should do
this). If you're using phonedash, it can serve the files by just
dropping them into phonedash/html/.

The s1s2 test is configured in the file configs/s1s2_settings.ini -- copy
configs/s1s2_settings.ini.example and customize as needed.

unittests
---------

The unittests also require a local installation of the XRE and the utility
programs such as xpcshell. A local build of Firefox can be used.

In order to process crash minidumps, you will also need a local
installation of breakpad's minidump_stackwalk. You can build
minidump_stack via:

svn checkout http://google-breakpad.googlecode.com/svn/trunk/ google-breakpad-read-only
cd google-breakpad-read-only
if [[ $(uname) == "Darwin" ]]; then
    CC=clang CXX=clang++ ./configure
else
   CXXFLAGS="-g -O1" ./configure
fi
make
sudo make install

If you wish to run a development environment, you will also need to
set up an ElasticSearch and Autolog server. Note that you do not need
to set up test data using testdata.py but you will need to create an
autolog index using curl -XPUT 'http://localhost:9200/autolog/'
once the ElasticSearch server is up and running and before
you start the Autolog server.

See
https://wiki.mozilla.org/Auto-tools/Projects/Autolog for more details.

To configure Autolog to display the results for a device, you will
need to update the OSNames property in js/Config.js in Autolog. See
http://hg.mozilla.org/automation/autolog/file/2a32ea0367f5/js/Config.js#l67 .
Note that the key for the device should consist of the string 'autophone-'
followed by the same value as used in SUTAgent.ini's HARDWARE property for
the device.

Once you have the XRE, utility programs and minidump_stack installed, change
configs/unittest_default.ini to point to your local environment.

Email notifications
-------------------

If you want to get notifications indicating when Autophone has disabled
a device due to errors, you can create email.ini like so:

    [report]
    from = <from address>

    [email]
    dest = <list of to addresses>
    server = <SMTP server>
    port = <SMTP server port, defaults to 465>
    ssl = <enable SMTP over SSL, defaults to true>
    username = <username for SMTP, optional>
    password = <password for SMTP, optional>

### Setting up devices ###

Each device must be rooted and reachable by adb. Check that "adb devices"
shows each desired device.

### Using an emulator ###

If desired, the Android emulator can be used in place of a physical device.

Set up an emulator AVD and verify that Firefox for Android can be run in
the resulting emulator instance.

These emulator device settings are recommended:
 - Target: API Level 15 or greater
 - RAM: 2048
 - Internal Storage: 200 MiB
 - SD Card: Size: 200 MiB
 - Use Host GPU

Be sure to select "Use Host GPU". A modern host computer and graphics card
may be required.

Also verify that the emulator can be reached by adb. Check the output of
"adb devices" and enter the emulator name as the serialno in devices.ini:

[test-emulator]
serialno=emulator-5554

The Android emulator normally reports a battery charge of 50%. With default
settings, autophone will wait indefinitely for the battery to charge. To
avoid this, edit autophone.ini and change device_battery_min to be less
than the emulator battery charge (eg. device_battery_min=40).

Also be aware that instead of rebooting the device, autophone will kill
and restart the emulator.

### Setting up phonedash ###

Set up Phonedash following the instructions at
https://github.com/markrcote/phonedash.


Now you are ready to use autophone -- see USAGE.md to get started.
