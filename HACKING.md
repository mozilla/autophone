Hacking Autophone
=================

Problem
-------

Autophone is a platform for running automated tests on physical mobile devices--phones and tablets. Autophone is responsible for tracking, verifying, and recovering devices.

Goals & Considerations
----------------------

-   Verify that a phone is working correctly: sd card is writable and not full, etc.
-   Attempt to recover a phone that reports errors, rerunning the current test/test framework.
-   Provide at least a high-level status for all phones: whether they are idle, running a test, or disabled/broken.
-   Support a large number of phones, potentially split amongst several host machines.

Non-Goals
---------

-   Provide a particular test framework. Autophone should be generic enough to run any framework: mochitest, talos, reftest, etc.
-   Record and present test results. Frameworks are responsible for gathering and reporting results. In the long run this should be Perfherder where possible.

Dependencies / Who will use this
--------------------------------

Autophone will run tests on the mobile Firefox browser on real hardware with the intended audience being developers, primarily mobile developers. As this runs as a Tier 2 job, the sheriffs will not look after this as part of their daily jobs.

Design and Approach
-------------------

Autophone is a multithreaded, multiprocess system. The main process is the controller. It itself has one thread for a TCP listener for user commands and phone registrations, one process for a pulse listener, and one thread that listens for worker messages. It spawns and controls one worker subprocess for each phone.

Milestones and Dates
--------------------

Implementation
--------------

### Command Thread

This thread listens for TCP connections (default port 28001). There are two expected sources of connections: phones and users. The phones use the connection only for registration messages from the SUT agent when it first starts. Autophone maintains a cache (JSON file) of known phones (the --restarting option is required to maintain this cache between Autophone restarts). Any phone registering over the command port is added to the list of known phones.

There are also a number of user commands that can be issued over the command port:

-   status: describes the status of all known phones.
-   disable <serial num>: disables a phone. Any currently executing test run will complete, but no more will be started.
-   reenable <serial num>: attempts to reenable a phone after Autophone has disabled it. Use this if a phone required manual maintenance and is being added back to the pool.
-   trigger <file or path>: start a test run against the given build, which can be a path to a local file or a URL.
-   stop: stops autophone.

### Pulse Listener Thread

A dedicated thread listens to the pulse server via the pulsebuildmonitor Python package. When a new build is detected, the workers either start a new test run, if one isn't currently running, or they queue the request.

### Worker Status Thread

The worker status thread simply listens for updates from the worker

### Worker Subprocesses

The main command thread spawns a subprocess for each phone. These worker subprocesses are responsible for

-   starting test runs, after receiving an appropriate command from the main process
-   between tests, pinging the phone to ensure that it is still reachable and that the SUT agent is still running
-   recovering phones after detecting errors
-   logging status messages and forwarding them onto the main process

If a worker has a problem communicating with a phone, it attempts to recover the phone by rebooting it. If it cannot fix the problem after 3 tries, the phone is disabled. An email is sent out with the ID of the phone and the reason for disabling it. The phone can be reenabled via the “reenable” command, e.g. after manually fixing it.

### PhoneDash

At the moment, Autophone only runs s1/s2 start-up tests. These are reported to a small web app called phonedash (https://github.com/markrcote/phonedash/), deployed to <http://phonedash.mozilla.org/>.

Getting Involved
----------------

[ctalbert] wrote the original implementation of Autophone and then handed it off to [mcote]. [bc] has been the owner since 2012. Currently [jmaher] and [gbrown] are actively involved in Autophone development.

Source code is at <https://github.com/mozilla/autophone/>

[Autophone bugs]

  [ctalbert]: https://mozillians.org/en-US/u/ctalbert/
  [mcote]: https://mozillians.org/en-US/u/mcote/
  [bc]: https://mozillians.org/en-US/u/bc/
  [jmaher]: https://mozillians.org/en-US/u/jmaher/
  [gbrown]: https://mozillians.org/en-US/u/geoffbrown/
  [Autophone bugs]: https://bugzilla.mozilla.org/buglist.cgi?resolution=---&query_format=advanced&component=Autophone&product=Testing&list_id=13052872

