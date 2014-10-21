# Autophone Production Configuration

These are the configuration files used to run the production instance
of Autophone reporting to phonedash.mozilla.org. Note that several
security-sensitive parameters have been replaced with XXXXX.

## autophone.ini
<pre>
[settings]
ipaddr = xxx.xxx.xxx.xxx
loglevel = INFO
test_path = tests/haxxor_mountainview_manifest.ini
emailcfg = email.ini
enable_pulse = True
pulse_user = XXXXX
pulse_password = XXXXX
repos = mozilla-central mozilla-inbound fx-team b2g-inbound
treeherder_url = https://treeherder.allizom.org
treeherder_credentials_path = XXXXX
devices = devices.ini
</pre>

## devices.ini
<pre>
[nexus-s-2]
serialno=3833F770946F00EC
[nexus-s-3]
serialno=30322BD3F19D00EC
[nexus-s-4]
serialno=32321420ECC000EC
[nexus-s-5]
serialno=3733E29C37C400EC
[nexus-4-jdq39-1]
serialno=0482e519dadfb15b
[nexus-4-jdq39-2]
serialno=04bf53e1115a0439
[nexus-4-jdq39-3]
serialno=04e279c3088a50d7
[nexus-4-jdq39-4]
serialno=01c2cd1d0e9a6c28
[nexus-5-kot49h-1]
serialno=04aaf5290a5cdbea
[nexus-5-kot49h-2]
serialno=04b4e9880a5cddcf
[nexus-5-kot49h-3]
serialno=093d5505012ac894
[nexus-5-kot49h-4]
serialno=094045720b0daa77
[nexus-7-jss15q-1]
serialno=0948fea5
[nexus-7-jss15q-2]
serialno=08e877cd
</pre>

## email.ini
<pre>
[report]
from = XXXX

[email]
dest = XXXX
username = XXXX
password = XXXX
</pre>

## tests/haxxor_mountainview_manifest.ini
<pre>
[smoketest.py]
config = ../configs/smoketest_settings.ini

nexus-s-2 = mozilla-central
nexus-s-3 = mozilla-inbound
nexus-s-4 = fx-team
nexus-s-5 = b2g-inbound

nexus-4-jdq39-1 = mozilla-inbound
nexus-4-jdq39-2 = mozilla-central
nexus-4-jdq39-3 = fx-team
nexus-4-jdq39-4 = b2g-inbound

nexus-5-kot49h-1 = mozilla-inbound
nexus-5-kot49h-2 = mozilla-central
nexus-5-kot49h-3 = fx-team
nexus-5-kot49h-4 = b2g-inbound

nexus-7-jss15q-1 = mozilla-inbound
nexus-7-jss15q-2 = mozilla-central fx-team b2g-inbound

[s1s2test.py]
config = ../configs/s1s2_settings.ini

nexus-s-2 = mozilla-central
nexus-s-3 = mozilla-inbound
nexus-s-4 = fx-team
nexus-s-5 = b2g-inbound

nexus-4-jdq39-1 = mozilla-inbound
nexus-4-jdq39-2 = mozilla-central
nexus-4-jdq39-3 = fx-team
nexus-4-jdq39-4 = b2g-inbound

nexus-5-kot49h-1 = mozilla-inbound
nexus-5-kot49h-2 = mozilla-central
nexus-5-kot49h-3 = fx-team
nexus-5-kot49h-4 = b2g-inbound

nexus-7-jss15q-1 = mozilla-inbound
nexus-7-jss15q-2 = mozilla-central fx-team b2g-inbound

[webappstartup.py]
config = ../configs/webappstartup_settings.ini

nexus-s-2 = mozilla-central
nexus-s-3 = mozilla-inbound
nexus-s-4 = fx-team
nexus-s-5 = b2g-inbound

nexus-4-jdq39-1 = mozilla-inbound
nexus-4-jdq39-2 = mozilla-central
nexus-4-jdq39-3 = fx-team
nexus-4-jdq39-4 = b2g-inbound

nexus-5-kot49h-1 = mozilla-inbound
nexus-5-kot49h-2 = mozilla-central
nexus-5-kot49h-3 = fx-team
nexus-5-kot49h-4 = b2g-inbound

nexus-7-jss15q-1 = mozilla-inbound
nexus-7-jss15q-2 = mozilla-central fx-team b2g-inbound
</pre>

## configs/smoketest_settings.ini
<pre>
[treeherder]
job_name = Autophone Smoketest
job_symbol = s
group_name = Autophone
group_symbol = A
</pre>

## configs/s1s2_settings.ini
<pre>
[paths]
sources = files/base/ files/s1s2/ files/ep1/twitter.com/

[locations]
local =

[tests]
blank = blank.html
twitter = twitter.html

[settings]
iterations = 8
resulturl = http://phonedash.mozilla.org/api/s1s2/
stderrp_accept = 0.5
stderrp_reject = 10.0
stderrp_attempts = 2

[signature]
id = XXXX
key = XXXX

[treeherder]
job_name = Autophone Throbber
job_symbol = t
group_name = Autophone
group_symbol = A
</pre>

## configs/webappstartup_settings.ini
<pre>
[settings]
iterations = 8
resulturl = http://phonedash.mozilla.org/api/s1s2/
stderrp_accept = 0.5
stderrp_reject = 15
stderrp_attempts = 2

[signature]
id = XXXX
key = XXXX

[treeherder]
job_name = Autophone Webappstartup
job_symbol = w
group_name = Autophone
group_symbol = A
</pre>
