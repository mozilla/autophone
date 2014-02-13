Using Autophone
===============

Starting Autophone
------------------

Autophone has a number of command-line options. Run "python autophone.py -h"
to see them. Some important ones are

--test-path <testpath>: Specifies the test manifest which will load the
                        appropriate phone test. Autophone will use
                        tests/manifest.ini by default.

--enable-unittests: Tells Autophone to download the appropriate tests.zip
                    for each build to use when running the unittests. This
                    is required when running the unittests.

--restarting: By default Autophone starts with no knowledge of any devices.
              It creates a local cache as devices register with it. This
              option preserves that cache when restarting.
              (FIXME: This should probably be the default behaviour.)

--no-reboot: Use with --restarting to prevent Autophone from rebooting
             all known devices while starting up.

--ipaddr: Autophone tries to determine an external IP address on the host
          machine. This option can override this address, or provide one
          if Autophone can't find one. (FIXME: This may no longer be
          needed?)

--logfile: Log main Autophone system messages to this file. Defaults to
           autophone.log. Devices will log to their own files in the
           format <logfile base>-<phone id>.<logfile extension>, e.g.
           autophone-a8_26_d9_93_fe_4b_nexus_one.log.

--loglevel: Log messages at or above this level. Can be set to
            DEBUG (default), INFO, WARNING, or ERROR.

--config <configfile>: Use the specified configuration file to set various
                       options. The values set in the config file override
                       options set on the command line.

                       Settings for command line options:

                       clear_cache
                       ipaddr
                       port
                       cachefile
                       logfile
                       loglevel
                       test_path
                       emailcfg
                       enable_pulse
                       enable_unittests
                       cache_dir
                       override_build_dir
                       repos
                       buildtypes
                       build_cache_port

                       Settings for internal parameters:

                       build_cache_size
                       build_cache_expires
                       devicemanager_retry_limit
                       devicemanager_settling_time
                       phone_retry_limit
                       phone_retry_wait
                       phone_max_reboots
                       phone_ping_interval
                       phone_command_queue_timeout
                       phone_command_queue_timeout
                       phone_crash_window
                       phone_crash_limit

Running Unit Tests
------------------

Autophone can run individual unit tests such as robocop, reftests,
crashtests, jsreftests or mochitests for each build or it can run combinations of
them.

Before running the unit tests, you will need to copy
configs/unittest_defaults.ini.example to configs/unittest_defaults.ini
and edit configs/unittest_defaults.ini to change the xre_path,
utility_path, and minidump_stackwalk values. If you wish to use a
development version of ElasticSearch or Autolog, you will need to edit
the es_server and rest_server values as well.

You can switch from using the experimental 'new' logparser and
logparser by changing the use_newparser value to False.

For example,

to run only the robocop tests:

python autophone.py --enable-unittests --test-path=./tests/robocoptests_manifest.ini

to run all of the unit tests specified in the configs/unittests_settings.ini file:

python autophone.py --enable-unittests --test-path=./tests/unittests_manifest.ini


Running Tests
-------------

Autophone listens to pulse for new fennec builds. When one is detected,
the main Autophone process downloads it and notifies all of its workers,
which install it on the phones and begin a test run. If a test run is
ongoing, the new job is queued.

You can also trigger test runs on past builds with trigger_runs.py. This
script takes a build ID or date/time range and finds the appropriate builds.
Run "python trigger_runs.py -h" for exact usage.
