# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# Parse Autophone logs and output formatted error lines.
#
# This file is dependent upon the format of the Autophone log
# output lines. Should the Autophone log format change, this
# file will need to be updated.

BEGIN {
    line_no = 0
    REMAINDER_LEN = 120
    TEST_LEN = 120
}

function get_remainder(start, len,    remainder) {
    # Beginning with field start, catenate the fields
    # together and return the resulting string.
    remainder=""
    for (i=start; i <= NF; i++) {
        if (remainder)
            remainder = remainder " " $i
        else
            remainder = $i
    }
    remainder = substr(remainder,1, len)
    return remainder
}

function dump_variables() {
    if (DEBUG) {
        printf "device='%s'\n", device
        printf "repo='%s'\n", repo
        printf "buildid='%s'\n", buildid
        printf "buildtype='%s'\n", buildtype
        printf "sdk='%s'\n", sdk
        printf "platform='%s'\n", platform
        printf "testsuite='%s'\n", testsuite
        printf "test='%s'\n", test
        printf "result='%s'\n", result
        printf "remainder='%s'\n", remainder
    }
    # If the result doesn't consist of capital letters, dashes
    # and spaces, skip the output.
    if (result ~ /^[A-Z -]+$/)
        printf "%-45s; %-120s; %-120s; %-20s; %-20s; %14s; %-5s; %-6s; %-22s; %-16s\n", testsuite, test, remainder, result, repo, buildid, buildtype, sdk, platform, device
    else
        if (DEBUG)
            printf "Skipped non-result %s\n", result
}

function dump_fields() {
    printf "========================================\n"
    printf "field_offset = %s\n", field_offset
    printf "0=%s\n", $0
    for (i=1; i <= NF; i++)
        printf "%s='%s'\n", i, $i
    printf "\n"
}

function get_field_number(field_number) {
    # Loglevel DEBUG logs have 3 additional fields following
    # the leading date time. This function hides the complexity
    # of the variable field offsets.
    return field_number + field_offset
}
function get_field(field_number) {
    # Loglevel DEBUG logs have 3 additional fields following
    # the leading date time. This function hides the complexity
    # of the variable field offsets.
    return $(get_field_number(field_number))
}

/^.*$/ {
    line_no++
    if (DEBUG) {
        printf "Line: %s\n", line_no
        dump_fields()
    }
}

# Check if the log file loglevel is DEBUG which includes
# the process id following the datetime at the beginning
# of the log line.
/^[^:]+:[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2},[0-9]{3}[ ]+[0-9]+[ ]+[^ ]/ {
    field_offset = 3
    if (DEBUG) {
        printf "----------------------------------------\n"
        printf "DEBUG LOGLEVEL   : %s, field_offset: %s\n", $0, field_offset
    }
}

# Check if the log file loglevel is not DEBUG which includes
# the process id following the datetime at the beginning
# of the log line.
/^[^:]+:[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2},[0-9]{3}[ ]+[^0-9]/ {
    field_offset = 0
    if (DEBUG) {
        printf "----------------------------------------\n"
        printf "NOTDEBUG LOGLEVEL: %s, field_offset: %s\n", $0, field_offset
    }
}

/ [0-9]+ ERROR/ {
    if (DEBUG) {
        printf "----------------------------------------\n"
        printf "[0-9]+ ERROR: %s\n", $0
    }
    device    = get_field(3)
    repo      = get_field(6)
    buildid   = get_field(7)
    buildtype = get_field(8)
    sdk       = get_field(9)
    platform  = get_field(10)
    testsuite = get_field(11)
    test      = testsuite
    result    = get_field(13)

    if (get_field(15) == "|")
        remainder = get_remainder(get_field_number(16), REMAINDER_LEN, remainder)
    else
        remainder = get_remainder(get_field_number(14), REMAINDER_LEN, remainder)
    test = substr(test, 1, TEST_LEN)
    dump_variables()
    next
}

/TEST-UNEXPECTED-[^|]+ [|] [^|]+ [|]/ {
    if (DEBUG) {
        printf "----------------------------------------\n"
        printf "TEST-UNEXPECTED: %s\n", $0
    }
    device    = get_field(3)
    repo      = get_field(6)
    buildid   = get_field(7)
    buildtype = get_field(8)
    sdk       = get_field(9)
    platform  = get_field(10)
    testsuite = get_field(11)

    if (get_field(13) == "INFO") {
        if (DEBUG) {
            printf "INFO TEST-UNEXPECTED\n"
        }
        result    = get_field(14)
        test      = get_field(16)
        remainder = get_remainder(get_field_number(18), REMAINDER_LEN, remainder)
        remainder = substr(remainder, 1, REMAINDER_LEN)
        test      = substr(test, 1, TEST_LEN)
        dump_variables()
    }
    else {
        if (get_field(12) == "REFTEST") {
            if (DEBUG) {
                printf "REFTEST TEST-UNEXPECTED\n"
            }
            result    = get_field(13)
            test      = get_field(15)
            test      = gensub(/http:\/\/[^\/]+\/tests\//, "", 1, test)
            remainder = get_remainder(get_field_number(17), REMAINDER_LEN, remainder)
            remainder = substr(remainder, 1, REMAINDER_LEN)
            test      = substr(test, 1, TEST_LEN)
            dump_variables()
        }
        else {
            if (DEBUG) {
                printf "TEST-UNEXPECTED\n"
            }
            result    = get_field(12)
            test      = get_field(14)
            remainder = get_remainder(get_field_number(16), REMAINDER_LEN, remainder)
            test      = substr(test, 1, TEST_LEN)
            dump_variables()
        }
    }
    next
}

/PROCESS-CRASH/ {
    if (DEBUG) {
        printf "----------------------------------------\n"
        printf "PROCESS-CRASH: %s\n", $0
    }
    device    = get_field(3)
    repo      = get_field(6)
    buildid   = get_field(7)
    buildtype = get_field(8)
    sdk       = get_field(9)
    platform  = get_field(10)
    testsuite = get_field(11)

    if (get_field(12) == "REFTEST") {
        if (DEBUG) {
            printf "----------------------------------------\n"
            printf "REFTEST PROCESS-CRASH: %s\n", $0
        }
        result    = get_field(13)
        test      = get_field(15)
        test      = gensub(/http:\/\/[^\/]+\/tests\//, "", 1, test)
        remainder = get_remainder(get_field_number(17), REMAINDER_LEN, remainder)
        test      = substr(test, 1, TEST_LEN)
        dump_variables()
    }
    else {
        if (DEBUG) {
            printf "----------------------------------------\n"
            printf "PROCESS-CRASH: %s\n", $0
        }
        result    = get_field(12)
        test      = get_field(14)
        remainder = get_remainder(get_field_number(16), REMAINDER_LEN, remainder)
        test = substr(test, 1, TEST_LEN)
        dump_variables()
    }
    next
}

/I.Gecko.*FATAL ERROR:/ {
    if (DEBUG) {
        printf "----------------------------------------\n"
        printf "I/Gecko.*FATAL ERROR: %s\n", $0
        dump_fields()
    }
    device    = get_field(3)
    repo      = get_field(6)
    buildid   = get_field(7)
    buildtype = get_field(8)
    sdk       = get_field(9)
    platform  = get_field(10)
    testsuite = get_field(11)
    test      = testsuite
    result    = "FATAL ERROR"

    if (get_field(12) == "logcat:")
        adjust = 1
    else
        adjust = 0
    remainder = get_remainder(get_field_number(17+adjust), REMAINDER_LEN, remainder)
    dump_variables()
    next
}

/I.Gecko.*ABORT:/ {
    if (DEBUG) {
        printf "----------------------------------------\n"
        printf "I/Gecko.*ABORT: %s\n", $0
        dump_fields()
    }
    device    = get_field(3)
    repo      = get_field(6)
    buildid   = get_field(7)
    buildtype = get_field(8)
    sdk       = get_field(9)
    platform  = get_field(10)
    testsuite = get_field(11)
    test      = testsuite
    result    = "FATAL ERROR"

    if (get_field(12) == "logcat:")
        adjust = 1
    else
        adjust = 0
    remainder = get_remainder(get_field_number(19+adjust), REMAINDER_LEN, remainder)
    dump_variables()
    next
}

/E.Gecko.*ABORT:/ {
    if (DEBUG) {
        printf "----------------------------------------\n"
        printf "E/Gecko.*ABORT: %s\n", $0
        dump_fields()
    }
    device    = get_field(3)
    repo      = get_field(6)
    buildid   = get_field(7)
    buildtype = get_field(8)
    sdk       = get_field(9)
    platform  = get_field(10)
    testsuite = get_field(11)
    test      = testsuite
    result    = "FATAL ERROR"

    if (get_field(12) == "logcat:")
        adjust = 1
    else
        adjust = 0
    remainder = get_remainder(get_field_number(20+adjust), REMAINDER_LEN, remainder)
    dump_variables()
    next
}

/F\/MOZ_CRASH/ {
    if (DEBUG) {
        printf "----------------------------------------\n"
        printf "F/MOZ_CRASH: %s\n", $0
        dump_fields()
    }
    device    = get_field(3)
    repo      = get_field(6)
    buildid   = get_field(7)
    buildtype = get_field(8)
    sdk       = get_field(9)
    platform  = get_field(10)
    testsuite = get_field(11)
    test      = testsuite
    result    = "FATAL ERROR"

    if (get_field(12) == "logcat:")
        adjust = 1
    else
        adjust = 0
    # Handle the case where we can have either
    # one field such as F/MOZ_CRASH(27723): or
    # two fields such as F/MOZ_CRASH( 2632)
    if ("F/MOZ_CRASH(" == get_field(14 + adjust)) {
        adjust = adjust + 1
    }
    if (DEBUG)
        printf "%s %s adjust %s\n", get_field(15), get_field(15 + adjust), adjust
    remainder = get_remainder(get_field_number(15 + adjust), REMAINDER_LEN, remainder)
    dump_variables()
    next
}

/F\/MOZ_Assert.*Assertion failure:/ {
    if (DEBUG) {
        printf "----------------------------------------\n"
        printf "F/MOZ_Assert.*Assertion failure: %s\n", $0
        dump_fields()
    }
    device    = get_field(3)
    repo      = get_field(6)
    buildid   = get_field(7)
    buildtype = get_field(8)
    sdk       = get_field(9)
    platform  = get_field(10)
    testsuite = get_field(11)
    test      = testsuite
    result    = "FATAL ERROR"

    if (get_field(12) == "logcat:")
        adjust = 1
    else
        adjust = 0
    # Handle the case where we can have either
    # one field such as F/MOZ_Assert(27723): or
    # two fields such as F/MOZ_Assert( 2632)
    if ("F/MOZ_Assert(" == get_field(14 + adjust)) {
        adjust = adjust + 1
    }
    if (DEBUG)
        printf "%s %s adjust %s\n", get_field(15), get_field(15 + adjust), adjust
    remainder = get_remainder(get_field_number(15 + adjust), REMAINDER_LEN, remainder)
    dump_variables()
    next
}
