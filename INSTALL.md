Installation Instructions
=========================

There are three separate components in a complete autophone system:

- one or more servers running the autophone app
- mobile devices with root access running the SUT agent
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

Autophone doesn't have a setup.py script, but "pip install -r requirements.txt"
will install all prerequisite packages.

Autophone is packaged with two tests: s1s2 and unittests.

s1s2
----
s1s2 measures fennec load times for web pages,
served both remotely and from a local file.

Put the pages to be served into autophone/files. You will need a way to
serve them (FIXME: autophone should do this). If you're using phonedash,
it can serve the files by just dropping them into phonedash/html/.

The s1s2 test is configured in the file configs/s1s2_settings.ini:

    [paths]
    # By default, Autophone will attempt to copy the test files
    # from the autophone/files directory. If you wish to customize
    # the location, you can use the source option to specify the
    # the path as either a local or absolute path.
    #source = files/

    # By default, Autophone will places the test files in
    # <testroot>/tests/autophone/s1test/ directory. You can customize
    # the location for all devices by setting the dest option.
    #dest = /mnt/sdcard/tests/autophone/s1test/

    # By default, Autophone will place Fennec's profile in
    # /data/local/tmp/profile. You can customize the location
    # for all devices by setting the profile option.
    #profile = /data/local/tmp/profile

    # The remote option specifies the web server where the devices
    # will retrieve the test files for the remote tests. It must
    # be specified and be reachable from the devices.
    remote = http://autophone.host/

    [tests]
    # List each testname as the option name and its corresponding
    # file as the option value. Remote and Local versions of the
    # tests will be created and displayed in the phonedash UI.
    blank = blank.html
    twitter = twitter.html

    [settings]
    # Autophone submits results to the server specified in the
    # resulturl option. By default, this is set to
    # http://phonedash.mozilla.org/api/s1s2/. You may customize
    # by setting the resulturl option.
    #resulturl = http://phonedash.mozilla.org/api/s1s2/

    # Autophone will load each test page the number of times
    # specified by the iterations option.
    iterations = 8

    [signature]
    # Set the id and key options to match those used on the
    # phonedash webserver where results will be submitted.
    id = foo
    key = bar

The resulturl is the URL used to POST results to the database.

unittests
---------

Autophone requires additional Python packages in order to run the unittests:

* logparser  - http://hg.mozilla.org/automation/logparser
* mozautolog - http://hg.mozilla.org/users/jgriffin_mozilla.com/mozautolog/

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

Each device must be rooted and have the SUT agent installed. See
https://wiki.mozilla.org/Auto-tools/Projects/SUTAgent for details
on the SUT agent.

After installing the SUT agent on all the phones, you need to configure
them. For each device, you will have to edit SUTAgent.ini appropriately to
configure how it connects to the registration server, setting "POOL" to the
phone's serial number (you can see the serial numbers of all connected devices
via "adb devices"), "IPAddr" to the external IP of the machine running
autophone (this is the SUT "registration server"), and "HARDWARE" to some short
descriptive string, e.g. "samsung_gs2" or "droid_pro".

You may also need to add a "Network Settings" section if you wish to configure
your device to connect to a specific network on startup.

An example SUTAgent.ini file might be:

    [Registration Server]
    IPAddr = 192.168.1.124
    PORT = 28001
    HARDWARE = lg_g2x
    POOL = 033c20444240b197
    
    [Network Settings]
    SSID = Mozilla Ateam
    AUTH = open
    ENCR = disabled
    KEY = auto
    ADHOC = 0

Run "python publishAgentIni.py -i <phone ip>" to push the SUTAgent.ini file
to the phone found at that IP.

### Setting up phonedash ###

Phonedash is a templeton-based app (https://github.com/markrcote/templeton).
It stores results in either a MySQL or a sqlite database.

Database configuration goes into server/settings.cfg. The file has one
section, [database], with the following options:

- SQL_TYPE: can be set to either "mysql" (default) or "sqlite".
- SQL_DB: database name (MySQL) or path (sqlite)
- SQL_SERVER: MySQL database server IP or hostname
- SQL_USER: MySQL username
- SQL_PASSWD: MySQL password

When phonedash is started, it will create the requisite table if not found.
