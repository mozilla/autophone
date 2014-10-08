# Installation Instructions

There are three separate components in a complete autophone system:

- one or more servers running the autophone app
- mobile devices with root access
- a server running phonedash to collect, serve, and present the results

Multiple autophone servers can run on the same machine. Until mozpool
support is added, each server runs independently with no knowledge of
the others and should be configured with individual device pools. If
running two or more instances from the same installation, you will
need to use the --port, --cache-dir, and --build-cache-port options on
all but the primary to avoid port and cache contentions.

The phonedash server is optional, e.g. for development environments. It can
be found at https://github.com/markrcote/phonedash/. It is customized for
the s1s2 test and will be eventually deprecated in favour of Treeherder.


## Setting up autophone

"pip install -r requirements.txt" will install all required packages.

Autophone is packaged with four tests: smoketest, S1S2Test,
WebappStartupTest and unittests.

## Smoketest

Smoketest is used to test if Fennec is able to run on devices and report
its Throbber values to logcat.

## S1S2Test

S1S2Test measures fennec load times for web pages,
served both remotely and from a local file.

The pages to be served are located in autophone/files/base/ and
autophone/files/s1s2/. The twitter test pages are retrieved from
https://git.mozilla.org/?p=automation/ep1.git;a=summary and placed in
autophone/files/ep1/twitter.com via

    git submodule update --init --remote

You will need a way to serve them (FIXME: autophone should do
this). If you're using phonedash, it can serve the files by just
dropping them into phonedash/html/.

The s1s2 test is configured in the file configs/s1s2_settings.ini -- copy
configs/s1s2_settings.ini.example and customize as needed.

## Unittests

To be updated as part of
[Bug 1079923 - Autophone - update unittests to fix bitrot](https://bugzilla.mozilla.org/show_bug.cgi?id=1079923).

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

Once you have the XRE, utility programs and minidump_stack installed, change
configs/unittest_default.ini to point to your local environment.

### Setting up devices ###

Each device must be rooted and reachable by adb. At a minimum, each
device should have USB Debugging and Unknown sources enabled.

Check that "adb devices" shows each desired device.

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

Now you are ready to use autophone -- see [USAGE.md](USAGE.md) to get started.
