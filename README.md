# Autophone, a mobile-device manager for automated-test frameworks

** This project is no longer active and has been archived. **

Autophone controls one or more mobile devices via adb. Its primary
goals are to:

* execute arbitrary tests on devices
* provide basic device status
* between tests, verify that devices are still connected and responsive, and,
  if not, attempt to recover them

Autophone does not provide a test framework. Rather, it executes arbitrary
Python code, which can also launch and control subprocesses to execute tests
of any format and design.

The [project page](https://wiki.mozilla.org/Auto-tools/Projects/AutoPhone)
contains more background, goals, implementation notes, and other such
information.

Source code is at https://github.com/mozilla/autophone/

See also [phonedash](https://github.com/markrcote/phonedash) for a basic
results server.

See the files [INSTALL.md](INSTALL.md) and [USAGE.md](USAGE.md) for
more detailed information.

