Using Autophone
===============

Starting Autophone
------------------

Autophone has a number of command-line options. Run "python autophone.py -h"
to see them. Some important ones are

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


Running Tests
-------------

Autophone listens to pulse for new fennec builds. When one is detected,
the main Autophone process downloads it and notifies all of its workers,
which install it on the phones and begin a test run. If a test run is
ongoing, the new job is queued.

You can also trigger test runs on past builds with trigger_runs.py. This
script takes a build ID or date/time range and finds the appropriate builds.
Run "python trigger_runs.py -h" for exact usage.
